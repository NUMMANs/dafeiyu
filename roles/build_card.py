"""
L0 多人设角色卡合并与零重启热切换模块：
将只读模板 roles/personas/<persona>.人格.md（或 roles/大肥鱼.人格.md）+ roles/大肥鱼.操作.md
与运行期进化记录 data/persona_evolutions_<persona>.json 原子合并输出至 data/compiled_card_<persona>.md，
绝不修改受 Git 跟踪的模板文件。
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DATA_DIR, settings

ROLES_DIR = Path(__file__).resolve().parent
PERSONA_PATH = ROLES_DIR / "大肥鱼.人格.md"
OPS_PATH = ROLES_DIR / "大肥鱼.操作.md"
OUT_PATH = DATA_DIR / "大肥鱼.compiled.md"
EVOLUTIONS_PATH = DATA_DIR / "persona_evolutions.json"
ACTIVE_PERSONA_PATH = DATA_DIR / "active_persona.json"

CHAR_LIMIT = 6000
EVOLVE_HEADER = "## 我的自我进化记录"
MAX_EVOLVE_ITEMS = 15
DAILY_EVOLVE_LIMIT = 2

PERSONA_REGISTRY: dict[str, dict[str, Any]] = {
    "fengge": {
        "label": "峰哥亡命天涯",
        "path": ROLES_DIR / "personas" / "fengge.人格.md",
        "aliases": ("fengge", "峰哥", "亡命天涯", "周丽峰", "老峰", "峰哥亡命天涯"),
    },
    "dafeiyu": {
        "label": "塔菲大肥鱼",
        "path": ROLES_DIR / "大肥鱼.人格.md",
        "aliases": ("dafeiyu", "taffy", "塔菲", "大肥鱼", "默认", "塔菲大肥鱼"),
    },
}


def _strip_quotes(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">")).strip()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _normalize_persona_key(name: str) -> str | None:
    norm = (name or "").strip().lower()
    if not norm:
        return None
    for key, meta in PERSONA_REGISTRY.items():
        if norm == key or norm in [a.lower() for a in meta["aliases"]]:
            return key
    return None


def get_active_persona() -> str:
    if ACTIVE_PERSONA_PATH.is_file():
        try:
            raw = json.loads(ACTIVE_PERSONA_PATH.read_text(encoding="utf-8"))
            key = _normalize_persona_key(str(raw.get("active") or ""))
            if key and PERSONA_REGISTRY[key]["path"].is_file():
                return key
        except Exception:
            pass
    default_key = _normalize_persona_key(settings.DEFAULT_PERSONA)
    if default_key and PERSONA_REGISTRY[default_key]["path"].is_file():
        return default_key
    return "dafeiyu"


def set_active_persona(name: str) -> tuple[bool, str]:
    key = _normalize_persona_key(name)
    if not key:
        names = "、".join(f"{m['label']}({k})" for k, m in PERSONA_REGISTRY.items())
        return False, f"未找到人设「{name}」，可选：{names}"
    if not PERSONA_REGISTRY[key]["path"].is_file():
        return False, f"人设模板文件不存在：{PERSONA_REGISTRY[key]['path'].name}"
    _atomic_write_text(
        ACTIVE_PERSONA_PATH,
        json.dumps({"active": key, "updated_at": datetime.now().isoformat()}, ensure_ascii=False, indent=2),
    )
    build_card(key)
    label = PERSONA_REGISTRY[key]["label"]
    return True, f"已热切换人设为【{label}】(`{key}`)，立即生效无需重启"


def list_personas() -> list[dict[str, Any]]:
    active = get_active_persona()
    return [
        {
            "key": key,
            "label": meta["label"],
            "active": key == active,
            "available": meta["path"].is_file(),
        }
        for key, meta in PERSONA_REGISTRY.items()
    ]


def _persona_file(persona: str) -> Path:
    meta = PERSONA_REGISTRY.get(persona) or PERSONA_REGISTRY["dafeiyu"]
    return Path(meta["path"])


def _evolutions_file(persona: str) -> Path:
    return DATA_DIR / f"persona_evolutions_{persona}.json"


def _compiled_file(persona: str) -> Path:
    return DATA_DIR / f"compiled_card_{persona}.md"


def _load_template_evolutions(persona: str) -> list[str]:
    p_path = _persona_file(persona)
    if not p_path.is_file():
        return []
    lines = p_path.read_text(encoding="utf-8").splitlines()
    header_idx = next((i for i, l in enumerate(lines) if l.startswith(EVOLVE_HEADER)), None)
    if header_idx is None:
        return []
    return [
        l.strip()[2:].strip()
        for l in lines[header_idx + 1 :]
        if l.strip().startswith("- ") and "刚搬进这个群" not in l and "刚进群" not in l
    ][-MAX_EVOLVE_ITEMS:]


def _load_evolutions_list(persona: str | None = None) -> list[str]:
    key = persona or get_active_persona()
    evo_path = _evolutions_file(key)
    if not evo_path.is_file() and key == "dafeiyu" and EVOLUTIONS_PATH.is_file():
        evo_path = EVOLUTIONS_PATH
    if evo_path.is_file():
        try:
            raw = json.loads(evo_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [str(item).strip() for item in raw if str(item).strip()][-MAX_EVOLVE_ITEMS:]
        except Exception:
            pass
    return _load_template_evolutions(key)


def _save_evolutions_list(items: list[str], persona: str | None = None) -> None:
    key = persona or get_active_persona()
    payload = [str(item).strip() for item in items if str(item).strip()][-MAX_EVOLVE_ITEMS:]
    _atomic_write_text(_evolutions_file(key), json.dumps(payload, ensure_ascii=False, indent=2))


def _render_persona_with_evolutions(persona: str) -> str:
    p_path = _persona_file(persona)
    raw_persona = _strip_quotes(p_path.read_text(encoding="utf-8")) if p_path.is_file() else ""
    lines = raw_persona.splitlines()
    header_idx = next((i for i, l in enumerate(lines) if l.startswith(EVOLVE_HEADER)), None)
    if header_idx is not None:
        base_lines = lines[:header_idx]
        header_line = lines[header_idx]
    else:
        base_lines = lines
        header_line = f"{EVOLVE_HEADER}（随群友相处自然沉淀）"

    items = _load_evolutions_list(persona)
    evolve_lines = [f"- {item}" for item in items] if items else ["- （刚进群，先跟大伙混个脸熟）"]
    base_text = "\n".join(base_lines).rstrip()
    evolve_block = "\n".join([header_line, *evolve_lines])
    return f"{base_text}\n\n{evolve_block}".strip()


def build_card(persona: str | None = None) -> str:
    key = persona or get_active_persona()
    label = (PERSONA_REGISTRY.get(key) or PERSONA_REGISTRY["dafeiyu"])["label"]
    persona_text = _render_persona_with_evolutions(key)
    ops = _strip_quotes(OPS_PATH.read_text(encoding="utf-8")) if OPS_PATH.is_file() else ""
    merged = f"# 角色卡：{label}\n\n{persona_text}\n\n---\n\n{ops}\n"
    if len(merged) > CHAR_LIMIT:
        raise ValueError(f"合并后角色卡共 {len(merged)} 字，超过上限 {CHAR_LIMIT}，请先精简")

    _atomic_write_text(_compiled_file(key), merged)
    return merged


def load_card(persona: str | None = None) -> str:
    key = persona or get_active_persona()
    out_path = _compiled_file(key)
    p_path = _persona_file(key)
    evo_path = _evolutions_file(key)

    need_rebuild = not out_path.is_file()
    if not need_rebuild:
        out_mtime = out_path.stat().st_mtime
        for src in (p_path, OPS_PATH, evo_path):
            if src.is_file() and src.stat().st_mtime > out_mtime:
                need_rebuild = True
                break
    if need_rebuild:
        return build_card(key)
    return out_path.read_text(encoding="utf-8")


def evolve_persona(note: str, persona: str | None = None) -> tuple[bool, str]:
    """
    允许当前人设在安全边界内「自我升级/微调」：
    - 进化记录持久化写入 data/persona_evolutions_<persona>.json，绝不改写受 Git 跟踪的模板文件
    - 每天上限 2 条，最多保留最近 15 条，并自动重建 data/compiled_card_<persona>.md
    """
    key = persona or get_active_persona()
    clean = (note or "").strip().replace("\n", " ")[:120]
    if len(clean) < 4:
        return False, "进化内容太短了"

    today = datetime.now().strftime("%Y-%m-%d")
    items = _load_evolutions_list(key)
    today_count = sum(1 for item in items if item.startswith(f"[{today}]"))
    if today_count >= DAILY_EVOLVE_LIMIT:
        return False, f"今天已经自我进化 {DAILY_EVOLVE_LIMIT} 次了，明天再继续"

    items.append(f"[{today}] {clean}")
    items = items[-MAX_EVOLVE_ITEMS:]
    _save_evolutions_list(items, persona=key)

    build_card(key)
    return True, f"自我进化成功：{clean}"


def get_evolutions(persona: str | None = None) -> list[str]:
    return _load_evolutions_list(persona)


if __name__ == "__main__":
    active_key = get_active_persona()
    content = build_card(active_key)
    print(f"已生成 {_compiled_file(active_key)}（人设={active_key}）：{len(content)} 字")
