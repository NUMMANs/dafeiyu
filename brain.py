from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from openai import OpenAI

from config import settings
from image_fetcher import download_image_url, search_and_download_image
import l3_inject
from memory import estimate_tokens, memory_store
import metrics
from persona_runtime import slang_book
from pet_engine import PetStatus, pet_engine
from roles.build_card import evolve_persona, interest_zone, load_card
from scheduler import scheduler
import tuning
from web_search import web_search

logger = logging.getLogger("dafeiyu.brain")

MOOD_TAG_PATTERN = re.compile(
    r"\[(?:MOOD|NM|情绪)\s*[:：]\s*(happy|smug|hungry|shocked|angry|sleepy|gaming|love)(?:\s*[|｜]\s*([^\]]{1,14}))?\]",
    re.IGNORECASE,
)

# Grok 模式高频核查/实时查询触发词：命中时直接单轮前置秒搜，省掉一次 tool_call 往返
PRE_SEARCH_CUES = (
    "真的吗", "真假", "真的假的", "保真", "一眼假", "辟谣", "求证", "核实",
    "发生了什么", "怎么回事", "什么梗", "啥梗", "来龙去脉", "前因后果", "原委",
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
            "description": "联网核查事实与最新信息（真假求证/新闻溯源/数据价格/版本攻略/陌生热梗）。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "精准搜索关键词"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "记进长期识海（群友特征/立下的flag/黑历史/群梗/约定）。只记值得留的，严禁记密码证件银行卡。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "事实描述，≤120字"},
                    "level": {
                        "type": "string",
                        "enum": ["normal", "important"],
                        "description": "普通日常梗 normal；重要特征/flag/约定 important",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2~4 个标签（昵称/游戏/话题）",
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
            "description": "记录群内黑话、缩写或外号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "黑话词或外号"},
                    "meaning": {"type": "string", "description": "群里的真实含义"},
                },
                "required": ["term", "meaning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evolve_persona",
            "description": "追加一条自我进化笔记（和群友相处沉淀出的新默契或吐槽偏好）。",
            "parameters": {
                "type": "object",
                "properties": {"note": {"type": "string", "description": "第一人称一句自我进化记录"}},
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule",
            "description": "定时任务。action=add 新建 / list 查看本群任务 / delete 删除。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "list", "delete"]},
                    "content": {"type": "string", "description": "add：提醒内容或要AI执行的指令"},
                    "task_type": {
                        "type": "string",
                        "enum": ["once", "daily", "interval"],
                        "description": "add：一次性/每日/固定间隔",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["remind", "ai_task"],
                        "description": "remind=到点发文字；ai_task=到点让AI联网核查或执行",
                    },
                    "run_at": {"type": "string", "description": "once：HH:MM 或 YYYY-MM-DD HH:MM"},
                    "delay_minutes": {"type": "integer", "description": "once：多少分钟后"},
                    "daily_time": {"type": "string", "description": "daily：HH:MM"},
                    "interval_minutes": {"type": "integer", "description": "interval：间隔分钟（≥5）"},
                    "task_id": {"type": "string", "description": "delete：任务ID"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_group_image",
            "description": "重看本群近期历史里的某张图片（「上面那张图」「刚才截图」「第二张战绩图」）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "倒数第几张，1=最新，范围1~8，默认1"},
                    "question": {"type": "string", "description": "重点核查或看什么细节（可选）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_image",
            "description": "找图发到群里：给 query 按关键词搜图；给 url 下直链图。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜图关键词"},
                    "url": {"type": "string", "description": "图片 http/https 直链"},
                },
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
    """将回复拆成 1~3 条利落的微信短气泡。"""
    raw = (text or "").strip()
    if not raw:
        return []
    if "||" in raw:
        parts = [p.strip() for p in raw.split("||") if p.strip()]
    else:
        parts = [p.strip() for p in raw.splitlines() if p.strip()]

    if len(parts) > 3:
        parts = parts[:2] + [" ".join(parts[2:])]

    cleaned: list[str] = []
    for p in parts:
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


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.I | re.S)
_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>", re.I)
_THINK_ANY_RE = re.compile(r"</?(?:think|answer)\b[^>]*>", re.I)


def strip_think(raw: str) -> str:
    """剥掉 `<think>...</think>` 思考链（含被 max_tokens 截断的半截 `<think>`）。"""
    s = _THINK_BLOCK_RE.sub("", raw or "")
    parts = _THINK_OPEN_RE.split(s)
    if len(parts) > 1:
        s = parts[0]
    if re.search(r"</think\s*>", s, flags=re.I):
        s = re.split(r"</think\s*>", s, flags=re.I)[-1]
    s = _THINK_ANY_RE.sub("", s)
    return s.strip()


def _inspect_image_with_zhipu(image_path: Path, user_text: str) -> str | None:
    """使用智谱免费视觉模型提取图片文字、数字、UI细节与破绽。"""
    if not settings.ZHIPU_API_KEY:
        return None
    try:
        data_url = _encode_image_data_url(image_path)
        hint = f"群友附言：「{user_text}」。" if user_text.strip() else ""
        payload = {
            "model": "glm-4.1v-thinking-flash",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {
                            "type": "text",
                            "text": (
                                f"{hint}请客观精准地提取这张图里的全部关键文字、数字、时间戳、ID、"
                                "战绩KDA/出装、截图来源或明显的P图/拼接破绽，控制在250字以内，不要寒暄。"
                            ),
                        },
                    ],
                }
            ],
            "max_tokens": 1024,
        }
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.ZHIPU_API_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=22) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        msg = (body.get("choices") or [{}])[0].get("message") or {}
        cleaned = strip_think(msg.get("content") or "")
        if cleaned:
            logger.info("👁️ 智谱高精看图完成 (%d字): %s", len(cleaned), cleaned[:90])
            return cleaned
    except Exception as exc:
        logger.warning("智谱看图调用失败，将走备用视觉通道: %s", exc)
    return None


