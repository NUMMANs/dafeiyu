import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
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


# 即使没叫名字，也可能是在议论大肥鱼或延续对话的指代词
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
        self._last_hungry_bubble_ts: dict[str, float] = {}
        self._last_evening_bubble_date: dict[str, str] = {}
        self._last_nightly_date: str = ""

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
        low = text.lower()
        return any(kw in low for kw in LOL_KEYWORDS)

    def _is_answerable_question(self, group_id: str, text: str) -> bool:
        """
        识别群友提出的「可解答疑问」（游戏评价、软件/报错、知识科普、选购推荐等），
        同时过滤掉纯“？”或熟人约时间/买票等私人行程，像贴心群助理一样只答该答的。
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

    def _is_triggered(self, group_id: str, text: str, force_trigger: bool = False) -> bool:
        if force_trigger or not settings.GROUP_AT_ONLY:
            return True
        lower = text.strip()
        if lower.startswith("/") or lower in ("查鱼", "喂白饭", "摸摸鱼", "鱼塘榜", "帮助"):
            return True
        # 英雄联盟相关提问/话题 100% 必回复
        if self._is_lol_related(lower):
            return True
        # 叫到大肥鱼名字触发
        if any(kw in lower for kw in settings.TRIGGER_KEYWORDS):
            return True
        # 群友有可解答疑问时，像助理一样主动回答
        return self._is_answerable_question(group_id, lower)

    def _strip_trigger(self, text: str) -> str:
        cleaned = text
        for kw in settings.TRIGGER_KEYWORDS:
            cleaned = cleaned.replace(kw, "")
        return cleaned.strip(" ,，:：!！@")

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
                f"• 群里直接叫 `大肥鱼`、问英雄联盟或提疑问都会自动回复\n"
                f"• 发图自动高清识图吐槽，发完图再叫大肥鱼必识别\n"
                f"• `/投喂` / `/摸鱼` / `/状态` / `/鱼塘榜`：养鱼互动\n"
                f"• `/记住 内容`：写进 L1 识海长期记忆\n"
                f"• `/记忆` / `/日记`：查看四层记忆统计与塔菲日记\n"
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
                reply_text = "📔 还没写过日记呢喵，每晚 23:50 会自动写一篇今天的群聊日记！"
            else:
                lines = ["📔 【大肥鱼最近日记】"]
                for d in diaries:
                    lines.append(f"• {d.get('时间', '')} [{d.get('心情', '')}] {d.get('内容', '')}")
                reply_text = "\n".join(lines)
            return BotResponse(should_reply=True, text=reply_text, mood="happy", status=st)

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

        # 3. 优先处理快捷养成指令
        if clean_text:
            cmd_resp = self._handle_command(group_id, sender, clean_text)
            if cmd_resp is not None:
                pet_engine.append_history(
                    group_id=group_id,
                    sender=settings.BOT_NAME,
                    role="assistant",
                    content=cmd_resp.text,
                    sticker_url=cmd_resp.sticker_url,
                )
                return cmd_resp

        # 4. 判断文本消息是否触发大肥鱼；
        # 若未直接命中关键词，先检查「对话粘性窗口(120秒内刚聊过)」或「群友在议论他」
        triggered = self._is_triggered(group_id, clean_text, force_trigger=force_trigger)
        if image_path is None and not triggered:
            in_active_window = (now - self._last_bot_reply_ts.get(group_id, 0.0)) <= 120
            mentions_pronoun = any(c in clean_text.lower() for c in ABOUT_ME_CUES)
            if (in_active_window and len(clean_text) >= 2) or (mentions_pronoun and len(clean_text) >= 4):
                recent_ctx = pet_engine.get_recent_history(group_id, limit=6)
                if brain.is_talking_to_or_about_me(sender, clean_text, recent_ctx):
                    triggered = True

        if image_path is None and not triggered:
            return self._maybe_proactive_chime_in(group_id, sender, clean_text, now)

        # 一旦被正式触发，适当消耗社交电量并重置计数
        self._unanswered_count[group_id] = 0
        heartflow.drain(group_id, amount=25.0)

        # 5. 如果这条是纯文本触发，从本群 SQLite 近期历史图片中智能关联（支持跨人问图、序号问图、引用图片、连续追问）
        effective_image = image_path
        image_sender = sender if effective_image is not None else ""
        if effective_image is None:
            effective_image, image_sender = self._pick_referenced_image(
                group_id=group_id,
                sender=sender,
                text=clean_text,
                now=now,
                quoted_sender=quoted_sender,
            )

        # 6. 调用 DeepSeek 多模态大脑（拉取最近 22 条群聊记录，保证多人群聊不断片）
        status = pet_engine.get_status()
        user_aff = pet_engine.get_user_affinity(group_id, sender)
        recent = pet_engine.get_recent_history(group_id, limit=22)
        prompt_text = self._strip_trigger(clean_text)

        brain_reply = brain.reply(
            sender=sender,
            text=prompt_text,
            status=status,
            user_affinity=user_aff,
            recent_history=recent,
            image_path=effective_image,
            group_id=group_id,
            image_sender=image_sender,
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

    def check_autonomous_heartbeat(self, group_id: str) -> BotResponse | None:
        """
        后台定时巡检（建议每 5 分钟调用一次）：
        1. 饱食度 <= 25 时自主冒泡催白饭（每 3 小时最多 1 次）
        2. 每天晚上 20:00 左右若群里安静超过 2 小时，自主问候冒泡 1 次
        3. 夜间静默期（默认 23:00 ~ 08:00）绝对不打扰
        """
        now = time.time()
        dt = datetime.now()
        today = dt.strftime("%Y-%m-%d")
        status = pet_engine.get_status()

        # 0. 每晚 23:50 自动执行四层记忆夜间养护（写 L2 日记 + L1 巩固合并淘汰 + 周日周结 + 自动备份）
        if dt.hour == 23 and dt.minute >= 45 and self._last_nightly_date != today:
            self._last_nightly_date = today
            try:
                recent = pet_engine.get_recent_history(group_id, limit=15)
                brain.write_daily_diary(recent, status)
                memory_store.consolidate()
                if dt.weekday() == 6:  # 周日自动生成周结
                    memory_store.generate_weekly_summary()
                memory_store.backup_memory(keep_last=7)
            except Exception:
                pass

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
