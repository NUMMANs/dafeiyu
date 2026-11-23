"""
高可靠性联网搜索层（带质量门禁 + 年份归一化 + 24h 磁盘持久化缓存）：
优先级：
1. Tavily (每月 1000 次免费，结构化 JSON + 摘要)
2. Serper (2500 次免费 Google JSON)
3. 博查 Bocha (国内中文结构化 JSON)
4. 免费零 Key 兜底：必应 RSS + 严格关键词相关性门禁（拒绝“玉渊潭公园”式假结果污染上下文）
"""
from datetime import datetime
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from config import DATA_DIR, settings

logger = logging.getLogger("dafeiyu.search")

CACHE_PATH = DATA_DIR / "search_cache.json"
CACHE_TTL = 24 * 3600.0  # 24 小时持久化缓存（省配额 + 0ms 秒回）

BAD_PAGE_SIGNALS = (
    "verify you are human",
    "unusual traffic",
    "请开启 javascript",
    "访问过于频繁",
    "请输入验证码",
    "人机验证",
    "403 forbidden",
    "access denied",
)

_FILLER_RE = re.compile(
    r"(帮我|请帮我|麻烦|请问|上网|查一下|搜一下|查查|搜搜|看看|告诉我|我想知道|知不知道|是什么情况|是多少|怎么回事|[？?！!。，,]+)"
)


