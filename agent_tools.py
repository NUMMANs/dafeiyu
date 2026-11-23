"""
常规桌面 Agent 工具箱（Windows 本机操作）：
- run_shell:         执行 PowerShell / CMD 命令（带高危毁机命令拦截）
- read_local_file:   读取本机文本文件
- write_local_file:  写入/创建本机文件
- list_directory:    查看目录文件列表
- open_url_or_app:   用默认浏览器打开网页，或启动本机程序/文件
- take_screenshot:   截取当前桌面屏幕（返回图片路径，可直接发回微信/QQ）
- get_system_info:   查看 CPU / 内存 / 磁盘与热门进程状态
- fetch_webpage:     抓取指定网页正文纯文本
"""
import html
import logging
import os
import re
import subprocess
import urllib.request
import uuid
from pathlib import Path

from PIL import ImageGrab

from config import DATA_DIR, UPLOADS_DIR, settings

logger = logging.getLogger("dafeiyu.agent_tools")

GENERATED_FILES_DIR = DATA_DIR / "generated_files"
GENERATED_FILES_DIR.mkdir(parents=True, exist_ok=True)

# 拦截明显毁机/格式化命令，防止群友捣乱或误操作
DANGEROUS_CMD_RE = re.compile(
    r"(format\s+[a-z]:|rd\s+/s\s+/q\s+[a-z]:\\|del\s+/f\s+/s\s+/q\s+[a-z]:\\|rm\s+-rf\s+/|shutdown\s+/[sr]|diskpart|bcdedit)",
    re.IGNORECASE,
)


def is_owner(sender: str) -> bool:
    """
    校验当前发言人是否有权调用本机电脑控制工具：
    - 若 .env 中配置了 OWNER_NAMES（如 群主,你的微信名），则仅名单内成员可操纵电脑；
    - 若 OWNER_NAMES 留空，则默认放开（适合独立虚拟机/专用电脑）。
    """
    if not settings.ENABLE_AGENT_TOOLS:
        return False
    if not settings.OWNER_NAMES:
        return True
    s = (sender or "").strip().lower()
    return any(owner.lower() == s for owner in settings.OWNER_NAMES)


def run_shell(command: str, timeout: int = 25) -> str:
    cmd = (command or "").strip()
    if not cmd:
        return "命令为空"
    if DANGEROUS_CMD_RE.search(cmd):
        return "触发安全拦截：拒绝执行高危毁机/关机/格式化命令"
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        combined = "\n".join(x for x in (out, f"[STDERR] {err}" if err else "") if x)
        if not combined:
            combined = f"命令已执行（退出码 {proc.returncode}，无输出）"
        return combined[:2500]
    except subprocess.TimeoutExpired:
        return f"命令执行超时（>{timeout}秒）"
    except Exception as exc:
        return f"执行失败: {exc}"


def read_local_file(path: str, max_chars: int = 3000) -> str:
    try:
        p = Path(path).expanduser()
        if not p.is_file():
            return f"文件不存在: {p}"
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + f"\n...(已截断，总长 {len(text)} 字符)"
        return text or "(空文件)"
    except Exception as exc:
        return f"读取文件失败: {exc}"


def write_local_file(path: str, content: str) -> str:
    try:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content or "", encoding="utf-8")
        return f"已写入文件: {p}（{len(content or '')} 字符）"
    except Exception as exc:
        return f"写入文件失败: {exc}"


def list_directory(path: str = ".") -> str:
    try:
        p = Path(path or ".").expanduser()
        if not p.is_dir():
            return f"目录不存在: {p}"
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:60]
        lines = [f"目录 {p}（共显示 {len(entries)} 项）："]
        for e in entries:
            kind = "[DIR]" if e.is_dir() else f"{e.stat().st_size}B"
            lines.append(f"- {e.name} ({kind})")
        return "\n".join(lines)
    except Exception as exc:
        return f"列出目录失败: {exc}"


def open_url_or_app(target: str) -> str:
    t = (target or "").strip()
    if not t:
        return "目标路径或网址为空"
    try:
        os.startfile(t)
        return f"已在电脑上打开: {t}"
    except Exception as exc:
        return f"打开失败 ({t}): {exc}"


