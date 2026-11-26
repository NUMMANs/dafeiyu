"""每日子代理调用统计（纯标准库，全程不调模型）。

- 每发生一次判定就记一次，按天落盘到 ``data/统计/counters_YYYY-MM-DD.json``
- 报告文本只读这些计数文件 + 历史表，不经过任何 LLM
- 每次记录都是「读盘 → 累加 → 原子写回」，进程随时重启都不会丢计数

计数口径：
- ``call``：真正把判定请求发给模型的次数（被闸门挡掉的不算）
- ``hit`` ：判定打通、触发开口 / 真的注入 / 决定派活的次数
- 回复率 = hit / call

前两类（l2 / l2_5）是子代理判定；后两类是 2026-11-26 给夜间人格复盘加的
观测点：L1 识海每轮有没有召回旧事、工具路由小模型每句判不判「要干活」。
报告主体仍只统计前两类，新增两类放在末尾附注里，不改动原有日报格式。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from config import DATA_DIR

logger = logging.getLogger("dafeiyu.metrics")

STATS_DIR = DATA_DIR / "统计"
STATS_DIR.mkdir(parents=True, exist_ok=True)

# kind -> 报告里的中文名
KIND_LABELS = {
    "l2": "L2 上下文追认（这句是不是在接我的话）",
    "l2_5": "L2.5 冷场吐槽雷达（冷场后开的话题值不值得吐槽）",
    "l1_recall": "L1 识海召回（这句有没有想起旧事）",
    "tool_router": "工具路由判定（这句要不要派活）",
}

_lock = threading.Lock()


def _counter_path(day: str) -> Path:
    return STATS_DIR / f"counters_{day}.json"


def _blank() -> dict:
    return {kind: {"call": 0, "hit": 0, "groups": {}} for kind in KIND_LABELS}


def _read(path: Path) -> dict:
    if not path.is_file():
        return _blank()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    for kind in KIND_LABELS:
        bucket = data.setdefault(kind, {"call": 0, "hit": 0, "groups": {}})
        bucket.setdefault("call", 0)
        bucket.setdefault("hit", 0)
        bucket.setdefault("groups", {})
    return data


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("统计计数落盘失败 %s: %s", path.name, exc)


def record(kind: str, group_id: str = "", hit: bool = False) -> None:
    """记一次调用。kind 取 ``l2`` / ``l2_5`` / ``l1_recall`` / ``tool_router``。"""
    if kind not in KIND_LABELS:
        return
    day = datetime.now().strftime("%Y-%m-%d")
    path = _counter_path(day)
    group = (group_id or "").strip() or "未知群"
    with _lock:
        data = _read(path)
        bucket = data[kind]
        bucket["call"] += 1
        if hit:
            bucket["hit"] += 1
        gb = bucket["groups"].setdefault(group, {"call": 0, "hit": 0})
        gb["call"] += 1
        if hit:
            gb["hit"] += 1
        _write(path, data)


def load_day(day: str) -> dict:
    return _read(_counter_path(day))


def _pct(hit: int, call: int) -> str:
    if call <= 0:
        return "—"
    return f"{hit / call * 100:.1f}%"


def _disp_width(text: str) -> int:
    """中文/全角字符按 2 列算，保证 txt 里分群列对得齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


def _message_count(day: str) -> int | None:
    """当日群消息条数，用来算「每百条消息发生几次判定」。取不到就返回 None。"""
    try:
        from pet_engine import pet_engine

        start = datetime.strptime(day, "%Y-%m-%d")
        end = start + timedelta(days=1)
        return pet_engine.count_history_between(start.timestamp(), end.timestamp())
    except Exception as exc:  # 统计失败不影响报告主体
        logger.warning("统计当日消息条数失败: %s", exc)
        return None


def build_report(day: str) -> str:
    """生成一天的统计文本（纯字符串拼装，不调模型）。"""
    data = load_day(day)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if day == today:
        span = f"{day} 00:00 ~ {now.strftime('%H:%M')}（当日未结束）"
    else:
        span = f"{day} 00:00 ~ 24:00"

    l2 = data["l2"]
    r25 = data["l2_5"]
    total_call = l2["call"] + r25["call"]
    total_hit = l2["hit"] + r25["hit"]

    lines = [
        "大肥鱼 · 子代理调用日报",
        f"统计日期：{span}",
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M')}（纯代码统计，未调用模型）",
        "",
        "────────────────────────────",
    ]
    for kind in ("l2", "l2_5"):
        b = data[kind]
        lines.extend(
            [
                KIND_LABELS[kind],
                f"  判定调用：{b['call']:>4} 次",
                f"  判定接通：{b['hit']:>4} 次",
                f"  回复率：  {_pct(b['hit'], b['call']):>8}",
                "",
            ]
        )
    lines.extend(
        [
            "────────────────────────────",
            f"合计：判定 {total_call} 次｜接通 {total_hit} 次｜整体回复率 {_pct(total_hit, total_call)}",
            "",
        ]
    )

    # 附注：夜间人格复盘新增的两个观测点（旧 counters 文件没这两项时不显示）
    extra_rows = []
    for kind, label in (("l1_recall", "L1 召回注入"), ("tool_router", "工具路由派活")):
        bucket = data.get(kind)
        if not isinstance(bucket, dict) or not bucket.get("call"):
            continue
        extra_rows.append(
            f"{label}：{bucket['hit']} / {bucket['call']}（{_pct(bucket['hit'], bucket['call'])}）"
        )
    if extra_rows:
        lines.extend(["附：L1 / 工具路由（复盘用）"] + [f"  {r}" for r in extra_rows] + [""])

    # 分群明细
    groups: dict[str, dict[str, dict]] = {}
    for kind in ("l2", "l2_5"):
        for gname, gb in data[kind]["groups"].items():
            groups.setdefault(gname, {})[kind] = gb
    if groups:
        lines.append("分群明细")
        width = max(_disp_width(g) for g in groups)
        for gname in sorted(groups, key=lambda g: -(groups[g].get("l2", {}).get("call", 0) + groups[g].get("l2_5", {}).get("call", 0))):
            g = groups[gname]
            parts = []
            for kind, short in (("l2", "L2"), ("l2_5", "L2.5")):
                gb = g.get(kind) or {"call": 0, "hit": 0}
                parts.append(f"{short} {gb['call']}次/{gb['hit']}接（{_pct(gb['hit'], gb['call'])}）")
            lines.append(f"  {_pad(gname, width)}  " + "  ".join(parts))
        lines.append("")

    msg_count = _message_count(day)
    if msg_count is not None:
        if msg_count > 0:
            per100 = total_call / msg_count * 100
            lines.append(f"参考：当日群消息 {msg_count} 条，每百条消息发生判定 {per100:.1f} 次。")
        else:
            lines.append("参考：当日没有群消息记录。")
    lines.append("")
    lines.append("口径：判定调用=真正发给模型的请求数（被规则闸门挡下的不计）；接通=判定通关并触发开口。")
    return "\n".join(lines)


def write_report(day: str) -> Path:
    """把某天报告写成 txt，返回文件路径。"""
    path = STATS_DIR / f"子代理调用统计_{day}.txt"
    content = build_report(day)
    tmp = path.with_suffix(".txt.tmp")
    tmp.write_text(content + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path
