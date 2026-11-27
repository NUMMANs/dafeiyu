"""冷场找话题：群里长时间没人说话时，自己上网找个感兴趣的东西，写成一条群聊分享。

为什么单独一层：
- 这件事有「外部不确定性」——搜索可能失败、可能搜回垃圾页、可能撞上不该发的内容，
  所以搜索、去重、敏感词拦截、限长都在这里做，``brain`` 只负责出关键词和写那句话；
- 分享过的主题要落盘（``state/topic_shares.json``），进程重启后也不会把同一条
  新闻隔两天再发一遍；
- 抓取放在后台线程里跑（见 ``bot_core._topic_share_worker``），不阻塞 1 秒一次的
  去抖/投递巡检。

调参：安静多久触发、两次间隔、正文长度、带不带原帖链接都在 ``tuning`` 的 sense 组；
想换「她爱看什么」，改 ``roles/judges/topic_share.md``（热加载，不用重启）。
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from config import BASE_DIR, settings
import tuning
from web_search import web_search

logger = logging.getLogger("dafeiyu.topic")

STATE_PATH: Path = BASE_DIR / "state" / "topic_shares.json"
KEEP_PER_GROUP = 20
KEEP_DAYS = 30

# 这些方向发到群里只会招麻烦：政治时事、灾害事故、绯闻、医疗、投资、对立话题。
# 提示词里已经禁了一遍，这里再硬拦一遍——模型偶尔会把搜索结果里的东西照搬过来。
SENSITIVE_TERMS = (
    "政治", "时政", "政府", "选举", "领导人", "外交部", "战争", "军事冲突", "导弹",
    "地震", "洪水", "台风灾害", "事故", "伤亡", "遇难", "失联", "坠机", "凶杀", "自杀",
    "疫情", "确诊病例", "疫苗", "癌症", "偏方", "治病", "疗效",
    "股票", "基金", "加密货币", "比特币", "炒币", "彩票", "赌博", "网贷",
    "明星", "绯闻", "塌房", "离婚", "出轨",
    "女权", "男权", "地域黑", "移民",
)

_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"[ \t\u3000]+")
_PUNCT_RE = re.compile(r"[^\u4e00-\u9fffa-zA-Z0-9]")
_DATE_RE = re.compile(r"\[[^\]]*?(\d{4})-(\d{2})-(\d{2})[^\]]*\]")
_ITEM_RE = re.compile(r"^\s*\d+[.、]")
# 「一个味道」「特别怪的东西」这种说了等于没说的写法：分享必须让人知道是什么，
# 含糊不清的宁可换一条（实测模型会说「出了个特别怪的味道薯片」却不说是什么味）。
_VAGUE_RE = re.compile(
    r"(一个|某个|某种|这个|那个|特别怪|很奇怪|奇奇怪怪)的?(味道|口味|东西|零食|游戏|地方|玩意)"
    r"|什么[\u4e00-\u9fff]{0,3}(味|口味|东西|玩意|游戏|零食)"
    r"|出了个什么"
)

# 搜索结果超过这个天数就不要了。分享的卖点是「新」，一年半前的旧闻发进群很尴尬。
MAX_AGE_DAYS = 540

# 搜索结果行尾的链接标记（由 web_search 的 with_url 模式追加，正文里不出现）。
_LINK_TAG_RE = re.compile(r"\s*⟨(https?://[^⟩\s]+)⟩\s*$")
# 这些不是「原帖」：搜索引擎跳转页/站内搜索页，发出去也不像人话。
_BAD_LINK_HOSTS = ("bing.com", "google.com", "baidu.com", "sogou.com", "so.com", "sm.cn")
_BAD_LINK_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".rar",
    ".exe", ".apk", ".mp4", ".pdf",
)

_state_lock = threading.Lock()


def _load_state() -> dict:
    if not STATE_PATH.is_file():
        return {"groups": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("groups"), dict):
            return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("topic_shares.json 读不出来，按空处理: %s", exc)
    return {"groups": {}}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - KEEP_DAYS * 86400
        for gid, items in list(state["groups"].items()):
            kept = [it for it in items if float(it.get("ts") or 0) >= cutoff][-KEEP_PER_GROUP:]
            if kept:
                state["groups"][gid] = kept
            else:
                state["groups"].pop(gid, None)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError as exc:
        logger.warning("topic_shares.json 写入失败: %s", exc)


def recent_topics(group_id: str, limit: int = 10) -> list[str]:
    """最近分享过的主题词，用来避免连着几天说同一件事。"""
    with _state_lock:
        items = _load_state()["groups"].get(group_id) or []
    return [str(it.get("topic") or "") for it in items[-limit:] if it.get("topic")]


def recent_topics_across(limit: int = 12) -> list[str]:
    """所有群最近分享过的主题：两个群同一晚都冷场时，别把同一条发两遍。"""
    with _state_lock:
        groups = _load_state()["groups"]
        rows = [it for items in groups.values() for it in items]
    rows.sort(key=lambda it: float(it.get("ts") or 0))
    return [str(it.get("topic") or "") for it in rows[-limit:] if it.get("topic")]


def _record_share(group_id: str, topic: str, text: str, source: str, link: str = "") -> None:
    with _state_lock:
        state = _load_state()
        state["groups"].setdefault(group_id, []).append(
            {
                "ts": time.time(),
                "topic": topic,
                "text": text[:120],
                "source": source[:80],
                "link": link[:200],
            }
        )
        _save_state(state)


def _bigrams(text: str) -> set[str]:
    clean = _PUNCT_RE.sub("", (text or "").lower())
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def is_repeat(topic: str, avoid: list[str], threshold: float = 0.45) -> bool:
    """主题词二元组重合度够高就算重复（「新出的独立游戏」vs「独立游戏推荐」）。"""
    a = _bigrams(topic)
    if not a:
        return False
    for prev in avoid:
        b = _bigrams(prev)
        if not b:
            continue
        overlap = len(a & b) / max(1, min(len(a), len(b)))
        if overlap >= threshold:
            return True
    return False


def is_sensitive(text: str) -> str:
    """命中敏感方向就返回命中的词，调用方据此丢弃这条分享。"""
    probe = (text or "").lower()
    for term in SENSITIVE_TERMS:
        if term in probe:
            return term
    return ""


def is_vague(text: str) -> bool:
    """说了等于没说（「一个味道」「某个东西」）：群友看完不知道是什么，就不必发。"""
    return bool(_VAGUE_RE.search(text or ""))


def clean_share_text(text: str, max_chars: int) -> str:
    """把模型输出收成「像微信里发的一句话」：去链接/markdown/换行，按句截断。"""
    body = (text or "").replace("\r", "").strip()
    if not body:
        return ""
    body = _URL_RE.sub("", body)
    body = body.replace("**", "").replace("`", "").replace("##", "")
    body = re.sub(r"[ \t]*\n+[ \t]*", " ", body)
    body = _WS_RE.sub(" ", body)
    body = re.sub(r"\s*[，,]\s*[，,]+", "，", body)
    body = body.strip(" ，、；：")
    if len(body) <= max_chars:
        return body
    out = ""
    for part in re.split(r"(?<=[。！？!?；;])", body):
        if len(out) + len(part) > max_chars:
            break
        out += part
    out = out.strip()
    if len(out) >= max_chars // 2:
        return out
    return body[: max_chars - 1].rstrip("，、；： ") + "…"


def _search_with_deadline(query: str, timeout: float = 20.0, with_url: bool = False) -> str:
    """web_search 内部会逐个引擎串行重试，最坏能拖到半分钟；这里到点就不要了。

    线程是 daemon，超时后让它自生自灭，不影响主流程。
    """
    box: dict[str, str] = {}

    def _run() -> None:
        try:
            box["text"] = web_search(query, max_results=4, with_url=with_url)
        except Exception as exc:  # noqa: BLE001 - 搜索失败不该影响冒泡主流程
            box["err"] = str(exc)

    worker = threading.Thread(target=_run, daemon=True, name="topic-search")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        logger.warning("🌐 冷场找话题：搜索「%s」超时 %.0fs，放弃", query, timeout)
        return ""
    if box.get("err"):
        logger.warning("🌐 冷场找话题：搜索「%s」失败: %s", query, box["err"])
    return box.get("text") or ""


def _usable(material: str) -> bool:
    if not material or len(material) < 40:
        return False
    if "未能" in material and "查到" in material:
        return False
    return True


def is_shareable_url(url: str) -> bool:
    """这条链接像不像「一条能点开的原帖」。

    排除：搜索引擎自己的跳转/站内页、图片和安装包直链、超长带空格串。
    链接发群里是要让人真的点得开，宁可这一轮不带，也不要甩一条打不开的。
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")) or len(url) > 200:
        return False
    if any(ch in url for ch in " \t\r\n\"'<>"):
        return False
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parts.netloc or "").lower()
    if "." not in host:
        return False
    if any(host == bad or host.endswith("." + bad) for bad in _BAD_LINK_HOSTS):
        return False
    if (parts.path or "").lower().endswith(_BAD_LINK_EXT):
        return False
    return True