def _strip_tags(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def _extract_domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc
    except Exception:
        return ""


def _normalize_query(query: str) -> str:
    """清洗口水词，并将模糊的「最新/今年/现在」替换为具体年份（如 2026）"""
    year = str(datetime.now().year)
    q = _FILLER_RE.sub(" ", query or "").strip()
    q = re.sub(r"(最新|今年|目前|现在|近期)", f" {year} ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q if len(q) >= 2 else (query or "").strip()


def _extract_key_terms(query: str) -> list[str]:
    """提取用于质量门禁的核心实体词（英文/数字词 + 2字中文词），判断返回页是不是驴唇不对马嘴"""
    q = query.lower()
    terms: list[str] = []
    # 英文/缩写/型号（如 blg, deepseek, s16, v4, python）
    for w in re.findall(r"[a-z0-9]{2,}", q):
        if w not in {"2025", "2026", "2027", "api", "www", "com"}:
            terms.append(w)
    # 中文关键词（排除泛词）
    skip = {"价格", "多少", "什么", "怎么", "最新", "新闻", "攻略", "出装", "版本", "今天", "昨天", "最近"}
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", q):
        if seg not in skip:
            terms.append(seg)
            if len(seg) >= 4:
                terms.append(seg[:2])
                terms.append(seg[-2:])
    return list(dict.fromkeys(terms))


def _passes_quality_gate(query: str, title: str, snippet: str) -> bool:
    """
    核心质量门禁：
    1. 命中验证码/反爬提示 -> 丢弃
    2. 标题+摘要里必须至少命中 1 个查询核心词 -> 彻底拦截「搜 BLG 返回玉渊潭公园」这类 200 假成功！
    """
    combined = f"{title} {snippet}".lower()
    if len(combined.strip()) < 8:
        return False
    if any(bad in combined for bad in BAD_PAGE_SIGNALS):
        return False

    key_terms = _extract_key_terms(query)
    if not key_terms:
        return True
    return any(term in combined for term in key_terms)


def _load_cache() -> dict[str, dict]:
    if CACHE_PATH.is_file():
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    try:
        # 清理过期项并限制最多 300 条
        now = time.time()
        fresh = {k: v for k, v in cache.items() if (now - float(v.get("ts", 0))) < CACHE_TTL}
        if len(fresh) > 300:
            items = sorted(fresh.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:300]
            fresh = dict(items)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except Exception:
        pass


_DISK_CACHE = _load_cache()


def search_zhipu(query: str, max_results: int = 5, timeout: float = 10.0) -> str | None:
    """国内免翻墙高速搜索：优先智谱直连 /v4/web_search（~3秒），失败则回退免费 glm-4-flash 联网"""
    if not settings.ZHIPU_API_KEY:
        return None

    headers = {
        "Authorization": f"Bearer {settings.ZHIPU_API_KEY}",
        "Content-Type": "application/json",
    }

    # 1. 优先调智谱直连 web_search 专用接口（快、结构化、无多余生成耗时）
    try:
        payload = json.dumps(
            {"search_query": query, "search_engine": "search_std", "count": max_results}
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/web_search",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        items = data.get("search_result") or []
        lines = [f"关于「{query}」的智谱实时搜索结果："]
        kept = 0
        for item in items[:max_results]:
            title = (item.get("title") or "").strip()
            content = (item.get("content") or "").strip()
            if not _passes_quality_gate(query, title, content):
                continue
            pub = (item.get("publish_date") or "")[:10]
            media = item.get("media") or _extract_domain(item.get("link", ""))
            kept += 1
            lines.append(f"{kept}. {title} [{media} {pub}] — {content[:280]}")
        if kept > 0:
            return "\n".join(lines)
    except Exception as exc:
        logger.warning("智谱直连 web_search 失败，尝试回退 glm-4-flash: %s", exc)

    # 2. 免费兜底：glm-4-flash (max_tokens=16 只拿原始 web_search 列表，省掉长文生成时间)
    payload2 = json.dumps(
        {
            "model": "glm-4-flash",
            "messages": [{"role": "user", "content": query}],
            "tools": [
                {
                    "type": "web_search",
                    "web_search": {
                        "enable": True,
                        "search_result": True,
                        "search_query": query,
                    },
                }
            ],
            "max_tokens": 16,
        }
    ).encode("utf-8")
    req2 = urllib.request.Request(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        data=payload2,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req2, timeout=14.0) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))

    web_items = data.get("web_search") or []
    lines = [f"关于「{query}」的智谱免费联网搜索结果："]
    kept = 0
    for item in web_items[:max_results]:
        title = (item.get("title") or "").strip()
        content = (item.get("content") or "").strip()
        if not _passes_quality_gate(query, title, content):
            continue
        pub = (item.get("publish_date") or "")[:10]
        media = item.get("media") or _extract_domain(item.get("link", ""))
        kept += 1
        lines.append(f"{kept}. {title} [{media} {pub}] — {content[:260]}")
    return "\n".join(lines) if kept > 0 else None


def search_tavily(query: str, max_results: int = 5, timeout: float = 6.0) -> str | None:
    """Tavily AI 搜索（每月 1000 次免费，无需绑卡）"""
    if not settings.TAVILY_API_KEY:
        return None
    payload = json.dumps(
        {
            "api_key": settings.TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "include_answer": True,
            "max_results": max_results,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))

    lines = [f"关于「{query}」的 Tavily 搜索结果："]
    if data.get("answer"):
        lines.append(f"[综合摘要] {str(data['answer'])[:350]}")
    for idx, item in enumerate((data.get("results") or [])[:max_results], 1):
        title = (item.get("title") or "").strip()
        content = (item.get("content") or "").strip()
        if not _passes_quality_gate(query, title, content):
            continue
        domain = _extract_domain(item.get("url", ""))
        lines.append(f"{idx}. {title} [{domain}] — {content[:260]}")
    return "\n".join(lines) if len(lines) > 1 else None


def search_serper(query: str, max_results: int = 5, timeout: float = 5.0) -> str | None:
    """Serper Google 结构化搜索（注册送 2500 次免费，无需绑卡）"""
    if not settings.SERPER_API_KEY:
        return None
    payload = json.dumps({"q": query, "gl": "cn", "hl": "zh-cn", "num": max_results}).encode("utf-8")
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=payload,
        headers={"X-API-KEY": settings.SERPER_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))

    organic = data.get("organic") or []
    lines = [f"关于「{query}」的 Serper 搜索结果："]
    for idx, item in enumerate(organic[:max_results], 1):
        title = (item.get("title") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        if not _passes_quality_gate(query, title, snippet):
            continue
        pub = item.get("date") or ""
        domain = _extract_domain(item.get("link", ""))
        meta = f"[{domain} {pub}]".strip()
        lines.append(f"{idx}. {title} {meta} — {snippet[:260]}")
    return "\n".join(lines) if len(lines) > 1 else None


def search_bocha(query: str, max_results: int = 5, timeout: float = 5.0) -> str | None:
    """博查 AI 搜索（国内中文结构化 JSON）"""
    if not settings.BOCHA_API_KEY:
        return None
    payload = json.dumps(
        {"query": query, "freshness": "noLimit", "summary": True, "count": max_results}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.bochaai.com/v1/web-search",
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.BOCHA_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))

    pages = (data.get("data") or {}).get("webPages", {}).get("value") or []
    lines = [f"关于「{query}」的博查搜索结果："]
    for idx, item in enumerate(pages[:max_results], 1):
        title = (item.get("name") or "").strip()
        summary = (item.get("summary") or item.get("snippet") or "").strip()
        if not _passes_quality_gate(query, title, summary):
            continue
        pub = (item.get("datePublished") or "")[:10]
        site = item.get("siteName") or _extract_domain(item.get("url", ""))
        lines.append(f"{idx}. {title} [{site} {pub}] — {summary[:280]}")
    return "\n".join(lines) if len(lines) > 1 else None


