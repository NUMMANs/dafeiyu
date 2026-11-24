"""峰哥非模板化人设质量验收与回归脚本（使用与人格卡示范完全不重叠的新题盲测）。"""

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

# 与 fengge.人格.md 示范完全不同的 8 道全新群聊盲测题
BLIND_PROMPTS = [
    ("life_rant", "追了半年的相亲对象今天把我删了，好难受"),
    ("flex_roast", "刚提了辆新车，准备周末开回村里转一圈"),
    ("life_rant", "天天写PPT给领导画饼，感觉人生毫无意义"),
    ("topic_talk", "花两万块报了个摄影训练营，想转行拍大片"),
    ("pushback", "峰哥你整天就知道泼冷水，能不能说点正能量"),
    ("flex_roast", "在健身房练了一年终于有腹肌了，怎么发朋友圈不显得刻意"),
    ("casual_chat", "今天下雨懒得下楼买饭，好饿"),
    ("topic_talk", "打大乱斗对面五个长手折磨我二十分钟"),
]

AI_CLICHES = (
    "总的来说",
    "综上所述",
    "希望我的回答",
    "建议你保持",
    "不仅",
    "与其",
)
FENGGE_MARKERS = (
    "好事儿",
    "恰恰相反",
    "面子",
    "力工",
    "魅力男孩",
    "连接",
    "逃避劳动",
    "认知位置",
    "老弟",
    "小伙子",
    "兄弟",
    "B友",
    "我跟你说",
    "说实话",
    "不就那么回事",
    "老峰",
    "自欺欺人",
    "你急什么",
)
CATCHPHRASES = ("这是个好事儿", "想连接了", "力工思维", "恰恰相反")


def main() -> int:
    orig_active = ACTIVE_PERSONA_PATH.read_text(encoding="utf-8") if ACTIVE_PERSONA_PATH.is_file() else None
    try:
        ok1, msg1 = set_active_persona("塔菲")
        assert ok1 and get_active_persona() == "dafeiyu", msg1
        ok2, msg2 = set_active_persona("峰哥")
        assert ok2 and get_active_persona() == "fengge", msg2
        card_feng = load_card()
        assert "峰哥" in card_feng and len(card_feng) <= 6000

        r_list = bot_core._handle_command("g_eval", "tester", "/当前人设")
        assert r_list and "峰哥亡命天涯" in r_list.text

        h1 = retrieve_style_hints("fengge", "刚提了辆新车回村", group_id="g_eval_rag")
        h2 = retrieve_style_hints("fengge", "刚提了辆新车回村", group_id="g_eval_rag")
        assert h1 and h2 and h1 != h2
        print(f"[PASS] hot-switch & RAG verified (fengge card={len(card_feng)} chars)")

        if not settings.has_valid_api_key:
            print("[SKIP] no valid DEEPSEEK_API_KEY")
            return 0

        status = pet_engine.get_status()
        aff = {"user_name": "群友老王", "affinity": 45, "title": "熟脸", "feed_count": 2, "msg_count": 10}
        leak_count = 0
        cliche_count = 0
        marker_hits = 0
        catch_counts = {c: 0 for c in CATCHPHRASES}

        print("\n=== 升级版峰哥人设 8 道全新盲测题样张 ===")
        for scene, prompt in BLIND_PROMPTS:
            reply = brain.reply(
                sender="群友老王",
                text=prompt,
                status=status,
                user_affinity=aff,
                recent_history=[],
                group_id="g_eval_live2",
            )
            txt = reply.text
            print(f"[{scene}] 问：{prompt}\n       答：{txt.replace(chr(10), ' || ')}")
            if "塔菲" in txt or "喵" in txt:
                leak_count += 1
            if any(c in txt for c in AI_CLICHES):
                cliche_count += 1
            if any(m in txt for m in FENGGE_MARKERS):
                marker_hits += 1
            for cp in CATCHPHRASES:
                if cp in txt:
                    catch_counts[cp] += 1

        print(f"\n[METRICS] 串戏={leak_count}, AI套话={cliche_count}, 峰味辨识命中={marker_hits}/8, 单一热梗频次={catch_counts}")
        assert leak_count == 0, f"发现塔菲/喵串戏 ({leak_count})!"
        assert cliche_count == 0, f"发现 AI 客服套话 ({cliche_count})!"
        assert marker_hits >= 5, f"峰味辨识度偏低 ({marker_hits}/8)!"
        for cp, cnt in catch_counts.items():
            assert cnt <= 3, f"口头禅「{cp}」过度复读 ({cnt}/8)!"
        print("[PASS] high-identity & non-template fengge blind eval passed!")
        return 0
    finally:
        if orig_active is not None:
            ACTIVE_PERSONA_PATH.write_text(orig_active, encoding="utf-8")
        elif ACTIVE_PERSONA_PATH.exists():
            ACTIVE_PERSONA_PATH.unlink()


if __name__ == "__main__":
    sys.exit(main())
