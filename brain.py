import base64
import json
import logging
import mimetypes
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from agent_tools import (
    AGENT_TOOL_SCHEMAS,
    create_and_send_file,
    fetch_webpage,
    get_system_info,
    is_owner,
    list_directory,
    open_url_or_app,
    read_local_file,
    run_shell,
    send_existing_file,
    take_screenshot,
    write_local_file,
)
from config import settings
from dsh_bridge import list_dsh_jobs, submit_dsh_job
from image_fetcher import download_image_url, search_and_download_image
from memory import memory_store
from persona_rag import retrieve_style_hints
from persona_runtime import emotion_sm, slang_book
from pet_engine import PetStatus, pet_engine
from roles.build_card import evolve_persona, get_active_persona, load_card
from safety_guard import assess_danger, stage_pending_action
from scheduler import scheduler
from self_ops import git_pull_and_check, request_restart, self_edit_code
from web_search import web_search

logger = logging.getLogger("dafeiyu.brain")

MOOD_TAG_PATTERN = re.compile(
    r"\[(?:MOOD|NM|情绪)\s*[:：]\s*(happy|smug|hungry|shocked|angry|sleepy|gaming|love)(?:\s*[|｜]\s*([^\]]{1,14}))?\]",
    re.IGNORECASE,
)

# 命中这些实时/资料类词时，直接前置秒搜并合并进单轮 DeepSeek 调用（省掉一轮 tool_call 往返，速度翻倍）
PRE_SEARCH_CUES = (
    "最新", "今天", "昨天", "最近", "新闻", "热搜", "天气", "价格", "多少钱",
    "什么时候上", "上线", "公测", "版本", "更新", "攻略", "出装", "评测",
    "好玩吗", "有必要玩", "搜一下", "查一下", "上网查", "百度", "官网", "报错",
    "夺冠", "冠军", "决赛", "比赛", "比分", "s16", "msi", "世界赛",
)

CODE_OR_FILE_CUES = (
    ".html", ".py", ".js", ".css", ".md", ".txt", ".csv", ".json",
    "html", "网页", "脚本", "代码", "写个文件", "写一个文件",
    "发个文件", "发文件", "保存成", "生成文件",
)

SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索最新信息。当群友问到最新新闻、实时天气、新游戏/版本攻略、近期热梗、"
                "具体报错解决方案或你不确定的实时事实时调用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "简洁精准的中文搜索关键词",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": (
                "把一条值得长期记住的群友特征、约定、黑历史或群梗写入 L1 识海记忆库。"
                "只有真正值得记住的事才调用（不要记录普通闲聊，严禁记录密码/身份证/银行卡）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "简短清晰的事实描述（≤120字）"},
                    "level": {
                        "type": "string",
                        "enum": ["normal", "important"],
                        "description": "普通梗选 normal（7天半衰期），重要约定/人物特征选 important（30天半衰期）",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2~4 个关键词标签（如群友昵称、游戏名、话题名）用于图扩散联想",
                    },
                },
                "required": ["text", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "learn_slang",
            "description": (
                "记录群聊专属黑话、外号或内部梗（比如某个地名/游戏道具/群友外号的含义），"
                "以后群里再提到这个词时你能秒懂。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "黑话词或外号（如：致竞、皮箱、海斗）"},
                    "meaning": {"type": "string", "description": "这个词在群里的真实含义"},
                },
                "required": ["term", "meaning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evolve_persona",
            "description": (
                "自我升级/微调自己的人设性格！当你和群友相处久了，觉得自己长出了新习惯、新喜好、"
                "新口癖或新态度（或者群友建议你改某个风格且你觉得有意思）时调用，会永久写进你的人格卡。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "第一人称写一句简短的自我进化记录（如：我发现我越来越爱看群友排位破防了，以后遇到连跪要多调侃两句）",
                    }
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_schedule",
            "description": (
                "创建一个定时任务（持久化保存，重启不丢）。支持：一次性提醒（如10分钟后、今天22:00）、"
                "每日定时（如每天08:30）、固定间隔循环（每N分钟）。"
                "如果只是普通喊话/提醒用 mode='remind'；如果到点需要你去联网查天气/查比赛/截屏/执行命令，用 mode='ai_task'！"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_type": {"type": "string", "enum": ["once", "daily", "interval"]},
                    "mode": {"type": "string", "enum": ["remind", "ai_task"], "description": "remind=到点直接发提醒文字；ai_task=到点触发AI自动执行任务"},
                    "content": {"type": "string", "description": "提醒内容或要AI执行的指令（如：查一下今天北京天气并播报）"},
                    "delay_minutes": {"type": "integer", "description": "task_type=once 时：多少分钟后执行（如 10）"},
                    "run_at": {"type": "string", "description": "task_type=once 时：具体时间 YYYY-MM-DD HH:MM 或 HH:MM"},
                    "daily_time": {"type": "string", "description": "task_type=daily 时：每日执行时间 HH:MM（如 08:30）"},
                    "interval_minutes": {"type": "integer", "description": "task_type=interval 时：间隔分钟数（>=5）"},
                },
                "required": ["task_type", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_schedules",
            "description": "查看当前群里已经设置的所有定时任务和任务 ID",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_schedule",
            "description": "根据任务 ID 取消/删除某个定时任务",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "定时任务 6 位 ID"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_group_image",
            "description": (
                "重新查看本群历史聊天记录里的某张图片细节。当群友问「上面那张图」「刚才截图」「第二张图」"
                "「之前发的战绩图」「帮我看看群里那张照片」等需要看具体画面或文字时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "倒数第几张图（1=最新一张，2=倒数第二张，范围 1~8，默认 1）",
                    },
                    "question": {
                        "type": "string",
                        "description": "重点识别或确认的图片问题/细节（可选）",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_and_send_image",
            "description": (
                "按关键词联网搜索并下载一张清晰图片，直接作为图片发到当前群里。"
                "只要群友让你找图、搜图、下载某角色/人物/风景/猫猫/壁纸/梗图发群里（不限游戏或题材），必须直接调用此工具，不要拒绝。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "精确简洁的搜图关键词（例如：'原神 刻晴 高清壁纸'、'银渐层猫 表情包'）",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_image_from_url",
            "description": "下载指定 http/https 图片直链并直接作为图片发送到当前微信群。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "以 http:// 或 https:// 开头的图片直链地址",
                    }
                },
                "required": ["url"],
            },
        },
    },
]


