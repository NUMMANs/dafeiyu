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

    # 3) 识海体检 / 整理（默认干跑，确认后加 --apply，写盘前自动备份）
    python tools/nightly_memory.py nodes --group 橸䒜鎵㘣
    python tools/nightly_memory.py prune --dupes --subject --meta          # 看会合并/删掉什么
    python tools/nightly_memory.py prune --all --todo --apply             # 真整理
    python tools/nightly_memory.py forget --id n1795621735287_64fd --apply

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
import re
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
    # 代码层再兜一道：提示词说最多 6 条，模型偶尔会多给。超出部分不写，
    # 免得一夜把识海灌满。
    if len(facts) > 8:
        print(f"⚠️ facts 给了 {len(facts)} 条，只取前 8 条")
    for fact in facts[:8]:
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
            # 夜间提炼不受「自然日 8 条」限制：00:02 自动养护已经写过几条，
            # 再按自然日卡，0:45 复盘就一条也写不进去。
            ignore_daily_limit=True,
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


def cmd_nodes(args: argparse.Namespace) -> int:
    rows = memory_store.list_nodes(args.group or None)
    if args.tag:
        rows = [r for r in rows if args.tag in (r.get("tags") or [])]
    if not rows:
        print("[空] 没有符合条件的节点")
        return 0
    print(f"共 {len(rows)} 条（权重高 → 低）")
    for r in rows[: args.limit]:
        ts = datetime.fromtimestamp(r["created"]).strftime("%m-%d") if r["created"] else "-"
        print(
            f"{r['id']}  {r['level']:9s} {ts}  hits={r['hits']:<4d} w={r['weight']:<4.1f} "
            f"g={r['group_id'] or '-'}"
        )
        print(f"    {r['text']}")
        if r["tags"]:
            print(f"    tags: {', '.join(r['tags'])}")
    return 0


# 「不记自己是什么」：夜间规则明确要求识海不存「我是机器人/我会自重启」这类元信息，
# 但模型偶尔还是会写进来。这里只做保守匹配，宁可漏删也不误删群友事实。
_META_PATTERNS = (
    re.compile(r"(大肥鱼|我|机器人).{0,8}(是|就是|属于).{0,10}(机器人|bot|ai|人工智能|电子宠物|程序|脚本)", re.I),
    re.compile(r"(我|大肥鱼).{0,6}(会|能|可以).{0,8}(自重启|自动重启|重启|写日记|整理群聊|发消息)", re.I),
)
# 技术待办不该进识海（那是 issue 的事）；默认只报告，加 --todo 才删
_TODO_HINTS = ("待办", "待优化", "待改", "TODO", "todo", "优化清单")


def _is_meta_node(text: str) -> bool:
    return any(p.search(text or "") for p in _META_PATTERNS)


def _is_todo_node(node: dict) -> bool:
    tags = " ".join(node.get("tags") or [])
    body = f"{tags} {node.get('text') or ''}"
    return any(h in body for h in _TODO_HINTS)


