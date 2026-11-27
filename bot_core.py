from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from agent_tools import (
    is_owner,
    open_url_or_app,
    read_local_file,
    run_shell,
    send_existing_file,
    write_local_file,
)
from brain import brain, split_human_bubbles
from config import BASE_DIR, DATA_DIR, settings
from dsh_bridge import is_dsh_available, list_dsh_jobs, submit_dsh_job
from image_fetcher import download_image_url, search_and_download_image
import link_reader
from memory import estimate_tokens, memory_store
import metrics
from persona_runtime import heartflow, slang_book
from pet_engine import PetStatus, pet_engine
from roles.build_card import get_active_persona, get_evolutions
from safety_guard import (
    assess_danger,
    cancel_pending_action,
    list_pending_actions,
    pop_pending_action,
    stage_pending_action,
)
from scheduler import scheduler
from self_ops import git_pull_and_check, request_restart, self_edit_code
from sticker_manager import sticker_manager
import tuning

logger = logging.getLogger(__name__)

# 派活前硬拦截：禁止后台沙箱任务修改大肥鱼项目自身源码/人格/配置
_SELF_MODIFY_RE = re.compile(
    r"(dafeiyu-wechat-bot|大肥鱼(?:的)?(?:项目|代码|源码|人格|人设|配置|程序)|"
    r"(?:roles[/\\])?大肥鱼\.(?:人格|操作)\.md|"
    r"(?:bot_core|brain|memory|watchdog|self_ops|config)\.py|项目目录)"
)
_SELF_MODIFY_ACT_RE = re.compile(r"(改|修改|编辑|重构|修复|优化|删|替换|更新|调整|重写)")


def looks_like_self_modify(task: str) -> bool:
    body = (task or "").strip()
    if not body:
        return False
    return bool(_SELF_MODIFY_RE.search(body) and _SELF_MODIFY_ACT_RE.search(body))