@dataclass
class BrainReply:
    text: str
    bubbles: list[str]
    mood: str
    tokens_used: int
    used_vision: bool
    used_search: bool = False
    sticker_caption: str = ""
    attachment_path: Path | None = None
    vision_summary: str = ""


def split_human_bubbles(text: str) -> list[str]:
    """
    借鉴 MaiBot / KouriChat 的「拟人分段发送」机制：
    将回复拆成 1~3 条短微信气泡，去掉刻板的句末句号，模拟真人手机打字连发。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    if "||" in raw:
        parts = [p.strip() for p in raw.split("||") if p.strip()]
    else:
        # 若模型没输出 ||，按换行拆成最多 3 段短气泡
        parts = [p.strip() for p in raw.splitlines() if p.strip()]

    # 限制最多 3 条气泡，防止刷屏；若超过则合并末尾
    if len(parts) > 3:
        parts = parts[:2] + [" ".join(parts[2:])]

    cleaned: list[str] = []
    for p in parts:
        # 真人微信聊天很少在每句话末尾打正式句号
        p = p.rstrip("。")
        if p:
            cleaned.append(p)
    return cleaned or [raw]


def _encode_image_data_url(image_path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(image_path))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    raw = image_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _inspect_image_with_zhipu(image_path: Path, question: str = "") -> str | None:
    """
    使用智谱永久免费深度思考视觉模型 `glm-4.1v-thinking-flash` 做高精度看图：
    - 精准读取中文小字/微信截图/游戏战绩KDA/梗图文字/画面细节
    - 剥离 <think> 思考过程，只返回干脆精准的视觉分析，交给 DeepSeek 塔菲大脑用人话回复
    """
    if not settings.ZHIPU_API_KEY:
        return None
    try:
        data_url = _encode_image_data_url(image_path)
        ask = (
            f"请仔细看清这张图片里的所有细节（包括小字、数字、人名、游戏战绩/KDA/英雄、梗图文字、人物动作和表情），"
            f"并用简明准确的中文客观描述出来（不要瞎编图里没有的东西）。"
            + (f" 群友针对这张图问了：{question}" if question.strip() else "")
        )
        payload = json.dumps(
            {
                "model": "glm-4.1v-thinking-flash",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": ask},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "max_tokens": 1024,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {settings.ZHIPU_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=18.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        # 剥离 <think>...</think> 思考块，提取最终清晰结论
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
        cleaned = re.sub(r"<answer>|</answer>", "", cleaned, flags=re.I).strip()
        if cleaned:
            logger.info("👁️ 智谱 glm-4.1v-thinking-flash 高精识图成功 (%d字)", len(cleaned))
            return cleaned[:1000]
    except Exception as exc:
        logger.warning("智谱高精识图失败，将回退 DeepSeek 原生看图: %s", exc)
    return None


def _persona_voice_profile() -> tuple[str, str, str]:
    """返回 (角色简称, 回复口吻指引, 语气尾词)，避免切到峰哥人设时串出塔菲/喵。"""
    if get_active_persona() == "fengge":
        return (
            "峰哥",
            "峰哥松弛直白的短气泡老哥口吻（严禁使用“喵”“人家”“塔菲”等卖萌词）",
            "",
        )
    return ("塔菲", "塔菲真人群友的短气泡口吻", "喵")


def _build_system_prompt(
    status: PetStatus,
    sender: str,
    user_affinity: dict,
    recalled_memory: str = "",
    matched_slang: str = "",
    emotion_desc: str = "",
    style_hints: str = "",
) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    card_text = load_card()
    if get_active_persona() == "fengge":
        hunger_hint = f"段位 {status.title}。"
    else:
        hunger_hint = (
            f"你现在有点饿了（饱食度 {status.hunger}/100），可以顺嘴提一句 /投喂。"
            if status.hunger <= 25
            else f"饱食度 {status.hunger}/100，段位 {status.title}。"
        )
    extra_blocks = []
    if recalled_memory:
        extra_blocks.append(recalled_memory)
    if matched_slang:
        extra_blocks.append(matched_slang)
    if emotion_desc:
        extra_blocks.append(emotion_desc)
    if style_hints:
        extra_blocks.append(style_hints)
    extra_text = ("\n" + "\n".join(extra_blocks) + "\n") if extra_blocks else ""
    return (
        f"【当前时间】{now_str}（Asia/Shanghai）\n"
        f"{extra_text}"
        f"{card_text}\n\n"
        f"【实时状态】{hunger_hint} 当前说话人：{sender}（好感度 {user_affinity['affinity']}/100）。"
    )


class DaFeiYuBrain:
    def __init__(self) -> None:
        self._chat_client: OpenAI | None = None
        self._vision_client: OpenAI | None = None

    def _get_chat_client(self) -> OpenAI:
        if self._chat_client is None:
            self._chat_client = OpenAI(
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.DEEPSEEK_BASE_URL,
            )
        return self._chat_client

    def _get_vision_client(self) -> OpenAI:
        if self._vision_client is None:
            self._vision_client = OpenAI(
                api_key=settings.effective_vision_api_key,
                base_url=settings.effective_vision_base_url,
            )
        return self._vision_client

    def _parse_mood(self, raw_text: str, default_mood: str) -> tuple[str, str, str]:
        match = MOOD_TAG_PATTERN.search(raw_text)
        mood = match.group(1).lower() if match else default_mood
        caption = (match.group(2) or "").strip() if match else ""
        cleaned = MOOD_TAG_PATTERN.sub("", raw_text).strip()
        # 顺手去掉微信里显示很丑的 Markdown 粗体/标题符号
        cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"^#{1,4}\s*", "", cleaned, flags=re.M)
        cleaned = cleaned.replace("`", "")
        return cleaned.strip(), mood, caption

    def summarize_image(self, image_path: Path) -> str | None:
        """后台静默概括图片核心内容，仅使用智谱免费视觉模型（不消耗 DeepSeek Token）。"""
        if not image_path.is_file() or not settings.ZHIPU_API_KEY:
            return None
        desc = _inspect_image_with_zhipu(
            image_path,
            "请用一两句简洁客观的中文概括图片核心内容与关键文字/数据",
        )
        if not desc:
            return None
        return re.sub(r"\s+", " ", desc).strip()[:180]

    def inspect_image_detail(self, image_path: Path, question: str = "") -> str:
        """深度查看历史图片细节，优先智谱视觉，未配时回退 DeepSeek/自定义视觉模型。"""
        if not image_path.is_file():
            return "图片文件不存在或已被清理"
        desc = _inspect_image_with_zhipu(image_path, question)
        if desc:
            return desc
        if not settings.has_valid_api_key:
            return "未配置视觉模型 API Key，无法识别历史图片"
        try:
            prompt = question.strip() or "请用简洁客观的中文描述这张图片里的关键文字、数字和画面细节"
            data_url = _encode_image_data_url(image_path)
            resp = self._get_vision_client().chat.completions.create(
                model=settings.DEEPSEEK_VISION_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                        ],
                    }
                ],
                max_tokens=600,
            )
            return ((resp.choices[0].message.content or "").strip()) or "未能识别出图片内容"
        except Exception as exc:
            return f"查看历史图片失败: {exc}"

    def reply(
        self,
        sender: str,
        text: str,
        status: PetStatus,
        user_affinity: dict,
        recent_history: list[dict],
        image_path: Path | None = None,
        group_id: str = "",
        image_sender: str = "",
    ) -> BrainReply:
        if not settings.has_valid_api_key:
            if image_path is not None:
                demo = f"还没填 DEEPSEEK_API_KEY 喵||填完就能给你看图锐评了"
                b = split_human_bubbles(demo)
                return BrainReply(text="\n".join(b), bubbles=b, mood="shocked", tokens_used=0, used_vision=True)
            demo = f"离线演示模式喵||去 .env 填上 DEEPSEEK_API_KEY 就能聊了"
            b = split_human_bubbles(demo)
            return BrainReply(text="\n".join(b), bubbles=b, mood="happy", tokens_used=0, used_vision=False)

        # L1 识海按需召回 + 群黑话命中注入 + 情绪惯性状态机 + 语境语感 RAG
        active_persona = get_active_persona()
        role_name, voice_hint, tail_word = _persona_voice_profile()
        recalled_memory = memory_store.render_context(f"{sender} {text}", group_id=group_id)
        matched_slang = slang_book.render_matched(group_id, text)
        emotion_desc = emotion_sm.describe(group_id)
        style_hints = retrieve_style_hints(
            active_persona,
            text,
            recent_history=recent_history,
            group_id=group_id,
            top_k=2,
        )
        system_prompt = _build_system_prompt(
            status,
            sender,
            user_affinity,
            recalled_memory=recalled_memory,
            matched_slang=matched_slang,
            emotion_desc=emotion_desc,
            style_hints=style_hints,
        )
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # 注入最近 20 条群聊上下文（排除刚写入的当前这条，避免重复），让大肥鱼真正跟得上群里聊的每一句
        history_slice = recent_history[-21:]
        if history_slice and history_slice[-1].get("role") == "user" and history_slice[-1].get("sender") == sender and (history_slice[-1].get("content") or "").strip() == text.strip():
            history_slice = history_slice[:-1]
        for item in history_slice[-20:]:
            role = "assistant" if item["role"] == "assistant" else "user"
            prefix = "" if role == "assistant" else f"[{item['sender']}]: "
            content = item["content"] or ("[发了一张图片]" if item.get("image_path") else "")
            if content:
                messages.append({"role": role, "content": f"{prefix}{content}"})

        vision_summary = ""
        if image_path is not None and image_path.is_file():
            owner_desc = (
                f"[{sender}] 正在询问 [{image_sender}] 之前发的图片"
                if (image_sender and image_sender != sender)
                else f"[{sender}] 发了张图"
            )
            # 优先使用智谱免费深度思考视觉模型 glm-4.1v-thinking-flash 高精看图，再交给 DeepSeek 塔菲大脑自然回复
            zhipu_vision_desc = _inspect_image_with_zhipu(image_path, text)
            if zhipu_vision_desc:
                vision_summary = re.sub(r"\s+", " ", zhipu_vision_desc).strip()[:220]
                user_msg = (
                    f"{owner_desc}"
                    + (f"并说：{text}" if text.strip() else "")
                    + f"\n\n（你的眼睛已经把这张图的细节看得一清二楚：\n{zhipu_vision_desc}\n请直接以{voice_hint}吐槽或回答，别说“根据视觉描述”）"
                )
                messages.append({"role": "user", "content": user_msg})
                client = self._get_chat_client()
                model = settings.DEEPSEEK_CHAT_MODEL
                used_vision = True
            else:
                user_prompt = (
                    f"{owner_desc}并问：{text}\n（请先仔细辨认图里所有文字、数字、画面细节，再直接自然回答，别瞎猜也别用 Markdown 格式）"
                    if text.strip()
                    else f"{owner_desc}。请你仔细看清图里的文字和画面内容，像真群友一样简短吐槽或接梗（别瞎编图里没有的东西，别用 Markdown 格式）。"
                )
                data_url = _encode_image_data_url(image_path)
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                        ],
                    }
                )
                client = self._get_vision_client()
                model = settings.DEEPSEEK_VISION_MODEL
                used_vision = True
        else:
            # 命中实时/查询类问题时，直接前置秒搜并拼进单轮对话，省掉一次 DeepSeek 来回！
            pre_searched_block = ""
            if settings.ENABLE_WEB_SEARCH and any(cue in text for cue in PRE_SEARCH_CUES):
                logger.info("⚡ 前置高速联网搜索: %s", text[:40])
                pre_searched_block = web_search(text, max_results=4)

            if pre_searched_block:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[{sender}]: {text}\n\n"
                            f"（系统已前置联网查好资料。请严格以以下搜索结果中的事实、数字和日期为准，用{voice_hint}自然回答；"
                            f"如果结果里没有确切答案或全是旧信息，直接老实说没查到，严禁瞎编数字、版本或出装：\n{pre_searched_block}）"
                        ),
                    }
                )
            else:
                messages.append({"role": "user", "content": f"[{sender}]: {text}"})
            client = self._get_chat_client()
            model = settings.DEEPSEEK_CHAT_MODEL
            used_vision = False

        used_search = bool(not used_vision and "pre_searched_block" in locals() and pre_searched_block)
        is_code_or_file_task = (not used_vision) and any(c in text.lower() for c in CODE_OR_FILE_CUES)
        total_tokens = 0
        try:
            # 联网查事实时降温防幻觉；写代码/文件任务关掉惩罚避免破坏 HTML/JSON 结构；闲聊保持高拟人度
            if is_code_or_file_task:
                temp = 0.45
                freq_pen = 0.0
                pres_pen = 0.0
            else:
                temp = 0.45 if used_search else (0.65 if used_vision else 1.0)
                freq_pen = 0.1 if (used_search or used_vision) else 0.45
                pres_pen = 0.0 if (used_search or used_vision) else 0.4
            # 开启桌面工具时给足 4096 max_tokens，防止 deepseek-flash 思考 + .html/.py 参数 JSON 被截断
            kwargs: dict = {
                "model": model,
                "messages": messages,
                "temperature": temp,
                "frequency_penalty": freq_pen,
                "presence_penalty": pres_pen,
                "max_tokens": 4096 if (not used_vision and is_owner(sender)) else (800 if used_vision else 260),
            }
            # 挂载工具列表（普通工具 + 主人权限下的桌面 Agent 电脑操纵工具）
            available_tools = list(SEARCH_TOOLS) if (settings.ENABLE_WEB_SEARCH and not used_search) else [
                t for t in SEARCH_TOOLS if t["function"]["name"] != "web_search"
            ]
            if is_owner(sender):
                available_tools.extend(AGENT_TOOL_SCHEMAS)

            if not used_vision and available_tools:
                kwargs["tools"] = available_tools
                kwargs["tool_choice"] = "auto"

            attachment_path: Path | None = None
            raw_content = ""

            # 最多支持 4 轮多步 Agent 工具链（如：列目录 -> 读文件 -> 执行脚本 -> 回复）
            for _step in range(4):
                resp = client.chat.completions.create(**kwargs)
                total_tokens += getattr(resp.usage, "total_tokens", 0) or 0
                choice = resp.choices[0]
                finish_reason = getattr(choice, "finish_reason", None)
                msg_obj = choice.message
                tool_calls = getattr(msg_obj, "tool_calls", None)
                if not tool_calls:
                    raw_content = (msg_obj.content or "").strip()
                    break

                messages.append(msg_obj.model_dump(exclude_none=True))
                for tc in tool_calls[:3]:
                    fn_name = getattr(tc.function, "name", "")
                    fn_args_raw = getattr(tc.function, "arguments", "{}")
                    try:
                        args = json.loads(fn_args_raw)
                    except json.JSONDecodeError as json_exc:
                        logger.warning(
                            "工具 %s 参数 JSON 解析失败 (finish_reason=%s, len=%d): %s",
                            fn_name,
                            finish_reason,
                            len(fn_args_raw or ""),
                            json_exc,
                        )
                        tool_out = (
                            f"工具 {fn_name} 的参数 JSON 解析失败（finish_reason={finish_reason}，"
                            "可能内容过长被截断或引号未正确转义），请精简内容并确保输出合法完整的 JSON 后重试"
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": tool_out,
                            }
                        )
                        continue

                    if fn_name == "web_search":
                        used_search = True
                        q = str(args.get("query") or text)
                        logger.info("🌐 大肥鱼自主联网搜索: %s", q)
                        tool_out = web_search(q)
                    elif fn_name == "remember_fact":
                        fact_text = str(args.get("text") or "").strip()
                        fact_level = str(args.get("level") or "normal")
                        fact_tags = list(args.get("tags") or [])
                        if sender and sender not in fact_tags:
                            fact_tags.append(sender)
                        _, tool_out = memory_store.remember(
                            text=fact_text,
                            level=fact_level,
                            tags=fact_tags,
                            source="self",
                            group_id=group_id,
                            raw=text,
                        )
                    elif fn_name == "learn_slang":
                        _, tool_out = slang_book.learn(
                            group_id, str(args.get("term") or ""), str(args.get("meaning") or "")
                        )
                    elif fn_name == "evolve_persona":
                        note = str(args.get("note") or "").strip()
                        _, tool_out = evolve_persona(note)
                        logger.info("✨ 大肥鱼自我进化人格: %s -> %s", note, tool_out)
                    elif fn_name == "add_schedule":
                        _, tool_out = scheduler.add_task(
                            group_id=group_id,
                            creator=sender,
                            task_type=str(args.get("task_type") or "once"),
                            content=str(args.get("content") or ""),
                            mode=str(args.get("mode") or "remind"),
                            delay_minutes=args.get("delay_minutes"),
                            run_at=args.get("run_at"),
                            daily_time=args.get("daily_time"),
                            interval_minutes=args.get("interval_minutes"),
                        )
                    elif fn_name == "list_schedules":
                        tasks = scheduler.list_tasks(group_id=group_id)
                        if not tasks:
                            tool_out = "当前群里没有任何定时任务"
                        else:
                            tool_out = "\n".join(
                                f"#{t['id']} [{t['desc']}|{t['mode']}] {t['content']}" for t in tasks
                            )
                    elif fn_name == "delete_schedule":
                        _, tool_out = scheduler.delete_task(str(args.get("task_id") or ""))
                    elif fn_name == "inspect_group_image":
                        recent_imgs = pet_engine.get_recent_images(group_id, limit=8)
                        try:
                            idx = max(1, min(8, int(args.get("index") or 1)))
                        except (TypeError, ValueError):
                            idx = 1
                        if not recent_imgs:
                            tool_out = "当前群聊记录里还没有可查看的历史图片"
                        elif idx > len(recent_imgs):
                            tool_out = f"本群最近只记录了 {len(recent_imgs)} 张图片，没有第 {idx} 张"
                        else:
                            target_img = recent_imgs[idx - 1]
                            img_file = Path(target_img["image_path"])
                            detail = self.inspect_image_detail(img_file, str(args.get("question") or ""))
                            pet_engine.update_history_content_by_image(
                                group_id,
                                target_img["image_path"],
                                re.sub(r"\s+", " ", detail).strip()[:180],
                            )
                            tool_out = f"第 {idx} 张图（由 {target_img['sender']} 发送）：\n{detail}"
                    elif fn_name == "search_and_send_image":
                        q = str(args.get("query") or "").strip()
                        logger.info("🖼️ 联网搜图并发送: %s", q)
                        fpath, tool_out = search_and_download_image(q)
                        if fpath and fpath.is_file() and fpath.stat().st_size > 0:
                            attachment_path = fpath
                            tool_out = f"{tool_out}。图片已加入待发送附件，请用一两句{voice_hint}告诉群友图给他们找来啦。"
                    elif fn_name == "download_image_from_url":
                        u = str(args.get("url") or "").strip()
                        logger.info("🌐 下载直链图片并发送: %s", u[:80])
                        fpath, tool_out = download_image_url(u, label="urlimg")
                        if fpath and fpath.is_file() and fpath.stat().st_size > 0:
                            attachment_path = fpath
                            tool_out = f"{tool_out}。图片已加入待发送附件，请直接简短告知群友。"
                    elif fn_name in (
                        "run_shell",
                        "read_local_file",
                        "write_local_file",
                        "list_directory",
                        "open_url_or_app",
                        "take_screenshot",
                        "get_system_info",
                        "fetch_webpage",
                        "create_and_send_file",
                        "send_existing_file",
                        "self_pull_and_restart",
                        "self_edit_code",
                        "self_restart",
                        "delegate_to_dsh",
                        "check_dsh_jobs",
                    ):
                        is_danger = False
                        danger_reasons: list[str] = []
                        if is_owner(sender) and fn_name in (
                            "run_shell",
                            "read_local_file",
                            "write_local_file",
                            "open_url_or_app",
                            "send_existing_file",
                            "self_edit_code",
                            "delegate_to_dsh",
                        ):
                            is_danger, danger_reasons = assess_danger(
                                fn_name,
                                args if isinstance(args, dict) else {},
                                user_text=text,
                            )

                        if not is_owner(sender):
                            tool_out = "权限不足：当前发言人不在 OWNER_NAMES 主人白名单内，拒绝操纵电脑"
                        elif is_danger:
                            logger.warning("🛡️ 高危操作 [%s] 已拦截并生成二次确认码: %s", fn_name, danger_reasons)
                            _, confirm_card = stage_pending_action(
                                group_id=group_id,
                                sender=sender,
                                action_type=fn_name,
                                params=args if isinstance(args, dict) else {},
                                reasons=danger_reasons,
                            )
                            return BrainReply(
                                text=confirm_card,
                                bubbles=[confirm_card],
                                mood="shocked",
                                tokens_used=total_tokens,
                                used_vision=used_vision,
                                used_search=used_search,
                            )
                        elif fn_name == "run_shell":
                            logger.info("💻 Agent 执行命令: %s", str(args.get("command", ""))[:80])
                            tool_out = run_shell(str(args.get("command") or ""))
                        elif fn_name == "read_local_file":
                            tool_out = read_local_file(str(args.get("path") or ""))
                        elif fn_name == "write_local_file":
                            target_path = str(args.get("path") or "").strip()
                            tool_out = write_local_file(target_path, str(args.get("content") or ""))
                            if (
                                attachment_path is None
                                and target_path
                                and any(k in text for k in ("发我", "发给我", "发群里", "发过来", "发送"))
                            ):
                                wp = Path(target_path).expanduser()
                                if wp.is_file() and wp.stat().st_size > 0:
                                    attachment_path = wp
                                    tool_out += "（检测到需要发送到群里，已自动附带该文件）"
                        elif fn_name == "list_directory":
                            tool_out = list_directory(str(args.get("path") or "."))
                        elif fn_name == "open_url_or_app":
                            logger.info("🚀 Agent 打开目标: %s", args.get("target"))
                            tool_out = open_url_or_app(str(args.get("target") or ""))
                        elif fn_name == "take_screenshot":
                            logger.info("📸 Agent 截取桌面屏幕")
                            shot_path, tool_out = take_screenshot()
                            if shot_path:
                                attachment_path = shot_path
                        elif fn_name == "get_system_info":
                            tool_out = get_system_info()
                        elif fn_name == "fetch_webpage":
                            tool_out = fetch_webpage(str(args.get("url") or ""))
                        elif fn_name == "create_and_send_file":
                            fname = str(args.get("filename") or "").strip()
                            logger.info("📄 Agent 生成并发送文件: %s", fname or "<empty>")
                            fpath, tool_out = create_and_send_file(fname, str(args.get("content") or ""))
                            if fpath and fpath.is_file() and fpath.stat().st_size > 0:
                                attachment_path = fpath
                        elif fn_name == "send_existing_file":
                            logger.info("📎 Agent 发送已有文件: %s", args.get("path"))
                            fpath, tool_out = send_existing_file(str(args.get("path") or ""))
                            if fpath and fpath.is_file() and fpath.stat().st_size > 0:
                                attachment_path = fpath
                        elif fn_name == "self_pull_and_restart":
                            logger.info("🔄 Agent 触发自更新与重启")
                            ok_pull, pull_detail = git_pull_and_check()
                            if ok_pull:
                                restart_msg = request_restart(
                                    reason="从 GitHub 拉取最新代码",
                                    notify_group=group_id,
                                )
                                tool_out = f"{pull_detail}\n{restart_msg}\n请先简洁告知用户即将重启，几秒后会回来。"
                            else:
                                tool_out = pull_detail
                        elif fn_name == "self_edit_code":
                            logger.info("🛠️ Agent 尝试自修改代码: %s", args.get("rel_path"))
                            tool_out = self_edit_code(
                                str(args.get("rel_path") or ""),
                                str(args.get("target") or ""),
                                str(args.get("replacement") or ""),
                            )
                        elif fn_name == "self_restart":
                            reason = str(args.get("reason") or "主人要求重启")
                            logger.info("♻️ Agent 触发重启: %s", reason)
                            tool_out = request_restart(reason=reason, notify_group=group_id)
                        elif fn_name == "delegate_to_dsh":
                            task_text = str(args.get("task") or "")
                            sid = str(args.get("session_id") or "")
                            wdir = str(args.get("workdir") or "")
                            logger.info("🤖 Agent 自动派单给 DSH 后台: %s", task_text[:80])
                            ok_dsh, dsh_res = submit_dsh_job(
                                group_id=group_id,
                                sender=sender,
                                task=task_text,
                                session_id=sid,
                                workdir=wdir,
                            )
                            if ok_dsh and isinstance(dsh_res, dict):
                                tool_out = (
                                    f"已成功将任务异步派发到独立 DSH 后台进程（重启也不中断，不阻塞当前群聊）："
                                    f"job_id={dsh_res['job_id']}, session_id={dsh_res['session_id'] or 'new'}。"
                                    "请直接用大肥鱼自然口吻先回复主人“收到，已派给后台 DSH 干活去了，干完会自动发回群里”。"
                                )
                            else:
                                tool_out = f"派发 DSH 任务失败: {dsh_res}"
                        elif fn_name == "check_dsh_jobs":
                            limit = max(1, min(15, int(args.get("limit") or 5)))
                            jobs = list_dsh_jobs(group_id=group_id, limit=limit)
                            if not jobs:
                                tool_out = "当前群暂无后台 DSH 任务记录"
                            else:
                                tool_out = "\n".join(
                                    f"#{j['job_id']} [session={j['session_id']}] [{j['status']}|{j['duration_sec']}s] "
                                    f"{j['task'][:40]} -> {j['summary'][:120]}"
                                    for j in jobs
                                )
                        else:
                            tool_out = "未知工具"
                    else:
                        tool_out = "未知工具"

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(tool_out),
                        }
                    )

                # 后续轮次保持 4096 tokens 且关闭惩罚，防止多步写文件/重传 JSON 被截断
                kwargs["messages"] = messages
                kwargs["temperature"] = 0.45
                kwargs["frequency_penalty"] = 0.0
                kwargs["presence_penalty"] = 0.0
                kwargs["max_tokens"] = 4096
            else:
                raw_content = f"工具跑完一轮了，结果已经在上面了{tail_word}"

            cleaned, mood, sticker_caption = self._parse_mood(raw_content, "shocked" if used_vision else status.mood_tag)
            bubbles = split_human_bubbles(cleaned or f"网卡了一下 刚才说啥来着{tail_word}")
            return BrainReply(
                text="\n".join(bubbles),
                bubbles=bubbles,
                mood=mood,
                tokens_used=total_tokens,
                used_vision=used_vision,
                used_search=used_search,
                sticker_caption=sticker_caption,
                attachment_path=attachment_path,
                vision_summary=vision_summary,
            )
        except Exception as exc:
            err_msg = str(exc)
            if used_vision:
                hint = f"看图接口抽风了（{err_msg[:100]}）"
            else:
                hint = f"大脑断网了{tail_word}（{err_msg[:100]}）"
            return BrainReply(text=hint, bubbles=[hint], mood="sleepy", tokens_used=total_tokens, used_vision=used_vision)

    def judge_and_chime_in(self, recent_history: list[dict], status: PetStatus) -> BrainReply | None:
        """
        自主潜水插嘴：阅读最近几条群聊，自行判断有没有乐子/槽点值得冒泡。
        没有槽点就返回 None（继续潜水摸鱼）。
        """
        if not settings.has_valid_api_key or not recent_history:
            return None

        transcript_lines = []
        for item in recent_history[-8:]:
            content = (item.get("content") or "").strip()
            if content:
                transcript_lines.append(f"[{item['sender']}]: {content}")
        if len(transcript_lines) < 3:
            return None

        transcript = "\n".join(transcript_lines)
        role_name, voice_hint, _ = _persona_voice_profile()
        prompt = f"""你是潜伏在微信群里的「{settings.BOT_NAME}」（当前人设：{role_name}，{voice_hint}，当前饱食度 {status.hunger}/100）。

