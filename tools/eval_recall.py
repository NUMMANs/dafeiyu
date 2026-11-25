"""
L1 识海召回自检工具（只读，不改图、不强化）

用法：
    python tools/eval_recall.py            # 取最近 300 条群消息
    python tools/eval_recall.py 500

输出：注入率（有多少条消息会触发【你想到的旧事】）、每条记忆被召回次数、抽样明细。
改动召回算法前后各跑一次，数字对比才可信。
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3  # noqa: E402

from config import DATA_DIR  # noqa: E402
from memory import memory_store  # noqa: E402


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    conn = sqlite3.connect(DATA_DIR / "dafeiyu.db")
    rows = conn.execute(
        "select group_id, sender, content from chat_history "
        "where role='user' and content <> '' order by id desc limit ?",
        (limit,),
    ).fetchall()
    conn.close()
    if not rows:
        print("没有可用的历史消息")
        return

    injected = 0
    hit_counter: Counter[str] = Counter()
    samples: list[str] = []
    for group_id, sender, content in rows:
        items = memory_store.recall(content, top_n=5, group_id=group_id, reinforce=False)
        if items:
            injected += 1
            for it in items:
                hit_counter[it["text"][:28]] += 1
            if len(samples) < 8:
                samples.append(f"  「{content[:30]}」→ " + " / ".join(i["text"][:18] for i in items))

    total = len(rows)
    print(f"抽样消息：{total} 条")
    print(f"触发注入：{injected} 条（{injected / total:.1%}）")
    print(f"节点总数：{memory_store.stats()['total_nodes']}，被召回节点种类：{len(hit_counter)}")
    print("\n被召回最多的记忆：")
    for text, cnt in hit_counter.most_common(8):
        print(f"  {cnt:>4} 次  {text}")
    if samples:
        print("\n抽样明细：")
        print("\n".join(samples))


if __name__ == "__main__":
    main()
