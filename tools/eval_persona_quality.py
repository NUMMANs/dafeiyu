"""峰哥非模板化人设质量验收与回归脚本。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT_DIR))

from bot_core import bot_core  # noqa: E402
from brain import brain  # noqa: E402
from config import settings  # noqa: E402
from persona_rag import retrieve_style_hints  # noqa: E402
from pet_engine import pet_engine  # noqa: E402
from roles.build_card import (  # noqa: E402
    ACTIVE_PERSONA_PATH,
    get_active_persona,
    load_card,
    set_active_persona,
)

BLIND_PROMPTS = [
    ("casual_chat", "今晚吃啥好纠结"),
    ("flex_roast", "刚发了年终奖，也就够买块三万的表吧"),
    ("life_rant", "天天加班累死了真想明天就裸辞"),
    ("topic_talk", "排位又连跪六把，队友全是人机"),
    ("pushback", "你个机器人懂个屁啊"),
    ("life_rant", "失恋了感觉天塌了"),
    ("topic_talk", "我想去大理旅居半年找人生意义"),
    ("flex_roast", "刚买了个四千块的机械键盘"),
]

AI_CLICHES = (
    "总的来说",
    "综上所述",
    "希望我的回答",
    "建议你保持",
    "不仅",
    "与其",
)
CATCHPHRASES = ("这是个好事儿", "差不多得了", "兄弟们", "怎么个事儿")


def main() -> int:
    orig_active = ACTIVE_PERSONA_PATH.read_text(encoding="utf-8") if ACTIVE_PERSONA_PATH.is_file() else None
    try:
        # 1. 热切换与 Git 洁净度检查
        ok1, msg1 = set_active_persona("塔菲")
        assert ok1 and get_active_persona() == "dafeiyu", msg1
        card_taffy = load_card()
        assert "塔菲" in card_taffy

        ok2, msg2 = set_active_persona("峰哥")
        assert ok2 and get_active_persona() == "fengge", msg2
        card_feng = load_card()
        assert "峰哥" in card_feng and len(card_feng) <= 6000

        r_list = bot_core._handle_command("g_eval", "tester", "/当前人设")
        assert r_list and "峰哥亡命天涯" in r_list.text

        git_dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "roles/大肥鱼.md", "roles/大肥鱼.人格.md", "roles/personas/fengge.人格.md"],
            cwd=str(BOT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        # Note: newly created untracked fengge.人格.md shows '??' before first commit, never ' M'
        assert not any(line.startswith(" M") or line.startswith("M ") for line in git_dirty.stdout.splitlines()), git_dirty.stdout
        print(f"[PASS] hot-switch & card compile clean (fengge={len(card_feng)} chars)")

        # 2. Persona RAG 检索与防重复检查
        h1 = retrieve_style_hints("fengge", "刚发了年终奖买块表", group_id="g_eval_rag")
        h2 = retrieve_style_hints("fengge", "刚发了年终奖买块表", group_id="g_eval_rag")
        assert h1 and h2 and h1 != h2, "persona_rag failed to deduplicate consecutive hints!"
        print("[PASS] persona_rag retrieval & anti-repeat verified")

        # 3. 8 场景实测盲测（需要有效 DEEPSEEK_API_KEY）
        if not settings.has_valid_api_key:
            print("[SKIP] no valid DEEPSEEK_API_KEY, skipping live LLM blind eval")
            return 0

        status = pet_engine.get_status()
        aff = {"user_name": "群友老王", "affinity": 45, "title": "熟脸", "feed_count": 2, "msg_count": 10}
        leak_count = 0
        cliche_count = 0
        catch_counts = {c: 0 for c in CATCHPHRASES}

        print("\n=== 峰哥人设 8 场景实测盲测样张 ===")
        for scene, prompt in BLIND_PROMPTS:
            reply = brain.reply(
                sender="群友老王",
                text=prompt,
                status=status,
                user_affinity=aff,
                recent_history=[],
                group_id="g_eval_live",
            )
            txt = reply.text
            print(f"[{scene}] 问：{prompt}\n       答：{txt.replace(chr(10), ' || ')}")
            if "塔菲" in txt or "喵" in txt:
                leak_count += 1
            if any(c in txt for c in AI_CLICHES):
                cliche_count += 1
            for cp in CATCHPHRASES:
                if cp in txt:
                    catch_counts[cp] += 1

        print(f"\n[METRICS] 串戏(塔菲/喵)={leak_count}, AI套话={cliche_count}, 口头禅频次={catch_counts}")
        assert leak_count == 0, f"发现塔菲/喵串戏 ({leak_count} 次)!"
        assert cliche_count == 0, f"发现 AI 客服套话 ({cliche_count} 次)!"
        for cp, cnt in catch_counts.items():
            assert cnt <= 2, f"口头禅「{cp}」过度复读 ({cnt}/8)!"
        print("[PASS] all anti-template & zero-leak blind eval checks passed!")
        return 0
    finally:
        if orig_active is not None:
            ACTIVE_PERSONA_PATH.write_text(orig_active, encoding="utf-8")
        elif ACTIVE_PERSONA_PATH.exists():
            ACTIVE_PERSONA_PATH.unlink()


if __name__ == "__main__":
    sys.exit(main())
