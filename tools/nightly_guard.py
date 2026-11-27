"""
夜间改动护栏 —— 两个 WorkBuddy 定时任务共用的「放开权限但不失控」底座。

2026-11-26 起，两个夜间任务被允许改代码、改小 LLM 提示词、改人格卡模板、
甚至调整唤醒层/工具层的架构。权限放开后必须有机器可执行的护栏，因为凌晨没人盯着：

1. 跨进程文件锁：两个任务（0:15 感官 / 0:45 记忆）绝不能同时改同一批文件、
   同时 commit+push。谁先拿到锁谁改，另一个等待。
2. 改动面检查：黑名单（.env / watchdog.py / main.py / self_ops.py / DB / state）
   一律拒绝；未跟踪的新文件默认拒绝；改过的 .py 必须能 py_compile；
   人格模板改了必须能通过 build_card 的 6000 字上限；tuning.json 的每个键
   必须在 tuning.BOUNDS 内、且单晚变动不超过 tuning.STEP_LIMITS。
3. 安全提交：只 add 明确列出的白名单文件，commit 后立刻 push。
   本项目 AUTO_GIT_UPDATE 每 60 秒会 `git stash push` + `git pull --ff-only`，
   pull 失败不会自动 pop —— 不立刻提交并推送，改动就会被吞掉甚至被 reset --hard 抹掉。
   绝不使用 git stash / reset --hard / push --force。
4. 平滑重启：改了 .py 必须重启才生效。只杀 main.py 子进程，绝不碰 watchdog.py 父进程；
   子进程 uptime < 20s 时不动手（否则看门狗判定「启动即崩」会 reset --hard）。
5. 台账：每天写了什么、为什么、落在哪个 commit，全部记进 data/nightly_changes/，
   调参前后值也留一行快照，第二天可以对照，出事可以按 commit 回滚。

用法（解释器必须用 C:/Users/CfLks/AppData/Local/Programs/Python/Python312/python.exe）：

    python tools/nightly_guard.py begin  --owner sense --wait 900
    # ... 这期间改文件、跑脚本、跑测试 ...
    python tools/nightly_guard.py check                    # 输出 JSON，ok=false 就别提交
    python tools/nightly_guard.py finish --owner sense --message "本轮改了什么" [--restart]
    python tools/nightly_guard.py abort  --owner sense    # 中途放弃，释放锁
    python tools/nightly_guard.py status                 # 锁 / HEAD / 最近台账
    python tools/nightly_guard.py health                # 重启后验证看门狗状态
    python tools/nightly_guard.py rollback              # revert 最近一次提交并 push

owner 只能是 sense（感官与工具助手）或 mind（记忆与人格）。
所有命令都只读/只写自己的锁与台账，不会主动往群里发消息，也不会派后台任务。
"""
from __future__ import annotations

import argparse
import json
import os
import py_compile
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import BASE_DIR, DATA_DIR  # noqa: E402
import tuning  # noqa: E402

LOCK_PATH = DATA_DIR / "nightly_guard.lock"
LEDGER_DIR = DATA_DIR / "nightly_changes"
LEDGER_FILE = LEDGER_DIR / "changes.md"
KNOBS_HISTORY = LEDGER_DIR / "knobs_history.jsonl"
WATCHDOG_STATE = DATA_DIR / "watchdog_state.json"

OWNERS = ("sense", "mind")
STALE_SEC = 2700  # 锁超过 45 分钟没人释放，视作上一轮崩了

# 动了这些就别提交：要么是密钥/进程模型/运行时数据，要么会破坏看门狗。
NEVER_EXACT = {
    ".env", ".env.bak", "watchdog.py", "main.py", "self_ops.py",
    "start_wechat.bat", "start_web.bat", "requirements.txt",
    "mcp.json", "dafeiyu.db", "data/dafeiyu.db",
}
NEVER_PREFIX = (
    "data/", "state/", "stickers/", "wechatauto_logs/", ".git/",
    ".workbuddy/", "logs/", "__pycache__/",
)
NEVER_SUFFIX = (".db", ".db.bak", ".bak", ".pyc", ".log")

