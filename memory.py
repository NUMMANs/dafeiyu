"""
四层记忆框架 · L1 识海图谱 + L2 日记/周结 + 自动备份（纯 Python 标准库，原子写）
- L0 人设层：roles/大肥鱼.人格.md + roles/大肥鱼.操作.md -> roles/大肥鱼.md
- L1 识海层：state/mind_graph.json（节点 + 标签共现边 + 艾宾浩斯衰减 + 2跳图扩散 + 命中强化）
- L2 日记层：data/日记/大肥鱼.jsonl + 周结 Markdown
- L3 会话层：SQLite chat_history（随时可清，不动 L0/L1/L2）
"""
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import json
import logging
import math
import re
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from config import BASE_DIR, DATA_DIR
import tuning

logger = logging.getLogger("dafeiyu.memory")

STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
MIND_GRAPH_PATH = STATE_DIR / "mind_graph.json"

DIARY_DIR = DATA_DIR / "日记"
DIARY_DIR.mkdir(parents=True, exist_ok=True)
DIARY_PATH = DIARY_DIR / "大肥鱼.jsonl"

BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

BASE_WEIGHT = {"core": 10.0, "important": 3.0, "normal": 1.0}
HALF_LIFE_DAYS = {"core": 3650.0, "important": 30.0, "normal": 7.0}
PRUNE_THRESHOLD = 0.06
MAX_NODES = 5000
DAILY_SELF_LIMIT = 8
RECALL_SCORE_THRESHOLD = 0.25
# 字面闸门：查询词 ≥3 个时必须命中 2 个二元组才算「相关」。
# 只命中 1 个词（例如「人」「天」这种单字）不足以证明这段记忆和当前这句有关，
# 否则每条记忆都会在每条消息上被召回一次（实测注入率 66%）。
MIN_LITERAL_HITS = 2
# 命中强化只加不减会让老节点变成「永磁铁」（weight 3.0 → 15.8、hits 160），
# 永远顶到打分因子上限 1.0。按等级封顶在基础权重的 2 倍：normal 最高 2.0、
# important 6.0、core 20.0，保持「core > important > normal」的等级秩序。
REINFORCE_CAP_MULT = 2.0
REINFORCE_STEP = 0.08

# 逻辑日以 04:00 为界。
# 夜间任务（0:15 / 0:45）写的是「昨天」的事，时间戳却落在新的一天；
# 按自然日算额度的话，零点后的每一条写入都会吃掉新一天的 DAILY_SELF_LIMIT，
# 白天再想记东西就被「今天已达上限」挡住。
LOGICAL_DAY_START_HOUR = 4


def _logical_day(ts: float) -> str:
    """时间戳属于哪个逻辑日（凌晨 0~4 点算前一天）。"""
    return (datetime.fromtimestamp(ts) - timedelta(hours=LOGICAL_DAY_START_HOUR)).strftime("%Y-%m-%d")


def _subject_key(text: str) -> str:
    """一句话的主语/主题键，用来在召回结果里做「同一件事只留一条」。

    识海里躺着四五条「我对萌王一心一意是…」，句子各不相同、说的是同一个人，
    文本相似度抓不住（0.39），但开头是同一个主语。这里取「引号里的名字」，
    没有引号就取开头一段（去掉标点），最长 12 字。
    """
    body = (text or "").strip()
    quoted = re.search(r"「([^」]{1,12})」", body)
    if quoted:
        return "「" + quoted.group(1) + "」"
    body = re.sub(r"^[\s\W_]+", "", body, flags=re.UNICODE)
    return body[:8]


def _token_jaccard(a: str, b: str) -> float:
    """两个句子的二元组重合度（0~1）。主语相同 + 用词重合才算「同一件事的两种说法」。"""
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _same_fact(a: str, b: str) -> bool:
    """两条记忆是不是同一件事。

    除文本相似度外，再认一种「包含关系」：短的那句整句出现在长句里
    （「我对萌王一心一意」/「萌王是主开发者，我对萌王一心一意」）。
    0.78 的相似度阈值对这类改写不敏感，2026-11-27 的复盘里就积了 3 条同义节点。
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    if SequenceMatcher(None, a, b).ratio() >= 0.78:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 8 and short in long

# 虚词过滤表（避免“她的/这个/就是”误召回无关记忆）
STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "这", "那",
    "有", "和", "就", "都", "也", "还", "又", "很", "太", "真", "好", "啊",
    "呀", "呢", "吧", "嘛", "吗", "哦", "嗯", "哈", "喵", "把", "被", "给",
    "到", "去", "来", "说", "看", "想", "要", "会", "能", "不", "没", "别",
    "一个", "什么", "怎么", "这个", "那个", "不是", "就是", "可以", "一下",
    "今天", "明天", "昨天", "现在", "刚才", "大家", "老哥", "大肥鱼", "塔菲",
}

# 敏感隐私过滤（绝不写入密码、令牌、身份证号、银行卡号）
SENSITIVE_RE = re.compile(
    r"(sk-[a-zA-Z0-9]{16,}|password|passwd|密码\s*[:：]?\s*\S+|\b\d{17}[\dXx]\b|\b\d{16,19}\b)",
    re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数（不调用 tokenizer，脱离外网也能算）。

    依据实测：中文约 1.5 字/token，英文与符号约 3~3.5 字符/token。
    这里刻意取偏大的系数（中文 1.4、其他 3.0），宁可早一点触发压缩。
    """
    s = text or ""
    if not s:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", s))
    other = len(s) - cjk
    return int(cjk / 1.4 + other / 3.0) + 1


