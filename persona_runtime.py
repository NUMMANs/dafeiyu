"""
轻量运行时辅助引擎（零额外依赖）：
1. 群黑话词典 (Slang Book)  -> state/slang.json，命中才注入，不浪费 Token
2. 群聊热力雷达 (HeartFlow) -> 多人热议/吃瓜/争论时热力升温，触发 Grok 半主动神补刀；平时潜水
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

from config import BASE_DIR

logger = logging.getLogger("dafeiyu.runtime")

STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SLANG_PATH = STATE_DIR / "slang.json"


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class SlangBook:
    """群黑话词典：记录群内专属外号/缩写/梗，仅在当前消息命中时精准注入。"""

    def __init__(self, path: Path = SLANG_PATH):
        self._lock = threading.Lock()
        self.path = path
        self._data: dict[str, dict[str, str]] = self._load()

    def _load(self) -> dict[str, dict[str, str]]:
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
            except Exception:
                pass
        return {}

    def learn(self, group_id: str, term: str, meaning: str) -> tuple[bool, str]:
        t = (term or "").strip()[:24]
        m = (meaning or "").strip()[:120]
        if len(t) < 1 or len(m) < 2:
            return False, "黑话词或解释太短了"
        gid = group_id or "global"
        with self._lock:
            grp = self._data.setdefault(gid, {})
            grp[t] = m
            _atomic_write(self.path, self._data)
        logger.info("📖 学会新群黑话 [%s]: %s -> %s", gid, t, m)
        return True, f"已记下群黑话「{t}」= {m}"

    def render_matched(self, group_id: str, text: str, max_items: int = 4) -> str:
        low = (text or "").lower()
        if not low:
            return ""
        with self._lock:
            merged: dict[str, str] = {}
            merged.update(self._data.get("global", {}))
            if group_id and group_id in self._data:
                merged.update(self._data[group_id])
        matched = []
        for term, meaning in merged.items():
            if term.lower() in low:
                matched.append(f"- {term}：{meaning}")
                if len(matched) >= max_items:
                    break
        if not matched:
            return ""
        return "【群黑话】\n" + "\n".join(matched)

    def list_terms(self, group_id: str) -> dict[str, str]:
        with self._lock:
            out = dict(self._data.get("global", {}))
            if group_id and group_id in self._data:
                out.update(self._data[group_id])
            return out


class HeartFlowEngine:
    """
    群聊热力引擎（驱动半主动 Grok 锐评插嘴）：
    - 群里短时间多人高频热议、争论真假、吃瓜吐槽 -> 热力飙升，触发一次精准锐评判断
    - 群里零星冷清发言 -> 热力自然衰减，安静潜水不刷屏
    """

    _HOT_CUES = (
        "哈哈", "笑死", "卧槽", "逆天", "牛", "草", "离谱", "离大谱",
        "真的假的", "真的吗", "保真", "扯淡", "一眼假", "翻车", "被打脸",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._energy: dict[str, float] = {}
        self._last_msg_ts: dict[str, float] = {}
        self._recent_senders: dict[str, list[tuple[str, float]]] = {}

    def record_group_pulse(self, group_id: str, sender: str, text: str) -> float:
        """记录一条群消息，返回当前群聊热力 (0~100)。"""
        now = time.time()
        with self._lock:
            prev_ts = self._last_msg_ts.get(group_id, now)
            elapsed_min = max(0.0, (now - prev_ts) / 60.0)
            cur = self._energy.get(group_id, 15.0) * math.exp(-elapsed_min / 12.0)

            senders = [(s, ts) for s, ts in self._recent_senders.get(group_id, []) if (now - ts) <= 120]
            senders.append((sender, now))
            self._recent_senders[group_id] = senders[-12:]
            unique_senders = len({s for s, _ in senders})

            gain = 9.0 + min(10.0, (unique_senders - 1) * 4.5)
            probe = text or ""
            if any(k in probe for k in self._HOT_CUES):
                gain += 6.5

            cur = min(100.0, cur + gain)
            self._energy[group_id] = cur
            self._last_msg_ts[group_id] = now
            return cur

    def drain(self, group_id: str, amount: float = 55.0) -> None:
        with self._lock:
            self._energy[group_id] = max(0.0, self._energy.get(group_id, 0.0) - amount)

    def get_energy(self, group_id: str) -> float:
        with self._lock:
            return round(self._energy.get(group_id, 0.0), 1)


slang_book = SlangBook()
heartflow = HeartFlowEngine()
