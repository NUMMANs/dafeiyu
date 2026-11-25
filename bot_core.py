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
from brain import brain
from config import settings
from dsh_bridge import is_dsh_available, list_dsh_jobs, submit_dsh_job
from image_fetcher import download_image_url, search_and_download_image
from memory import memory_store
import metrics
from persona_runtime import emotion_sm, heartflow, slang_book
from pet_engine import PetStatus, pet_engine, progress_bar
from roles.build_card import get_active_persona, get_evolutions, list_personas, set_active_persona
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

logger = logging.getLogger(__name__)


# ⚠️ 2026-11-25 起 L1 硬触发只认 @ 唤名，以下三张词表已不参与唤醒判定。
# 保留是为了随时可恢复（在 _is_triggered 里加回对应判定即可），不是死配置。
# 英雄联盟 / 游戏话题词表
LOL_KEYWORDS = (
    "lol", "英雄联盟", "联盟", "排位", "大乱斗", "海斗", "海克斯", "极地大乱斗",
    "云顶", "金铲铲", "峡谷", "召唤师峡谷", "上单", "打野", "中单", "下路", "辅助",
    "adc", "ad", "ap", "开黑", "五排", "双排", "单排", "掉分", "上分", "连跪", "连胜",
    "战绩", "kda", "出装", "符文", "天赋", "对线", "打团", "五杀", "掌盟", "补刀",
    "ban", "bp", "黑铁", "青铜", "白银", "黄金", "铂金", "翡翠", "钻石", "大师", "王者",
    "lpl", "lck", "msi", "s赛", "faker", "uzi", "theshy", "bin", "knight",
    "亚索", "劫", "盲僧", "剑圣", "提莫", "诺手", "剑魔", "卡莎", "ez", "vn", "锤石", "机器人",
)


QUESTION_CUES = (
    "怎么", "咋办", "咋弄", "咋样", "为啥", "为什么", "什么意思", "是什么", "是啥",
    "哪个好", "推荐", "区别", "原理", "报错", "怎么办", "怎么解决", "多少钱",
    "有必要", "好玩吗", "值得", "氪吗", "肝吗", "怎么选", "哪款", "有人知道",
    "谁知道", "请问", "教程", "攻略", "怎么买", "怎么用", "怎么装", "为啥会",
    "是什么意思", "怎么回事", "哪里下", "在哪下",
)

# 排除纯熟人之间约饭/买票/问行程的私人对话，避免助理乱插嘴
PRIVATE_LOGISTICS_CUES = (
    "回家", "买票", "候补", "转车", "出去玩", "值班", "请假", "打电话",
    "今晚怎么说", "老地方", "几点去", "哪天吃饭", "咱几个", "咱4个", "咱两",
)


# 【已停用】原用于"没叫名字但含你/他/她/它 就去做上下文判定"。
# 2026-11-25 起 L2 只看"我发言后 5 条内"，不再用代词作为进入判定条件。
# 保留词表是为了需要时能快速恢复；"谁在跟谁说话"现在交给判定提示词自己读上下文。
ABOUT_ME_CUES = (
    "你", "他", "它", "她", "这鱼", "那鱼", "机器人", "ai", "小号", "头像", "换了", "塔菲",
)