def split_sources(material: str) -> tuple[str, list[dict]]:
    """把结果行尾的 ⟨链接⟩ 摘掉：正文给模型，链接留给代码决定要不要发。

    模型看不到链接，就不会把它抄进正文（``clean_share_text`` 也会兜一层）。
    """
    clean_lines: list[str] = []
    sources: list[dict] = []
    for line in (material or "").splitlines():
        body, url = line, ""
        match = _LINK_TAG_RE.search(line)
        if match:
            body = line[: match.start()].rstrip()
            url = match.group(1).rstrip(".,;)]}'\"")
        if url and is_shareable_url(url):
            sources.append({"url": url, "line": body})
        clean_lines.append(body)
    return "\n".join(clean_lines).strip(), sources


def pick_source_url(evidence: str, sources: list[dict], threshold: float = 0.5) -> str:
    """找出模型引用的那句话出自哪条结果，返回那条结果的原帖链接。

    证据必须能定位到具体来源才给链接：链接和正文对不上，比不带链接更假。
    """
    e = _norm(evidence)
    if len(e) < 6 or not sources:
        return ""
    for item in sources:
        if e in _norm(item.get("line") or ""):
            return str(item.get("url") or "")
    # 证据可能横跨标题行和摘要行，退一步按二元组重合度挑最像的一条
    want = _bigrams(evidence)
    best_url, best_score = "", 0.0
    for item in sources:
        have = _bigrams(item.get("line") or "")
        if not have:
            continue
        score = len(want & have) / max(1, min(len(want), len(have)))
        if score > best_score:
            best_url, best_score = str(item.get("url") or ""), score
    return best_url if best_score >= threshold else ""


