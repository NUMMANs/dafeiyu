"""今日生活：让大肥鱼有一条「今天也在过日子」的线。

为什么要有这一层：
她原来只会在冷场时上网找话题，久了像个资讯转发号——永远在讲别人的事，自己身上
什么都没发生。真人不是这样的：上午出门踩了水，晚上就会说鞋还没干；昨天买的薯片，
今天还在惦记。这一层就是给她一条前后连贯的日常。

怎么做的：
- 每天第一次真要分享时，才按 ``SLOTS`` 这个作息骨架编出今天 8 件事（懒生成：
  一天只花一次调用，整天没机会冒泡就一次都不花）；
- 写手默认是 CodeBuddy CLI（``LIFE_WRITER=codebuddy``，见 ``dsh_bridge.ask_codebuddy``），
  提示词里带整块人格卡；CLI 不可用/超时/输出解不出来时自动退回 DeepSeek，两条路共用
  同一份提示词（``brain.build_daily_life_prompt``），换后端不换文风；
- 结果落盘 ``state/daily_life.json``，一整天复用，所以上午买的东西晚上还能提起来；
- 只有「已经到点、还没讲过、且不超过 10 小时」的那条能发出去——不能下午就讲晚上的事，
  也不能半夜把早饭的事翻出来说；
- 生成时把前几天的剧情一起喂进去：不重复（不能天天捡猫），也能接着昨天的话头；
- 另外写一份 ``state/daily_life.md``：0:45 的记忆任务可以读它来写 L2 日记，
  这样日记里的「今天」和她群里说过的对得上。

调参：走生活线还是走网上话题的概率、正文长度在 ``tuning`` 的 sense 组；
想改「她平时过什么日子」，改 ``roles/judges/life.md``（热加载，不用重启）。
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from config import BASE_DIR, settings
from dsh_bridge import ask_codebuddy
import tuning

logger = logging.getLogger("dafeiyu.life")

STATE_PATH: Path = BASE_DIR / "state" / "daily_life.json"
MD_PATH: Path = BASE_DIR / "state" / "daily_life.md"

# 一天的骨架：时间点 / 在做什么 / 给模型的提示。
# 用户的例子是「9 点刷手机、10 点出门」，所以白天的骨架就照这个来；
# 整体贴合人设里的「有点懒、爱摸鱼、爱打游戏、爱吃」——起床晚、零食多、晚上开黑。
SLOTS: tuple[tuple[str, str, str], ...] = (
    ("08:20", "起床", "刚醒，赖床、摸手机、拖延"),
    ("09:40", "刷手机", "刷到一个具体的东西或视频"),
    ("11:00", "出门", "路上/楼下/店里看到的、听到的"),
    ("12:40", "吃饭", "吃了什么，好吃还是一般"),
    ("15:00", "在家", "打游戏/看视频/收拾屋子"),
    ("17:40", "傍晚", "嘴馋、买零食、弄点吃的"),
    ("20:20", "晚上", "开黑/排位/看比赛"),
    ("22:40", "睡前", "躺着刷手机、犯困"),
)

# 超过这么久还没讲过的事就不讲了：半夜说「我今天出门看见…」不像活人干的事。
MAX_AGE_HOURS = 10
# 留几天的剧情，用来避免重复 + 给「接着昨天」留材料
KEEP_DAYS = 6
# 生成失败后隔多久再试（否则每次巡检都会去调一遍模型）
RETRY_AFTER_SEC = 1800

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_lock = threading.RLock()
# 后台补生成是否正在跑（连问几句「在干嘛」时只补一次）
_ensure_inflight = False


def _empty() -> dict:
    return {"date": "", "slots": [], "recent": [], "life_miss": 0, "last_kind": ""}


def _load() -> dict:
    if not STATE_PATH.is_file():
        return _empty()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("slots"), list):
            data.setdefault("recent", [])
            return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("daily_life.json 读不出来，按空处理: %s", exc)
    return _empty()


def _render_md(state: dict) -> str:
    """给人看的一份：夜里的记忆任务要照它写日记，所以必须是没有 Markdown 表格的纯清单。"""
    if not state.get("slots"):
        return ""
    date_str = str(state.get("date") or "")
    head = f"# 大肥鱼今天的生活（{date_str}）\n\n"
    lines = []
    for slot in state["slots"]:
        mark = "（群里说过了）" if float(slot.get("shared_at") or 0) > 0 else ""
        detail = str(slot.get("detail") or "").strip()
        tail = f"：{detail}" if detail else ""
        lines.append(f"- {slot.get('at')} {slot.get('kind')}：{slot.get('title')}{tail}{mark}")
    return head + "\n".join(lines) + "\n"


def _save(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now().timestamp() - KEEP_DAYS * 86400
        recent = [
            it
            for it in (state.get("recent") or [])
            if float(it.get("ts") or 0) >= cutoff
        ][-KEEP_DAYS:]
        state["recent"] = recent
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_PATH)
        MD_PATH.write_text(_render_md(state), encoding="utf-8")
    except OSError as exc:
        logger.warning("daily_life.json 写入失败: %s", exc)


def _norm_text(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


_FRAGMENT_STARTS = tuple("的了是着过地得又还就才也都")

def _looks_standalone(rest: str) -> bool:
    """削掉开头后剩下的半句还读得通吗。

    「楼下烤红薯的摊子支起来了」如果照标题长度硬削，会剩下「的摊子支起来了」这种东西，
    比留着重复更难读。剩下太短、或者以一个虚词开头，就宁可保留原文。
    """
    text = (rest or "").strip()
    return len(text) >= 6 and not text.startswith(_FRAGMENT_STARTS)


def _dedupe_detail(title: str, detail: str) -> str:
    """CodeBuddy 爱把 detail 写成「标题 + 补充」，这里把重复的开头削掉。

    不去掉的话，喂给「写这一句话」的模型就是同一个信息说两遍，写出来的分享也会绕回来
    重复一遍标题（实测会出现「刷到修牛蹄的视频，一个师傅给牛修蹄子…」这种同义反复）。
    阈值 0.45 是照实测标定的：真正的改写落在 0.48~0.59，同一件事换个说法就在这个区间。
    """
    body = (detail or "").strip()
    if not body:
        return ""
    head = re.split(r"[，,。；;：:]", body, maxsplit=1)[0].strip()
    if head and SequenceMatcher(None, _norm_text(title), _norm_text(head)).ratio() >= 0.45:
        rest = body[len(head) :].lstrip("，,。；;：: ")
        if _looks_standalone(rest):
            return rest
        return body
    key = _norm_text(title)
    if key and _norm_text(body).startswith(key):
        rest = body[len(title) :].lstrip("，,。；;：: ")
        if _looks_standalone(rest):
            return rest
    return body


def _at_minutes(at: str) -> int:
    try:
        hour, minute = at.split(":")
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return 0


def _now_minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _pending(state: dict, now: datetime) -> list[dict]:
    """已到点、还没讲过、也没过期的那几条。"""
    if state.get("date") != now.strftime("%Y-%m-%d"):
        return []
    now_m = _now_minutes(now)
    out: list[dict] = []
    for slot in state.get("slots") or []:
        if float(slot.get("shared_at") or 0) > 0:
            continue
        at_m = _at_minutes(str(slot.get("at") or ""))
        if at_m > now_m:
            continue
        if now_m - at_m > MAX_AGE_HOURS * 60:
            continue
        if not str(slot.get("title") or "").strip():
            continue
        out.append(slot)
    return out


def _available(state: dict, now: datetime) -> bool:
    """今天还有没有可能讲点自己的生活。

    今天还没生成过也算「有」——真去讲的时候才现生成（懒生成，省掉整天没机会冒泡时的调用）。
    生成完发现是空的（模型失败/时间还没到），这里自然就变成 False，不会一直空转。
    """
    if state.get("date") != today_str(now):
        return True
    return bool(_pending(state, now))


def has_candidate(now: datetime | None = None) -> bool:
    """今天还有没有能讲的、已经发生过的事（还没生成也算有）。"""
    now = now or datetime.now()
    with _lock:
        return _available(_load(), now)


def _pick(state: dict, now: datetime) -> dict | None:
    """挑一条来分享：越有意思、离现在越近，越容易被挑中。

    不做成「按时间顺序讲最早的」，因为早上 8 点那条往往是「赖床」——第一次开口就讲
    这个，群里只会得到一个「哦」。让模型自己打的 interest 分数参与抽签更自然。
    """
    cands = _pending(state, now)
    if not cands:
        return None
    now_m = _now_minutes(now)
    weights: list[float] = []
    for slot in cands:
        interest = float(slot.get("interest") or 3)
        gap = now_m - _at_minutes(str(slot.get("at") or ""))
        # 4 小时内的加权重：刚发生的事说起来最自然
        recency = 1.8 if gap <= 240 else 1.0
        weights.append(max(0.1, interest) * recency)
    return random.choices(cands, weights=weights, k=1)[0]


def _when_label(at: str, now: datetime) -> str:
    """给模型的时间说法：别把上午的事写成「刚发生」。"""
    hour = _at_minutes(at) // 60
    if hour < 5:
        period = "凌晨"
    elif hour < 9:
        period = "早上"
    elif hour < 11:
        period = "上午"
    elif hour < 13:
        period = "中午"
    elif hour < 17:
        period = "下午"
    elif hour < 19:
        period = "傍晚"
    else:
        period = "晚上"
    gap = _now_minutes(now) - _at_minutes(at)
    if 0 <= gap <= 90:
        return "刚才"
    return period


def _season_hint(now: datetime) -> str:
    month = now.month
    if month in (12, 1, 2):
        return "冬天，外面很冷，手伸出被窝都嫌冻"
    if month in (3, 4, 5):
        return "春天，忽冷忽热，容易穿错衣服"
    if month in (6, 7, 8):
        return "夏天，热得不想动，空调和冰饮是命"
    return "秋天，早晚凉，白天晒"


def _recent_titles(state: dict, limit: int = 24) -> list[str]:
    rows: list[str] = []
    for day in state.get("recent") or []:
        for title in day.get("titles") or []:
            if title:
                rows.append(str(title)[:24])
    return rows[-limit:]


def _generate(brain, now: datetime, state: dict) -> list[dict]:
    """让模型编出今天这 8 件事。失败返回空列表，由调用方决定这轮不分享。

    主写手是 CodeBuddy（``LIFE_WRITER=codebuddy``，走订阅额度，出来的东西更像人过的日子）；
    CLI 没装、没登录、超时、输出解不出来时自动退回 DeepSeek——
    宁可这次是 DeepSeek 写的，也不能让她今天没有生活。
    两条路共用 ``brain.build_daily_life_prompt`` 同一份提示词（都带人格卡），
    换后端不会换文风。
    """
    extra = tuning.judge_extra("life").strip()
    date_str = now.strftime("%Y-%m-%d")
    args = {
        "date_str": date_str,
        "weekday": _WEEKDAYS[now.weekday()],
        "season_hint": _season_hint(now),
        "recent_titles": _recent_titles(state),
        "slots_spec": [(at, kind, hint) for at, kind, hint in SLOTS],
        "extra": extra,
    }

    items: list[dict] = []
    if (settings.LIFE_WRITER or "").strip().lower() == "codebuddy":
        try:
            prompt = brain.build_daily_life_prompt(**args)
            raw = ask_codebuddy(
                prompt, timeout=settings.LIFE_WRITER_TIMEOUT, purpose="life-plan"
            )
            items = brain.parse_daily_life_json(raw) if raw else []
        except Exception as exc:  # noqa: BLE001 - CLI 出问题不该让她今天没日子过
            logger.warning("🗓️ CodeBuddy 编生活线失败: %s", exc)
        if items:
            logger.info("🗓️ 今日生活由 CodeBuddy 生成（%d 条）", len(items))
        else:
            logger.info("🗓️ CodeBuddy 没编出生活线，退回 DeepSeek")
    if not items:
        items = brain.generate_daily_life(**args)
    if not items:
        return []

    canonical = [at for at, _kind, _hint in SLOTS]
    slots: list[dict] = []
    used_at: set[str] = set()
    for index, item in enumerate(items[: len(SLOTS)]):
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:24]
        if len(title) < 4:
            continue
        at = str(item.get("at") or "").strip()
        if at not in canonical or at in used_at:
            at = canonical[min(index, len(canonical) - 1)]
        if at in used_at:
            continue
        used_at.add(at)
        detail = _dedupe_detail(title, " ".join(str(item.get("detail") or "").split())[:60])
        try:
            interest = int(item.get("interest") or 3)
        except (TypeError, ValueError):
            interest = 3
        kind = next(k for a, k, _ in SLOTS if a == at)
        slots.append(
            {
                "at": at,
                "kind": kind,
                "title": title,
                "detail": detail,
                "interest": max(1, min(5, interest)),
                "mood": str(item.get("mood") or "happy").lower(),
                "shared_at": 0,
            }
        )
    if len(slots) < 4:
        logger.info("今日生活：只生成出 %d 条可用的事，今天先不启用", len(slots))
        return []
    slots.sort(key=lambda s: _at_minutes(s["at"]))
    return slots


def ensure_today(brain, now: datetime | None = None) -> dict:
    """保证 state 里是今天的剧情；没有就现生成。返回最新 state。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    with _lock:
        state = _load()
        if state.get("date") == today and state.get("slots"):
            return state
        failed_at = float(state.get("gen_fail_ts") or 0)
        if state.get("date") == today and (now.timestamp() - failed_at) < RETRY_AFTER_SEC:
            return state  # 刚失败过，别每轮都去调模型
        prev_state = state

    # 生成放在锁外：一次模型调用要十几秒，而 context_line() 每轮回复都要拿锁，
    # 在锁里调模型会把所有群的消息卡住十几秒。
    slots = _generate(brain, now, prev_state)
    with _lock:
        state = _load()
        if state.get("date") == today and state.get("slots"):
            return state  # 另一个群的线程先跑完了，直接用它的，别再覆盖
        if not slots:
            state["date"] = today
            state["slots"] = []
            state["gen_fail_ts"] = now.timestamp()
            _save(state)
            return state
        # 昨天的剧情归档进 recent：既防重复，也让「接着昨天」有材料
        prev = state.get("slots") or []
        if prev:
            state["recent"] = list(state.get("recent") or []) + [
                {
                    "date": state.get("date") or "",
                    "ts": now.timestamp(),
                    "titles": [str(s.get("title") or "")[:24] for s in prev],
                }
            ]
        state["date"] = today
        state["slots"] = slots
        state["gen_fail_ts"] = 0
        _save(state)
        logger.info(
            "🗓️ 今日生活生成好了（%d 条）：%s",
            len(slots),
            " / ".join(str(s["title"])[:14] for s in slots[:4]),
        )
        return state


