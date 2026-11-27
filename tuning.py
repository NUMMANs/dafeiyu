"""运行期轻量热调参层（按文件 mtime 热加载，带硬上下界保护，无需重启）。

- ``data/tuning.json``      数值闸门（半主动唤醒阈值、L1 识海召回、L3 注文注入、工具路由）
- ``roles/judges/*.md``     轻量判定器补充规则（l2 / l2_5 / tool_router）
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from config import BASE_DIR, DATA_DIR

logger = logging.getLogger("dafeiyu.tuning")

TUNING_PATH = DATA_DIR / "tuning.json"
JUDGE_DIR = BASE_DIR / "roles" / "judges"

BOUNDS: dict[str, tuple[float, float]] = {
    # 半主动唤醒与去抖
    "TOPIC_INTEREST_MIN_SCORE": (0.55, 0.92),
    "FOLLOWUP_WINDOW_SEC": (120, 900),
    "TOPIC_GAP_SEC": (300, 1800),
    "PROACTIVE_ENERGY_MIN": (45.0, 92.0),
    "IMAGE_REPLY_PROB": (0.0, 0.7),
    "TRIGGER_DEBOUNCE_SEC": (2.0, 10.0),
    "TRIGGER_BURST_MAX_ITEMS": (2, 10),
    "VISION_SUMMARY_CHARS": (80, 400),

    # L1 识海召回
    "RECALL_SCORE_THRESHOLD": (0.15, 0.45),
    "MIN_LITERAL_HITS": (1, 3),
    "RECALL_TOP_N": (3, 8),
    "RECALL_MAX_CHARS": (200, 800),

    # 工具路由
    "TOOL_ROUTER_MIN_CHARS": (4, 20),

    # 上下文注入开关与限额
    "L3_NOTE_MAX_ITEMS": (1, 8),
    "L3_NOTE_MAX_CHARS": (40, 300),
    "L3_INJECT_ON": (0, 1),
    "RECALL_INJECT_ON": (0, 1),
    "DIARY_INJECT_ON": (0, 1),
    "DIARY_DAYS": (1, 7),
    "DIARY_MAX_ITEMS": (1, 3),
    "DIARY_ITEM_CHARS": (30, 100),
    "DIARY_MAX_CHARS": (60, 240),
}

GROUPS: dict[str, tuple[str, ...]] = {
    "sense": (
        "IMAGE_REPLY_PROB",
        "PROACTIVE_ENERGY_MIN",
        "TRIGGER_DEBOUNCE_SEC",
        "TRIGGER_BURST_MAX_ITEMS",
        "VISION_SUMMARY_CHARS",
    ),
    "mind": (
        "RECALL_SCORE_THRESHOLD",
        "MIN_LITERAL_HITS",
        "RECALL_TOP_N",
        "RECALL_MAX_CHARS",
        "L3_NOTE_MAX_ITEMS",
        "L3_NOTE_MAX_CHARS",
        "L3_INJECT_ON",
        "RECALL_INJECT_ON",
        "DIARY_INJECT_ON",
        "DIARY_DAYS",
        "DIARY_MAX_ITEMS",
        "DIARY_ITEM_CHARS",
        "DIARY_MAX_CHARS",
    ),
    "core": (
        "TOPIC_INTEREST_MIN_SCORE",
        "FOLLOWUP_WINDOW_SEC",
        "TOPIC_GAP_SEC",
        "TOOL_ROUTER_MIN_CHARS",
    ),
}

LEGACY_ALIASES: dict[str, str] = {
    "TOPIC_INTEREST_MIN_SCORE": "TOPIC_ROAST_MIN_SCORE",
}

STEP_LIMITS: dict[str, float] = {
    "IMAGE_REPLY_PROB": 0.1,
    "PROACTIVE_ENERGY_MIN": 8.0,
    "TRIGGER_DEBOUNCE_SEC": 1.0,
    "TRIGGER_BURST_MAX_ITEMS": 1,
    "VISION_SUMMARY_CHARS": 60,
    "L3_NOTE_MAX_ITEMS": 1,
    "L3_NOTE_MAX_CHARS": 60,
    "L3_INJECT_ON": 1,
    "RECALL_INJECT_ON": 1,
    "DIARY_INJECT_ON": 1,
    "DIARY_DAYS": 1,
    "DIARY_MAX_ITEMS": 1,
    "DIARY_ITEM_CHARS": 20,
    "DIARY_MAX_CHARS": 40,
    "RECALL_SCORE_THRESHOLD": 0.03,
    "MIN_LITERAL_HITS": 1,
    "RECALL_TOP_N": 1,
    "RECALL_MAX_CHARS": 100,
    "TOPIC_INTEREST_MIN_SCORE": 0.05,
    "FOLLOWUP_WINDOW_SEC": 150,
    "TOPIC_GAP_SEC": 300,
    "TOOL_ROUTER_MIN_CHARS": 2,
}

_cache_lock = threading.Lock()
_knobs_cache: tuple[float, dict[str, Any]] = (0.0, {})
_judge_cache: dict[str, tuple[float, str]] = {}


def _load_knobs() -> dict[str, Any]:
    global _knobs_cache
    try:
        mtime = TUNING_PATH.stat().st_mtime
    except OSError:
        return {}
    if _knobs_cache[0] == mtime:
        return _knobs_cache[1]
    try:
        raw = json.loads(TUNING_PATH.read_text(encoding="utf-8"))
        knobs = raw.get("knobs") if isinstance(raw, dict) else None
        data = knobs if isinstance(knobs, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("tuning.json 读取失败，按默认值走: %s", exc)
        data = {}
    _knobs_cache = (mtime, data)
    return data


def get(name: str, default: Any) -> Any:
    knobs = _load_knobs()
    raw = knobs.get(name)
    if raw is None:
        legacy = LEGACY_ALIASES.get(name)
        if legacy:
            raw = knobs.get(legacy)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    low, high = BOUNDS.get(name, (float("-inf"), float("inf")))
    if not (low <= value <= high):
        return default
    return int(value) if isinstance(default, int) else value


def get_int(name: str, default: int) -> int:
    return int(get(name, default))


def get_float(name: str, default: float) -> float:
    return float(get(name, default))


def current_knobs() -> dict[str, Any]:
    return dict(_load_knobs())


def _judge_path(kind: str) -> Path:
    return JUDGE_DIR / f"{kind}.md"


def judge_extra(kind: str) -> str:
    path = _judge_path(kind)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    with _cache_lock:
        cached = _judge_cache.get(kind)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith(("#", "<!--"))
    ]
    text = "\n".join(lines).strip()
    with _cache_lock:
        _judge_cache[kind] = (mtime, text)
    return text


def _interest_zone() -> str:
    try:
        from roles.build_card import interest_zone

        return interest_zone()
    except Exception:
        return ""


def snapshot() -> dict[str, Any]:
    return {
        "tuning_path": str(TUNING_PATH),
        "knobs": current_knobs(),
        "bounds": {k: list(v) for k, v in BOUNDS.items()},
        "groups": {k: list(v) for k, v in GROUPS.items()},
        "step_limits": dict(STEP_LIMITS),
        "interest_zone": _interest_zone(),
        "judges": {kind: judge_extra(kind) for kind in ("l2", "l2_5", "tool_router")},
    }


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
