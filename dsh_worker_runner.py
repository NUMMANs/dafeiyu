"""独立脱离进程树的 DSH CLI 后台任务执行器（仅使用 Python 标准库）。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_CONFLICTING_ENV_KEY_RE = re.compile(
    r"(?m)^\s*(?:DEEPSEEK_BASE_URL|DSH_[A-Z0-9_]*|OPENAI_BASE_URL|ANTHROPIC_BASE_URL)\s*="
)

_STRIP_RUN_ENV_KEYS = (
    "DEEPSEEK_BASE_URL",
    "DSH_BIN",
    "DSH_MODE",
    "DSH_APPROVAL_POLICY",
    "DSH_MODEL",
    "DSH_WORKDIR",
    "DSH_TIMEOUT_SEC",
)


def _has_conflicting_dotenv(target_dir: Path) -> bool:
    try:
        resolved = target_dir.resolve()
    except Exception:
        resolved = target_dir

    for folder in (resolved, *resolved.parents):
        env_file = folder / ".env"
        if not env_file.is_file():
            continue
        try:
            text = env_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if _CONFLICTING_ENV_KEY_RE.search(text):
            return True
    return False


_SESSION_ID_RE = re.compile(
    r"(?:session[_\-\s]?id|sessionId)\s*[:=]\s*[\"']?([A-Za-z0-9][A-Za-z0-9_\-]{5,})[\"']?",
    re.IGNORECASE,
)


def _is_missing_session_error(stderr_text: str) -> bool:
    low = (stderr_text or "").lower()
    return "omit --session-id" in low or ("session" in low and "does not exist" in low)


def _extract_session_id(stderr_text: str) -> str:
    match = _SESSION_ID_RE.search(stderr_text or "")
    if match:
        cand = match.group(1).strip()
        if cand and not cand.startswith("dfy-"):
            return cand
    return ""


def _build_dsh_cmd(
    dsh_bin: str,
    session_id: str,
    mode: str,
    approval_policy: str,
    model: str,
) -> list[str]:
    if os.name == "nt" and dsh_bin.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/c", dsh_bin]
    else:
        cmd = [dsh_bin]

    cmd.extend(["--profile", "headless"])
    if session_id and not session_id.startswith("dfy-"):
        cmd.extend(["--session-id", session_id])
    if mode:
        cmd.extend(["--permission-mode", mode])
    if approval_policy:
        cmd.extend(["--approval-policy", approval_policy])
    if model:
        cmd.extend(["--model", model])
    return cmd


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
    if session_id.startswith("dfy-"):
        session_id = ""
    mode = str(job.get("mode") or "danger-full-access").strip()
    approval_policy = str(job.get("approval_policy") or "never").strip()
    model = str(job.get("model") or "").strip()
    workdir = str(job.get("workdir") or str(job_dir))
    timeout_sec = max(60, int(job.get("timeout_sec") or 900))

    launch_cwd = workdir
    if _has_conflicting_dotenv(Path(workdir)):
        launch_dir = Path(tempfile.gettempdir()) / "dafeiyu_dsh_launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        launch_cwd = str(launch_dir)
        mode = "danger-full-access"
        prompt_text = (
            f"[目标项目根目录]：{workdir}\n"
            "请直接在上述目标项目根目录中读取、修改文件与执行命令，不要把项目产物写到当前临时启动目录。\n\n"
            f"{prompt_text}"
        )

    cmd = _build_dsh_cmd(dsh_bin, session_id, mode, approval_policy, model)

    run_env = os.environ.copy()
    run_env["PYTHONUTF8"] = "1"
    run_env["PYTHONIOENCODING"] = "utf-8"
    for env_key in _STRIP_RUN_ENV_KEYS:
        run_env.pop(env_key, None)

    status = "done"
    exit_code = 0
    stdout_text = ""
    stderr_text = ""
    error_msg = ""

    try:
        proc = subprocess.run(
            cmd,
            cwd=launch_cwd,
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
        if exit_code != 0 and session_id and _is_missing_session_error(stderr_text):
            # 若传入的旧 session_id 已失效，自动去掉 --session-id 重试一次以开启新会话
            session_id = ""
            retry_cmd = _build_dsh_cmd(dsh_bin, "", mode, approval_policy, model)
            proc = subprocess.run(
                retry_cmd,
                cwd=launch_cwd,
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

    if not session_id and status == "done":
        discovered_sid = _extract_session_id(stderr_text)
        if discovered_sid:
            session_id = discovered_sid
            job["session_id"] = session_id
            _atomic_write_text(job_path, json.dumps(job, ensure_ascii=False, indent=2))

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