def humanize_job_announcement(text: str, limit: int = 120) -> str:
    """将后台任务汇报清洗为简洁利落的群聊口语（剥除 Markdown 表格、路径与编号列表）。"""
    body = (text or "").replace("\r", "")
    if not body.strip():
        return ""
    body = re.sub(r"(?m)^\s*\|.*\|\s*$", "", body)
    body = re.sub(r"\n+", "。", body)
    body = re.sub(r"(?:^|。)\s*#{1,6}\s*", "。", body)
    body = re.sub(r"\*{1,3}|`{1,3}", "", body)
    body = re.sub(r"\|", " ", body)
    body = re.sub(
        r"(?:^|。)\s*(?:\d{1,2}[.、)）]|[（(]\d{1,2}[)）]|[①-⑳])\s*", "。", body
    )
    body = re.sub(r"\s\d{1,2}[.、)]\s+", "。", body)
    body = re.sub(r"(?:^|。)\s*[-*•·]\s+", "。", body)
    body = re.sub(r"[A-Za-z]:[\\/][^\s，。；、）」'\"】]*", "文件", body)
    body = re.sub(
        r"(?:[\w\u4e00-\u9fa5.()（）-]+/)+[\w\u4e00-\u9fa5.()（）-]+\."
        r"(?:md|txt|py|json|csv|xlsx?|docx?|pdf|pptx?|png|jpe?g|webp|gif|zip|html?|ya?ml|log|sql|svg)\b",
        "文件",
        body,
    )
    body = re.sub(r"\s+", " ", body)
    body = re.sub(r"[：:]\s*(?=[。，、；])", "", body)
    body = re.sub(r"[。；;]{2,}", "。", body)
    body = re.sub(r"([。！？!?；;])[，、]+", r"\1", body)
    body = re.sub(r"[，、]{2,}", "，", body)
    body = re.sub(r" +文件", "文件", body)
    body = re.sub(r"^[。，、；：\s]+", "", body).strip()
    if not body:
        return ""
    if body[-1] not in "。！？!?…":
        body += "。"
    if len(body) <= limit:
        return body
    out = ""
    for part in re.split(r"(?<=[。！？!?；;])", body):
        part = part.strip()
        if not part:
            continue
        if len(out) + len(part) > limit:
            break
        out += part
        if len(out) >= max(40, limit // 2):
            break
    out = out.strip().rstrip("，、；： ")
    return out or (body[: limit - 1].rstrip("，、；： ") + "…")


@dataclass
class BotResponse:
    should_reply: bool
    text: str = ""
    bubbles: list[str] = field(default_factory=list)
    sticker_path: Path | None = None
    sticker_url: str | None = None
    file_path: Path | None = None
    mood: str = "smug"
    status: PetStatus | None = None

    def get_bubbles(self) -> list[str]:
        if self.bubbles:
            return self.bubbles
        return [self.text] if self.text else []


DISPATCH_ACKS = (
    "收到，后台跑一下，出结果发群里",
    "行，这活我放后台弄了，稍等",
    "等着，我去跑一遍给你结果",
    "接了，弄完把文件甩出来",
)

DSH_DONE_NOTE_TTL_SEC = 1800


def _execute_confirmed_action(group_id: str, sender: str, item: dict) -> tuple[str, Path | None]:
    action_type = str(item.get("action_type") or "")
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    code = str(item.get("code") or "")

    if action_type == "delegate_to_dsh":
        ok, payload = submit_dsh_job(
            group_id=group_id,
            sender=sender,
            task=str(params.get("task") or ""),
            session_id=str(params.get("session_id") or ""),
            workdir=str(params.get("workdir") or ""),
        )
        if ok and isinstance(payload, dict):
            sid_label = payload.get("session_id") or "新建会话（自动分配）"
            return (
                f"✅ 二次确认通过（`{code}`），已派发后台执行！\n"
                f"• 任务号：`{payload['job_id']}`\n"
                f"• 会话ID：`{sid_label}`",
                None,
            )
        return f"❌ 确认后派单失败（`{code}`）：{payload}", None

    if action_type == "run_shell":
        return f"✅ 已确认执行命令（`{code}`）：\n{run_shell(str(params.get('command') or ''))}", None
    if action_type == "write_local_file":
        return f"✅ 已确认写入文件（`{code}`）：\n{write_local_file(str(params.get('path') or ''), str(params.get('content') or ''))}", None
    if action_type == "read_local_file":
        return f"✅ 已确认读取文件（`{code}`）：\n{read_local_file(str(params.get('path') or ''))}", None
    if action_type == "send_existing_file":
        fpath, out = send_existing_file(str(params.get("path") or ""))
        valid = fpath if (fpath and fpath.is_file() and fpath.stat().st_size > 0) else None
        return f"✅ 已确认发送文件（`{code}`）：{out}", valid
    if action_type == "open_url_or_app":
        return f"✅ 已确认打开目标（`{code}`）：{open_url_or_app(str(params.get('target') or ''))}", None
    if action_type == "self_edit_code":
        out = self_edit_code(
            str(params.get("rel_path") or ""),
            str(params.get("target") or ""),
            str(params.get("replacement") or ""),
        )
        return f"✅ 已确认修改源码（`{code}`）：\n{out}", None
    return f"⚠️ 未知的待确认操作类型：{action_type}", None


class DaFeiYuBotCore:
    """
    Grok 模式核心调度器：
    - 主力唤醒：`@大肥鱼` / 句首唤名 / 引用回复 / L2 5分钟内追问
    - 半主动插嘴：L2.5 兴趣区雷达 + HeartFlow 多人热议补刀（克制精准，不救冷场、不编假日常）
    - 后台工兵：群链接静默预读（写 L3 注文）+ 重任务异步派发 DSH / CodeBuddy 沙箱
    - 夜间轻维护：仅保留 L1/L2 轻量群友记忆沉淀、L3 超限归档压缩与 0 点调用日报
    """

    def __init__(self) -> None:
        self._recent_group_images: dict[tuple[str, str], tuple[Path, float]] = {}
        self._last_proactive_ts: dict[str, float] = {}
        self._last_bot_reply_ts: dict[str, float] = {}
        self._last_group_activity_ts: dict[str, float] = {}

        self._pending_trigger: dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        self._last_nightly_date: str = ""
        self._nightly_state_path: Path = BASE_DIR / "state" / "nightly_state.json"
        self._last_metrics_date: str = ""
        self._last_l3_check_day: dict[str, str] = {}

        self._dsh_running: dict[str, dict] = {}
        self._link_limiter = link_reader.LinkLimiter(settings.TOOL_LINK_SEEN_TTL_SEC)
        self._link_decision: dict[str, tuple[str, dict, float]] = {}
        self._dsh_done: dict[str, dict] = {}

    def _maybe_pick_sticker(self, mood: str, caption: str | None = None) -> Path | None:
        if not settings.ENABLE_GROK_STICKERS or settings.STICKER_REPLY_PROB <= 0:
            return None
        return sticker_manager.pick_sticker(mood, caption=caption)

    def is_quiet_hours(self) -> bool:
        hour = datetime.now().hour
        start, end = settings.QUIET_START_HOUR, settings.QUIET_END_HOUR
        return (hour >= start or hour < end) if start > end else (start <= hour < end)

    def _name_prefixes(self) -> list[str]:
        prefixes = [settings.BOT_NAME] + [k.lstrip("@") for k in settings.TRIGGER_KEYWORDS]
        return [p for p in prefixes if p]

    def _is_triggered(self, group_id: str, text: str, force_trigger: bool = False) -> bool:
        if force_trigger or not settings.GROUP_AT_ONLY:
            return True
        probe = text.strip()
        if not probe:
            return False
        low = probe.lower()
        if any(kw.lower() in low for kw in settings.TRIGGER_KEYWORDS):
            return True
        return any(probe.startswith(p) for p in self._name_prefixes())

    def _strip_trigger(self, text: str) -> str:
        cleaned = text
        for kw in settings.TRIGGER_KEYWORDS:
            cleaned = cleaned.replace(kw, "")
        cleaned = cleaned.lstrip()
        for prefix in sorted(self._name_prefixes(), key=len, reverse=True):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :]
                break
        return cleaned.strip(" \u2005\u3000\t\r\n,，:：!！@")

    # ------------------------------------------------------------------
    # L2 追认 & L2.5 兴趣雷达 & 连发去抖
    # ------------------------------------------------------------------

    def _followup_gate(
        self,
        group_id: str,
        text: str,
        quoted_sender: str = "",
        max_turns: int = 5,
    ) -> tuple[bool, bool]:
        body = (text or "").strip()
        if len(body) < 2:
            return False, False

        quoted = (quoted_sender or "").strip()
        if quoted and settings.BOT_NAME and settings.BOT_NAME in quoted:
            return True, True

        recent = pet_engine.get_recent_history(group_id, limit=max_turns + 8)
        if recent and (recent[-1].get("content") or "").strip() == body:
            recent = recent[:-1]

        last_asst_idx = -1
        for idx in range(len(recent) - 1, -1, -1):
            if recent[idx].get("role") == "assistant":
                last_asst_idx = idx
                break
        if last_asst_idx < 0:
            return False, False

        window = recent[last_asst_idx:]
        turns_after = sum(1 for m in window[1:] if m.get("role") != "assistant")
        if turns_after >= max_turns:
            return False, False

        asst_ts = float(recent[last_asst_idx].get("created_ts") or 0.0)
        followup_window = tuning.get_int("FOLLOWUP_WINDOW_SEC", settings.FOLLOWUP_WINDOW_SEC)
        if asst_ts and (time.time() - asst_ts) > followup_window:
            return False, False

        return True, False

    def _followup_judge(self, group_id: str, sender: str, text: str, max_turns: int = 5) -> bool:
        body = (text or "").strip()
        if not body:
            return False
        recent = pet_engine.get_recent_history(group_id, limit=max_turns + 12)
        if recent and (recent[-1].get("content") or "").strip() == body:
            recent = recent[:-1]

        last_asst_idx = -1
        for idx in range(len(recent) - 1, -1, -1):
            if recent[idx].get("role") == "assistant":
                last_asst_idx = idx
                break
        if last_asst_idx < 0:
            return False

        window = recent[last_asst_idx:]
        verdict = brain.is_talking_to_or_about_me(
            sender=sender,
            text=body,
            recent_history=window,
            at_others="@" in body,
        )
        metrics.record("l2", group_id, verdict)
        return verdict

    @staticmethod
    def _topic_probe_ok(text: str) -> bool:
        body = (text or "").strip()
        if len(body) < 6 or body.startswith("/") or body.startswith("<"):
            return False
        return sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff") >= 2

    @staticmethod
    def _burst_item_ok(text: str) -> bool:
        body = (text or "").strip()
        if not body or body.startswith("/") or body.startswith("<"):
            return False
        return sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff") >= 1

    def _interest_gate(
        self,
        group_id: str,
        text: str,
        now: float,
        prev_activity_ts: float,
    ) -> tuple[bool, str]:
        if not settings.ENABLE_TOPIC_INTEREST or self.is_quiet_hours():
            return False, ""
        if not self._topic_probe_ok(text) or prev_activity_ts <= 0:
            return False, ""
        gap = now - prev_activity_ts
        if gap < tuning.get_int("TOPIC_GAP_SEC", settings.TOPIC_GAP_SEC):
            return False, ""
        gap_desc = f"约 {int(gap // 3600)} 小时" if gap >= 3600 else f"约 {int(gap // 60)} 分钟"
        return True, gap_desc

    def _interest_judge(
        self,
        group_id: str,
        sender: str,
        text: str,
        gap_desc: str,
        multi_speaker: bool = False,
    ) -> str:
        body = (text or "").strip()
        if not body:
            return ""
        recent = pet_engine.get_recent_history(group_id, limit=8)
        merged_lines = {
            re.sub(r"^\[[^\]]{1,40}\]:\s*", "", ln.strip())
            for ln in body.splitlines()
            if ln.strip()
        }
        if merged_lines:
            recent = [m for m in recent if (m.get("content") or "").strip() not in merged_lines]

        angle = brain.judge_topic_interest(
            sender=sender,
            text=body,
            recent_history=recent,
            gap_desc=gap_desc,
            at_others="@" in body,
            multi_speaker=multi_speaker,
        )
        metrics.record("l2_5", group_id, bool(angle))
        return angle

    def _append_burst_locked(self, item: dict, sender: str, text: str) -> None:
        if not self._burst_item_ok(text):
            return
        burst: list[tuple[str, str]] = item.setdefault("burst", [])
        clean = text.strip()
        if burst and burst[-1] == (sender, clean):
            return
        max_items = tuning.get_int("TRIGGER_BURST_MAX_ITEMS", settings.TRIGGER_BURST_MAX_ITEMS)
        if len(burst) < max_items:
            burst.append((sender, clean))
            if len(burst) >= max_items:
                item["full"] = True

    def _defer_trigger(
        self,
        group_id: str,
        kind: str,
        sender: str,
        text: str,
        quoted_sender: str,
        gap_desc: str,
        now: float,
        gap_sec: float = 0.0,
    ) -> None:
        with self._pending_lock:
            existing = self._pending_trigger.get(group_id)
            if existing is None:
                item = {
                    "kind": kind,
                    "sender": sender,
                    "text": text,
                    "quoted_sender": quoted_sender,
                    "gap_desc": gap_desc,
                    "gap_sec": gap_sec,
                    "first_ts": now,
                    "last_ts": now,
                    "burst": [],
                    "full": False,
                }
                self._append_burst_locked(item, sender, text)
                self._pending_trigger[group_id] = item
                return

            existing["last_ts"] = now
            if existing["kind"] == "interest" and kind == "l2":
                existing.update(
                    {"kind": "l2", "sender": sender, "text": text, "quoted_sender": quoted_sender}
                )
            elif existing["kind"] == "l2" and kind == "l2":
                existing.update({"sender": sender, "text": text, "quoted_sender": quoted_sender})
            else:
                self._append_burst_locked(existing, sender, text)

    def _touch_pending(self, group_id: str, now: float, text: str = "", sender: str = "") -> None:
        with self._pending_lock:
            item = self._pending_trigger.get(group_id)
            if item is not None:
                item["last_ts"] = now
                if text and sender:
                    self._append_burst_locked(item, sender, text)

    def _cancel_pending(self, group_id: str) -> None:
        with self._pending_lock:
            self._pending_trigger.pop(group_id, None)

    @staticmethod
    def _compose_burst(item: dict) -> tuple[str, str, bool, list[str]]:
        burst: list[tuple[str, str]] = item.get("burst") or []
        if not burst:
            raw = (item.get("text") or "").strip()
            return item.get("sender") or "", raw, False, ([raw] if raw else [])
        raw_texts = [t for _, t in burst if t]
        speakers = {s for s, _ in burst}
        if len(speakers) == 1:
            return burst[0][0], "\n".join(t for _, t in burst), False, raw_texts
        merged = "\n".join(f"[{s}]: {t}" for s, t in burst)
        return burst[-1][0], merged, True, raw_texts

    def _judge_pending(self, group_id: str, item: dict) -> BotResponse:
        now = time.time()
        kind = item["kind"]
        sender = item["sender"]
        text = item["text"]
        quoted_sender = item["quoted_sender"]
        gap_desc = item["gap_desc"]

        if kind == "l2":
            if self._followup_judge(group_id, sender, text):
                return self._compose_reply(
                    group_id=group_id,
                    sender=sender,
                    text=text,
                    quoted_sender=quoted_sender,
                    now=now,
                )
            gap_sec = float(item.get("gap_sec") or 0.0)
            if gap_sec >= tuning.get_int("TOPIC_GAP_SEC", settings.TOPIC_GAP_SEC) and self._topic_probe_ok(text):
                kind = "interest"
                gap_desc = (
                    f"约 {int(gap_sec // 3600)} 小时" if gap_sec >= 3600 else f"约 {int(gap_sec // 60)} 分钟"
                )
            else:
                return self._maybe_proactive_chime_in(group_id, now, record_pulse=False)

        if kind == "interest":
            b_sender, probe_text, multi, raw_texts = self._compose_burst(item)
            angle = self._interest_judge(
                group_id, b_sender, probe_text, gap_desc, multi_speaker=multi
            )
            if angle:
                return self._compose_reply(
                    group_id=group_id,
                    sender=b_sender,
                    text=probe_text,
                    quoted_sender=quoted_sender,
                    now=now,
                    interest_angle=angle,
                    allow_image_lookup=False,
                    history_strip=raw_texts,
                )

        # 若 L2/L2.5 均未接话，但这一波多人连发已把群聊热力顶满，顺滑走一次半主动锐评检查
        return self._maybe_proactive_chime_in(group_id, now, record_pulse=False)

    def pop_due_triggers(self, now: float | None = None) -> list[tuple[str, BotResponse]]:
        now = now or time.time()
        due: list[tuple[str, dict]] = []
        debounce_sec = tuning.get_float("TRIGGER_DEBOUNCE_SEC", settings.TRIGGER_DEBOUNCE_SEC)
        with self._pending_lock:
            for gid, item in list(self._pending_trigger.items()):
                quiet = now - item["last_ts"] >= debounce_sec
                waited = now - item["first_ts"] >= settings.TRIGGER_DEBOUNCE_MAX_WAIT_SEC
                if quiet or waited or item.get("full"):
                    due.append((gid, self._pending_trigger.pop(gid)))

        results: list[tuple[str, BotResponse]] = []
        for gid, item in due:
            try:
                resp = self._judge_pending(gid, item)
                if resp.should_reply:
                    results.append((gid, resp))
            except Exception as exc:
                logger.warning("[%s] 去抖后判定异常: %s", gid, exc)
        return results

    # ------------------------------------------------------------------
    # 快捷指令
    # ------------------------------------------------------------------

    def _handle_command(self, group_id: str, sender: str, text: str) -> BotResponse | None:
        cmd = text.strip()

        if cmd in ("/状态", "查状态", "查鱼", "/鱼状态"):
            st = pet_engine.get_status()
            ms = memory_store.stats(group_id=group_id)
            ok_dsh, dsh_info = is_dsh_available()
            energy = heartflow.get_energy(group_id)
            reply_text = (
                f"⚡ 【{settings.BOT_NAME} · Grok 核查终端状态】\n"
                f"• 主脑模型：`{settings.DEEPSEEK_CHAT_MODEL}`｜联网核查：{'开启' if settings.ENABLE_WEB_SEARCH else '关闭'}\n"
                f"• 沙箱工兵：`{settings.TOOL_BACKEND}`（{'就绪' if ok_dsh else '未就绪'}）\n"
                f"• 记忆容量：L1 识海 {ms['total_nodes']} 条｜本群观察笔记 {ms['group_diaries']} 篇\n"
                f"• 累计记录：{st.total_messages} 条消息｜{st.total_images} 张图片｜当前群热力 {energy:.0f}/100"
            )
            return BotResponse(should_reply=True, text=reply_text, mood="smug", status=st)

        if cmd in ("/帮助", "帮助", "/help"):
            st = pet_engine.get_status()
            reply_text = (
                f"⚡ 【{settings.BOT_NAME}（Grok 模式）使用指南】\n"
                f"• `@{settings.BOT_NAME} 这是真的吗 / 怎么看 / 总结一下`：联网事实核查与直球锐评\n"
                f"• 发图（或引用历史图）`@{settings.BOT_NAME}`：高清扒细节、鉴 P 图、评战绩\n"
                f"• 群里甩链接会自动静默预读，后续聊起随时接梗核查\n"
                f"• `/搜图 关键词`（或直链）：搜图并直接发群\n"
                f"• `/记住 内容` / `/记忆` / `/学黑话 词 意思` / `/黑话`：管理群友黑历史与群梗\n"
                f"• `/日记` / `/写日记` / `/人设` / `/统计`：查看群观察笔记与调用统计\n"
                f"• `/定时` / `/取消定时 ID`：管理定时提醒或定时核查任务\n"
                f"• `/dsh 任务` / `/dsh 续 要求` / `/dsh状态`：派发沙箱重任务（仅主人）\n"
                f"• `/确认 码` / `/取消 码` / `/待确认`：高危操作二次确认\n"
                f"• `/更新` / `/重启`：拉取最新代码并平滑重启（仅主人）"
            )
            return BotResponse(should_reply=True, text=reply_text, mood="smug", status=st)

        if cmd.startswith("/记住"):
            fact = cmd[3:].strip()
            st = pet_engine.get_status()
            if not fact:
                return BotResponse(
                    should_reply=True,
                    text="格式：`/记住 要记住的事`（比如：`/记住 老王立flag今年上王者否则女装`）",
                    mood="smug",
                    status=st,
                )
            ok, msg = memory_store.remember(
                text=fact,
                level="important",
                tags=[sender, "群友记录"],
                source="owner",
                group_id=group_id,
                raw=cmd,
            )
            return BotResponse(
                should_reply=True,
                text=f"记下了：{msg}" if ok else f"没记进去：{msg}",
                mood="smug" if ok else "angry",
                status=st,
            )

        if cmd in ("/记忆", "查记忆"):
            st = pet_engine.get_status()
            ms = memory_store.stats(group_id=group_id)
            lv = ms["levels"]
            reply_text = (
                f"🧠 【群友记忆库状态】\n"
                f"• L1 识海节点：{ms['total_nodes']} 条（core {lv['core']} / important {lv['important']} / normal {lv['normal']}）\n"
                f"• 标签关联边：{ms['total_edges']} 条｜今日自动沉淀：{ms['today_self_writes']}/{ms['daily_limit']}\n"
                f"• L2 群观察笔记：{ms['group_diaries']} 篇"
            )
            return BotResponse(should_reply=True, text=reply_text, mood="smug", status=st)

        if cmd in ("/日记", "看日记"):
            st = pet_engine.get_status()
            diaries = memory_store.read_recent_diaries(limit=3, group_id=group_id)
            if not diaries:
                return BotResponse(should_reply=True, text="📔 暂无群观察笔记，可发 `/写日记` 立即生成一篇。", mood="smug", status=st)
            lines = ["📔 【最近群观察笔记】"] + [
                f"• {d.get('时间', '')} [{d.get('心情', '')}] {d.get('内容', '')}" for d in diaries
            ]
            return BotResponse(should_reply=True, text="\n".join(lines), mood="smug", status=st)

        if cmd in ("/写日记", "写日记"):
            st = pet_engine.get_status()
            today = datetime.now().strftime("%Y-%m-%d")
            recent = self._collect_day_material(group_id, today) or pet_engine.get_recent_history(group_id, limit=40)
            if not recent:
                return BotResponse(should_reply=True, text="📔 今天群里还没啥聊天素材可写。", mood="sleepy", status=st)
            ok = brain.write_daily_diary(recent, st, group_id=group_id, day=today)
            if ok:
                latest = memory_store.read_recent_diaries(limit=1, group_id=group_id)
                d = latest[0] if latest else {}
                return BotResponse(should_reply=True, text=f"📔 已归档 [{d.get('心情', '')}] {d.get('内容', '')}", mood="smug", status=st)
            return BotResponse(should_reply=True, text="📔 今天已经记过差不多的了，未重复写入。", mood="sleepy", status=st)

        if cmd in ("/统计", "/日报", "看统计") or cmd.startswith("/统计 "):
            st = pet_engine.get_status()
            arg = cmd.split(maxsplit=1)[1].strip() if " " in cmd else ""
            if arg in ("昨天", "昨日"):
                day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            elif arg:
                try:
                    day = datetime.strptime(arg, "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    return BotResponse(should_reply=True, text="格式：`/统计`、`/统计 昨天`、`/统计 2026-11-27`", mood="smug", status=st)
            else:
                day = datetime.now().strftime("%Y-%m-%d")
            return BotResponse(should_reply=True, text=metrics.build_report(day), mood="smug", status=st)

        if cmd.startswith("/学黑话"):
            st = pet_engine.get_status()
            parts = cmd[4:].strip().split(maxsplit=1)
            if len(parts) < 2:
                return BotResponse(should_reply=True, text="格式：`/学黑话 词 意思`", mood="smug", status=st)
            ok, msg = slang_book.learn(group_id, parts[0], parts[1])
            return BotResponse(should_reply=True, text=msg, mood="smug" if ok else "angry", status=st)

        if cmd in ("/黑话", "查黑话"):
            st = pet_engine.get_status()
            terms = slang_book.list_terms(group_id)
            if not terms:
                return BotResponse(should_reply=True, text="📖 暂无本群黑话记录，发 `/学黑话 词 意思` 添加。", mood="smug", status=st)
            lines = ["📖 【本群黑话词典】"] + [f"• {k}：{v}" for k, v in list(terms.items())[:15]]
            return BotResponse(should_reply=True, text="\n".join(lines), mood="smug", status=st)

        if cmd in ("/人设", "查人设", "/进化"):
            st = pet_engine.get_status()
            active = get_active_persona()
            evos = get_evolutions(active)
            lines = [f"⚡ 【当前模式：{active}（Grok 锐评核查官）｜进化笔记】"] + [f"• {e}" for e in evos]
            return BotResponse(should_reply=True, text="\n".join(lines), mood="smug", status=st)

        if cmd in ("/定时", "查定时", "/定时任务"):
            st = pet_engine.get_status()
            tasks = scheduler.list_tasks(group_id=group_id)
            if not tasks:
                return BotResponse(
                    should_reply=True,
                    text="⏰ 本群暂无定时任务。直接 @ 我说「10分钟后提醒我xxx」或「每天9点查一下xxx」即可。",
                    mood="smug",
                    status=st,
                )
            lines = ["⏰ 【本群定时任务】"] + [
                f"• #{t['id']}｜{t['desc']}｜[{'AI执行' if t.get('mode') == 'ai_task' else '提醒'}] {t['content']}"
                for t in tasks
            ]
            return BotResponse(should_reply=True, text="\n".join(lines), mood="smug", status=st)

        if cmd.startswith("/取消定时"):
            st = pet_engine.get_status()
            tid = cmd[5:].strip()
            if not tid:
                return BotResponse(should_reply=True, text="格式：`/取消定时 任务ID`（先发 `/定时` 查看）", mood="smug", status=st)
            _, msg = scheduler.delete_task(tid)
            return BotResponse(should_reply=True, text=msg, mood="smug", status=st)

        if cmd in ("/更新", "/拉代码", "/gitpull"):
            st = pet_engine.get_status()
            if not is_owner(sender):
                return BotResponse(should_reply=True, text="🛑 仅限主人执行代码更新。", mood="angry", status=st)
            ok, detail = git_pull_and_check()
            if ok:
                restart_msg = request_restart(reason="主人指令更新代码", notify_group=group_id)
                return BotResponse(should_reply=True, text=f"🔄 {detail}\n♻️ {restart_msg}", mood="happy", status=st)
            return BotResponse(should_reply=True, text=f"📦 {detail}", mood="smug", status=st)

        if cmd in ("/重启", "/restart"):
            st = pet_engine.get_status()
            if not is_owner(sender):
                return BotResponse(should_reply=True, text="🛑 仅限主人执行重启。", mood="angry", status=st)
            restart_msg = request_restart(reason="主人发送 /重启 指令", notify_group=group_id)
            return BotResponse(should_reply=True, text=f"♻️ {restart_msg}", mood="happy", status=st)

        if cmd in ("/dsh状态", "/后台任务", "/dsh任务"):
            st = pet_engine.get_status()
            if not is_owner(sender):
                return BotResponse(should_reply=True, text="🛑 仅限主人查看后台工兵状态。", mood="angry", status=st)
            ok_dsh, dsh_info = is_dsh_available()
            jobs = list_dsh_jobs(group_id=group_id, limit=5)
            lines = [f"🤖 【后台沙箱工兵（{settings.TOOL_BACKEND}）】{'已就绪' if ok_dsh else '未就绪'}（{dsh_info}）"]
            if not jobs:
                lines.append("• 当前群暂无后台任务记录")
            else:
                for j in jobs:
                    preview = j["task"][:36] + ("..." if len(j["task"]) > 36 else "")
                    lines.append(f"• #{j['job_id']} [{j['status']}｜{j['duration_sec']}s] {preview}")
            return BotResponse(should_reply=True, text="\n".join(lines), mood="smug", status=st)

        if cmd == "/dsh" or cmd.startswith("/dsh ") or cmd.startswith("/dsh\n"):
            st = pet_engine.get_status()
            if not is_owner(sender):
                return BotResponse(should_reply=True, text="🛑 仅限主人派发后台沙箱任务。", mood="angry", status=st)
            body = cmd[4:].strip()
            if not body:
                return BotResponse(should_reply=True, text="格式：`/dsh 任务说明` 或 `/dsh 续 补充要求`", mood="smug", status=st)
            sid = ""
            task_text = body
            if body == "续" or body.startswith("续 ") or body.startswith("续\n"):
                sid = "last"
                task_text = body[1:].strip()
                if not task_text:
                    return BotResponse(should_reply=True, text="请在 `/dsh 续 ` 后写明要追加的要求。", mood="smug", status=st)
            dsh_params = {"task": task_text, "session_id": sid}
            is_danger, reasons = assess_danger("delegate_to_dsh", dsh_params, user_text=task_text)
            if is_danger:
                _, confirm_card = stage_pending_action(
                    group_id=group_id,
                    sender=sender,
                    action_type="delegate_to_dsh",
                    params=dsh_params,
                    reasons=reasons,
                    preview=task_text,
                )
                return BotResponse(should_reply=True, text=confirm_card, mood="shocked", status=st)
            ok, payload = submit_dsh_job(group_id=group_id, sender=sender, task=task_text, session_id=sid)
            if ok and isinstance(payload, dict):
                sid_label = payload.get("session_id") or "新建会话（自动分配）"
                reply_text = (
                    f"🚀 已派发独立沙箱后台执行！\n"
                    f"• 任务号：`{payload['job_id']}`\n"
                    f"• 会话ID：`{sid_label}`\n"
                    f"完成后自动回传群内，发 `/dsh状态` 可查进度。"
                )
                return BotResponse(should_reply=True, text=reply_text, mood="happy", status=st)
            return BotResponse(should_reply=True, text=f"❌ 派单失败：{payload}", mood="angry", status=st)

        search_img_match = re.match(r"^/(?:搜图|找图|发图)(?:\s*(.*))?$", cmd)
        if search_img_match:
            st = pet_engine.get_status()
            target_q = (search_img_match.group(1) or "").strip()
            if not target_q:
                return BotResponse(should_reply=True, text="格式：`/搜图 关键词` 或 `/搜图 图片直链`", mood="smug", status=st)
            img_path, desc = (
                download_image_url(target_q, label="direct")
                if target_q.startswith(("http://", "https://"))
                else search_and_download_image(target_q)
            )
            if img_path is not None and img_path.is_file():
                return BotResponse(
                    should_reply=True,
                    text=f"🖼️ {desc}",
                    sticker_path=img_path,
                    sticker_url=sticker_manager.to_web_url(img_path),
                    mood="happy",
                    status=st,
                )
            return BotResponse(should_reply=True, text=f"😵 {desc}", mood="angry", status=st)

        if cmd in ("/待确认", "待确认"):
            st = pet_engine.get_status()
            items = list_pending_actions(group_id)
            if not items:
                return BotResponse(should_reply=True, text="🛡️ 当前没有待二次确认的操作。", mood="smug", status=st)
            now_ts = time.time()
            lines = ["⚠️ 【本群待二次确认操作】"]
            for it in items:
                ttl_left = max(0, int(float(it.get("expires_at") or now_ts) - now_ts))
                lines.append(
                    f"• 码 `{it.get('code')}`｜发起人：{it.get('sender')}｜剩 {ttl_left}s｜{str(it.get('preview') or '')[:45]}"
                )
            lines.append("回复 `/确认 码` 执行，`/取消 码` 放弃。")
            return BotResponse(should_reply=True, text="\n".join(lines), mood="smug", status=st)

        confirm_match = re.match(
            r"^(?:(?:/确认执行|确认执行|/确认)\s*([A-Za-z0-9]{0,8})|确认\s+([A-Za-z0-9]{4,5}))$",
            cmd,
        )
        if confirm_match:
            st = pet_engine.get_status()
            code_arg = (confirm_match.group(1) or confirm_match.group(2) or "").strip()
            item, pop_msg = pop_pending_action(group_id, code_arg)
            if item is None:
                return BotResponse(should_reply=True, text=pop_msg, mood="smug", status=st)
            result_text, attach_path = _execute_confirmed_action(group_id, sender, item)
            return BotResponse(should_reply=True, text=result_text, sticker_path=attach_path, mood="happy", status=st)

        cancel_match = re.match(
            r"^(?:(?:/取消|/拒绝)(?:\s+([A-Za-z0-9]{0,8}))?|取消\s+([A-Za-z0-9]{4,5}))$",
            cmd,
        )
        if cancel_match:
            st = pet_engine.get_status()
            code_arg = (cancel_match.group(1) or cancel_match.group(2) or "").strip()
            ok_cancel, cancel_msg = cancel_pending_action(
                group_id, code_arg, requester=sender, is_requester_owner=True
            )
            return BotResponse(should_reply=True, text=cancel_msg, mood="smug", status=st)

        return None

    # ------------------------------------------------------------------
    # 图片关联与主消息入口
    # ------------------------------------------------------------------

    def _caption_image_async(self, group_id: str, image_path: Path) -> None:
        if not settings.ZHIPU_API_KEY or not image_path.is_file():
            return

        def _worker() -> None:
            try:
                summary = brain.summarize_image(image_path)
                if summary:
                    pet_engine.update_history_content_by_image(group_id, str(image_path), summary)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _pick_referenced_image(
        self,
        group_id: str,
        sender: str,
        text: str,
        now: float,
        quoted_sender: str = "",
    ) -> tuple[Path | None, str]:
        low = text.lower()
        img_cues = (
            "图", "这张", "那张", "上面", "刚才", "战绩", "截图", "照片",
            "表情包", "p的", "真假", "真的吗", "发的那", "看下", "看看", "锐评", "啥意思",
        )
        ordinal_map = {"上一张": 1, "最新一张": 1, "第一张": 1, "第二张": 2, "上两张": 2, "第三张": 3, "第四张": 4}

        recent_imgs = pet_engine.get_recent_images(group_id, limit=8)
        if not recent_imgs:
            cached = self._recent_group_images.get((group_id, sender))
            if cached and (now - cached[1]) <= 120 and cached[0].is_file():
                return cached[0], sender
            return None, ""

        if quoted_sender:
            for item in recent_imgs:
                if item["sender"] == quoted_sender and (now - item["created_ts"]) <= 1800:
                    return Path(item["image_path"]), item["sender"]

        for kw, idx in ordinal_map.items():
            if kw in low and idx <= len(recent_imgs):
                picked = recent_imgs[idx - 1]
                return Path(picked["image_path"]), picked["sender"]

        has_img_cue = any(c in low for c in img_cues)
        if has_img_cue:
            for item in recent_imgs:
                sp = item["sender"]
                if sp and sp != sender and sp.lower() in low:
                    return Path(item["image_path"]), sp

        for item in recent_imgs:
            if item["sender"] == sender and (now - item["created_ts"]) <= 120:
                return Path(item["image_path"]), sender

        if has_img_cue:
            latest = recent_imgs[0]
            if (now - latest["created_ts"]) <= 600:
                return Path(latest["image_path"]), latest["sender"]

        return None, ""

    def handle_message(
        self,
        group_id: str,
        sender: str,
        text: str = "",
        image_path: Path | None = None,
        force_trigger: bool = False,
        quoted_sender: str = "",
    ) -> BotResponse:
        now = time.time()
        clean_text = (text or "").strip()

        prev_activity_ts = self._last_group_activity_ts.get(group_id, 0.0)
        self._last_group_activity_ts[group_id] = now
        if clean_text or image_path:
            pet_engine.append_history(
                group_id=group_id,
                sender=sender,
                role="user",
                content=clean_text,
                image_path=str(image_path) if image_path else None,
            )

        if image_path is not None and image_path.is_file():
            self._recent_group_images[(group_id, sender)] = (image_path, now)
            sticker_manager.maybe_steal_meme(image_path)
            if not clean_text:
                image_reply_prob = tuning.get_float("IMAGE_REPLY_PROB", settings.AUTO_ROAST_IMAGE_PROB)
                if not (force_trigger or random.random() < image_reply_prob):
                    self._caption_image_async(group_id, image_path)
                    return BotResponse(should_reply=False)

        self._touch_pending(group_id, now, clean_text, sender)

        if clean_text:
            cmd_resp = self._handle_command(group_id, sender, clean_text)
            if cmd_resp is not None:
                self._cancel_pending(group_id)
                pet_engine.append_history(
                    group_id=group_id,
                    sender=settings.BOT_NAME,
                    role="assistant",
                    content=cmd_resp.text,
                    sticker_url=cmd_resp.sticker_url,
                )
                return cmd_resp

        if clean_text and settings.TOOL_LINK_ENABLED:
            try:
                self._handle_group_links(group_id, sender, clean_text)
            except Exception as exc:
                logger.warning("[%s] 链接理解调度异常: %s", group_id, exc)

        triggered = self._is_triggered(group_id, clean_text, force_trigger=force_trigger)
        interest_angle = ""

        if image_path is None and not triggered:
            # 先记录群聊热力脉冲（避免被去抖提前 return 吞掉热议升温）
            if clean_text:
                heartflow.record_group_pulse(group_id, sender, clean_text)

            followup_ok, is_quoted = self._followup_gate(group_id, clean_text, quoted_sender)
            if is_quoted:
                logger.info("🎯 L2 零成本唤醒：引用了 %s 的消息", (quoted_sender or "").strip())
                triggered = True
            else:
                kind, gap_desc = "", ""
                if followup_ok:
                    kind = "l2"
                elif clean_text:
                    interest_ok, gap_desc = self._interest_gate(group_id, clean_text, now, prev_activity_ts)
                    if interest_ok:
                        kind = "interest"

                if kind and settings.ENABLE_TRIGGER_DEBOUNCE:
                    self._defer_trigger(
                        group_id,
                        kind,
                        sender,
                        clean_text,
                        quoted_sender,
                        gap_desc,
                        now,
                        gap_sec=now - prev_activity_ts,
                    )
                    return BotResponse(should_reply=False)

                if kind == "interest":
                    interest_angle = self._interest_judge(group_id, sender, clean_text, gap_desc)
                    triggered = bool(interest_angle)
                elif kind == "l2":
                    triggered = self._followup_judge(group_id, sender, clean_text)
                    if not triggered and clean_text:
                        interest_ok, gap_desc = self._interest_gate(group_id, clean_text, now, prev_activity_ts)
                        if interest_ok:
                            interest_angle = self._interest_judge(group_id, sender, clean_text, gap_desc)
                            triggered = bool(interest_angle)

        if image_path is None and not triggered:
            return self._maybe_proactive_chime_in(group_id, now, record_pulse=False)

        self._cancel_pending(group_id)
        return self._compose_reply(
            group_id=group_id,
            sender=sender,
            text=clean_text,
            quoted_sender=quoted_sender,
            now=now,
            interest_angle=interest_angle,
            image_path=image_path,
        )

    # ------------------------------------------------------------------
    # 群内链接静默理解 & 后台沙箱派活
    # ------------------------------------------------------------------

    def _link_decision_for(self, group_id: str, text: str) -> dict | None:
        cached = self._link_decision.get(group_id)
        if not cached:
            return None
        cached_text, decision, ts = cached
        self._link_decision.pop(group_id, None)
        if cached_text != text or time.time() - ts > 90:
            return None
        return decision

    def _handle_group_links(self, group_id: str, sender: str, text: str) -> None:
        urls = link_reader.extract_urls(text, settings.TOOL_LINK_MAX_PER_MSG)
        if not urls:
            self._flush_queued_links(group_id, sender)
            return
        urls = self._link_limiter.fresh(group_id, urls)
        if not urls or not settings.ENABLE_TOOL_ROUTER or not settings.has_valid_api_key:
            return
        if not is_dsh_available()[0]:
            return

        recent = pet_engine.get_recent_history(group_id, limit=6)
        try:
            decision = brain.judge_tool_need(sender, text, recent, link_urls=urls)
        except Exception as exc:
            logger.warning("[%s] 链接判定异常: %s", group_id, exc)
            return
        metrics.record("tool_router", group_id, bool(decision.get("need")))

        self._link_decision[group_id] = (text, decision, time.time())
        if not decision.get("need") or str(decision.get("kind") or "link") == "work":
            return
        if self._link_limiter.is_busy(group_id):
            self._link_limiter.queue(group_id, urls)
            return
        self._dispatch_link_job(group_id, sender, urls, str(decision.get("task") or ""))

    def _dispatch_link_job(self, group_id: str, sender: str, urls: list[str], task: str) -> None:
        if not urls:
            return
        clean_task = (task or "").strip() or ("读懂群友发的链接并提炼核心事实：" + "、".join(urls[:2]))
        ok, payload = submit_dsh_job(group_id=group_id, sender=sender, task=clean_task, kind="link", urls=urls)
        if not ok:
            logger.warning("[%s] 派链接理解任务失败: %s", group_id, payload)
            return
        self._link_limiter.mark_seen(group_id, urls)
        self._link_limiter.mark_running(group_id, settings.TOOL_LINK_TIMEOUT_SEC)

    def _flush_queued_links(self, group_id: str, sender: str) -> None:
        if self._link_limiter.is_busy(group_id):
            return
        queued = self._link_limiter.pop_queued(group_id)
        fresh = self._link_limiter.fresh(group_id, queued)
        if fresh:
            self._dispatch_link_job(group_id, sender, fresh[: settings.TOOL_LINK_MAX_PER_MSG], "")

    def on_link_job_done(self, group_id: str) -> None:
        self._link_limiter.release((group_id or "").strip())

    def _dsh_busy(self, group_id: str) -> dict | None:
        info = self._dsh_running.get(group_id)
        if not info:
            return None
        if time.time() > float(info.get("deadline") or 0.0):
            self._dsh_running.pop(group_id, None)
            return None
        try:
            jobs = list_dsh_jobs(group_id=group_id, limit=6)
        except Exception:
            return info
        job_id = str(info.get("job_id") or "")
        cur = next((j for j in jobs if str(j.get("job_id") or "") == job_id), None)
        if cur is None or str(cur.get("status")) in ("done", "timeout", "error", "failed"):
            self._dsh_running.pop(group_id, None)
            return None
        info["elapsed"] = int(time.time() - float(info.get("since") or time.time()))
        return info

    def _bg_note(self, group_id: str) -> str:
        info = self._dsh_busy(group_id)
        if info:
            task = str(info.get("task") or "")[:60]
            return (
                f"【后台工作中】你刚把「{task}」放进后台跑（已 {int(info.get('elapsed') or 0)} 秒）。"
                "别重复派单，有人问进度就如实说还在跑。"
            )
        done = self._recent_done_job(group_id)
        if not done:
            return ""
        when = time.strftime("%H:%M", time.localtime(float(done.get("at") or 0.0)))
        note = f"【你自己刚干完的后台活】{when} 你已完成「{done.get('task')}」"
        if str(done.get("status") or "done") != "done":
            note += "（未成功）"
        if done.get("summary"):
            note += f"，结果：{done['summary']}"
        if done.get("artifacts"):
            note += f"，产出文件：{'、'.join(done['artifacts'])}"
        return note + "。（这是你自己干的活，别算到群友头上。）"

    def note_job_delivered(self, group_id: str, done: dict, spoken: str = "") -> None:
        self._dsh_running.pop(group_id, None)
        self._dsh_done[group_id] = {
            "job_id": str(done.get("job_id") or ""),
            "task": str(done.get("task") or "").strip()[:60],
            "status": str(done.get("status") or "done"),
            "summary": " ".join(str(done.get("summary") or "").split())[:120],
            "artifacts": [Path(str(p)).name for p in (done.get("_artifacts") or [])][:3],
            "spoken": (spoken or "").strip()[:100],
            "at": time.time(),
        }

    def _recent_done_job(self, group_id: str) -> dict | None:
        info = self._dsh_done.get(group_id)
        if not info:
            return None
        if time.time() - float(info.get("at") or 0.0) > DSH_DONE_NOTE_TTL_SEC:
            self._dsh_done.pop(group_id, None)
            return None
        return info

    def _route_tool_task(self, group_id: str, sender: str, text: str) -> str:
        if not settings.ENABLE_TOOL_ROUTER:
            return ""
        body = (text or "").strip()

        cached = self._link_decision_for(group_id, body)
        if cached is not None:
            if not cached.get("need") or str(cached.get("kind") or "") == "link" or self._dsh_busy(group_id):
                return ""
            return self._dispatch_work_job(group_id, sender, str(cached.get("task") or ""))

        if len(body) < tuning.get_int("TOOL_ROUTER_MIN_CHARS", settings.TOOL_ROUTER_MIN_CHARS):
            return ""
        if not is_dsh_available()[0] or self._dsh_busy(group_id):
            return ""

        recent = pet_engine.get_recent_history(group_id, limit=6)
        try:
            decision = brain.judge_tool_need(sender, body, recent)
        except Exception as exc:
            logger.warning("[%s] 工具路由判定异常: %s", group_id, exc)
            return ""
        metrics.record("tool_router", group_id, bool(decision.get("need")))
        if not decision.get("need"):
            return ""
        return self._dispatch_work_job(group_id, sender, str(decision.get("task") or ""))

    def _dispatch_work_job(self, group_id: str, sender: str, task: str) -> str:
        task = (task or "").strip()[:400]
        if not task or looks_like_self_modify(task):
            return ""
        ok, payload = submit_dsh_job(group_id=group_id, sender=sender, task=task)
        if not ok:
            logger.warning("[%s] 派活失败: %s", group_id, payload)
            return ""
        job_id = str(payload.get("job_id") or "") if isinstance(payload, dict) else ""
        self._dsh_running[group_id] = {
            "job_id": job_id,
            "task": task,
            "sender": sender,
            "since": time.time(),
            "deadline": time.time() + settings.DSH_TIMEOUT_SEC + 60,
        }
        return task

    def report_job_result(self, group_id: str, done: dict) -> BotResponse | None:
        sender = str(done.get("sender") or "").strip() or settings.BOT_NAME
        task = str(done.get("task") or "").strip()
        summary = str(done.get("summary") or "").strip()
        status = str(done.get("status") or "done")
        duration = done.get("duration_sec", 0)
        artifacts = done.get("_artifacts") or []
        report_path = str(done.get("_report_path") or "")

        label = {"done": "完成", "timeout": "超时"}.get(status, "执行异常")
        lines = [
            "【后台任务结果】你刚在后台跑完一单任务，请向群里简短交代结论。",
            f"发起人：{sender}",
            f"任务：{task[:120]}",
            f"状态：{label}（耗时 {duration} 秒）",
            f"结果摘要：\n{summary[:700] if summary else '(无摘要)'}",
        ]
        if artifacts:
            names = "、".join(Path(str(p)).name for p in artifacts[:5])
            lines.append(f"产出文件：{names}（系统会自动附带文件发群，你无需重复贴路径）")
        elif report_path:
            lines.append("产出文件：无（详细报告 txt 会自动附带发群）")
        lines.append("要求：1~2 句利落口语讲清核心结论（或报错卡点），禁用 Markdown、编号列表与本地路径。")

        try:
            brain_reply = brain.reply(
                sender="系统",
                text="后台任务已跑完，请用 1~2 句利落口语交代核心结论。",
                recent_history=pet_engine.list_history(group_id),
                group_id=group_id,
                job_report="\n".join(lines),
            )
        except Exception as exc:
            logger.warning("[%s] 生成后台任务播报失败: %s", group_id, exc)
            return None

        text = (brain_reply.text or "").strip()
        if not text or "信号抖了一下" in text or "刚查完一轮" in text:
            return None
        text = re.sub(r"^@[\s:：]{0,3}\S{0,12}[：:]\s*", "", text).strip()
        text = humanize_job_announcement(text.replace("**", "").replace("`", ""))
        if not text:
            return None
        return BotResponse(
            should_reply=True,
            text=text,
            mood="smug" if status == "done" else "sleepy",
            status=pet_engine.get_status(),
        )

    # ------------------------------------------------------------------
    # 主回复编排与半主动热议补刀
    # ------------------------------------------------------------------

    def _compose_reply(
        self,
        group_id: str,
        sender: str,
        text: str,
        quoted_sender: str,
        now: float,
        interest_angle: str = "",
        image_path: Path | None = None,
        allow_image_lookup: bool = True,
        history_strip: list[str] | None = None,
    ) -> BotResponse:
        if not interest_angle and image_path is None:
            routed_task = self._route_tool_task(group_id, sender, text)
            if routed_task:
                ack = random.choice(DISPATCH_ACKS)
                return BotResponse(should_reply=True, text=ack, mood="smug", status=pet_engine.get_status())

        heartflow.drain(group_id, amount=25.0)

        effective_image = image_path
        image_sender = sender if effective_image is not None else ""
        if effective_image is None and allow_image_lookup and not interest_angle:
            effective_image, image_sender = self._pick_referenced_image(
                group_id=group_id,
                sender=sender,
                text=text,
                now=now,
                quoted_sender=quoted_sender,
            )

        if settings.L3_INJECT_ALL:
            recent = pet_engine.list_history(group_id)
        else:
            recent = pet_engine.get_recent_history(group_id, limit=settings.L3_HISTORY_LIMIT + 2)
        if history_strip:
            strip_set = {s.strip() for s in history_strip if s and s.strip()}
            recent = [m for m in recent if (m.get("content") or "").strip() not in strip_set]

        brain_reply = brain.reply(
            sender=sender,
            text=self._strip_trigger(text),
            recent_history=recent,
            image_path=effective_image,
            group_id=group_id,
            image_sender=image_sender,
            interest_hint=interest_angle,
            bg_note=self._bg_note(group_id),
        )

        if brain_reply.vision_summary and effective_image is not None:
            pet_engine.update_history_content_by_image(
                group_id, str(effective_image), brain_reply.vision_summary
            )

        self._last_bot_reply_ts[group_id] = time.time()

        sticker_path = None
        if brain_reply.attachment_path and brain_reply.attachment_path.is_file():
            sticker_path = brain_reply.attachment_path
        elif settings.STICKER_REPLY_PROB > 0 and random.random() < settings.STICKER_REPLY_PROB:
            sticker_path = self._maybe_pick_sticker(
                brain_reply.mood, caption=brain_reply.sticker_caption or None
            )
        sticker_url = sticker_manager.to_web_url(sticker_path)

        pet_engine.append_history(
            group_id=group_id,
            sender=settings.BOT_NAME,
            role="assistant",
            content=brain_reply.text,
            sticker_url=sticker_url,
        )
        return BotResponse(
            should_reply=True,
            text=brain_reply.text,
            bubbles=brain_reply.bubbles,
            sticker_path=sticker_path,
            sticker_url=sticker_url,
            mood=brain_reply.mood,
            status=pet_engine.get_status(),
        )

    def _maybe_proactive_chime_in(
        self,
        group_id: str,
        now: float,
        sender: str = "",
        text: str = "",
        record_pulse: bool = True,
    ) -> BotResponse:
        """群聊多人热议/争论真假时，热力达标则触发一次 Grok 半主动锐评判断。"""
        if not settings.ENABLE_PROACTIVE_CHIME or self.is_quiet_hours():
            return BotResponse(should_reply=False)

        energy = (
            heartflow.record_group_pulse(group_id, sender, text)
            if record_pulse and text
            else heartflow.get_energy(group_id)
        )
        threshold = tuning.get_float("PROACTIVE_ENERGY_MIN", 68.0)
        if energy < threshold:
            return BotResponse(should_reply=False)

        last_ts = self._last_proactive_ts.get(group_id, 0.0)
        if (now - last_ts) < settings.PROACTIVE_COOLDOWN_SEC:
            return BotResponse(should_reply=False)

        heartflow.drain(group_id, amount=60.0)
        self._last_proactive_ts[group_id] = now

        recent = pet_engine.get_recent_history(group_id, limit=8)
        chime = brain.judge_and_chime_in(recent, group_id=group_id)
        if chime is None:
            return BotResponse(should_reply=False)

        self._last_bot_reply_ts[group_id] = time.time()
        sticker_path = (
            self._maybe_pick_sticker(chime.mood)
            if (settings.STICKER_REPLY_PROB > 0 and random.random() < settings.STICKER_REPLY_PROB)
            else None
        )
        sticker_url = sticker_manager.to_web_url(sticker_path)
        pet_engine.append_history(
            group_id=group_id,
            sender=settings.BOT_NAME,
            role="assistant",
            content=chime.text,
            sticker_url=sticker_url,
        )
        return BotResponse(
            should_reply=True,
            text=chime.text,
            bubbles=chime.bubbles,
            sticker_path=sticker_path,
            sticker_url=sticker_url,
            mood=chime.mood,
            status=pet_engine.get_status(),
        )

    # ------------------------------------------------------------------
    # 夜间轻量维护（统计日报 + L3 超限归档 + 群友记忆沉淀）
    # ------------------------------------------------------------------

    def _maybe_write_daily_metrics(self) -> Path | None:
        dt = datetime.now()
        if dt.hour >= 4:
            return None
        target = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        if self._last_metrics_date == target:
            return None
        self._last_metrics_date = target
        out = DATA_DIR / "统计" / f"子代理调用统计_{target}.txt"
        if out.is_file():
            return None
        try:
            return metrics.write_report(target)
        except Exception as exc:
            logger.warning("生成每日调用统计失败: %s", exc)
            return None

    def _maybe_compress_l3(self, group_id: str) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_l3_check_day.get(group_id) == today:
            return
        self._last_l3_check_day[group_id] = today
        try:
            rows = pet_engine.list_history(group_id)
            if not rows:
                return
            total_tk = sum(estimate_tokens(r.get("content") or "") + 6 for r in rows)
            if total_tk < settings.L3_CONTEXT_LIMIT_TOKENS or len(rows) <= settings.L3_KEEP_RECENT:
                return

            archive_dir = DATA_DIR / "会话归档"
            archive_dir.mkdir(parents=True, exist_ok=True)
            safe_gid = re.sub(r'[\\/:*?"<>|]+', "_", group_id) or "default"
            archive_file = archive_dir / f"{safe_gid}.jsonl"

            candidates = rows[: len(rows) - settings.L3_KEEP_RECENT]
            chunk_size = settings.L3_COMPRESS_CHUNK
            for start in range(0, len(candidates), chunk_size):
                batch = candidates[start : start + chunk_size]
                if not batch:
                    continue
                with archive_file.open("a", encoding="utf-8") as fh:
                    for r in batch:
                        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                summary = brain.summarize_l3_history(batch, group_id=group_id)
                if summary:
                    t0 = datetime.fromtimestamp(float(batch[0]["created_ts"])).strftime("%Y-%m-%d")
                    t1 = datetime.fromtimestamp(float(batch[-1]["created_ts"])).strftime("%Y-%m-%d")
                    span = t0 if t0 == t1 else f"{t0}~{t1}"
                    memory_store.remember(
                        text=f"【旧聊归档 {span}】{summary}",
                        level="important",
                        tags=["会话归档", span],
                        source="owner",
                        group_id=group_id,
                    )
                pet_engine.delete_history_ids([int(r["id"]) for r in batch])
                cur_rows = pet_engine.list_history(group_id)
                if sum(estimate_tokens(r.get("content") or "") + 6 for r in cur_rows) <= int(settings.L3_CONTEXT_LIMIT_TOKENS * 0.7):
                    break
        except Exception as exc:
            logger.warning("[%s] L3 会话压缩异常: %s", group_id, exc)

    @staticmethod
    def _day_bounds(day_str: str) -> tuple[float, float]:
        start = datetime.strptime(day_str, "%Y-%m-%d")
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    def _collect_day_material(self, group_id: str, day_str: str) -> list[dict]:
        start_ts, end_ts = self._day_bounds(day_str)
        return pet_engine.get_history_between(
            group_id, start_ts, end_ts, limit=settings.NIGHTLY_MATERIAL_MAX
        )

    def _nightly_state(self) -> dict:
        if not self._nightly_state_path.is_file():
            return {}
        try:
            data = json.loads(self._nightly_state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_nightly_state(self, state: dict) -> None:
        try:
            self._nightly_state_path.parent.mkdir(parents=True, exist_ok=True)
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            state = {
                gid: {d: v for d, v in days.items() if d >= cutoff}
                for gid, days in state.items()
                if isinstance(days, dict)
            }
            tmp = self._nightly_state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._nightly_state_path)
        except Exception as exc:
            logger.warning("夜间进度落盘失败: %s", exc)

    def run_nightly_maintenance(self, group_id: str, logical_day: str = "") -> dict:
        """轻量群友记忆沉淀：巩固淘汰旧节点 + 自动备份 + 可选提炼当日群友新梗/flag。"""
        logical_day = logical_day or datetime.now().strftime("%Y-%m-%d")
        state = self._nightly_state()
        if state.get(group_id, {}).get(logical_day):
            return {"skipped": "already_done"}

        result: dict = {"facts": 0, "consolidate": None, "backup": ""}
        if settings.MEMORY_NIGHTLY_EXTRACT:
            material = self._collect_day_material(group_id, logical_day)
            if material:
                start_ts, end_ts = self._day_bounds(logical_day)
                if memory_store.count_writes_between(start_ts, end_ts) == 0:
                    for fact in brain.extract_daily_facts(material, group_id=group_id):
                        ok, _ = memory_store.remember(
                            text=fact["text"],
                            level="normal",
                            tags=fact["tags"],
                            source="self",
                            group_id=group_id,
                            ignore_daily_limit=True,
                        )
                        if ok:
                            result["facts"] += 1

        if self._last_nightly_date != logical_day:
            self._last_nightly_date = logical_day
            try:
                result["consolidate"] = memory_store.consolidate()
            except Exception as exc:
                logger.warning("L1 巩固异常: %s", exc)
            try:
                result["backup"] = str(memory_store.backup_memory(keep_last=7) or "")
            except Exception as exc:
                logger.warning("记忆备份异常: %s", exc)

        state.setdefault(group_id, {})[logical_day] = time.time()
        self._save_nightly_state(state)
        return result

    def check_autonomous_heartbeat(self, group_id: str) -> BotResponse | None:
        """每 5 分钟后台轻巡检：只做夜间记忆巩固备份、L3 压缩与 0 点日报，绝不自嗨刷屏。"""
        dt = datetime.now()
        if dt.hour == 23 and dt.minute >= 45:
            self.run_nightly_maintenance(group_id, logical_day=dt.strftime("%Y-%m-%d"))
        elif dt.hour < 4:
            prev_day = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
            self.run_nightly_maintenance(group_id, logical_day=prev_day)

        self._maybe_compress_l3(group_id)

        report_path = self._maybe_write_daily_metrics()
        if report_path is not None and not self.is_quiet_hours():
            day_label = report_path.stem.rsplit("_", 1)[-1]
            return BotResponse(
                should_reply=True,
                text=f"📊 {day_label} 的子代理调用日报（见附件）",
                file_path=report_path,
                mood="smug",
                status=pet_engine.get_status(),
            )
        return None

    def execute_scheduled_task(self, task: dict) -> BotResponse:
        group_id = task.get("group_id", "")
        creator = task.get("creator", "群友")
        content = task.get("content", "")
        mode = task.get("mode", "remind")

        if mode == "ai_task":
            return self.handle_message(
                group_id=group_id,
                sender=creator,
                text=f"定时任务到点，请立刻联网核查或执行：{content}",
                force_trigger=True,
            )

        msg = f"⏰ 定时提醒（{creator}）：{content}"
        pet_engine.append_history(
            group_id=group_id,
            sender=settings.BOT_NAME,
            role="assistant",
            content=msg,
        )
        return BotResponse(
            should_reply=True,
            text=msg,
            bubbles=[msg],
            mood="smug",
            status=pet_engine.get_status(),
        )


bot_core = DaFeiYuBotCore()
