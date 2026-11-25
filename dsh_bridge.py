"""DeepSeek Harness (DSH) CLI 异步后台任务桥接与断线续投模块。"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import BASE_DIR, DATA_DIR, settings

logger = logging.getLogger("dafeiyu.dsh")

DSH_JOBS_DIR = DATA_DIR / "dsh_jobs"
_IO_LOCK = threading.Lock()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def find_dsh_binary(custom_bin: str = "") -> str | None:
    """查找可用的 DSH CLI 可执行文件或 .cmd 路径。"""
    candidates: list[str] = []
    for raw in (custom_bin, settings.DSH_BIN, "dsh", "dsh.cmd", "dsh.exe"):
        val = (raw or "").strip()
        if val and val not in candidates:
            candidates.append(val)

    for cand in candidates:
        cand_path = Path(cand).expanduser()
        if cand_path.is_file():
            return str(cand_path.resolve())
        resolved = shutil.which(cand)
        if resolved:
            return resolved
    return None


def is_dsh_available() -> tuple[bool, str]:
    if not settings.ENABLE_DSH:
        return False, "配置已关闭 DSH（ENABLE_DSH=false）"
    found = find_dsh_binary()
    if found:
        return True, found
    return (
        False,
        "未检测到 DSH CLI（请先确认已安装 Node.js 并执行 npm install -g @deepseek-ai/dsh，或在 .env 设置 DSH_BIN）",
    )


def _iter_job_dirs() -> list[Path]:
    if not DSH_JOBS_DIR.is_dir():
        return []
    dirs = [p for p in DSH_JOBS_DIR.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.name, reverse=True)
    return dirs


def _is_valid_persisted_session_id(sid: str) -> bool:
    clean = (sid or "").strip()
    if not clean:
        return False
    # 兼容清理早期版本本地伪造的 dfy-xxxxxxxx 会话 ID（DSH 不接受未创建的自定义 session_id）
    if clean.startswith("dfy-"):
        return False
    return True


def get_last_session_id(group_id: str = "") -> str:
    """获取指定群最近一次 DSH 任务真实落盘的 session_id。"""
    for jdir in _iter_job_dirs():
        job = _read_json(jdir / "job.json")
        if not job:
            continue
        if group_id and job.get("group_id") != group_id:
            continue
        done = _read_json(jdir / "done.json")
        for candidate in (
            (done or {}).get("session_id"),
            job.get("session_id"),
        ):
            sid = str(candidate or "").strip()
            if _is_valid_persisted_session_id(sid):
                return sid
    return ""


def get_last_job_context(group_id: str = "") -> dict[str, Any] | None:
    """获取指定群最近一次已完成的 DSH 任务摘要（用于无持久化 session_id 时的续聊兜底）。"""
    for jdir in _iter_job_dirs():
        job = _read_json(jdir / "job.json")
        if not job:
            continue
        if group_id and job.get("group_id") != group_id:
            continue
        done = _read_json(jdir / "done.json")
        if not done or done.get("status") != "done":
            continue
        return {
            "job_id": str(done.get("job_id") or job.get("job_id") or jdir.name),
            "task": str(job.get("task") or ""),
            "summary": str(done.get("summary") or ""),
            "session_id": str(done.get("session_id") or job.get("session_id") or "").strip(),
        }
    return None


def _resolve_workdir(workdir: str = "") -> Path:
    raw = (workdir or settings.DSH_WORKDIR or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    return BASE_DIR.resolve()


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            return str(pid) in (proc.stdout or "")
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def submit_dsh_job(
    group_id: str,
    sender: str,
    task: str,
    session_id: str = "",
    workdir: str = "",
    mode: str = "",
    dsh_bin_override: str = "",
) -> tuple[bool, dict[str, Any] | str]:
    """将耗时编程/重任务异步派发到脱离进程树的 DSH 后台执行，立即返回。"""
    if not settings.ENABLE_DSH:
        return False, "当前配置已禁用 DSH（ENABLE_DSH=false）"

    clean_task = (task or "").strip()
    if not clean_task:
        return False, "任务内容不能为空"

    dsh_bin = find_dsh_binary(dsh_bin_override)
    if not dsh_bin:
        return False, "未找到 DSH CLI，请确认已全局安装 @deepseek-ai/dsh 或检查 DSH_BIN 配置"

    req_sid = (session_id or "").strip()
    prompt_text = clean_task
    if req_sid.lower() == "last":
        resolved_sid = get_last_session_id(group_id)
        if not resolved_sid:
            prev = get_last_job_context(group_id)
            if prev:
                prev_task = prev["task"][:300]
                prev_summary = prev["summary"][:900]
                prompt_text = (
                    f"[续接上一轮任务上下文（#{prev['job_id']}）]\n"
                    f"上一轮任务：{prev_task}\n"
                    f"上一轮结果摘要：\n{prev_summary}\n\n"
                    f"[本轮追问/追加要求]\n{clean_task}"
                )
    elif _is_valid_persisted_session_id(req_sid):
        resolved_sid = req_sid
    else:
        # 新建会话必须省略 --session-id，由 DSH 自行创建会话
        resolved_sid = ""

    resolved_workdir = _resolve_workdir(workdir)
    resolved_mode = (mode or settings.DSH_MODE or "danger-full-access").strip()
    job_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    job_dir = DSH_JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    job_payload: dict[str, Any] = {
        "job_id": job_id,
        "group_id": (group_id or "").strip(),
        "sender": (sender or "").strip(),
        "session_id": resolved_sid,
        "task": clean_task,
        "dsh_bin": dsh_bin,
        "mode": resolved_mode,
        "approval_policy": settings.DSH_APPROVAL_POLICY,
        "model": settings.DSH_MODEL,
        "workdir": str(resolved_workdir),
        "timeout_sec": settings.DSH_TIMEOUT_SEC,
        "created_at": time.time(),
    }

    with _IO_LOCK:
        _atomic_write_text(job_dir / "job.json", json.dumps(job_payload, ensure_ascii=False, indent=2))
        _atomic_write_text(job_dir / "prompt.txt", prompt_text)

    runner_script = BASE_DIR / "dsh_worker_runner.py"
    popen_kwargs: dict[str, Any] = {
        "cwd": str(BASE_DIR),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        popen_kwargs["creationflags"] = flags
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen([sys.executable, str(runner_script), str(job_dir)], **popen_kwargs)
    except Exception as exc:
        return False, f"启动 DSH 后台进程失败: {exc}"

    logger.info(
        "🚀 已派发 DSH 后台任务 job=%s session=%s sender=%s group=%s",
        job_id,
        resolved_sid,
        sender,
        group_id,
    )
    return True, job_payload


def list_dsh_jobs(group_id: str = "", limit: int = 8) -> list[dict[str, Any]]:
    """列出运行中与最近完成的 DSH 任务状态。"""
    results: list[dict[str, Any]] = []
    for jdir in _iter_job_dirs():
        job = _read_json(jdir / "job.json")
        if not job:
            continue
        if group_id and job.get("group_id") != group_id:
            continue

        done = _read_json(jdir / "done.json")
        running = _read_json(jdir / "running.json")

        if done:
            status = str(done.get("status") or "done")
            duration = done.get("duration_sec", 0.0)
            summary = str(done.get("summary") or "")
        elif running:
            pid = int(running.get("pid") or 0)
            elapsed = round(time.time() - float(running.get("started_at") or time.time()), 1)
            status = "running" if _pid_is_alive(pid) else "stalled"
            duration = elapsed
            summary = f"运行中 ({elapsed}s)"
        else:
            status = "queued"
            duration = 0.0
            summary = "排队启动中"

        results.append(
            {
                "job_id": job.get("job_id", jdir.name),
                "session_id": (done or {}).get("session_id") or job.get("session_id", ""),
                "group_id": job.get("group_id", ""),
                "sender": job.get("sender", ""),
                "task": str(job.get("task") or ""),
                "status": status,
                "created_at": job.get("created_at", 0.0),
                "duration_sec": duration,
                "summary": summary,
            }
        )
        if len(results) >= max(1, limit):
            break
    return results


def pop_finished_jobs() -> list[dict[str, Any]]:
    """读取并标记所有尚未投递的已完成 DSH 任务（保证重启后也能续投）。"""
    finished: list[dict[str, Any]] = []
    with _IO_LOCK:
        for jdir in _iter_job_dirs():
            done_path = jdir / "done.json"
            done = _read_json(done_path)
            if not done or done.get("delivered"):
                continue
            done["delivered"] = True
            _atomic_write_text(done_path, json.dumps(done, ensure_ascii=False, indent=2))
            done["_job_dir"] = str(jdir)
            finished.append(done)

    finished.sort(key=lambda item: float(item.get("finished_at") or 0.0))
    return finished


def format_job_delivery(done_data: dict[str, Any]) -> tuple[str, Path | None]:
    """格式化适合发到微信群的任务结果文本，长输出额外生成 .txt 附件。"""
    job_id = str(done_data.get("job_id") or "unknown")
    session_id = str(done_data.get("session_id") or "")
    status = str(done_data.get("status") or "done")
    duration = done_data.get("duration_sec", 0.0)
    task = str(done_data.get("task") or "").strip()
    summary = str(done_data.get("summary") or "").strip()
    task_preview = task[:60] + ("..." if len(task) > 60 else "")

    if status == "done":
        header = f"✅ 后台 DSH 任务完成（#{job_id}｜耗时 {duration}s）"
    elif status == "timeout":
        header = f"⏱️ 后台 DSH 任务超时（#{job_id}｜耗时 {duration}s）"
    else:
        header = f"⚠️ 后台 DSH 任务执行异常（#{job_id}｜退出码 {done_data.get('exit_code')}）"

    attach_path: Path | None = None
    job_dir_str = str(done_data.get("_job_dir") or "").strip()
    job_dir = Path(job_dir_str) if job_dir_str else (DSH_JOBS_DIR / job_id)
    stdout_path = job_dir / "stdout.txt"
    full_stdout = stdout_path.read_text(encoding="utf-8", errors="replace").strip() if stdout_path.is_file() else ""

    if len(full_stdout) > 700:
        attach_file = job_dir / f"dsh_result_{job_id}.txt"
        attach_file.write_text(full_stdout, encoding="utf-8")
        attach_path = attach_file

    body_text = summary[:650] + ("..." if len(summary) > 650 else "")
    lines = [header, f"📝 任务：{task_preview}"]
    if session_id:
        lines.append(f"🔗 会话：{session_id}（发 `/dsh 续 补充要求` 可继续追问）")
    elif status == "done":
        lines.append("💡 发 `/dsh 续 补充要求` 可基于本轮结果继续追问")
    if body_text:
        lines.append(f"\n{body_text}")
    if attach_path is not None:
        lines.append("\n📎 完整输出较长，已随附结果文本文件。")

    return "\n".join(lines), attach_path


# ======================================================================
# 机械汇报 txt + DSH 产出文件回传
# ======================================================================

_STATUS_LABEL = {"done": "完成", "timeout": "超时", "failed": "执行异常", "error": "执行异常"}

# 值得往群里回传的产出文件类型（图片/文档/表格/代码/压缩包）
_ARTIFACT_EXTS = {
    ".md", ".txt", ".csv", ".json", ".xlsx", ".xls", ".docx", ".doc",
    ".pdf", ".pptx", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".zip", ".py",
    ".html", ".htm", ".yaml", ".yml", ".log", ".sql", ".drawio", ".svg",
}

# 目录扫描时跳过的路径片段：缓存、依赖、数据库和本模块自己的任务目录
_SKIP_PATH_PARTS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode",
    "dsh_jobs", "wechatauto_logs", "archive", "site-packages",
}

# 浅扫描额外要躲开的目录：这里放的是机器人自己的运行数据，不是任务产出
_SCAN_SKIP_PARTS = _SKIP_PATH_PARTS | {"data", "state", "logs", "models", "media"}

_SKIP_NAME_SUFFIXES = (".db", ".db-wal", ".db-shm", ".bak", ".pyc", ".tmp", ".log.old", ".jsonl")

# DSH 输出里常见的两种路径写法：反引号包裹的路径、裸盘符路径
_BACKTICK_RE = re.compile(r"`([^`\n]{4,300})`")
_ABS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'`<>|*?\n\r]{2,300}")


def _job_dir_of(done_data: dict[str, Any]) -> Path:
    job_dir_str = str(done_data.get("_job_dir") or "").strip()
    job_id = str(done_data.get("job_id") or "unknown")
    return Path(job_dir_str) if job_dir_str else (DSH_JOBS_DIR / job_id)


def _read_job_meta(job_dir: Path) -> dict[str, Any]:
    return _read_json(job_dir / "job.json") or {}


# ----------------------------------------------------------------------
# 本次任务花了多少钱
# ----------------------------------------------------------------------
# DeepSeek 官方价，单位「元 / 百万 tokens」（2026-09-10 12:00 起执行，deepseek-flash）：
#             缓存命中   缓存未命中   输出
#   空闲       0.02       1.0        4.0
#   高峰       0.04       2.0        8.0     ← 高峰＝工作日的 9:00-12:00、14:00-18:00（北京时间）
# deepseek-v4-pro 自 2026-09-14 起整体路由到 flash 并按其单价计费，所以统一用这张表算。
_PRICE_FLASH = {"hit": (0.02, 0.04), "miss": (1.0, 2.0), "out": (4.0, 8.0)}
_PEAK_WINDOWS = ((9 * 60, 12 * 60), (14 * 60, 18 * 60))
_CST = timezone(timedelta(hours=8))


def _is_peak_hour(ts: float) -> bool:
    """北京时间工作日 9-12 点、14-18 点为高峰，其余（含周末）算空闲。"""
    dt = datetime.fromtimestamp(ts or time.time(), _CST)
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    return any(start <= minutes < end for start, end in _PEAK_WINDOWS)


def job_cost(done_data: dict[str, Any]) -> dict[str, Any] | None:
    """按 done.json 里记的 token 账算这次任务的 API 花费（元）。"""
    usage = done_data.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None

    hit_tokens = int(usage.get("cacheReadTokens") or 0)
    miss_tokens = int(usage.get("uncachedInputTokens") or 0)
    out_tokens = int(usage.get("outputTokens") or 0)

    peak = _is_peak_hour(float(done_data.get("started_at") or 0.0))
    idx = 1 if peak else 0
    hit_cost = hit_tokens * _PRICE_FLASH["hit"][idx] / 1_000_000
    miss_cost = miss_tokens * _PRICE_FLASH["miss"][idx] / 1_000_000
    out_cost = out_tokens * _PRICE_FLASH["out"][idx] / 1_000_000

    return {
        "total": hit_cost + miss_cost + out_cost,
        "hit_cost": hit_cost,
        "miss_cost": miss_cost,
        "out_cost": out_cost,
        "hit_tokens": hit_tokens,
        "miss_tokens": miss_tokens,
        "out_tokens": out_tokens,
        "peak": peak,
    }


def format_cost_line(done_data: dict[str, Any]) -> str:
    """一行话把花费和账目说清，放在机械汇报的第一行。"""
    cost = job_cost(done_data)
    if cost is None:
        return "本次花费：未知（没拿到 DSH 的 token 统计，多半是任务没真正跑起来）"

    model = str(done_data.get("model") or "deepseek-flash")
    tier = "高峰价" if cost["peak"] else "空闲价"
    return (
        f"本次花费：¥{cost['total']:.4f}"
        f"（{model} {tier}｜输入命中 {cost['hit_tokens']:,} / 未命中 {cost['miss_tokens']:,}"
        f" / 输出 {cost['out_tokens']:,} tokens）"
    )


def build_job_report_file(done_data: dict[str, Any], artifacts: list[Path] | None = None) -> Path | None:
    """
    把一次 DSH 任务的全部机械信息写成 txt，原样发给群，不经过任何模型。

    以前这些内容被拼成一条几百字的微信群消息，markdown 星号反引号原样露出、
    Windows 路径整条摆在正文里，失败时更糟——`dsh: session "dfy-xxx" does not exist`
    这种英文报错直接甩群里。现在聊天部分交给大肥鱼用自己的话说，这些原始信息
    一律落到附件里，想看细节的人自己点开。
    """
    job_dir = _job_dir_of(done_data)
    job_id = str(done_data.get("job_id") or job_dir.name)
    job_meta = _read_job_meta(job_dir)
    status = str(done_data.get("status") or "done")
    label = _STATUS_LABEL.get(status, status)

    started_at = float(done_data.get("started_at") or job_meta.get("created_at") or 0.0)
    finished_at = float(done_data.get("finished_at") or 0.0)

    def _fmt_ts(ts: float) -> str:
        if ts <= 0:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    stdout_text = ""
    stderr_text = ""
    for name, holder in (("stdout.txt", "out"), ("stderr.txt", "err")):
        path = job_dir / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if holder == "out":
                stdout_text = text
            else:
                stderr_text = text

    lines = [
        format_cost_line(done_data),
        "=" * 64,
        "大肥鱼 · DSH 后台任务机械汇报（本文件由代码生成，未经模型改写）",
        "=" * 64,
        "",
        f"任务编号：{job_id}",
        f"执行状态：{label}（status={status}）",
        f"退出码：{done_data.get('exit_code')}",
        f"派单人：{done_data.get('sender') or job_meta.get('sender') or '-'}",
        f"会话 ID：{done_data.get('session_id') or '-'}",
        f"开始时间：{_fmt_ts(started_at)}",
        f"结束时间：{_fmt_ts(finished_at)}",
        f"耗时：{done_data.get('duration_sec', 0)} 秒",
        f"工作目录：{job_meta.get('workdir') or '-'}",
        f"权限模式：{job_meta.get('mode') or '-'}",
        "",
        "-" * 64,
        "【任务原文】",
        "-" * 64,
        str(done_data.get("task") or job_meta.get("task") or "").strip() or "(空)",
        "",
        "-" * 64,
        "【结果摘要】",
        "-" * 64,
        str(done_data.get("summary") or "").strip() or "(无)",
        "",
    ]

    if artifacts:
        lines.extend(
            ["-" * 64, "【本次回传的产出文件】", "-" * 64]
            + [f"- {p}" for p in artifacts]
            + [""]
        )

    lines.extend(
        [
            "-" * 64,
            f"【完整标准输出】共 {len(stdout_text)} 字符",
            "-" * 64,
            stdout_text.strip() or "(空)",
            "",
        ]
    )
    if stderr_text.strip():
        lines.extend(["-" * 64, "【标准错误输出】", "-" * 64, stderr_text.strip(), ""])
    lines.append("=" * 64)

    report_path = job_dir / f"报告_{job_id}.txt"
    try:
        _atomic_write_text(report_path, "\n".join(lines))
        return report_path
    except Exception as exc:
        logger.warning("生成 DSH 机械汇报 txt 失败: %s", exc)
        return None


def _looks_like_artifact(path: Path, earliest_mtime: float, latest_mtime: float, max_bytes: int) -> bool:
    try:
        if not path.is_file():
            return False
        name_low = path.name.lower()
        if name_low.endswith(_SKIP_NAME_SUFFIXES):
            return False
        if path.suffix.lower() not in _ARTIFACT_EXTS:
            return False
        st = path.stat()
        if st.st_size <= 0 or st.st_size > max_bytes:
            return False
        # 只认「任务执行期间落盘」的文件：下界卡在任务开始前 30 秒（够容时钟误差，
        # 又不会把任务开始前就存在的老文件算进来），上界卡在结束后 3 分钟——否则事后
        # 被改动的其他文件（比如我自己在改的代码）也会被当成这次任务的产出发进群。
        if st.st_mtime < earliest_mtime - 30:
            return False
        if latest_mtime > 0 and st.st_mtime > latest_mtime + 180:
            return False
        return True
    except Exception:
        return False


def collect_artifacts(
    done_data: dict[str, Any],
    limit: int = 5,
    max_mb: int = 20,
    allow_scan: bool = True,
) -> list[Path]:
    """
    找出这次任务真正产出的文件，供群里回传。

    两条路，优先第一条（准）：
    1. 从 DSH 自己的输出里抠路径——它的收尾语通常就是「文档已写入：`C:\\...\\x.md`」；
    2. 抠不到就把工作目录浅扫一遍，只认「任务开始到结束后 3 分钟内落盘」的白名单类型文件
       （跳过 .git / data / state / node_modules / 日志目录，避免把数据库、识海和运行日志
       当成果发出去）。

    上限 limit 个，按修改时间倒序取最新的。
    """
    if limit <= 0:
        return []
    job_dir = _job_dir_of(done_data)
    job_meta = _read_job_meta(job_dir)
    earliest = float(done_data.get("started_at") or job_meta.get("created_at") or 0.0)
    latest = float(done_data.get("finished_at") or 0.0)
    max_bytes = max(1, max_mb) * 1024 * 1024
    skip_text = str(done_data.get("summary") or "")
    for name in ("stdout.txt", "stderr.txt"):
        f = job_dir / name
        if f.is_file():
            try:
                skip_text += "\n" + f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

    found: list[Path] = []
    seen: set[str] = set()

    def _try(raw: str, strict: bool = False) -> None:
        if len(found) >= limit:
            return
        cand = raw.strip().strip("\"'`.,;:，。；：）)")
        if not cand or len(cand) < 4:
            return
        skip_parts = _SCAN_SKIP_PARTS if strict else _SKIP_PATH_PARTS
        try:
            path = Path(cand).expanduser()
            if not path.is_absolute():
                return
            key = str(path.resolve()).lower()
        except Exception:
            return
        if key in seen:
            return
        if any(part.lower() in skip_parts for part in path.parts):
            return
        report_prefix = f"报告_{job_dir.name}"
        if path.name.startswith(report_prefix) or path.name.startswith("dsh_result_"):
            return
        if _looks_like_artifact(path, earliest, latest, max_bytes):
            seen.add(key)
            found.append(path)

    for match in _BACKTICK_RE.finditer(skip_text):
        _try(match.group(1))
    for match in _ABS_PATH_RE.finditer(skip_text):
        _try(match.group(0))

    # 只要 DSH 自己的话里点到了产物路径，就以它为准，不再去扫目录——
    # 扫描是兜底手段，多扫一次反而容易把同期被改的源码、配置当成产出发出去。
    if allow_scan and not found and len(found) < limit:
        workdir_raw = str(job_meta.get("workdir") or "").strip()
        root = Path(workdir_raw) if workdir_raw else BASE_DIR
        if root.is_dir():
            scanned = 0
            for cur_dir, dir_names, file_names in os.walk(root):
                cur = Path(cur_dir)
                dir_names[:] = [
                    d for d in dir_names
                    if d.lower() not in _SCAN_SKIP_PARTS and not d.startswith(".")
                ]
                try:
                    depth = len(cur.relative_to(root).parts)
                except ValueError:
                    depth = 0
                if depth >= 3:
                    dir_names[:] = []
                for fname in file_names:
                    scanned += 1
                    if scanned > 4000:
                        break
                    _try(str(cur / fname), strict=True)
                if scanned > 4000 or len(found) >= limit:
                    break

    found.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    return found[:limit]
