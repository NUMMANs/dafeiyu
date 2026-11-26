#!/usr/bin/env python
"""夜间人格复盘：读当天群聊，找「不像人」的地方，微调人格与 L2/L2.5 判定。

用法：
    python tools/persona_review.py                 # 只看不改（默认）
    python tools/persona_review.py --apply         # 落盘微调（人设记录 / 判定补充 / 数值闸门）
    python tools/persona_review.py --day 2026-11-25 --apply
    python tools/persona_review.py --no-llm        # 只出统计报告，不调模型

为什么要「复盘 + 微调」而不是直接改代码：
- 人格文件里有一节「自我进化记录」，本来就是留给它自己长的，改这里不破坏模板结构；
- L2 / L2.5 的提示词是硬编码在 brain.py 里的，让模型写代码太危险，所以把「临时偏好」
  抽到 roles/judges/*.md，brain 每次判定时热加载拼接；
- 数值闸门（阈值、时间窗）走 data/tuning.json，bot_core / brain 每次用时读一次，
  改完不用重启 —— 半夜重启机器人会打断冒泡、还会在群里多发一条「重启回来」。

守门规则（模型只能建议，落盘由脚本决定）：
- 人设每晚最多 2 条、每条 ≤60 字、累计 15 条，去重，出现「喵/塔菲/峰哥/AI/模型/机器人/助手」直接丢；
- 判定补充每晚整体重写、最多 5 条、每条 ≤45 字，出现「输出/格式/JSON/YES/NO」直接丢；
- 数值每晚只准动 1 项，且必须落在 tuning.BOUNDS 内、单次变动不超过预设步长；
- 任一环节失败就整轮放弃并写报告，绝不半途改一半。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "roles"))

from config import DATA_DIR, settings  # noqa: E402
import tuning  # noqa: E402
import build_card as card_mod  # noqa: E402  (roles/build_card.py)

REVIEW_DIR = DATA_DIR / "persona_review"
JUDGE_DIR = BASE_DIR / "roles" / "judges"
JUDGE_ARCHIVE = DATA_DIR / "judge_tuning" / "history"
LOG_DIR = BASE_DIR / "wechatauto_logs"

MAX_PERSONA_ITEMS = 2
MAX_PERSONA_CHARS = 60
MAX_JUDGE_ITEMS = 5
MAX_JUDGE_CHARS = 45

PERSONA_BANNED = ("喵", "塔菲", "峰哥", "AI", "模型", "机器人", "助手", "客服", "作为一个")
# 只挡「改输出格式」的措辞。YES/NO 不能挡 —— 判定规则本来就要写「什么情况判 YES」，
# 早先连这个词一起挡，结果 L2 的建议全被丢掉，等于这一层从没调过。
JUDGE_BANNED = ("输出格式", "只输出", "JSON", "返回格式", "分数", "阈值", "只回")
# L2.5 只该回答「这段内容值不值得吐槽」，「安静多久、要不要开口」是 TOPIC_GAP_SEC 的活。
# 实测模型会写出「冷场超过 5 分钟、无人 @ 我：0 分，不找话」这种规则，
# 效果等于把 L2.5 整层关掉 —— 所以凡是碰时间/冷场/有没有人叫我的措辞，一律不收。
L25_BANNED = ("冷场", "安静", "间隔", "分钟", "小时", "无人@", "没人@", "@我", "别开口", "不找话")

# 代码自己发的消息，不是模型说的，复盘时要剔掉，否则会把系统播报当成「它话多」
SYSTEM_PREFIXES = (
    "🛰️", "🔧", "⚙️", "📊", "✅", "⏱️", "⚠️", "🍚", "⏰", "📢", "🛠️", "🐟",
    "【", "本次花费", "大肥鱼 ·", "已向", "任务完成", "后台任务",
)
SYSTEM_MARKERS = (
    "检测到 Git 新提交", "检测到远程新提交", "重启回来", "机械汇报", "本次花费",
    "后台任务结果", "代码已更新", "自动更新", "退出以触发看门狗",
)

DAY_HOUR_CUTOFF = 5  # 凌晨 5 点前算「昨天」，和夜间养护的口径一致


# ----------------------------------------------------------------------
# 时间与数据
# ----------------------------------------------------------------------
def logical_day(now: datetime | None = None) -> date:
    now = now or datetime.now()
    if now.hour < DAY_HOUR_CUTOFF:
        return (now - timedelta(days=1)).date()
    return now.date()


def day_bounds(day: date) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def load_messages(day: date) -> dict[str, list[dict]]:
    """直接从 sqlite 读，不 import pet_engine —— 复盘进程不该拉起机器人的任何状态。"""
    import sqlite3

    db = DATA_DIR / "dafeiyu.db"
    if not db.is_file():
        return {}
    start, end = day_bounds(day)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT group_id, sender, role, content, created_ts
            FROM chat_history
            WHERE created_ts >= ? AND created_ts < ?
            ORDER BY id ASC
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()

    groups: dict[str, list[dict]] = {}
    for r in rows:
        content = (r["content"] or "").strip()
        if not content:
            continue
        groups.setdefault(r["group_id"], []).append(
            {
                "sender": r["sender"] or "群友",
                "role": r["role"],
                "content": content,
                "ts": float(r["created_ts"]),
            }
        )
    return groups


def is_system_message(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return True
    if body.startswith(SYSTEM_PREFIXES):
        return True
    return any(mark in body for mark in SYSTEM_MARKERS)


def split_speech(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """(人说的话, 代码说的话)。人说的话 = 群友 + 模型真实回复。"""
    human, machine = [], []
    for m in messages:
        if m["role"] == "assistant" and is_system_message(m["content"]):
            machine.append(m)
        else:
            human.append(m)
    return human, machine


# ----------------------------------------------------------------------
# 统计
# ----------------------------------------------------------------------
def _pct(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def _percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(len(ordered) * ratio) - 1))
    return ordered[idx]


def compute_stats(human: list[dict], machine: list[dict], counters: dict) -> dict:
    user_msgs = [m for m in human if m["role"] == "user"]
    bot_msgs = [m for m in human if m["role"] == "assistant"]
    lengths = [len(m["content"]) for m in bot_msgs]

    # 每一条回复之后 5 分钟内有没人接话（同一人连发算一次发言）
    no_reply = 0
    for i, m in enumerate(human):
        if m["role"] != "assistant":
            continue
        later = [
            x for x in human[i + 1 :] if x["ts"] - m["ts"] <= 300
        ]
        if not any(x["role"] == "user" for x in later):
            no_reply += 1

    # 连发：两条以上机器人消息之间没有群友说话
    self_talk = 0
    streak = 0
    for m in human:
        if m["role"] == "assistant":
            streak += 1
        else:
            if streak >= 2:
                self_talk += 1
            streak = 0
    if streak >= 2:
        self_talk += 1

    hours = Counter(datetime.fromtimestamp(m["ts"]).hour for m in bot_msgs)
    night = sum(v for h, v in hours.items() if h < settings.QUIET_END_HOUR or h >= 23)

    l2 = counters.get("l2", {})
    l2_5 = counters.get("l2_5", {})
    return {
        "user_msgs": len(user_msgs),
        "bot_msgs": len(bot_msgs),
        "machine_msgs": len(machine),
        "bot_share_pct": _pct(len(bot_msgs), len(user_msgs) + len(bot_msgs)),
        "avg_len": round(sum(lengths) / len(lengths), 1) if lengths else 0,
        "p90_len": _percentile(lengths, 0.9),
        "over_60": sum(1 for n in lengths if n > 60),
        "over_120": sum(1 for n in lengths if n > 120),
        "no_reply": no_reply,
        "no_reply_pct": _pct(no_reply, len(bot_msgs)),
        "self_talk": self_talk,
        "night_msgs": night,
        "bot_by_hour": dict(sorted(hours.items())),
        "l2_call": l2.get("call", 0),
        "l2_hit": l2.get("hit", 0),
        "l2_hit_pct": _pct(l2.get("hit", 0), l2.get("call", 0)),
        "l25_call": l2_5.get("call", 0),
        "l25_hit": l2_5.get("hit", 0),
        "l25_hit_pct": _pct(l2_5.get("hit", 0), l2_5.get("call", 0)),
    }


def load_counters(day: date) -> dict:
    path = DATA_DIR / "统计" / f"counters_{day.isoformat()}.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def parse_log(day: date) -> dict:
    """从运行日志里挖 L2/L2.5 的真实判定结果（比计数器更有信息量）。"""
    path = LOG_DIR / f"app_{day.strftime('%Y%m%d')}.log"
    out = {"l2_yes": 0, "l2_no": 0, "l2_samples": [], "roast_scores": [], "roast_pass": 0}
    if not path.is_file():
        return out
    l2_re = re.compile(r"L2 追认判定 \[(.+?)\] @他人=(\w+): (.+?) -> (YES|NO)")
    roast_re = re.compile(r"吐槽判定 \[(.+?)\] ([\d.]+)/10 \(阈值 ([\d.]+)\) 槽点=(.+?) \| (.+)")
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                m = l2_re.search(line)
                if m:
                    if m.group(4) == "YES":
                        out["l2_yes"] += 1
                    else:
                        out["l2_no"] += 1
                        if len(out["l2_samples"]) < 8:
                            out["l2_samples"].append(f"{m.group(1)}: {m.group(3)[:40]}")
                    continue
                m = roast_re.search(line)
                if m:
                    out["roast_scores"].append(float(m.group(2)))
                    if m.group(4) not in ("无", ""):
                        out["roast_pass"] += 1
    except OSError:
        pass
    return out


# ----------------------------------------------------------------------
# 证据片段
# ----------------------------------------------------------------------
def build_episodes(human: list[dict], limit: int = 9) -> list[dict]:
    """给每条模型回复截一个上下文小窗。越「没人接」「越长」「连发」越值得看。"""
    episodes: list[dict] = []
    for i, m in enumerate(human):
        if m["role"] != "assistant":
            continue
        before = [x for x in human[:i] if x["role"] == "user"][-3:]
        after = []
        for x in human[i + 1 :]:
            after.append(x)
            if len(after) >= 2 or x["ts"] - m["ts"] > 600:
                break
        reply_window = [x for x in after if x["role"] == "user" and x["ts"] - m["ts"] <= 300]
        length = len(m["content"])
        score = 0.0
        if not reply_window:
            score += 3.0
        if length > 60:
            score += 1.5
        if length > 120:
            score += 1.5
        prev_is_bot = i > 0 and human[i - 1]["role"] == "assistant"
        if prev_is_bot:
            score += 2.0
        if before and (m["ts"] - before[-1]["ts"]) > 1800:
            score += 1.0  # 冷场里自己冒出来
        episodes.append(
            {
                "ts": m["ts"],
                "before": before,
                "reply": m["content"],
                "after": after,
                "answered": bool(reply_window),
                "score": score,
            }
        )
    episodes.sort(key=lambda e: (-e["score"], -e["ts"]))
    picked = episodes[:limit]
    picked.sort(key=lambda e: e["ts"])
    return picked


def format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def render_episodes(episodes: list[dict]) -> str:
    lines: list[str] = []
    for idx, ep in enumerate(episodes, 1):
        answered = "之后有人接话" if ep["answered"] else "之后 5 分钟没人理"
        lines.append(f"【片段 {idx}｜{format_time(ep['ts'])}｜{answered}】")
        for m in ep["before"]:
            lines.append(f"[{format_time(m['ts'])}] {m['sender'][:12]}: {m['content'][:80]}")
        lines.append(f"→ 大肥鱼({format_time(ep['ts'])}, {len(ep['reply'])}字): {ep['reply'][:160]}")
        for m in ep["after"][:2]:
            who = "大肥鱼" if m["role"] == "assistant" else m["sender"][:12]
            lines.append(f"[{format_time(m['ts'])}] {who}: {m['content'][:80]}")
        lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 提示词
# ----------------------------------------------------------------------
def build_prompt(day: date, stats: dict, log_info: dict, episodes: list[dict]) -> str:
    current_evo = card_mod._load_evolutions_list()
    current_l2 = tuning.judge_extra("l2") or "（无）"
    current_l25 = tuning.judge_extra("l2_5") or "（无）"
    knobs = tuning.current_knobs() or "（无覆盖，全走默认）"

    stats_block = (
        f"群友消息 {stats['user_msgs']} 条，大肥鱼发言 {stats['bot_msgs']} 条"
        f"（占 {stats['bot_share_pct']}%），另有代码自动播报 {stats['machine_msgs']} 条（不计入）。\n"
        f"发言长度：平均 {stats['avg_len']} 字，90 分位 {stats['p90_len']} 字，"
        f"超过 60 字 {stats['over_60']} 条，超过 120 字 {stats['over_120']} 条。\n"
        f"说完之后 5 分钟没人接：{stats['no_reply']} 次（{stats['no_reply_pct']}%）。\n"
        f"连着自说自话（两条以上中间没人说话）：{stats['self_talk']} 次。\n"
        f"深夜/静默时段发言：{stats['night_msgs']} 条。\n"
        f"L2 追认判定：调用 {stats['l2_call']} 次，判 YES {stats['l2_hit']} 次（{stats['l2_hit_pct']}%）；"
        f"日志里 YES {log_info.get('l2_yes', 0)} / NO {log_info.get('l2_no', 0)}。\n"
        f"L2.5 吐槽判定：调用 {stats['l25_call']} 次，放行 {stats['l25_hit']} 次"
        f"（{stats['l25_hit_pct']}%）；日志里 {len(log_info.get('roast_scores', []))} 次打分，"
        f"放行 {log_info.get('roast_pass', 0)} 次。"
    )
    if log_info.get("l2_samples"):
        stats_block += "\n被判 NO 的样本：" + "；".join(log_info["l2_samples"][:6])
    if log_info.get("roast_scores"):
        scores = log_info["roast_scores"]
        stats_block += f"\n吐槽分：最高 {max(scores):.0f}、平均 {sum(scores)/len(scores):.1f}、最低 {min(scores):.0f}。"

    return f"""你要复盘一个微信群机器人「大肥鱼」在 {day.isoformat()} 这一天的表现。
