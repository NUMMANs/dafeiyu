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
from dsh_bridge import is_dsh_available
from dsh_bridge import list_dsh_jobs, submit_dsh_job
from image_fetcher import download_image_url, search_and_download_image
import l3_inject
import metrics
from memory import estimate_tokens, memory_store
from persona_runtime import emotion_sm, slang_book
from pet_engine import PetStatus, pet_engine
from roles.build_card import evolve_persona, interest_zone, load_card
from safety_guard import assess_danger, stage_pending_action
from scheduler import scheduler
from self_ops import git_pull_and_check, request_restart, self_edit_code
import tuning
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


# 句末符号后天然可以断一条气泡；太长了再退到逗号处断。
_SENT_END_RE = re.compile(r"(?<=[。！？!?…])")
_CLAUSE_END_RE = re.compile(r"(?<=[，,、；;：:])")
_TRAIL_JUNK = "，,、；;：:。 　"

# 气泡默认值（可在 data/tuning.json 里热调，夜间感官任务管）
BUBBLE_MAX_CHARS_DEFAULT = 30
BUBBLE_MAX_COUNT_DEFAULT = 6


def _pack_pieces(pieces: list[str], limit: int) -> list[str]:
    """把若干小片贪心塞进不超过 limit 字的气泡，尽量不把一句话拦腰截断。"""
    out: list[str] = []
    cur = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if not cur:
            cur = piece
        elif len(cur) + len(piece) <= limit:
            cur += piece
        else:
            out.append(cur)
            cur = piece
    if cur:
        out.append(cur)
    return out


def _split_long_bubble(text: str, limit: int) -> list[str]:
    """超长的一条按「句末 → 逗号 → 硬切」三层降级拆开。"""
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks = _pack_pieces([x for x in _SENT_END_RE.split(text) if x.strip()], limit)
    out: list[str] = []
    for chunk in chunks:
        if len(chunk) <= limit:
            out.append(chunk)
            continue
        out.extend(_pack_pieces([x for x in _CLAUSE_END_RE.split(chunk) if x.strip()], limit))
    # 整句一个标点都没有（比如连着一串字/链接）：只能硬切，否则又发成一大段
    final: list[str] = []
    for chunk in out:
        while len(chunk) > limit:
            final.append(chunk[:limit])
            chunk = chunk[limit:]
        if chunk:
            final.append(chunk)
    return final


# 「另一件事开始了」的信号：时段、语气转折、换了说话对象。
# 模型没写 `||` 时会顺手把素材抄成多行，换行常常断在半句话中间，空格后面才是新的一段——
# 只按换行切，就会切出「翻了两遍袋子确认没漏 下午大乱斗连输四把」这种一条塞两件事的气泡。
_BEAT_WORDS = (
    "凌晨", "早上", "早晨", "上午", "中午", "下午", "傍晚", "晚上",
    "夜里", "深夜", "半夜", "今早", "今晚", "昨天",
    "现在", "刚才", "后来", "然后", "结果", "反正", "对了", "话说",
    "你们", "大家", "咱们", "咱",
)
_BEAT_BREAK_RE = re.compile(
    r"(?<=[\s，,。！？!?…~～])(?=%s)" % "|".join(_BEAT_WORDS)
)
_SENT_TAIL = "。！？!?…~～"


def _reflow_lines(raw: str, limit: int) -> list[str]:
    """没有 `||` 时，把换行和空格处的断点重排成「一件事一条气泡」。

    两步：先按「另一件事开头」把每行再切开，再把不是新事开头的碎片接回上一条
    （合起来不超过 limit 字）。硬折行留下的半句会被接回去，跨时段的两件事会被拆开。
    """
    pieces: list[str] = []
    for line in raw.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        pieces.extend(p.strip() for p in _BEAT_BREAK_RE.split(line) if p.strip())

    merged: list[str] = []
    for piece in pieces:
        prev = merged[-1] if merged else ""
        # 上一条已经说到句号、这一条又是新事开头，就该断开；否则能接就接
        can_join = (
            bool(prev)
            and not piece.startswith(_BEAT_WORDS)
            and prev[-1] not in _SENT_TAIL
            and len(prev) + 1 + len(piece) <= limit
        )
        if can_join:
            merged[-1] = f"{prev.rstrip(_TRAIL_JUNK)}，{piece}"
        else:
            merged.append(piece)
    return merged


