"""
L0 角色卡合并脚本：
将 roles/大肥鱼.人格.md + roles/大肥鱼.操作.md 原子合并为 roles/大肥鱼.md（上限 6000 字）。
"""
from datetime import datetime
from pathlib import Path
import shutil

ROLES_DIR = Path(__file__).resolve().parent
PERSONA_PATH = ROLES_DIR / "大肥鱼.人格.md"
OPS_PATH = ROLES_DIR / "大肥鱼.操作.md"
OUT_PATH = ROLES_DIR / "大肥鱼.md"
CHAR_LIMIT = 6000
EVOLVE_HEADER = "## 我的自我进化记录"
MAX_EVOLVE_ITEMS = 15
DAILY_EVOLVE_LIMIT = 2


def _strip_quotes(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">")).strip()


def build_card() -> str:
    persona = _strip_quotes(PERSONA_PATH.read_text(encoding="utf-8")) if PERSONA_PATH.is_file() else ""
    ops = _strip_quotes(OPS_PATH.read_text(encoding="utf-8")) if OPS_PATH.is_file() else ""
    merged = f"# 角色卡：大肥鱼\n\n{persona}\n\n---\n\n{ops}\n"
    if len(merged) > CHAR_LIMIT:
        raise ValueError(f"合并后角色卡共 {len(merged)} 字，超过上限 {CHAR_LIMIT}，请先精简")

    tmp = OUT_PATH.with_suffix(".md.tmp")
    tmp.write_text(merged, encoding="utf-8")
    tmp.replace(OUT_PATH)  # 原子替换
    return merged


def load_card() -> str:
    # 若人格或操作文件比合并文件新，自动重新合并
    need_rebuild = not OUT_PATH.is_file()
    if not need_rebuild:
        out_mtime = OUT_PATH.stat().st_mtime
        for src in (PERSONA_PATH, OPS_PATH):
            if src.is_file() and src.stat().st_mtime > out_mtime:
                need_rebuild = True
                break
    if need_rebuild:
        return build_card()
    return OUT_PATH.read_text(encoding="utf-8")


def evolve_persona(note: str) -> tuple[bool, str]:
    """
    允许大肥鱼在安全边界内「自我升级/改造人格」：
    - 仅追加到 roles/大肥鱼.人格.md 的进化记录区（绝不碰操作规范）
    - 每天上限 2 条，最多保留最近 15 条，修改前自动备份并原子重建角色卡
    """
    clean = (note or "").strip().replace("\n", " ")[:120]
    if len(clean) < 4:
        return False, "进化内容太短了"

    today = datetime.now().strftime("%Y-%m-%d")
    text = PERSONA_PATH.read_text(encoding="utf-8") if PERSONA_PATH.is_file() else ""
    lines = text.splitlines()

    # 统计今天已经自我进化了几条
    today_count = sum(1 for l in lines if l.startswith(f"- [{today}]"))
    if today_count >= DAILY_EVOLVE_LIMIT:
        return False, f"今天已经自我进化 {DAILY_EVOLVE_LIMIT} 次啦，明天再继续长脑子"

    # 备份旧人格文件
    bak = PERSONA_PATH.with_suffix(".md.bak")
    if PERSONA_PATH.is_file():
        shutil.copyfile(PERSONA_PATH, bak)

    # 找到进化区，保留最近 MAX_EVOLVE_ITEMS - 1 条
    header_idx = next((i for i, l in enumerate(lines) if l.startswith(EVOLVE_HEADER)), None)
    if header_idx is None:
        lines.append("")
        lines.append(f"{EVOLVE_HEADER}（我会随着和群友相处自己长出新习惯和新性格）")
        header_idx = len(lines) - 1

    before = lines[: header_idx + 1]
    existing_items = [
        l for l in lines[header_idx + 1 :] if l.strip().startswith("- ") and "刚搬进这个群" not in l
    ]
    existing_items.append(f"- [{today}] {clean}")
    existing_items = existing_items[-MAX_EVOLVE_ITEMS:]

    new_content = "\n".join(before + existing_items) + "\n"
    tmp = PERSONA_PATH.with_suffix(".md.tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(PERSONA_PATH)

    # 立即重新合并角色卡
    build_card()
    return True, f"自我进化成功：{clean}"


def get_evolutions() -> list[str]:
    if not PERSONA_PATH.is_file():
        return []
    lines = PERSONA_PATH.read_text(encoding="utf-8").splitlines()
    header_idx = next((i for i, l in enumerate(lines) if l.startswith(EVOLVE_HEADER)), None)
    if header_idx is None:
        return []
    return [l.strip()[2:] for l in lines[header_idx + 1 :] if l.strip().startswith("- ")]


if __name__ == "__main__":
    content = build_card()
    print(f"已生成 {OUT_PATH}：{len(content)} 字")
