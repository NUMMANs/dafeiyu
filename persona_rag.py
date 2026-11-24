"""动态语感检索（Persona RAG）：按当前对话语境召回少量真实原话作语感参考，防重复、防照抄。"""

from __future__ import annotations

import json
import logging
import random
import threading
from collections import deque
from pathlib import Path
from typing import Any

from config import BASE_DIR, DATA_DIR

logger = logging.getLogger("dafeiyu.persona_rag")

_CACHE_LOCK = threading.Lock()
_CORPUS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_RECENT_USED_IDS: dict[str, deque[str]] = {}

_SCENE_CUES: list[tuple[str, tuple[str, ...]]] = [
    (
        "flex_roast",
        ("年终奖", "高薪", "月薪", "年薪", "豪车", "买房", "买表", "五千", "一万", "十万", "百万", "战绩", "五杀", "mvp", "晒", "炫"),
    ),
    (
        "life_rant",
        ("加班", "辞职", "裸辞", "老板", "累死", "打工", "上班", "失业", "穷", "房贷", "焦虑", "分手", "失恋", "吵架", "对象", "相亲", "结婚", "单身", "破防", "难受"),
    ),
    (
        "pushback",
        ("懂个屁", "胡说", "瞎说", "闭嘴", "傻", "菜", "滚", "急了", "杠", "吹牛", "骗人", "机器人", "ai"),
    ),
    (
        "topic_talk",
        ("游戏", "排位", "连跪", "大乱斗", "原神", "电脑", "手机", "键盘", "旅游", "出国", "城市", "吃啥", "外卖", "奶茶", "咖啡", "健身", "电影"),
    ),
]


def _resolve_corpus_path(persona_name: str) -> Path | None:
    norm = (persona_name or "").strip().lower()
    if not norm:
        return None
    for candidate in (
        DATA_DIR / "persona_corpus" / f"{norm}_quotes.json",
        BASE_DIR / "roles" / "personas" / f"{norm}_quotes.json",
    ):
        if candidate.is_file():
            return candidate
    return None


def _load_corpus(persona_name: str) -> list[dict[str, Any]]:
    path = _resolve_corpus_path(persona_name)
    if path is None:
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []

    key = str(path.resolve())
    with _CACHE_LOCK:
        cached = _CORPUS_CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = [x for x in raw if isinstance(x, dict) and x.get("text")] if isinstance(raw, list) else []
        except Exception as exc:
            logger.warning("加载语感语料失败 (%s): %s", path.name, exc)
            items = []
        _CORPUS_CACHE[key] = (mtime, items)
        return items


def _detect_scene(text: str) -> str:
    low = (text or "").lower()
    best_scene = "casual_chat"
    best_score = 0
    for scene, cues in _SCENE_CUES:
        score = sum(1 for c in cues if c in low)
        if score > best_score:
            best_score = score
            best_scene = scene
    return best_scene


def retrieve_style_hints(
    persona_name: str,
    user_text: str,
    recent_history: list[dict[str, Any]] | None = None,
    group_id: str = "",
    top_k: int = 2,
) -> str:
    """按当前消息语境检索 0~top_k 条不重复的真实原话作语感参考。"""
    corpus = _load_corpus(persona_name)
    if not corpus or top_k <= 0:
        return ""

    combined = (user_text or "").strip()
    if recent_history:
        tail_texts = [
            str(m.get("content") or "")
            for m in recent_history[-3:]
            if m.get("role") == "user" and m.get("content")
        ]
        if tail_texts:
            combined = f"{combined} {' '.join(tail_texts)}"

    scene = _detect_scene(combined)
    group_key = f"{persona_name}:{(group_id or 'default').strip()}"

    with _CACHE_LOCK:
        used_deque = _RECENT_USED_IDS.setdefault(group_key, deque(maxlen=15))
        used_set = set(used_deque)

    scored: list[tuple[float, dict[str, Any]]] = []
    for item in corpus:
        iid = str(item.get("id") or "")
        if iid and iid in used_set:
            continue
        score = 0.0
        if item.get("scene") == scene:
            score += 3.0
        elif item.get("scene") == "casual_chat":
            score += 1.0
        kws = item.get("keywords") or []
        for kw in kws:
            if isinstance(kw, str) and kw and kw in combined:
                score += 2.0
        # 适度随机抖动，避免同类型问题总是召回固定两条
        score += random.uniform(0.0, 1.2)
        scored.append((score, item))

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [item for _, item in scored[:top_k]]

    with _CACHE_LOCK:
        used_deque = _RECENT_USED_IDS.setdefault(group_key, deque(maxlen=15))
        for it in picked:
            iid = str(it.get("id") or "")
            if iid:
                used_deque.append(iid)

    lines = [
        "【本轮真实原声语感参考（只学说话松弛度、接地气切入角度与断句节奏，严禁照抄原句、严禁生搬无关名词）】"
    ]
    for idx, it in enumerate(picked, 1):
        lines.append(f"- 语感{idx}：「{str(it['text']).strip()}」")
    return "\n".join(lines)
