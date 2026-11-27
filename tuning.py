"""运行期调参层：夜间复盘写这两个地方，机器人不用重启就生效。

- ``data/tuning.json``      数值闸门（唤醒灵敏度、感官、注入方式、L1 召回、工具路由）
- ``roles/judges/*.md``     判定器补充规则（L2 / L2.5 / 工具路由的提示词加临时偏好）

为什么单独做一层：
配置项都写在 ``config.Settings`` 的类属性里，进程启动时求值一次，之后改 ``.env``
必须重启才生效。夜间复盘在凌晨跑，那时候重启会把正在冒泡/正在接话的状态打断。
所以把「可以随时微调」的值下沉到这里，按文件 mtime 热加载，改完下一句就生效。

数值都有硬边界（``BOUNDS``）。模型可以建议，但不许把阈值调到离谱区间，
比如把 L2.5 阈值压到 0.3，那样群里每句废话它都会接。

2026-11-26 扩展：两个夜间定时任务分了工，也把各自的调参面写进 ``GROUPS``：
- ``sense``（感官与工具助手）：图片认知、主动冒泡、去抖合并等唤醒灵敏度；
- ``mind``（记忆与人格）：L2 日记 / L3 注文 / L1 旧事怎么拼进上下文；
- ``core``（原人格复盘脚本）：L2/L2.5 阈值与 L1 召回、工具路由。
写 ``data/tuning.json`` 时请遵守 ``STEP_LIMITS`` 的单晚步长，别一夜把行为掰弯。
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

# 允许夜间复盘的数值项：名称 -> (下限, 上限)
BOUNDS: dict[str, tuple[float, float]] = {
    # L2.5 兴趣阈值（0~1）。0.7 表示判定器认为「她真感兴趣」给到 7 分才接话。
    # 2026-11-27 由「吐槽判定」改成「兴趣判定」，键名随之改名（旧名仍兼容，见 LEGACY_ALIASES）。
    "TOPIC_INTEREST_MIN_SCORE": (0.5, 0.9),
    # L2 追认时间窗（秒）：我发言后多久内的群友发言还算「可能接我的话」
    "FOLLOWUP_WINDOW_SEC": (120, 900),
    # L2.5 冷场判定间隔（秒）：群里安静多久才算「重新开话题」
    "TOPIC_GAP_SEC": (300, 1800),

    # ---- L1 识海召回（2026-11-26 夜间复盘新增）----
    # 最终打分低于这个值直接丢掉。调高 = 少想旧事，调低 = 更容易触景生情。
    # 0.15 是下限：再低会让每条记忆在每条消息上都冒出来。
    "RECALL_SCORE_THRESHOLD": (0.15, 0.45),
    # 字面闸门：查询词 ≥3 个时至少命中几个二元组才算相关（短查询恒为 1，不受此项影响）
    "MIN_LITERAL_HITS": (1, 3),
    # 一次最多召回几条 / 注入最多多少字
    "RECALL_TOP_N": (3, 8),
    "RECALL_MAX_CHARS": (200, 800),

    # ---- 工具路由小模型（判断「这句要不要派活」）----
    # 短于这么多字的消息不送判定器，省一次调用（也可用来压误派率）
    "TOOL_ROUTER_MIN_CHARS": (4, 20),

    # ---- 感官 / 唤醒层（夜间「感官与工具助手」任务负责）----
    # 群里只发一张图、没带文字时，当场接话的概率。调高 = 更爱接图，调低 = 只默默看懂。
    "IMAGE_REPLY_PROB": (0.0, 0.8),
    # HeartFlow 氛围热力到多少才允许主动冒泡。调低 = 更爱主动开口，调高 = 更沉得住气。
    "PROACTIVE_ENERGY_MIN": (40.0, 90.0),
    # 连发合并：等群里停顿几秒才判定，一波最多攒几句。
    "TRIGGER_DEBOUNCE_SEC": (2.0, 10.0),
    "TRIGGER_BURST_MAX_ITEMS": (2, 10),
    # 智谱看图成功后写进历史的描述字数；太短丢细节，太长会把 L3 历史撑肥。
    "VISION_SUMMARY_CHARS": (80, 400),
    # 冷场找话题：群里安静多少分钟才允许上网找一条分享（调低 = 更容易开口）
    "TOPIC_SHARE_SILENCE_MIN": (30, 360),
    # 同一群两次分享的间隔（分钟）。没有每日次数上限，靠这个控频。
    "TOPIC_SHARE_COOLDOWN_MIN": (20, 480),
    # 分享正文最多多少字（超出按句截断）。群聊里 50 字上下最像真人。
    "TOPIC_SHARE_MAX_CHARS": (30, 120),
    # 分享时带原帖链接的概率（0~1）。调高 = 更像随手转发，调低 = 更像自己随口一说。
    "TOPIC_SHARE_LINK_PROB": (0.0, 1.0),
    # 今日生活：冷场分享里讲「今天自己身上的事」的概率（其余是上网找话题）。
    # 调高 = 更像在过日子，调低 = 更像个资讯号。
    "LIFE_SHARE_PROB": (0.0, 1.0),
    # 生活分享正文最多多少字
    "LIFE_MAX_CHARS": (30, 120),

    # ---- 注入方式（夜间「记忆与人格」任务负责）----
    # L3 注文：后台工具读懂链接/图片后留下的话，每轮最多几条、每条多少字、总开关。
    "L3_NOTE_MAX_ITEMS": (1, 8),
    "L3_NOTE_MAX_CHARS": (40, 300),
    "L3_INJECT_ON": (0, 1),
    # L1 旧事 / L2 日记是否注入本轮上下文（0 关 1 开）
    "RECALL_INJECT_ON": (0, 1),
    "DIARY_INJECT_ON": (0, 1),
    # 「你今天」这块（今天自己的生活进度）是否每轮注入（0 关 1 开）。
    # 关了之后她照样会分享生活，只是别人问「在干嘛」时容易和刚说过的话打架。
    "LIFE_INJECT_ON": (0, 1),
    # L2 日记注入方式：看最近几天、最多几条、每条截多少字、整块最多多少字。
    "DIARY_DAYS": (1, 7),
    "DIARY_MAX_ITEMS": (1, 3),
    "DIARY_ITEM_CHARS": (30, 100),
    "DIARY_MAX_CHARS": (60, 240),
}

# 两个夜间任务各自的调参面，写进 prompt 与护栏报告，避免互相踩。
GROUPS: dict[str, tuple[str, ...]] = {
    "sense": (
        "IMAGE_REPLY_PROB", "PROACTIVE_ENERGY_MIN", "TRIGGER_DEBOUNCE_SEC",
        "TRIGGER_BURST_MAX_ITEMS", "VISION_SUMMARY_CHARS",
        "TOPIC_SHARE_SILENCE_MIN", "TOPIC_SHARE_COOLDOWN_MIN", "TOPIC_SHARE_MAX_CHARS",
        "TOPIC_SHARE_LINK_PROB", "LIFE_SHARE_PROB", "LIFE_MAX_CHARS",
    ),
    "mind": (
        "RECALL_SCORE_THRESHOLD", "MIN_LITERAL_HITS", "RECALL_TOP_N", "RECALL_MAX_CHARS",
        "L3_NOTE_MAX_ITEMS", "L3_NOTE_MAX_CHARS", "L3_INJECT_ON",
        "RECALL_INJECT_ON", "DIARY_INJECT_ON", "LIFE_INJECT_ON", "DIARY_DAYS",
        "DIARY_MAX_ITEMS", "DIARY_ITEM_CHARS", "DIARY_MAX_CHARS",
    ),
    "core": (
        "TOPIC_INTEREST_MIN_SCORE", "FOLLOWUP_WINDOW_SEC", "TOPIC_GAP_SEC",
        "TOOL_ROUTER_MIN_CHARS",
    ),
}

# 改过名的键：新名 -> 旧名。data/tuning.json 里还留着旧名的按旧名继续生效，
# 免得一改名就把夜间调好的阈值丢回默认值（2026-11-27 TOPIC_ROAST → TOPIC_INTEREST）。
LEGACY_ALIASES: dict[str, str] = {
    "TOPIC_INTEREST_MIN_SCORE": "TOPIC_ROAST_MIN_SCORE",
}

# 单晚允许的最大变动幅度。守门在 tools/nightly_guard.py 里；手写 tuning.json 也请照这个来。
STEP_LIMITS: dict[str, float] = {
    "IMAGE_REPLY_PROB": 0.1,
    "PROACTIVE_ENERGY_MIN": 8.0,
    "TRIGGER_DEBOUNCE_SEC": 1.0,
    "TRIGGER_BURST_MAX_ITEMS": 1,
    "VISION_SUMMARY_CHARS": 60,
    "TOPIC_SHARE_SILENCE_MIN": 30,
    "TOPIC_SHARE_COOLDOWN_MIN": 30,
    "TOPIC_SHARE_MAX_CHARS": 15,
    "TOPIC_SHARE_LINK_PROB": 0.2,
    "LIFE_SHARE_PROB": 0.2,
    "LIFE_MAX_CHARS": 15,
    "L3_NOTE_MAX_ITEMS": 1,
    "L3_NOTE_MAX_CHARS": 60,
    "L3_INJECT_ON": 1,
    "RECALL_INJECT_ON": 1,
    "DIARY_INJECT_ON": 1,
    "LIFE_INJECT_ON": 1,
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
        logger.warning("tuning.json 读不出来，按默认值走: %s", exc)
        data = {}
    _knobs_cache = (mtime, data)
    return data


def get(name: str, default: Any) -> Any:
    """取一个运行期覆盖值；没配、类型不对、越界都回落到默认值。"""
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
        logger.warning("tuning.json 里 %s=%s 超出边界 [%s, %s]，忽略", name, raw, low, high)
        return default
    return int(value) if isinstance(default, int) else value


def get_int(name: str, default: int) -> int:
    return int(get(name, default))


def get_float(name: str, default: float) -> float:
    return float(get(name, default))


def current_knobs() -> dict[str, Any]:
    """报告用：当前生效的覆盖值（不含默认值）。"""
    return dict(_load_knobs())


def _judge_path(kind: str) -> Path:
    return JUDGE_DIR / f"{kind}.md"


def judge_extra(kind: str) -> str:
    """读判定器补充规则。文件不存在或只有标题时返回空串，调用方按「无补充」处理。"""
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
    lines: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("<!--"):
            continue
        lines.append(s)
    text = "\n".join(lines).strip()
    with _cache_lock:
        _judge_cache[kind] = (mtime, text)
    return text


def _interest_zone() -> str:
    """人格卡「我的兴趣区」（只读）。放在函数里懒导入，避免模块加载期的循环依赖。"""
    try:
        from roles.build_card import interest_zone

        return interest_zone()
    except Exception:  # noqa: BLE001 - 快照只为排查，读不到不该让进程起不来
        return ""


def snapshot() -> dict[str, Any]:
    """完整快照，给排查用。"""
    return {
        "tuning_path": str(TUNING_PATH),
        "knobs": current_knobs(),
        "bounds": {k: list(v) for k, v in BOUNDS.items()},
        "groups": {k: list(v) for k, v in GROUPS.items()},
        "step_limits": dict(STEP_LIMITS),
        # 人格卡里的「兴趣区」：L2.5 兴趣判定的唯一依据，排查时先看这段
        "interest_zone": _interest_zone(),
        "judges": {
            kind: judge_extra(kind)
            for kind in ("l2", "l2_5", "tool_router", "topic_share", "life")
        },
    }


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
