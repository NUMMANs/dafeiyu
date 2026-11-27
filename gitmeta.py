"""Git 分支与网络的自愈小工具。

三处代码都要跑 git（watchdog.py、self_ops.py、tools/nightly_guard.py），各自封装不同，
但有两件事必须一致，所以集中在这里：

1. **代理探活**：git 配置里如果有 ``http.*.proxy`` 指向一个已经关掉的代理，
   fetch/push 会以「schannel: failed to receive handshake」失败。夜间任务因此推不上去，
   改动就白改了。这里在每条命令前把它就地清空（``-c key=``），只对本次调用生效，
   不改用户配置——用户把代理重新打开后又能照常用。
2. **当前分支**：自动更新、回滚闸门、夜间推送都要知道「现在跑在哪个分支上」，
   不能再写死 main。写死的后果是：切到分支后自动更新把分支快进回 main，
   夜间提交则留在本地分支上、push 却推的是 main（还返回成功）。

只依赖标准库。watchdog 的崩溃处理路径会 import 它，别在这里引入项目内模块。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

# 改了这些扩展名的文件必须重启进程才生效；只改 .md/.json 的话热加载就够了。
CODE_SUFFIXES = (".py", ".bat", ".cmd", ".ps1", ".sh")
CODE_FILES = ("requirements.txt",)

# 代理探活结果缓存：配置一般不会几秒一变，省掉每条 git 命令都去 connect 一次。
_PROBE_TTL = 60.0
_probe_cache: dict[str, tuple[float, bool]] = {}
# 结果缓存：一次拉取/推送流程里会连跑七八条 git 命令，不必每条都去查一遍配置。
_NET_TTL = 60.0
_net_cache: dict[str, tuple[float, list[str]]] = {}


def _run(repo: Path, args: list[str], timeout: int = 15) -> tuple[int, str]:
    env = dict(os.environ)
    # 卡在凭据输入上会让夜间任务一直挂着，直到锁过期
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except Exception:  # noqa: BLE001 - 探测失败一律当作"没拿到"
        return 1, ""
    return proc.returncode, (proc.stdout or "").strip()


def _alive(url: str, timeout: float = 1.2) -> bool:
    """代理端口还活着吗。只做 TCP 连接，不发请求，够判断"端口上有没有人听"。"""
    now = time.monotonic()
    hit = _probe_cache.get(url)
    if hit and now - hit[0] < _PROBE_TTL:
        return hit[1]
    ok = False
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 8080)
        if host:
            with socket.create_connection((host, port), timeout=timeout):
                ok = True
    except OSError:
        ok = False
    _probe_cache[url] = (now, ok)
    return ok


def net_args(repo: Path) -> list[str]:
    """返回要追加到 git 命令前的 ``-c`` 参数，把指向死代理的配置就地清空。

    没有死代理时返回空列表，调用方可以无脑 ``[*net_args(repo), ...]``。
    """
    key = str(repo)
    now = time.monotonic()
    hit = _net_cache.get(key)
    if hit and now - hit[0] < _NET_TTL:
        return list(hit[1])
    args = _probe_net_args(repo)
    _net_cache[key] = (now, args)
    return list(args)


def _probe_net_args(repo: Path) -> list[str]:
    rc, out = _run(repo, ["config", "--get-regexp", r"^http\..*proxy$"], timeout=8)
    if rc != 0 or not out:
        return []
    args: list[str] = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        key = parts[0].strip()
        value = parts[1].strip() if len(parts) > 1 else ""
        if not key or not value:
            continue
        if _alive(value):
            continue
        args += ["-c", f"{key}="]
    if args:
        # 写 stderr：tools/nightly_guard.py 的 stdout 是给调用方解析的 JSON，
        # 往那里打一行提示会让 JSON 解析直接失败。同一份参数只提示一次。
        print(f"[gitmeta] 代理不通，本次直连：{' '.join(args)}", file=sys.stderr, flush=True)
    return args


def branch(repo: Path) -> str:
    """当前分支名；detached HEAD 时返回 "HEAD"。"""
    rc, out = _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=8)
    return out if rc == 0 else ""


def commit(repo: Path) -> str:
    rc, out = _run(repo, ["rev-parse", "HEAD"], timeout=8)
    return out if rc == 0 else ""


def upstream_ref(repo: Path) -> str:
    """上游的完整引用，如 ``origin/main``；没有上游返回空串。"""
    rc, out = _run(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], timeout=8)
    return out if rc == 0 else ""


def upstream_branch(repo: Path) -> str:
    """上游分支名，如 ``main``；没有上游返回空串。"""
    ref = upstream_ref(repo)
    if not ref:
        return ""
    return ref.split("/", 1)[1] if "/" in ref else ref


def touches_code(repo: Path, old: str, new: str) -> bool:
    """两个提交之间的差异是否包含需要重启才能生效的文件。

    夜间任务只改 roles/judges/*.md 这类热加载文件，提交后不该把机器人重启一遍；
    改了 .py 或启动脚本就必须重启，否则跑的还是旧代码。
    拿不到差异时保守地当作"要重启"。
    """
    if not old or not new or old == new:
        return False
    rc, out = _run(repo, ["diff", "--name-only", old, new], timeout=15)
    if rc != 0:
        return True
    for line in out.splitlines():
        path = line.strip().replace("\\", "/")
        if not path:
            continue
        if path.endswith(CODE_SUFFIXES) or path.rsplit("/", 1)[-1] in CODE_FILES:
            return True
    return False