def choose_link(evidence: str, sources: list[dict]) -> str:
    """按概率决定这一轮带不带原帖链接；要带就必须指向正文真正引用的那条。

    默认三成左右带链接：每次都贴链接像营销号，一次都不贴又少了「随手转发」的真实感。
    链接由代码追加，模型自己写链接会被 ``clean_share_text`` 去掉。
    """
    prob = tuning.get_float("TOPIC_SHARE_LINK_PROB", settings.TOPIC_SHARE_LINK_PROB)
    if prob <= 0 or not sources:
        return ""
    if random.random() >= prob:
        logger.info("🌐 冷场找话题：这一轮按概率不带链接（%.0f%%）", prob * 100)
        return ""
    url = pick_source_url(evidence, sources)
    if not url:
        logger.info("🌐 冷场找话题：抽中带链接，但证据定位不到具体来源，本轮不带")
        return ""
    logger.info("🌐 冷场分享带原帖链接（%s）：%s", prob * 100, url[:100])
    return url


def _fresh_enough(line: str, now: datetime | None = None) -> bool:
    """带日期的结果行，超过 MAX_AGE_DAYS 就丢掉；没写日期的先留着（很多站不返回）。"""
    match = _DATE_RE.search(line)
    if not match:
        return True
    try:
        pub = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return True
    return ((now or datetime.now()) - pub).days <= MAX_AGE_DAYS


def filter_fresh(material: str) -> str:
    """把过期条目剔掉，只留标题行和新鲜条目；全过期就返回空串。"""
    kept: list[str] = []
    items = 0
    for line in (material or "").splitlines():
        if _ITEM_RE.match(line):
            items += 1
            if not _fresh_enough(line):
                logger.info("🌐 冷场找话题：丢掉过期条目 %s", line.strip()[:60])
                continue
        kept.append(line)
    if items and all(_ITEM_RE.match(ln) is None for ln in kept):
        return ""
    return "\n".join(kept).strip()


def _norm(text: str) -> str:
    return _PUNCT_RE.sub("", (text or "").lower())


