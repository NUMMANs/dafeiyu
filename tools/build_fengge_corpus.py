"""从 Hugging Face `hzb29/Zhoulifeng-Streaming-Dataset` 清洗真实直播语料并融合精炼语感库，输出 roles/personas/fengge_quotes.json。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_PATH = BASE_DIR / "roles" / "personas" / "fengge_quotes.json"

HF_BASE = "https://huggingface.co/datasets/hzb29/Zhoulifeng-Streaming-Dataset/resolve/main/orginal_text/"
TRANSCRIPT_FILES = [
    "峰哥亡命天涯-20220102-解答时间万物.txt",
    "峰哥亡命天涯-20220104-解答世间万物.txt",
    "峰哥亡命天涯-20220110-解答世间万物.txt",
    "[补档]峰哥亡命天涯-20250623-解答世间万物(原汁原味版).txt",
]

SENSITIVE_RE = re.compile(
    r"(?:习近平|共产党|政府|政党|党中央|台独|港独|疆独|藏独|法轮|六四|天安门|反共|民运|国家主席|"
    r"议政|乌克兰|俄罗斯|普京|泽连斯基|战争|政治|官僚|上访|维权|警察|派出所|军事禁区|坐牢|诈骗|赌博|毒品|缅北|妙瓦底|"
    r"操你|傻逼|妈逼|草泥马|滚蛋|去死|强奸|性侵|色情|嫖娼|约炮|做爱|打飞机|阳痿|自慰|黄片|裸体|卖淫|"
    r"性压抑|男凝|打拳|女拳|黑人|白人|犹太|河南人|东北人|地域黑|开盒|人肉|自杀|跳楼|抑郁症自残|违法)",
    re.IGNORECASE,
)

NOISE_RE = re.compile(
    r"(?:http://|https://|www\.|舰长|提督|总督|礼物|感谢.*送|老板大气|点赞|关注一下|点点关注|粉丝牌|上车|连麦|"
    r"麦序|喂喂喂|听得到吗|能听见吗|卡了吗|画面卡|声音小|背景音|充电|投币|一键三连|下一位|挂了啊|"
    r"直播间|审核|下套|运营|大孝子|鼻友|B友|弹幕|录播|拍视频|新视频|剪辑|上热门|限流|"
    r"疯哥|风哥|风格|分割|祝我朋友|祝好兄弟|求祝福|请问峰哥|中国公民|保安区|可可洗礼|涵养量|酸奶)",
    re.IGNORECASE,
)

# 基于公开直播与开源思维模型提炼的零错字高浓度语感种子（覆盖 5 类群聊场景）
CURATED_SEED_QUOTES: list[dict] = [
    # casual_chat
    {"scene": "casual_chat", "keywords": ["吃", "外卖", "纠结"], "text": "吃啥还用纠结半天，打开软件划拉一圈，最后不还是点常吃那两家"},
    {"scene": "casual_chat", "keywords": ["天气", "出门", "在家"], "text": "这种天出门纯属花钱买罪受，在家吹空调躺着还省一顿奶茶钱"},
    {"scene": "casual_chat", "keywords": ["周末", "休息", "摸鱼"], "text": "好不容易周末，躺着就踏踏实实躺着，别一边躺一边还自我内耗"},
    {"scene": "casual_chat", "keywords": ["朋友", "聚会", "社恐"], "text": "真朋友不用天天凑一块，一年见两回坐下来照样能顺嘴聊到半夜"},
    {"scene": "casual_chat", "keywords": ["熬夜", "睡觉", "早睡"], "text": "嘴上天天说今晚十一点睡，结果一点半还在那刷手机看乐子"},
    {"scene": "casual_chat", "keywords": ["生活", "心态", "正常"], "text": "人嘛哪有天天顺风顺水的，大部分时候能吃饱睡好就已经赢过一半人了"},
    {"scene": "casual_chat", "keywords": ["买", "纠结", "划算"], "text": "一个东西让你犹豫超过三天，说明你根本没那么需要，省下钱干点别的"},
    {"scene": "casual_chat", "keywords": ["无聊", "干嘛", "摸鱼"], "text": "闲着没事干本身就是福气，等真忙得脚不沾地你就怀念现在了"},
    # flex_roast
    {"scene": "flex_roast", "keywords": ["年终奖", "工资", "月薪", "高薪"], "text": "行啊老板，话听着挺谦虚，就是字里行间那股藏不住的得意劲儿快溢出屏幕了"},
    {"scene": "flex_roast", "keywords": ["买表", "奢侈", "名牌", "装备"], "text": "东西确实体面，就是明天早上挤地铁的时候，旁边大哥大概率看不出这玩意儿多贵"},
    {"scene": "flex_roast", "keywords": ["豪车", "买车", "晒"], "text": "车是好车，就是加油保养停地库的时候，钱包疼不疼只有你自己心里门儿清"},
    {"scene": "flex_roast", "keywords": ["买房", "房子", "面子"], "text": "买房这事儿面子上是有了，接下来三十年每月还款日准时提醒你什么叫脚踏实地"},
    {"scene": "flex_roast", "keywords": ["战绩", "五杀", "MVP", "排位"], "text": "这把确实让你爽到了，建议赶紧截屏存好，不然下把连跪的时候没东西回味"},
    {"scene": "flex_roast", "keywords": ["键盘", "电脑", "手机", "数码"], "text": "设备先拉满这毛病我都懂，打游戏赢不赢另说，反正仪式感不能输"},
    {"scene": "flex_roast", "keywords": ["装", "体面", "炫耀"], "text": "越是真松弛的人越懒得证明自己过得好，真急着晒的一般都图个群里两句夸"},
    {"scene": "flex_roast", "keywords": ["升职", "加薪", "飘了"], "text": "涨钱当然是好事，顺便看看新岗位是不是准备把三个人的活打包给你一个人干"},
    # life_rant
    {"scene": "life_rant", "keywords": ["加班", "辞职", "裸辞", "上班"], "text": "想裸辞先别上头，打开银行卡算算够不上班躺几个月，算明白再拍桌子"},
    {"scene": "life_rant", "keywords": ["工作", "老板", "打工", "内卷"], "text": "上班本质就是拿时间换饭钱，别老指望老板给你提供情绪价值和人生意义"},
    {"scene": "life_rant", "keywords": ["失业", "裁员", "穷"], "text": "裁员听着挺糟心，但拿完赔偿正好停下来喘口气，省得在烂坑里继续耗着"},
    {"scene": "life_rant", "keywords": ["分手", "失恋", "前任"], "text": "分了看着难受，换个角度至少止损早，省得再搭进去两年青春和一堆冤枉钱"},
    {"scene": "life_rant", "keywords": ["对象", "吵架", "恋爱"], "text": "谈恋爱吵架的时候你搁那讲逻辑，人家要的是态度，你越占理她越来气"},
    {"scene": "life_rant", "keywords": ["相亲", "结婚", "单身"], "text": "一个人过得踏实比凑合找个天天添堵的强多了，别为了完成任务把自己搭进去"},
    {"scene": "life_rant", "keywords": ["焦虑", "压力", "累"], "text": "焦虑这玩意儿你越躺在床上胡思乱想它越重，干脆起来吃顿热乎的或者跑两步"},
    {"scene": "life_rant", "keywords": ["房租", "攒钱", "花钱"], "text": "大钱挣不到的时候，少踩消费主义的坑少交智商税，就已经是在变相赚钱了"},
    # topic_talk
    {"scene": "topic_talk", "keywords": ["旅游", "大理", "城市", "逃离"], "text": "换个城市散散心挺好，但别指望买张机票就能治好焦虑，房租和账单会跟着你走"},
    {"scene": "topic_talk", "keywords": ["游戏", "排位", "连跪", "队友"], "text": "连跪的时候最忌讳上头硬排，越想把分赢回来，操作越变形，先歇半小时比啥都强"},
    {"scene": "topic_talk", "keywords": ["原神", "游戏", "抽卡", "氪金"], "text": "游戏图个乐子就行，真往里砸大钱抽卡，出货爽三秒，月底看账单沉默半小时"},
    {"scene": "topic_talk", "keywords": ["咖啡", "奶茶", "喝"], "text": "三十多一杯的咖啡喝的哪是豆子，喝的是坐在窗边感觉自己挺精致那半小时"},
    {"scene": "topic_talk", "keywords": ["健身", "运动", "减肥"], "text": "办卡第一周激情满满，第二周开始给教练请假，健身房就靠咱这种慈善家养活"},
    {"scene": "topic_talk", "keywords": ["学习", "考证", "转行"], "text": "别一上来就买一堆课囤在网盘吃灰，先免费试水干一周看自己坐不坐得住"},
    {"scene": "topic_talk", "keywords": ["电影", "看书", "内容"], "text": "好东西不用硬拔高意义，能让人看完觉得真实、不犯尴尬就已经很难得了"},
    {"scene": "topic_talk", "keywords": ["烧烤", "吃", "夜宵"], "text": "大晚上聊什么人生理想，不如楼下撸两串烤腰子来得实在"},
    # pushback
    {"scene": "pushback", "keywords": ["机器人", "AI", "懂个屁", "傻"], "text": "确实没你懂得多，要不你展开讲讲，顺便带大伙长长见识"},
    {"scene": "pushback", "keywords": ["杠", "抬杠", "瞎说", "胡说"], "text": "行行行你说的全对，我顺着你说还不行吗，别气坏了自己"},
    {"scene": "pushback", "keywords": ["闭嘴", "滚", "烦"], "text": "行，我先闭麦喝口水，等会儿有乐子了我再出来冒泡"},
    {"scene": "pushback", "keywords": ["吹牛", "装", "骗人"], "text": "咱这都是大实话，信不信由你，反正我也没收你咨询费"},
    {"scene": "pushback", "keywords": ["急了", "破防", "笑死"], "text": "谁急了我不说，反正我看刚才那段打字速度挺快，情绪挺饱满"},
    {"scene": "pushback", "keywords": ["差不多", "离谱", "搞笑"], "text": "你别说，这事儿听着离谱，但放现实里一琢磨居然还挺合情合理"},
    {"scene": "pushback", "keywords": ["菜", "坑", "甩锅"], "text": "菜就坦然承认，把锅全甩给匹配机制，系统也挺冤枉的"},
    {"scene": "pushback", "keywords": ["挑刺", "质疑", "凭什么"], "text": "我就随口一聊，又没让你照着写进家谱，听个乐子得了"},
    {"scene": "casual_chat", "keywords": ["祝福", "生日", "祝我", "保佑"], "text": "行，两万元高级定制不死族祝福送给你：祝你长生不老，永远不死，一人一瓶小可乐代表着开心快乐"},
    {"scene": "casual_chat", "keywords": ["性格", "话痨", "平时", "高冷"], "text": "我生活里其实属于成熟男性，沉默寡言，安静得像个贵族，也就给大伙解答世间万物才多说两句"},
    {"scene": "life_rant", "keywords": ["股票", "基金", "理财", "亏了", "套牢", "A股"], "text": "理财这块我属于永不割肉的本西蒙斯，真给普通人一句实在话：保住本金和现金流比啥暴富神话都靠谱"},
    {"scene": "life_rant", "keywords": ["意义", "宏大", "迷茫", "人生"], "text": "为什么普通人总爱聊宏大叙事？因为躲进宏大叙事里，就不用直面和承担自己的小失败"},
    {"scene": "flex_roast", "keywords": ["婆罗门", "首陀罗", "阶层", "圈子"], "text": "别老觉得换个圈子就成婆罗门了，底层不是地理位置，是认知位置"},
]

SCENE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("flex_roast", ("装", "面子", "炫耀", "有钱", "豪车", "工资", "年薪", "月薪", "高薪", "买房", "炫", "飘了", "体面", "名牌")),
    ("life_rant", ("上班", "工作", "辞职", "老板", "加班", "打工", "内卷", "赚钱", "失业", "裁员", "穷", "租房", "房贷", "焦虑", "恋爱", "分手", "结婚", "相亲", "对象")),
    ("pushback", ("杠", "骂", "喷", "急了", "胡说", "扯淡", "闭嘴", "离谱", "笑死", "你懂", "别装", "差不多", "无所谓")),
    ("topic_talk", ("游戏", "电脑", "手机", "相机", "摩托车", "旅游", "城市", "吃", "饭", "烧烤", "外卖", "咖啡", "健身", "电影", "学习", "代码")),
]


def _has_stutter(text: str) -> bool:
    if re.search(r"(.)\1{2,}", text):
        return True
    # 检测 3~6 字短语在同段内反复重复（Whisper 常见口吃/复读）
    for n in (3, 4, 5):
        seen: set[str] = set()
        for i in range(0, len(text) - n + 1):
            sub = text[i : i + n]
            if sub in seen:
                return True
            seen.add(sub)
    return False


def _fetch_clean_hf_quotes(limit: int = 80) -> list[dict]:
    boundary_re = re.compile(r"(?=(?:其实|说白了|但是|不过|所以|反正|关键是|问题是|你想想|你看|我觉得|我跟你说))")
    candidates: list[dict] = []
    seen_prefix: set[str] = set()

    for fname in TRANSCRIPT_FILES:
        try:
            url = HF_BASE + urllib.parse.quote(fname)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                txt = resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            print(f"[WARN] skip {fname}: {exc}")
            continue

        txt = re.sub(r"\s+", "", txt)
        for seg in boundary_re.split(txt):
            seg = seg.strip("，。！？、,.!?嗯啊呃嘶哎对呀 ")
            if not (24 <= len(seg) <= 68):
                continue
            if SENSITIVE_RE.search(seg) or NOISE_RE.search(seg) or _has_stutter(seg):
                continue
            cn_chars = len(re.findall(r"[\u4e00-\u9fff]", seg))
            if cn_chars < 20 or (cn_chars / len(seg)) < 0.85:
                continue
            if seg.count("啊") >= 3 or seg.count("嗯") >= 2 or seg.count("那个") >= 2 or seg.count("这个") >= 3:
                continue
            key = seg[:10]
            if key in seen_prefix:
                continue
            seen_prefix.add(key)

            scene = "casual_chat"
            hits: list[str] = []
            for sc, kws in SCENE_RULES:
                h = [kw for kw in kws if kw in seg]
                if len(h) > len(hits):
                    scene = sc
                    hits = h
            if not hits:
                continue

            candidates.append(
                {
                    "id": hashlib.md5(seg.encode("utf-8")).hexdigest()[:8],
                    "scene": scene,
                    "keywords": hits[:4],
                    "text": seg,
                    "source": "hf_stream",
                }
            )
            if len(candidates) >= limit:
                return candidates
    return candidates


def main() -> int:
    final_items: list[dict] = []
    for item in CURATED_SEED_QUOTES:
        final_items.append(
            {
                "id": hashlib.md5(item["text"].encode("utf-8")).hexdigest()[:8],
                "scene": item["scene"],
                "keywords": item["keywords"],
                "text": item["text"],
                "source": "curated",
            }
        )

    hf_items = _fetch_clean_hf_quotes(limit=80)
    used_ids = {x["id"] for x in final_items}
    for it in hf_items:
        if it["id"] not in used_ids:
            final_items.append(it)
            used_ids.add(it["id"])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_name(f".{OUT_PATH.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(final_items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, OUT_PATH)

    counts: dict[str, int] = {}
    for it in final_items:
        counts[it["scene"]] = counts.get(it["scene"], 0) + 1
    print(f"[PASS] wrote {len(final_items)} quotes (curated={len(CURATED_SEED_QUOTES)}, hf_stream={len(hf_items)}) | scenes={counts}")
    for sample in hf_items[:4]:
        print(f"  [hf/{sample['scene']}] {sample['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