def _tokenize(text: str) -> set[str]:
    """
    中文 2 字滑窗 + 英文单词，并过滤虚词。

    这里**刻意不再产出单字**：单字是召回噪声的主要来源。
    「大肥鱼」会被拆成 大/肥/鱼 三个单字，只要记忆里出现任意一个字就算命中，
    于是「今天有人玩吗」也能把「主人要求：@了别人但没@大肥鱼时不要接话」召回。
    二元组（大肥/肥鱼）才是一个中文词的最小可辨识单位。
    """
    t = (text or "").lower()
    tokens: set[str] = set()

    # 英文/数字词（如 lol, kda, python, 0-8）
    for w in re.findall(r"[a-z0-9_-]{2,}", t):
        if w not in STOPWORDS:
            tokens.add(w)

    # 中文连续段：2 字滑窗
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        for i in range(len(seg) - 1):
            bg = seg[i : i + 2]
            if bg in STOPWORDS:
                continue
            # 两个都是虚词组成的二元组（「了就」「的都」）没有信息量
            if bg[0] in STOPWORDS and bg[1] in STOPWORDS:
                continue
            tokens.add(bg)
    return tokens


def _effective_weight(node: dict[str, Any], now: float) -> float:
    level = node.get("level", "normal")
    base = float(node.get("weight", BASE_WEIGHT.get(level, 1.0)))
    owner_mult = 1.8 if node.get("source") == "owner" else 1.0
    updated = float(node.get("updated", node.get("created", now)))
    days = max(0.0, (now - updated) / 86400.0)
    half_life = HALF_LIFE_DAYS.get(level, 7.0)
    return base * owner_mult * math.exp(-days / half_life)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class FourLayerMemory:
    """L1 识海 + L2 日记/周结 + 自动备份管理器"""

    def __init__(self, graph_path: Path = MIND_GRAPH_PATH, diary_path: Path = DIARY_PATH):
        self._lock = threading.Lock()
        self.graph_path = graph_path
        self.diary_path = diary_path
        self._graph = self._load_graph()
        self._graph_mtime = self._current_mtime()

    def _current_mtime(self) -> float:
        try:
            return self.graph_path.stat().st_mtime
        except OSError:
            return 0.0

    def _maybe_reload_locked(self) -> None:
        """文件被外部改过（整理脚本 / 手工编辑）就重新加载。

        以前 `_save_graph_locked` 每次都把内存里那份整份写回：夜间跑完 prune 删掉的
        重复节点，机器人下一句话一强化就被旧图覆盖回来。现在写盘前先看一眼 mtime。
        """
        mtime = self._current_mtime()
        if mtime and mtime > self._graph_mtime + 0.5:
            graph = self._load_graph()
            self._graph = graph
            self._graph_mtime = mtime
            logger.info(
                "🔄 识海文件被外部更新（%s），已重新加载 %d 条节点",
                self.graph_path.name,
                len(graph.get("nodes") or []),
            )

    def _load_graph(self) -> dict[str, Any]:
        if self.graph_path.is_file():
            try:
                data = json.loads(self.graph_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("nodes"), list):
                    data.setdefault("version", 1)
                    data.setdefault("edges", [])
                    return data
            except Exception as exc:
                logger.warning("读取 mind_graph.json 失败，将初始化新识海: %s", exc)
        return {"version": 1, "nodes": [], "edges": []}

    def _save_graph_locked(self) -> None:
        _atomic_write_json(self.graph_path, self._graph)
        self._graph_mtime = self._current_mtime()

    def _rebuild_edges_for_node_locked(self, new_node: dict[str, Any]) -> None:
        new_id = new_node["id"]
        new_tags = set(new_node.get("tags") or [])
        if not new_tags:
            return
        edges = self._graph["edges"]
        existing_pairs = {(e[0], e[1]) for e in edges} | {(e[1], e[0]) for e in edges}
        for other in self._graph["nodes"]:
            oid = other["id"]
            if oid == new_id:
                continue
            shared = new_tags & set(other.get("tags") or [])
            if shared and (new_id, oid) not in existing_pairs:
                tag_name = next(iter(shared))
                edges.append([new_id, oid, 0.6, tag_name])
                existing_pairs.add((new_id, oid))

    def _count_today_self_writes_locked(self, now: float) -> int:
        # 按逻辑日（04:00 为界）算，别让夜间任务把新一天的额度吃掉
        today = _logical_day(now)
        cnt = 0
        for n in self._graph["nodes"]:
            if n.get("source") == "self":
                ts = float(n.get("created", 0))
                if ts > 0 and _logical_day(ts) == today:
                    cnt += 1
        return cnt

    def remember(
        self,
        text: str,
        level: str = "normal",
        tags: list[str] | None = None,
        source: str = "self",
        group_id: str = "",
        raw: str = "",
        ignore_daily_limit: bool = False,
    ) -> tuple[bool, str]:
        """写入一条记忆节点到 L1 识海（带隐私过滤、权限检查、每日 8 条上限、去重）

        ignore_daily_limit 只给夜间提炼用：L1 现在只由夜间复盘任务（0:45）写，
        任务本身有 6 条/晚的提示词约束和 8 条代码上限，不属于「日常自发膨胀」，
        不该被自然日额度挡住。
        """
        clean_text = (text or "").strip()[:200]
        if len(clean_text) < 3:
            return False, "记忆内容太短"
        if SENSITIVE_RE.search(clean_text) or SENSITIVE_RE.search(raw or ""):
            return False, "触发隐私安全保护，拒绝写入敏感凭据/证件号"

        level = level if level in BASE_WEIGHT else "normal"
        # 铁律：AI 自己不能写 core（core 仅限主人手动确认）
        if source == "self" and level == "core":
            level = "important"

        clean_tags = [str(t).strip()[:16] for t in (tags or []) if str(t).strip()][:6]

        with self._lock:
            self._maybe_reload_locked()
            now = time.time()
            if (
                source == "self"
                and not ignore_daily_limit
                and self._count_today_self_writes_locked(now) >= DAILY_SELF_LIMIT
            ):
                return False, f"今天主动记忆已达上限（{DAILY_SELF_LIMIT} 条），防膨胀拒绝写入"

            # 去重：同一件事（高相似或整句包含）就强化旧节点，不再重复写
            for existing in self._graph["nodes"]:
                if existing.get("group_id", "") == group_id:
                    if _same_fact(existing["text"], clean_text):
                        existing["updated"] = now
                        existing["hits"] = int(existing.get("hits", 0)) + 1
                        existing["weight"] = round(float(existing.get("weight", 1.0)) + 0.2, 3)
                        merged_tags = list(dict.fromkeys((existing.get("tags") or []) + clean_tags))[:8]
                        existing["tags"] = merged_tags
                        self._save_graph_locked()
                        return True, f"已强化已有相似记忆: {existing['text']}"

            node_id = f"n{int(now * 1000)}_{uuid.uuid4().hex[:4]}"
            node = {
                "id": node_id,
                "text": clean_text,
                "level": level,
                "tags": clean_tags,
                "source": source,
                "group_id": group_id,
                "created": round(now, 2),
                "updated": round(now, 2),
                "weight": BASE_WEIGHT[level],
                "hits": 0,
                "raw": (raw or clean_text)[:80],
            }
            self._graph["nodes"].append(node)
            self._rebuild_edges_for_node_locked(node)

            # 容量保护（上限 5000）
            if len(self._graph["nodes"]) > MAX_NODES:
                self._prune_overflow_locked(now)

            self._save_graph_locked()
            logger.info("🧠 L1 识海写入 [%s/%s]: %s (tags=%s)", level, source, clean_text, clean_tags)
            return True, f"已记入识海（{level}）: {clean_text}"

    def _prune_overflow_locked(self, now: float) -> None:
        nodes = self._graph["nodes"]
        nodes.sort(
            key=lambda n: (1 if n.get("level") == "core" else 0, _effective_weight(n, now)),
            reverse=True,
        )
        kept = nodes[:MAX_NODES]
        kept_ids = {n["id"] for n in kept}
        self._graph["nodes"] = kept
        self._graph["edges"] = [e for e in self._graph["edges"] if e[0] in kept_ids and e[1] in kept_ids]

    def recall(
        self,
        query: str,
        top_n: int = 5,
        group_id: str | None = None,
        reinforce: bool = True,
    ) -> list[dict[str, Any]]:
        """
        L1 识海召回算法：
        分词（过滤虚词）-> 字面命中（按 IDF 加权，专有名词一个词就够）-> 标签召回
        -> 沿标签边 2 跳图扩散 (×0.6) -> 有效权重加权 -> 低于阈值丢弃 -> 命中强化

        2026-11-28：原来只有「字面命中 ≥2 个二元组」一条路，而识海里多是
        「谁是谁/规则」这类元信息，群里聊装机、游戏时字面几乎零命中，注入率掉到 2%。
        现在两条路并行：
        - IDF 加权：一个高区分度的词（型号、人名、产品名）命中就够，不再要求凑两个；
        - 标签召回：夜间提炼打的 tags（DLSS5/A卡/周浩楠/群梗…）直接参与打分，
          太泛的分类标签（身份/习惯/项目，超过三成节点都有）自动忽略。
        """
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        with self._lock:
            self._maybe_reload_locked()
            now = time.time()
            nodes = self._graph["nodes"]
            if not nodes:
                return []
            # 夜间复盘可以热调这三个闸门（data/tuning.json），不用重启
            score_threshold = tuning.get_float("RECALL_SCORE_THRESHOLD", RECALL_SCORE_THRESHOLD)
            min_literal = tuning.get_int("MIN_LITERAL_HITS", MIN_LITERAL_HITS)

            # 群聊隔离：优先匹配当前群或全局（group_id == ""）的记忆
            valid_nodes: dict[str, dict[str, Any]] = {}
            for n in nodes:
                ng = n.get("group_id", "")
                if group_id and ng and ng != group_id:
                    continue
                valid_nodes[n["id"]] = n

            if not valid_nodes:
                return []

            # 每个词的 IDF：越少节点提到它，它越能证明「这句就是在说这件事」
            df: dict[str, int] = {}
            for n in valid_nodes.values():
                for tok in _tokenize(n["text"] + " " + " ".join(n.get("tags") or [])):
                    df[tok] = df.get(tok, 0) + 1
            node_total = len(valid_nodes)
            idf = {tok: math.log((node_total + 1) / (cnt + 0.5)) for tok, cnt in df.items()}
            max_idf = math.log((node_total + 1) / 1.5) or 1.0

            # 查询很短（1~2 个词）时命中 1 个就够，否则要求命中 min_literal 个
            min_hits = 1 if len(q_tokens) <= 2 else min_literal
            literal_scores: dict[str, float] = {}
            for nid, n in valid_nodes.items():
                doc_tokens = _tokenize(n["text"] + " " + " ".join(n.get("tags") or []))
                matched = q_tokens & doc_tokens
                if not matched:
                    continue
                # 命中词的区分度之和决定这条记忆够不够格：
                # 「大肥鱼」「群」这种到处都是的词几乎不加分，「dlss5」「枕猫」一个就顶用
                rel = min(1.0, sum(idf.get(t, 0.0) for t in matched) / (2 * max_idf))
                if len(matched) >= min_hits or rel >= 0.45:
                    literal_scores[nid] = rel

            # 标签召回：tags 是夜间提炼时人工挑的词，比 2 字滑窗准
            tag_index: dict[str, list[str]] = {}
            for nid, n in valid_nodes.items():
                for tag in n.get("tags") or []:
                    body = str(tag).strip().lower()
                    if len(body) >= 2:
                        tag_index.setdefault(body, []).append(nid)
            too_common = max(3, int(node_total * 0.3))
            q_low = (query or "").lower()
            tag_scores: dict[str, float] = {}
            for tag, ids in tag_index.items():
                if len(ids) > too_common:
                    continue  # 分类标签（身份/习惯/项目），区分度为零
                if tag not in q_low and not any(tok in tag for tok in q_tokens):
                    continue
                for nid in ids:
                    tag_scores[nid] = min(0.85, tag_scores.get(nid, 0.0) + 0.55)

            if not literal_scores and not tag_scores:
                return []

            relevance = dict(literal_scores)
            for nid, sc in tag_scores.items():
                relevance[nid] = max(relevance.get(nid, 0.0), sc)

            # 构建邻接表进行 2 跳图扩散
            adj: dict[str, list[tuple[str, float]]] = {}
            for e in self._graph["edges"]:
                u, v, w = e[0], e[1], float(e[2])
                if u in valid_nodes and v in valid_nodes:
                    adj.setdefault(u, []).append((v, w))
                    adj.setdefault(v, []).append((u, w))

            relevance = dict(literal_scores)
            # 1 跳扩散
            hop1: dict[str, float] = {}
            for nid, sc in literal_scores.items():
                for nb, ew in adj.get(nid, []):
                    cand = sc * ew
                    if cand > relevance.get(nb, 0.0):
                        hop1[nb] = max(hop1.get(nb, 0.0), cand)
            for nb, sc in hop1.items():
                relevance[nb] = max(relevance.get(nb, 0.0), sc)

            # 2 跳扩散
            for nid, sc in hop1.items():
                for nb, ew in adj.get(nid, []):
                    cand = sc * ew
                    if cand > relevance.get(nb, 0.0):
                        relevance[nb] = cand

            scored: list[tuple[float, dict[str, Any]]] = []
            for nid, rel in relevance.items():
                node = valid_nodes[nid]
                eff_w = _effective_weight(node, now)
                final_score = rel * (0.5 + min(eff_w, 12.0) / 24.0)
                if final_score >= score_threshold:
                    scored.append((final_score, node))

            scored.sort(key=lambda x: x[0], reverse=True)
            # 同一件事/同一个主语不必占好几个位置：识海里常年躺着四五条「萌王是主开发者」，
            # 以前 top5 全被它占满，等于每轮都在重复同一句话，还把真正相关的记忆挤掉。
            top_items: list[tuple[float, dict[str, Any]]] = []
            used_subjects: set[str] = set()
            for sc, node in scored:
                subject = _subject_key(node["text"])
                if subject and subject in used_subjects:
                    continue
                if any(_same_fact(node["text"], picked["text"]) for _, picked in top_items):
                    continue
                top_items.append((sc, node))
                if subject:
                    used_subjects.add(subject)
                if len(top_items) >= top_n:
                    break
            # 只留跟最相关那条分数差不多的：图扩散会把一串「沾点边」的旧事带上来，
            # 全塞进提示词等于给模型一堆无关素材，反而带偏回复。
            if len(top_items) > 1:
                floor = max(score_threshold, top_items[0][0] * 0.55)
                top_items = [(sc, n) for sc, n in top_items if sc >= floor]

            if reinforce and top_items:
                for _, node in top_items:
                    node["hits"] = int(node.get("hits", 0)) + 1
                    lvl = node.get("level", "normal")
                    base = BASE_WEIGHT.get(lvl, 1.0)
                    cap = round(base * REINFORCE_CAP_MULT, 3)
                    cur = float(node.get("weight", base))
                    node["weight"] = round(min(cur + REINFORCE_STEP, cap), 3)
                    node["updated"] = round(now, 2)
                self._save_graph_locked()

            logger.info(
                "L1 召回 %d 条｜阈值 %.2f｜字面 %d 条｜标签 %d 条｜query=%s",
                len(top_items),
                score_threshold,
                len(literal_scores),
                len(tag_scores),
                " ".join((query or "").split())[:24],
            )
            return [
                {
                    "id": n["id"],
                    "text": n["text"],
                    "level": n["level"],
                    "source": n.get("source", "self"),
                    "tags": n.get("tags", []),
                    "score": round(sc, 3),
                }
                for sc, n in top_items
            ]

    def render_context(self, query: str, group_id: str | None = None, max_chars: int = 400) -> str:
        """拼装本轮注入提示词；条数/字数/阈值都能被夜间复盘热调，没相关记忆时返回空串。"""
        top_n = tuning.get_int("RECALL_TOP_N", 5)
        limit = tuning.get_int("RECALL_MAX_CHARS", max_chars)
        items = self.recall(query, top_n=top_n, group_id=group_id, reinforce=True)
        if not items:
            return ""
        src_map = {"owner": "主人说的", "self": "你自己记的", "group": "群里聊的", "web": "查到的"}
        lines = ["【你想到的旧事】"]
        total = len(lines[0])
        for it in items:
            line = f"- {it['text']}（{src_map.get(it['source'], '群里聊的')}）"
            if total + len(line) + 1 > limit:
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else ""

    def consolidate(self) -> dict[str, int]:
        """
        夜间养护：
        0. 权重归一（把历史命中强化堆出来的超高权重压回等级上限）
        1. 同 level 同群「同一件事」的合并（相似度 ≥0.78，或短句整句被长句包含）
        2. 非 core 且有效权重 < 0.06 淘汰
        """
        with self._lock:
            self._maybe_reload_locked()
            now = time.time()
            nodes = self._graph["nodes"]
            merged_count = 0
            pruned_count = 0
            clamped_count = 0

            # 0. 权重归一：早期版本命中强化只加不减，普通节点被顶到 core 的量级
            for n in nodes:
                lvl = n.get("level", "normal")
                cap = round(BASE_WEIGHT.get(lvl, 1.0) * REINFORCE_CAP_MULT, 3)
                if float(n.get("weight", cap)) > cap:
                    n["weight"] = cap
                    clamped_count += 1

            # 1. 同 level 内是同一件事的就合并（高相似，或短句整句被长句包含）
            kept: list[dict[str, Any]] = []
            for node in sorted(nodes, key=lambda n: _effective_weight(n, now), reverse=True):
                found_dup = False
                for k in kept:
                    if k["level"] == node["level"] and k.get("group_id", "") == node.get("group_id", ""):
                        if _same_fact(k["text"], node["text"]):
                            k["hits"] = int(k.get("hits", 0)) + int(node.get("hits", 0))
                            k["tags"] = list(dict.fromkeys((k.get("tags") or []) + (node.get("tags") or [])))[:8]
                            merged_count += 1
                            found_dup = True
                            break
                if not found_dup:
                    kept.append(node)

            # 2. 淘汰非 core 且有效权重 < 0.06
            survivors: list[dict[str, Any]] = []
            for n in kept:
                if n.get("level") != "core" and _effective_weight(n, now) < PRUNE_THRESHOLD:
                    pruned_count += 1
                    continue
                survivors.append(n)

            kept_ids = {n["id"] for n in survivors}
            self._graph["nodes"] = survivors
            self._graph["edges"] = [e for e in self._graph["edges"] if e[0] in kept_ids and e[1] in kept_ids]
            self._save_graph_locked()
            logger.info(
                "🌙 L1 夜间巩固完成: 权重归一 %d 条, 合并 %d 条, 淘汰 %d 条, 剩余 %d 条",
                clamped_count,
                merged_count,
                pruned_count,
                len(survivors),
            )
            return {
                "clamped": clamped_count,
                "merged": merged_count,
                "pruned": pruned_count,
                "remaining": len(survivors),
            }

    # ========================
    # L2 日记与周结
    # ========================
    def count_writes_between(self, start_ts: float, end_ts: float) -> int:
        """统计某时间段内新写入的 L1 节点数（凌晨兜底用它判断「这一天有没有人写过」，
        兜底默认关，只有 MEMORY_NIGHTLY_FALLBACK=1 时才会用到）。"""
        cnt = 0
        with self._lock:
            for n in self._graph["nodes"]:
                ts = float(n.get("created", 0) or 0)
                if start_ts <= ts < end_ts:
                    cnt += 1
        return cnt

    def write_diary(
        self,
        mood: str,
        content: str,
        group_id: str = "",
        max_per_day: int = 3,
        dedupe_ratio: float = 0.7,
        day: str = "",
    ) -> bool:
        """
        写一条 L2 日记（≤300 字）。

        两道闸，都是为了别让她把同一天写成两遍：
        1. 每群每天最多 max_per_day 条；
        2. 和今天已有日记相似度 ≥ dedupe_ratio 就丢弃（实测两次触发会写出
           「那坨草稿糊屏幕上」和「最烦的是那破页面全是JS拼的」这种近重复）。

        day 用于补跑：凌晨 0~4 点写的是「昨天」的日记，条目日期得落回逻辑日，
        否则同一天会被写成两条（一条 23:45、一条 00:0x）。
        """
        clean = (content or "").strip()[:300]
        if not clean:
            return False
        now = datetime.now()
        today = day or now.strftime("%Y-%m-%d")
        stamp = f"{today} {now.strftime('%H:%M')}"
        with self._lock:
            same_day = [
                d
                for d in self._read_diaries_locked()
                if d.get("时间", "")[:10] == today and d.get("群", "") == group_id
            ]
            if len(same_day) >= max_per_day:
                logger.info("📔 L2 日记跳过：今天本群已有 %d 条（上限 %d）", len(same_day), max_per_day)
                return False
            for d in same_day:
                if SequenceMatcher(None, d.get("内容", ""), clean).ratio() >= dedupe_ratio:
                    logger.info("📔 L2 日记跳过：与今天已有日记高度重复 -> %s", clean[:40])
                    return False
            entry = {
                "时间": stamp,
                "心情": mood or "开心",
                "内容": clean,
                "群": group_id,
            }
            with self.diary_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("📔 L2 日记已写入[%s]: [%s] %s", group_id or "全局", mood, clean[:60])
        return True

    def _read_diaries_locked(self) -> list[dict[str, str]]:
        if not self.diary_path.is_file():
            return []
        out: list[dict[str, str]] = []
        for line in self.diary_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def read_recent_diaries(
        self,
        limit: int = 5,
        group_id: str | None = None,
        days: int | None = None,
    ) -> list[dict[str, str]]:
        """读最近日记。group_id 给定时只返回本群 + 早期无群标记的旧条目；days 限定最近几天。"""
        with self._lock:
            items = self._read_diaries_locked()
        if group_id is not None:
            items = [d for d in items if not d.get("群") or d.get("群") == group_id]
        if days is not None:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            items = [d for d in items if d.get("时间", "")[:10] >= cutoff]
        return items[-limit:]

    def render_diary_context(self, group_id: str = "", days: int = 3, max_chars: int = 120) -> str:
        """
        把最近 1~2 条日记拼成一小块「心情底色」注入系统提示词。

        日记以前只被 /日记 命令读到，写完就躺在磁盘里从不进 prompt，等于白写。
        只取本群近几天、最多 2 条、每条截 52 字、合计 ≤120 字，并明确告诉她别复述。
        （system 段命中缓存，实测这点开销基本为 0；关掉用 MEMORY_DIARY_INJECT=0）
        2026-11-26：条数/天数/字数/开关全部下沉到 data/tuning.json，夜间「记忆与人格」
        任务可以热调注入方式，不用改代码、不用重启。
        """
        if not tuning.get_int("DIARY_INJECT_ON", 1):
            return ""
        days = tuning.get_int("DIARY_DAYS", days)
        max_items = tuning.get_int("DIARY_MAX_ITEMS", 2)
        item_chars = tuning.get_int("DIARY_ITEM_CHARS", 52)
        limit = tuning.get_int("DIARY_MAX_CHARS", max_chars)
        diaries = self.read_recent_diaries(limit=max_items, group_id=group_id, days=days)
        if not diaries:
            return ""
        lines = ["【你前两天写的日记（只作心情底色，别主动复述细节）】"]
        total = len(lines[0])
        for d in diaries:
            line = f"- {d.get('时间', '')[5:10]} {d.get('心情', '平常')}：{d.get('内容', '')[:item_chars]}"
            if total + len(line) + 1 > limit:
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else ""

    def generate_weekly_summary(self, group_id: str = "") -> Path | None:
        """把最近 7 天日记压缩为周结 Markdown，并写入一条 important 记忆进 L1"""
        diaries = self.read_recent_diaries(limit=50, group_id=group_id or None)
        if not diaries:
            return None
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        week_items = [d for d in diaries if d.get("时间", "")[:10] >= cutoff]
        if not week_items:
            return None

        start_d = week_items[0]["时间"][:10]
        end_d = datetime.now().strftime("%Y-%m-%d")
        lines = [f"# 周结 {start_d} ~ {end_d}", f"共 {len(week_items)} 条日记。", ""]
        brief_parts = []
        for d in week_items:
            mmdd = d["时间"][5:10]
            lines.append(f"- {mmdd} · {d.get('心情', '平常')}｜{d.get('内容', '')}")
            brief_parts.append(f"{mmdd}{d.get('内容', '')[:25]}")

        suffix = f"_{group_id}" if group_id else ""
        out_path = DIARY_DIR / f"周结_{end_d}{suffix}.md"
        tmp = out_path.with_suffix(".md.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(out_path)

        # 同步写一条 important 周结进 L1
        summary_text = f"周结({start_d}~{end_d})：" + "；".join(brief_parts)[:170]
        self.remember(summary_text, level="important", tags=["周结", "日记"], source="self", group_id=group_id)
        return out_path

    def backup_memory(self, keep_last: int = 7) -> Path:
        """打包 roles/ + state/ + data/日记/ 到 data/backups/，保留最近 7 份"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        zip_path = BACKUP_DIR / f"dafeiyu_memory_{stamp}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for folder in (BASE_DIR / "roles", STATE_DIR, DIARY_DIR):
                if folder.is_dir():
                    for p in folder.rglob("*"):
                        if p.is_file():
                            zf.write(p, arcname=str(p.relative_to(BASE_DIR)))
        backups = sorted(BACKUP_DIR.glob("dafeiyu_memory_*.zip"), key=lambda p: p.stat().st_mtime)
        if len(backups) > keep_last:
            for old in backups[:-keep_last]:
                old.unlink(missing_ok=True)
        logger.info("💾 记忆备份完成: %s", zip_path.name)
        return zip_path

    def stats(self, group_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._maybe_reload_locked()
            nodes = self._graph["nodes"]
            dist = {"core": 0, "important": 0, "normal": 0}
            for n in nodes:
                lvl = n.get("level", "normal")
                dist[lvl] = dist.get(lvl, 0) + 1
            # 注意：这里已经在 self._lock 里，必须用不加锁的内部读法，否则会自锁死
            diaries = self._read_diaries_locked()
            return {
                "total_nodes": len(nodes),
                "total_edges": len(self._graph["edges"]),
                "levels": dist,
                "diaries": len(diaries),
                "group_diaries": len(
                    [d for d in diaries if not group_id or not d.get("群") or d.get("群") == group_id]
                )
                if group_id
                else len(diaries),
                "today_self_writes": self._count_today_self_writes_locked(time.time()),
                "daily_limit": DAILY_SELF_LIMIT,
            }

    # ------------------------------------------------------------------
    # 维护接口（给 tools/nightly_memory.py 的 list / prune / forget 用）
    # 以前识海里堆积的同义节点、违反「不记自己是什么」的元信息节点只能人工开 JSON 删，
    # 现在统一走这里：先 dry-run 出清单，确认后再 apply，删前自动备份。
    # ------------------------------------------------------------------
    def list_nodes(self, group_id: str | None = None) -> list[dict[str, Any]]:
        """节点快照（只读），按权重从高到低。"""
        with self._lock:
            self._maybe_reload_locked()
            rows = [
                {
                    "id": n.get("id", ""),
                    "text": n.get("text", ""),
                    "level": n.get("level", "normal"),
                    "tags": list(n.get("tags") or []),
                    "group_id": n.get("group_id", ""),
                    "hits": int(n.get("hits", 0)),
                    "weight": float(n.get("weight", 1.0)),
                    "created": float(n.get("created", 0)),
                }
                for n in self._graph["nodes"]
                if not group_id or not n.get("group_id") or n.get("group_id") == group_id
            ]
        rows.sort(key=lambda r: (r["weight"], r["hits"]), reverse=True)
        return rows

    def find_duplicate_groups(
        self, group_id: str | None = None, *, by_subject: bool = False
    ) -> list[list[dict[str, Any]]]:
        """找出本该合并的节点组，每组第一个是建议保留的那条（权重/命中更高）。

        by_subject=True 时再认「同一个主语」：识海里那几条「我对萌王一心一意是…」
        句子各不相同、文本相似度只有 0.39，_same_fact 抓不到，但开头是同一个主语。
        """
        with self._lock:
            self._maybe_reload_locked()
            snapshot = [
                dict(n) for n in self._graph["nodes"]
                if not group_id or not n.get("group_id") or n.get("group_id") == group_id
            ]
        snapshot.sort(key=lambda n: (float(n.get("weight", 1.0)), int(n.get("hits", 0))), reverse=True)

        groups: list[list[dict[str, Any]]] = []
        for node in snapshot:
            for g in groups:
                head = g[0]
                if head.get("level") != node.get("level"):
                    continue
                if head.get("group_id", "") != node.get("group_id", ""):
                    continue
                same = _same_fact(head.get("text", ""), node.get("text", ""))
                if not same and by_subject:
                    # 只认「同一个主语 + 用词也重合」：光同主语会把「ㅤㅤㅤ爱装机」和
                    # 「ㅤㅤㅤ想用 scrcpy」并成一条，那是两件事，不是同义节点。
                    same = (
                        _subject_key(head.get("text", "")) == _subject_key(node.get("text", ""))
                        and _token_jaccard(head.get("text", ""), node.get("text", "")) >= 0.18
                    )
                if same:
                    g.append(node)
                    break
            else:
                groups.append([node])
        return [g for g in groups if len(g) > 1]

    def merge_nodes(self, keep_id: str, drop_ids: list[str]) -> int:
        """把 drop_ids 并进 keep_id（命中数相加、标签取并集），返回删掉几条。

        不做备份由调用方负责——整理脚本会先调用 backup_memory()。
        """
        drop = {i for i in drop_ids if i and i != keep_id}
        if not drop:
            return 0
        with self._lock:
            keep = next((n for n in self._graph["nodes"] if n.get("id") == keep_id), None)
            if keep is None:
                return 0
            removed = [n for n in self._graph["nodes"] if n.get("id") in drop]
            keep["hits"] = int(keep.get("hits", 0)) + sum(int(n.get("hits", 0)) for n in removed)
            keep["weight"] = max(
                [float(keep.get("weight", 1.0))] + [float(n.get("weight", 1.0)) for n in removed]
            )
            keep["tags"] = list(
                dict.fromkeys((keep.get("tags") or []) + [t for n in removed for t in (n.get("tags") or [])])
            )[:8]
            keep["updated"] = time.time()
            self._graph["nodes"] = [n for n in self._graph["nodes"] if n.get("id") not in drop]
            kept_ids = {n.get("id") for n in self._graph["nodes"]}
            self._graph["edges"] = [
                e for e in self._graph["edges"] if e[0] in kept_ids and e[1] in kept_ids
            ]
            self._save_graph_locked()
            return len(removed)

    def forget(self, ids: list[str]) -> int:
        """按 id 删节点（连带清边），返回删掉的条数。备份由调用方负责。"""
        drop = {i for i in ids if i}
        if not drop:
            return 0
        with self._lock:
            before = len(self._graph["nodes"])
            self._graph["nodes"] = [n for n in self._graph["nodes"] if n.get("id") not in drop]
            kept_ids = {n.get("id") for n in self._graph["nodes"]}
            self._graph["edges"] = [
                e for e in self._graph["edges"] if e[0] in kept_ids and e[1] in kept_ids
            ]
            removed = before - len(self._graph["nodes"])
            if removed:
                self._save_graph_locked()
            return removed


memory_store = FourLayerMemory()
