"""
夜间 L1/L2 的外部写入入口（供 WorkBuddy 定时任务 / DSH 调用，不经过机器人进程）

为什么要有它：夜间养护改成「素材=当天全部消息」以后，内容生成可以交给外部 agent，
但落库必须还走 memory 里那套闸门——去重（相似度 0.82 强化旧节点）、每日 8 条上限、
隐私正则、L2 每群每天 3 条 + 相似度 0.7 丢弃。绕开这两条写文件会把这套全废掉。

用法：
    # 1) 打印某天某群的全部聊天记录（给外部模型当素材）
    python tools/nightly_memory.py digest --group 橸䒜鎵㘣 --day 2026-11-25
    python tools/nightly_memory.py digest --group 橸䒜鎵㘣 --day 2026-11-25 --json out.jsonl

    # 2) 把提炼结果写进 L1/L2（JSON 文件，字段见下）
    python tools/nightly_memory.py write --group 橸䒜鎵㘣 --day 2026-11-25 --file payload.json

payload.json 形如：
    {
      "facts": [
        {"text": "枕猫常问晚上有没有人一起玩游戏", "tags": ["习惯"], "level": "normal"}
      ],
      "diary": {"mood": "有点心疼", "content": "60~160 字的日记正文"}
    }
facts / diary 都可以只给一个；level 只允许 normal / important（self 写入不允许 core）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import memory_store  # noqa: E402
from pet_engine import pet_engine  # noqa: E402


def _day_bounds(day: str) -> tuple[float, float]:
    start = datetime.strptime(day, "%Y-%m-%d")
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def cmd_digest(args: argparse.Namespace) -> int:
    start, end = _day_bounds(args.day)
    rows = pet_engine.get_history_between(args.group, start, end, limit=args.limit)
    if not rows:
        print(f"[空] {args.group} 在 {args.day} 没有聊天记录")
        return 0
    if args.json:
        out = Path(args.json)
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"已导出 {len(rows)} 条 → {out}")
        return 0
    print(f"# {args.group} · {args.day} · 共 {len(rows)} 条")
    for r in rows:
        t = datetime.fromtimestamp(float(r["created_ts"])).strftime("%H:%M")
        who = r.get("sender") or "群友"
        body = " ".join((r.get("content") or "").split())[:200]
        print(f"{t} [{r.get('role')}] {who}: {body}")
    return 0


def cmd_write(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    facts = payload.get("facts") or []
    diary = payload.get("diary") or {}
    written_facts = 0
    for fact in facts:
        text = str(fact.get("text") or "").strip()
        if not text:
            continue
        ok, msg = memory_store.remember(
            text=text,
            level=str(fact.get("level") or "normal"),
            tags=list(fact.get("tags") or []),
            source="self",
            group_id=args.group,
            raw=str(fact.get("raw") or ""),
        )
        print(("✅ " if ok else "⏭ ") + f"L1 {text[:50]} → {msg}")
        written_facts += 1 if ok else 0
    wrote_diary = False
    if diary.get("content"):
        wrote_diary = memory_store.write_diary(
            str(diary.get("mood") or "摸鱼"),
            str(diary.get("content")),
            group_id=args.group,
            day=args.day,
        )
        print(("✅ " if wrote_diary else "⏭ ") + "L2 日记" + ("已写入" if wrote_diary else "未写入（重复或超上限）"))
    print(f"汇总：L1 写入 {written_facts} 条 / L2 日记 {'1 条' if wrote_diary else '0 条'}")
    return 0


def cmd_groups(args: argparse.Namespace) -> int:
    rows = pet_engine.list_groups_with_history()
    if not rows:
        print("[空] 库里还没有任何群记录")
        return 0
    for g in rows:
        print(f"{g['group_id']}\t{g['cnt']} 条\t最近 {g['last']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="夜间 L1/L2 外部写入入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("groups", help="列出库里有聊天记录的群")
    g.set_defaults(func=cmd_groups)

    d = sub.add_parser("digest", help="导出某天某群的全部聊天记录")
    d.add_argument("--group", required=True)
    d.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"))
    d.add_argument("--limit", type=int, default=2000)
    d.add_argument("--json", default="", help="导出成 jsonl 文件（默认直接打印）")
    d.set_defaults(func=cmd_digest)

    w = sub.add_parser("write", help="把提炼结果写进 L1/L2")
    w.add_argument("--group", required=True)
    w.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"))
    w.add_argument("--file", required=True, help="payload.json 路径")
    w.set_defaults(func=cmd_write)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
