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


def _tokenize(text: str) -> set[str]:
    """中文 2 字滑窗 + 单字 + 英文单词，并过滤虚词"""
    t = (text or "").lower()
    tokens: set[str] = set()

    # 英文/数字词（如 lol, kda, python, 0-8）
    for w in re.findall(r"[a-z0-9_-]{2,}", t):
        if w not in STOPWORDS:
            tokens.add(w)

    # 中文连续段：提取 2 字滑窗 + 单字
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        for ch in seg:
            if ch not in STOPWORDS:
                tokens.add(ch)
        for i in range(len(seg) - 1):
            bg = seg[i : i + 2]
            if bg not in STOPWORDS:
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
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        cnt = 0
        for n in self._graph["nodes"]:
            if n.get("source") == "self":
                ts = float(n.get("created", 0))
                if ts > 0 and datetime.fromtimestamp(ts).strftime("%Y-%m-%d") == today:
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
    ) -> tuple[bool, str]:
        """写入一条记忆节点到 L1 识海（带隐私过滤、权限检查、每日 8 条上限、去重）"""
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
            now = time.time()
            if source == "self" and self._count_today_self_writes_locked(now) >= DAILY_SELF_LIMIT:
                return False, f"今天主动记忆已达上限（{DAILY_SELF_LIMIT} 条），防膨胀拒绝写入"

            # 去重：如果已有高度相似节点（>=0.82），直接强化旧节点而不是重复写
            for existing in self._graph["nodes"]:
                if existing.get("group_id", "") == group_id:
                    sim = SequenceMatcher(None, existing["text"], clean_text).ratio()
                    if sim >= 0.82:
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
        分词（过滤虚词）-> 字面命中分 -> 沿标签边 2 跳图扩散 (×0.6) -> 有效权重加权 -> 低于 0.25 丢弃 -> 命中强化
        """
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        with self._lock:
            now = time.time()
            nodes = self._graph["nodes"]
            if not nodes:
                return []

            # 群聊隔离：优先匹配当前群或全局（group_id == ""）的记忆
            valid_nodes: dict[str, dict[str, Any]] = {}
            for n in nodes:
                ng = n.get("group_id", "")
                if group_id and ng and ng != group_id:
                    continue
                valid_nodes[n["id"]] = n

            if not valid_nodes:
                return []

            denom = min(len(q_tokens), 4)
            literal_scores: dict[str, float] = {}
            for nid, n in valid_nodes.items():
                doc_tokens = _tokenize(n["text"] + " " + " ".join(n.get("tags") or []))
                hits = len(q_tokens & doc_tokens)
                if hits > 0:
                    literal_scores[nid] = min(1.0, hits / denom)

            if not literal_scores:
                return []

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
                if final_score >= RECALL_SCORE_THRESHOLD:
                    scored.append((final_score, node))

            scored.sort(key=lambda x: x[0], reverse=True)
            top_items = scored[:top_n]

            if reinforce and top_items:
                for _, node in top_items:
                    node["hits"] = int(node.get("hits", 0)) + 1
                    node["weight"] = round(float(node.get("weight", 1.0)) + 0.08, 3)
                    node["updated"] = round(now, 2)
                self._save_graph_locked()

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
        """拼装本轮注入提示词（≤5 条、≤400 字；没有相关记忆时返回空串，绝不硬凑）"""
        items = self.recall(query, top_n=5, group_id=group_id, reinforce=True)
        if not items:
            return ""
        src_map = {"owner": "主人说的", "self": "你自己记的", "group": "群里聊的", "web": "查到的"}
        lines = ["【你想到的旧事】"]
        total = len(lines[0])
        for it in items:
            line = f"- {it['text']}（{src_map.get(it['source'], '群里聊的')}）"
            if total + len(line) + 1 > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else ""

    def consolidate(self) -> dict[str, int]:
        """夜间养护：同 level 文本相似度 >= 0.78 合并 + 非 core 低于淘汰线清除"""
        with self._lock:
            now = time.time()
            nodes = self._graph["nodes"]
            merged_count = 0
            pruned_count = 0

            # 1. 同 level 内相似度 >= 0.78 合并
            kept: list[dict[str, Any]] = []
            for node in sorted(nodes, key=lambda n: _effective_weight(n, now), reverse=True):
                found_dup = False
                for k in kept:
                    if k["level"] == node["level"] and k.get("group_id", "") == node.get("group_id", ""):
                        if SequenceMatcher(None, k["text"], node["text"]).ratio() >= 0.78:
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
            logger.info("🌙 L1 夜间巩固完成: 合并 %d 条, 淘汰 %d 条, 剩余 %d 条", merged_count, pruned_count, len(survivors))
            return {"merged": merged_count, "pruned": pruned_count, "remaining": len(survivors)}

    # ========================
    # L2 日记与周结
    # ========================
    def write_diary(self, mood: str, content: str) -> bool:
        clean = (content or "").strip()[:300]
        if not clean:
            return False
        entry = {
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "心情": mood or "开心",
            "内容": clean,
        }
        with self._lock:
            with self.diary_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("📔 L2 日记已写入: [%s] %s", mood, clean[:60])
        return True

    def read_recent_diaries(self, limit: int = 5) -> list[dict[str, str]]:
        if not self.diary_path.is_file():
            return []
        lines = [l.strip() for l in self.diary_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        out: list[dict[str, str]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def generate_weekly_summary(self) -> Path | None:
        """把最近 7 天日记压缩为周结 Markdown，并写入一条 important 记忆进 L1"""
        diaries = self.read_recent_diaries(limit=30)
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

        out_path = DIARY_DIR / f"周结_{end_d}.md"
        tmp = out_path.with_suffix(".md.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(out_path)

        # 同步写一条 important 周结进 L1
        summary_text = f"周结({start_d}~{end_d})：" + "；".join(brief_parts)[:170]
        self.remember(summary_text, level="important", tags=["周结", "日记"], source="self")
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

    def stats(self) -> dict[str, Any]:
        with self._lock:
            nodes = self._graph["nodes"]
            dist = {"core": 0, "important": 0, "normal": 0}
            for n in nodes:
                lvl = n.get("level", "normal")
                dist[lvl] = dist.get(lvl, 0) + 1
            return {
                "total_nodes": len(nodes),
                "total_edges": len(self._graph["edges"]),
                "levels": dist,
                "diaries": len(self.read_recent_diaries(limit=999)),
                "today_self_writes": self._count_today_self_writes_locked(time.time()),
                "daily_limit": DAILY_SELF_LIMIT,
            }


memory_store = FourLayerMemory()