def split_human_bubbles(text: str) -> list[str]:
    """把一条回复拆成几条微信气泡，模拟真人手机打字连发。

    拆分顺序（模型怎么编排、代码怎么兜底）：
    1. 模型自己用 `||` 分段时，按 `||` 走——她编排的分段最自然，代码只管长度兜底；
    2. 没有 `||` 就按换行/空格重排（`_reflow_lines`）：断在半句的接回去，跨时段的两件事拆开；
    3. 每一段再按 `BUBBLE_MAX_CHARS`（默认 30 字，tuning 可热调）拆：先句末，
       再逗号，最后才硬切——保证发出去的每一条都不会是一大坨；
    4. 最多 `BUBBLE_MAX_COUNT`（默认 6）条，超出的并进最后一条，防止刷屏。

    段与段之间的停顿不在这里做，由发送层（adapters/wxauto_adapter.py `_send_bubbles`）
    按字数算打字时间，这里只负责「切多长、切几条」。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    max_chars = tuning.get_int("BUBBLE_MAX_CHARS", BUBBLE_MAX_CHARS_DEFAULT)
    max_count = max(1, tuning.get_int("BUBBLE_MAX_COUNT", BUBBLE_MAX_COUNT_DEFAULT))

    if "||" in raw:
        parts = [p.strip() for p in raw.split("||") if p.strip()]
    else:
        parts = _reflow_lines(raw, max_chars)

    expanded: list[str] = []
    for part in parts:
        part = part.rstrip("。").strip()
        if part:
            expanded.extend(_split_long_bubble(part, max_chars))

    # 真人微信聊天很少在每句话末尾打正式句号；顺便把断句留下的逗号尾巴收掉
    cleaned = [p.strip().rstrip(_TRAIL_JUNK) for p in expanded]
    cleaned = [p for p in cleaned if p]
    if not cleaned:
        return [raw]

    if len(cleaned) > max_count:
        tail = "，".join(cleaned[max_count - 1:]).strip().rstrip(_TRAIL_JUNK)
        cleaned = cleaned[: max_count - 1] + ([tail] if tail else [])
    if len(cleaned) > 1:
        logger.info("💬 气泡 %d 条: %s", len(cleaned), " ¦ ".join(cleaned))
    logger.debug("💬 原文: %r", raw)
    return cleaned


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
                # 这个模型是「先思考再给正文」的，思考草稿和正文共用 max_tokens。
                # 原来给 1024，2026-11-26 实测 16 张图里 7 张（44%）思考没写完就被截断，
                # 剥掉 think 块后正文为空 → 白白回退 DeepSeek 原生视觉、历史也拿不到摘要。
                # 给到 2600，让它既有空间思考完，也留得下正文。
                "max_tokens": 2600,
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
    """返回 (角色简称, 回复口吻指引, 语气尾词)。只剩大肥鱼一个人设，不带卖萌尾字。"""
    return ("大肥鱼", "大肥鱼真人群友的短气泡口吻", "")


# 出口要硬抠掉的串戏词。人设卡里是「真人短气泡、不卖萌」的路子，但 L3 整层注入会把
# 它自己更早（塔菲时代）说过的「…喵」「塔菲」当成自己的口吻喂回来，光靠提示词压不住，
# 偶发就会顺着旧口吻补一个尾字——所以在出口兜一层，保证发出去的一定干净。
_PERSONA_LEAK_WORDS = ("塔菲", "喵")
_DUP_PUNCT_RE = re.compile(r"([，。！？、~～]{1})\1+")


def _strip_persona_leaks(text: str) -> str:
    """把「喵／塔菲」这类串戏词从最终回复里删掉（幂等）。"""
    if not text:
        return text
    out = text
    for word in _PERSONA_LEAK_WORDS:
        if word in out:
            logger.info("🧹 清理人设串戏词「%s」：%s", word, out[:60])
            out = out.replace(word, "")
    if out == text:
        return text
    out = _DUP_PUNCT_RE.sub(r"\1", out)
    # 删词后可能留下「，，」「～。」这种尾巴，按气泡逐条收个尾
    bubbles = []
    for part in out.split("||"):
        part = part.strip().strip("，、~～ ")
        bubbles.append(part)
    return "||".join(b for b in bubbles if b)


def _build_system_prompt(
    status: PetStatus,
    sender: str,
    user_affinity: dict,
    recalled_memory: str = "",
    matched_slang: str = "",
    emotion_desc: str = "",
    diary_note: str = "",
) -> str:
    """
    system 里只放人设卡这一块「每轮完全一致」的稳定文本。

    为什么动态内容不在这儿：L3 全量注入后单次 prompt 已经 7.9 万 tk（实测），
    DeepSeek 的前缀缓存要求从第一个 token 起严格一致。以前 system 第一行是
    `【当前时间】YYYY-MM-DD HH:MM:SS`，每轮都变 —— 实测同样内容第二次调用本可
    命中 99.8%，只要改掉开头的时间戳，命中率立刻掉到 0%，等于每轮都按全价计费。
    所以时间、召回旧事、日记、黑话、情绪这些每轮都变的东西统一挪到最后一条
    user 消息里（见 _build_turn_context），system 保持静态，把缓存吃满。

    参数保留只是为了兼容旧调用方，函数内不再使用。
    """
    return load_card()


def _build_turn_context(
    status: PetStatus,
    sender: str,
    user_affinity: dict,
    recalled_memory: str = "",
    matched_slang: str = "",
    emotion_desc: str = "",
    diary_note: str = "",
    bg_note: str = "",
    job_report: str = "",
    life_note: str = "",
    l3_notes: list[str] | None = None,
) -> str:
    """本轮动态上下文（时间/后台任务/旧事/日记/黑话/情绪），拼在最后一条 user 消息前面。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 【实时状态】块已按需求移除（饱食度/段位/好感度不再注入）。
    # 【本轮真实原声语感参考】（persona_rag 场景召回）也已移除：
    # 它只按场景类别匹配、不看语义，经常给打游戏的对话塞「出门买罪受」这种无关原话，
    # 反而让模型把无关名词带进回复。语料文件保留，随时可以重新接回来。
    extra_blocks = []
    if job_report:
        extra_blocks.append(job_report)
    if l3_notes:
        # 后台工具唯一被允许留下的「话」：写进 l3_inject.json 的几句话，带过期时间。
        # 明确告诉她这是自己知道的、不是别人刚说的，避免她当成新消息接话。
        # 条数与每条字数由 data/tuning.json 热调（夜间「记忆与人格」任务管注入方式）。
        note_chars = tuning.get_int("L3_NOTE_MAX_CHARS", 200)
        bullets = "\n".join(f"- {n[:note_chars]}" for n in l3_notes)
        extra_blocks.append(
            "【你自己知道的事】这些是你自己私下看过、查过的，不是群里刚有人说的：\n"
            f"{bullets}\n"
            "（知道就行。没人问到就别主动提，别人正好聊到就直接用，别念成清单。）"
        )
    if bg_note:
        extra_blocks.append(bg_note)
    if life_note:
        # 她今天自己过的日子（见 daily_life.py）：有了这块，别人问「你在干嘛」时
        # 不会和半小时前在群里说过的对不上，也能顺口带出「上午买的那包薯片」。
        extra_blocks.append(life_note)
    if recalled_memory:
        extra_blocks.append(recalled_memory)
    if diary_note:
        extra_blocks.append(diary_note)
    if matched_slang:
        extra_blocks.append(matched_slang)
    if emotion_desc:
        extra_blocks.append(emotion_desc)
    extra_text = "\n".join(extra_blocks)
    parts = [f"【当前时间】{now_str}（Asia/Shanghai）"]
    if extra_text:
        parts.append(extra_text)
    return "\n".join(parts) + "\n"


_WXID_PREFIX_RE = re.compile(r"^wxid_[0-9a-zA-Z_\-]{4,}:\s*")
_XML_HEAD_RE = re.compile(r"<\?xml|&lt;\?xml|<msg>|<appmsg")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_ESC_TITLE_RE = re.compile(r"&lt;title&gt;(.*?)&lt;/title&gt;", re.S)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_TAG_RE = re.compile(r"<[^>]{1,600}>")
_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&"))


