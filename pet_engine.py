import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import DATA_DIR

DB_PATH = DATA_DIR / "dafeiyu.db"

LEVEL_TITLES = [
    (0, "Lv.1 小鱼苗"),
    (120, "Lv.2 摸鱼学徒"),
    (350, "Lv.3 白饭干饭王"),
    (800, "Lv.4 群聊大肥鱼"),
    (1600, "Lv.5 赛博深海巨鲸"),
    (3200, "Lv.6 吞噬Token之神"),
]


def calc_level(exp: int) -> tuple[int, str, int | None]:
    """返回 (等级数字, 称号, 下一级所需经验或None)"""
    level_idx = 0
    for idx, (threshold, _) in enumerate(LEVEL_TITLES):
        if exp >= threshold:
            level_idx = idx
    next_threshold = LEVEL_TITLES[level_idx + 1][0] if level_idx + 1 < len(LEVEL_TITLES) else None
    return level_idx + 1, LEVEL_TITLES[level_idx][1], next_threshold


def affinity_title(score: int) -> str:
    if score >= 85:
        return "挚爱饲主"
    if score >= 55:
        return "白饭金主"
    if score >= 25:
        return "熟脸铲屎官"
    return "路过群友"


def progress_bar(value: int, total: int = 100, width: int = 10) -> str:
    filled = max(0, min(width, round(value / total * width)))
    return "█" * filled + "░" * (width - filled)


@dataclass
class PetStatus:
    hunger: int
    mood: int
    exp: int
    level: int
    title: str
    next_exp: int | None
    total_tokens: int
    total_images: int
    total_messages: int
    mood_tag: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hunger": self.hunger,
            "mood": self.mood,
            "exp": self.exp,
            "level": self.level,
            "title": self.title,
            "next_exp": self.next_exp,
            "total_tokens": self.total_tokens,
            "total_images": self.total_images,
            "total_messages": self.total_messages,
            "mood_tag": self.mood_tag,
            "is_hungry": self.hunger <= 25,
        }


