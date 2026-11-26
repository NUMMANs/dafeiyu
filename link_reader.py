"""群聊链接识别：把「群里甩了个链接」变成一条可控的后台理解任务。

这里只做确定性的活（正则提取、垃圾过滤、去重、并发计数），一次模型都不调。
「这个链接要不要读、是分享还是让人干活」交给 brain.judge_tool_need(link_urls=...)。

为什么要独立于回复决策：群里发链接时大肥鱼常常不回话（没人 @、没接话茬），
如果等「决定回复」才去读，链接就永远读不到。所以入口处静默派单，结果走 L3 注文，
等后面有人聊到，她自然就懂了。
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

# 微信里链接常带的包裹字符、尾随中文标点，都要切干净再交给工具
_URL_RE = re.compile(r"https?://[^\s<>'\"（）【】「」《》]+", re.I)
_WWW_RE = re.compile(
    r"(?<![\w/])(www\.[a-z0-9.-]+\.[a-z]{2,}(?:/[^\s<>'\"（）【】「」《》]*)?)",
    re.I,
)
_TRAILING_CHARS = "，。！？、；：,.!?;:）)》】」』》\"'…—-~·|/>"

# 明显不是「值得读的文章」：图片/表情/文件直链，交给工具只会浪费一次调用
_SKIP_EXTS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".heic",
    ".pdf", ".zip", ".rar", ".7z", ".apk", ".exe", ".msi", ".dmg", ".iso",
    ".mp3", ".mp4", ".m4a", ".wav", ".avi", ".mov", ".mkv", ".flv",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".torrent",
)
_SKIP_HOST_PARTS = (
    "qpic.cn",          # 微信图片 / 公众号封面
    "qlogo.cn",         # 头像
    "res.wx.qq.com",    # 微信静态资源
    "mmbiz.qpic.cn",
)


def _clean(raw: str) -> str:
    u = (raw or "").strip()
    # 正则会一路吃到下一个空格，中文标点后面基本都是群友的话、不是网址的一部分；
    # 先在这里截断，免得「https://x.com/a。还有这个」被当成一个超长 URL。
    for i, ch in enumerate(u):
        if ch in "，。！？、；：":
            u = u[:i]
            break
    while u and u[-1] in _TRAILING_CHARS:
        u = u[:-1]
    # 微信偶尔会把 & 转义成 &，还原一下免得后端参数被截断
    return u.replace("&" + "amp;", "&")


def should_skip(url: str) -> bool:
    """图片、表情、软件安装包这类直链不值得派活去「理解」。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    path = (parts.path or "").lower()
    if path.endswith(_SKIP_EXTS):
        return True
    host = (parts.netloc or "").lower()
    return any(p in host for p in _SKIP_HOST_PARTS)


def extract_urls(text: str, max_urls: int = 2) -> list[str]:
    """从一条群消息里抽出值得读的链接，按出现顺序去重，最多 max_urls 个。"""
    raw = text or ""
    found: list[str] = []

    for m in _URL_RE.finditer(raw):
        u = _clean(m.group(0))
        if u.startswith(("http://", "https://")):
            found.append(u)

    # 没带协议的 www.xxx.com 也认，补成 https（微信里偶尔就是纯文本）
    if len(found) < max_urls:
        for m in _WWW_RE.finditer(raw):
            u = _clean("https://" + m.group(1))
            if u not in found:
                found.append(u)

    out: list[str] = []
    for u in found:
        if len(u) < 12 or should_skip(u) or u in out:
            continue
        out.append(u)
        if len(out) >= max(1, max_urls):
            break
    return out


def describe(url: str) -> str:
    """给判定器/日志看的简短描述，不联网。"""
    try:
        p = urlsplit(url)
        return f"{p.netloc or url}{p.path or ''}"[:80]
    except ValueError:
        return url[:80]


class LinkLimiter:
    """每群并发 1 个链接任务 + 同 URL 一段时间不重复读 + 忙时排队补派。

    纯内存即可：重启后最坏是同一个链接再读一次，L3 注文按内容去重不会串。
    """

    def __init__(self, seen_ttl_sec: float = 7200.0) -> None:
        self.seen_ttl = max(60.0, float(seen_ttl_sec))
        self._seen: dict[tuple[str, str], float] = {}
        self._running: dict[str, float] = {}       # group -> 到期时间
        self._queued: dict[str, list[str]] = {}    # group -> 忙时攒下的 URL

    def _sweep(self, now: float) -> None:
        if len(self._seen) > 400:
            self._seen = {
                k: ts for k, ts in self._seen.items() if now - ts < self.seen_ttl
            }

    def is_seen(self, group_id: str, url: str, now: float | None = None) -> bool:
        now = now or time.time()
        ts = self._seen.get((group_id, url))
        if ts is None:
            return False
        if now - ts >= self.seen_ttl:
            self._seen.pop((group_id, url), None)
            return False
        return True

    def mark_seen(self, group_id: str, urls: list[str], now: float | None = None) -> None:
        now = now or time.time()
        self._sweep(now)
        for u in urls:
            self._seen[(group_id, u)] = now

    def is_busy(self, group_id: str, now: float | None = None) -> bool:
        now = now or time.time()
        return float(self._running.get(group_id) or 0) > now

    def mark_running(self, group_id: str, timeout_sec: float, now: float | None = None) -> None:
        now = now or time.time()
        self._running[group_id] = now + max(30.0, float(timeout_sec))

    def release(self, group_id: str) -> None:
        self._running.pop(group_id, None)

    def queue(self, group_id: str, urls: list[str]) -> None:
        if not urls:
            return
        bucket = self._queued.setdefault(group_id, [])
        for u in urls:
            if u not in bucket:
                bucket.append(u)
        self._queued[group_id] = bucket[-6:]

    def pop_queued(self, group_id: str) -> list[str]:
        return self._queued.pop(group_id, [])

    def fresh(self, group_id: str, urls: list[str]) -> list[str]:
        """过滤掉刚读过的，返回真正需要派的链接。"""
        now = time.time()
        self._sweep(now)
        return [u for u in urls if not self.is_seen(group_id, u, now)]
