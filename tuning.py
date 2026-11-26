"""运行期调参层：夜间复盘写这两个地方，机器人不用重启就生效。

- ``data/tuning.json``      数值闸门（L2 时间窗、L2.5 冷场间隔、吐槽阈值）
- ``roles/judges/*.md``     判定器补充规则（给 L2 / L2.5 的提示词加几条临时偏好）

为什么单独做一层：
配置项都写在 ``config.Settings`` 的类属性里，进程启动时求值一次，之后改 ``.env``
必须重启才生效。夜间复盘在 0:30 跑，那时候重启会把正在冒泡/正在接话的状态打断，
而且看门狗会重新走一遍「重启播报」，群里平白多一条消息。
所以把「可以随时微调」的几个值下沉到这里，按文件 mtime 热加载，改完下一句就生效。

数值都有硬边界（``BOUNDS``）。模型可以建议，但不许把阈值调到离谱区间，
比如把 L2.5 阈值压到 0.3，那样群里每句废话它都会接。
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
    # L2.5 吐槽阈值（0~1）。0.7 表示模型给 7 分才接话
    "TOPIC_ROAST_MIN_SCORE": (0.5, 0.9),
    # L2 追认时间窗（秒）：我发言后多久内的群友发言还算「可能接我的话」
    "FOLLOWUP_WINDOW_SEC": (120, 900),
    # L2.5 冷场判定间隔（秒）：群里安静多久才算「重新开话题」
    "TOPIC_GAP_SEC": (300, 1800),
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
    raw = _load_knobs().get(name)
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


def snapshot() -> dict[str, Any]:
    """完整快照，给排查用。"""
    return {
        "tuning_path": str(TUNING_PATH),
        "knobs": current_knobs(),
        "bounds": {k: list(v) for k, v in BOUNDS.items()},
        "judges": {
            kind: judge_extra(kind)
            for kind in ("l2", "l2_5")
        },
    }


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