ALLOW_ROOT_SUFFIX = (".py", ".md", ".txt", ".json", ".bat")
ALLOW_DIRS = ("roles/", "tools/", "adapters/", "docs/", "static/")

PERSONA_FILES = ("roles/大肥鱼.人格.md", "roles/大肥鱼.操作.md")

PS_FIND = r"""
$cands = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like 'python*' -and $_.CommandLine -match 'watchdog\.py'
})
if ($cands.Count -lt 1) { Write-Output 'WATCHDOG_COUNT=0'; exit 2 }
foreach ($w in $cands) {
    $k = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ParentProcessId -eq $w.ProcessId -and $_.Name -like 'python*' -and $_.CommandLine -match 'dafeiyu'
    })
    if ($k.Count -eq 1) {
        $up = ((Get-Date) - $k[0].CreationDate).TotalSeconds
        Write-Output (ConvertTo-Json -Compress @{ watchdog = $w.ProcessId; child = $k[0].ProcessId; uptime = $up; watchdog_count = $cands.Count })
        exit 0
    }
}
Write-Output ('CANDIDATES=' + $cands.Count + ' NO_UNIQUE_CHILD'); exit 3
"""


# ---------------------------------------------------------------- 基础工具

def _git(*args: str, input_text: str = "", check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(BASE_DIR), "-c", "core.quotepath=false", *args],
        input=input_text or None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {(proc.stderr or proc.stdout).strip()[:400]}")
    return (proc.stdout or "").strip()


