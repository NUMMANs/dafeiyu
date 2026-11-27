"""大肥鱼自更新、自修改、重启与健康状态模块"""

from __future__ import annotations

import json
import logging
import os
import py_compile
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from config import BASE_DIR, DATA_DIR
import gitmeta

logger = logging.getLogger("dafeiyu.self_ops")

RESTART_EXIT_CODE = 42
# Web 端口被占用时的专用退出码：属于环境冲突，不是代码崩溃，看门狗不得据此回滚版本
PORT_BUSY_EXIT_CODE = 78
WATCHDOG_STATE_PATH = DATA_DIR / "watchdog_state.json"
RESTART_NOTICE_PATH = DATA_DIR / "restart_notice.json"


def is_port_in_use(host: str, port: int, timeout: float = 0.4) -> bool:
    """探测本机端口是否已有服务在监听，用于避免两个大肥鱼实例抢 8765。

    以 bind 探测为主：能绑上说明没人监听，绑不上说明被占。比 connect 探测
    稳得多 —— 后者在对方 accept 队列满 / 连接超时时会把「有人占着」误判成空闲。
    再补一次 connect 兜底，覆盖绑在不同地址族上的情况。
    """
    probe = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
            probe_sock.bind((probe, port))
    except OSError:
        return True
    try:
        with socket.create_connection((probe, port), timeout=timeout):
            return True
    except OSError:
        return False

_SKIP_COMPILE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "data",
    "state",
    "wechatauto_logs",
    "node_modules",
}

_PROTECTED_EXACT = {".env", "watchdog.py", "self_ops.py"}
_PROTECTED_PREFIXES = (".git/", ".env.", "data/", "state/", "wechatauto_logs/")

_restart_lock = threading.Lock()
_restart_pending = False


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(args: list[str], timeout: int = 25) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *gitmeta.net_args(BASE_DIR), *args],
            cwd=str(BASE_DIR),
            env=_git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} 超时 ({timeout}s)"
    except Exception as exc:
        return 1, "", str(exc)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def load_watchdog_state() -> dict[str, Any]:
    if not WATCHDOG_STATE_PATH.is_file():
        return {}
    try:
        data = json.loads(WATCHDOG_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_watchdog_state(state: dict[str, Any]) -> None:
    try:
        _atomic_write_text(
            WATCHDOG_STATE_PATH,
            json.dumps(state, ensure_ascii=False, indent=2),
        )
    except Exception as exc:
        logger.warning("保存 watchdog_state 失败: %s", exc)


def git_current_commit(short: bool = False) -> str:
    if not (BASE_DIR / ".git").exists():
        return ""
    args = ["rev-parse", "--short", "HEAD"] if short else ["rev-parse", "HEAD"]
    code, out, _ = _run_git(args, timeout=10)
    return out if code == 0 else ""


def check_remote_update() -> tuple[bool, str]:
    """检查**当前分支的上游**是否有尚未拉取的新提交。

    以前这里写死 origin/main。切到别的分支后，HEAD 永远不等于 origin/main，
    于是每一轮都判定「远程有新提交」，再 ``git pull --ff-only origin main``
    把刚切过去的分支直接快进回 main——等于分支白切。
    现在跟着当前分支走：在 Smila_FZ 上就只比对 origin/Smila_FZ。
    """
    if not (BASE_DIR / ".git").exists():
        return False, "当前目录不是 Git 仓库"

    branch = gitmeta.branch(BASE_DIR)
    if not branch or branch == "HEAD":
        return False, "当前处于 detached HEAD，跳过自动更新"
    upstream = gitmeta.upstream_branch(BASE_DIR)
    if not upstream:
        return False, f"分支 {branch} 没有上游，跳过自动更新"
    remote_ref = f"origin/{upstream}"

    code, _, err = _run_git(["fetch", "--quiet", "origin", upstream], timeout=60)
    if code != 0:
        return False, f"git fetch 失败: {err or '未知错误'}"

    code_local, local_head, err_local = _run_git(["rev-parse", "HEAD"], timeout=10)
    code_remote, remote_head, err_remote = _run_git(["rev-parse", remote_ref], timeout=10)
    if code_local != 0 or code_remote != 0:
        return False, f"解析提交失败: {err_local or err_remote}"

    if local_head == remote_head:
        return False, f"已是最新版本 ({local_head[:7]})"

    state = load_watchdog_state()
    if state.get("failed_commit") == remote_head:
        return False, f"远程提交 {remote_head[:7]} 曾导致启动崩溃并被自动回滚，已跳过拉取"

    _, summary, _ = _run_git(["log", "-1", "--oneline", remote_ref], timeout=10)
    return True, f"{branch}: {summary or remote_head[:7]}"


def compile_all_python() -> tuple[bool, str]:
    """全量语法检查项目中所有 Python 文件。"""
    py_files: list[Path] = []
    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in _SKIP_COMPILE_DIRS and not d.startswith(".")]
        for fname in files:
            if fname.endswith(".py"):
                py_files.append(Path(root) / fname)

    errors: list[str] = []
    for py_path in sorted(py_files):
        try:
            py_compile.compile(str(py_path), doraise=True)
        except py_compile.PyCompileError as exc:
            rel = py_path.relative_to(BASE_DIR).as_posix()
            errors.append(f"{rel}: {exc.msg}")
        except Exception as exc:
            rel = py_path.relative_to(BASE_DIR).as_posix()
            errors.append(f"{rel}: {exc}")

    if errors:
        return False, "语法检查未通过:\n" + "\n".join(errors[:5])
    return True, f"已检查 {len(py_files)} 个 Python 文件，全部语法正常"


