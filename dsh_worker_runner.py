"""独立脱离进程树的 DSH CLI 后台任务执行器（仅使用 Python 标准库）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
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


# ======================================================================
# CodeBuddy CLI 后端（TOOL_BACKEND=codebuddy）
# ======================================================================
# 与 DSH 的四处硬差异，都是实测出来的，改之前先看这里：
# 1. 必须剥掉 WorkBuddy 桌面端注入的 ACC_PRODUCT_CONFIG* / CODEBUDDY_FORCE_*，
#    否则 CLI 按 workbuddy 产品身份去找凭证 → Authentication required。
#    用户那次登录落在默认产品（codebuddy.cn）下，只有不带这些变量才认得出已登录。
# 2. 不给 session 时它会默认「续上该目录最近一次会话」，所以新任务必须显式 --session-id，
#    否则这次活会带着上一次的上下文跑。
# 3. --output-format json 输出的是「消息数组」，最后一条 type=result 才是结果与用量。
# 4. total_cost_usd 恒为 0（订阅制），只能报 token，不能报钱。

_CBC_STRIP_ENV_KEYS = (
    "ACC_PRODUCT_CONFIG",
    "ACC_PRODUCT_CONFIG_V2",
    "ACC_PRODUCT_CONFIG_V3",
    "ACC_PRODUCT_CONFIG_PATH",
    "CODEBUDDY_FORCE_LITE_WB_BUNDLE",
    "CODEBUDDY_FORCE_HEADLESS_BUNDLE",
    "CODEBUDDY_MCP_CONFIG",
    "CODEBUDDY_SESSION_ID",
    "CODEBUDDY_API_KEY_HELPER_DISABLED",
    "CODEBUDDY_GATEWAY_AUTH",
)

_CBC_DEFAULT_BIN = (
    Path.home()
    / ".codebuddy-cli"
    / "node_modules"
    / "@tencent-ai"
    / "codebuddy-code"
    / "bin"
    / "codebuddy"
)


def _resolve_cbc_bin(explicit: str = "") -> str:
    for cand in (explicit, os.environ.get("CBC_BIN", ""), str(_CBC_DEFAULT_BIN)):
        value = (cand or "").strip()
        if value and Path(value).is_file():
            return value
    found = shutil.which("codebuddy")
    return found or ""


def _build_cbc_cmd(
    cbc_bin: str,
    session_id: str,
    resume: bool,
    model: str,
    permission_mode: str,
) -> list[str]:
    low = cbc_bin.lower()
    if low.endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/c", cbc_bin]
    elif low.endswith(".exe"):
        cmd = [cbc_bin]
    else:
        # npm 包里的 bin/codebuddy 是无扩展名的 shebang 脚本，Windows 上直接 CreateProcess 会
        # 报 WinError 193，必须显式用 node 拉起。
        cmd = [shutil.which("node") or "node", cbc_bin]

    cmd.extend(["-p", "--output-format", "json"])
    if permission_mode:
        cmd.extend(["--permission-mode", permission_mode])
    if model:
        cmd.extend(["--model", model])
    if session_id:
        cmd.extend(["-r", session_id] if resume else ["--session-id", session_id])
    return cmd


def _parse_cbc_result(stdout_text: str) -> dict:
    """从消息数组里取出 type=result 那一条；取不到返回空 dict。"""
    raw = (stdout_text or "").strip()
    if not raw:
        return {}
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        match = re.search(r'\{"type"\s*:\s*"result".*\}\s*$', raw, re.S)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None
    if isinstance(data, list):
        for item in reversed(data):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
        tail = data[-1] if data else None
        return tail if isinstance(tail, dict) else {}
    return data if isinstance(data, dict) else {}


def _cbc_usage(result: dict, model: str) -> dict:
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return {}
    total_in = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    return {
        "uncachedInputTokens": max(0, total_in - cache_read),
        "outputTokens": out,
        "cacheReadTokens": cache_read,
        "cacheWriteTokens": cache_write,
        "model": model,
    }


def _cbc_broken_reason(result: dict, status: str, exit_code: int, stderr_text: str, error_msg: str) -> str:
    """判断这次失败是「CodeBuddy 本身不可用」还是「活干砸了」。

    只有前者才值得用 DSH 重跑一遍：认证失效 / CLI 崩了属于基础设施问题；
    模型把任务做错、工具报错属于业务结果，重跑只会白烧一遍额度。
    """
    if status == "timeout":
        return ""

    text = " ".join([str(result.get("result") or ""), stderr_text or "", error_msg or ""]).lower()
    for marker in ("authentication required", "please use /login", "invalid api key", "unauthorized"):
        if marker in text:
            return "CodeBuddy 未登录或凭证已失效"
    if "未找到 codebuddy cli" in text:
        return "CodeBuddy CLI 未找到"
    if status == "failed" and not result and exit_code != 0:
        return f"CodeBuddy 异常退出（exit={exit_code}）"
    return ""


def _log_backend_fallback(reason: str, job: dict, job_dir: Path) -> None:
    """回退事件单独留一份流水，方便事后看「到底多久退一次」。"""
    record = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "job_id": job.get("job_id", job_dir.name),
        "group_id": job.get("group_id", ""),
        "from": "codebuddy",
        "to": "dsh",
        "reason": reason,
    }
    try:
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "tool_backend_fallback.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        (job_dir / "fallback.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _run_codebuddy(job: dict, prompt_text: str, job_dir: Path, started_at: float):
    """CodeBuddy CLI 版执行路径。

    返回 (done_payload, "") 表示跑完了（成功或业务失败都算跑完）；
    返回 (None, 原因) 表示 CodeBuddy 这层不可用，调用方应改用 DSH 重跑。
    """
    running_path = job_dir / "running.json"
    done_path = job_dir / "done.json"

    cbc_bin = _resolve_cbc_bin(str(job.get("cbc_bin") or ""))
    model = str(job.get("cbc_model") or os.environ.get("CBC_MODEL") or "deepseek-v4.1-flash").strip()
    permission_mode = str(
        job.get("cbc_permission_mode") or os.environ.get("CBC_PERMISSION_MODE") or "bypassPermissions"
    ).strip()
    workdir = str(job.get("workdir") or str(job_dir))
    timeout_sec = max(60, int(job.get("timeout_sec") or 900))

    session_id = str(job.get("session_id") or "").strip()
    if session_id.startswith("dfy-"):
        session_id = ""
    resume = bool(session_id)
    if not session_id:
        # 新任务必须显式给 id，否则会续上该目录上一次的会话
        session_id = str(uuid.uuid4())

    status = "done"
    exit_code = 0
    stdout_text = ""
    stderr_text = ""
    error_msg = ""

    if not cbc_bin:
        _log_backend_fallback("CodeBuddy CLI 未找到", job, job_dir)
        running_path.unlink(missing_ok=True)
        return None, "CodeBuddy CLI 未找到"

    result: dict = {}
    if status == "done":
        run_env = os.environ.copy()
        run_env["PYTHONUTF8"] = "1"
        run_env["PYTHONIOENCODING"] = "utf-8"
        for env_key in _CBC_STRIP_ENV_KEYS:
            run_env.pop(env_key, None)

        def _invoke(sid: str, is_resume: bool):
            cmd = _build_cbc_cmd(cbc_bin, sid, is_resume, model, permission_mode)
            return subprocess.run(
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

        try:
            proc = _invoke(session_id, resume)
            exit_code = int(proc.returncode)
            stdout_text = proc.stdout or ""
            stderr_text = proc.stderr or ""
            if exit_code != 0 and resume and _is_missing_session_error(stderr_text):
                # 旧会话在这台机器已经不认了，换一个新会话重跑，别让整单活白丢
                session_id = str(uuid.uuid4())
                resume = False
                proc = _invoke(session_id, False)
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
            error_msg = f"CodeBuddy 执行超时（超过 {timeout_sec} 秒）"
        except Exception as exc:
            status = "failed"
            exit_code = -2
            error_msg = f"启动 CodeBuddy CLI 失败: {exc}"

        result = _parse_cbc_result(stdout_text)
        if status == "done" and result:
            subtype = str(result.get("subtype") or "")
            if result.get("is_error") or (subtype and subtype != "success"):
                status = "failed"
                error_msg = error_msg or str(result.get("result") or subtype or "任务未成功")

        broken = _cbc_broken_reason(result, status, exit_code, stderr_text, error_msg)
        if broken:
            # 先把 CodeBuddy 自己的现场留下来，再交给 DSH 重跑，别把原始报错丢掉
            try:
                (job_dir / "stdout.txt").write_text(stdout_text, encoding="utf-8")
                (job_dir / "stderr.txt").write_text(stderr_text, encoding="utf-8")
            except Exception:
                pass
            _log_backend_fallback(broken, job, job_dir)
            running_path.unlink(missing_ok=True)
            return None, broken

    final_text = str(result.get("result") or "").strip()
    real_sid = str(result.get("session_id") or session_id)
    usage = _cbc_usage(result, model)

    finished_at = time.time()
    (job_dir / "stdout.txt").write_text(stdout_text, encoding="utf-8")
    (job_dir / "stderr.txt").write_text(stderr_text, encoding="utf-8")

    summary = _build_summary(status, exit_code, final_text, stderr_text, error_msg=error_msg)
    done_payload = {
        "job_id": job.get("job_id", job_dir.name),
        "group_id": job.get("group_id", ""),
        "sender": job.get("sender", ""),
        "session_id": real_sid,
        "task": job.get("task", ""),
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_sec": round(finished_at - started_at, 2),
        "summary": summary,
        "usage": usage,
        "model": model,
        "usage_session": real_sid,
        "backend": "codebuddy",
        "delivered": False,
    }
    _atomic_write_text(done_path, json.dumps(done_payload, ensure_ascii=False, indent=2))
    running_path.unlink(missing_ok=True)
    return done_payload, ""


# ======================================================================
# DSH 会话用量采集：算「这次任务花了多少钱」要拿它自己的 token 账
# ======================================================================
# DSH 把每个会话的 token 累计写进投影缓存（纯 JSON，不用解压 zstd 会话文件）：
#   <home>/storages/session_projcache/sessions/session-<id>.json
# 结构：record.rows.tokenUsage.val.totals = {uncachedInputTokens, outputTokens,
#       cacheReadTokens, cacheWriteTokens}；model 在 record.rows.modelSelection.val.lastUsed.model
# 会话目录按工作目录分桶：<home>/sessions/--C-Users-x-proj--/session-<id>/

_USAGE_KEYS = ("uncachedInputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")


def _dsh_home() -> Path:
    raw = (os.environ.get("DSH_HOME") or "").strip()
    if raw:
        try:
            return Path(raw).expanduser()
        except Exception:
            pass
    return Path.home() / ".dsh"


def _project_key(cwd: str) -> str:
    """复刻 DSH 的会话目录命名：分隔符折叠成 '-'，非法字符转 ~XXXX。"""
    readable = ""
    separator_run = False
    for ch in cwd:
        code = ord(ch)
        if ch in ("/", "\\", ":"):
            if not separator_run:
                readable += "-"
            separator_run = True
        elif ch != "~" and re.fullmatch(r"[A-Za-z0-9._-]", ch):
            readable += ch
            separator_run = False
        else:
            readable += "~" + format(code, "04X")
            separator_run = False
    head = readable.lstrip("-") or "root"
    return f"--{head[:251]}--"


def _session_root(launch_cwd: str) -> Path:
    return _dsh_home() / "sessions" / _project_key(launch_cwd)


def _list_sessions(session_root: Path) -> list[Path]:
    try:
        return [p for p in session_root.iterdir() if p.is_dir() and p.name.startswith("session-")]
    except Exception:
        return []


def _read_session_usage(session_name: str) -> dict | None:
    """读一次会话的 token 累计与模型名；缓存读不到返回 None。"""
    if not session_name:
        return None
    cache = _dsh_home() / "storages" / "session_projcache" / "sessions" / f"{session_name}.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        return None
    rows = ((data.get("record") or {}).get("rows") or {})
    totals = (((rows.get("tokenUsage") or {}).get("val") or {}).get("totals") or {})
    if not isinstance(totals, dict):
        totals = {}
    model = str(
        ((((rows.get("modelSelection") or {}).get("val") or {}).get("lastUsed") or {}).get("model")) or ""
    )
    usage = {key: int(totals.get(key) or 0) for key in _USAGE_KEYS}
    usage["model"] = model
    return usage


def _read_session_usage_wait(session_name: str, wait_sec: float = 2.0) -> dict | None:
    """投影缓存可能比进程退出晚一点点落盘，短暂重试。"""
    deadline = time.time() + max(0.0, wait_sec)
    while True:
        usage = _read_session_usage(session_name)
        if usage is not None:
            return usage
        if time.time() >= deadline:
            return None
        time.sleep(0.2)


def _delta_usage(after: dict, before: dict) -> dict:
    """续聊会往同一个会话累加，本次花费要减掉进来之前的账。"""
    delta = {key: max(0, int(after.get(key) or 0) - int(before.get(key) or 0)) for key in _USAGE_KEYS}
    delta["model"] = after.get("model") or before.get("model") or ""
    return delta


def _session_created_at(session_name: str) -> float:
    cache = _dsh_home() / "storages" / "session_projcache" / "sessions" / f"{session_name}.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        ms = int((((data.get("record") or {}).get("identity") or {}).get("createdAt")) or 0)
        return ms / 1000.0
    except Exception:
        return 0.0


def _pick_new_session(session_root: Path, before: set[str], started_at: float, wait_sec: float = 3.0) -> str:
    """找出本次任务新开的会话：优先按会话创建时间贴任务启动时间挑（同目录并发也不易串）。"""
    deadline = time.time() + max(0.0, wait_sec)
    while True:
        fresh = [p for p in _list_sessions(session_root) if p.name not in before]
        if fresh:
            def _rank(p: Path) -> tuple[float, float]:
                created = _session_created_at(p.name)
                gap = abs(created - started_at) if created > 0 else 1e9
                try:
                    mtime = p.stat().st_mtime
                except Exception:
                    mtime = 0.0
                return (gap, -mtime)

            fresh.sort(key=_rank)
            return fresh[0].name
        if time.time() >= deadline:
            return ""
        time.sleep(0.25)


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

    backend = str(job.get("backend") or os.environ.get("TOOL_BACKEND") or "dsh").strip().lower()
    fallback_from = ""
    fallback_reason = ""
    if backend == "codebuddy":
        payload, reason = _run_codebuddy(job, prompt_text, job_dir, started_at)
        if payload is not None:
            return 0
        # CodeBuddy 这层不可用（没登录 / CLI 崩了 / 找不到）——同一进程直接改用 DSH 重跑，
        # 派单方和群友都不用重来一遍。
        fallback_from = "codebuddy"
        fallback_reason = reason or "CodeBuddy 不可用"
        backend = "dsh"
        job["backend"] = "dsh"
        print(f"[fallback] CodeBuddy 不可用（{fallback_reason}），改用 DSH 重跑", file=sys.stderr)

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

    # 记账基线：新会话靠「执行前后目录对比」找出自己那份；续聊靠「进来前的累计」做差
    session_root = _session_root(str(launch_cwd))
    sessions_before = {p.name for p in _list_sessions(session_root)}
    requested_sid = session_id
    usage_before = _read_session_usage(requested_sid) if requested_sid else None

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

    # 本次任务的 token 账（不管成功失败，只要真调过模型就有）
    # 注意：这里的会话 id 只用来对账，不回写 done.session_id——headless 会话的 cwd 可能
    # 是临时启动目录，回写会让下一次 `/dsh 续` 真的去 resume 那个临时会话。
    usage: dict = {}
    model_used = ""
    usage_session = ""
    picked_session = ""
    if requested_sid:
        latest = _read_session_usage_wait(requested_sid)
        if latest is not None:
            picked_session = requested_sid
            usage = _delta_usage(latest, usage_before) if usage_before else latest
    if not picked_session:
        found = _pick_new_session(session_root, sessions_before, started_at)
        if found:
            picked_session = found
            usage = _read_session_usage_wait(found) or {}
    if picked_session:
        usage_session = picked_session
        model_used = str(usage.pop("model", "") or "")

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
        "usage": usage,
        "model": model_used,
        "usage_session": usage_session,
        "backend": "dsh",
        "fallback_from": fallback_from,
        "fallback_reason": fallback_reason,
        "delivered": False,
    }
    _atomic_write_text(done_path, json.dumps(done_payload, ensure_ascii=False, indent=2))
    running_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