@dataclass
class BotResponse:
    should_reply: bool
    text: str = ""
    bubbles: list[str] = field(default_factory=list)
    sticker_path: Path | None = None
    sticker_url: str | None = None
    # 需要以「文件」形式发到群里的附件（如每日统计 txt），与 sticker_path 互不影响
    file_path: Path | None = None
    mood: str = "happy"
    status: PetStatus | None = None

    def get_bubbles(self) -> list[str]:
        if self.bubbles:
            return self.bubbles
        return [self.text] if self.text else []


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
            sid_label = payload.get("session_id") or "新建会话（DSH 自动分配）"
            return (
                f"✅ 二次确认通过（`{code}`），已交给独立 DSH 后台开工！\n"
                f"• 任务号：`{payload['job_id']}`\n"
                f"• 会话ID：`{sid_label}`\n"
                f"做完会自动发回群里。",
                None,
            )
        return f"❌ 确认后派单失败（`{code}`）：{payload}", None

    if action_type == "run_shell":
        out = run_shell(str(params.get("command") or ""))
        return f"✅ 已确认执行命令（`{code}`）：\n{out}", None

    if action_type == "write_local_file":
        out = write_local_file(str(params.get("path") or ""), str(params.get("content") or ""))
        return f"✅ 已确认写入文件（`{code}`）：\n{out}", None

    if action_type == "read_local_file":
        out = read_local_file(str(params.get("path") or ""))
        return f"✅ 已确认读取文件（`{code}`）：\n{out}", None

    if action_type == "send_existing_file":
        fpath, out = send_existing_file(str(params.get("path") or ""))
        valid_path = fpath if (fpath and fpath.is_file() and fpath.stat().st_size > 0) else None
        return f"✅ 已确认发送文件（`{code}`）：{out}", valid_path

    if action_type == "open_url_or_app":
        out = open_url_or_app(str(params.get("target") or ""))
        return f"✅ 已确认打开目标（`{code}`）：{out}", None

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
    大肥鱼核心调度器：
    - 处理养成指令（/状态、/投喂、/摸鱼、/鱼塘榜、/帮助）
    - 处理群聊触发规则（@触发、图片自动锐评概率、发图后紧跟提问的上下文关联）
    - 调度 DeepSeek 多模态大脑 + 养成数值更新 + 表情包挑选
    """

    def __init__(self) -> None:
        # 缓存每个群最近 90 秒内群友发过的图片，解决“先发图、再 @大肥鱼 问这是啥”的微信常见场景
        self._recent_group_images: dict[tuple[str, str], tuple[Path, float]] = {}
        # 自主潜水插嘴 & 助理自动答疑冷却追踪
        self._unanswered_count: dict[str, int] = {}
        self._last_proactive_ts: dict[str, float] = {}
        self._last_assistant_ts: dict[str, float] = {}
        self._last_bot_reply_ts: dict[str, float] = {}
        self._last_group_activity_ts: dict[str, float] = {}
        # L2.5 冷场重启话题吐槽：只靠 TOPIC_GAP_SEC 控频，不再有同群冷却/日上限
        # L2/L2.5 去抖队列：群里连续发言时先挂起，等停顿够久再判定（适配器每秒 pop 一次）
        self._pending_trigger: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        self._last_hungry_bubble_ts: dict[str, float] = {}
        self._last_evening_bubble_date: dict[str, str] = {}
        self._last_nightly_date: str = ""
        self._last_metrics_date: str = ""

    def _maybe_pick_sticker(self, mood: str, caption: str | None = None) -> Path | None:
        """峰哥人设不发表情包（仅保留搜图/截屏/文件等功能性附件），塔菲人设正常发表情包。"""
        if get_active_persona() == "fengge":
            return None
        return sticker_manager.pick_sticker(mood, caption=caption)

    def is_quiet_hours(self) -> bool:
        """23:00 ~ 次日 08:00 默认静默睡觉，绝不半夜扰民"""
        hour = datetime.now().hour
        start = settings.QUIET_START_HOUR
        end = settings.QUIET_END_HOUR
        if start > end:
            return hour >= start or hour < end
        return start <= hour < end

    def _is_lol_related(self, text: str) -> bool:
        """【已停用】原先用于"游戏话题 100% 必回"，现 L1 只认 @唤名，保留待恢复。"""
        low = text.lower()
        return any(kw in low for kw in LOL_KEYWORDS)

    def _is_answerable_question(self, group_id: str, text: str) -> bool:
        """
        【已停用】原先用于"群友有疑问就主动当助理回答"，现 L1 只认 @唤名，保留待恢复。
        """
        t = text.strip()
        if len(t) < 4:
            return False
        # 过滤纯熟人私人行程
        if any(p in t for p in PRIVATE_LOGISTICS_CUES):
            return False
        is_q = any(cue in t for cue in QUESTION_CUES) or (
            len(t) >= 6 and t.endswith(("?", "？", "吗", "嘛"))
        )
        return is_q

    def _name_prefixes(self) -> list[str]:
        """「以名字开头」判定用：机器人名 + 去掉 @ 的唤醒词。"""
        prefixes = [settings.BOT_NAME] + [k.lstrip("@") for k in settings.TRIGGER_KEYWORDS]
        return [p for p in prefixes if p]

    def _is_triggered(self, group_id: str, text: str, force_trigger: bool = False) -> bool:
        """
        群聊唤醒判定（L1 硬触发）：
        1. 文本里出现唤醒词（默认 `@大肥鱼`）；
        2. 文本以机器人名字开头（如「大肥鱼，BLG夺冠了」）——2026-11-25 新增，没 @ 也算叫我。

        2026-11-25 收紧说明：
        - 原先还会因「英雄联盟关键词」「任意答疑词（怎么/为什么/推荐…）」自动接话，
          这两个条件已下线，对应常量 LOL_KEYWORDS / QUESTION_CUES 保留但不再参与判定。
        - 群内快捷指令（/状态 /投喂 /搜图 …）在 handle_message 第 3 步就由
          _handle_command 处理并直接返回，不依赖本函数的 "/" 前缀判定。
        """
        if force_trigger or not settings.GROUP_AT_ONLY:
            return True
        probe = text.strip()
        if not probe:
            return False
        low = probe.lower()
        if any(kw.lower() in low for kw in settings.TRIGGER_KEYWORDS):
            return True
        # 以名字开头：微信里很多人习惯直接「大肥鱼，xxx」不带 @
        return any(probe.startswith(p) for p in self._name_prefixes())

    def _strip_trigger(self, text: str) -> str:
        """去掉唤醒词本体、开头的名字称呼，以及微信 @ 之后紧跟的窄空格 U+2005 / 全角空格。"""
        cleaned = text
        for kw in settings.TRIGGER_KEYWORDS:
            cleaned = cleaned.replace(kw, "")
        cleaned = cleaned.lstrip()
        # 没 @ 但以名字开头时同样剥掉，避免 prompt 里重复一句「大肥鱼」
        for prefix in sorted(self._name_prefixes(), key=len, reverse=True):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
        return cleaned.strip(" \u2005\u3000\t\r\n,，:：!！@")

    def _followup_gate(
        self,
        group_id: str,
        text: str,
        quoted_sender: str = "",
        max_turns: int = 5,
    ) -> tuple[bool, bool]:
        """
        L2 轻量闸门（不调模型，纯本地判断），供去抖入队时使用。

        返回 (是否值得进入追认判定, 是否命中「引用我」零成本快路)。
        设计要点：
        - 取消原 120 秒时间窗，改为"按条数"推进：我发言后最多再看 5 条，第 6 条起不再判定。
          好处是成本有上界——每次我开口最多换来 5 次判定，不会无限膨胀。
        - 引用我的消息是强信号，直接唤醒，不需要模型也不需要去抖。
        """
        body = (text or "").strip()
        if len(body) < 2:
            return False, False

        # 快路 1：引用的是我的消息，必是在跟我说话（群昵称常带后缀，用包含匹配）
        quoted = (quoted_sender or "").strip()
        if quoted and settings.BOT_NAME and settings.BOT_NAME in quoted:
            return True, True

        recent = pet_engine.get_recent_history(group_id, limit=max_turns + 8)
        # 第 1 步就已把当前这条写进历史，这里剔除，避免拿自己当上下文
        if recent and (recent[-1].get("content") or "").strip() == body:
            recent = recent[:-1]

        last_asst_idx = -1
        for idx in range(len(recent) - 1, -1, -1):
            if recent[idx].get("role") == "assistant":
                last_asst_idx = idx
                break
        if last_asst_idx < 0:
            return False, False  # 我从没在这个群说过话，无从"追认"

        window = recent[last_asst_idx:]
        turns_after = sum(1 for m in window[1:] if m.get("role") != "assistant")
        if turns_after >= max_turns:
            return False, False  # 超出 5 条窗口，交回潜水逻辑

        return True, False

    def _followup_judge(self, group_id: str, sender: str, text: str, max_turns: int = 5) -> bool:
        """
        L2 正式判定：用「此刻」的历史重建窗口再调模型。

        停顿后执行的好处：连续发言的几句都已经落库，模型看到的是完整的一波对话，
        而不是第一条刚说完就急着判断。
        """
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
        # L1 已确认没 @ 我，此时文本里出现 @ 只可能是 @ 了别人
        verdict = brain.is_talking_to_or_about_me(
            sender=sender,
            text=body,
            recent_history=window,
            at_others="@" in body,
        )
        metrics.record("l2", group_id, verdict)
        return verdict

    @staticmethod
    def _roast_text_ok(text: str) -> bool:
        """一句话够不够格当「话题锚点」：非指令、非原始 XML、有基本话题量。"""
        body = (text or "").strip()
        if len(body) < 6:
            return False  # 「在吗」「哈哈哈」这种没有话题量
        if body.startswith("/"):
            return False  # 未识别的指令，不是话题
        if body.startswith("<"):
            return False  # 适配器偶尔漏出的原始 XML 消息体
        # 至少要有两个汉字，过滤 wxid、纯表情、纯数字
        return sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff") >= 2

    @staticmethod
    def _burst_item_ok(text: str) -> bool:
        """一句话能不能并进「这一波的合并探针」（比话题锚点宽松，只挡明显无意义的）。"""
        body = (text or "").strip()
        if not body or body.startswith("/") or body.startswith("<"):
            return False
        return sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff") >= 1

    def _roast_gate(
        self,
        group_id: str,
        text: str,
        now: float,
        prev_activity_ts: float,
    ) -> tuple[bool, str]:
        """
        L2.5 规则闸门（不调模型）：判断这条消息是不是「冷场后重新开启的话题」。

        2026-11-25 调整：
        - 取消「含 @ 就不判」：@ 别人也照常送判定（用户要求），交给判定器看话题本身；
        - 取消「以名字开头就不判」：L1 已把「以名字开头」改成强制唤醒，走不到这里；
        - 取消同群冷却与每日上限：连发去抖已经能约束频率，用户要求只保留 TOPIC_GAP_SEC。

        返回 (是否值得送判定, 冷场时长的人类可读描述)。
        """
        if not settings.ENABLE_TOPIC_ROAST or self.is_quiet_hours():
            return False, ""

        if not self._roast_text_ok(text):
            return False, ""
        # 进程刚启动时不知道这个群冷场了多久，宁可不吐槽，避免重启后乱接一句话
        if prev_activity_ts <= 0:
            return False, ""

        gap = now - prev_activity_ts
        if gap < settings.TOPIC_GAP_SEC:
            return False, ""

        gap_desc = f"约 {int(gap // 3600)} 小时" if gap >= 3600 else f"约 {int(gap // 60)} 分钟"
        return True, gap_desc

    def _roast_judge(
        self,
        group_id: str,
        sender: str,
        text: str,
        gap_desc: str,
        multi_speaker: bool = False,
    ) -> str:
        """
        L2.5 正式判定：调轻量判定器，通过就返回可吐槽的点，否则返回空串。

        text 可能是这一波连续发言的合并探针：同一人连发时按行拼接，
        多人接话时每行带 `[说话人]:` 前缀（multi_speaker=True），
        这样判定器不会把几个人的话误读成一个人说的。
        """
        body = (text or "").strip()
        if not body:
            return ""

        # 上下文取最近几条，剔除已经并入探针的那部分（避免同一句出现两次）。
        # 多人探针带 `[昵称]: ` 前缀，历史里存的是原文，所以要剥掉前缀再比对。
        recent = pet_engine.get_recent_history(group_id, limit=8)
        merged_lines: set[str] = set()
        for ln in body.splitlines():
            s = ln.strip()
            if not s:
                continue
            merged_lines.add(re.sub(r"^\[[^\]]{1,40}\]:\s*", "", s))
        # 已并入探针的句子要从上下文里整体移除，否则同一句会同时出现在「探针」和「更早的几句」里
        if merged_lines:
            recent = [m for m in recent if (m.get("content") or "").strip() not in merged_lines]

        angle = brain.judge_topic_roast(
            sender=sender,
            text=body,
            recent_history=recent,
            gap_desc=gap_desc,
            at_others="@" in body,
            multi_speaker=multi_speaker,
        )
        metrics.record("l2_5", group_id, bool(angle))
        if not angle:
            return ""

        logger.info(
            "🔥 L2.5 冷场吐槽放行：%s 隔 %s 开话题「%s」，槽点=%s",
            sender,
            gap_desc,
            body[:40].replace("\n", " / "),
            angle,
        )
        return angle

    # ------------------------------------------------------------------
    # L2 / L2.5 去抖：群里连发时先挂起，等「发言停顿」再判定
    # ------------------------------------------------------------------

    def _append_burst_locked(self, item: dict, sender: str, text: str) -> None:
        """
        把一条发言并进这一波的合并探针（调用方必须已持有 _pending_lock）。

        会挡掉指令、原始 XML、纯符号这几类 —— 它们并进去只会污染判定，
        但「群还在说话」这件事由 _touch_pending 单独续期，不受影响。

        攒满 TRIGGER_BURST_MAX_ITEMS（默认 5，与 L2 的上下文窗口一致）就把 due
        拉到当前时刻：这一波已经够长了，不必再等停顿，整批一次性交给判定器。
        """
        body = (text or "").strip()
        if not body or not self._burst_item_ok(body):
            return
        burst = item.setdefault("burst", [])
        cap = max(1, settings.TRIGGER_BURST_MAX_ITEMS)
        if len(burst) >= cap:
            item["flushed"] = True
            item["due"] = 0.0
            return
        who = (sender or "").strip()
        if burst and burst[-1].get("text") == body and (burst[-1].get("sender") or "") == who:
            return
        burst.append({"sender": who, "text": body})
        if len(burst) >= cap:
            # 已经攒满判定窗口：立刻送判，不再等停顿
            item["flushed"] = True
            item["due"] = 0.0

    def _defer_trigger(
        self,
        group_id: str,
        kind: str,
        sender: str,
        text: str,
        quoted_sender: str,
        gap_desc: str,
        now: float,
    ) -> None:
        """把一次待判定登记进队列，due 时间由「最后一条消息」决定（新的覆盖旧的）。"""
        body = (text or "").strip()
        with self._pending_lock:
            existing = self._pending_trigger.get(group_id)
            if existing is not None:
                if existing.get("flushed"):
                    # 这一波已经攒满、正在等发车，新来的没赶上，交给下一轮
                    return
                # 同一波里既像「接我话」又像「冷场开话题」时，接我话优先级更高（信号更明确）
                if kind == "l2" and existing.get("kind") != "l2":
                    existing["kind"] = "l2"
                # 同一波对话里的后续发言：保留已有 burst，只续期
                existing["due"] = now + settings.TRIGGER_DEBOUNCE_SEC
                if quoted_sender:
                    existing["quoted"] = quoted_sender
                self._append_burst_locked(existing, sender, body)
                return
            self._pending_trigger[group_id] = {
                "group_id": group_id,
                "kind": kind,  # "l2" | "roast"
                "sender": sender,
                # 每项记说话人：多人接话时探针要带 [昵称] 前缀，不能让判定器以为是一人连发
                "burst": [{"sender": (sender or "").strip(), "text": body}],
                "quoted": quoted_sender,
                "gap_desc": gap_desc,
                "due": now + settings.TRIGGER_DEBOUNCE_SEC,
                "start": now,
            }
        logger.debug("⏳ 去抖挂起 [%s/%s] due=+%.1fs: %s", group_id, kind, settings.TRIGGER_DEBOUNCE_SEC, body[:30])

    def _touch_pending(self, group_id: str, now: float, text: str = "", sender: str = "") -> None:
        """
        群里每来一条消息就把停顿时钟往后推：只要还在连着说话，就不判定。

        同时把这条也并进 burst —— 连发时包袱经常在最后一句（「…」「现在又好了草」），
        只拿第一条去判定会漏掉关键信息。条数上限见 TRIGGER_BURST_MAX_ITEMS。
        """
        body = (text or "").strip()
        with self._pending_lock:
            item = self._pending_trigger.get(group_id)
            if item is None or item.get("flushed"):
                return
            item["due"] = now + settings.TRIGGER_DEBOUNCE_SEC
            self._append_burst_locked(item, sender, body)

    def _cancel_pending(self, group_id: str) -> None:
        """已经用别的方式回复了（@唤醒、指令、图片），作废本群挂起的判定，避免二次回复。"""
        with self._pending_lock:
            self._pending_trigger.pop(group_id, None)

    @staticmethod
    def _compose_burst(burst: list[dict], primary: str = "last") -> tuple[str, str, bool]:
        """
        把一波发言拼成判定探针，返回 (探针文本, 主说话人, 是否多人)。

        - 同一人连发：按行拼接，保持「一个人一口气说完」的语义；
        - 多人接话：每行加 `[昵称]:` 前缀，判定器才能分清谁说了什么（否则会张冠李戴）；
        - 主说话人：L2 取最后一条（要判的是「现在这句」），L2.5 取第一条（冷场开话题的人）。
        """
        items = [b for b in (burst or []) if (b.get("text") or "").strip()]
        if not items:
            return "", "", False
        senders: list[str] = []
        for b in items:
            who = (b.get("sender") or "").strip()
            if who and who not in senders:
                senders.append(who)
        multi = len(senders) > 1
        if multi:
            probe = "\n".join(f"[{(b.get('sender') or '群友').strip()}]: {b['text'].strip()}" for b in items)
        else:
            probe = "\n".join(b["text"].strip() for b in items)
        target = items[0] if primary == "first" else items[-1]
        return probe, (target.get("sender") or "").strip(), multi

    def _judge_pending(self, item: dict) -> tuple[bool, str, str, str]:
        """
        执行一次挂起的判定，返回 (是否要回复, roast 槽点, 探针文本, 主说话人)。

        - L2.5：判定对象是这一波的**全部**发言（合并探针），因为是评价整段话题；
        - L2：判定对象只取**最后一句**（「现在这句是不是在接我话」），
              前面的几句已经落库成历史上下文，再拼进探针只会重复。
        """
        group_id = item["group_id"]
        fallback = (item.get("sender") or "").strip()
        if item.get("kind") == "roast":
            probe, who, multi = self._compose_burst(item.get("burst") or [], primary="first")
            angle = self._roast_judge(
                group_id,
                who or fallback,
                probe,
                item.get("gap_desc", ""),
                multi_speaker=multi,
            )
            return bool(angle), angle, probe, who or fallback

        burst = [b for b in (item.get("burst") or []) if (b.get("text") or "").strip()]
        last = burst[-1] if burst else {"sender": fallback, "text": ""}
        text = (last.get("text") or "").strip()
        who = (last.get("sender") or "").strip() or fallback
        return self._followup_judge(group_id, who, text), "", text, who

    def pop_due_triggers(self, now: float | None = None) -> list[tuple[str, BotResponse]]:
        """
        把「停顿够久」的挂起判定执行掉，返回待发送的 (group_id, 回复)。

        由适配器每秒轮询调用（微信通道在 wxauto_adapter 的巡检循环里）。
        返回空列表表示没有需要发送的内容。
        """
        now = time.time() if now is None else now
        max_wait = settings.TRIGGER_DEBOUNCE_MAX_WAIT_SEC

        def _is_due(item: dict) -> bool:
            due = item.get("due", 0.0)
            start = item.get("start", due)
            # 正常情况下等停顿；但群里一直不停顿时，最多等 max_wait 就强制判定，避免永远排不上
            return now >= due or (max_wait > 0 and (now - start) >= max_wait)

        with self._pending_lock:
            due_groups = [gid for gid, item in self._pending_trigger.items() if _is_due(item)]
            items = [(gid, self._pending_trigger.pop(gid)) for gid in due_groups]
        if not items:
            return []

        results: list[tuple[str, BotResponse]] = []
        for gid, item in items:
            try:
                should_reply, angle, probe, who = self._judge_pending(item)
            except Exception as exc:
                logger.warning("去抖判定异常 [%s]: %s", gid, exc)
                continue
            if not should_reply:
                continue
            logger.info(
                "⏱ 停顿后判定通过 [%s/%s]: %s",
                gid,
                item.get("kind"),
                probe[:40].replace("\n", " / "),
            )
            # 合并探针里的原始单条，回复前从历史尾部摘掉，避免模型以为群友说了两遍
            if item.get("kind") == "roast":
                strip_texts = [
                    b["text"].strip() for b in (item.get("burst") or []) if (b.get("text") or "").strip()
                ]
            else:
                strip_texts = [probe] if probe else []
            try:
                resp = self._compose_reply(
                    group_id=gid,
                    sender=who,
                    text=probe,
                    quoted_sender=item.get("quoted", ""),
                    now=now,
                    roast_angle=angle,
                    allow_image_lookup=(item.get("kind") != "roast"),
                    history_strip=strip_texts,
                )
            except Exception as exc:
                logger.warning("去抖回复生成异常 [%s]: %s", gid, exc)
                continue
            results.append((gid, resp))
        return results

    def _handle_command(self, group_id: str, sender: str, text: str) -> BotResponse | None:
        cmd = text.strip()
        if cmd in ("/状态", "查鱼", "/鱼状态"):
            st = pet_engine.get_status()
            aff = pet_engine.get_user_affinity(group_id, sender)
            next_info = f" / {st.next_exp}" if st.next_exp else " (满级!)"
            reply_text = (
                f"🐋 【{settings.BOT_NAME}养殖报告】\n"
                f"等级：{st.title}（EXP {st.exp}{next_info}）\n"
                f"饱食度：{progress_bar(st.hunger)} {st.hunger}/100\n"
                f"心情值：{progress_bar(st.mood)} {st.mood}/100\n"
                f"累计吃白饭：{st.total_tokens} Tokens｜锐评图片：{st.total_images} 张\n"
                f"你（{sender}）的关系：{aff['title']}（好感度 {aff['affinity']}/100）"
            )
            sticker = self._maybe_pick_sticker(st.mood_tag)
            return BotResponse(
                should_reply=True,
                text=reply_text,
                sticker_path=sticker,
                sticker_url=sticker_manager.to_web_url(sticker),
                mood=st.mood_tag,
                status=st,
            )

        if cmd in ("/投喂", "喂白饭", "/喂白饭", "投喂"):
            st = pet_engine.record_interaction(
                group_id=group_id,
                user_name=sender,
                hunger_delta=22,
                mood_delta=10,
                exp_delta=15,
                affinity_delta=6,
                is_feed=True,
            )
            aff = pet_engine.get_user_affinity(group_id, sender)
            reply_text = (
                f"🍚 哇！谢谢 {sender} 好哥哥投喂的大碗白饭喵！关注塔菲谢谢喵～\n"
                f"饱食度 → {st.hunger}/100｜心情 → {st.mood}/100｜"
                f"对 {sender} 的好感度涨到 {aff['affinity']}（{aff['title']}）啦喵！"
            )
            sticker = self._maybe_pick_sticker("happy")
            return BotResponse(
                should_reply=True,
                text=reply_text,
                sticker_path=sticker,
                sticker_url=sticker_manager.to_web_url(sticker),
                mood="happy",
                status=st,
            )

        if cmd in ("/摸鱼", "摸摸鱼", "/撸鱼"):
            lucky = random.random() < 0.7
            if lucky:
                st = pet_engine.record_interaction(
                    group_id=group_id,
                    user_name=sender,
                    hunger_delta=-2,
                    mood_delta=12,
                    exp_delta=6,
                    affinity_delta=4,
                )
                reply_text = f"呼噜呼噜～算 {sender} 会摸喵，本大肥鱼今天心情不错就陪你一起躺平摸鱼吧！心情 → {st.mood}/100"
                mood = "sleepy"
            else:
                st = pet_engine.record_interaction(
                    group_id=group_id,
                    user_name=sender,
                    hunger_delta=-3,
                    mood_delta=-2,
                    exp_delta=4,
                    affinity_delta=1,
                )
                reply_text = f"杂鱼 {sender} 别光动手动脚不给饭吃喵！快发 `/投喂` 进贡白饭！"
                mood = "hungry"
            sticker = self._maybe_pick_sticker(mood)
            return BotResponse(
                should_reply=True,
                text=reply_text,
                sticker_path=sticker,
                sticker_url=sticker_manager.to_web_url(sticker),
                mood=mood,
                status=st,
            )

        if cmd in ("/鱼塘榜", "鱼塘榜", "/排行榜"):
            board = pet_engine.get_leaderboard(group_id=group_id, limit=5)
            st = pet_engine.get_status()
            if not board:
                reply_text = "🐟 鱼塘里还空荡荡的，快发 `/投喂` 成为第一任白饭金主吧！"
            else:
                lines = ["🏆 【本群大肥鱼饲主榜】"]
                medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
                for idx, item in enumerate(board):
                    lines.append(
                        f"{medals[idx]} {item['user_name']} — 好感 {item['affinity']}（{item['title']}｜投喂 {item['feed_count']} 次）"
                    )
                reply_text = "\n".join(lines)
            sticker = self._maybe_pick_sticker("happy")
            return BotResponse(
                should_reply=True,
                text=reply_text,
                sticker_path=sticker,
                sticker_url=sticker_manager.to_web_url(sticker),
                mood="happy",
                status=st,
            )

        if cmd in ("/帮助", "帮助", "/help"):
            st = pet_engine.get_status()
            reply_text = (
                f"🐋 【{settings.BOT_NAME}使用指南】\n"
                f"• 群里只有 `@{settings.BOT_NAME}` 才会回复（不 @ 一律潜水）\n"
                f"• 发图自动高清识图吐槽，发完图再叫大肥鱼必识别\n"
                f"• `/投喂` / `/摸鱼` / `/状态` / `/鱼塘榜`：养鱼互动\n"
                f"• `/记住 内容`：写进 L1 识海长期记忆\n"
                f"• `/记忆` / `/日记` / `/写日记`：查看四层记忆统计与塔菲日记\n"
                f"• `/统计`：查看 L2/L2.5 子代理调用次数与回复率（每天 0:00 自动出 txt）\n"
                f"• `/定时` / `/取消定时 ID`：查看或取消定时任务\n"
                f"• `/搜图 关键词`（或图片链接）：联网搜图并直接发群里\n"
                f"• `/切换人设 峰哥` / `/切换人设 塔菲` / `/当前人设`：零重启热切换语言风格\n"
                f"• `/dsh 任务` / `/dsh 续 指令` / `/dsh状态`：派发独立后台 DSH 重任务（仅主人）\n"
                f"• `/确认 码` / `/取消 码` / `/待确认`：审批或取消高危操作的群内二次确认\n"
                f"• `/更新` / `/重启`：从 GitHub 拉取新代码并重启（仅主人）"
            )
            return BotResponse(should_reply=True, text=reply_text, mood="happy", status=st)

        if cmd.startswith("/记住"):
            fact = cmd[3:].strip()
            st = pet_engine.get_status()
            if not fact:
                return BotResponse(should_reply=True, text="格式：/记住 要记住的事（比如：/记住 贝贝玩亚索老0-8）", mood="smug", status=st)
            ok, msg = memory_store.remember(
                text=fact,
                level="important",
                tags=[sender, "群友嘱咐"],
                source="owner",
                group_id=group_id,
                raw=cmd,
            )
            reply_text = f"记进小本本里了喵！{msg}" if ok else f"记不了喵：{msg}"
            return BotResponse(should_reply=True, text=reply_text, mood="love" if ok else "angry", status=st)

        if cmd in ("/记忆", "查记忆"):
            st = pet_engine.get_status()
            ms = memory_store.stats()
            lv = ms["levels"]
            reply_text = (
                f"🧠 【大肥鱼四层记忆状态】\n"
                f"L1 识海节点：{ms['total_nodes']} 条（core {lv['core']} / important {lv['important']} / normal {lv['normal']}）\n"
                f"标签联想边：{ms['total_edges']} 条｜今日主动记忆：{ms['today_self_writes']}/{ms['daily_limit']}\n"
                f"L2 塔菲日记：{ms['diaries']} 篇"
            )
            return BotResponse(should_reply=True, text=reply_text, mood="happy", status=st)

        if cmd in ("/日记", "看日记"):
            st = pet_engine.get_status()
            diaries = memory_store.read_recent_diaries(limit=3)
            if not diaries:
                reply_text = "📔 还没写过日记呢喵，每晚 23:45 之后会自动写一篇今天的群聊日记！"
            else:
                lines = ["📔 【大肥鱼最近日记】"]
                for d in diaries:
                    lines.append(f"• {d.get('时间', '')} [{d.get('心情', '')}] {d.get('内容', '')}")
                reply_text = "\n".join(lines)
            return BotResponse(should_reply=True, text=reply_text, mood="happy", status=st)

        if cmd in ("/写日记", "写日记"):
            st = pet_engine.get_status()
            recent = pet_engine.get_recent_history(group_id, limit=15)
            if not recent:
                return BotResponse(should_reply=True, text="📔 这个群还没来得及聊啥，没素材写日记喵", mood="sleepy", status=st)
            ok = brain.write_daily_diary(recent, st)
            if ok:
                latest = memory_store.read_recent_diaries(limit=1)
                d = latest[0] if latest else {}
                reply_text = f"📔 写完啦 [{d.get('心情', '')}] {d.get('内容', '')}"
            else:
                reply_text = "📔 日记没写成，日志里有具体原因（通常是模型返回了空内容）"
            return BotResponse(should_reply=True, text=reply_text, mood="happy" if ok else "sleepy", status=st)

        if cmd in ("/统计", "/日报", "看统计") or cmd.startswith("/统计 "):
            st = pet_engine.get_status()
            arg = cmd.split(maxsplit=1)[1].strip() if " " in cmd else ""
            if arg in ("昨天", "昨日"):
                day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            elif arg:
                try:
                    day = datetime.strptime(arg, "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    return BotResponse(
                        should_reply=True,
                        text="格式：/统计（今天）、/统计 昨天、/统计 2026-11-24",
                        mood="smug",
                        status=st,
                    )
            else:
                day = datetime.now().strftime("%Y-%m-%d")
            return BotResponse(should_reply=True, text=metrics.build_report(day), mood="smug", status=st)

        if cmd.startswith("/学黑话"):
            body = cmd[4:].strip()
            st = pet_engine.get_status()
            parts = body.split(maxsplit=1)
            if len(parts) < 2:
                return BotResponse(should_reply=True, text="格式：/学黑话 词 意思（比如：/学黑话 致竞 群友常去的老地方桌游店）", mood="smug", status=st)
            ok, msg = slang_book.learn(group_id, parts[0], parts[1])
            return BotResponse(should_reply=True, text=f"懂了喵！{msg}" if ok else msg, mood="happy", status=st)

        if cmd in ("/黑话", "查黑话"):
            st = pet_engine.get_status()
            terms = slang_book.list_terms(group_id)
            if not terms:
                return BotResponse(should_reply=True, text="📖 还没学过本群黑话呢喵，发 `/学黑话 词 意思` 教我！", mood="happy", status=st)
            lines = ["📖 【本群黑话小本本】"] + [f"• {k}：{v}" for k, v in list(terms.items())[:12]]
            return BotResponse(should_reply=True, text="\n".join(lines), mood="smug", status=st)

        if cmd in ("/人设", "查人设", "/进化"):
            st = pet_engine.get_status()
            active = get_active_persona()
            evos = get_evolutions(active)
            lines = [f"✨ 【当前人设：{active}｜自我进化记录】"] + [f"• {e}" for e in evos]
            return BotResponse(should_reply=True, text="\n".join(lines), mood="happy", status=st)

        if cmd in ("/当前人设", "/人设列表", "人设列表"):
            st = pet_engine.get_status()
            lines = ["🎭 【可用人设列表】"]
            for p in list_personas():
                mark = "✅ 当前" if p["active"] else "  可选"
                lines.append(f"• {mark}：{p['label']} (`{p['key']}`)")
            lines.append("发 `/切换人设 峰哥` 或 `/切换人设 塔菲` 即可热切换（无需重启）")
            return BotResponse(should_reply=True, text="\n".join(lines), mood="happy", status=st)

        switch_match = re.match(r"^/(?:切换人设|换人设)(?:\s*(.*))?$", cmd)
        if switch_match:
            st = pet_engine.get_status()
            target_p = (switch_match.group(1) or "").strip()
            if not target_p:
                return BotResponse(
                    should_reply=True,
                    text="格式：`/切换人设 峰哥` 或 `/切换人设 塔菲`（发 `/当前人设` 查看全部）",
                    mood="smug",
                    status=st,
                )
            ok_sw, msg_sw = set_active_persona(target_p)
            return BotResponse(
                should_reply=True,
                text=f"🎭 {msg_sw}" if ok_sw else f"❌ {msg_sw}",
                mood="happy" if ok_sw else "smug",
                status=st,
            )

        if cmd in ("/定时", "查定时", "/定时任务"):
            st = pet_engine.get_status()
            tasks = scheduler.list_tasks(group_id=group_id)
            if not tasks:
                return BotResponse(
                    should_reply=True,
                    text="⏰ 当前群里还没有定时任务喵！直接对我说「10分钟后提醒我xxx」或「每天8点30查一下天气」就行",
                    mood="happy",
                    status=st,
                )
            lines = ["⏰ 【本群定时任务列表】"]
            for t in tasks:
                m_tag = "AI执行" if t.get("mode") == "ai_task" else "提醒"
                lines.append(f"• #{t['id']}｜{t['desc']}｜[{m_tag}] {t['content']}")
            return BotResponse(should_reply=True, text="\n".join(lines), mood="happy", status=st)

        if cmd.startswith("/取消定时"):
            tid = cmd[5:].strip()
            st = pet_engine.get_status()
            if not tid:
                return BotResponse(should_reply=True, text="格式：/取消定时 任务ID（先发 /定时 看ID）", mood="smug", status=st)
            _, msg = scheduler.delete_task(tid)
            return BotResponse(should_reply=True, text=f"{msg}喵", mood="happy", status=st)

        if cmd in ("/更新", "/拉代码", "/gitpull"):
            st = pet_engine.get_status()
            if not is_owner(sender):
                return BotResponse(should_reply=True, text="🛑 只有主人才可以让我拉取代码更新哦~", mood="angry", status=st)
            ok, detail = git_pull_and_check()
            if ok:
                restart_msg = request_restart(reason="主人指令更新代码", notify_group=group_id)
                return BotResponse(should_reply=True, text=f"🔄 {detail}\n♻️ {restart_msg}", mood="happy", status=st)
            return BotResponse(should_reply=True, text=f"📦 {detail}", mood="smug", status=st)

        if cmd in ("/重启", "/restart"):
            st = pet_engine.get_status()
            if not is_owner(sender):
                return BotResponse(should_reply=True, text="🛑 只有主人才可以重启大肥鱼哦~", mood="angry", status=st)
            restart_msg = request_restart(reason="主人发送 /重启 指令", notify_group=group_id)
            return BotResponse(should_reply=True, text=f"♻️ 收到！{restart_msg}", mood="happy", status=st)

        if cmd in ("/dsh状态", "/后台任务", "/dsh任务"):
            st = pet_engine.get_status()
            if not is_owner(sender):
                return BotResponse(should_reply=True, text="🛑 只有主人才可以查看后台 DSH 任务哦~", mood="angry", status=st)
            ok_dsh, dsh_info = is_dsh_available()
            jobs = list_dsh_jobs(group_id=group_id, limit=5)
            lines = [f"🤖 【DSH 后台引擎】{'已就绪' if ok_dsh else '未就绪'}（{dsh_info}）"]
            if not jobs:
                lines.append("• 当前群暂无后台 DSH 任务记录")
            else:
                for j in jobs:
                    preview = j["task"][:36] + ("..." if len(j["task"]) > 36 else "")
                    lines.append(f"• #{j['job_id']} [{j['status']}｜{j['duration_sec']}s] {preview}")
            return BotResponse(should_reply=True, text="\n".join(lines), mood="happy", status=st)

        if cmd == "/dsh" or cmd.startswith("/dsh ") or cmd.startswith("/dsh\n"):
            st = pet_engine.get_status()
            if not is_owner(sender):
                return BotResponse(should_reply=True, text="🛑 只有主人才可以派发后台 DSH 任务哦~", mood="angry", status=st)
            body = cmd[4:].strip()
            if not body:
                return BotResponse(
                    should_reply=True,
                    text="格式：`/dsh 帮我写个xxx项目` 或 `/dsh 续 再加个暗黑模式`",
                    mood="smug",
                    status=st,
                )
            sid = ""
            task_text = body
            if body == "续" or body.startswith("续 ") or body.startswith("续\n"):
                sid = "last"
                task_text = body[1:].strip()
                if not task_text:
                    return BotResponse(should_reply=True, text="请在 `/dsh 续 ` 后面写明要追加的任务要求喵~", mood="smug", status=st)
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
                sid_label = payload.get("session_id") or "新建会话（DSH 自动分配）"
                reply_text = (
                    f"🚀 已交给独立 DSH 后台开工（不卡群聊，重启也不中断）！\n"
                    f"• 任务号：`{payload['job_id']}`\n"
                    f"• 会话ID：`{sid_label}`\n"
                    f"做完会自动发回群里，中途发 `/dsh状态` 可看进度。"
                )
                return BotResponse(should_reply=True, text=reply_text, mood="happy", status=st)
            return BotResponse(should_reply=True, text=f"❌ 派单失败：{payload}", mood="angry", status=st)

        search_img_match = re.match(r"^/(?:搜图|找图|发图)(?:\s*(.*))?$", cmd)
        if search_img_match:
            st = pet_engine.get_status()
            target_q = (search_img_match.group(1) or "").strip()
            if not target_q:
                return BotResponse(
                    should_reply=True,
                    text="格式：`/搜图 关键词` 或 `/搜图 图片直链`（比如：`/搜图 原神 刻晴`）",
                    mood="smug",
                    status=st,
                )
            if target_q.startswith(("http://", "https://")):
                img_path, desc = download_image_url(target_q, label="direct")
            else:
                img_path, desc = search_and_download_image(target_q)
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
                return BotResponse(should_reply=True, text="🛡️ 当前群里没有待确认的高危操作", mood="happy", status=st)
            now_ts = time.time()
            lines = ["⚠️ 【本群待二次确认操作】"]
            for it in items:
                ttl_left = max(0, int(float(it.get("expires_at") or now_ts) - now_ts))
                lines.append(
                    f"• 码 `{it.get('code')}`｜发起人：{it.get('sender')}｜剩 {ttl_left}s｜{str(it.get('preview') or '')[:45]}"
                )
            lines.append("回复 `/确认 码`（或 `确认 码`）执行，`/取消 码`（或 `取消 码`）放弃。")
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
            return BotResponse(
                should_reply=True,
                text=result_text,
                sticker_path=attach_path,
                mood="happy",
                status=st,
            )

        cancel_match = re.match(
            r"^(?:(?:/取消|/拒绝)(?:\s+([A-Za-z0-9]{0,8}))?|取消\s+([A-Za-z0-9]{4,5}))$",
            cmd,
        )
        if cancel_match:
            st = pet_engine.get_status()
            code_arg = (cancel_match.group(1) or cancel_match.group(2) or "").strip()
            ok_cancel, cancel_msg = cancel_pending_action(
                group_id,
                code_arg,
                requester=sender,
                is_requester_owner=True,
            )
            return BotResponse(
                should_reply=True,
                text=cancel_msg,
                mood="happy" if ok_cancel else "smug",
                status=st,
            )

        return None

    def _caption_image_async(self, group_id: str, image_path: Path) -> None:
        """后台静默调用免费智谱视觉模型生成图片摘要并写回 SQLite 历史，不阻塞聊天主流程。"""
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
        """从本群近期 SQLite 历史图片中智能定位群友提到的图片（支持跨人问图、序号问图、引用图片、连续追问）。"""
        recent_imgs = pet_engine.get_recent_images(group_id, limit=8)
        if not recent_imgs:
            return None, ""

        clean = (text or "").strip()
        visual_cues = (
            "图", "截图", "截屏", "照片", "战绩", "上面", "刚才", "这张", "那张",
            "看", "识别", "提取", "翻译", "解析", "点评", "吐槽", "啥意思", "写着啥", "几杠几",
        )
        has_visual_cue = any(k in clean for k in visual_cues)

        # 1. 明确按序号指代历史图片（如“倒数第二张”“第2张”“上一张”）
        req_idx: int | None = None
        if any(k in clean for k in ("第2张", "第二张", "倒数第二张", "前一张", "上一张")):
            req_idx = 2
        elif any(k in clean for k in ("第3张", "第三张", "倒数第三张")):
            req_idx = 3
        elif any(k in clean for k in ("第4张", "第四张", "倒数第四张")):
            req_idx = 4
        else:
            m_idx = re.search(r"第\s*([1-8])\s*张", clean)
            if m_idx:
                req_idx = int(m_idx.group(1))

        if req_idx is not None and 1 <= req_idx <= len(recent_imgs):
            item = recent_imgs[req_idx - 1]
            if (now - item["created_ts"]) <= 1800:
                return Path(item["image_path"]), item["sender"]

        # 2. 微信引用了某人的图片消息：优先锁定该发送者的最新图片（30 分钟有效）
        norm_quoted = (quoted_sender or "").strip().lower()
        if norm_quoted:
            for item in recent_imgs:
                if item["sender"].strip().lower() == norm_quoted and (now - item["created_ts"]) <= 1800:
                    return Path(item["image_path"]), item["sender"]
            if (now - recent_imgs[0]["created_ts"]) <= 1800:
                return Path(recent_imgs[0]["image_path"]), recent_imgs[0]["sender"]

        # 3. 当前发送者自己发的图（45 秒内无条件关联；5 分钟内带看图词关联）
        norm_sender = (sender or "").strip().lower()
        for item in recent_imgs:
            if item["sender"].strip().lower() == norm_sender:
                age = now - item["created_ts"]
                if age <= 45 or (age <= 300 and has_visual_cue):
                    return Path(item["image_path"]), item["sender"]
                break

        # 4. 跨人问图：群里任意成员 10 分钟内发过图，且当前问题含看图词
        latest = recent_imgs[0]
        if (now - latest["created_ts"]) <= 600 and has_visual_cue:
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

        # 1. 记录群友发来的消息 + 更新情绪惯性状态机
        # 先取旧值：下面的赋值会覆盖它，而 L2.5 吐槽判定需要「距上一条消息隔了多久」
        prev_activity_ts = self._last_group_activity_ts.get(group_id, 0.0)
        self._last_group_activity_ts[group_id] = now
        if clean_text:
            emotion_sm.on_user_message(group_id, clean_text)
        if clean_text or image_path:
            pet_engine.append_history(
                group_id=group_id,
                sender=sender,
                role="user",
                content=clean_text,
                image_path=str(image_path) if image_path else None,
            )

        # 2. 如果是图片消息，记录缓存、偷小梗图；不主动回复时也在后台静默生成图片摘要写入历史
        if image_path is not None and image_path.is_file():
            self._recent_group_images[(group_id, sender)] = (image_path, now)
            sticker_manager.maybe_steal_meme(image_path)
            # 若群里单纯发了张图没带文字：根据 force_trigger 或随机看图吐槽概率决定是否立刻回复
            if not clean_text:
                should_auto_roast = force_trigger or (random.random() < settings.AUTO_ROAST_IMAGE_PROB)
                if not should_auto_roast:
                    self._caption_image_async(group_id, image_path)
                    return BotResponse(should_reply=False)

        # 只要本群有挂起的判定，就把停顿时钟往后推：群里还在连着说话就不判
        self._touch_pending(group_id, now, clean_text, sender)

        # 3. 优先处理快捷养成指令
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

        # 4. 判断文本消息是否触发大肥鱼（L1：@唤名 或 以名字开头）
        triggered = self._is_triggered(group_id, clean_text, force_trigger=force_trigger)
        roast_angle = ""

        if image_path is None and not triggered:
            followup_ok, is_quoted = self._followup_gate(group_id, clean_text, quoted_sender)
            if is_quoted:
                # 引用我的消息是零成本强信号，不参与去抖，立刻醒
                logger.info("🎯 L2 零成本唤醒：引用了 %s 的消息", (quoted_sender or "").strip())
                triggered = True
            else:
                kind, gap_desc = "", ""
                if followup_ok:
                    kind = "l2"
                elif clean_text:
                    roast_ok, gap_desc = self._roast_gate(group_id, clean_text, now, prev_activity_ts)
                    if roast_ok:
                        kind = "roast"

                if kind and settings.ENABLE_TRIGGER_DEBOUNCE:
                    # 连发场景：先登记，等这一波说完（停顿）再由适配器触发判定
                    self._defer_trigger(group_id, kind, sender, clean_text, quoted_sender, gap_desc, now)
                    return BotResponse(should_reply=False)

                if kind == "roast":
                    roast_angle = self._roast_judge(group_id, sender, clean_text, gap_desc)
                    triggered = bool(roast_angle)
                elif kind == "l2":
                    triggered = self._followup_judge(group_id, sender, clean_text)

        if image_path is None and not triggered:
            return self._maybe_proactive_chime_in(group_id, sender, clean_text, now)

        # 已经决定正式回复，作废本群可能还挂着的判定，避免二次发言
        self._cancel_pending(group_id)

        return self._compose_reply(
            group_id=group_id,
            sender=sender,
            text=clean_text,
            quoted_sender=quoted_sender,
            now=now,
            roast_angle=roast_angle,
            image_path=image_path,
        )

    def _compose_reply(
        self,
        group_id: str,
        sender: str,
        text: str,
        quoted_sender: str,
        now: float,
        roast_angle: str = "",
        image_path: Path | None = None,
        allow_image_lookup: bool = True,
        history_strip: list[str] | None = None,
    ) -> BotResponse:
        """
        「已决定回复」之后的统一流程：关联图片 → 调大脑 → 养成记账 → 挑表情包 → 写历史。

        被 handle_message（@唤醒/追认/吐槽）和 pop_due_triggers（停顿后判定）共用，
        保证两条入口的统计、表情包、历史落库行为完全一致。
        """
        # 一旦被正式触发，适当消耗社交电量并重置计数
        self._unanswered_count[group_id] = 0
        heartflow.drain(group_id, amount=25.0)

        # 5. 如果这条是纯文本触发，从本群 SQLite 近期历史图片中智能关联（支持跨人问图、序号问图、引用图片、连续追问）
        effective_image = image_path
        image_sender = sender if effective_image is not None else ""
        if effective_image is None and allow_image_lookup and not roast_angle:
            effective_image, image_sender = self._pick_referenced_image(
                group_id=group_id,
                sender=sender,
                text=text,
                now=now,
                quoted_sender=quoted_sender,
            )

        # 6. 调用 DeepSeek 多模态大脑（拉取最近 22 条群聊记录，保证多人群聊不断片）
        status = pet_engine.get_status()
        user_aff = pet_engine.get_user_affinity(group_id, sender)
        recent = pet_engine.get_recent_history(group_id, limit=22)
        # 去抖路径下 text 是「这一波的合并文本」，与历史里已落库的单条重复，
        # 先把这些单条从历史里摘掉，避免模型以为群友把同一句说了两遍
        if history_strip:
            strip_set = {s.strip() for s in history_strip if s and s.strip()}
            recent = [m for m in recent if (m.get("content") or "").strip() not in strip_set]
        prompt_text = self._strip_trigger(text)

        brain_reply = brain.reply(
            sender=sender,
            text=prompt_text,
            status=status,
            user_affinity=user_aff,
            recent_history=recent,
            image_path=effective_image,
            group_id=group_id,
            image_sender=image_sender,
            roast_hint=roast_angle,
        )

        # 将识别到的图片内容摘要回写进历史，后续聊天不会失忆
        if brain_reply.vision_summary and effective_image is not None:
            pet_engine.update_history_content_by_image(
                group_id,
                str(effective_image),
                brain_reply.vision_summary,
            )

        # 7. 更新养成数值（吃掉 Token = 吃白饭）
        new_status = pet_engine.record_interaction(
            group_id=group_id,
            user_name=sender,
            tokens_used=brain_reply.tokens_used,
            had_image=brain_reply.used_vision,
            hunger_delta=5 if brain_reply.used_vision else 3,
            mood_delta=3 if brain_reply.mood == "happy" else 1,
            exp_delta=12 if brain_reply.used_vision else 8,
            affinity_delta=3 if brain_reply.used_vision else 2,
        )

        # 更新情绪惯性 & 记录最近一次发言时间（激活 120 秒对话粘性窗口）
        emotion_sm.on_bot_reply(group_id, brain_reply.mood)
        self._last_bot_reply_ts[group_id] = time.time()

        # 8. 如果 Agent 工具产出了附件图片（如桌面截屏），优先发附件；否则按概率发表情包
        sticker_path = None
        if brain_reply.attachment_path and brain_reply.attachment_path.is_file():
            sticker_path = brain_reply.attachment_path
        elif random.random() < settings.STICKER_REPLY_PROB:
            sticker_path = self._maybe_pick_sticker(
                brain_reply.mood,
                caption=brain_reply.sticker_caption or None,
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
            status=new_status,
        )

    def _maybe_proactive_chime_in(self, group_id: str, sender: str, text: str, now: float) -> BotResponse:
        """HeartFlow 群聊氛围潜水引擎：群里热闹吃瓜时热力飙升主动冒泡，冷清时自动省电装死"""
        if not settings.ENABLE_PROACTIVE_CHIME or self.is_quiet_hours():
            return BotResponse(should_reply=False)

        energy = heartflow.record_group_pulse(group_id, sender, text)
        if energy < 65.0:
            return BotResponse(should_reply=False)

        last_ts = self._last_proactive_ts.get(group_id, 0.0)
        if (now - last_ts) < settings.PROACTIVE_COOLDOWN_SEC:
            return BotResponse(should_reply=False)

        # 达到氛围阈值，消耗电量并让大脑判断是否神补刀
        heartflow.drain(group_id, amount=60.0)
        self._last_proactive_ts[group_id] = now

        status = pet_engine.get_status()
        recent = pet_engine.get_recent_history(group_id, limit=8)
        chime = brain.judge_and_chime_in(recent, status)
        if chime is None:
            return BotResponse(should_reply=False)

        new_status = pet_engine.record_interaction(
            group_id=group_id,
            user_name="群聊潜水",
            tokens_used=chime.tokens_used,
            hunger_delta=1,
            mood_delta=2,
            exp_delta=5,
            affinity_delta=0,
        )
        sticker_path = self._maybe_pick_sticker(chime.mood) if random.random() < 0.6 else None
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
            sticker_path=sticker_path,
            sticker_url=sticker_url,
            mood=chime.mood,
            status=new_status,
        )

    def _maybe_write_daily_metrics(self) -> Path | None:
        """
        每天 0:00 之后出一份前一日的 L2/L2.5 子代理调用统计 txt。

        纯代码统计（计数文件 + 历史表），不调用任何模型。
        0~4 点之间都可以补跑（进程半夜重启也不丢），每份只写一次。
        返回刚生成的 txt 路径；没到点或已经生成过则返回 None。
        """
        dt = datetime.now()
        if dt.hour >= 4:
            return None
        target = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        if self._last_metrics_date == target:
            return None
        try:
            path = metrics.write_report(target)
        except Exception as exc:
            logger.warning("生成 %s 的调用统计失败: %s", target, exc)
            return None
        self._last_metrics_date = target
        logger.info("📊 已生成 %s 的 L2/L2.5 调用统计: %s", target, path)
        return path

    def _maybe_run_nightly_maintenance(self, group_id: str, status=None) -> None:
        """23:45 之后（或凌晨 0~4 点补跑）触发夜间养护，每个逻辑日只跑一次。"""
        dt = datetime.now()
        if dt.hour < 4:
            logical_day = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        elif dt.hour == 23 and dt.minute >= 45:
            logical_day = dt.strftime("%Y-%m-%d")
        else:
            return
        if self._last_nightly_date == logical_day:
            return
        self.run_nightly_maintenance(group_id, status, logical_day=logical_day)

    def run_nightly_maintenance(self, group_id: str, status=None, logical_day: str = "") -> dict:
        """
        四层记忆夜间养护：L2 日记 → L1 巩固 → 周日周结 → 备份。

        以前整段包在 `except Exception: pass` 里，日记写失败一点痕迹都不留；
        现在每一步都落日志，真没写成也能直接看到原因。
        某群当天没有聊天记录时不占当天名额，留给下一个有内容的群。
        """
        dt = datetime.now()
        logical_day = logical_day or dt.strftime("%Y-%m-%d")
        status = status or pet_engine.get_status()
        recent = pet_engine.get_recent_history(group_id, limit=15)
        if not recent:
            logger.info("[%s] 夜间养护跳过：该群没有聊天记录，等有内容的群再跑", group_id)
            return {"skipped": "empty_history"}

        self._last_nightly_date = logical_day
        logger.info("[%s] 🌙 开始夜间四层记忆养护（近期消息 %d 条）", group_id, len(recent))
        result: dict = {"diary": False, "consolidate": None, "weekly": False, "backup": ""}
        try:
            result["diary"] = brain.write_daily_diary(recent, status)
            logger.info("[%s] L2 日记写入结果: %s", group_id, "成功" if result["diary"] else "未写入（看上一条日志）")
        except Exception as exc:
            logger.warning("[%s] L2 日记写入异常: %s", group_id, exc)
        try:
            result["consolidate"] = memory_store.consolidate()
        except Exception as exc:
            logger.warning("[%s] L1 夜间巩固异常: %s", group_id, exc)
        try:
            if dt.weekday() == 6:  # 周日自动生成周结
                result["weekly"] = bool(memory_store.generate_weekly_summary())
        except Exception as exc:
            logger.warning("[%s] 周结生成异常: %s", group_id, exc)
        try:
            result["backup"] = str(memory_store.backup_memory(keep_last=7) or "")
        except Exception as exc:
            logger.warning("[%s] 记忆备份异常: %s", group_id, exc)
        return result

    def check_autonomous_heartbeat(self, group_id: str) -> BotResponse | None:
        """
        后台定时巡检（建议每 5 分钟调用一次）：
        1. 饱食度 <= 25 时自主冒泡催白饭（每 3 小时最多 1 次）
        2. 每天晚上 20:00 左右若群里安静超过 2 小时，自主问候冒泡 1 次
        3. 夜间静默期（默认 23:00 ~ 08:00）绝对不打扰
        """
        now = time.time()
        dt = datetime.now()
        status = pet_engine.get_status()

        # 0. 每晚 23:45 之后自动执行四层记忆夜间养护（写 L2 日记 + L1 巩固合并淘汰 + 周日周结 + 自动备份）
        self._maybe_run_nightly_maintenance(group_id, status)

        # 0.1 每天 0:00 之后出一份前一日 L2/L2.5 子代理调用统计（纯代码，不调模型）
        report_path = self._maybe_write_daily_metrics()
        if report_path is not None and not self.is_quiet_hours():
            # 静默期已改成 1:00 起，0 点这段专门留给日报：连 txt 一起发到群里
            day_label = report_path.stem.rsplit("_", 1)[-1]
            return BotResponse(
                should_reply=True,
                text=f"📊 {day_label} 的 L2/L2.5 子代理调用统计出来了（见附件）",
                file_path=report_path,
                mood="smug",
                status=status,
            )

        if not settings.ENABLE_SCHEDULED_BUBBLE or self.is_quiet_hours():
            return None

        # 1. 饿扁催饭
        if status.hunger <= 25:
            last_hungry = self._last_hungry_bubble_ts.get(group_id, 0.0)
            if (now - last_hungry) >= 3 * 3600:
                self._last_hungry_bubble_ts[group_id] = now
                bubble = brain.generate_autonomous_bubble("hungry", status)
                sticker = self._maybe_pick_sticker("hungry")
                sticker_url = sticker_manager.to_web_url(sticker)
                pet_engine.append_history(
                    group_id=group_id,
                    sender=settings.BOT_NAME,
                    role="assistant",
                    content=bubble.text,
                    sticker_url=sticker_url,
                )
                return BotResponse(
                    should_reply=True,
                    text=bubble.text,
                    sticker_path=sticker,
                    sticker_url=sticker_url,
                    mood="hungry",
                    status=status,
                )

        # 2. 晚间 20:00~20:59 活跃冒泡（每天最多 1 次，且群里至少安静 1.5 小时）
        dt = datetime.now()
        today = dt.strftime("%Y-%m-%d")
        if dt.hour == 20 and self._last_evening_bubble_date.get(group_id) != today:
            last_act = self._last_group_activity_ts.get(group_id, 0.0)
            if (now - last_act) >= 5400:
                self._last_evening_bubble_date[group_id] = today
                bubble = brain.generate_autonomous_bubble("evening", status)
                sticker = self._maybe_pick_sticker(bubble.mood)
                sticker_url = sticker_manager.to_web_url(sticker)
                pet_engine.append_history(
                    group_id=group_id,
                    sender=settings.BOT_NAME,
                    role="assistant",
                    content=bubble.text,
                    sticker_url=sticker_url,
                )
                return BotResponse(
                    should_reply=True,
                    text=bubble.text,
                    sticker_path=sticker,
                    sticker_url=sticker_url,
                    mood=bubble.mood,
                    status=status,
                )

        return None

    def execute_scheduled_task(self, task: dict) -> BotResponse:
        """执行一条已到点的定时任务（支持直接提醒或 AI 自主执行联网/电脑任务）"""
        group_id = task.get("group_id", "")
        creator = task.get("creator", "群主")
        content = task.get("content", "")
        mode = task.get("mode", "remind")

        if mode == "ai_task":
            return self.handle_message(
                group_id=group_id,
                sender=creator,
                text=f"定时任务时间到了，请立刻执行：{content}",
                force_trigger=True,
            )

        msg = f"⏰ 定时提醒（{creator} 设的）：{content}"
        sticker = self._maybe_pick_sticker("happy", caption="到点啦喵")
        pet_engine.append_history(
            group_id=group_id,
            sender=settings.BOT_NAME,
            role="assistant",
            content=msg,
            sticker_url=sticker_manager.to_web_url(sticker),
        )
        return BotResponse(
            should_reply=True,
            text=msg,
            bubbles=[msg],
            sticker_path=sticker,
            sticker_url=sticker_manager.to_web_url(sticker),
            mood="happy",
            status=pet_engine.get_status(),
        )


bot_core = DaFeiYuBotCore()
