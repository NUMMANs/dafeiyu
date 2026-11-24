"""独立脱离进程树的 DSH CLI 后台任务执行器（仅使用 Python 标准库）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _decode_stream(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def _build_summary(status: str, exit_code: int, stdout_text: str, stderr_text: str, error_msg: str = "") -> str:
    clean_out = stdout_text.strip()
    clean_err = stderr_text.strip()

    if status == "done":
        if not clean_out:
            return "DSH 已执行完成（无标准输出）。"
        if len(clean_out) <= 1500:
            return clean_out
        return f"{clean_out[:450]}\n...\n{clean_out[-980:]}"

    parts: list[str] = []
    if error_msg:
        parts.append(error_msg)
    else:
        parts.append(f"DSH 退出码 {exit_code}")
    if clean_err:
        parts.append(f"错误输出:\n{clean_err[-900:]}")
    elif clean_out:
        parts.append(f"输出:\n{clean_out[-900:]}")
    return "\n".join(parts)[:1500]


def main() -> int:
    if len(sys.argv) < 2:
        return 2

    job_dir = Path(sys.argv[1]).resolve()
    job_path = job_dir / "job.json"
    prompt_path = job_dir / "prompt.txt"
    running_path = job_dir / "running.json"
    done_path = job_dir / "done.json"

    job = json.loads(job_path.read_text(encoding="utf-8"))
    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else str(job.get("task") or "")

    started_at = time.time()
    _atomic_write_text(
        running_path,
        json.dumps({"pid": os.getpid(), "started_at": started_at}, ensure_ascii=False, indent=2),
    )

    dsh_bin = str(job.get("dsh_bin") or "dsh")
    session_id = str(job.get("session_id") or "").strip()
    mode = str(job.get("mode") or "workspace-write").strip()
    model = str(job.get("model") or "").strip()
    workdir = str(job.get("workdir") or str(job_dir))
    timeout_sec = max(60, int(job.get("timeout_sec") or 900))

    if os.name == "nt" and dsh_bin.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/c", dsh_bin]
    else:
        cmd = [dsh_bin]

    cmd.extend(["--profile", "headless"])
    if session_id:
        cmd.extend(["--session-id", session_id])
    if mode:
        cmd.extend(["--mode", mode])
    if model:
        cmd.extend(["--model", model])

    run_env = os.environ.copy()
    run_env["PYTHONUTF8"] = "1"
    run_env["PYTHONIOENCODING"] = "utf-8"

    status = "done"
    exit_code = 0
    stdout_text = ""
    stderr_text = ""
    error_msg = ""

    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            input=prompt_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            env=run_env,
        )
        exit_code = int(proc.returncode)
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
        if exit_code != 0:
            status = "failed"
    except subprocess.TimeoutExpired as exc:
        status = "timeout"
        exit_code = -1
        stdout_text = _decode_stream(exc.stdout)
        stderr_text = _decode_stream(exc.stderr)
        error_msg = f"DSH 执行超时（超过 {timeout_sec} 秒）"
    except Exception as exc:
        status = "failed"
        exit_code = -2
        error_msg = f"启动 DSH 失败: {exc}"

    finished_at = time.time()
    (job_dir / "stdout.txt").write_text(stdout_text, encoding="utf-8")
    (job_dir / "stderr.txt").write_text(stderr_text, encoding="utf-8")

    summary = _build_summary(status, exit_code, stdout_text, stderr_text, error_msg=error_msg)
    done_payload = {
        "job_id": job.get("job_id", job_dir.name),
        "group_id": job.get("group_id", ""),
        "sender": job.get("sender", ""),
        "session_id": session_id,
        "task": job.get("task", ""),
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_sec": round(finished_at - started_at, 2),
        "summary": summary,
        "delivered": False,
    }
    _atomic_write_text(done_path, json.dumps(done_payload, ensure_ascii=False, indent=2))
    running_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