def git_pull_and_check(auto_pip: bool = True) -> tuple[bool, str]:
    """从 origin/main 拉取更新，按需更新依赖并校验所有 Python 文件语法。"""
    if not (BASE_DIR / ".git").exists():
        return False, "当前目录不是 Git 仓库，无法拉取更新"

    before_commit = git_current_commit()
    branch = gitmeta.branch(BASE_DIR)
    if not branch or branch == "HEAD":
        return False, "当前处于 detached HEAD，不知道要拉哪个分支，已跳过"
    upstream = gitmeta.upstream_branch(BASE_DIR)
    if not upstream:
        return False, f"分支 {branch} 还没有上游，已跳过（先 git push -u origin {branch}）"
    remote_ref = f"origin/{upstream}"

    code_fetch, _, err_fetch = _run_git(["fetch", "--quiet", "origin", upstream], timeout=60)
    if code_fetch != 0:
        return False, f"git fetch 失败: {err_fetch or '网络或认证异常'}"

    code_remote, remote_head, _ = _run_git(["rev-parse", remote_ref], timeout=10)
    if code_remote == 0 and before_commit and before_commit == remote_head:
        return False, f"当前已经是最新提交 ({before_commit[:7]})，无需重启"

    stash_note = ""
    _, dirty_out, _ = _run_git(["status", "--porcelain", "--untracked-files=no"], timeout=10)
    if dirty_out.strip():
        stash_msg = f"auto-before-pull-{time.strftime('%Y%m%d_%H%M%S')}"
        code_stash, _, stash_err = _run_git(["stash", "push", "-m", stash_msg], timeout=15)
        if code_stash == 0:
            stash_note = f"（已自动暂存本地修改: {stash_msg}）"
        else:
            logger.warning("拉取前自动 stash 失败: %s", stash_err)

    code_pull, pull_out, pull_err = _run_git(["pull", "--ff-only", "origin", upstream], timeout=90)
    if code_pull != 0:
        return False, (
            f"git pull --ff-only origin {upstream} 失败（可能存在本地未提交改动或分叉）:\n"
            f"{pull_err or pull_out}"
        )

    after_commit = git_current_commit()
    ok_compile, compile_msg = compile_all_python()
    if not ok_compile:
        if after_commit and after_commit != before_commit:
            state = load_watchdog_state()
            state["failed_commit"] = after_commit
            save_watchdog_state(state)
        if before_commit:
            _run_git(["reset", "--hard", before_commit], timeout=20)
        return False, f"新代码存在语法错误，已自动回滚到 {before_commit[:7]}：\n{compile_msg}"

    pip_note = ""
    if auto_pip and before_commit and after_commit and before_commit != after_commit:
        _, diff_files, _ = _run_git(
            ["diff", "--name-only", before_commit, after_commit],
            timeout=15,
        )
        changed = {line.strip() for line in diff_files.splitlines() if line.strip()}
        if "requirements.txt" in changed:
            try:
                pip_proc = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                    cwd=str(BASE_DIR),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                )
                if pip_proc.returncode == 0:
                    pip_note = "，已同步更新 requirements.txt 依赖"
                else:
                    pip_note = f"，但依赖安装异常: {(pip_proc.stderr or pip_proc.stdout)[-200:]}"
            except Exception as exc:
                pip_note = f"，但依赖安装异常: {exc}"

    _, commit_msg, _ = _run_git(["log", "-1", "--oneline", "HEAD"], timeout=10)
    state = load_watchdog_state()
    if state.get("failed_commit") == after_commit:
        state["failed_commit"] = ""
        save_watchdog_state(state)

    return True, f"已更新至 {commit_msg or after_commit[:7]}{pip_note}{stash_note}；{compile_msg}"