def evidence_supported(evidence: str, material: str) -> bool:
    """模型必须指出「这句话来自材料哪一段」，且能对得上，否则视为编造。

    这是分享可信度的最后一道闸：提示词说了「别编」，模型照样会把 2021 年的怪味薯片
    写成「刚出的紫薯椰子味」。让它回抄证据，再用归一化子串比对，编的抄不出来。
    """
    e = _norm(evidence)
    if len(e) < 6:
        return False
    return e in _norm(material)


def numbers_supported(text: str, material: str) -> bool:
    """正文里的价格/年份/数量必须能在材料里找到，防止「售价 12 元」这种凭空数字。"""
    mat = _norm(material)
    for num in re.findall(r"\d{2,}(?:\.\d+)?", text or ""):
        if num not in mat:
            return False
    return True


def compose(brain, group_id: str, recent_lines: list[str]) -> dict | None:
    """完整走一遍：出关键词 → 搜 → 挑一条写成群聊话。返回 None 表示这轮不分享。

    返回 ``{"text", "topic", "mood", "source", "queries"}``。
    """
    if not settings.has_valid_api_key:
        return None

    avoid = list(dict.fromkeys(recent_topics(group_id) + recent_topics_across(12)))
    queries = brain.suggest_share_topics(recent_lines, avoid)
    queries = [q for q in queries if 4 <= len(q) <= 30][:2]
    if not queries:
        logger.info("🌐 冷场找话题：模型没给出可用关键词，本轮跳过")
        return None

    material_parts: list[str] = []
    sources: list[dict] = []
    used_queries: list[str] = []
    for q in queries:
        if is_sensitive(q):
            logger.info("🌐 冷场找话题：关键词命中敏感方向，跳过「%s」", q)
            continue
        text = _search_with_deadline(q, with_url=True)
        text = filter_fresh(text) if _usable(text) else ""
        if _usable(text):
            clean, found = split_sources(text)
            if not _usable(clean):
                continue
            material_parts.append(clean)
            sources.extend(found)
            used_queries.append(q)
        if len(material_parts) >= 2:
            break
    if not material_parts:
        logger.info("🌐 冷场找话题：搜不到可用结果（关键词 %s），本轮不发声", queries)
        return None

    material = "\n\n".join(material_parts)
    max_chars = tuning.get_int("TOPIC_SHARE_MAX_CHARS", 60)
    for attempt in range(2):
        out = brain.compose_topic_share(material, recent_lines, strict=attempt > 0)
        text = clean_share_text(str(out.get("text") or ""), max_chars)
        topic = " ".join(str(out.get("topic") or "").split())[:16]
        mood = str(out.get("mood") or "happy").lower()
        if not text or not topic:
            logger.info("🌐 冷场找话题：材料不值得分享或模型返回空，本轮跳过")
            return None

        hit = is_sensitive(f"{topic} {text}")
        if hit:
            logger.info("🌐 冷场找话题：内容命中敏感词「%s」，丢弃：%s", hit, text[:40])
            return None
        if is_repeat(topic, avoid):
            logger.info("🌐 冷场找话题：主题「%s」近期分享过，丢弃", topic)
            return None
        if is_vague(text):
            logger.info("🌐 冷场找话题：写得太含糊（没说清是什么），重试：%s", text[:40])
            continue
        if not evidence_supported(str(out.get("evidence") or ""), material):
            logger.info(
                "🌐 冷场找话题：第 %d 次写的内容对不上原文，丢：%s（证据 %r）",
                attempt + 1,
                text[:40],
                str(out.get("evidence") or "")[:40],
            )
            continue
        if not numbers_supported(text + topic, material):
            logger.info("🌐 冷场找话题：正文里的数字在材料里找不到，丢：%s", text[:40])
            continue

        evidence = str(out.get("evidence") or "")
        link = choose_link(evidence, sources)
        # 链接单独一行：微信里会显示成独立一条，像是顺手把原帖甩过来
        full_text = f"{text}\n{link}" if link else text
        _record_share(
            group_id, topic, full_text, used_queries[0] if used_queries else "", link
        )
        return {
            "text": full_text,
            "topic": topic,
            "mood": mood
            if mood in {"happy", "smug", "shocked", "angry", "sleepy", "gaming", "love", "hungry"}
            else "happy",
            "source": used_queries[0] if used_queries else "",
            "queries": used_queries,
            "evidence": evidence[:60],
            "link": link,
            "at": datetime.now().strftime("%H:%M"),
        }
    logger.info("🌐 冷场找话题：两次都没写出可核实的内容，本轮不发声")
    return None