def cmd_prune(args: argparse.Namespace) -> int:
    """识海整理：合并同义节点 / 删元信息节点 / 清技术待办。

    默认干跑（只出清单，不改图）。确认清单没问题再加 --apply，会先备份再写盘。
    """
    group = args.group or None
    want_dupes = args.dupes or args.all
    want_subject = args.subject or args.all
    want_meta = args.meta or args.all
    want_todo = args.todo

    groups = (
        memory_store.find_duplicate_groups(group, by_subject=want_subject)
        if (want_dupes or want_subject)
        else []
    )
    nodes = memory_store.list_nodes(group)
    meta_nodes = [n for n in nodes if want_meta and _is_meta_node(n["text"])]
    todo_nodes = [n for n in nodes if want_todo and _is_todo_node(n)]

    print(f"识海体检：{len(nodes)} 条节点（group={group or '全部'}）")
    print("=" * 60)
    print(f"① 同义节点：{len(groups)} 组，可省 {sum(len(g) - 1 for g in groups)} 条"
          f"{'（含同主语）' if want_subject else ''}")
    for i, g in enumerate(groups, 1):
        print(f"  组{i} 保留 {g[0]['id']} hits={g[0]['hits']}：{g[0]['text'][:60]}")
        for dup in g[1:]:
            print(f"       合并 {dup['id']} hits={dup['hits']}：{dup['text'][:60]}")
    print(f"② 元信息节点（不记自己是什么）：{len(meta_nodes)} 条")
    for n in meta_nodes:
        print(f"   - {n['id']}：{n['text'][:70]}")
    print(f"③ 技术待办（默认不删，加 --todo 才清）：{len([n for n in nodes if _is_todo_node(n)])} 条")
    for n in todo_nodes:
        print(f"   ? {n['id']}：{n['text'][:70]}")

    if not args.apply:
        print("=" * 60)
        print("干跑结束，没有改动任何数据。确认后加 --apply 执行（会先备份 state/ 到 data/backups/）。")
        return 0

    if not groups and not meta_nodes and not todo_nodes:
        print("没有需要整理的内容。")
        return 0

    backup = memory_store.backup_memory(keep_last=7)
    print(f"已备份 → {backup.name}")
    merged = 0
    for g in groups:
        merged += memory_store.merge_nodes(g[0]["id"], [n["id"] for n in g[1:]])
    forgotten = memory_store.forget([n["id"] for n in meta_nodes + todo_nodes])
    left = memory_store.stats()["total_nodes"]
    print(f"完成：合并掉 {merged} 条、删除 {forgotten} 条，剩余 {left} 条节点。")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """按 id 手删节点（可一次给多个）。默认干跑，--apply 才写盘。"""
    ids = [i for chunk in args.id for i in str(chunk).split(",") if i.strip()]
    nodes = {n["id"]: n for n in memory_store.list_nodes()}
    targets = [(i, nodes.get(i)) for i in ids]
    missing = [i for i, n in targets if n is None]
    for i, n in targets:
        if n:
            print(f"待删 {i}：{n['text'][:70]}")
    if missing:
        print(f"⚠️ 找不到这些 id：{', '.join(missing)}")
    found = [i for i, n in targets if n]
    if not found:
        return 1
    if not args.apply:
        print(f"干跑：将删除 {len(found)} 条，加 --apply 才执行（会先备份）。")
        return 0
    backup = memory_store.backup_memory(keep_last=7)
    removed = memory_store.forget(found)
    print(f"已备份 → {backup.name}；删除 {removed} 条，剩余 {memory_store.stats()['total_nodes']} 条。")
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

    n = sub.add_parser("nodes", help="列出识海节点（只读）")
    n.add_argument("--group", default="", help="只看某个群（空=全部）")
    n.add_argument("--tag", default="", help="只看带某个标签的节点")
    n.add_argument("--limit", type=int, default=50)
    n.set_defaults(func=cmd_nodes)

    p = sub.add_parser("prune", help="整理识海：合并同义节点、删元信息节点（默认干跑）")
    p.add_argument("--group", default="", help="只整理某个群（空=全部）")
    p.add_argument("--dupes", action="store_true", help="合并高度相似的节点")
    p.add_argument("--subject", action="store_true", help="额外按「同一主语」合并（如四条萌王介绍）")
    p.add_argument("--meta", action="store_true", help="删「我是机器人/我会自重启」这类元信息节点")
    p.add_argument("--todo", action="store_true", help="删技术待办类节点（默认只报告）")
    p.add_argument("--all", action="store_true", help="等于 --dupes --subject --meta（不含 --todo）")
    p.add_argument("--apply", action="store_true", help="真的写盘（默认干跑，写盘前自动备份）")
    p.set_defaults(func=cmd_prune)

    f = sub.add_parser("forget", help="按 id 删除节点（默认干跑）")
    f.add_argument("--id", nargs="+", required=True, help="一个或多个节点 id，可用逗号分隔")
    f.add_argument("--apply", action="store_true", help="真的写盘（默认干跑，写盘前自动备份）")
    f.set_defaults(func=cmd_forget)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
