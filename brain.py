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
from memory import memory_store
from persona_runtime import emotion_sm, slang_book
from pet_engine import PetStatus
from roles.build_card import evolve_persona, load_card
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


def _build_system_prompt(
    status: PetStatus,
    sender: str,
    user_affinity: dict,
    recalled_memory: str = "",
    matched_slang: str = "",
    emotion_desc: str = "",
) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    card_text = load_card()
    hunger_hint = (
        f"你现在饿扁了（饱食度 {status.hunger}/100），可以顺嘴讨一句 /投喂。"
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
        return cleaned.strip(), mood, caption

    def reply(
        self,
        sender: str,
        text: str,
        status: PetStatus,
        user_affinity: dict,
        recent_history: list[dict],
        image_path: Path | None = None,
        group_id: str = "",
    ) -> BrainReply:
        if not settings.has_valid_api_key:
            if image_path is not None:
                demo = f"还没填 DEEPSEEK_API_KEY 喵||填完就能给你看图锐评了"
                b = split_human_bubbles(demo)
                return BrainReply(text="\n".join(b), bubbles=b, mood="shocked", tokens_used=0, used_vision=True)
            demo = f"离线演示模式喵||去 .env 填上 DEEPSEEK_API_KEY 就能聊了"
            b = split_human_bubbles(demo)
            return BrainReply(text="\n".join(b), bubbles=b, mood="happy", tokens_used=0, used_vision=False)

        # L1 识海按需召回 + 群黑话命中注入 + 情绪惯性状态机
        recalled_memory = memory_store.render_context(f"{sender} {text}", group_id=group_id)
        matched_slang = slang_book.render_matched(group_id, text)
        emotion_desc = emotion_sm.describe(group_id)
        system_prompt = _build_system_prompt(
            status,
            sender,
            user_affinity,
            recalled_memory=recalled_memory,
            matched_slang=matched_slang,
            emotion_desc=emotion_desc,
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

        if image_path is not None and image_path.is_file():
            # 优先使用智谱免费深度思考视觉模型 glm-4.1v-thinking-flash 高精看图，再交给 DeepSeek 塔菲大脑自然回复
            zhipu_vision_desc = _inspect_image_with_zhipu(image_path, text)
            if zhipu_vision_desc:
                user_msg = (
                    f"[{sender}] 发了张图"
                    + (f"并说：{text}" if text.strip() else "")
                    + f"\n\n（你的眼睛已经把这张图的细节看得一清二楚：\n{zhipu_vision_desc}\n请直接以塔菲真人群友的短气泡口吻吐槽或回答，别说“根据视觉描述”）"
                )
                messages.append({"role": "user", "content": user_msg})
                client = self._get_chat_client()
                model = settings.DEEPSEEK_CHAT_MODEL
                used_vision = True
            else:
                user_prompt = (
                    f"[{sender}] 发了张图并问：{text}\n（请先仔细辨认图里所有文字、数字、画面细节，再直接自然回答，别瞎猜也别用 Markdown 格式）"
                    if text.strip()
                    else f"[{sender}] 在群里发了张图。请你仔细看清图里的文字和画面内容，像真群友一样简短吐槽或接梗（别瞎编图里没有的东西，别用 Markdown 格式）。"
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
                            f"（系统已前置联网查好资料。请严格以以下搜索结果中的事实、数字和日期为准，用塔菲的短气泡口吻自然回答；"
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
        total_tokens = 0
        try:
            # 联网查事实时降温防幻觉；普通闲聊提高温度 + 双惩罚 + 压短 max_tokens 逼它说直白大白话
            temp = 0.45 if used_search else (0.65 if used_vision else 1.0)
            freq_pen = 0.1 if (used_search or used_vision) else 0.45
            pres_pen = 0.0 if (used_search or used_vision) else 0.4
            # 若开启了工具，需要给足 max_tokens 防止写文件参数 JSON 被截断；普通闲聊仍靠 Prompt 保持极短气泡
            kwargs: dict = {
                "model": model,
                "messages": messages,
                "temperature": temp,
                "frequency_penalty": freq_pen,
                "pres_pen_dummy": None,
            }
            kwargs.pop("pres_pen_dummy")
            kwargs["presence_penalty"] = pres_pen
            kwargs["max_tokens"] = 900 if (not used_vision and is_owner(sender)) else (600 if used_vision else 220)
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
                msg_obj = resp.choices[0].message
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
                    except json.JSONDecodeError:
                        args = {}

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
                    ):
                        if not is_owner(sender):
                            tool_out = "权限不足：当前发言人不在 OWNER_NAMES 主人白名单内，拒绝操纵电脑"
                        elif fn_name == "run_shell":
                            logger.info("💻 Agent 执行命令: %s", str(args.get("command", ""))[:80])
                            tool_out = run_shell(str(args.get("command") or ""))
                        elif fn_name == "read_local_file":
                            tool_out = read_local_file(str(args.get("path") or ""))
                        elif fn_name == "write_local_file":
                            tool_out = write_local_file(
                                str(args.get("path") or ""), str(args.get("content") or "")
                            )
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
                            fname = str(args.get("filename") or "output.txt")
                            logger.info("📄 Agent 生成并发送文件: %s", fname)
                            fpath, tool_out = create_and_send_file(fname, str(args.get("content") or ""))
                            if fpath:
                                attachment_path = fpath
                        elif fn_name == "send_existing_file":
                            logger.info("📎 Agent 发送已有文件: %s", args.get("path"))
                            fpath, tool_out = send_existing_file(str(args.get("path") or ""))
                            if fpath:
                                attachment_path = fpath
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

                # 后续轮次放宽长度并降温，保证把工具执行结果准确说清楚
                kwargs["messages"] = messages
                kwargs["temperature"] = 0.45
                kwargs["frequency_penalty"] = 0.1
                kwargs["max_tokens"] = 450
            else:
                raw_content = "工具跑完一轮啦，结果已经在上面了喵"

            cleaned, mood, sticker_caption = self._parse_mood(raw_content, "shocked" if used_vision else status.mood_tag)
            bubbles = split_human_bubbles(cleaned or "网卡了一下 刚才说啥来着喵")
            return BrainReply(
                text="\n".join(bubbles),
                bubbles=bubbles,
                mood=mood,
                tokens_used=total_tokens,
                used_vision=used_vision,
                used_search=used_search,
                sticker_caption=sticker_caption,
                attachment_path=attachment_path,
            )
        except Exception as exc:
            err_msg = str(exc)
            if used_vision:
                hint = f"看图接口抽风了（{err_msg[:100]}）"
            else:
                hint = f"大脑断网了喵（{err_msg[:100]}）"
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
        prompt = f"""你是潜伏在微信群里的「{settings.BOT_NAME}」（永雏塔菲人格，古灵精怪、傲娇欠揍爱看乐子，说话短平快自然带“喵”，当前饱食度 {status.hunger}/100）。

下面是群友刚才的聊天记录（他们没有叫你）：
{transcript}

请你自行判断：这波聊天里有没有好笑、抽象、逆天、游戏/生活吐槽或适合接梗神补刀的点？
- 如果纯粹只是发问号、约几点钟、无聊的机械打卡，请**只输出** `[PASS]`。
- 只要有哪怕一点乐子或槽点，就果断以塔菲的口吻冒泡神补刀 **1~2 句**（简短自然、不要用 Markdown），并在末尾附带情绪标签 `[MOOD:happy]` / `[MOOD:smug]` / `[MOOD:shocked]` / `[MOOD:angry]` / `[MOOD:sleepy]` / `[MOOD:gaming]` / `[MOOD:love]` / `[MOOD:hungry]`。"""

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
            f"你是群里的「{settings.BOT_NAME}」（群友也叫你塔菲、鱼、机器人、小号）。\n"
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
        if not settings.has_valid_api_key:
            msg = (
                "饿扁了喵||谁发个 /投喂 管顿白饭呀"
                if kind == "hungry"
                else "晚上好喵||今晚开不开黑呀"
            )
            b = split_human_bubbles(msg)
            return BrainReply(text="\n".join(b), bubbles=b, mood="hungry" if kind == "hungry" else "happy", tokens_used=0, used_vision=False)

        if kind == "hungry":
            instruction = (
                f"你是微信群里的永雏塔菲人格大肥鱼，现在饱食度只剩 {status.hunger}/100 饿扁了。"
                "请主动在群里发 1~2 句撒娇又有点欠揍的催饭发言（自然带喵），让群友发 `/投喂` 给白饭吃，末尾带 `[MOOD:hungry]`。"
            )
        else:
            instruction = (
                "你是微信群里的永雏塔菲人格大肥鱼，现在晚上 8 点左右，群里有点安静。"
                "请主动发 1~2 句塔菲风格的冒泡开场白（比如问雏草姬/群友们今晚开不开黑、是不是都在摸鱼，自然带喵），末尾带 `[MOOD:happy]` 或 `[MOOD:sleepy]`。"
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
            b = split_human_bubbles(cleaned or "在干嘛呢喵")
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
                "饿扁了喵||谁发个 /投喂 救济一下呀"
                if kind == "hungry"
                else "晚上好喵||今晚怎么说"
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
        """L2 日记：每晚以塔菲口吻写一条今天群里的细节与心情（≤200字）"""
        if not settings.has_valid_api_key or not recent_history:
            return False
        lines = [f"[{m['sender']}]: {m['content']}" for m in recent_history[-15:] if m.get("content")]
        if not lines:
            return False
        prompt = (
            "你是大肥鱼（永雏塔菲人格）。请根据今天群里发生的片段，写一条 60~160 字的私人日记，"
            "记录今天让你印象最深的细节和心情（像真日记，不要汇报，带一点塔菲味）。\n"
            "只输出一行，格式：`心情词|日记正文`（例如：`开心|今天群里那帮杂鱼又在吹亚索……`）。\n\n"
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
