"""
拟人化运行时三大引擎（借鉴 MaiBot / DSH 社区设计，零额外依赖）：
1. 群黑话词典 (Slang Book)        -> state/slang.json，命中才注入，不浪费 Token
2. 情绪惯性状态机 (Emotion Inertia) -> 情绪有持续性与自然衰减，不再每句重置
3. 群聊氛围/社交电量 (HeartFlow)   -> 根据群聊节奏动态决定潜水还是冒泡
"""
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
    """群黑话词典：记录群内专属外号/缩写/梗，仅在当前消息命中时精准注入"""

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


class EmotionStateMachine:
    """
    情绪惯性状态机（带指数衰减）：
    - valence: 情绪正负向 [-1.0 暴躁嫌弃 ~ +1.0 开心贴贴]，自然回落到 +0.15
    - arousal: 兴奋度/社交电量 [0.0 困倦摸鱼 ~ 1.0 兴奋吃瓜]，自然回落到 0.45
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # group_id -> {"valence": float, "arousal": float, "updated": float, "recent_reason": str}
        self._states: dict[str, dict[str, Any]] = {}

    def _get_decayed_locked(self, group_id: str, now: float) -> dict[str, Any]:
        st = self._states.get(group_id)
        if not st:
            st = {"valence": 0.15, "arousal": 0.45, "updated": now, "recent_reason": ""}
            self._states[group_id] = st
            return st
        elapsed_min = max(0.0, (now - st["updated"]) / 60.0)
        # 半衰期约 12 分钟，慢慢平复到默认基线
        decay = math.exp(-elapsed_min / 12.0)
        st["valence"] = 0.15 + (st["valence"] - 0.15) * decay
        st["arousal"] = 0.45 + (st["arousal"] - 0.45) * decay
        st["updated"] = now
        if elapsed_min > 20:
            st["recent_reason"] = ""
        return st

    def on_user_message(self, group_id: str, text: str) -> None:
        """根据群友消息内容微调当前情绪惯性"""
        t = (text or "").lower()
        dv, da, reason = 0.0, 0.06, ""
        if any(k in t for k in ("可爱", "厉害", "牛", "喜欢", "贴贴", "夸", "投喂", "好耶", "谢谢")):
            dv, da, reason = +0.28, +0.15, "刚被群友夸了/投喂了，心里美滋滋"
        elif any(k in t for k in ("笨", "傻", "滚", "闭嘴", "菜", "sb", "垃圾", "吵", "烦")):
            dv, da, reason = -0.32, +0.18, "刚才被群友呛了，现在有点小脾气"
        elif any(k in t for k in ("笑死", "哈哈", "草", "逆天", "绷不住", "6", "卧槽", "抽象")):
            dv, da, reason = +0.12, +0.20, "群里正在哈哈大笑看乐子"
        elif any(k in t for k in ("累", "困", "难受", "感冒", "加班", "值班")):
            dv, da, reason = -0.08, -0.10, "群友有点emo，语气可以稍微软一点"

        with self._lock:
            now = time.time()
            st = self._get_decayed_locked(group_id, now)
            st["valence"] = max(-1.0, min(1.0, st["valence"] + dv))
            st["arousal"] = max(0.0, min(1.0, st["arousal"] + da))
            if reason:
                st["recent_reason"] = reason

    def on_bot_reply(self, group_id: str, mood: str) -> None:
        """机器人自身表达的情绪也会带来轻度惯性"""
        delta = {
            "happy": (+0.10, +0.05),
            "love": (+0.18, +0.05),
            "smug": (+0.08, +0.08),
            "gaming": (+0.02, +0.12),
            "shocked": (0.0, +0.15),
            "angry": (-0.18, +0.12),
            "hungry": (-0.10, -0.05),
            "sleepy": (-0.02, -0.15),
        }.get(mood, (0.0, 0.0))
        with self._lock:
            now = time.time()
            st = self._get_decayed_locked(group_id, now)
            st["valence"] = max(-1.0, min(1.0, st["valence"] + delta[0]))
            st["arousal"] = max(0.0, min(1.0, st["arousal"] + delta[1]))

    def describe(self, group_id: str) -> str:
        with self._lock:
            st = self._get_decayed_locked(group_id, time.time())
            v, a, reason = st["valence"], st["arousal"], st["recent_reason"]

        if v >= 0.45:
            tone = "心情超甜超得意，说话可以多带点撒娇和嘚瑟"
        elif v <= -0.35:
            tone = "现在还有点小脾气/记仇，说话带点傲娇阴阳怪气和嫌弃"
        elif a >= 0.72:
            tone = "现在很亢奋爱凑热闹，接梗节奏快"
        elif a <= 0.25:
            tone = "现在有点犯困懒洋洋的，回复更简短随性"
        else:
            tone = "心态平稳自然"

        extra = f"（{reason}）" if reason else ""
        return f"【当前情绪惯性】{tone}{extra}（愉悦 {v:+.2f} / 活跃 {a:.2f}）"


class HeartFlowEngine:
    """
    群聊氛围 / 社交电量引擎（替代死板的“每 6 条消息必触发”）：
    - 群里短时间内多人高频刷屏吃瓜 -> 氛围热力值飙升，容易主动冒泡插嘴
    - 群里零星冷清发言 -> 热力值自然衰减，乖乖潜水省电
    - 插嘴一次后消耗电量，自然休息一段时间
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._energy: dict[str, float] = {}
        self._last_msg_ts: dict[str, float] = {}
        self._recent_senders: dict[str, list[tuple[str, float]]] = {}

    def record_group_pulse(self, group_id: str, sender: str, text: str) -> float:
        """记录一条未触发的群消息，返回当前群聊热力/社交兴奋度 (0~100)"""
        now = time.time()
        with self._lock:
            prev_ts = self._last_msg_ts.get(group_id, now)
            elapsed_min = max(0.0, (now - prev_ts) / 60.0)
            # 随时间自然冷却（每分钟衰减约 6%）
            cur = self._energy.get(group_id, 15.0) * math.exp(-elapsed_min / 12.0)

            # 2 分钟内有多位不同群友都在说话，说明群里很热闹，加速充能
            senders = [(s, ts) for s, ts in self._recent_senders.get(group_id, []) if (now - ts) <= 120]
            senders.append((sender, now))
            self._recent_senders[group_id] = senders[-12:]
            unique_senders = len({s for s, _ in senders})

            gain = 9.0 + min(10.0, (unique_senders - 1) * 4.5)
            # 遇到笑声/吃瓜词额外升温
            if any(k in (text or "") for k in ("哈哈", "笑死", "卧槽", "逆天", "牛", "草", "离谱", "离大谱")):
                gain += 6.0

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
emotion_sm = EmotionStateMachine()
heartflow = HeartFlowEngine()
