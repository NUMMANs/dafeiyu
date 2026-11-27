"""L3 群聊会话与图片历史 SQLite 存储引擎（无电子宠物养成数值，纯轻量存储）。"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DATA_DIR

DB_PATH = DATA_DIR / "dafeiyu.db"

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.I | re.S)
_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>", re.I)
_THINK_ANY_RE = re.compile(r"</?(?:think|answer)\b[^>]*>", re.I)


def scrub_think(text: str) -> str:
    """清洗写进聊天历史的图片摘要，剥除视觉模型可能混入的 `<think>` 思考草稿。"""
    s = _THINK_BLOCK_RE.sub("", text or "")
    parts = _THINK_OPEN_RE.split(s)
    if len(parts) > 1:
        s = parts[0]
    if re.search(r"</think\s*>", s, flags=re.I):
        s = re.split(r"</think\s*>", s, flags=re.I)[-1]
    s = _THINK_ANY_RE.sub("", s)
    return " ".join(s.split()).strip()


@dataclass
class PetStatus:
    """轻量运行统计快照（保留类名以兼容 Web 控制台与历史接口）。"""

    total_messages: int = 0
    total_images: int = 0
    total_groups: int = 0
    mood_tag: str = "smug"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "grok",
            "title": "Grok 锐评核查官",
            "total_messages": self.total_messages,
            "total_images": self.total_images,
            "total_groups": self.total_groups,
            "mood_tag": self.mood_tag,
        }


class ChatHistoryEngine:
    """群聊 L3 会话历史与近期图片索引（SQLite 持久化）。"""

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
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_group_id ON chat_history (group_id, id)"
            )
            self._conn.commit()

    def get_status(self) -> PetStatus:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total_msgs,
                    SUM(CASE WHEN image_path IS NOT NULL AND image_path != '' THEN 1 ELSE 0 END) AS total_imgs,
                    COUNT(DISTINCT group_id) AS total_grps
                FROM chat_history
                """
            ).fetchone()
        return PetStatus(
            total_messages=int(row["total_msgs"] or 0) if row else 0,
            total_images=int(row["total_imgs"] or 0) if row else 0,
            total_groups=int(row["total_grps"] or 0) if row else 0,
            mood_tag="smug",
        )

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

    def get_history_between(
        self,
        group_id: str,
        start_ts: float,
        end_ts: float,
        limit: int = 3000,
    ) -> list[dict[str, Any]]:
        """取某时间段内的完整群聊记录（按时间正序），供夜间记忆沉淀读取全天素材。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT sender, role, content, image_path, sticker_url, created_ts
                FROM chat_history
                WHERE group_id = ? AND created_ts >= ? AND created_ts < ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (group_id, start_ts, end_ts, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_groups_with_history(self) -> list[dict[str, Any]]:
        """列出库里有聊天记录的群（群号 / 条数 / 最近一条时间）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT group_id, COUNT(*) AS cnt, MAX(created_ts) AS last_ts
                FROM chat_history
                WHERE group_id != ''
                GROUP BY group_id
                ORDER BY cnt DESC
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            last_ts = float(r["last_ts"] or 0)
            out.append(
                {
                    "group_id": r["group_id"],
                    "cnt": int(r["cnt"]),
                    "last": datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M")
                    if last_ts
                    else "",
                }
            )
        return out

    def count_history_between(self, start_ts: float, end_ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM chat_history WHERE created_ts >= ? AND created_ts < ?",
                (start_ts, end_ts),
            ).fetchone()
            return int(row[0]) if row else 0

    def list_history(
        self,
        group_id: str,
        limit: int | None = None,
        offset: int = 0,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        order = "DESC" if newest_first else "ASC"
        sql = (
            "SELECT id, sender, role, content, image_path, created_ts "
            f"FROM chat_history WHERE group_id = ? ORDER BY id {order}"
        )
        args: tuple = (group_id,)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = (group_id, limit, offset)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def delete_history_ids(self, ids: list[int]) -> int:
        if not ids:
            return 0
        deleted = 0
        with self._lock:
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                marks = ",".join("?" * len(chunk))
                cur = self._conn.execute(f"DELETE FROM chat_history WHERE id IN ({marks})", chunk)
                deleted += cur.rowcount or 0
            self._conn.commit()
        return deleted

    def update_history_content_by_image(self, group_id: str, image_path: str, caption: str) -> None:
        clean_cap = scrub_think(caption)[:220]
        clean_path = (image_path or "").strip()
        if not clean_path:
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
            if not clean_cap:
                if cur_content:
                    return
                new_content = "[发了一张图片]"
            elif not cur_content:
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


pet_engine = ChatHistoryEngine()
