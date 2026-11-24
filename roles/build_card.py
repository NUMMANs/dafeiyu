"""
L0 角色卡合并脚本：
将只读模板 roles/大肥鱼.人格.md + roles/大肥鱼.操作.md 与运行期进化记录 data/persona_evolutions.json
原子合并输出至 data/大肥鱼.compiled.md（上限 6000 字），不修改受 Git 跟踪的模板文件。
"""
import json
import os
from datetime import datetime
from pathlib import Path

from config import DATA_DIR

ROLES_DIR = Path(__file__).resolve().parent
PERSONA_PATH = ROLES_DIR / "大肥鱼.人格.md"
OPS_PATH = ROLES_DIR / "大肥鱼.操作.md"
OUT_PATH = DATA_DIR / "大肥鱼.compiled.md"
EVOLUTIONS_PATH = DATA_DIR / "persona_evolutions.json"
CHAR_LIMIT = 6000
EVOLVE_HEADER = "## 我的自我进化记录"
MAX_EVOLVE_ITEMS = 15
DAILY_EVOLVE_LIMIT = 2


def _strip_quotes(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">")).strip()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _load_template_evolutions() -> list[str]:
    if not PERSONA_PATH.is_file():
        return []
    lines = PERSONA_PATH.read_text(encoding="utf-8").splitlines()
    header_idx = next((i for i, l in enumerate(lines) if l.startswith(EVOLVE_HEADER)), None)
    if header_idx is None:
        return []
    return [
        l.strip()[2:].strip()
        for l in lines[header_idx + 1 :]
        if l.strip().startswith("- ") and "刚搬进这个群" not in l
    ][-MAX_EVOLVE_ITEMS:]


def _load_evolutions_list() -> list[str]:
    if EVOLUTIONS_PATH.is_file():
        try:
            raw = json.loads(EVOLUTIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [str(item).strip() for item in raw if str(item).strip()][-MAX_EVOLVE_ITEMS:]
        except Exception:
            pass
    return _load_template_evolutions()


def _save_evolutions_list(items: list[str]) -> None:
    payload = [str(item).strip() for item in items if str(item).strip()][-MAX_EVOLVE_ITEMS:]
    _atomic_write_text(EVOLUTIONS_PATH, json.dumps(payload, ensure_ascii=False, indent=2))


def _render_persona_with_evolutions() -> str:
    raw_persona = _strip_quotes(PERSONA_PATH.read_text(encoding="utf-8")) if PERSONA_PATH.is_file() else ""
    lines = raw_persona.splitlines()
    header_idx = next((i for i, l in enumerate(lines) if l.startswith(EVOLVE_HEADER)), None)
    if header_idx is not None:
        base_lines = lines[:header_idx]
        header_line = lines[header_idx]
    else:
        base_lines = lines
        header_line = f"{EVOLVE_HEADER}（我会随着和群友相处自己长出新习惯和新性格）"

    items = _load_evolutions_list()
    evolve_lines = [f"- {item}" for item in items] if items else ["- （刚搬进这个群，还在慢慢熟悉大家的脾气喵）"]
    base_text = "\n".join(base_lines).rstrip()
    evolve_block = "\n".join([header_line, *evolve_lines])
    return f"{base_text}\n\n{evolve_block}".strip()


def build_card() -> str:
    persona = _render_persona_with_evolutions()
    ops = _strip_quotes(OPS_PATH.read_text(encoding="utf-8")) if OPS_PATH.is_file() else ""
    merged = f"# 角色卡：大肥鱼\n\n{persona}\n\n---\n\n{ops}\n"
    if len(merged) > CHAR_LIMIT:
        raise ValueError(f"合并后角色卡共 {len(merged)} 字，超过上限 {CHAR_LIMIT}，请先精简")

    _atomic_write_text(OUT_PATH, merged)
    return merged


def load_card() -> str:
    need_rebuild = not OUT_PATH.is_file()
    if not need_rebuild:
        out_mtime = OUT_PATH.stat().st_mtime
        for src in (PERSONA_PATH, OPS_PATH, EVOLUTIONS_PATH):
            if src.is_file() and src.stat().st_mtime > out_mtime:
                need_rebuild = True
                break
    if need_rebuild:
        return build_card()
    return OUT_PATH.read_text(encoding="utf-8")


def evolve_persona(note: str) -> tuple[bool, str]:
    """
    允许大肥鱼在安全边界内「自我升级/改造人格」：
    - 进化记录持久化写入 data/persona_evolutions.json，绝不改写受 Git 跟踪的模板文件
    - 每天上限 2 条，最多保留最近 15 条，并自动重建 data/大肥鱼.compiled.md
    """
    clean = (note or "").strip().replace("\n", " ")[:120]
    if len(clean) < 4:
        return False, "进化内容太短了"

    today = datetime.now().strftime("%Y-%m-%d")
    items = _load_evolutions_list()
    today_count = sum(1 for item in items if item.startswith(f"[{today}]"))
    if today_count >= DAILY_EVOLVE_LIMIT:
        return False, f"今天已经自我进化 {DAILY_EVOLVE_LIMIT} 次啦，明天再继续长脑子"

    items.append(f"[{today}] {clean}")
    items = items[-MAX_EVOLVE_ITEMS:]
    _save_evolutions_list(items)

    build_card()
    return True, f"自我进化成功：{clean}"


def get_evolutions() -> list[str]:
    return _load_evolutions_list()


if __name__ == "__main__":
    content = build_card()
    print(f"已生成 {OUT_PATH}：{len(content)} 字")