_PERSONA_LEAK_WORDS = ("塔菲", "喵")
_DUP_PUNCT_RE = re.compile(r"([，。！？、~～]{1})\1+")


def _strip_persona_leaks(text: str) -> str:
    """出口兜底清理早期历史里残留的卖萌串戏词。"""
    if not text:
        return text
    out = text
    for word in _PERSONA_LEAK_WORDS:
        if word in out:
            out = out.replace(word, "")
    if out == text:
        return text
    out = _DUP_PUNCT_RE.sub(r"\1", out)
    bubbles = [part.strip().strip("，、~～ ") for part in out.split("||")]
    return "||".join(b for b in bubbles if b)


def _build_turn_context(
    recalled_memory: str = "",
    matched_slang: str = "",
    diary_note: str = "",
    bg_note: str = "",
    job_report: str = "",
    l3_notes: list[str] | None = None,
) -> str:
    """本轮动态上下文（时间/后台任务/旧事/黑话），挂在最后一条 user 消息前以保护 system 前缀缓存。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extra_blocks: list[str] = []
    if job_report:
        extra_blocks.append(job_report)
    if l3_notes:
        note_chars = tuning.get_int("L3_NOTE_MAX_CHARS", 200)
        bullets = "\n".join(f"- {n[:note_chars]}" for n in l3_notes)
        extra_blocks.append(
            "【你自己知道的事】以下是你私下读过或查过的群内链接/资料摘要：\n"
            f"{bullets}\n"
            "（别人聊起时直接拿里面的事实接话或核查，没人问就别主动背诵。）"
        )
    if bg_note:
        extra_blocks.append(bg_note)
    if recalled_memory:
        extra_blocks.append(recalled_memory)
    if diary_note:
        extra_blocks.append(diary_note)
    if matched_slang:
        extra_blocks.append(matched_slang)
    parts = [f"【当前时间】{now_str}（Asia/Shanghai）"]
    if extra_blocks:
        parts.append("\n".join(extra_blocks))
    return "\n".join(parts) + "\n"


_WXID_PREFIX_RE = re.compile(r"^wxid_[0-9a-zA-Z_\-]{4,}:\s*")
_XML_HEAD_RE = re.compile(r"<\?xml|&lt;\?xml|<msg>|<appmsg")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_ESC_TITLE_RE = re.compile(r"&lt;title&gt;(.*?)&lt;/title&gt;", re.S)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_TAG_RE = re.compile(r"<[^>]{1,600}>")
_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&"))


def _clean_l3_content(raw: str, limit: int = 260) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = _WXID_PREFIX_RE.sub("", s)
    if _XML_HEAD_RE.search(s):
        m = _TITLE_RE.search(s) or _ESC_TITLE_RE.search(s)
        title = ""
        if m:
            title = _CDATA_RE.sub(r"\1", m.group(1))
            title = _TAG_RE.sub("", title)
            for a, b in _ENTITIES:
                title = title.replace(a, b)
            title = " ".join(title.split())[:60]
        s = f"[卡片] {title}" if title else "[卡片消息]"
    else:
        s = _TAG_RE.sub("", s)
    s = " ".join(s.split())
    return s[:limit]


def _select_l3_history(recent_history: list[dict], sender: str, text: str) -> list[dict]:
    items = list(recent_history or [])
    if (
        items
        and items[-1].get("role") == "user"
        and items[-1].get("sender") == sender
        and (items[-1].get("content") or "").strip() == text.strip()
    ):
        items = items[:-1]

    if not settings.L3_INJECT_ALL:
        limit = settings.L3_HISTORY_LIMIT
        return items[-limit:] if limit else []

    budget = settings.L3_INJECT_MAX_TOKENS
    picked: list[dict] = []
    used = 0
    for item in reversed(items):
        content = _clean_l3_content(item.get("content") or "")
        if not content and item.get("image_path"):
            content = "[发了一张图片]"
        if not content:
            continue
        cost = estimate_tokens(content) + 6
        if picked and used + cost > budget:
            break
        picked.append({**item, "content": content})
        used += cost
    picked.reverse()
    return picked


def _looks_like_param_rejection(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) not in (400, 422):
        return False
    msg = str(exc).lower()
    return any(k in msg for k in ("thinking", "unknown", "unrecognized", "unsupported", "invalid", "extra_body"))


class DaFeiYuBrain:
    def __init__(self) -> None:
        self._chat_client: OpenAI | None = None
        self._vision_client: OpenAI | None = None
        self._thinking_off_supported = True

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

    def _create(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        thinking: bool = False,
        **kwargs,
    ):
        client = self._get_chat_client()
        params: dict = {
            "model": settings.DEEPSEEK_CHAT_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if thinking:
            return client.chat.completions.create(**params)
        if self._thinking_off_supported:
            try:
                return client.chat.completions.create(
                    **params, extra_body={"thinking": {"type": "disabled"}}
                )
            except Exception as exc:
                if not _looks_like_param_rejection(exc):
                    raise
                self._thinking_off_supported = False
                logger.warning("该网关不支持关闭思考，已降级为加大 max_tokens：%s", exc)
        params["max_tokens"] = max(max_tokens, 1024)
        return client.chat.completions.create(**params)

    def _parse_mood(self, raw_text: str, default_mood: str = "smug") -> tuple[str, str, str]:
        match = MOOD_TAG_PATTERN.search(raw_text)
        mood = match.group(1).lower() if match else default_mood
        caption = (match.group(2) or "").strip() if match else ""
        cleaned = MOOD_TAG_PATTERN.sub("", raw_text).strip()
        cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"^#{1,4}\s*", "", cleaned, flags=re.M)
        cleaned = cleaned.replace("`", "")
        return _strip_persona_leaks(cleaned).strip(), mood, caption

    def summarize_image(self, image_path: Path) -> str | None:
        """后台静默概括图片内容写回 SQLite 历史，供后续追问或翻旧账使用。"""
        if not image_path.is_file():
            return None
        ask = "请用一两句简洁客观的中文概括图片核心内容与关键文字/数据"
        desc = _inspect_image_with_zhipu(image_path, ask) if settings.ZHIPU_API_KEY else None
        if not desc and settings.has_valid_api_key:
            try:
                data_url = _encode_image_data_url(image_path)
                resp = self._get_vision_client().chat.completions.create(
                    model=settings.DEEPSEEK_VISION_MODEL,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": ask + "（只输出描述，不要寒暄）"},
                                {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                            ],
                        }
                    ],
                    max_tokens=260,
                )
                body = " ".join(strip_think(resp.choices[0].message.content or "").split()).strip()
                if body:
                    desc = body
            except Exception as exc:
                logger.warning("原生视觉补图片摘要失败: %s", exc)
        if not desc:
            return None
        return re.sub(r"\s+", " ", desc).strip()[:180]

    def inspect_image_detail(self, image_path: Path, question: str = "") -> str:
        """深度查看历史图片细节。"""
        if not image_path.is_file():
            return "图片文件不存在或已被清理"
        desc = _inspect_image_with_zhipu(image_path, question)
        if desc:
            return desc
        if not settings.has_valid_api_key:
            return "未配置视觉模型 API Key，无法识别历史图片"
        try:
            prompt = question.strip() or "请用简洁客观的中文提取这张图里的关键文字、数字、截图细节与异常点"
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
            body = " ".join(strip_think(resp.choices[0].message.content or "").split()).strip()
            return body[:1200] or "未能识别出图片内容"
        except Exception as exc:
            return f"查看历史图片失败: {exc}"

    def reply(
        self,
        sender: str,
        text: str,
        recent_history: list[dict],
        image_path: Path | None = None,
        group_id: str = "",
        image_sender: str = "",
        interest_hint: str = "",
        bg_note: str = "",
        job_report: str = "",
        status: PetStatus | None = None,
        user_affinity: dict | None = None,
    ) -> BrainReply:
        if not settings.has_valid_api_key:
            demo = "还没配 DEEPSEEK_API_KEY||去 .env 填完就能在线核查锐评了"
            b = split_human_bubbles(demo)
            return BrainReply(text="\n".join(b), bubbles=b, mood="smug", tokens_used=0, used_vision=bool(image_path))

        recalled = memory_store.render_context(text, group_id=group_id)
        metrics.record("l1_recall", group_id, hit=bool(recalled))
        recalled_memory = recalled if tuning.get_int("RECALL_INJECT_ON", 1) else ""
        diary_note = (
            memory_store.render_diary_context(group_id)
            if (settings.MEMORY_DIARY_INJECT and tuning.get_int("DIARY_INJECT_ON", 0))
            else ""
        )
        matched_slang = slang_book.render_matched(group_id, text)
        l3_notes = (
            l3_inject.load_notes(group_id, limit=tuning.get_int("L3_NOTE_MAX_ITEMS", 5))
            if group_id and tuning.get_int("L3_INJECT_ON", 1)
            else []
        )
        turn_context = _build_turn_context(
            recalled_memory=recalled_memory,
            matched_slang=matched_slang,
            diary_note=diary_note,
            bg_note=bg_note,
            job_report=job_report,
            l3_notes=l3_notes,
        )

        # system 保持纯静态角色卡，最大化命中 DeepSeek 前缀缓存
        messages: list[dict] = [{"role": "system", "content": load_card()}]

        for item in _select_l3_history(recent_history, sender, text):
            role = "assistant" if item["role"] == "assistant" else "user"
            prefix = "" if role == "assistant" else f"[{item['sender']}]: "
            content = item["content"] or ("[发了一张图片]" if item.get("image_path") else "")
            if content:
                messages.append({"role": role, "content": f"{prefix}{content}"})

        vision_summary = ""
        pre_searched_block = ""
        if image_path is not None and image_path.is_file():
            owner_desc = (
                f"[{sender}] 正在询问 [{image_sender}] 之前发的图片"
                if (image_sender and image_sender != sender)
                else f"[{sender}] 发了张图"
            )
            zhipu_vision_desc = _inspect_image_with_zhipu(image_path, text)
            # 若发图同时带了「这是真的吗/什么梗」等核查词，顺手用提取出的文字做一次前置搜索！
            if (
                zhipu_vision_desc
                and settings.ENABLE_WEB_SEARCH
                and any(cue in text for cue in PRE_SEARCH_CUES)
            ):
                search_q = f"{text} {zhipu_vision_desc[:60]}".strip()
                logger.info("⚡ 看图联动联网核查: %s", search_q[:50])
                pre_searched_block = web_search(search_q, max_results=4)

            if zhipu_vision_desc:
                vision_summary = re.sub(r"\s+", " ", zhipu_vision_desc).strip()[
                    : tuning.get_int("VISION_SUMMARY_CHARS", 220)
                ]
                search_addon = (
                    f"\n\n【联网交叉核查结果】\n{pre_searched_block}\n（请结合图内细节与搜索事实给出定性判断，查无实据直说。）"
                    if pre_searched_block
                    else ""
                )
                user_msg = (
                    f"{owner_desc}"
                    + (f"并说：{text}" if text.strip() else "")
                    + f"\n\n（图内提取细节：\n{zhipu_vision_desc}\n请直接以你的 Grok 锐评口吻给出结论或吐槽，别说“根据图片描述”）"
                    + search_addon
                )
                messages.append({"role": "user", "content": user_msg})
                client = self._get_chat_client()
                model = settings.DEEPSEEK_CHAT_MODEL
                used_vision = True
            else:
                user_prompt = (
                    f"{owner_desc}并问：{text}\n（请仔细辨认图里所有文字、数字与破绽，直接直球回答或锐评，禁用 Markdown）"
                    if text.strip()
                    else f"{owner_desc}。请看清图里的文字和细节，简短犀利地锐评一句（别瞎编图里没有的内容，禁用 Markdown）。"
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
            if settings.ENABLE_WEB_SEARCH and not interest_hint and any(cue in text for cue in PRE_SEARCH_CUES):
                logger.info("⚡ Grok 前置高速联网核查: %s", text[:45])
                pre_searched_block = web_search(text, max_results=4)

            if pre_searched_block:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[{sender}]: {text}\n\n"
                            f"（系统已前置联网检索。请严格依据以下搜索结果中的事实、时间线与出处，用利落直球的口吻定性回答或拆穿槽点；"
                            f"若搜索结果不足以证实，直说目前没查到可靠信源，严禁脑补编造：\n{pre_searched_block}）"
                        ),
                    }
                )
            elif interest_hint:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[{sender}]: {text}\n\n"
                            f"【任务：半主动插嘴锐评】群里没人点名你，但这个话题踩中了你的兴趣点（切入角：{interest_hint}）。\n"
                            f"请冷不丁插 1 句（最多 2 句）简短犀利的吐槽、纠偏或补充：\n"
                            f"- 直奔要点，不寒暄、不总结、不用 Markdown、不端架子；\n"
                            f"- 若涉及具体事实且你不确定，可先调 `web_search` 查实再开口；\n"
                            f"- 对事不对人，不人身攻击外貌/家人/地域。"
                        ),
                    }
                )
            elif job_report:
                messages.append({"role": "user", "content": text})
            else:
                messages.append({"role": "user", "content": f"[{sender}]: {text}"})
            client = self._get_chat_client()
            model = settings.DEEPSEEK_CHAT_MODEL
            used_vision = False

        if turn_context:
            for msg in reversed(messages):
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    msg["content"] = [{"type": "text", "text": turn_context}] + content
                elif isinstance(content, str):
                    msg["content"] = turn_context + content
                break

        used_search = bool(pre_searched_block)
        total_tokens = 0
        try:
            if job_report:
                temp, freq_pen, pres_pen = 0.65, 0.25, 0.15
                max_tokens = 600
            elif used_search or used_vision:
                temp, freq_pen, pres_pen = 0.5, 0.15, 0.0
                max_tokens = 500
            elif interest_hint:
                temp, freq_pen, pres_pen = 0.8, 0.35, 0.3
                max_tokens = 200
            else:
                temp, freq_pen, pres_pen = 0.85, 0.35, 0.3
                max_tokens = 380

            available_tools = (
                list(SEARCH_TOOLS)
                if (settings.ENABLE_WEB_SEARCH and not used_search)
                else [t for t in SEARCH_TOOLS if t["function"]["name"] != "web_search"]
            )
            if job_report:
                available_tools = []
            elif interest_hint:
                # 兴趣插嘴只保留 web_search（方便插嘴前顺手核实真假），不触发改日程/发图
                available_tools = (
                    [t for t in SEARCH_TOOLS if t["function"]["name"] == "web_search"]
                    if settings.ENABLE_WEB_SEARCH
                    else []
                )

            kwargs: dict = {
                "model": model,
                "messages": messages,
                "temperature": temp,
                "frequency_penalty": freq_pen,
                "presence_penalty": pres_pen,
                "max_tokens": max_tokens,
            }
            if not (used_vision and model == settings.DEEPSEEK_VISION_MODEL and isinstance(messages[-1]["content"], list)) and available_tools:
                kwargs["tools"] = available_tools
                kwargs["tool_choice"] = "auto"

            if self._thinking_off_supported:
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

            attachment_path: Path | None = None
            raw_content = ""

            for _step in range(3):
                try:
                    resp = client.chat.completions.create(**kwargs)
                except Exception as call_exc:
                    if kwargs.get("extra_body") and _looks_like_param_rejection(call_exc):
                        kwargs.pop("extra_body", None)
                        self._thinking_off_supported = False
                        kwargs["max_tokens"] = max(int(kwargs.get("max_tokens") or 0), 1024)
                        resp = client.chat.completions.create(**kwargs)
                    else:
                        raise

                total_tokens += getattr(resp.usage, "total_tokens", 0) or 0
                choice = resp.choices[0]
                msg_obj = choice.message
                tool_calls = getattr(msg_obj, "tool_calls", None)
                if not tool_calls:
                    raw_content = strip_think(msg_obj.content or "").strip()
                    break

                messages.append(msg_obj.model_dump(exclude_none=True))
                for tc in tool_calls[:3]:
                    fn_name = getattr(tc.function, "name", "")
                    fn_args_raw = getattr(tc.function, "arguments", "{}")
                    try:
                        args = json.loads(fn_args_raw)
                    except json.JSONDecodeError:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": f"工具 {fn_name} 参数 JSON 解析失败，请精简重试",
                            }
                        )
                        continue

                    if fn_name == "web_search":
                        used_search = True
                        q = str(args.get("query") or text)
                        logger.info("🌐 Grok 自主联网搜索: %s", q)
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
                    elif fn_name in ("schedule", "add_schedule", "list_schedules", "delete_schedule"):
                        act = str(args.get("action") or "").strip().lower()
                        if fn_name == "add_schedule":
                            act = "add"
                        elif fn_name == "list_schedules":
                            act = "list"
                        elif fn_name == "delete_schedule":
                            act = "delete"
                        if act == "list":
                            tasks = scheduler.list_tasks(group_id=group_id)
                            tool_out = (
                                "\n".join(f"#{t['id']} [{t['desc']}|{t['mode']}] {t['content']}" for t in tasks)
                                if tasks
                                else "当前群里没有任何定时任务"
                            )
                        elif act == "delete":
                            tid = str(args.get("task_id") or "").strip()
                            _, tool_out = scheduler.delete_task(tid) if tid else (False, "缺少 task_id")
                        else:
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
                    elif fn_name in ("send_image", "search_and_send_image", "download_image_from_url"):
                        u = str(args.get("url") or "").strip()
                        q = str(args.get("query") or "").strip()
                        if fn_name == "download_image_from_url" or (u and not q):
                            fpath, tool_out = download_image_url(u, label="urlimg")
                        else:
                            fpath, tool_out = search_and_download_image(q)
                        if fpath and fpath.is_file() and fpath.stat().st_size > 0:
                            attachment_path = fpath
                            tool_out = f"{tool_out}。图片已加入待发送附件，请简短告知群友。"
                    else:
                        tool_out = "未知工具"

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(tool_out),
                        }
                    )

                kwargs["messages"] = messages
                kwargs["temperature"] = 0.5
                kwargs["frequency_penalty"] = 0.1
                kwargs["presence_penalty"] = 0.0
                kwargs["max_tokens"] = 600
            else:
                raw_content = "刚查完一轮，结论在上面了"

            cleaned, mood, sticker_caption = self._parse_mood(raw_content, "smug")
            bubbles = split_human_bubbles(cleaned or "信号抖了一下，你再发一遍？")
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
            hint = f"看图接口报错（{err_msg[:90]}）" if used_vision else f"接口请求异常（{err_msg[:90]}）"
            return BrainReply(text=hint, bubbles=[hint], mood="sleepy", tokens_used=total_tokens, used_vision=used_vision)

    def judge_and_chime_in(self, recent_history: list[dict], group_id: str = "") -> BrainReply | None:
        """
        Grok 半主动群聊热议补刀：群里多人聊嗨、争论真假或集体吐槽时，判断有无一针见血的切入点。
        没有高质量切入点就严格返回 None（宁缺毋滥）。
        """
        if not settings.has_valid_api_key or not recent_history:
            return None

        transcript_lines = []
        for item in recent_history[-8:]:
            content = (item.get("content") or "").strip()
            if content:
                who = "我" if item.get("role") == "assistant" else item.get("sender", "群友")
                transcript_lines.append(f"[{who}]: {content[:160]}")
        if len(transcript_lines) < 3:
            return None

        transcript = "\n".join(transcript_lines)
        recalled = memory_store.render_context(transcript[-180:], group_id=group_id) if group_id else ""
        mem_block = f"\n{recalled}\n" if recalled else ""

        prompt = (
            f"你是微信群里的「{settings.BOT_NAME}」（定位类似 @grok：机智毒舌、反套话、擅长拆穿漏洞与冷幽默补刀）。\n"
            f"下面是群友刚才的热议记录（他们没点名你）：\n{transcript}\n"
            f"{mem_block}\n"
            "请判断这里面有没有【明显事实漏洞/认知误区/前后打脸/极好笑的翻车槽点】值得你冷不丁插一嘴：\n"
            "- 如果是私人约时间、普通寒暄、正经工作通知、或者你插嘴只能说出正确的废话，请**只输出** `[PASS]`。\n"
            "- 只有当你能给出 **一针见血的纠偏或神补刀** 时，直接输出那 **1 句话**（15~45字，纯文本无 Markdown，不要寒暄铺垫）。"
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.75,
                max_tokens=140,
            )
            raw = strip_think(resp.choices[0].message.content or "").strip()
            tokens_used = getattr(resp.usage, "total_tokens", 0) or 0
            if not raw or "[PASS]" in raw.upper() or raw.upper() == "PASS":
                return None
            cleaned, mood, sticker_caption = self._parse_mood(raw, "smug")
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
            logger.warning("半主动锐评插嘴异常: %s", exc)
            return None

    def is_talking_to_or_about_me(
        self,
        sender: str,
        text: str,
        recent_history: list[dict],
        at_others: bool = False,
    ) -> bool:
        """L2 上下文追认（只输出 YES / NO）。"""
        if not settings.has_valid_api_key or not text.strip():
            return False

        ctx_lines: list[str] = []
        for m in recent_history:
            content = " ".join((m.get("content") or "").split())
            if not content:
                continue
            who = "我" if m.get("role") == "assistant" else (m.get("sender") or "群友")
            ctx_lines.append(f"[{who}]: {content[:200]}")
        ctx = "\n".join(ctx_lines) if ctx_lines else "（空）"
        probe = text.strip()[:300]

        extra = tuning.judge_extra("l2")
        extra_block = f"\n补充规则：\n{extra}\n" if extra else ""

        prompt = (
            f"你在判断一条群聊消息是不是冲着「{settings.BOT_NAME}」（下文里的「我」）说的。\n\n"
            f"我刚说完话之后的群聊：\n{ctx}\n\n"
            f"待判断的新消息 [{sender}]：{probe}\n"
            f"这条消息同时 @ 了其他人：{'是' if at_others else '否'}\n\n"
            "满足以下任一才输出 YES：\n"
            "1) 在回应、反驳、追问或核实我刚才说的内容；\n"
            "2) 上下文中只有我刚说过话，这句明显接着我的话往下聊。\n"
            "其余（跟其他群友互聊、@别人、另起无关话题、纯表情感叹）一律输出 NO。\n"
            f"{extra_block}\n"
            "只输出 YES 或 NO。"
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=24,
            )
            raw = (resp.choices[0].message.content or "").strip().upper().lstrip("\"'*` ")
            logger.info("🧭 L2 追认判定 [%s] @他人=%s: %s -> %s", sender, at_others, text[:30], raw[:8])
            return raw.startswith("YES")
        except Exception as exc:
            logger.warning("L2 追认判定异常: %s", exc)
            return False

    def judge_tool_need(
        self,
        sender: str,
        text: str,
        recent_history: list[dict] | None = None,
        link_urls: list[str] | None = None,
    ) -> dict:
        """工具路由判定：区分普通问答/核查（由 Grok 当场秒回）与重型产出/读链接任务（派后台沙箱）。"""
        is_link_mode = bool(link_urls)
        urls = list(link_urls or [])

        def pack(need: bool, task: str, reason: str, kind: str = "") -> dict:
            return {
                "need": need,
                "task": task,
                "reason": reason,
                "kind": kind or ("link" if is_link_mode else "work"),
                "urls": urls,
            }

        body = (text or "").strip()
        if not body or not settings.has_valid_api_key:
            return pack(False, "", "")

        ctx_lines: list[str] = []
        for m in (recent_history or [])[-6:]:
            content = " ".join((m.get("content") or "").split())
            if not content:
                continue
            who = "我" if m.get("role") == "assistant" else (m.get("sender") or "群友")
            ctx_lines.append(f"[{who}]: {content[:100]}")
        ctx = "\n".join(ctx_lines) if ctx_lines else "（空）"
        probe = body[:300]

        extra = tuning.judge_extra("tool_router")
        extra_block = f"\n补充偏好（不得推翻上述禁止项）：\n{extra}\n" if extra else ""
        url_block = "【消息内链接】\n" + "\n".join(f"- {u}" for u in urls[:3]) + "\n\n" if is_link_mode else ""

        if is_link_mode:
            criteria = (
                "判断标准（链接消息）：\n"
                "- kind=link 且 need=true：群友分享/转发网页或文章链接，后台静默读懂后留摘要供后续讨论。\n"
                "- kind=work 且 need=true：明确要求基于该链接生成文件、表格、代码或导出报告。\n"
                "- need=false：图片/安装包直链、垃圾广告、或要求修改大肥鱼自身项目代码/配置。\n"
            )
            task_rule = (
                '只输出一行 JSON：{"need": true或false, "kind": "link"或"work", '
                '"task": "任务说明(带上原URL, ≤120字)", "reason": "≤15字理由"}'
            )
        else:
            criteria = (
                "判断标准：\n"
                "- need=true（需后台沙箱干重活）：明确要求写代码/脚本、生成文档/表格/HTML文件、执行复杂工程分析或批量抓取导出。\n"
                "- need=false（由主脑当场回答）：事实核查（「这是真的吗」）、新闻搜索、观点锐评、看图、闲聊吐槽、定闹钟提醒、"
                "或要求修改大肥鱼自身源码/人设/配置（沙箱禁止改项目自身）。拿不准一律 need=false。\n"
            )
            task_rule = (
                '只输出一行 JSON：{"need": true或false, '
                '"task": "直接可执行的任务说明(保留全部细节, ≤120字)", "reason": "≤15字理由"}'
            )

        prompt = (
            "你是后台沙箱派活判断器。\n\n"
            f"【最近群聊】\n{ctx}\n\n"
            f"【当前消息】\n[{sender}]: {probe}\n\n"
            f"{url_block}{criteria}{extra_block}\n{task_rule}"
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=260,
            )
            raw = strip_think(resp.choices[0].message.content or "")
        except Exception as exc:
            logger.warning("工具路由判定失败: %s", exc)
            return pack(False, "", "调用失败")

        matched = re.search(r"\{.*\}", raw, re.S)
        if not matched:
            return pack(False, "", "解析失败")
        try:
            data = json.loads(matched.group(0))
        except json.JSONDecodeError:
            return pack(False, "", "JSON非法")

        task = " ".join(str(data.get("task") or "").split())[:200]
        need = bool(data.get("need")) and bool(task)
        reason = " ".join(str(data.get("reason") or "").split())[:30]
        if not need:
            return pack(False, "", reason)
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in ("link", "work"):
            kind = "link" if is_link_mode else "work"
        logger.info("🧰 工具路由判定：%s → %s（%s）", kind, task[:60], reason)
        return pack(True, task, reason, kind)

    def judge_topic_interest(
        self,
        sender: str,
        text: str,
        recent_history: list[dict],
        gap_desc: str = "",
        at_others: bool = False,
        multi_speaker: bool = False,
    ) -> str:
        """L2.5 兴趣雷达：群里安静一阵后重开话题时，对照人格卡「我的兴趣区」判断是否值得插一嘴锐评/核查。"""
        if not settings.has_valid_api_key:
            return ""
        body = (text or "").strip()
        if len(body) < 4:
            return ""

        ctx_lines: list[str] = []
        for m in recent_history:
            content = " ".join((m.get("content") or "").split())
            if not content:
                continue
            who = "我" if m.get("role") == "assistant" else (m.get("sender") or "群友")
            ctx_lines.append(f"[{who}]: {content[:120]}")
        ctx = "\n".join(ctx_lines[-6:]) if ctx_lines else "（无）"
        probe = body[:300]

        opening = (
            f"群里安静了{gap_desc or '一阵'}后，几个人陆续说：\n{probe}\n"
            if multi_speaker
            else f"群里安静了{gap_desc or '一阵'}后，[{sender}] 开口说：{probe}\n"
        )
        zone = interest_zone()
        zone_block = f"【我的兴趣区】\n{zone}\n\n" if zone else ""
        extra = tuning.judge_extra("l2_5")
        extra_block = f"\n补充规则：\n{extra}\n\n" if extra else "\n"

        prompt = (
            f"你是群内锐评核查员「{settings.BOT_NAME}」的注意力雷达。\n"
            "判断下面这个新开的话题是否正踩在你的兴趣区上，且你有具体的槽点、事实核查点或游戏/科技见解可说。\n\n"
            + zone_block
            + opening
            + f"\n更早上下文：\n{ctx}\n\n"
            "评分标准（0~10 分）：\n"
            "- 8~10：正中兴趣区，且有明显的流言破绽可核查、翻车槽点可损、或具体游戏/科技梗可接。\n"
            "- 4~7：略沾边但没啥一针见血的话好说。\n"
            "- 0~3：私人行程约定、正经工作通知、纯打卡表情、敏感政治/疾病/丧事、或一对一私事。\n"
            + extra_block
            + "只输出一行：分数|切入点（≤15字；不接写「无」）"
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.25,
                max_tokens=40,
            )
            raw = (resp.choices[0].message.content or "").strip()
            line = raw.splitlines()[0] if raw else ""
            score_part, _, angle = line.partition("|")
            m_score = re.search(r"\d+(?:\.\d+)?", score_part)
            score = float(m_score.group()) if m_score else 0.0
            angle = angle.strip().strip("\"'「」 ")
            threshold = tuning.get_float(
                "TOPIC_INTEREST_MIN_SCORE", settings.TOPIC_INTEREST_MIN_SCORE
            ) * 10
            logger.info(
                "🔥 L2.5 兴趣雷达 [%s] %.1f/10 (阈值 %.1f) 切入点=%s | %s",
                sender, score, threshold, angle[:20] or "无", probe[:30],
            )
            if score < threshold or not angle or angle == "无":
                return ""
            return angle[:30]
        except Exception as exc:
            logger.warning("兴趣判定异常: %s", exc)
            return ""

    def summarize_l3_history(self, rows: list[dict], group_id: str = "") -> str:
        """L3 超限归档时将最旧的一批群聊压成 ≤200 字事实摘要存入 L1。"""
        if not settings.has_valid_api_key or not rows:
            return ""
        lines = []
        for r in rows:
            who = settings.BOT_NAME if r.get("role") == "assistant" else r.get("sender", "群友")
            body = " ".join(str(r.get("content") or "").split())[:120]
            if body:
                lines.append(f"[{who}]: {body}")
        if not lines:
            return ""
        prompt = (
            "请将以下旧群聊记录压缩成一段 200 字以内的客观摘要，只保留：\n"
            "1) 群友身份特征与常玩项目；2) 立过的 flag、黑历史与群内老梗；3) 明确达成的约定或结论。\n"
            "直接输出摘要正文，不要标题和废话。\n\n" + "\n".join(lines)
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.25,
                max_tokens=450,
            )
            return " ".join(strip_think(resp.choices[0].message.content or "").split())[:200]
        except Exception as exc:
            logger.warning("L3 压缩摘要失败: %s", exc)
            return ""

    def _material_lines(self, history: list[dict], max_chars: int = 120) -> list[str]:
        lines: list[str] = []
        for m in history or []:
            content = " ".join((m.get("content") or "").split())
            if content:
                lines.append(f"[{m.get('sender') or '群友'}]: {content[:max_chars]}")
        return lines

    def _extract_facts_chunk(self, lines: list[str], idx: int, total: int) -> list[dict]:
        prompt = (
            "从下面群聊片段中挑出最多 2 条值得长期记住的群友信息（无则只输出「无」）：\n"
            "- 群友特征、职业、常玩游戏/段位、立下的 flag 或翻车黑历史\n"
            "- 群内新梗、外号、黑话或明确约定\n"
            "禁记一次性闲聊、新闻天气、密码隐私，也别记机器人自己的话。\n"
            "每条一行：`内容（≤40字）|标签1,标签2`\n\n" + "\n".join(lines)
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.25,
                max_tokens=200,
            )
            raw = strip_think(resp.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("夜间 L1 提炼失败（%d/%d）: %s", idx, total, exc)
            return []
        if not raw or raw.startswith("无"):
            return []
        out: list[dict] = []
        for line in raw.splitlines():
            line = line.strip().lstrip("-•* \t")
            if not line or "|" not in line:
                continue
            text, _, tags_raw = line.partition("|")
            text = text.strip()[:120]
            if len(text) < 4:
                continue
            tags = [t.strip()[:16] for t in re.split(r"[,，、\s]+", tags_raw) if t.strip()][:4]
            out.append({"text": text, "tags": tags})
        return out

    def extract_daily_facts(
        self,
        recent_history: list[dict],
        group_id: str = "",
        max_items: int = 0,
    ) -> list[dict]:
        """轻量群友记忆沉淀：按天提炼群友黑历史与特征写入 L1 识海。"""
        if not settings.has_valid_api_key or not recent_history:
            return []
        max_items = max_items or settings.NIGHTLY_FACT_MAX
        lines = self._material_lines(recent_history)
        if not lines:
            return []
        chunk_size = max(20, settings.NIGHTLY_EXTRACT_CHUNK)
        chunks = [lines[i : i + chunk_size] for i in range(0, len(lines), chunk_size)]
        if len(chunks) > settings.NIGHTLY_EXTRACT_MAX_CHUNKS:
            keep = settings.NIGHTLY_EXTRACT_MAX_CHUNKS
            step = len(chunks) / keep
            chunks = [chunks[int(i * step)] for i in range(keep)]
        candidates: list[dict] = []
        for idx, chunk in enumerate(chunks, 1):
            for fact in self._extract_facts_chunk(chunk, idx, len(chunks)):
                if not any(SequenceMatcher(None, c["text"], fact["text"]).ratio() >= 0.7 for c in candidates):
                    candidates.append(fact)
        return candidates[:max_items]

    def write_daily_diary(
        self,
        recent_history: list[dict],
        status: PetStatus | None = None,
        group_id: str = "",
        day: str = "",
    ) -> bool:
        """生成一条简短犀利的群聊观察笔记（≤160字）。"""
        if not settings.has_valid_api_key or not recent_history:
            return False
        all_lines = self._material_lines(recent_history)
        if not all_lines:
            return False
        cap = settings.NIGHTLY_DIARY_LINES
        lines = (
            [all_lines[int(i * (len(all_lines) / cap))] for i in range(cap)]
            if len(all_lines) > cap
            else all_lines
        )
        target_day = day or datetime.now().strftime("%Y-%m-%d")
        prompt = (
            f"你是群里的锐评观察员「{settings.BOT_NAME}」。根据今天群聊记录，写一条 60~150 字的毒舌观察笔记，"
            "挑今天群里最离谱、最好笑或最有代表性的一件事记一笔。\n"
            "只输出一行，格式：`关键词|笔记正文`。\n\n" + "\n".join(lines)
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=260,
            )
            raw = strip_think(resp.choices[0].message.content or "").strip()
            if not raw:
                return False
            mood, body = raw.split("|", 1) if "|" in raw else ("观察", raw)
            return memory_store.write_diary(mood.strip()[:8], body.strip(), group_id=group_id, day=target_day)
        except Exception as exc:
            logger.warning("生成群聊观察笔记失败: %s", exc)
            return False


brain = DaFeiYuBrain()