def _ps(script: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    return proc.returncode, (proc.stdout or "").strip()


def _changed_files() -> list[tuple[str, str]]:
    """用 -z 原始输出解析：普通 status 的第一行前导空格会被 strip 吃掉，导致路径少一个字母。"""
    proc = subprocess.run(
        ["git", "-C", str(BASE_DIR), "-c", "core.quotepath=false",
         "status", "--porcelain", "-z", "-uall"],
        capture_output=True, timeout=60,
    )
    raw = (proc.stdout or b"").decode("utf-8", errors="replace")
    items = [x for x in raw.split("\0") if x]
    rows: list[tuple[str, str]] = []
    i = 0
    while i < len(items):
        item = items[i]
        if len(item) < 4:
            i += 1
            continue
        status, path = item[:2], item[3:]
        # rename / copy 在 -z 下后面紧跟一条「原路径」，跳过它
        if status.strip().startswith(("R", "C")):
            i += 1
        rows.append((status, path.replace("\\", "/")))
        i += 1
    return rows


def _read_lock() -> dict[str, Any]:
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_knobs() -> dict[str, Any]:
    try:
        raw = json.loads((DATA_DIR / "tuning.json").read_text(encoding="utf-8"))
        return raw.get("knobs") if isinstance(raw, dict) and isinstance(raw.get("knobs"), dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _last_knobs() -> dict[str, Any]:
    if not KNOBS_HISTORY.is_file():
        return {}
    try:
        lines = [x for x in KNOBS_HISTORY.read_text(encoding="utf-8").splitlines() if x.strip()]
        return (json.loads(lines[-1]).get("knobs") or {}) if lines else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 检查

def _allowed(path: str) -> tuple[bool, str]:
    p = path.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if p in NEVER_EXACT or p.startswith(NEVER_PREFIX) or p.endswith(NEVER_SUFFIX):
        return False, "命中禁改清单（密钥 / 进程模型 / 运行时数据）"
    if p == ".gitignore":
        return True, ""
    if "/" in p:
        return (True, "") if p.startswith(ALLOW_DIRS) else (False, "不在允许的子目录（roles/tools/adapters/docs/static）")
    return (True, "") if p.endswith(ALLOW_ROOT_SUFFIX) else (False, "根目录只允许 .py/.md/.txt/.json/.bat")


def _verify_persona() -> list[str]:
    """人格模板改了必须还能编译成 ≤6000 字的卡，否则机器人每轮 reply 都会抛异常。"""
    try:
        from roles import build_card as card_mod
        card_mod.build_card()
        return []
    except Exception as exc:
        return [f"人格卡编译失败：{type(exc).__name__}: {exc}"]


def _verify_tuning() -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    knobs = _read_knobs()
    prev = _last_knobs()
    for key, raw in knobs.items():
        if key not in tuning.BOUNDS:
            errors.append(f"tuning.json 出现未知键 {key}（必须先加进 tuning.BOUNDS，否则运行期静默忽略）")
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            errors.append(f"tuning.json 的 {key}={raw!r} 不是数值")
            continue
        low, high = tuning.BOUNDS[key]
        if not (low <= value <= high):
            errors.append(f"tuning.json 的 {key}={value} 越界（允许 {low}~{high}）")
        if key in prev:
            try:
                delta = abs(value - float(prev[key]))
            except (TypeError, ValueError):
                continue
            step = tuning.STEP_LIMITS.get(key)
            if step is not None and delta > step + 1e-6:
                errors.append(
                    f"{key} 单晚变动 {delta:g} 超过步长 {step:g}（{prev[key]} → {value}）；"
                    "想大幅调整请分几晚，或在台账里写明理由并显式跳过本项"
                )
            elif delta > 0:
                notes.append(f"{key}: {prev[key]} → {value}")
        else:
            # 第一次写进 tuning.json 的键没有上一轮可比，但必须进台账——
            # 否则 11-26 那种「实际调了 2 项、台账写调参：无」还会发生。
            notes.append(f"{key}: 新设为 {value:g}")
    return errors, notes


def run_check(allow_new: bool = False) -> dict[str, Any]:
    rows = _changed_files()
    blocked: list[str] = []
    errors: list[str] = []
    changed: list[str] = []
    py_files: list[str] = []

    for status, path in rows:
        untracked = "?" in status
        if untracked and not allow_new:
            blocked.append(f"{path}（未跟踪的新文件；确需新增请显式 --allow-new）")
            continue
        ok, why = _allowed(path)
        if not ok:
            blocked.append(f"{path}（{why}）")
            continue
        changed.append(path)
        if path.endswith(".py") and (BASE_DIR / path).is_file():
            py_files.append(path)

    with tempfile.TemporaryDirectory(prefix="dfy_guard_compile_") as tmp:
        for rel in py_files:
            try:
                py_compile.compile(
                    str(BASE_DIR / rel),
                    cfile=str(Path(tmp) / (Path(rel).name + "c")),
                    doraise=True,
                )
            except Exception as exc:
                errors.append(f"{rel} 语法检查失败：{exc}")

    if any(p in changed for p in PERSONA_FILES):
        errors.extend(_verify_persona())

    tuning_errors, knob_notes = _verify_tuning()
    errors.extend(tuning_errors)

    return {
        "ok": not blocked and not errors,
        "changed": changed,
        "blocked": blocked,
        "errors": errors,
        "knob_notes": knob_notes,
        "head": _git("rev-parse", "HEAD"),
        "lock": _read_lock().get("owner") or "",
    }


# ---------------------------------------------------------------- 锁

def _pid_alive(pid: Any) -> bool:
    """持锁进程还在不在。查不到就当作还活着，宁可等 TTL 也不要抢活锁。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        rc, out = _ps(f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'ALIVE' }} else {{ 'DEAD' }}")
        if rc != 0:
            return True
        return "ALIVE" in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_begin(args: argparse.Namespace) -> int:
    if args.owner not in OWNERS:
        _emit({"ok": False, "error": f"owner 只能是 {OWNERS}"})
        return 2
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    waited = 0.0
    while True:
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = _read_lock()
            age = time.time() - float(info.get("ts") or 0)
            # 进程被任务超时杀掉时锁文件不会自己消失；只等 TTL 会把下一个任务
            # 堵到 1:00。先看持锁 pid 还在不在，死了立刻回收。
            if age > STALE_SEC or not _pid_alive(info.get("pid")):
                try:
                    LOCK_PATH.unlink()
                except OSError:
                    pass
                continue
            if waited >= args.wait:
                _emit({
                    "ok": False,
                    "error": f"另一个任务 {info.get('owner') or '?'} 持锁已 {age:.0f}s，等了 {waited:.0f}s",
                    "hint": "本轮不要改代码；只做只读复盘，或等下一晚",
                })
                return 3
            time.sleep(5)
            waited += 5
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"owner": args.owner, "pid": os.getpid(), "ts": time.time(),
                       "at": datetime.now().isoformat(timespec="seconds")}, f)
        # 上一轮超时被杀时，改动会留在工作区没人管；AUTO_GIT_UPDATE 一 pull 就 stash 吞掉。
        # 开局先把它们摆出来，让任务决定「先收下还是先还原」，别再留给下一轮捡。
        dirty = [f"{status} {path}" for status, path in _changed_files()]
        _emit({
            "ok": True,
            "owner": args.owner,
            "waited_sec": waited,
            "stale_after_sec": STALE_SEC,
            "head": _git("rev-parse", "HEAD"),
            "dirty": dirty,
            "hint": (
                "工作区有未提交改动（多半是上一轮超时遗留）：先看清是什么，"
                "能过 check 就随本轮 finish 一并收下，不能过就 abort 并在汇报里写明"
                if dirty else ""
            ),
        })
        return 0


def _assert_owner(owner: str) -> bool:
    info = _read_lock()
    if info.get("owner") != owner:
        _emit({"ok": False, "error": f"锁不在 {owner} 手里（当前 {info.get('owner') or '无'}）；先 begin"})
        return False
    return True


def cmd_abort(args: argparse.Namespace) -> int:
    info = _read_lock()
    if info and info.get("owner") not in (args.owner, None) and not args.force:
        _emit({"ok": False, "error": f"锁属于 {info.get('owner')}，不给释放"})
        return 2
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    _emit({"ok": True, "released": info.get("owner") or ""})
    return 0


# ---------------------------------------------------------------- 提交 / 台账

def _write_ledger(owner: str, message: str, result: dict[str, Any], commit: str) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    lines = [
        f"\n## {now} · {owner}",
        f"- 说明：{message}",
        f"- 改动文件：{', '.join(result['changed']) or '（仅热加载文件，无 git 改动）'}",
        f"- 提交：{commit or '无（本轮没有需要入库的改动）'}",
        f"- 调参：{'; '.join(result.get('knob_notes') or []) or '无'}",
        f"- 校验：ok={result['ok']}，blocked={len(result['blocked'])}，errors={len(result['errors'])}",
        "",
    ]
    with LEDGER_FILE.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with KNOBS_HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "owner": owner, "commit": commit,
                            "knobs": _read_knobs()}, ensure_ascii=False) + "\n")


def cmd_check(args: argparse.Namespace) -> int:
    result = run_check(allow_new=args.allow_new)
    _emit(result)
    return 0 if result["ok"] else 1


def cmd_health(args: argparse.Namespace) -> int:
    payload = health_payload()
    _emit(payload)
    return 0 if payload["ok"] else 1


def cmd_restart(args: argparse.Namespace) -> int:
    payload = _restart()
    _emit(payload)
    return 0 if payload["ok"] else 1


def cmd_finish(args: argparse.Namespace) -> int:
    if not _assert_owner(args.owner):
        return 2
    result = run_check(allow_new=args.allow_new)
    if not result["ok"]:
        _emit({"ok": False, "stage": "check", **result,
               "hint": "检查没过，没有提交也没有推送；修好后重跑 check/finish，或 abort 放弃"})
        return 4

    commit = ""
    if result["changed"]:
        try:
            _git("add", "--", *result["changed"])
            staged = _git("diff", "--cached", "--name-only")
            if staged:
                _git("commit", "-F", "-", input_text=args.message.strip() + "\n")
                commit = _git("rev-parse", "HEAD")[:7]
                _git("push", "--quiet", "origin", "main")
        except RuntimeError as exc:
            _emit({"ok": False, "stage": "git", "error": str(exc), **result,
                   "hint": "提交/推送失败：不要放着不管，AUTO_GIT_UPDATE 会 stash 吞改动"})
            return 5

    _write_ledger(args.owner, args.message.strip(), result, commit)
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    out = {"ok": True, "commit": commit or "", "pushed": bool(commit),
           "changed": result["changed"], "knob_notes": result["knob_notes"],
           "ledger": str(LEDGER_FILE)}
    if args.restart:
        out["restart"] = _restart()
    _emit(out)
    return 0


# ---------------------------------------------------------------- 重启 / 健康

def _restart() -> dict[str, Any]:
    rc, out = _ps(PS_FIND)
    if rc != 0:
        return {"ok": False, "error": f"找不到唯一的看门狗/子进程：rc={rc} out={out[:200]}"}
    try:
        info = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": f"无法解析进程信息：{out[:200]}"}
    uptime = float(info.get("uptime") or 0)
    if uptime < 25:
        return {"ok": False, "error": f"子进程 uptime={uptime:.0f}s <25s，此刻杀会被看门狗判成启动即崩并 reset --hard"}
    child = int(info["child"])
    rc2, out2 = _ps(f"Stop-Process -Id {child} -Force -ErrorAction Stop; Write-Output STOPPED")
    if rc2 != 0:
        return {"ok": False, "error": f"停止子进程失败：{out2[:200]}"}
    time.sleep(30)
    payload = health_payload(before_pid=child)
    if not payload["ok"]:
        # 看门狗 1.5s 后拉起，但微信重连和 20s 健康窗口可能还没走完，再给一次机会
        time.sleep(20)
        payload = health_payload(before_pid=child)
    return payload


def health_payload(before_pid: int = 0) -> dict[str, Any]:
    head = _git("rev-parse", "HEAD")
    state: dict[str, Any] = {}
    try:
        state = json.loads(WATCHDOG_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    rc, out = _ps(PS_FIND)
    child_now = 0
    watchdog_count = 0
    if rc == 0:
        try:
            proc_info = json.loads(out.splitlines()[-1])
            child_now = int(proc_info.get("child") or 0)
            watchdog_count = int(proc_info.get("watchdog_count") or 0)
        except (ValueError, IndexError, AttributeError):
            pass
    last_good = str(state.get("last_good_commit") or "")
    # HEAD 领先健康版本不一定有问题：只改 tools/、docs/、.gitignore 这类
    # 机器人进程不加载的文件时，本就不该重启。只有改到运行模块才必须重启。
    needs_restart = False
    changed_since_good: list[str] = []
    if last_good and not last_good.startswith(head[:7]):
        diff = _git("diff", "--name-only", f"{last_good[:7]}..{head[:7]}", check=False)
        changed_since_good = [x for x in diff.splitlines() if x.strip()]
        needs_restart = any(
            (p.endswith(".py") and not p.startswith(("tools/", "docs/")))
            or p in (".env", "config.py")
            for p in changed_since_good
        )
    healthy = (
        int(state.get("crash_streak") or 0) == 0
        and (last_good.startswith(head[:7]) or not needs_restart)
        and child_now not in (0, before_pid)
        and watchdog_count <= 1
    )
    hint = "ok=false 时先别继续改：改了运行模块却没重启就补 --restart，或跑 rollback / 人工查日志"
    if watchdog_count > 1:
        hint = (
            f"检测到 {watchdog_count} 个 watchdog 同时运行：它们会互抢 8765 端口，"
            "还会把端口冲突误判成代码崩溃并反复回滚版本，请只保留一个"
        )
    return {
        "ok": healthy,
        "head": head[:7],
        "last_good_commit": last_good[:7],
        "crash_streak": state.get("crash_streak"),
        "old_child_pid": before_pid,
        "new_child_pid": child_now,
        "watchdog_count": watchdog_count,
        "pending_restart": needs_restart,
        "changed_since_good": changed_since_good,
        "hint": hint,
    }


def cmd_rollback(args: argparse.Namespace) -> int:
    """用 revert 留痕回滚，绝不 reset --hard（那会抹掉未提交改动）。"""
    head = _git("rev-parse", "HEAD")
    if not args.yes:
        _emit({"ok": False, "error": f"将 revert {head[:7]} 并 push；确认请加 --yes"})
        return 2
    try:
        _git("revert", "--no-edit", "HEAD")
        _git("push", "--quiet", "origin", "main")
    except RuntimeError as exc:
        _emit({"ok": False, "error": str(exc)})
        return 3
    _emit({"ok": True, "reverted": head[:7], "new_head": _git("rev-parse", "HEAD")[:7],
           "hint": "回滚已推送，等自动更新或手动重启让机器人加载回来"})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    lock = _read_lock()
    tail = ""
    if LEDGER_FILE.is_file():
        lines = LEDGER_FILE.read_text(encoding="utf-8").splitlines()
        tail = "\n".join(lines[-8:])
    _emit({
        "lock": lock,
        "lock_age_sec": round(time.time() - float(lock.get("ts") or 0), 1) if lock else None,
        "head": _git("rev-parse", "HEAD")[:7],
        "changed": [p for _, p in _changed_files()],
        "knobs": _read_knobs(),
        "ledger_tail": tail,
    })
    return 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="大肥鱼夜间改动护栏")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("begin", help="抢跨进程锁，成功后才能改文件")
    b.add_argument("--owner", required=True, choices=OWNERS)
    b.add_argument("--wait", type=float, default=900, help="锁被占时最多等多少秒")
    b.set_defaults(func=cmd_begin)

    c = sub.add_parser("check", help="校验改动面（不写任何东西）")
    c.add_argument("--allow-new", action="store_true", help="允许本次新增未跟踪文件")
    c.set_defaults(func=cmd_check)

    f = sub.add_parser("finish", help="校验 + commit + push + 写台账 + 释放锁")
    f.add_argument("--owner", required=True, choices=OWNERS)
    f.add_argument("--message", required=True, help="本次改动的说明，写进 commit 与台账")
    f.add_argument("--allow-new", action="store_true")
    f.add_argument("--restart", action="store_true", help="提交后平滑重启（改了 .py 才需要）")
    f.set_defaults(func=cmd_finish)

    a = sub.add_parser("abort", help="放弃本轮，释放锁")
    a.add_argument("--owner", required=True, choices=OWNERS)
    a.add_argument("--force", action="store_true")
    a.set_defaults(func=cmd_abort)

    s = sub.add_parser("status", help="看锁 / HEAD / 当前调参 / 台账尾部")
    s.set_defaults(func=cmd_status)

    h = sub.add_parser("health", help="重启后验证看门狗是否已把新 commit 记为健康")
    h.set_defaults(func=cmd_health)

    r = sub.add_parser("restart", help="只杀 main.py 子进程，让 watchdog 拉起新代码")
    r.set_defaults(func=cmd_restart)

    rb = sub.add_parser("rollback", help="revert 最近一次提交并 push")
    rb.add_argument("--yes", action="store_true")
    rb.set_defaults(func=cmd_rollback)

    args = ap.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