它的目标不是当一个好助手，而是**像一个真实的人**待在群里：有节制、有情绪、会说半截话、不句句都接。

# 当天数据
{stats_block}

# 真实对话片段（按「最值得反思」排序后按时间排列）
{render_episodes(episodes)}

# 它现在的人格进化记录（最多 15 条，旧的自然淘汰）
{chr(10).join('- ' + x for x in current_evo) or '（空）'}

# L2 判定器当前的补充规则（判断「这句是不是在接我的话」）
{current_l2}

# L2.5 判定器当前的补充规则（判断「冷场后这个话题值不值得吐槽」）
{current_l25}

# 当前数值覆盖
{knobs}

# 你的任务
只诊断「不像人」的地方，并给出最小幅度的修正。不要写代码，不要重写人格文件全文。

判断标准（按重要性）：
1. 话太多/句句都接/自说自话 —— 真人大部分时间在听；
2. 回复太长、太完整、结构太整齐（列点、总结、面面俱到）—— 真人在微信里不回长段落；
3. 接不住梗、接得太规矩、把玩笑当真解释 —— 真人会损回去或者干脆不理；
4. 冷场时硬找话说 —— 真人会让群冷着；
5. 内容跟上下文脱节（自言自语、突然转话题）。

对 L2 / L2.5 的修正要具体到「什么情况该放行、什么情况该拦」，能直接写进判定提示词。
对人格的修正要写成「习惯/态度」，不要写「我要更幽默」这种空话。