def wants_turn() -> bool:
    """这一轮冷场，是先讲今天的生活，还是上网找话题。

    概率抽签 + 「欠账」：连着两次抽到上网，下一轮概率会抬高（每次 +0.25，封顶 0.9），
    免得运气不好一整天都在讲别人的事，自己的生活一次都没讲。
    """
    if not settings.ENABLE_DAILY_LIFE:
        return False
    now = datetime.now()
    with _lock:
        state = _load()
        if not _available(state, now):
            return False
        miss = int(state.get("life_miss") or 0)
    base = tuning.get_float("LIFE_SHARE_PROB", settings.LIFE_SHARE_PROB)
    prob = min(0.9, base + 0.25 * miss)
    hit = random.random() < prob
    if not hit:
        logger.info("🗓️ 这轮先上网找话题（生活线已欠 %d 次，下次概率 %.0f%%）", miss + 1, prob * 100)
    return hit


def note_result(kind: str) -> None:
    """记一笔「这轮讲的是生活还是网上的事」，用来算欠账。"""
    with _lock:
        state = _load()
        state["last_kind"] = kind
        if kind == "life":
            state["life_miss"] = 0
        else:
            state["life_miss"] = int(state.get("life_miss") or 0) + 1
        _save(state)


def mark_shared(at: str, text: str) -> None:
    """这条已经讲过了：打上时间戳，顺带把原文留在 state 里方便事后核对。"""
    with _lock:
        state = _load()
        for slot in state.get("slots") or []:
            if str(slot.get("at") or "") == at and not float(slot.get("shared_at") or 0):
                slot["shared_at"] = datetime.now().timestamp()
                slot["shared_text"] = (text or "")[:100]
                break
        _save(state)