def take_screenshot() -> tuple[Path | None, str]:
    try:
        img = ImageGrab.grab(all_screens=False)
        out = UPLOADS_DIR / f"screen_{uuid.uuid4().hex[:8]}.png"
        img.save(out, format="PNG")
        return out, f"已截取当前桌面屏幕: {out.name} ({img.size[0]}x{img.size[1]})"
    except Exception as exc:
        return None, f"截屏失败（请确认电脑桌面未锁屏）: {exc}"


def get_system_info() -> str:
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("C:\\")
        procs = sorted(
            psutil.process_iter(["name", "memory_percent"]),
            key=lambda p: p.info.get("memory_percent") or 0,
            reverse=True,
        )[:5]
        top_str = ", ".join(
            f"{p.info['name']}({(p.info.get('memory_percent') or 0):.1f}%)" for p in procs
        )
        return (
            f"CPU: {cpu:.1f}% | 内存: {mem.used / (1024**3):.1f}/{mem.total / (1024**3):.1f}GB ({mem.percent}%) | "
            f"C盘剩余: {disk.free / (1024**3):.1f}GB | 内存前5进程: {top_str}"
        )
    except Exception as exc:
        return f"获取系统信息失败: {exc}"


def fetch_webpage(url: str, max_chars: int = 2200) -> str:
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return "无效网址，需以 http:// 或 https:// 开头"
    try:
        req = urllib.request.Request(
            u,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        raw = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        plain = re.sub(r"\s+", " ", plain).strip()
        return plain[:max_chars] if plain else "网页正文为空"
    except Exception as exc:
        return f"抓取网页失败: {exc}"


def create_and_send_file(filename: str, content: str) -> tuple[Path | None, str]:
    """生成任意文本/代码/表格/文档文件（.md/.txt/.py/.csv/.html/.json等）并作为附件发到微信群"""
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", (filename or "output.txt").strip())
    if not safe_name or safe_name.startswith("."):
        safe_name = "dafeiyu_output.txt"
    if "." not in safe_name:
        safe_name += ".txt"
    try:
        out = GENERATED_FILES_DIR / safe_name
        out.write_text(content or "", encoding="utf-8")
        return out, f"已生成文件 {out.name}（{out.stat().st_size}B），将随回复直接发送到聊天里"
    except Exception as exc:
        return None, f"生成文件失败: {exc}"


def send_existing_file(path: str) -> tuple[Path | None, str]:
    """把本机已有的任意文件（文档/压缩包/图片/代码等，上限 50MB）直接发送到当前微信聊天"""
    try:
        p = Path(path).expanduser()
        if not p.is_file():
            return None, f"文件不存在: {p}"
        if p.stat().st_size > 50 * 1024 * 1024:
            return None, f"文件超过 50MB，拒绝发送: {p.name}"
        return p, f"已准备发送文件 {p.name}（{p.stat().st_size}B）到当前聊天"
    except Exception as exc:
        return None, f"准备发送文件失败: {exc}"


AGENT_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "在当前 Windows 电脑上执行 PowerShell 命令（如查进程、运行脚本、系统操作等）",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "要执行的 PowerShell 命令"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_local_file",
            "description": "读取本机文本文件内容",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件绝对路径或相对路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_local_file",
            "description": "在本机创建或覆盖写入文本文件（如写脚本、记笔记、改配置）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出本机某个文件夹里的文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件夹路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url_or_app",
            "description": "在电脑上打开一个网址（如B站/网页）或启动本机应用程序/文件",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string", "description": "网址（https://...）或程序/文件路径"}},
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "截取当前电脑桌面画面，并将截图发回给群友/主人看",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "查看当前电脑的 CPU、内存、C盘空间和占用最高的进程",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "抓取指定网页 URL 的正文纯文本内容",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "以 http:// 或 https:// 开头的网页链接"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_and_send_file",
            "description": (
                "生成一个完整文件（如 .md 攻略/报告、.txt 整理文档、.py/.html/.js 代码脚本、.csv 表格、.json 数据）"
                "并直接作为微信文件附件发到群里。当群友要你写长文档、攻略、代码文件、表格或明确要求「发个文件」时调用！"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（带后缀，如 剑魔攻略.md 或 整理脚本.py 或 清单.csv）"},
                    "content": {"type": "string", "description": "文件的完整文本内容"},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_existing_file",
            "description": "把本机硬盘上已有的文件（文档/图片/代码/压缩包等）直接发到当前微信聊天里",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "本机文件的完整路径"}},
                "required": ["path"],
            },
        },
    },
]