def _clean_l3_content(raw: str, limit: int = 260) -> str:
    """
    清洗一条 L3 历史再注入。

    以前只取最近 20 条，脏数据露不出来；改成整层注入后，947 条里有 149 条是
    「wxid_xxx:\\n<?xml version=1.0?><msg><appmsg>…」这种没解析干净的卡片原文，
    最长一条上万字符、合计吃掉 4 万多 token，等于每轮都在给模型喂一堆 XML 标签。
    这里把 wxid 前缀剥掉，XML 卡片只留 <title>（「给你发了一个红包」这类真正有信息量的部分），
    再压掉连续空白并截断，避免单条长文占满预算。
    """
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
    """
    决定这一轮把 L3 会话层的哪些消息塞给模型。

    - 默认（L3_INJECT_ALL=1）：整层全部注入，让模型看得到群里从头到尾聊了什么；
    - 受 L3_INJECT_MAX_TOKENS 约束：从最新往回累加，超预算就停，最旧的那批不注入；
    - L3_INJECT_ALL=0 时退回老行为：只带最近 L3_HISTORY_LIMIT 条。
    末尾刚落库的「当前这句」永远剔除，避免同一条重复出现两次。
    每条都先过 _clean_l3_content（剥 wxid 前缀、精简 XML 卡片、截断超长文本）。
    """
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
    hit_budget = False
    for item in reversed(items):
        content = _clean_l3_content(item.get("content") or "")
        if not content and item.get("image_path"):
            content = "[发了一张图片]"
        if not content:
            continue
        cost = estimate_tokens(content) + 6
        if picked and used + cost > budget:
            hit_budget = True
            break
        picked.append({**item, "content": content})
        used += cost
    picked.reverse()
    if hit_budget:
        logger.warning(
            "L3 整层注入超预算：共 %d 条，只注入最近 %d 条（约 %d tk / 上限 %d tk）；建议调低 L3_CONTEXT_LIMIT_TOKENS 让压缩早点生效",
            len(items),
            len(picked),
            used,
            budget,
        )
    else:
        skipped = len(items) - len(picked)
        if skipped:
            logger.info("L3 整层注入：%d 条，约 %d tk（另有 %d 条空内容跳过）", len(picked), used, skipped)
        else:
            logger.info("L3 整层注入：%d 条，约 %d tk", len(picked), used)
    return picked


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
        return _strip_persona_leaks(cleaned).strip(), mood, caption

    def summarize_image(self, image_path: Path) -> str | None:
        """后台静默概括图片核心内容：先走智谱免费视觉，拿不到正文再回退一次原生视觉。

        为什么要有回退：这段摘要会写回 SQLite 历史，是后面所有轮次「她记不记得这张图」
        的唯一来源。以前智谱一失败这里直接 return None，历史里就永远只剩
        「[发了一张图片]」，下次有人问起这张图她只能装失忆。
        回退只花一次便宜的 200tk 描述，比丢一张图的记忆划算。
        """
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
                    logger.info("👁️ 智谱没给正文，改用 DeepSeek 原生视觉补图片摘要 (%d字)", len(body))
            except Exception as exc:
                logger.warning("原生视觉补图片摘要也失败: %s", exc)
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
        interest_hint: str = "",
        bg_note: str = "",
        job_report: str = "",
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
        # （语境语感 RAG 已按需求下线，persona_rag.py 与语料文件保留未删）
        role_name, voice_hint, tail_word = _persona_voice_profile()
        # 只用这句话本身去召回：把 sender 也拼进查询会让「这条记忆的 tags 里有这个人」
        # 变成命中条件，于是这个人以后说的每一句（哪怕只有一个「行」）都会把它召回。
        recalled = memory_store.render_context(text, group_id=group_id)
        # 夜间复盘要靠这个看「L1 有没有把无关旧事塞进每轮上下文」
        metrics.record("l1_recall", group_id, hit=bool(recalled))
        recalled_memory = recalled if tuning.get_int("RECALL_INJECT_ON", 1) else ""
        diary_note = (
            memory_store.render_diary_context(group_id) if settings.MEMORY_DIARY_INJECT else ""
        )
        matched_slang = slang_book.render_matched(group_id, text)
        emotion_desc = emotion_sm.describe(group_id)
        # L3 注入：后台工具干完活留下的几句话（带 TTL，过期自动消失）
        l3_notes = (
            l3_inject.load_notes(group_id, limit=tuning.get_int("L3_NOTE_MAX_ITEMS", 5))
            if group_id and tuning.get_int("L3_INJECT_ON", 1)
            else []
        )
        # 她今天自己的生活：让「你在干嘛」这种回答前后一致
        life_note = ""
        life_hint = ""
        if tuning.get_int("LIFE_INJECT_ON", 1):
            try:
                import daily_life

                # 有人直接问「你在干嘛」时，再补一条「就照现在这一段答」的指令；
                # 光有生活线，模型容易顺着当天话题编，或者回一句没内容的「在刷手机」。
                life_hint = daily_life.question_hint(text)
                # 被问时素材只给最近两段：摆三五条在眼前，它就会一口气全念出来，
                # 变成「中午…下午…晚上…」的行程汇报。念不全反而更像聊天。
                life_note = daily_life.context_line(max_items=2 if life_hint else 3)
                if not life_note:
                    # 今天还没生成过：后台补一条，这轮先不带，下次问起就能对上
                    daily_life.ensure_today_async(self)
            except Exception as exc:  # noqa: BLE001 - 生活线读不出来不影响聊天
                logger.warning("今日生活：上下文读取失败: %s", exc)
        turn_context = _build_turn_context(
            status,
            sender,
            user_affinity,
            recalled_memory=recalled_memory,
            matched_slang=matched_slang,
            emotion_desc=emotion_desc,
            diary_note=diary_note,
            bg_note=bg_note,
            job_report=job_report,
            life_note="\n".join(x for x in (life_note, life_hint) if x),
            l3_notes=l3_notes,
        )
        # system 只放静态人设卡：动态内容一旦进了 system，前缀缓存就会从第一个 token 起失效
        messages: list[dict] = [
            {"role": "system", "content": _build_system_prompt(status, sender, user_affinity)}
        ]

        # L3 会话层：默认注入整层历史（L3_INJECT_ALL=1），让大肥鱼能看到群里全部的来龙去脉。
        # 整层太长时按 L3_INJECT_MAX_TOKENS 从最旧的往前砍，绝不会撑爆上下文。
        # 这一层是四层里唯一可以丢的：超过 L3_CONTEXT_LIMIT_TOKENS 时由 bot_core._compress_l3 每天 0 点压缩。
        for item in _select_l3_history(recent_history, sender, text):
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
                vision_summary = re.sub(r"\s+", " ", zhipu_vision_desc).strip()[
                    : tuning.get_int("VISION_SUMMARY_CHARS", 220)
                ]
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
            if settings.ENABLE_WEB_SEARCH and not interest_hint and any(cue in text for cue in PRE_SEARCH_CUES):
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
                if interest_hint:
                    # L2.5 兴趣接话：这不是在问我，是我自己对这话题有兴趣，主动凑一句
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"[{sender}]: {text}\n\n"
                                f"【任务：接话】群里刚冷了一阵，是 {sender} 重新开了这个话题，"
                                f"没人叫你，也不是在问你问题。\n"
                                f"你自己对这话题有兴趣（你想说的点：{interest_hint}），"
                                f"现在以群友身份主动接一句：\n"
                                f"- 可以损、可以接梗、可以问细节、可以说一句自己类似的经历，"
                                f"短、准，像真人随口接话，别铺垫别解释。\n"
                                f"- 只发 1~2 句。\n"
                                f"- 不要问好、不要说“这个问题很好”、不要分析、不要总结、不要用 Markdown、"
                                f"不要自称 AI 或助手。\n"
                                f"- 对事不对人：可以损这事离谱，别攻击长相、家人、地域、职业，别碰政治和疾病。"
                            ),
                        }
                    )
                else:
                    if job_report:
                        # 任务播报是系统指令，不是群友发言。加 [系统]: 前缀的话模型会
                        # 跟着模仿群聊格式，实测回了一句「@：^）」这种莫名其妙的 @ 开头。
                        messages.append({"role": "user", "content": text})
                    else:
                        messages.append({"role": "user", "content": f"[{sender}]: {text}"})
            client = self._get_chat_client()
            model = settings.DEEPSEEK_CHAT_MODEL
            used_vision = False

        # 把本轮动态块（时间/旧事/日记/黑话/情绪）挂到最后一条 user 消息最前面。
        # 这样 system + 历史消息构成从头到尾都一致的前缀，DeepSeek 前缀缓存才能命中。
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

        used_search = bool(not used_vision and "pre_searched_block" in locals() and pre_searched_block)
        is_code_or_file_task = (not used_vision) and any(c in text.lower() for c in CODE_OR_FILE_CUES)
        total_tokens = 0
        try:
            # 联网查事实时降温防幻觉；写代码/文件任务关掉惩罚避免破坏 HTML/JSON 结构；闲聊保持高拟人度
            if is_code_or_file_task:
                temp = 0.45
                freq_pen = 0.0
                pres_pen = 0.0
            elif job_report:
                # 后台任务播报：要照实交代结果（路径、数字不能编），但措辞仍是个群友在说话
                temp = 0.7
                freq_pen = 0.3
                pres_pen = 0.2
            else:
                temp = 0.45 if used_search else (0.65 if used_vision else 1.0)
                freq_pen = 0.1 if (used_search or used_vision) else 0.45
                pres_pen = 0.0 if (used_search or used_vision) else 0.4
            # 开启桌面工具时给足 4096 max_tokens，防止 deepseek-flash 思考 + .html/.py 参数 JSON 被截断
            # 兴趣接话只发 1~2 句，160 足够，避免 owner 拿到 4096 后开始长篇大论
            # 任务播报要交代清楚做了什么/结果在哪，但也不许写论文，700 封顶
            if interest_hint:
                max_tokens = 160
            elif job_report:
                max_tokens = 700
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
            if is_owner(sender) and _needs_agent_tools(text) and not settings.TOOLS_ALL_TO_DSH:
                # TOOLS_ALL_TO_DSH=true（默认）：13 个电脑工具一律不挂，干活全交给后台执行助手。
                # 除了省 1.4k tk，更关键是 tools 字段排在 messages 前面——「挂/不挂」来回切换
                # 会让 2.6 万 token 的前缀缓存整段作废（实测命中从 25600 掉到 2304）。
                # 只有显式关掉开关时才退回旧行为（DSH 不可用时挂电脑工具兜底）。
                if not (settings.ENABLE_TOOL_ROUTER and is_dsh_available()[0]):
                    available_tools.extend(AGENT_TOOL_SCHEMAS)
            if interest_hint or job_report:
                # 兴趣接话是纯嘴活；任务播报只需要张嘴说结果，再挂工具只会又派一次活
                available_tools = []

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

        # 夜间复盘沉淀下来的临时偏好（roles/judges/l2.md），没有就是空串。
        # 只能改「判多严」，不能改输出格式 —— 否则 YES/NO 解析会崩。
        extra = tuning.judge_extra("l2")
        extra_block = (
            "\n另外，昨天群里复盘出这几条，判的时候一并考虑（和上面冲突时以这里为准）：\n"
            f"{extra}\n"
            if extra
            else ""
        )

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
            f"自言自语、单纯发感叹或表情、在议论第三个人。\n"
            f"{extra_block}\n"
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

    def judge_tool_need(
        self,
        sender: str,
        text: str,
        recent_history: list[dict] | None = None,
        link_urls: list[str] | None = None,
    ) -> dict:
        """
        工具路由判定（轻量，只输出一行 JSON）：这句话要不要让后台助手动手。

        两种模式：
        - 不传 link_urls：只判「派不派活」，判错代价可控（漏派退回聊天，误派多一句占位）；
        - 传 link_urls：这条消息带着链接，要区分「分享链接，后台静默读懂」(kind=link)
          与「基于链接要求产出实物」(kind=work)。链接消息在入口处就判，不等大肥鱼
          决定回不回话——否则她不回话时链接永远读不到。

        写法刻意压到极简（不带人设、不带工具 schema、不带长上下文），一次约 300~500 tk
        输入、40 tk 输出。返回 {"need", "task", "reason", "kind", "urls"}。
        """
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
        ctx = "\n".join(ctx_lines) if ctx_lines else "（群里之前没人说话）"
        probe = body if len(body) <= 300 else body[:300] + "…"

        # 夜间复盘沉淀下来的临时偏好（roles/judges/tool_router.md）。
        # 只能收「什么场景该派/不该派」的具体补充，不许改输出格式、更不许一刀切关掉路由。
        extra = tuning.judge_extra("tool_router")
        extra_block = (
            "\n另外，昨天群里复盘出这几条，判的时候一并考虑"
            "（只能细化「什么场景」，不得推翻上面的禁止项）：\n"
            f"{extra}\n"
            if extra
            else ""
        )

        url_block = ""
        if is_link_mode:
            url_block = (
                "【这条消息里的链接】\n"
                + "\n".join(f"- {u}" for u in urls[:3])
                + "\n\n"
            )

        if is_link_mode:
            criteria = (
                "判断标准（链接消息专用）：\n"
                "- kind=link 且 need=true（最常见）：群友分享、转发、顺手甩了个链接，"
                "没要求产出文件。后台助手会把链接读懂，留一两句话给大肥鱼，"
                "等后面聊到时她自然接得上。\n"
                "- kind=work 且 need=true（少数）：明确要求基于这个链接产出文件、"
                "抓全文存档、整理成表格，或要求操作电脑/代码/系统。\n"
                "- need=false：链接是图片、表情、安装包之类的直链，纯广告刷屏，"
                "或群里根本没在聊它。\n"
                "- 固定不派：要求改大肥鱼自己项目的代码、配置或人格文件。"
                "后台助手被关在沙箱里，只准生成文件和留话，改不了项目本身，派了也是空跑。\n"
                "拿不准时优先 kind=link（读一下成本很低），但明显是垃圾链接就 need=false。\n"
            )
            task_rule = (
                "need=true 时写任务说明：\n"
                "- kind=link：一句话点明读哪个链接、群里在聊什么、大肥鱼该关注什么，"
                "把上面的 URL 原样带上，不超过 100 字；\n"
                "- kind=work：不带人称、不带寒暄，直接写要做什么，保留原话所有细节，"
                "不超过 120 字；\n"
                "- 不许编造原话里没出现的路径、参数或账号。\n\n"
                "只输出一行 JSON，不要 markdown、不要代码块、不要解释：\n"
                '{"need": true 或 false, "kind": "link" 或 "work", '
                '"task": "任务说明（need=false 时为空字符串）", "reason": "15 字以内理由"}'
            )
        else:
            criteria = (
                "判断标准（只有两类）：\n"
                "- need=true（要干活）：明确要求操作电脑/服务器/文件/代码/网页/系统，或要求产出实物结果。"
                "例如：改代码、跑脚本、看报错、查本机状态、装软件、整理文件、生成表格或文档、"
                "抓网页内容、排查问题、部署上线。\n"
                "- need=false（不干活）：闲聊、吐槽、开玩笑、问知识、问观点、问建议、问天气时间、"
                "约人打游戏、发表情、问机器人自己（“你在干嘛”）、要表情包、让它记住某件事、"
                "单纯复述或抱怨。\n"
                "- 还有一类固定不派：要求改大肥鱼自己项目的代码、配置或人格文件。"
                "后台执行助手被关在沙箱里，只准生成文件和留话，改不了项目本身，派了也是空跑；"
                "这类活得由外部工具来做。\n"
                "拿不准时一律 need=false —— 宁可漏派，也不要给群里派一堆空任务。\n"
            )
            task_rule = (
                "need=true 时，把这件事改写成一段可以直接交给执行助手的任务说明：\n"
                "- 不带人称、不带寒暄、不要写“请帮我”，直接写要做什么；\n"
                "- 保留原话里的所有细节（文件路径、人名、数字、时间、格式要求）；\n"
                "- 不超过 120 字，一句话说完；\n"
                "- 不许编造原话里没出现的路径、参数或账号。\n\n"
                "只输出一行 JSON，不要 markdown、不要代码块、不要解释：\n"
                '{"need": true 或 false, "task": "任务说明（need=false 时为空字符串）", "reason": "15 字以内理由"}'
            )

        prompt = (
            "你是群聊机器人的「派活判断器」。只判断群友这句话要不要让后台助手动手，"
            "不负责真的干活，也不负责聊天。\n\n"
            f"【最近几句群聊】\n{ctx}\n\n"
            f"【现在这句】\n[{sender}]: {probe}\n\n"
            f"{url_block}"
            f"{criteria}"
            f"{extra_block}\n"
            f"{task_rule}"
        )

        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
            )
            raw = strip_think(resp.choices[0].message.content or "")
        except Exception as exc:
            logger.warning("工具路由判定调用失败: %s", exc)
            return pack(False, "", "调用失败")

        matched = re.search(r"\{.*\}", raw, re.S)
        if not matched:
            logger.info("工具路由判定输出无法解析: %r", raw[:120])
            return pack(False, "", "解析失败")
        try:
            data = json.loads(matched.group(0))
        except json.JSONDecodeError:
            logger.info("工具路由判定输出不是合法 JSON: %r", raw[:120])
            return pack(False, "", "JSON非法")

        task = " ".join(str(data.get("task") or "").split())[:200]
        need = bool(data.get("need")) and bool(task)
        reason = " ".join(str(data.get("reason") or "").split())[:30]
        if not need:
            return pack(False, "", reason)
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in ("link", "work"):
            kind = "link" if is_link_mode else "work"
        logger.info(
            "🧰 工具路由判定：%s → %s（理由：%s）",
            "读链接" if kind == "link" else "需要派活",
            task[:60],
            reason,
        )
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
        """
        L2.5 冷场重启话题的「兴趣雷达」（轻量判定，只输出一行结论）。

        2026-11-27 改动：原来是「这个话题有没有槽点」的吐槽判定，现在改成
        「她自己对这个话题有没有兴趣」的兴趣判定。原因：只按槽点评分，她只会接
        「离谱、翻车、抽象」那一类，聊到游戏、吃的、群友日常反而一声不吭 —— 不像一个
        有自己爱好的活人，倒像个蹲着等笑点的段子手。

        判定依据是她人格卡里的「兴趣区」（roles/build_card.interest_zone）：那一整段会
        原样交给判定器。**这是唯一一处把人格卡带进轻量判定的地方**，所以提示词里
        明确写了「以兴趣区为准」，否则模型会退回到「普遍意义上什么话题热门」。

        场景：群里安静了很久，某人重新开口。此时既不是在叫我，也算不上接话茬。
        输出格式 `SCORE|她想接的那个点`，SCORE 为 0~10 整数，由调用方按阈值决定是否接话。

        multi_speaker=True 表示 text 是这一波多人接话的合并探针（每行 `[昵称]: 内容`），
        此时不能沿用「[sender] 重新开口说」的说法，否则会把几个人的话当成一个人说的。

        返回：她想说的那个点（非空字符串）表示值得接话；空字符串表示不接话。
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

        # 她自己的兴趣区（人格卡 roles/大肥鱼.人格.md 的「我的兴趣区」）。
        # 这是 L2.5 的唯一判断依据：有这一段，她接的是自己关心的事；
        # 没有这一段，判定器就会退回「什么话题在普遍意义上热门」，那又变成资讯号了。
        zone = interest_zone()
        zone_block = (
            "【她的兴趣区】（写在里面的，她看见就想凑上去说两句）\n" + zone + "\n\n"
            if zone
            else "【她的兴趣区】（还没写，按一个爱吐槽、爱打游戏、嘴有点欠的普通人判断，别对什么都来劲）\n\n"
        )

        # 夜间复盘沉淀下来的临时偏好（roles/judges/l2_5.md）。只调整「什么算她感兴趣」，
        # 输出格式必须保持不变，否则 SCORE|兴趣点 的解析会崩。
        extra = tuning.judge_extra("l2_5")
        extra_block = (
            "\n昨天群里复盘出这几条，评分时一并考虑（和上面冲突时以这里为准）：\n"
            f"{extra}\n\n"
            if extra
            else "\n"
        )

        prompt = (
            f"你是「{settings.BOT_NAME}」的注意力开关。她是个普通群友，不是助手。\n"
            "现在群里冷了一阵，有人重新开了个话题。你要判断：这个话题她自己有没有兴趣凑上去说一句。\n\n"
            + zone_block
            + opening
            + (
                "（这条消息 @ 的是群里的其他人，不是叫她。"
                "@ 别人不算扣分，照常只看这个话题本身她感不感兴趣。）\n"
                if at_others
                else ""
            )
            + f"\n更早的几句（可能为空）：\n{ctx}\n\n"
            "评分标准（0~10 分，只看「她对这个话题有多感兴趣 + 她接不接得上话」，不看话题热不热门）：\n"
            "- 8~10：正踩在兴趣区上，而且有具体的东西可以说（她懂这个、她吃过这个、这事离谱到她忍不住要损一句）。\n"
            "- 4~7：沾一点边，但懂得不够多，或者没什么可接的。\n"
            "- 0~3：跟她没关系（正事通知、纯打卡、纯表情、广告、别人的私事、她完全不懂的领域）。\n"
            "注意：话题再热，只要不在兴趣区里、她其实插不上话，就给 0~3 分。\n\n"
            "另外，出现以下任一情况直接给 0 分：\n"
            "在问正经求助问题、在讨论别人的隐私或外貌、内容涉及政治/疾病/丧事、纯粹约时间见面的私事。\n"
            "如果这几句其实是几个人在聊各自不相干的事、或者已经变成一对一私聊，也给 0 分。\n\n"
            + extra_block
            + "输出格式（只输出一行，不要解释）：\n"
            "分数|她最想说的那个点（15 字以内，具体到事；没兴趣就写「无」）\n"
            "例如：9|队友把大乱斗当排位打"
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
            threshold = tuning.get_float(
                "TOPIC_INTEREST_MIN_SCORE", settings.TOPIC_INTEREST_MIN_SCORE
            ) * 10
            logger.info(
                "🔥 兴趣判定 [%s] %.1f/10 (阈值 %.0f) 兴趣点=%s | %s",
                sender, score, threshold, angle[:20] or "无", probe[:30],
            )
            if score < threshold or not angle or angle == "无":
                return ""
            return angle[:30]
        except Exception as exc:
            logger.warning("兴趣判定异常: %s", exc)
            return ""

    def generate_autonomous_bubble(self, kind: str, status: PetStatus) -> BrainReply:
        """生成定时作息 / 饿扁催饭的主动冒泡发言"""
        role_name, voice_hint, tail_word = _persona_voice_profile()
        if not settings.has_valid_api_key:
            msg = (
                f"饿扁了{tail_word}||谁发个 /投喂 管顿白饭"
                if kind == "hungry"
                else "鱼缸水好像该换了"
            )
            b = split_human_bubbles(msg)
            return BrainReply(text="\n".join(b), bubbles=b, mood="hungry" if kind == "hungry" else "happy", tokens_used=0, used_vision=False)

        if kind == "hungry":
            instruction = (
                f"你是微信群里的「{settings.BOT_NAME}」（当前人设：{role_name}，{voice_hint}），现在饱食度只剩 {status.hunger}/100。"
                f"请主动在群里发 1~2 句符合你人设的催饭发言，让群友发 `/投喂` 管顿饭，末尾带 `[MOOD:hungry]`。"
            )
        else:
            # 2026-11-25 复盘：原来的写法直接教它「问大伙今晚干啥、开不开黑」，
            # 落地就成了「都在干嘛呢」这种空问句，正是「冷场硬找话」本身。
            # 现在只许说自己眼下的具体事，不掀新话题、不反问、不报假时间。
            instruction = (
                f"你是微信群里的「{settings.BOT_NAME}」（当前人设：{role_name}，{voice_hint}），"
                f"现在是 {datetime.now().strftime('%H:%M')}，群里安静了一阵。"
                f"请自己嘀咕一句眼下正在干的具体事（在吃什么、在弄什么、卡在哪一步都行），"
                f"像随手发的一条微信，15~25 字，只发 1 句。"
                f"严禁问「都在干嘛」「今晚怎么说」「有人吗」这类空问句，也别主动掀话题。"
                f"末尾带 `[MOOD:happy]` 或 `[MOOD:sleepy]`。"
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
            # 兜底也不能是空问句（原来是「都在干嘛呢」）：宁可说一件死板的实事
            b = split_human_bubbles(cleaned or "盯着鱼缸发了会儿呆")
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
                else "鱼缸水好像该换了"
            )
            b = split_human_bubbles(fallback)
            return BrainReply(
                text="\n".join(b),
                bubbles=b,
                mood="hungry" if kind == "hungry" else "happy",
                tokens_used=0,
                used_vision=False,
            )

    def suggest_share_topics(self, recent_lines: list[str], avoid_topics: list[str]) -> list[str]:
        """冷场找话题第一步：出 2 条具体、能搜到东西的关键词。

        为什么不直接让它「写一条分享」：那样模型会凭空编一条新闻。必须先去搜，
        拿到真实材料再让它挑——「分享」的可信度全在这一步。
        """
        if not settings.has_valid_api_key:
            return []
        extra = tuning.judge_extra("topic_share").strip()
        recent = "\n".join(recent_lines[-12:]) or "（最近没人说话）"
        avoid = "、".join(avoid_topics[-10:]) or "（无）"
        prompt = (
            f"你是微信群里的「{settings.BOT_NAME}」，是群友不是助手。群里已经安静很久了，"
            "你想主动分享一个自己刚在网上看到的、真的感兴趣的东西。\n"
            "【平时感兴趣的方向】游戏（英雄联盟/主机/独立游戏）、吃的东西、数码新品、动漫、"
            "奇怪的冷知识、生活里的小发现。别的领域偶尔也行，但得像是你随手刷到的。\n"
            "【最近群里聊过】（只用来避开重复，不要接着聊）\n"
            f"{recent}\n"
            f"【最近已经分享过】（必须换方向）\n{avoid}\n"
            + (f"【额外偏好】{extra}\n" if extra else "")
            + "请给 2 条可以拿去搜索的具体关键词，例如「2026 新出的独立游戏 推荐」"
            "「今年流行的奇怪零食」「猫为什么喜欢纸箱」。要求：\n"
            "1. 必须具体到能搜出东西，不要「最近有什么好玩的」这种空话；\n"
            "2. 两条方向要不一样；\n"
            "3. 不碰政治时事、灾害事故、明星绯闻、医疗诊断、投资理财/股票加密货币、"
            "地域或性别对立；\n"
            "4. 每条 6~20 个字，尽量带上「2026」，避免搜回几年前的旧闻。\n"
            '只输出 JSON：{"queries": ["...", "..."]}'
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.9,
                max_tokens=200,
            )
            raw = strip_think(resp.choices[0].message.content or "")
            matched = re.search(r"\{.*\}", raw, re.S)
            if not matched:
                logger.info("冷场找话题：关键词输出无法解析 %r", raw[:120])
                return []
            data = json.loads(matched.group(0))
            items = data.get("queries") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return []
            out: list[str] = []
            for item in items:
                q = " ".join(str(item or "").split())[:30]
                if q and q not in out:
                    out.append(q)
            return out[:2]
        except Exception as exc:
            logger.warning("冷场找话题：关键词调用失败: %s", exc)
            return []

    def compose_topic_share(self, material: str, recent_lines: list[str], strict: bool = False) -> dict:
        """冷场找话题第二步：从搜回来的真实材料里挑一个东西，写成一句群聊话。

        返回 ``{"text", "topic", "mood", "evidence"}``；材料不值得分享时返回空 dict。
        ``evidence`` 是从材料里原样抄回的支撑句，调用方会拿它和原文比对——模型编不出来，
        这一条就发不出去（实测会把 2021 年的旧闻写成「新出的紫薯椰子味薯片」）。
        """
        if not settings.has_valid_api_key or not material.strip():
            return {}
        extra = tuning.judge_extra("topic_share").strip()
        recent = "\n".join(recent_lines[-8:]) or "（最近没人说话）"
        prompt = (
            f"你是微信群里的「{settings.BOT_NAME}」。下面是你刚在网上看到的内容。"
            "群里安静了很久，你想挑一个自己真感兴趣的东西，随手发到群里。\n"
            f"【你的人设（只学语气，别照抄）】\n{load_card()[:900]}\n"
            f"【最近群聊】（只用来避免驴唇不对马嘴）\n{recent}\n"
            + (f"【额外偏好】{extra}\n" if extra else "")
            + f"【网上看到的内容】\n{material[:2600]}\n"
            + (
                "【注意】上一次你写了材料里没有的东西。这次只能写材料里逐字出现的信息，"
                "evidence 必须是从材料里原样复制的一句话。\n"
                if strict
                else ""
            )
            + "要求：\n"
            "1. 只挑一个具体的东西（一个新游戏／一个怪东西／一条冷知识／一个好吃的地方），别罗列；\n"
            "2. 必须把它叫什么说出来（游戏名、口味、型号、地名都行），不能用「一个味道」「某个东西」"
            "这种含糊说法代替——群友看完得知道你说的是啥；\n"
            "3. 1~2 句、总共 20~50 字，像随手发的微信，可以带一点自己的评价或吐槽；\n"
            "4. 只能用材料里真实存在的信息。品牌、口味、价格、日期、数据都不能编，"
            "材料没写的不要补；写法上不要下结论式科普；\n"
            "5. 材料里的日期要看：2025 年及更早的东西，不许说成「新出」「刚出」「最近」，"
            "宁可只说它是什么；\n"
            "6. 不要写「我搜到」「根据报道」「今日热点」，不要列点、不要放链接、不要 Markdown；\n"
            "7. 不要用「你们觉得呢」「有人玩吗」这种空问句结尾；\n"
            "8. 如果材料里没有一条值得分享，text 返回空字符串。\n"
            "另外必须给 evidence：从材料里原样复制一句支撑这条分享的话（15~40 字），"
            "一个字都不许改写；找不到这样的话就不要写 text。\n"
            '输出 JSON：{"text": "...", "topic": "不超过12字的主题词", "evidence": "...", '
            '"mood": "happy|smug|shocked|gaming|hungry|sleepy|love"}'
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.6 if strict else 0.85,
                max_tokens=300,
            )
            raw = strip_think(resp.choices[0].message.content or "")
            matched = re.search(r"\{.*\}", raw, re.S)
            if not matched:
                logger.info("冷场找话题：分享文本输出无法解析 %r", raw[:120])
                return {}
            data = json.loads(matched.group(0))
            if not isinstance(data, dict):
                return {}
            text = " ".join(str(data.get("text") or "").split())[:120]
            topic = " ".join(str(data.get("topic") or "").split())[:16]
            evidence = " ".join(str(data.get("evidence") or "").split())[:60]
            return {
                "text": text,
                "topic": topic,
                "evidence": evidence,
                "mood": str(data.get("mood") or "happy"),
            }
        except Exception as exc:
            logger.warning("冷场找话题：分享文本调用失败: %s", exc)
            return {}

    def build_daily_life_prompt(
        self,
        date_str: str,
        weekday: str,
        season_hint: str,
        recent_titles: list[str],
        slots_spec: list[tuple[str, str, str]],
        extra: str = "",
    ) -> str:
        """「编今天这一天」的提示词。CodeBuddy 和 DeepSeek 共用这一份，换后端不改文风。

        人格卡整块给进来（编译卡上限 6000 字，这里只取前 1400）：这一天要像她过的，
        不是像某个模板套出来的——爱吃、爱打游戏、有点懒、说话短，这些都得从卡里来。
        """
        recent = "\n".join(f"- {t}" for t in recent_titles[-24:]) or "（没有记录，这是第一天）"
        return (
            f"你在替微信群里的「{settings.BOT_NAME}」编她今天这一天。她是一个普通人，"
            "不是助手也不是 AI，现在是她的生活，不是任务清单。\n"
            f"今天是 {date_str} {weekday}，{season_hint}。\n"
            f"【她的人格卡（语气、爱好、生活习惯都以这张卡为准）】\n{load_card()[:1400]}\n"
            f"【她前几天干过的事】（别重复；能接着昨天的话头更好）\n{recent}\n"
            + (f"【额外偏好】{extra}\n" if extra else "")
            + "请编出她今天这一天，按下面这些时间点各写一件事：\n"
            + "\n".join(f"- {at} {kind}：{hint}" for at, kind, hint in slots_spec)
            + "\n要求：\n"
            "1. 每条都是发生在她自己身上的具体小事，要能说出「在哪儿、是什么、谁说了什么」；\n"
            "2. 不能是「出去转了转」「看了会儿手机」这种空话，也别写成段子合集——普通的一天，"
            "有一两件值得说的就够了，其余平淡；\n"
            "3. 一天之内要连贯：上午买的东西晚上可以吃，出门淋了雨回家就换衣服、鞋还没干，"
            "熬夜了下午就困；\n"
            "4. 只写她自己的日常，不碰真实新闻、明星、政事、灾害、他人隐私，也别报价格和型号"
            "（随手买的便宜零食除外）；\n"
            "5. 时间要跟时段对得上：早上别在打排位，晚上别在挤地铁；\n"
            "6. interest 给 1~5 分，表示「这件事值不值得在群里说」，一天里最多两条打 4 分以上；\n"
            "7. title 不超过 16 字；detail 不超过 45 字，而且必须是说完整的一句话"
            "（别写到一半断掉，宁可少说点）。\n"
            "只输出一个 JSON 对象，不要解释、不要用 Markdown 代码块、不要调用任何工具、"
            "不要读写文件：\n"
            '{"slots": [{"at": "09:40", "title": "...", "detail": "...", '
            '"interest": 4, "mood": "happy"}, ...]}'
        )

    @staticmethod
    def parse_daily_life_json(raw: str) -> list[dict]:
        """从返回里抠出 slots 数组。

        CodeBuddy 偶尔会带一段解释或 ```json 围栏再给 JSON，所以只认第一个 { 到最后一个 }；
        实在解不出来就当没生成，由调用方回退，不猜。
        """
        body = strip_think(raw or "")
        matched = re.search(r"\{.*\}", body, re.S)
        if not matched:
            if body.strip():
                logger.info("今日生活：生成结果无法解析 %r", body[:120])
            return []
        try:
            data = json.loads(matched.group(0))
        except json.JSONDecodeError:
            logger.info("今日生活：生成结果不是合法 JSON %r", body[:120])
            return []
        items = data.get("slots") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    def generate_daily_life(
        self,
        date_str: str,
        weekday: str,
        season_hint: str,
        recent_titles: list[str],
        slots_spec: list[tuple[str, str, str]],
        extra: str = "",
    ) -> list[dict]:
        """编出「今天这一天」：按作息骨架给出 8 件发生在她身上的具体小事（DeepSeek 版）。

        为什么要一次编一整天，而不是每次现编一段：一天之内要有因果。上午买的东西
        晚上还在吃，出门淋了雨回家鞋还没干——分开编就会互相打架，听起来像两个人在过日子。
        前几天的事一起喂进来，是为了不重复（不能天天捡猫），也能接着昨天的话头。
        """
        if not settings.has_valid_api_key:
            return []
        prompt = self.build_daily_life_prompt(
            date_str, weekday, season_hint, recent_titles, slots_spec, extra
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=1.0,
                max_tokens=900,
            )
            return self.parse_daily_life_json(resp.choices[0].message.content or "")
        except Exception as exc:  # noqa: BLE001 - 编不出今天的生活不该影响冒泡
            logger.warning("今日生活：生成失败: %s", exc)
            return []

    def compose_life_share(self, event: dict, when_label: str, recent_lines: list[str]) -> dict:
        """把「今天自己身上发生的一件小事」写成一句群聊话。

        和 ``compose_topic_share`` 的区别：那边有搜回来的材料可以核对，这边是编出来的一天，
        没有原文可比。所以闸门换成「具体」——必须说出在哪儿、什么东西、什么味道，
        含糊的（「今天遇到点好笑的事」）重写一遍，还含糊就这轮不发。
        """
        if not settings.has_valid_api_key:
            return {}
        extra = tuning.judge_extra("life").strip()
        recent = "\n".join(recent_lines[-8:]) or "（最近没人说话）"
        detail = str(event.get("detail") or "").strip()
        prompt = (
            f"你是微信群里的「{settings.BOT_NAME}」，是群友不是助手。"
            f"现在是 {datetime.now().strftime('%m月%d日 %H:%M')}，群里安静了很久。\n"
            f"你今天这一小段：{when_label}，{event.get('kind')}——{event.get('title')}"
            + (f"（{detail}）" if detail else "")
            + "\n"
            f"【你的人设（只学语气，别照抄句子）】\n{load_card()[:900]}\n"
            f"【最近群聊】（只用来避免驴唇不对马嘴，别接着聊）\n{recent}\n"
            + (f"【额外偏好】{extra}\n" if extra else "")
            + "你想把这件事随口发到群里。要求：\n"
            "1. 只讲这一件事，1~2 句、20~45 字；\n"
            "2. 必须具体：在哪儿、买了什么、看到了什么、谁说了什么，得写出来；"
            "不许写成「今天挺有意思」「遇到点好笑的事」这种没有内容的概括；\n"
            f"3. 时间要用「{when_label}」这种说法，别说得像刚发生（除非确实是刚才）；\n"
            "4. 第一人称口语，可以带一句自己的评价或吐槽，像随手发的微信；\n"
            "5. 只能讲这一件事，可以补自己的感受，但别给她加新的经历（又出门一趟、又买了什么），"
            "那些她自己也不记得，后面聊起来会前后打架；\n"
            "6. 不要「今日分享」「跟你们说个事」这种开头，不要列点、不要链接、不要 Markdown，"
            "不要用「你们呢」「有人吗」收尾；\n"
            "7. 这是你自己的日常，别扯上新闻、明星、别人的隐私。\n"
            '输出 JSON：{"text": "...", "topic": "不超过10字的主题词", '
            '"mood": "happy|smug|shocked|gaming|hungry|sleepy|love"}'
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.9,
                max_tokens=220,
            )
            raw = strip_think(resp.choices[0].message.content or "")
            matched = re.search(r"\{.*\}", raw, re.S)
            if not matched:
                logger.info("今日生活：分享文本输出无法解析 %r", raw[:120])
                return {}
            data = json.loads(matched.group(0))
            if not isinstance(data, dict):
                return {}
            return {
                "text": " ".join(str(data.get("text") or "").split())[:120],
                "topic": " ".join(str(data.get("topic") or "").split())[:16],
                "mood": str(data.get("mood") or "happy"),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("今日生活：分享文本调用失败: %s", exc)
            return {}

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

    def _material_lines(self, history: list[dict], max_chars: int = 120) -> list[str]:
        """把一天的消息摊成「[说话人]: 内容」的行，供夜间提炼/写日记用。"""
        lines: list[str] = []
        for m in history or []:
            content = (m.get("content") or "").strip()
            if not content:
                continue
            content = " ".join(content.split())
            lines.append(f"[{m.get('sender') or '群友'}]: {content[:max_chars]}")
        return lines

    def _extract_facts_chunk(self, lines: list[str], idx: int, total: int) -> list[dict]:
        """对一段聊天提炼最多 2 条事实，返回 [{text, tags}]。"""
        prompt = (
            "下面是群聊片段。请挑出最多 2 条「以后还会用到」的长期记忆，只挑真正有用的：\n"
            "- 群友的固定特征/身份/职业/习惯（谁在玩什么游戏、谁做什么工作、谁有什么梗）\n"
            "- 群里新出现的梗、外号、黑话\n"
            "- 明确的约定或要求（时间、规则、让机器人怎么做）\n"
            "- 群友自己的计划、进展、一直在做的项目\n"
            "不要记：一次性的闲聊和情绪、天气、新闻、密码证件银行卡，"
            "也不要记大肥鱼（机器人）自己是什么、能做什么、说过什么、有什么脾气——"
            "它是谁跟群友无关。\n"
            "同一个人/同一件事只写一条，把零散信息并进那一句，别重复。\n"
            "每条一行，格式：内容（≤40字）|标签1,标签2\n"
            "如果这段里一条都不值得记，就只输出：无\n\n" + "\n".join(lines)
        )
        try:
            resp = self._create(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
            )
            raw = strip_think(resp.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("夜间 L1 提炼失败（第 %d/%d 段）: %s", idx, total, exc)
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
        """
        夜间批量提炼 L1：从「当天全部聊天」里挑出最多 N 条「以后还用得上」的事实。

        为什么需要这一步：这套四层框架原本假设模型在对话里自己调 remember_fact，
        但群里每轮都是独立的一问一答，实测 3 天 980 条消息只写进去 3 条节点（0.3%），
        识海等于空的。夜间用一次轻量调用把当天值得留的东西补录，是最省 token 的补法。

        以前只看「最近 40 条」——11-25 当天 266 条，覆盖 15%，上午下午全丢。
        现在按 60 条一段切分逐段提炼，再合并去重，全天内容都能进视野。
        写入仍走 memory_store.remember（去重 + 每日 8 条上限 + 隐私过滤）。
        """
        if not settings.has_valid_api_key or not recent_history:
            return []
        max_items = max_items or settings.NIGHTLY_FACT_MAX
        lines = self._material_lines(recent_history)
        if not lines:
            return []
        chunk_size = max(20, settings.NIGHTLY_EXTRACT_CHUNK)
        chunks = [lines[i : i + chunk_size] for i in range(0, len(lines), chunk_size)]
        if len(chunks) > settings.NIGHTLY_EXTRACT_MAX_CHUNKS:
            # 极端长的一天：均匀抽样，别一次调几十遍
            keep = settings.NIGHTLY_EXTRACT_MAX_CHUNKS
            step = len(chunks) / keep
            chunks = [chunks[int(i * step)] for i in range(keep)]
        candidates: list[dict] = []
        for idx, chunk in enumerate(chunks, 1):
            for fact in self._extract_facts_chunk(chunk, idx, len(chunks)):
                dup = any(
                    SequenceMatcher(None, c["text"], fact["text"]).ratio() >= 0.7 for c in candidates
                )
                if not dup:
                    candidates.append(fact)
        if len(candidates) > max_items:
            logger.info(
                "夜间 L1 提炼：%d 段共 %d 条候选，按上限保留前 %d 条",
                len(chunks),
                len(candidates),
                max_items,
            )
            candidates = candidates[:max_items]
        logger.info("夜间 L1 提炼：素材 %d 条 / %d 段，候选 %d 条", len(lines), len(chunks), len(candidates))
        return candidates

    def write_daily_diary(
        self,
        recent_history: list[dict],
        status: PetStatus,
        group_id: str = "",
        day: str = "",
    ) -> bool:
        """L2 日记：每晚以当前人设口吻写一条今天群里的细节与心情（≤200字）"""
        if not settings.has_valid_api_key or not recent_history:
            return False
        all_lines = self._material_lines(recent_history)
        if not all_lines:
            return False
        # 日记要覆盖「一整天」，不是只有睡前那 40 条：长的话按时间均匀抽样，
        # 保证上午、下午、晚上各段都有代表，而不是被晚上刷屏挤掉。
        cap = settings.NIGHTLY_DIARY_LINES
        if len(all_lines) > cap:
            step = len(all_lines) / cap
            lines = [all_lines[int(i * step)] for i in range(cap)]
        else:
            lines = all_lines
        role_name, voice_hint, _ = _persona_voice_profile()
        # 把今天已经写过的日记塞进提示词，否则「/写日记」多按两次就会得到两篇几乎一样的日记
        target_day = day or datetime.now().strftime("%Y-%m-%d")
        wrote_today = [
            d.get("内容", "")
            for d in memory_store.read_recent_diaries(limit=5, group_id=group_id)
            if d.get("时间", "")[:10] == target_day
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
            return memory_store.write_diary(
                mood.strip()[:8], body.strip(), group_id=group_id, day=target_day
            )
        except Exception as exc:
            logger.warning("生成 L2 每日日记失败: %s", exc)
            return False


brain = DaFeiYuBrain()