def compose(brain, group_id: str, recent_lines: list[str]) -> dict | None:
    """走一遍：保证今天有剧情 → 挑一条能讲的 → 写一句群聊话。None 表示这轮不分享。"""
    if not settings.has_valid_api_key:
        return None

    import topic_bubble  # 复用清洗/闸门，避免两套规则各写一遍

    now = datetime.now()
    state = ensure_today(brain, now)
    with _lock:
        slot = _pick(state, now)
    if not slot:
        return None

    avoid = [str(s.get("title") or "") for s in (state.get("slots") or []) if s is not slot]
    avoid += _recent_titles(state, 12)
    when = _when_label(str(slot["at"]), now)
    event = {
        "at": slot["at"],
        "kind": slot["kind"],
        "title": slot["title"],
        "detail": slot.get("detail") or "",
    }
    out = brain.compose_life_share(event, when, recent_lines)
    max_chars = tuning.get_int("LIFE_MAX_CHARS", settings.LIFE_MAX_CHARS)
    text = topic_bubble.clean_share_text(str(out.get("text") or ""), max_chars)
    topic = " ".join(str(out.get("topic") or "").split())[:16] or str(slot["title"])[:16]
    if not text:
        logger.info("🗓️ 今日生活：模型没写出可用的话，本轮跳过（%s）", slot["title"])
        return None

    hit = topic_bubble.is_sensitive(f"{topic} {text}")
    if hit:
        logger.info("🗓️ 今日生活：内容命中敏感词「%s」，丢弃：%s", hit, text[:40])
        return None
    if topic_bubble.is_vague(text):
        logger.info("🗓️ 今日生活：写得太含糊（没说清是什么），丢弃：%s", text[:40])
        return None
    if topic_bubble.is_repeat(topic, avoid):
        logger.info("🗓️ 今日生活：「%s」和前几天说过的太像了，丢弃", topic)
        return None

    mark_shared(str(slot["at"]), text)
    mood = str(out.get("mood") or slot.get("mood") or "happy").lower()
    if mood not in {"happy", "smug", "shocked", "angry", "sleepy", "gaming", "love", "hungry"}:
        mood = "happy"
    logger.info("🗓️ 今日生活分享（%s %s）：%s", slot["at"], slot["kind"], text[:60])
    return {
        "text": text,
        "topic": topic,
        "mood": mood,
        "source": f"life:{slot['at']}",
        "at": now.strftime("%H:%M"),
    }


