"""
L0 角色卡合并模块：把只读模板 roles/大肥鱼.人格.md + roles/大肥鱼.操作.md
与运行期进化记录 data/persona_evolutions.json 原子合并，输出 data/compiled_card_dafeiyu.md，
绝不修改受 Git 跟踪的模板文件。

历史说明：以前这里有「峰哥亡命天涯」(fengge) 与「塔菲大肥鱼」两套人设和一个零重启热切换
机制。2026-11-26 按需求删掉峰哥，只保留大肥鱼一个人设；函数名保持原样，是为了不牵动
bot_core / brain 里已经在用的 get_active_persona()、load_card()、evolve_persona() 等调用点。
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DATA_DIR

ROLES_DIR = Path(__file__).resolve().parent
PERSONA_KEY = "dafeiyu"
PERSONA_LABEL = "大肥鱼"
PERSONA_PATH = ROLES_DIR / "大肥鱼.人格.md"
OPS_PATH = ROLES_DIR / "大肥鱼.操作.md"
EVOLUTIONS_PATH = DATA_DIR / "persona_evolutions.json"
# 老版本的进化记录文件名（曾按人设分文件），仍兼容读取
LEGACY_EVOLUTIONS_PATH = DATA_DIR / "persona_evolutions_dafeiyu.json"

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


def get_active_persona() -> str:
    """现在只有一个人设，恒返回 dafeiyu；保留函数是为了兼容既有调用点。"""
    return PERSONA_KEY


def set_active_persona(name: str) -> tuple[bool, str]:
    """人设热切换已下线：只剩大肥鱼一个，直接说明情况，别再改状态文件。"""
    return False, "现在只有大肥鱼一个人设，不用切换"


def list_personas() -> list[dict[str, Any]]:
    return [
        {
            "key": PERSONA_KEY,
            "label": PERSONA_LABEL,
            "active": True,
            "available": PERSONA_PATH.is_file(),
        }
    ]


def _compiled_file() -> Path:
    return DATA_DIR / f"compiled_card_{PERSONA_KEY}.md"


def _load_template_evolutions() -> list[str]:
    if not PERSONA_PATH.is_file():
        return []
    lines = PERSONA_PATH.read_text(encoding="utf-8").splitlines()
    header_idx = next((i for i, line in enumerate(lines) if line.startswith(EVOLVE_HEADER)), None)
    if header_idx is None:
        return []
    return [
        line.strip()[2:].strip()
        for line in lines[header_idx + 1 :]
        if line.strip().startswith("- ") and "刚搬进这个群" not in line and "刚进群" not in line
    ][-MAX_EVOLVE_ITEMS:]


def _load_evolutions_list() -> list[str]:
    for path in (EVOLUTIONS_PATH, LEGACY_EVOLUTIONS_PATH):
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [str(item).strip() for item in raw if str(item).strip()][-MAX_EVOLVE_ITEMS:]
        except Exception:
            continue
    return _load_template_evolutions()


def _save_evolutions_list(items: list[str]) -> None:
    payload = [str(item).strip() for item in items if str(item).strip()][-MAX_EVOLVE_ITEMS:]
    _atomic_write_text(EVOLUTIONS_PATH, json.dumps(payload, ensure_ascii=False, indent=2))


def _render_persona_with_evolutions() -> str:
    raw_persona = _strip_quotes(PERSONA_PATH.read_text(encoding="utf-8")) if PERSONA_PATH.is_file() else ""
    lines = raw_persona.splitlines()
    header_idx = next((i for i, line in enumerate(lines) if line.startswith(EVOLVE_HEADER)), None)
    if header_idx is not None:
        base_lines = lines[:header_idx]
        header_line = lines[header_idx]
    else:
        base_lines = lines
        header_line = f"{EVOLVE_HEADER}（随群友相处自然沉淀）"

    items = _load_evolutions_list()
    evolve_lines = [f"- {item}" for item in items] if items else ["- （刚进群，先跟大伙混个脸熟）"]
    base_text = "\n".join(base_lines).rstrip()
    evolve_block = "\n".join([header_line, *evolve_lines])
    return f"{base_text}\n\n{evolve_block}".strip()


def build_card(persona: str | None = None) -> str:
    """合并模板并落盘。persona 参数只为兼容旧调用，传什么都拼大肥鱼。"""
    persona_text = _render_persona_with_evolutions()
    ops = _strip_quotes(OPS_PATH.read_text(encoding="utf-8")) if OPS_PATH.is_file() else ""
    merged = f"# 角色卡：{PERSONA_LABEL}\n\n{persona_text}\n\n---\n\n{ops}\n"
    if len(merged) > CHAR_LIMIT:
        raise ValueError(f"合并后角色卡共 {len(merged)} 字，超过上限 {CHAR_LIMIT}，请先精简")

    _atomic_write_text(_compiled_file(), merged)
    return merged


def load_card(persona: str | None = None) -> str:
    out_path = _compiled_file()
    need_rebuild = not out_path.is_file()
    if not need_rebuild:
        out_mtime = out_path.stat().st_mtime
        for src in (PERSONA_PATH, OPS_PATH, EVOLUTIONS_PATH):
            if src.is_file() and src.stat().st_mtime > out_mtime:
                need_rebuild = True
                break
    if need_rebuild:
        return build_card()
    return out_path.read_text(encoding="utf-8")


def evolve_persona(note: str, persona: str | None = None) -> tuple[bool, str]:
    """
    允许大肥鱼在安全边界内「自我升级/微调」：
    - 进化记录持久化写入 data/persona_evolutions.json，绝不改写受 Git 跟踪的模板文件
    - 每天上限 2 条，最多保留最近 15 条，并自动重建 data/compiled_card_dafeiyu.md
    """
    clean = (note or "").strip().replace("\n", " ")[:120]
    if len(clean) < 4:
        return False, "进化内容太短了"

    today = datetime.now().strftime("%Y-%m-%d")
    items = _load_evolutions_list()
    today_count = sum(1 for item in items if item.startswith(f"[{today}]"))
    if today_count >= DAILY_EVOLVE_LIMIT:
        return False, f"今天已经自我进化 {DAILY_EVOLVE_LIMIT} 次了，明天再继续"

    items.append(f"[{today}] {clean}")
    items = items[-MAX_EVOLVE_ITEMS:]
    _save_evolutions_list(items)

    build_card()
    return True, f"自我进化成功：{clean}"


def get_evolutions(persona: str | None = None) -> list[str]:
    return _load_evolutions_list()


if __name__ == "__main__":
    content = build_card()
    print(f"已生成 {_compiled_file()}：{len(content)} 字")