# 输出格式
只输出一个 JSON 对象，不要 markdown 代码块，不要解释：
{{
  "like_human_score": 1到10的整数,
  "diagnosis": ["最多5条，每条40字以内，必须引用片段编号，指出具体毛病"],
  "persona_evolution": ["0到2条，每条60字以内，第一人称，写成以后要改的习惯"],
  "judge_l2": ["0到4条，每条40字以内，只写什么情况判YES/什么情况判NO"],
  "judge_l2_5": ["0到4条，每条40字以内，只写什么情况给高分/给0分"],
  "knobs": {{"TOPIC_ROAST_MIN_SCORE": 0.75}},
  "note": "一句话总结今天"
}}
knobs 只允许出现这三个键，最多改一个，不改就给空对象：TOPIC_ROAST_MIN_SCORE(0.5~0.9)、FOLLOWUP_WINDOW_SEC(120~900)、TOPIC_GAP_SEC(300~1800)。
没有把握的字段给空数组，不要凑数。
"""


# ----------------------------------------------------------------------
# 模型调用
# ----------------------------------------------------------------------
def _cbc_binary() -> str:
    candidates = [
        (settings.CBC_BIN or "").strip(),
        str(Path.home() / ".codebuddy-cli" / "node_modules" / "@tencent-ai" / "codebuddy-code" / "bin" / "codebuddy"),
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return ""


def call_codebuddy(prompt: str) -> str:
    binary = _cbc_binary()
    if not binary:
        raise RuntimeError("找不到 CodeBuddy CLI")
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    if not Path(node).is_file() and not shutil.which(node):
        raise RuntimeError("找不到 node")

    # 桌面端会给子进程注入产品身份变量，带着它们会按 workbuddy 身份找凭证，导致「未登录」
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("ACC_PRODUCT_CONFIG") and not k.startswith("CODEBUDDY_FORCE_")
    }
    cmd = [
        node,
        binary,
        "--model",
        settings.CBC_MODEL or "deepseek-v4.1-flash",
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--session-id",
        str(uuid.uuid4()),
    ]
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        cwd=str(BASE_DIR),
        env=env,
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError(f"CodeBuddy 无输出（exit={proc.returncode}）: {(proc.stderr or '')[:200]}")
    start, end = raw.find("["), raw.rfind("]")
    if start >= 0 and end > start:
        try:
            messages = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            messages = []
        for item in messages:
            if isinstance(item, dict) and item.get("type") == "result":
                if item.get("is_error"):
                    raise RuntimeError(f"CodeBuddy 返回错误: {str(item.get('result'))[:200]}")
                return str(item.get("result") or "")
    return raw


def call_deepseek(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.DEEPSEEK_API_KEY, base_url=settings.DEEPSEEK_BASE_URL)
    params = {
        "model": settings.DEEPSEEK_CHAT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 1600,
    }
    try:
        resp = client.chat.completions.create(
            **params, extra_body={"thinking": {"type": "disabled"}}
        )
    except Exception:
        resp = client.chat.completions.create(**params)
    return (resp.choices[0].message.content or "").strip()


def ask_model(prompt: str) -> tuple[str, str]:
    """先走 CodeBuddy（订阅额度），失败再走 DeepSeek 直连。返回 (文本, 用了谁)。"""
    backend = (settings.TOOL_BACKEND or "dsh").strip().lower()
    order = ["codebuddy", "deepseek"] if backend == "codebuddy" else ["deepseek", "codebuddy"]
    errors = []
    for name in order:
        try:
            if name == "codebuddy":
                return call_codebuddy(prompt), "codebuddy"
            return call_deepseek(prompt), "deepseek"
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError(" | ".join(errors))


def extract_json(text: str) -> dict:
    body = (text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\n?", "", body)
        body = re.sub(r"\n?```$", "", body).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出里没有 JSON")
    return json.loads(body[start : end + 1])


# ----------------------------------------------------------------------
# 落盘（全部带守门）
# ----------------------------------------------------------------------
def _clean_line(raw: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    text = text.strip("-•· 　")
    if len(text) > limit:
        text = text[:limit]
    return text


def _too_similar(a: str, b: str, threshold: float = 0.72) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= threshold


def apply_persona(items: list[str], day: date) -> list[str]:
    existing = card_mod._load_evolutions_list()
    snapshot = list(existing)
    # 同一天最多长 2 条：手动跑一次 + 夜间任务再跑一次，也不该长出 4 条
    today_tag = f"[{day.isoformat()}]"
    slots = MAX_PERSONA_ITEMS - sum(1 for old in existing if old.startswith(today_tag))
    added: list[str] = []
    for raw in items[: max(0, slots)]:
        text = _clean_line(raw, MAX_PERSONA_CHARS)
        if len(text) < 6:
            continue
        if any(word in text for word in PERSONA_BANNED):
            continue
        if any(_too_similar(text, old) for old in existing + added):
            continue
        added.append(text)
    if not added:
        return []

    merged = existing + [f"[{day.isoformat()}] {t}" for t in added]
    merged = merged[-card_mod.MAX_EVOLVE_ITEMS :]
    card_mod._save_evolutions_list(merged)
    try:
        card_mod.build_card()
    except Exception:
        card_mod._save_evolutions_list(snapshot)
        try:
            card_mod.build_card()
        except Exception:
            pass
        raise
    return added


def apply_judge(kind: str, items: list[str], day: date) -> list[str]:
    bullets: list[str] = []
    banned = JUDGE_BANNED + (L25_BANNED if kind == "l2_5" else ())
    for raw in items[:MAX_JUDGE_ITEMS]:
        text = _clean_line(raw, MAX_JUDGE_CHARS)
        if len(text) < 4:
            continue
        if any(word in text for word in banned):
            continue
        if any(_too_similar(text, old) for old in bullets):
            continue
        bullets.append(text)
    if not bullets:
        return []

    JUDGE_ARCHIVE.mkdir(parents=True, exist_ok=True)
    path = JUDGE_DIR / f"{kind}.md"
    if path.is_file():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, JUDGE_ARCHIVE / f"{kind}_{stamp}.md")
    label = "L2 追认判定补充规则" if kind == "l2" else "L2.5 冷场吐槽判定补充规则"
    lines = [
        f"# {label}",
        f"<!-- 由 tools/persona_review.py 夜间复盘自动维护，最后更新 {day.isoformat()} -->",
        "",
        *[f"- {b}" for b in bullets],
        "",
    ]
    JUDGE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return bullets


KNOB_STEPS = {
    "TOPIC_ROAST_MIN_SCORE": 0.05,
    "FOLLOWUP_WINDOW_SEC": 150,
    "TOPIC_GAP_SEC": 300,
}


def apply_knobs(knobs: dict, day: date) -> tuple[dict, list[str]]:
    if not isinstance(knobs, dict):
        return {}, []
    current = tuning.current_knobs()
    defaults = {
        "TOPIC_ROAST_MIN_SCORE": settings.TOPIC_ROAST_MIN_SCORE,
        "FOLLOWUP_WINDOW_SEC": settings.FOLLOWUP_WINDOW_SEC,
        "TOPIC_GAP_SEC": settings.TOPIC_GAP_SEC,
    }
    applied: dict = {}
    notes: list[str] = []
    for key, raw in knobs.items():
        if key not in tuning.BOUNDS or len(applied) >= 1:
            continue
        try:
            target = float(raw)
        except (TypeError, ValueError):
            continue
        low, high = tuning.BOUNDS[key]
        base = float(current.get(key, defaults.get(key, 0)))
        target = min(high, max(low, target))
        step = KNOB_STEPS.get(key, 0)
        if abs(target - base) > step:
            target = base + step if target > base else base - step
            target = min(high, max(low, target))
        if target == base:
            continue
        applied[key] = int(target) if isinstance(defaults.get(key), int) else round(target, 3)
        notes.append(f"{key}: {base:g} → {target:g}")

    if not applied:
        return {}, []

    TUNING_PATH = tuning.TUNING_PATH
    payload: dict = {}
    if TUNING_PATH.is_file():
        try:
            payload = json.loads(TUNING_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
    merged = dict(payload.get("knobs") or {})
    merged.update(applied)
    payload["knobs"] = merged
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["updated_by"] = "persona_review"
    payload["day"] = day.isoformat()
    payload["last_notes"] = notes
    TUNING_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return applied, notes


# ----------------------------------------------------------------------
# 报告
# ----------------------------------------------------------------------
def recent_scores(days: int = 7, exclude: date | None = None) -> list[tuple[str, int]]:
    """历史自评分，用来看「像人程度」是变好还是变坏（只是展示，不参与决策）。"""
    out: list[tuple[str, int]] = []
    if not REVIEW_DIR.is_dir():
        return out
    for path in sorted(REVIEW_DIR.glob("*.md")):
        if exclude and path.stem == exclude.isoformat():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"像人程度自评：\*\*(\d+) / 10\*\*", text)
        if m:
            out.append((path.stem, int(m.group(1))))
    return out[-days:]


def write_report(
    day: date,
    stats: dict,
    log_info: dict,
    plan: dict | None,
    changes: dict,
    note: str,
    applied: bool = True,
) -> Path:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / f"{day.isoformat()}.md"
    lines = [
        f"# 大肥鱼夜间复盘 · {day.isoformat()}",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 当天数据",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 群友消息 | {stats['user_msgs']} |",
        f"| 大肥鱼发言 | {stats['bot_msgs']}（{stats['bot_share_pct']}%） |",
        f"| 代码播报（剔除） | {stats['machine_msgs']} |",
        f"| 平均长度 / 90 分位 | {stats['avg_len']} / {stats['p90_len']} 字 |",
        f"| 超 60 字 / 超 120 字 | {stats['over_60']} / {stats['over_120']} 条 |",
        f"| 说完 5 分钟没人接 | {stats['no_reply']}（{stats['no_reply_pct']}%） |",
        f"| 连续自说自话 | {stats['self_talk']} 次 |",
        f"| L2 调用 / 放行 | {stats['l2_call']} / {stats['l2_hit']}（{stats['l2_hit_pct']}%） |",
        f"| L2.5 调用 / 放行 | {stats['l25_call']} / {stats['l25_hit']}（{stats['l25_hit_pct']}%） |",
        "",
    ]
    if plan:
        lines += [
            "## 诊断",
            "",
            f"像人程度自评：**{plan.get('like_human_score', '?')} / 10**",
            "",
        ]
        trend = recent_scores(exclude=day)
        if trend:
            lines.append(
                "近几晚：" + " → ".join(f"{d} {s}" for d, s in trend) + f" → {day.isoformat()} "
                f"{plan.get('like_human_score', '?')}"
            )
            lines.append("")
        lines += [f"- {d}" for d in plan.get("diagnosis", [])] or ["- （无）"]
        lines += ["", "## 本轮改动", ""]
    else:
        lines += ["## 本轮改动", ""]

    if changes.get("persona"):
        lines.append("**人格进化记录（data/persona_evolutions.json）**")
        lines += [f"- {x}" for x in changes["persona"]]
        lines.append("")
    if changes.get("judge_l2") or changes.get("judge_l2_5"):
        lines.append("**判定补充规则（roles/judges/）**")
        lines += [f"- L2：{x}" for x in changes.get("judge_l2", [])]
        lines += [f"- L2.5：{x}" for x in changes.get("judge_l2_5", [])]
        lines.append("")
    if changes.get("knob_notes"):
        lines.append("**数值闸门（data/tuning.json）**")
        lines += [f"- {x}" for x in changes["knob_notes"]]
        lines.append("")
    if changes.get("skipped"):
        lines += ["**被守门规则丢弃的建议**", ""]
        lines += [f"- {x}" for x in changes["skipped"]]
        lines += [""]
    if not any(changes.get(k) for k in ("persona", "judge_l2", "judge_l2_5", "knob_notes")):
        lines += ["本轮没有改动（证据不足或建议都撞了守门规则）。", ""]

    if plan and not applied:
        lines += ["## 模型建议（本轮未落盘）", ""]
        if plan.get("persona_evolution"):
            lines.append("**人格进化**")
            lines += [f"- {x}" for x in plan["persona_evolution"]]
            lines.append("")
        if plan.get("judge_l2"):
            lines.append("**L2 补充**")
            lines += [f"- {x}" for x in plan["judge_l2"]]
            lines.append("")
        if plan.get("judge_l2_5"):
            lines.append("**L2.5 补充**")
            lines += [f"- {x}" for x in plan["judge_l2_5"]]
            lines.append("")
        if plan.get("knobs"):
            lines.append("**建议改的数值**")
            lines += [f"- {k}: {v}" for k, v in plan["knobs"].items()]
            lines.append("")

    if note:
        lines += ["## 备注", "", note, ""]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def git_commit(paths: list[str], day: date) -> str:
    tracked = [p for p in paths if p]
    if not tracked:
        return "nothing to commit"
    subprocess.run(["git", "add", *tracked], cwd=str(BASE_DIR), check=False)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=str(BASE_DIR), capture_output=True, text=True
    )
    if not (diff.stdout or "").strip():
        return "no staged change"
    msg = (
        f"chore(persona): {day.isoformat()} 夜间复盘自动微调\n\n"
        "由 tools/persona_review.py 生成：人格进化记录、L2/L2.5 判定补充规则、数值闸门。\n"
        "人设记录与 tuning.json 在 data/ 下不入库，本次只提交 roles/judges 的规则文件。"
    )
    proc = subprocess.run(
        ["git", "commit", "-m", msg], cwd=str(BASE_DIR), capture_output=True, text=True
    )
    if proc.returncode != 0:
        return f"commit failed: {proc.stderr[:200]}"

    # 必须推回远端：机器人的自动更新每 60 秒跑一次 git pull --ff-only，
    # 本地领先远端时它会一直失败（还会把工作区 stash 掉），日志里刷满警告。
    push = subprocess.run(
        ["git", "push", "--quiet", "origin", "main"],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        timeout=90,
    )
    head = (proc.stdout or "").strip().splitlines()[0]
    return head if push.returncode == 0 else f"{head}（推送失败，待下次补推：{push.stderr[:120]}）"


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="大肥鱼夜间人格复盘")
    parser.add_argument("--day", default="", help="复盘哪一天（默认逻辑日：凌晨 5 点前算昨天）")
    parser.add_argument("--apply", action="store_true", help="真的写文件；不加只出报告")
    parser.add_argument("--no-llm", action="store_true", help="跳过模型，只出统计")
    parser.add_argument("--top", type=int, default=9, help="证据片段数量")
    args = parser.parse_args()

    day = date.fromisoformat(args.day) if args.day else logical_day()
    groups = load_messages(day)
    if not groups:
        print(f"[{day.isoformat()}] 没有聊天记录，跳过")
        return 0

    # 只复盘有真人说话的群（按消息量取最大那个，其余合并统计）
    main_group = max(groups.items(), key=lambda kv: len(kv[1]))[0]
    all_human: list[dict] = []
    all_machine: list[dict] = []
    for _, msgs in groups.items():
        h, m = split_speech(msgs)
        all_human += h
        all_machine += m

    counters = load_counters(day)
    stats = compute_stats(all_human, all_machine, counters)
    log_info = parse_log(day)
    episodes = build_episodes(all_human, limit=max(3, args.top))

    print(f"=== {day.isoformat()} 复盘 ===")
    print(
        f"群友 {stats['user_msgs']} 条 / 大肥鱼 {stats['bot_msgs']} 条 "
        f"({stats['bot_share_pct']}%) / 均长 {stats['avg_len']} 字 / "
        f"无人接 {stats['no_reply_pct']}% / L2 {stats['l2_hit_pct']}% / L2.5 {stats['l25_hit_pct']}%"
    )

    if args.no_llm:
        path = write_report(day, stats, log_info, None, {"skipped": ["--no-llm：未调用模型"]}, "")
        print(f"报告：{path}")
        return 0

    prompt = build_prompt(day, stats, log_info, episodes)
    try:
        raw, backend = ask_model(prompt)
    except Exception as exc:
        print(f"模型调用失败：{exc}", file=sys.stderr)
        write_report(day, stats, log_info, None, {"skipped": [f"模型调用失败：{exc}"]}, "")
        return 2

    try:
        plan = extract_json(raw)
    except Exception as exc:
        print(f"模型输出不是合法 JSON：{exc}\n--- 原文 ---\n{raw[:800]}", file=sys.stderr)
        write_report(day, stats, log_info, None, {"skipped": [f"JSON 解析失败：{exc}"]}, raw[:400])
        return 3

    print(f"模型：{backend}｜像人程度：{plan.get('like_human_score', '?')}/10")
    for d in plan.get("diagnosis", []):
        print(f"  · {d}")
    if plan.get("note"):
        print(f"  备注：{plan['note']}")

    changes = {
        "persona": [],
        "judge_l2": [],
        "judge_l2_5": [],
        "knob_notes": [],
        "skipped": [],
    }
    if not args.apply:
        changes["skipped"].append("未加 --apply，本轮不落盘")
    else:
        # 记录被丢掉的建议，方便第二天回看模型给了什么没被采纳
        def _diff(requested: list[str], applied: list[str], field: str) -> None:
            for item in requested or []:
                if not any(item[:12] in a for a in applied):
                    changes["skipped"].append(f"{field} 被拦：{str(item)[:60]}")

        try:
            persona_items = plan.get("persona_evolution") or []
            changes["persona"] = apply_persona(persona_items, day)
            _diff(persona_items, changes["persona"], "人格")

            l2_items = plan.get("judge_l2") or []
            changes["judge_l2"] = apply_judge("l2", l2_items, day)
            _diff(l2_items, changes["judge_l2"], "L2")

            l25_items = plan.get("judge_l2_5") or []
            changes["judge_l2_5"] = apply_judge("l2_5", l25_items, day)
            _diff(l25_items, changes["judge_l2_5"], "L2.5")

            _, changes["knob_notes"] = apply_knobs(plan.get("knobs") or {}, day)
        except Exception as exc:
            print(f"落盘失败，已回滚人设：{exc}", file=sys.stderr)
            write_report(day, stats, log_info, plan, {**changes, "skipped": [f"落盘失败：{exc}"]}, "")
            return 4

    path = write_report(
        day, stats, log_info, plan, changes, plan.get("note", ""), applied=args.apply
    )

    if args.apply and (changes["judge_l2"] or changes["judge_l2_5"]):
        print("git:", git_commit(["roles/judges"], day))

    print(f"报告：{path}")
    print(
        f"改动：人格 {len(changes['persona'])} 条 / L2 {len(changes['judge_l2'])} 条 / "
        f"L2.5 {len(changes['judge_l2_5'])} 条 / 数值 {len(changes['knob_notes'])} 项"
    )
    if changes["knob_notes"]:
        for n in changes["knob_notes"]:
            print(f"  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