def context_line(max_items: int = 4) -> str:
    """给正常回复用的「你今天」块：让别人问起「在干嘛」时她前后对得上。

    只放已经发生的事，最多 4 条——既是省 token，也是防她张口就把一整天的流水账念出来。
    """
    now = datetime.now()
    state = _load()
    if state.get("date") != today_str(now) or not state.get("slots"):
        return ""
    passed = [
        s
        for s in state["slots"]
        if _at_minutes(str(s.get("at") or "")) <= _now_minutes(now) and s.get("title")
    ][-max_items:]
    if not passed:
        return ""
    lines = []
    for slot in passed:
        detail = str(slot.get("detail") or "").strip()
        tail = f"，{detail}" if detail else ""
        lines.append(f"- {slot.get('at')} {slot.get('kind')}：{slot.get('title')}{tail}")
    return (
        "【你今天】（你自己过的日子，不是群里谁说的。别人问起或者正好聊到就自然用，"
        "没人问别提，也别一次性念成清单）\n" + "\n".join(lines)
    )


def today_str(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


# 「在干嘛」这类问句。必须有主语或时间词兜着，否则「你干嘛啊」这种责怪、
# 「干嘛要这样」这种反问也会被当成询问，白白塞一段生活线进上下文。
_ASK_LIFE_RE = re.compile(
    r"(?:你|你们|鱼|肥鱼|大肥鱼|现在|这会儿|今天|刚刚|刚才)[^，。！？\n]{0,6}?"
    r"(?:在忙|忙不忙|在做|在干|干啥|干什么|做什么|做啥|忙啥|忙什么)"
    r"|(?:在干嘛|干嘛呢|干啥呢|做啥呢|在忙吗|忙啥呢|忙什么呢|在忙什么|在做什么)"
)


def _current_slots(state: dict, now: datetime) -> tuple[dict | None, dict | None]:
    """返回 (现在大概正在做的那段, 下一段)。今天还没到第一段时前者是 None。"""
    slots = [s for s in (state.get("slots") or []) if s.get("title")]
    now_m = _now_minutes(now)
    cur = None
    nxt = None
    for slot in slots:
        at_m = _at_minutes(str(slot.get("at") or ""))
        if at_m <= now_m:
            cur = slot
        elif nxt is None:
            nxt = slot
    return cur, nxt


def question_hint(text: str, now: datetime | None = None) -> str:
    """有人问「你在干嘛」时，补一条「就照这段答」的指令；不是问句就返回空串。

    只把【你今天】塞进上下文还不够：模型更愿意顺着群里正在聊的话题答，
    被问「在干嘛」时容易回一句「在刷手机」这种没内容的敷衍，或者干脆现编一件
    跟今天对不上的事。这里点明「这个点她正在做哪一件」，并附上「别另编行程、
    别把一天念完」的约束，前后才对得上。
    """
    if not text or not _ASK_LIFE_RE.search(text):
        return ""
    now = now or datetime.now()
    state = _load()
    if state.get("date") != today_str(now) or not state.get("slots"):
        return ""
    cur, nxt = _current_slots(state, now)
    if not cur:
        # 还没到今早第一段：按作息就是还没起，别让她凭空编一趟早市
        if not nxt:
            return ""
        logger.info("🐟 有人在问「在干嘛」，今天还没到第一段，按「还没起」作答")
        return (
            "【本轮：对方在问你现在在干嘛】按今天的作息你这时候还没起"
            f"（{nxt.get('at')} 才{nxt.get('kind')}）。照「还没起／在床上赖着」这种答，"
            "一句话，别编别的行程。"
        )
    detail = str(cur.get("detail") or "").strip()
    tail = f"，{detail}" if detail else ""
    soon = ""
    if nxt:
        gap = _at_minutes(str(nxt.get("at") or "")) - _now_minutes(now)
        if 0 < gap <= 60:
            soon = f"再过一会儿打算{nxt.get('kind')}。"
    logger.info(
        "🐟 有人在问「在干嘛」，按生活线作答：%s %s",
        cur.get("at"),
        str(cur.get("title"))[:20],
    )
    return (
        "【本轮：对方在问你现在在干嘛】就照今天这一段答，别另编行程：\n"
        f"- 现在大概是：{cur.get('at')} {cur.get('kind')}，{cur.get('title')}{tail}\n"
        f"{soon}"
        "一句话说完，像随口说的；别报备，也别把今天其它几段一起倒出来。"
    )


def ensure_today_async(brain) -> None:
    """今天还没有生活线时，后台补生成一次（别人问「在干嘛」用得上）。

    生成一次要十几秒（CodeBuddy 写手最长 120 秒），绝不能卡在回消息的路径上：
    这一轮先不带，生成完了下一轮问起就能对上。失败过的半小时内不再重试，
    免得每句「在干嘛」都去调一次模型。
    """
    now = datetime.now()
    state = _load()
    if state.get("date") == today_str(now) and state.get("slots"):
        return
    failed_at = float(state.get("gen_fail_ts") or 0)
    if state.get("date") == today_str(now) and (now.timestamp() - failed_at) < RETRY_AFTER_SEC:
        return

    # 同一时刻只允许一个补生成线程：连问三句「在干嘛」不该触发三次模型调用
    global _ensure_inflight
    with _lock:
        if _ensure_inflight:
            return
        _ensure_inflight = True

    def _run() -> None:
        global _ensure_inflight
        try:
            ensure_today(brain, datetime.now())
        except Exception as exc:  # noqa: BLE001 - 后台补生成失败不影响聊天
            logger.warning("今日生活：后台补生成失败: %s", exc)
        finally:
            with _lock:
                _ensure_inflight = False

    threading.Thread(target=_run, name="daily-life-ensure", daemon=True).start()


def snapshot() -> dict:
    """排查用：今天生成到哪一步了。"""
    state = _load()
    now = datetime.now()
    return {
        "date": state.get("date"),
        "today": today_str(now),
        "slots": [
            {
                "at": s.get("at"),
                "kind": s.get("kind"),
                "title": s.get("title"),
                "interest": s.get("interest"),
                "shared": bool(float(s.get("shared_at") or 0) > 0),
            }
            for s in (state.get("slots") or [])
        ],
        "pending": [s.get("at") for s in _pending(state, now)],
        "life_miss": state.get("life_miss", 0),
        "last_kind": state.get("last_kind", ""),
        "recent_days": len(state.get("recent") or []),
    }


if __name__ == "__main__":
    import os
    import sys

    os.chdir(BASE_DIR)
    if "--regen" in sys.argv:
        from brain import brain as brain_singleton

        STATE_PATH.unlink(missing_ok=True)
        ensure_today(brain_singleton)
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
    if MD_PATH.is_file():
        print()
        print(MD_PATH.read_text(encoding="utf-8"))
