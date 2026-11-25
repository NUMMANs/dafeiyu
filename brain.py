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

# 命中这些迹象才把「主人专用电脑工具」挂上来。平时群聊根本用不到，
# 而 13 个 schema 的 JSON 每轮固定要吃掉约 1.3k tokens（占输入四成），
# 关掉思考省下来的钱不该在这里漏掉。宁可放宽一点，别让正经干活时找不到工具。
AGENT_TOOL_CUES = (
    "文件", "目录", "文件夹", "路径", "保存", "读取", "写入", "新建",
    "代码", "脚本", "运行", "执行", "命令", "终端", "powershell", "cmd",
    "python", "java", "node", "sql", "数据库", "装一下", "安装", "依赖",
    "报错", "bug", "调试", "编译",
    ".py", ".js", ".html", ".json", ".csv", ".md", ".txt", ".bat", ".ps1", ".exe",
    "进程", "内存", "cpu", "盘", "磁盘", "硬盘", "截图", "截屏", "桌面",
    "电脑", "本机", "系统", "配置", "日志",
    "重启", "更新", "拉取", "git", "部署", "上线", "改自己",
    "dsh", "后台任务", "派给",
    "抓取", "网页", "链接", "http",
)


def _needs_agent_tools(text: str) -> bool:
    """这句是不是在指挥我操作电脑/改自己。不是就只挂聊天用的轻量工具。"""
    low = (text or "").lower()
    return any(c in low for c in AGENT_TOOL_CUES)


SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜最新信息（新闻/天气/版本攻略/报错/不确定的实时事实）。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "记进长期记忆（群友特征/约定/黑历史/群梗）。只记值得记的，禁记密码证件银行卡。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "事实描述，≤120字"},
                    "level": {
                        "type": "string",
                        "enum": ["normal", "important"],
                        "description": "普通梗 normal；重要约定/人物特征 important",
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
            "description": "记群内黑话/外号/内部梗，以后提到就懂。",
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
            "description": "永久改自己的人设（长出新的习惯/口癖/态度，或群友建议且你觉得有意思）。",
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
                        "description": "remind=到点发文字；ai_task=到点让AI干活",
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
            "description": "重看本群历史里的某张图（「上面那张」「刚才截图」「第二张战绩图」）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "倒数第几张，1=最新，范围1~8，默认1"},
                    "question": {"type": "string", "description": "重点看什么（可选）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_image",
            "description": "找图发到群里：给 query 按关键词搜图；给 url 下直链图。别拒绝找图请求。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜图关键词，如「原神 刻晴 壁纸」"},
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


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.I | re.S)
_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>", re.I)
_THINK_ANY_RE = re.compile(r"</?(?:think|answer)\b[^>]*>", re.I)


def strip_think(text: str) -> str:
    """
    剥掉模型的思考草稿，只留最终正文。

    三种情况都要能处理（第二种原来会整段漏进正文）：
    1. 正常闭合 `<think>草稿</think>正式内容` → 删掉整块，留「正式内容」
    2. **未闭合** `<think>草稿……`（被 max_tokens 截断）→ 标签之后全是草稿，整段丢弃
    3. 孤立 `</think>`（缺开头）→ 只保留最后一个闭合标签之后的内容
    """
    if not text:
        return ""
    s = _THINK_BLOCK_RE.sub("", text)
    parts = _THINK_OPEN_RE.split(s)
    if len(parts) > 1:
        s = parts[0]
    if re.search(r"</think\s*>", s, flags=re.I):
        s = re.split(r"</think\s*>", s, flags=re.I)[-1]
    s = _THINK_ANY_RE.sub("", s)
    return s.strip()


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
        choice = (data.get("choices") or [{}])[0]
        raw = (choice.get("message") or {}).get("content") or ""
        finish = str(choice.get("finish_reason") or "")
        # 剥离思考块；未闭合时（被 max_tokens 截断）strip_think 会整段丢弃
        cleaned = " ".join(strip_think(raw).split()).strip()
        # 剥完什么都不剩 = 这一轮全是思考草稿，宁可回退去用原生视觉，也不能把草稿当摘要
        if not cleaned or len(cleaned) < 6 or _THINK_ANY_RE.search(cleaned):
            logger.warning(
                "智谱识图没给出正文（finish_reason=%s，原始 %d 字全是思考），回退 DeepSeek 原生看图",
                finish,
                len(raw),
            )
            return None
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
    diary_note: str = "",
) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    card_text = load_card()
    # 【实时状态】块已按需求移除（饱食度/段位/好感度不再注入 system）。
    # 这三个参数保留在签名里只是为了让调用方不用改，函数内不再使用。
    extra_blocks = []
    if recalled_memory:
        extra_blocks.append(recalled_memory)
    if diary_note:
        extra_blocks.append(diary_note)
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
        f"{card_text}\n"
    )


