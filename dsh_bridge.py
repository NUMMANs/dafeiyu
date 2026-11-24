"""DeepSeek Harness (DSH) CLI 异步后台任务桥接与断线续投模块。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
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


def get_last_session_id(group_id: str = "") -> str:
    """获取指定群最近一次 DSH 任务的 session_id。"""
    for jdir in _iter_job_dirs():
        job = _read_json(jdir / "job.json")
        if not job:
            continue
        if group_id and job.get("group_id") != group_id:
            continue
        sid = str(job.get("session_id") or "").strip()
        if sid:
            return sid
    return ""


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
    if req_sid.lower() == "last":
        resolved_sid = get_last_session_id(group_id) or f"dfy-{uuid.uuid4().hex[:8]}"
    elif req_sid:
        resolved_sid = req_sid
    else:
        resolved_sid = f"dfy-{uuid.uuid4().hex[:8]}"

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
        _atomic_write_text(job_dir / "prompt.txt", clean_task)

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
                "session_id": job.get("session_id", ""),
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
    if body_text:
        lines.append(f"\n{body_text}")
    if attach_path is not None:
        lines.append("\n📎 完整输出较长，已随附结果文本文件。")

    return "\n".join(lines), attach_path