def search_bing_rss(query: str, max_results: int = 4, timeout: float = 4.5) -> str | None:
    """零 Key 免费兜底：必应 RSS + 严格质量门禁（无关垃圾页直接丢弃）"""
    url = "https://cn.bing.com/search?format=rss&q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        xml_data = resp.read().decode("utf-8", errors="ignore")

    root = ET.fromstring(xml_data)
    items = root.findall(".//item")
    if not items:
        return None

    lines = [f"关于「{query}」的必应实时搜索结果："]
    kept = 0
    for it in items[: max_results * 2]:
        title = _strip_tags(it.findtext("title") or "")
        desc = _strip_tags(it.findtext("description") or "")
        if not _passes_quality_gate(query, title, desc):
            continue
        pub = (it.findtext("pubDate") or "").strip()[:16]
        domain = _extract_domain(it.findtext("link") or "")
        kept += 1
        lines.append(f"{kept}. {title} [{domain} {pub}] — {desc[:260]}")
        if kept >= max_results:
            break
    return "\n".join(lines) if kept > 0 else None


def web_search(query: str, max_results: int = 4) -> str:
    """
    智能检索入口：年份归一化 -> 24h 磁盘缓存 -> 多引擎路由 + 严格质量门禁。
    """
    q = _normalize_query(query)
    if not q:
        return "搜索关键词为空。"

    now = time.time()
    cached = _DISK_CACHE.get(q)
    if cached and (now - float(cached.get("ts", 0))) < CACHE_TTL:
        return str(cached["text"])

    result_text: str | None = None
    for engine_name, fn in (
        ("Zhipu", search_zhipu),
        ("Bocha", search_bocha),
        ("Tavily", search_tavily),
        ("Serper", search_serper),
        ("BingRSS", search_bing_rss),
    ):
        try:
            result_text = fn(q, max_results=max_results)
            if result_text:
                logger.info("🔍 联网搜索命中 [%s]: %s", engine_name, q)
                break
        except Exception as exc:
            logger.warning("%s 搜索失败 (%s): %s", engine_name, q, exc)

    if not result_text:
        return f"未能在网上查到关于「{q}」的可靠结果（已拦截无关垃圾页，请直接告知群友暂未查到确切消息，不要瞎编）。"

    _DISK_CACHE[q] = {"ts": now, "text": result_text}
    _save_cache(_DISK_CACHE)
    return result_text