def _resolve_editable_path(rel_path: str) -> tuple[Path | None, str, str]:
    raw = (rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        return None, "", "文件路径不能为空"

    base_resolved = BASE_DIR.resolve()
    try:
        target_path = (BASE_DIR / raw).resolve()
    except Exception as exc:
        return None, "", f"非法路径 {rel_path}: {exc}"

    if not target_path.is_relative_to(base_resolved):
        return None, "", f"拒绝修改项目目录之外的路径: {rel_path}"

    norm_rel = target_path.relative_to(base_resolved).as_posix()
    if norm_rel in _PROTECTED_EXACT or any(norm_rel.startswith(p) for p in _PROTECTED_PREFIXES):
        return None, norm_rel, f"受保护文件禁止自修改: {norm_rel}"

    return target_path, norm_rel, ""


def self_edit_code(rel_path: str, target: str, replacement: str) -> str:
    """安全修改项目内代码文件：原子写入并在 .py 修改后全量语法校验，失败自动回滚。"""
    target_path, norm_rel, err = _resolve_editable_path(rel_path)
    if err or target_path is None:
        return err

    existed = target_path.exists()
    original_content: str | None = None

    if existed:
        if not target_path.is_file():
            return f"目标不是普通文件: {norm_rel}"
        try:
            original_content = target_path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"读取原文件失败 {norm_rel}: {exc}"

        if not target:
            return f"文件 {norm_rel} 已存在，请提供精确的 target 原文片段以执行替换"
        matches = original_content.count(target)
        if matches == 0:
            return f"未在 {norm_rel} 中找到匹配的 target 片段，未做任何修改"
        if matches > 1:
            return f"在 {norm_rel} 中找到 {matches} 处匹配的 target，请补充上下文使其唯一"
        new_content = original_content.replace(target, replacement, 1)
    else:
        if target:
            return f"文件不存在且指定了非空 target: {norm_rel}"
        new_content = replacement

    try:
        _atomic_write_text(target_path, new_content)
    except Exception as exc:
        return f"写入 {norm_rel} 失败: {exc}"

    if target_path.suffix.lower() == ".py":
        ok_compile, compile_msg = compile_all_python()
        if not ok_compile:
            try:
                if existed and original_content is not None:
                    _atomic_write_text(target_path, original_content)
                elif not existed and target_path.exists():
                    target_path.unlink()
            except Exception as rb_exc:
                return f"语法校验失败且回滚失败: {compile_msg} | 回滚异常: {rb_exc}"
            return f"修改 {norm_rel} 后语法检查失败，已自动回滚：\n{compile_msg}"
        return f"已成功修改 {norm_rel} 并通过全量 Python 语法校验（{compile_msg}）。如需立即生效，请调用 self_restart。"

    return f"已成功写入 {norm_rel}。"


def save_restart_notice(group_id: str, msg: str) -> None:
    payload = {
        "group_id": (group_id or "").strip(),
        "msg": (msg or "").strip(),
        "ts": time.time(),
    }
    if not payload["msg"]:
        return
    try:
        _atomic_write_text(
            RESTART_NOTICE_PATH,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
    except Exception as exc:
        logger.warning("保存重启通知失败: %s", exc)


def consume_restart_notice() -> dict[str, Any] | None:
    if not RESTART_NOTICE_PATH.is_file():
        return None
    try:
        raw = RESTART_NOTICE_PATH.read_text(encoding="utf-8")
        RESTART_NOTICE_PATH.unlink(missing_ok=True)
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("msg"):
            return data
    except Exception as exc:
        logger.warning("读取重启通知失败: %s", exc)
        try:
            RESTART_NOTICE_PATH.unlink(missing_ok=True)
        except Exception:
            pass
    return None


def request_restart(
    reason: str = "",
    notify_group: str = "",
    delay_sec: float = 2.0,
    notice_msg: str = "",
) -> str:
    """保存重启后问候通知，并在短暂延迟后以退出码 42 退出，由 watchdog.py 自动拉起。"""
    global _restart_pending
    delay = max(1.5, float(delay_sec))
    commit_short = git_current_commit(short=True) or "local"
    reason_text = (reason or "收到重启指令").strip()
    final_notice = (
        (notice_msg or "").strip()
        or f"鱼重启回来啦~（{reason_text}，版本 {commit_short}）"
    )
    save_restart_notice(notify_group, final_notice)

    with _restart_lock:
        if _restart_pending:
            return f"重启已排队，将在约 {delay:.1f} 秒内重启（原因：{reason_text}）"
        _restart_pending = True

        def _do_exit() -> None:
            logger.info("准备退出以触发看门狗重启 (exit=%s, reason=%s)", RESTART_EXIT_CODE, reason_text)
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(RESTART_EXIT_CODE)

        timer = threading.Timer(delay, _do_exit)
        timer.daemon = True
        timer.start()

    return f"已安排在 {delay:.1f} 秒后重启进程（原因：{reason_text}，当前版本 {commit_short}）"