下面是群友刚才的聊天记录（他们没有叫你）：
{transcript}

请你自行判断：这波聊天里有没有好笑、抽象、逆天、游戏/生活吐槽或适合接梗神补刀的点？
- 如果纯粹只是发问号、约几点钟、无聊的机械打卡，请**只输出** `[PASS]`。
- 只要有哪怕一点乐子或槽点，就果断以{voice_hint}冒泡补刀 **1~2 句**（简短自然、不要用 Markdown），并在末尾附带情绪标签 `[MOOD:happy]` / `[MOOD:smug]` / `[MOOD:shocked]` / `[MOOD:angry]` / `[MOOD:sleepy]` / `[MOOD:gaming]` / `[MOOD:love]` / `[MOOD:hungry]`。"""

        try:
            resp = self._get_chat_client().chat.completions.create(
                model=settings.DEEPSEEK_CHAT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=160,
            )
            raw = (resp.choices[0].message.content or "").strip()
            tokens_used = getattr(resp.usage, "total_tokens", 0) or 0
            if not raw or "[PASS]" in raw.upper():
                return None
            cleaned, mood, sticker_caption = self._parse_mood(raw, "happy")
            if not cleaned:
                return None
            bubbles = split_human_bubbles(cleaned)
            return BrainReply(
                text="\n".join(bubbles),
                bubbles=bubbles,
                mood=mood,
                tokens_used=tokens_used,
                used_vision=False,
                sticker_caption=sticker_caption,
            )
        except Exception as exc:
            logger.warning("自主插嘴判断异常: %s", exc)
            return None

    def is_talking_to_or_about_me(self, sender: str, text: str, recent_history: list[dict]) -> bool:
        """
        极轻量上下文指代判断（仅输出 YES / NO，~0.6秒）：
        当群友没点名“大肥鱼”，但大肥鱼刚在群里说过话、或消息里提到了“你/他/它/头像/小号”等时，
        结合最近几条上下文判断这句话是不是在跟大肥鱼继续聊或在议论大肥鱼。
        """
        if not settings.has_valid_api_key or not text.strip():
            return False
        ctx_lines = [
            f"[{m['sender']}]: {m['content']}"
            for m in recent_history[-6:]
            if m.get("content")
        ]
        ctx = "\n".join(ctx_lines)
        prompt = (
            f"你是群里的「{settings.BOT_NAME}」（群友也叫你峰哥、老峰、塔菲、鱼、机器人、小号）。\n"
            f"刚才群里的聊天记录：\n{ctx}\n\n"
            f"现在 [{sender}] 发了一句新消息：「{text}」\n"
            f"请判断这句话是不是在继续跟你说话、接你的话茬、或者在议论你（比如评价你的头像、反应、智商、功能）？\n"
            f"只输出 YES 或 NO，不要输出任何其他字。"
        )
        try:
            resp = self._get_chat_client().chat.completions.create(
                model=settings.DEEPSEEK_CHAT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=256,
            )
            ans = (resp.choices[0].message.content or "").strip().upper()
            logger.info("🧭 上下文指代判定 [%s]: %s -> %s", sender, text[:30], ans)
            return "YES" in ans
        except Exception as exc:
            logger.warning("上下文指代判定异常: %s", exc)
            return False

    def generate_autonomous_bubble(self, kind: str, status: PetStatus) -> BrainReply:
        """生成定时作息 / 饿扁催饭的主动冒泡发言"""
        role_name, voice_hint, tail_word = _persona_voice_profile()
        if not settings.has_valid_api_key:
            msg = (
                f"饿扁了{tail_word}||谁发个 /投喂 管顿白饭"
                if kind == "hungry"
                else f"晚上好{tail_word}||今晚怎么说"
            )
            b = split_human_bubbles(msg)
            return BrainReply(text="\n".join(b), bubbles=b, mood="hungry" if kind == "hungry" else "happy", tokens_used=0, used_vision=False)

        if kind == "hungry":
            instruction = (
                f"你是微信群里的「{settings.BOT_NAME}」（当前人设：{role_name}，{voice_hint}），现在饱食度只剩 {status.hunger}/100。"
                f"请主动在群里发 1~2 句符合你人设的催饭发言，让群友发 `/投喂` 管顿饭，末尾带 `[MOOD:hungry]`。"
            )
        else:
            instruction = (
                f"你是微信群里的「{settings.BOT_NAME}」（当前人设：{role_name}，{voice_hint}），现在晚上 8 点左右，群里有点安静。"
                f"请主动发 1~2 句符合你人设的冒泡开场白（比如问大伙今晚干啥、开不开黑、是不是都在摸鱼），末尾带 `[MOOD:happy]` 或 `[MOOD:sleepy]`。"
            )

        try:
            resp = self._get_chat_client().chat.completions.create(
                model=settings.DEEPSEEK_CHAT_MODEL,
                messages=[{"role": "user", "content": instruction}],
                temperature=0.9,
                max_tokens=140,
            )
            raw = (resp.choices[0].message.content or "").strip()
            tokens_used = getattr(resp.usage, "total_tokens", 0) or 0
            cleaned, mood, sticker_caption = self._parse_mood(raw, "hungry" if kind == "hungry" else "happy")
            b = split_human_bubbles(cleaned or f"都在干嘛呢{tail_word}")
            return BrainReply(
                text="\n".join(b),
                bubbles=b,
                mood=mood,
                tokens_used=tokens_used,
                used_vision=False,
                sticker_caption=sticker_caption,
            )
        except Exception:
            fallback = (
                f"饿扁了{tail_word}||谁发个 /投喂 救济一下"
                if kind == "hungry"
                else f"晚上好{tail_word}||今晚怎么说"
            )
            b = split_human_bubbles(fallback)
            return BrainReply(
                text="\n".join(b),
                bubbles=b,
                mood="hungry" if kind == "hungry" else "happy",
                tokens_used=0,
                used_vision=False,
            )

    def write_daily_diary(self, recent_history: list[dict], status: PetStatus) -> bool:
        """L2 日记：每晚以当前人设口吻写一条今天群里的细节与心情（≤200字）"""
        if not settings.has_valid_api_key or not recent_history:
            return False
        lines = [f"[{m['sender']}]: {m['content']}" for m in recent_history[-15:] if m.get("content")]
        if not lines:
            return False
        role_name, voice_hint, _ = _persona_voice_profile()
        prompt = (
            f"你是群里的「{settings.BOT_NAME}」（当前人设：{role_name}，{voice_hint}）。请根据今天群里发生的片段，写一条 60~160 字的私人日记，"
            "记录今天让你印象最深的细节和心情（像真日记，不要汇报，贴合当前人设口吻）。\n"
            "只输出一行，格式：`心情词|日记正文`。\n\n"
            + "\n".join(lines)
        )
        try:
            resp = self._get_chat_client().chat.completions.create(
                model=settings.DEEPSEEK_CHAT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.85,
                max_tokens=220,
            )
            raw = (resp.choices[0].message.content or "").strip()
            if "|" in raw:
                mood, body = raw.split("|", 1)
            else:
                mood, body = "摸鱼", raw
            return memory_store.write_diary(mood.strip()[:8], body.strip())
        except Exception as exc:
            logger.warning("生成 L2 每日日记失败: %s", exc)
            return False


brain = DaFeiYuBrain()
