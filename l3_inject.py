"""L3 注入通道：工具 LLM 唯一被允许「往大肥鱼脑子里塞东西」的入口。

工具在沙箱里写 l3_inject.json，runner 读出来落进 data/l3_inject.json，
大肥鱼每轮说话时由 brain 读一遍拼进动态上下文。三处设计上的取舍：

1. 不进 chat_history。L3 会话层是整层注入的，一条旁注会被每一轮重复带着走，
   而且会永久留在历史里；这里用带过期时间的独立文件，到点自动消失。
2. 写入受控。工具只能写沙箱里的约定文件，真正落盘由 runner 做校验（长度、条数），
   它没有直接改记忆库或人格文件的路。
3. 全局条数封顶。后台任务再多也不会把上下文顶满，超了只留最新的。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from config import DATA_DIR

DATA_FILE = DATA_DIR / "l3_inject.json"
MAX_ENTRIES = 40
NOTE_MAX_CHARS = 200


def _read() -> dict[str, Any]:
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_name(f".{DATA_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def clean_note(raw: Any) -> str:
    """一句话事实：压掉换行和多余空白，超长截断。"""
    if not isinstance(raw, str):
        return ""
    text = " ".join(raw.replace("\u3000", " ").split())
    if not text:
        return ""
    return text[:NOTE_MAX_CHARS]


def record_notes(
    group_id: str,
    notes: list[str],
    job_id: str = "",
    ttl_hours: float = 6.0,
    max_notes: int = 5,
) -> int:
    """把一次任务提交的注入内容落盘，返回实际写入条数。"""
    if max_notes <= 0 or not notes:
        return 0
    cleaned: list[str] = []
    for raw in notes:
        text = clean_note(raw)
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= max_notes:
            break
    if not cleaned:
        return 0

    now = time.time()
    entries = [
        e for e in _read().get("entries", [])
        if isinstance(e, dict) and float(e.get("expire_ts") or 0) > now
    ]
    for text in cleaned:
        entries.append(
            {
                "group_id": (group_id or "").strip(),
                "text": text,
                "job_id": job_id,
                "created_ts": now,
                "expire_ts": now + max(0.5, float(ttl_hours)) * 3600,
            }
        )
    _write({"entries": entries[-MAX_ENTRIES:]})
    return len(cleaned)


def load_notes(group_id: str, limit: int = 5) -> list[str]:
    """取这个群还没过期的注入内容，按写入顺序返回。"""
    if limit <= 0:
        return []
    now = time.time()
    out: list[str] = []
    for entry in _read().get("entries", []):
        if not isinstance(entry, dict):
            continue
        if (entry.get("group_id") or "") != (group_id or ""):
            continue
        if float(entry.get("expire_ts") or 0) <= now:
            continue
        text = clean_note(entry.get("text"))
        if text:
            out.append(text)
    return out[-limit:]


def read_from_sandbox(sandbox: Path, max_notes: int = 5) -> list[str]:
    """从沙箱读工具写的 l3_inject.json（宽松解析：数组/对象/单字符串都收）。"""
    path = Path(sandbox) / "l3_inject.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    raw_items: list[Any]
    if isinstance(data, dict):
        raw_items = data.get("notes") or data.get("items") or []
        if isinstance(raw_items, str):
            raw_items = [raw_items]
    elif isinstance(data, list):
        raw_items = data
    else:
        return []
    out: list[str] = []
    for raw in raw_items:
        text = clean_note(raw)
        if text and text not in out:
            out.append(text)
        if len(out) >= max(0, max_notes):
            break
    return out