class PetEngine:
    """大肥鱼养成数值与群友记忆引擎（SQLite 持久化）"""

    def __init__(self, db_path: Path = DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pet_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    hunger INTEGER NOT NULL DEFAULT 75,
                    mood INTEGER NOT NULL DEFAULT 80,
                    exp INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    total_images INTEGER NOT NULL DEFAULT 0,
                    total_messages INTEGER NOT NULL DEFAULT 0,
                    last_decay_ts REAL NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_affinity (
                    group_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    affinity INTEGER NOT NULL DEFAULT 10,
                    feed_count INTEGER NOT NULL DEFAULT 0,
                    msg_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_ts REAL NOT NULL,
                    PRIMARY KEY (group_id, user_name)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    image_path TEXT,
                    sticker_url TEXT,
                    created_ts REAL NOT NULL
                )
                """
            )
            cur.execute("SELECT id FROM pet_state WHERE id = 1")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO pet_state (id, hunger, mood, exp, total_tokens, total_images, total_messages, last_decay_ts) VALUES (1, 75, 80, 0, 0, 0, 0, ?)",
                    (time.time(),),
                )
            self._conn.commit()

    def _apply_natural_decay_locked(self) -> None:
        """随时间自然消耗饱食度与心情（每 30 分钟掉 2 点饱食度、1 点心情）"""
        cur = self._conn.cursor()
        row = cur.execute("SELECT * FROM pet_state WHERE id = 1").fetchone()
        now = time.time()
        elapsed_min = (now - row["last_decay_ts"]) / 60.0
        steps = int(elapsed_min // 30)
        if steps > 0:
            new_hunger = max(5, row["hunger"] - steps * 2)
            new_mood = max(15, row["mood"] - steps * 1)
            cur.execute(
                "UPDATE pet_state SET hunger = ?, mood = ?, last_decay_ts = ? WHERE id = 1",
                (new_hunger, new_mood, now),
            )
            self._conn.commit()

    def _infer_mood_tag(self, hunger: int, mood: int) -> str:
        if hunger <= 25:
            return "hungry"
        if mood >= 75:
            return "happy"
        if mood <= 40:
            return "sleepy"
        return "happy"

    def get_status(self) -> PetStatus:
        with self._lock:
            self._apply_natural_decay_locked()
            row = self._conn.execute("SELECT * FROM pet_state WHERE id = 1").fetchone()
            level, title, next_exp = calc_level(row["exp"])
            return PetStatus(
                hunger=row["hunger"],
                mood=row["mood"],
                exp=row["exp"],
                level=level,
                title=title,
                next_exp=next_exp,
                total_tokens=row["total_tokens"],
                total_images=row["total_images"],
                total_messages=row["total_messages"],
                mood_tag=self._infer_mood_tag(row["hunger"], row["mood"]),
            )

    def get_user_affinity(self, group_id: str, user_name: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM user_affinity WHERE group_id = ? AND user_name = ?",
                (group_id, user_name),
            ).fetchone()
            if not row:
                return {
                    "user_name": user_name,
                    "affinity": 10,
                    "title": affinity_title(10),
                    "feed_count": 0,
                    "msg_count": 0,
                }
            return {
                "user_name": row["user_name"],
                "affinity": row["affinity"],
                "title": affinity_title(row["affinity"]),
                "feed_count": row["feed_count"],
                "msg_count": row["msg_count"],
            }

    def record_interaction(
        self,
        group_id: str,
        user_name: str,
        tokens_used: int = 0,
        had_image: bool = False,
        hunger_delta: int = 4,
        mood_delta: int = 2,
        exp_delta: int = 8,
        affinity_delta: int = 2,
        is_feed: bool = False,
    ) -> PetStatus:
        with self._lock:
            self._apply_natural_decay_locked()
            row = self._conn.execute("SELECT * FROM pet_state WHERE id = 1").fetchone()

            # 吃掉的 Token 越多，额外多涨一点饱食度和经验（赛博吃白饭！）
            bonus_hunger = min(10, tokens_used // 250)
            bonus_exp = min(15, tokens_used // 150)

            new_hunger = max(0, min(100, row["hunger"] + hunger_delta + bonus_hunger))
            new_mood = max(0, min(100, row["mood"] + mood_delta))
            new_exp = row["exp"] + exp_delta + bonus_exp
            new_tokens = row["total_tokens"] + max(0, tokens_used)
            new_images = row["total_images"] + (1 if had_image else 0)
            new_msgs = row["total_messages"] + 1

            self._conn.execute(
                """
                UPDATE pet_state
                SET hunger = ?, mood = ?, exp = ?, total_tokens = ?, total_images = ?, total_messages = ?
                WHERE id = 1
                """,
                (new_hunger, new_mood, new_exp, new_tokens, new_images, new_msgs),
            )

            now = time.time()
            u_row = self._conn.execute(
                "SELECT * FROM user_affinity WHERE group_id = ? AND user_name = ?",
                (group_id, user_name),
            ).fetchone()
            if u_row is None:
                self._conn.execute(
                    """
                    INSERT INTO user_affinity (group_id, user_name, affinity, feed_count, msg_count, last_seen_ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        user_name,
                        max(0, min(100, 10 + affinity_delta)),
                        1 if is_feed else 0,
                        1,
                        now,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE user_affinity
                    SET affinity = ?, feed_count = ?, msg_count = ?, last_seen_ts = ?
                    WHERE group_id = ? AND user_name = ?
                    """,
                    (
                        max(0, min(100, u_row["affinity"] + affinity_delta)),
                        u_row["feed_count"] + (1 if is_feed else 0),
                        u_row["msg_count"] + 1,
                        now,
                        group_id,
                        user_name,
                    ),
                )

            self._conn.commit()
            level, title, next_exp = calc_level(new_exp)
            return PetStatus(
                hunger=new_hunger,
                mood=new_mood,
                exp=new_exp,
                level=level,
                title=title,
                next_exp=next_exp,
                total_tokens=new_tokens,
                total_images=new_images,
                total_messages=new_msgs,
                mood_tag=self._infer_mood_tag(new_hunger, new_mood),
            )

    def get_leaderboard(self, group_id: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            if group_id:
                rows = self._conn.execute(
                    """
                    SELECT user_name, affinity, feed_count, msg_count
                    FROM user_affinity
                    WHERE group_id = ?
                    ORDER BY affinity DESC, feed_count DESC
                    LIMIT ?
                    """,
                    (group_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT user_name, MAX(affinity) as affinity, SUM(feed_count) as feed_count, SUM(msg_count) as msg_count
                    FROM user_affinity
                    GROUP BY user_name
                    ORDER BY affinity DESC, feed_count DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [
                {
                    "user_name": r["user_name"],
                    "affinity": r["affinity"],
                    "title": affinity_title(r["affinity"]),
                    "feed_count": r["feed_count"],
                    "msg_count": r["msg_count"],
                }
                for r in rows
            ]

    def append_history(
        self,
        group_id: str,
        sender: str,
        role: str,
        content: str,
        image_path: str | None = None,
        sticker_url: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO chat_history (group_id, sender, role, content, image_path, sticker_url, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (group_id, sender, role, content, image_path, sticker_url, time.time()),
            )
            self._conn.commit()

    def get_recent_history(self, group_id: str, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT sender, role, content, image_path, sticker_url, created_ts
                FROM chat_history
                WHERE group_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def update_history_content_by_image(self, group_id: str, image_path: str, caption: str) -> None:
        clean_cap = " ".join((caption or "").split()).strip()[:220]
        clean_path = (image_path or "").strip()
        if not clean_cap or not clean_path:
            return
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, content FROM chat_history
                WHERE group_id = ? AND image_path = ?
                ORDER BY id DESC LIMIT 1
                """,
                (group_id, clean_path),
            ).fetchone()
            if not row:
                return
            cur_content = (row["content"] or "").strip()
            if not cur_content:
                new_content = f"[发了一张图片：{clean_cap}]"
            elif "[图片内容：" not in cur_content and "[发了一张图片：" not in cur_content:
                new_content = f"{cur_content}\n[图片内容：{clean_cap}]"
            else:
                return
            self._conn.execute(
                "UPDATE chat_history SET content = ? WHERE id = ?",
                (new_content, row["id"]),
            )
            self._conn.commit()

    def get_recent_images(self, group_id: str, limit: int = 8) -> list[dict[str, Any]]:
        max_limit = max(1, limit)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT sender, content, image_path, created_ts
                FROM chat_history
                WHERE group_id = ? AND role = 'user' AND image_path IS NOT NULL AND image_path != ''
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_id, max(max_limit * 3, 24)),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            img_str = str(r["image_path"] or "").strip()
            if not img_str:
                continue
            p = Path(img_str)
            if not p.is_file() or p.stat().st_size <= 0:
                continue
            results.append(
                {
                    "index": len(results) + 1,
                    "sender": str(r["sender"] or "群友"),
                    "image_path": img_str,
                    "content": str(r["content"] or ""),
                    "created_ts": float(r["created_ts"] or 0.0),
                }
            )
            if len(results) >= max_limit:
                break
        return results


pet_engine = PetEngine()