def _looks_like_param_rejection(exc: Exception) -> bool:
    """网关/模型不认识 thinking 参数时通常返回 400/422 并在报文里点名该参数"""
    if getattr(exc, "status_code", None) not in (400, 422):
        return False
    msg = str(exc).lower()
    return any(k in msg for k in ("thinking", "unknown", "unrecognized", "unsupported", "invalid", "extra_body"))


class DaFeiYuBrain:
    def __init__(self) -> None:
        self._chat_client: OpenAI | None = None
        self._vision_client: OpenAI | None = None
        # 当前网关是否支持 thinking={"type":"disabled"}（首次被拒后置 False，全程走降级路径）
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
        """
        轻量任务（L2 判定 / L2.5 吐槽雷达 / 日记 / 冒泡）必须显式关掉思考链。

        deepseek-flash 是带思考的模型，reasoning token 和正文共用 max_tokens。
        实测写日记时 max_tokens=220 的请求：220 个 token 全被 reasoning 吃掉，
        finish_reason=length、content 为空串，日记因此永远写不出来。
        关掉思考后同样内容只要 6 个 token：又便宜又不会空。

        网关不认识该参数时自动降级为「加大 max_tokens 给思考留空间」。
        """
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
            body = strip_think(resp.choices[0].message.content or "")
            body = " ".join(body.split()).strip()
            return body[:1200] or "未能识别出图片内容"
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
        roast_hint: str = "",
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
        # 只用这句话本身去召回：把 sender 也拼进查询会让「这条记忆的 tags 里有这个人」
        # 变成命中条件，于是这个人以后说的每一句（哪怕只有一个「行」）都会把它召回。
        recalled_memory = memory_store.render_context(text, group_id=group_id)
        diary_note = (
            memory_store.render_diary_context(group_id) if settings.MEMORY_DIARY_INJECT else ""
        )
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
            diary_note=diary_note,
        )
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # L3 会话层：带最近 N 条群聊上下文（默认 20，L3_HISTORY_LIMIT 可调），
        # 让大肥鱼真正跟得上群里聊的每一句。这一层是四层里唯一可以丢的：
        # 超过 L3_CONTEXT_LIMIT_TOKENS 时由 bot_core._compress_l3 每天 0 点压缩。
        l3_limit = settings.L3_HISTORY_LIMIT
        history_slice = recent_history[-(l3_limit + 1) :] if l3_limit else []
        if history_slice and history_slice[-1].get("role") == "user" and history_slice[-1].get("sender") == sender and (history_slice[-1].get("content") or "").strip() == text.strip():
            history_slice = history_slice[:-1]
        for item in (history_slice[-l3_limit:] if l3_limit else []):
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
            if settings.ENABLE_WEB_SEARCH and not roast_hint and any(cue in text for cue in PRE_SEARCH_CUES):
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
                if roast_hint:
                    # L2.5 冷场吐槽任务：明确告知这是「吐槽需求」，而不是有人在问我
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"[{sender}]: {text}\n\n"
                                f"【任务：吐槽】群里刚冷场了一阵，是 {sender} 重新开了这个话题，"
                                f"没人叫你，也不是在问你问题。\n"
                                f"现在请你以群友身份，就这个话题接一句吐槽/损一句/接个梗：\n"
                                f"- 可下嘴的点：{roast_hint}\n"
                                f"- 只发 1~2 句，短、准、有包袱，像真人随口接话，别铺垫别解释。\n"
                                f"- 不要问好、不要说“这个问题很好”、不要分析、不要总结、不要用 Markdown、"
                                f"不要自称 AI 或助手。\n"
                                f"- 吐槽对事不对人：可以损这事离谱，别攻击长相、家人、地域、职业，别碰政治和疾病。"
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
            # 吐槽任务只发 1~2 句，160 足够，避免 owner 拿到 4096 后开始长篇大论
            if roast_hint:
                max_tokens = 160
            else:
                max_tokens = 4096 if (not used_vision and is_owner(sender)) else (800 if used_vision else 260)
            kwargs: dict = {
                "model": model,
                "messages": messages,
                "temperature": temp,
                "frequency_penalty": freq_pen,
                "presence_penalty": pres_pen,
                "max_tokens": max_tokens,
            }
            # 挂载工具列表（普通工具 + 主人权限下的桌面 Agent 电脑操纵工具）
            available_tools = list(SEARCH_TOOLS) if (settings.ENABLE_WEB_SEARCH and not used_search) else [
                t for t in SEARCH_TOOLS if t["function"]["name"] != "web_search"
            ]
            if is_owner(sender) and _needs_agent_tools(text):
                available_tools.extend(AGENT_TOOL_SCHEMAS)
            if roast_hint:
                available_tools = []  # 吐槽是纯嘴活，挂工具只会误触发搜索/改文件

            if not used_vision and available_tools:
                kwargs["tools"] = available_tools
                kwargs["tool_choice"] = "auto"

            # 只有写代码/改文件这类任务才值得让模型先想；闲聊/吐槽/看图关掉思考链，
            # 既避免 260 上限被 reasoning 吃光导致回复变空，也省掉大量 token。
            if not is_code_or_file_task and self._thinking_off_supported:
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

            attachment_path: Path | None = None
            raw_content = ""

            # 最多支持 4 轮多步 Agent 工具链（如：列目录 -> 读文件 -> 执行脚本 -> 回复）
            for _step in range(4):
                try:
                    resp = client.chat.completions.create(**kwargs)
                except Exception as call_exc:
                    # 网关不认 thinking 参数：摘掉它并给思考留足空间，再试一次
                    if kwargs.get("extra_body") and _looks_like_param_rejection(call_exc):
                        kwargs.pop("extra_body", None)
                        self._thinking_off_supported = False
                        kwargs["max_tokens"] = max(int(kwargs.get("max_tokens") or 0), 1024)
                        logger.warning("该网关不支持关闭思考，已降级为加大 max_tokens：%s", call_exc)
                        resp = client.chat.completions.create(**kwargs)
                    else:
                        raise
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
                    elif fn_name in ("schedule", "add_schedule", "list_schedules", "delete_schedule"):
                        # 统一入口 schedule(action=...)；旧名字保留兼容模型的历史习惯
                        act = str(args.get("action") or "").strip().lower()
                        if fn_name == "add_schedule":
                            act = "add"
                        elif fn_name == "list_schedules":
                            act = "list"
                        elif fn_name == "delete_schedule":
                            act = "delete"
                        if act == "list":
                            tasks = scheduler.list_tasks(group_id=group_id)
                            if not tasks:
                                tool_out = "当前群里没有任何定时任务"
                            else:
                                tool_out = "\n".join(
                                    f"#{t['id']} [{t['desc']}|{t['mode']}] {t['content']}" for t in tasks
                                )
                        elif act == "delete":
                            tid = str(args.get("task_id") or "").strip()
                            _, tool_out = (
                                scheduler.delete_task(tid) if tid else (False, "缺少 task_id，删不掉")
                            )
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
                            logger.info("🌐 下载直链图片并发送: %s", u[:80])
                            fpath, tool_out = download_image_url(u, label="urlimg")
                            if fpath and fpath.is_file() and fpath.stat().st_size > 0:
                                attachment_path = fpath
                                tool_out = f"{tool_out}。图片已加入待发送附件，请直接简短告知群友。"
                        else:
                            logger.info("🖼️ 联网搜图并发送: %s", q)
                            fpath, tool_out = search_and_download_image(q)
                            if fpath and fpath.is_file() and fpath.stat().st_size > 0:
                                attachment_path = fpath
                                tool_out = f"{tool_out}。图片已加入待发送附件，请用一两句{voice_hint}告诉群友图给他们找来啦。"
                    elif fn_name in (
                        "run_shell",
                        "read_local_file",
                        "write_local_file",
                        "list_directory",
                        "open_url_or_app",
                        "take_screenshot",
                        "get_system_info",
                        "fetch_webpage",
                        "send_file",
                        "create_and_send_file",
                        "send_existing_file",
                        "self_ops",
                        "self_pull_and_restart",
                        "self_edit_code",
                        "self_restart",
                        "delegate_to_dsh",
                        "check_dsh_jobs",
                    ):
                        is_danger = False
                        danger_reasons: list[str] = []
                        # 合并后的新工具名要还原成安全策略认识的动作类型，
                        # 否则确认码卡片和路径风险检查都会认不出来
                        if fn_name == "send_file":
                            act_type = (
                                "create_and_send_file"
                                if (args.get("filename") and not args.get("path"))
                                else "send_existing_file"
                            )
                        elif fn_name == "self_ops":
                            act_type = (
                                "self_pull_and_restart"
                                if str(args.get("action") or "").strip().lower() == "update"
                                else "self_restart"
                            )
                        else:
                            act_type = fn_name
                        if is_owner(sender) and act_type in (
                            "run_shell",
                            "read_local_file",
                            "write_local_file",
                            "open_url_or_app",
                            "send_existing_file",
                            "self_edit_code",
                            "delegate_to_dsh",
                        ):
                            is_danger, danger_reasons = assess_danger(
                                act_type,
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
                                action_type=act_type,
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
                        elif fn_name in ("send_file", "create_and_send_file", "send_existing_file"):
                            fname = str(args.get("filename") or "").strip()
                            path_arg = str(args.get("path") or "").strip()
                            if act_type == "create_and_send_file":
                                logger.info("📄 Agent 生成并发送文件: %s", fname or "<empty>")
                                fpath, tool_out = create_and_send_file(
                                    fname, str(args.get("content") or "")
                                )
                            else:
                                logger.info("📎 Agent 发送已有文件: %s", path_arg)
                                fpath, tool_out = send_existing_file(path_arg)
                            if fpath and fpath.is_file() and fpath.stat().st_size > 0:
                                attachment_path = fpath
                        elif fn_name in ("self_ops", "self_pull_and_restart", "self_restart"):
                            if act_type == "self_pull_and_restart":
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
                            else:
                                reason = str(args.get("reason") or "主人要求重启")
                                logger.info("♻️ Agent 触发重启: %s", reason)
                                tool_out = request_restart(reason=reason, notify_group=group_id)
                        elif fn_name == "self_edit_code":
                            logger.info("🛠️ Agent 尝试自修改代码: %s", args.get("rel_path"))
                            tool_out = self_edit_code(
                                str(args.get("rel_path") or ""),
                                str(args.get("target") or ""),
                                str(args.get("replacement") or ""),
                            )
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
            resp = self._create(
                [{"role": "user", "content": prompt}],
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

    def is_talking_to_or_about_me(
        self,
        sender: str,
        text: str,
        recent_history: list[dict],
        at_others: bool = False,
    ) -> bool:
        """
        L2 上下文追认（只输出 YES / NO，单次约 300~500 tokens）：
        在我刚说完话之后的几条消息里，判断这句是不是冲着「我」来的。

        刻意不注入人格设定：
        判定只需要「谁是我 / 我上一句说了什么 / 现在这句是什么」三件事。
        喂完整人设（人格.md 约 936 tokens）会让模型转去扮演角色、吐不出 YES/NO，
        同时把单次输入抬到 1500 tokens 量级，实测等于把所有优化吃掉。
        """
        if not settings.has_valid_api_key or not text.strip():
            return False

        ctx_lines: list[str] = []
        for m in recent_history:
            content = " ".join((m.get("content") or "").split())
            if not content:
                continue
            if m.get("role") == "assistant":
                # 显式标出哪句是我说的，避免模型把「大肥鱼」当成第三个人
                ctx_lines.append(f"[我]: {content[:200]}")
            else:
                ctx_lines.append(f"[{m.get('sender') or '群友'}]: {content[:200]}")
        ctx = "\n".join(ctx_lines) if ctx_lines else "（空）"

        probe = text.strip()
        if len(probe) > 300:
            probe = probe[:300] + "…"

        prompt = (
            f"你在判断一条群聊消息是不是冲着机器人「{settings.BOT_NAME}」说的。"
            f"下文里的「我」都指这个机器人。\n\n"
            f"我刚说完话之后的群聊：\n{ctx}\n\n"
            f"待判断的新消息 [{sender}]：{probe}\n"
            f"这条新消息同时 @ 了群里其他人：{'是' if at_others else '否'}\n\n"
            f"满足以下任一才输出 YES：\n"
            f"1) 在回应、追问或点评我刚才说的内容；\n"
            f"2) 上下文中只有我刚说过话，这句明显是接着我的话往下说。\n\n"
            f"其余一律输出 NO，例如：跟其他群友互相聊天、明显在 @ 别人、开新话题、"
            f"自言自语、单纯发感叹或表情、在议论第三个人。\n\n"
            f"只输出 YES 或 NO，不要解释。"
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=24,
            )
            raw = (resp.choices[0].message.content or "").strip().upper().lstrip("\"'*` ")
            logger.info(
                "🧭 L2 追认判定 [%s] @他人=%s: %s -> %s",
                sender,
                at_others,
                text[:30],
                raw[:8],
            )
            return raw.startswith("YES")
        except Exception as exc:
            logger.warning("L2 追认判定异常: %s", exc)
            return False

    def judge_topic_roast(
        self,
        sender: str,
        text: str,
        recent_history: list[dict],
        gap_desc: str = "",
        at_others: bool = False,
        multi_speaker: bool = False,
    ) -> str:
        """
        L2.5 冷场重启话题的「吐槽雷达」（轻量判定，只输出一行结论）。

        场景：群里安静了很久，某人重新开口。此时既不是在叫我，也算不上接话茬，
        但如果他抛的话题够抽象/够有槽点，接一句会很有节目效果。

        输入刻意保持极简：不带人格、不带工具、只给「冷场多久 + 最近几句 + 这句」。
        输出格式 `SCORE|可吐槽的点`，SCORE 为 0~10 整数，由调用方按阈值决定是否接话。

        multi_speaker=True 表示 text 是这一波多人接话的合并探针（每行 `[昵称]: 内容`），
        此时不能沿用「[sender] 重新开口说」的说法，否则会把几个人的话当成一个人说的。

        返回：可吐槽的点（非空字符串）表示值得吐槽；空字符串表示不接话。
        """
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
        ctx = "\n".join(ctx_lines[-6:]) if ctx_lines else "（群里之前没有人说话）"

        probe = body if len(body) <= 300 else body[:300] + "…"

        if multi_speaker:
            opening = (
                f"这个群{('已经安静了' + gap_desc) if gap_desc else '刚刚冷场'}，"
                f"然后这几个人陆续开口（每行是谁说的都标在前面）：\n{probe}\n"
            )
        else:
            opening = (
                f"这个群{('已经安静了' + gap_desc) if gap_desc else '刚刚冷场'}，"
                f"现在 [{sender}] 重新开口说：{probe}\n"
            )

        prompt = (
            "你是群聊里的「吐槽雷达」，只负责判断一段群聊值不值得接梗吐槽，不负责真的吐槽。\n\n"
            + opening
            + (
                "（这条消息 @ 的是群里的其他人，不是叫你。"
                "@ 别人不算扣分，照常只看他抛的话题本身有没有槽点。）\n"
                if at_others
                else ""
            )
            + f"\n更早的几句（可能为空）：\n{ctx}\n\n"
            "评分标准（0~10 分）：\n"
            "- 8~10：有明显的槽点、反差、离谱、抽象、可接梗的具体事（吹牛、翻车、奇葩经历、逆天观点、被生活暴打）。\n"
            "- 4~7：有点意思但不好接，或者需要很多背景才懂。\n"
            "- 0~3：打招呼、问在不在、单纯陈述事实、发牢骚没内容、纯表情、广告、正事通知、涉政涉黄涉暴。\n\n"
            "另外，出现以下任一情况直接给 0 分：\n"
            "在问正经求助问题、在讨论别人的隐私或外貌、内容涉及政治/疾病/丧事、纯粹约时间见面的私事。\n"
            "如果这几句其实是几个人在聊各自不相干的事、或者已经变成一对一私聊，也给 0 分。\n\n"
            "输出格式（只输出一行，不要解释）：\n"
            "分数|可以吐槽的那个点（15 字以内，没槽点就写「无」）\n"
            "例如：9|他居然把鱼缸当显示器"
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=40,
            )
            raw = (resp.choices[0].message.content or "").strip()
            line = raw.splitlines()[0] if raw else ""
            score_part, _, angle = line.partition("|")
            m_score = re.search(r"\d+(?:\.\d+)?", score_part)
            score = float(m_score.group()) if m_score else 0.0
            angle = angle.strip().strip("\"'「」 ")
            threshold = settings.TOPIC_ROAST_MIN_SCORE * 10
            logger.info(
                "🔥 吐槽判定 [%s] %.1f/10 (阈值 %.0f) 槽点=%s | %s",
                sender, score, threshold, angle[:20] or "无", probe[:30],
            )
            if score < threshold or not angle or angle == "无":
                return ""
            return angle[:30]
        except Exception as exc:
            logger.warning("吐槽判定异常: %s", exc)
            return ""

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
            resp = self._create(
                [{"role": "user", "content": instruction}],
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

    def summarize_l3_history(self, rows: list[dict], group_id: str = "") -> str:
        """
        把一段即将从 L3 会话层删掉的旧聊天压成一段摘要（≤200 字）。

        L3 是可以丢的一层，但「丢了什么」得留下个影子：这段摘要会被写成一条
        important 记忆进 L1，以后还能被召回。压缩只处理最旧的一批消息，
        最近 L3_KEEP_RECENT 条永远不参与（见 bot_core._compress_l3）。
        """
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
            "下面是一段微信群聊记录（旧消息，即将被清理）。请压成一段 200 字以内的中文摘要，只保留：\n"
            "1) 谁是什么人、有什么特征；2) 聊过什么话题、达成了什么约定或结论；3) 出现过的梗/外号；\n"
            "4) 有明确结果的事（谁要做什么、谁不做什么）。\n"
            "不要逐条复述，不要评价，不要编造记录里没有的东西。直接输出摘要正文，不要标题。\n\n"
            + "\n".join(lines)
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=500,
            )
            return " ".join(strip_think(resp.choices[0].message.content or "").split())[:200]
        except Exception as exc:
            logger.warning("L3 会话压缩摘要失败: %s", exc)
            return ""

    def extract_daily_facts(self, recent_history: list[dict], group_id: str = "", max_items: int = 3) -> list[dict]:
        """
        夜间批量提炼 L1：从当天聊天里挑出最多 3 条「以后还用得上」的事实。

        为什么需要这一步：这套四层框架原本假设模型在对话里自己调 remember_fact，
        但群里每轮都是独立的一问一答，实测 3 天 980 条消息只写进去 3 条节点（0.3%），
        识海等于空的。夜间用一次轻量调用把当天值得留的东西补录，是最省 token 的补法。
        写入仍走 memory_store.remember（去重 + 每日 8 条上限 + 隐私过滤）。
        """
        if not settings.has_valid_api_key or not recent_history:
            return []
        lines = [f"[{m['sender']}]: {m['content']}" for m in recent_history[-40:] if m.get("content")]
        if not lines:
            return []
        prompt = (
            "下面是群聊片段。请挑出最多 3 条「以后还会用到」的长期记忆，只挑真正有用的：\n"
            "- 群友的固定特征/身份/习惯（谁在玩什么游戏、谁做什么工作、谁有什么梗）\n"
            "- 群里新出现的梗、外号、黑话\n"
            "- 明确的约定或要求（时间、规则、让机器人怎么做）\n"
            "不要记：一次性的闲聊和情绪、天气、你（机器人）自己说过的话、密码证件银行卡。\n"
            "同一个人/同一件事只写一条，把零散信息并进那一句，别重复。\n"
            "每条一行，格式：内容（≤40字）|标签1,标签2\n"
            "如果一条都不值得记，就只输出：无\n\n" + "\n".join(lines)
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=400,
            )
            raw = strip_think(resp.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("夜间 L1 提炼失败: %s", exc)
            return []
        if not raw or raw.startswith("无"):
            return []
        facts: list[dict] = []
        for line in raw.splitlines():
            line = line.strip().lstrip("-•* \t")
            if not line or "|" not in line:
                continue
            text, _, tags_raw = line.partition("|")
            text = text.strip()[:120]
            if len(text) < 4:
                continue
            tags = [t.strip()[:16] for t in re.split(r"[,，、\s]+", tags_raw) if t.strip()][:4]
            facts.append({"text": text, "tags": tags})
            if len(facts) >= max_items:
                break
        return facts

    def write_daily_diary(self, recent_history: list[dict], status: PetStatus, group_id: str = "") -> bool:
        """L2 日记：每晚以当前人设口吻写一条今天群里的细节与心情（≤200字）"""
        if not settings.has_valid_api_key or not recent_history:
            return False
        lines = [f"[{m['sender']}]: {m['content']}" for m in recent_history[-40:] if m.get("content")]
        if not lines:
            return False
        role_name, voice_hint, _ = _persona_voice_profile()
        # 把今天已经写过的日记塞进提示词，否则「/写日记」多按两次就会得到两篇几乎一样的日记
        today = datetime.now().strftime("%Y-%m-%d")
        wrote_today = [
            d.get("内容", "")
            for d in memory_store.read_recent_diaries(limit=3, group_id=group_id)
            if d.get("时间", "")[:10] == today
        ]
        avoid = (
            "\n今天已经写过这些，别再重复同样的角度和句子：\n" + "\n".join(f"- {t[:80]}" for t in wrote_today)
            if wrote_today
            else ""
        )
        prompt = (
            f"你是群里的「{settings.BOT_NAME}」（当前人设：{role_name}，{voice_hint}）。请根据今天群里发生的片段，写一条 60~160 字的私人日记，"
            "记录今天让你印象最深的细节和心情（像真日记，不要汇报，贴合当前人设口吻）。"
            "挑一件具体的小事写，别把一天的事都罗列一遍。\n"
            "只输出一行，格式：`心情词|日记正文`。" + avoid + "\n\n"
            + "\n".join(lines)
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.85,
                max_tokens=260,
            )
            raw = (resp.choices[0].message.content or "").strip()
            if not raw:
                logger.warning(
                    "L2 日记生成返回空内容（finish_reason=%s, 完成 token=%s），本篇跳过",
                    resp.choices[0].finish_reason,
                    getattr(resp.usage, "completion_tokens", "?"),
                )
                return False
            if "|" in raw:
                mood, body = raw.split("|", 1)
            else:
                mood, body = "摸鱼", raw
            return memory_store.write_diary(mood.strip()[:8], body.strip(), group_id=group_id)
        except Exception as exc:
            logger.warning("生成 L2 每日日记失败: %s", exc)
            return False


brain = DaFeiYuBrain()
