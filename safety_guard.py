"""高危操作识别与群内二次确认拦截模块。"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from config import BASE_DIR, DATA_DIR, settings

PENDING_CONFIRM_PATH = DATA_DIR / "pending_confirmations.json"
_LOCK = threading.Lock()
_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

_SCRIPT_OR_EXE_SUFFIXES = {
    ".bat",
    ".cmd",
    ".ps1",
    ".py",
    ".exe",
    ".reg",
    ".vbs",
    ".msi",
    ".dll",
    ".scr",
}

_ACTION_LABELS = {
    "delegate_to_dsh": "DSH 后台深度执行",
    "run_shell": "执行本机 Shell/PowerShell 命令",
    "write_local_file": "写入/覆盖本机文件",
    "read_local_file": "读取本机文件",
    "send_existing_file": "读取并发送本机文件",
    "open_url_or_app": "启动本机程序/文件/链接",
    "self_edit_code": "修改大肥鱼自身源码",
}

_RISK_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "破坏性删除 / 清空 / 格式化 / 强推操作",
        re.compile(
            r"(?i)(?:"
            r"\brm\s+-[a-z]*[rf]|\brm\s+-rf\b|"
            r"(?:^|[;&|]\s*)(?:rm|del|erase|rd|rmdir)\s+|"
            r"\b(?:remove-item|clear-content|diskpart|bcdedit|sdelete)\b|"
            r"\bformat\s+[a-z]:|"
            r"\b(?:del|rd|rmdir)\s+/[sqf]|"
            r"\bgit\s+(?:reset\s+--hard|clean\b|push\s+(?:-f|--force)\b)|"
            r"\b(?:drop\s+(?:table|database|schema)|truncate\s+table)\b|"
            r"(?:删除|删掉|删光|删了|清空|清除|清掉|格式化|销毁|抹掉|彻底移除|卸载|强推|强制推送|删库)"
            r")"
        ),
    ),
    (
        "系统控制 / 强杀进程 / 注册表与权限变更",
        re.compile(
            r"(?i)(?:"
            r"\b(?:shutdown|restart-computer|stop-computer|logoff|taskkill|stop-process|"
            r"regedit|set-executionpolicy|schtasks|icacls|takeown|chmod|chown|vssadmin|netsh)\b|"
            r"\bkill\s+-9\b|"
            r"\breg\s+(?:add|delete|import|export)\b|"
            r"\bnet\s+(?:user|localgroup|share)\b|"
            r"\bsc\s+(?:stop|delete|config)\b|"
            r"(?:关机|重启电脑|注销|休眠|杀进程|结束进程|强杀|强退|注册表|防火墙|改密码|管理员权限|系统服务|开机启动|环境变量)"
            r")"
        ),
    ),
    (
        "敏感密钥 / 隐私目录或系统核心路径访问",
        re.compile(
            r"(?i)(?:"
            r"(?:^|[\s'\"/\\])\.env(?!\.example)(?:$|[\s'\"/\\.]|\b)|"
            r"\.ssh[/\\]|id_rsa|credentials(?:\.json)?|login\s+data|local\s+state|"
            r"\bcookies\b|\bapi[_-]?key\b|\b(?:access|auth|bot|github)[_-]?token\b|\bsecret\b|\bpasswd\b|\bpassword\b|"
            r"wechat\s+files|xwechat_files|"
            r"c:\\\\?windows|system32|program\s+files|\\appdata\\|/appdata/|"
            r"(?:密钥|秘钥|私钥|密码|导出聊天记录)"
            r")"
        ),
    ),
    (
        "远程下载执行 / 卸载依赖 / 资金或群发外联",
        re.compile(
            r"(?i)(?:"
            r"\birm\s+|\biex\b|\binvoke-expression\b|"
            r"\b(?:curl|wget)\b[^|\n]*\|[^|\n]*(?:sh|bash|iex|pwsh|powershell)|"
            r"powershell\s+[^\n]*-e(?:nc|ncodedcommand)?\b|"
            r"\b(?:certutil|bitsadmin)\b|"
            r"\b(?:pip|npm)\s+uninstall\b|"
            r"(?:下载并运行|下载并执行|远程执行|转账|付款|支付|发红包|群发|消息轰炸|刷屏|退群|删好友|拉黑)"
            r")"
        ),
    ),
]


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _load_entries_locked(now: float) -> list[dict[str, Any]]:
    if not PENDING_CONFIRM_PATH.is_file():
        return []
    try:
        raw = json.loads(PENDING_CONFIRM_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [
            item
            for item in raw
            if isinstance(item, dict) and float(item.get("expires_at") or 0.0) > now
        ]
    except Exception:
        return []


def _save_entries_locked(entries: list[dict[str, Any]]) -> None:
    _atomic_write_text(
        PENDING_CONFIRM_PATH,
        json.dumps(entries, ensure_ascii=False, indent=2),
    )


def _is_within_dir(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except Exception:
        return False


def _assess_path_risks(action_type: str, raw_path: str) -> list[str]:
    reasons: list[str] = []
    clean = (raw_path or "").strip()
    if not clean:
        return reasons

    p = Path(clean).expanduser()
    if action_type == "write_local_file":
        if p.exists():
            reasons.append("覆盖已存在的本机文件")
        if p.suffix.lower() in _SCRIPT_OR_EXE_SUFFIXES:
            reasons.append(f"写入可执行/脚本文件 ({p.suffix.lower()})")
        if not _is_within_dir(p, DATA_DIR):
            reasons.append("写入路径位于机器人 data/ 目录之外")
    elif action_type == "open_url_or_app":
        if not re.match(r"(?i)^https?://", clean):
            if p.suffix.lower() in _SCRIPT_OR_EXE_SUFFIXES or any(sep in clean for sep in ("\\", "/", ":", "%")):
                reasons.append("启动本机可执行程序、脚本或系统路径")
    elif action_type in ("read_local_file", "send_existing_file"):
        if re.search(r"(?i)(?:^|[\s/\\])token(?:\.|$|[/\\])", clean):
            reasons.append("疑似读取敏感凭证文件")
        if p.is_absolute() and not _is_within_dir(p, BASE_DIR):
            # 允许读取桌面/用户普通文档，但如果落在系统/应用配置根目录仍提醒
            norm = str(p).lower()
            if any(k in norm for k in ("\\windows\\", "\\program files", "\\appdata\\", "\\.ssh\\")):
                reasons.append("访问系统或应用隐藏敏感目录")
    return reasons


def assess_danger(action_type: str, params: dict[str, Any], user_text: str = "") -> tuple[bool, list[str]]:
    """判断即将执行的操作或任务是否属于高危请求，返回 (是否高危, 风险原因列表)。"""
    if not settings.ENABLE_DANGER_CONFIRM:
        return False, []

    reasons: list[str] = []

    if action_type == "self_edit_code":
        reasons.append("修改大肥鱼自身程序源码（需群内二次确认）")

    texts_to_scan: list[str] = []
    if user_text:
        texts_to_scan.append(user_text)
    for key in ("command", "task", "path", "target", "rel_path", "workdir"):
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            texts_to_scan.append(val)

    combined = "\n".join(texts_to_scan)
    for label, pattern in _RISK_RULES:
        if pattern.search(combined) and label not in reasons:
            reasons.append(label)

    if action_type == "write_local_file":
        for r in _assess_path_risks(action_type, str(params.get("path") or "")):
            if r not in reasons:
                reasons.append(r)
    elif action_type == "open_url_or_app":
        for r in _assess_path_risks(action_type, str(params.get("target") or "")):
            if r not in reasons:
                reasons.append(r)
    elif action_type in ("read_local_file", "send_existing_file"):
        for r in _assess_path_risks(action_type, str(params.get("path") or "")):
            if r not in reasons:
                reasons.append(r)

    return bool(reasons), reasons


def build_action_preview(action_type: str, params: dict[str, Any]) -> str:
    if action_type == "delegate_to_dsh":
        return str(params.get("task") or "")[:220]
    if action_type == "run_shell":
        return str(params.get("command") or "")[:220]
    if action_type == "write_local_file":
        content = str(params.get("content") or "")
        return f"写入 {params.get('path')}（{len(content)} 字符）"
    if action_type in ("read_local_file", "send_existing_file"):
        return f"路径：{params.get('path')}"
    if action_type == "open_url_or_app":
        return f"打开：{params.get('target')}"
    if action_type == "self_edit_code":
        return f"修改文件 {params.get('rel_path')}"
    return json.dumps(params, ensure_ascii=False)[:200]


def stage_pending_action(
    group_id: str,
    sender: str,
    action_type: str,
    params: dict[str, Any],
    reasons: list[str],
    preview: str = "",
) -> tuple[str, str]:
    """暂存高危操作并生成一次性 4 位确认码与群内提示卡片。"""
    now = time.time()
    ttl = settings.DANGER_CONFIRM_TTL_SEC
    norm_group = (group_id or "").strip()
    clean_preview = (preview or build_action_preview(action_type, params)).strip()
    if len(clean_preview) > 220:
        clean_preview = clean_preview[:220] + "..."

    with _LOCK:
        entries = _load_entries_locked(now)
        used_codes = {
            str(e.get("code") or "").upper()
            for e in entries
            if (e.get("group_id") or "").strip() == norm_group
        }
        code = ""
        for _ in range(50):
            cand = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
            if cand not in used_codes:
                code = cand
                break
        if not code:
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(5))

        entries.append(
            {
                "code": code,
                "group_id": norm_group,
                "sender": (sender or "").strip(),
                "action_type": action_type,
                "params": params,
                "reasons": list(reasons),
                "preview": clean_preview,
                "created_at": now,
                "expires_at": now + ttl,
            }
        )
        _save_entries_locked(entries[-30:])

    action_label = _ACTION_LABELS.get(action_type, action_type)
    reason_text = "；".join(reasons) if reasons else "命中高危操作策略"
    card = (
        f"⚠️ 【高危操作需要群内二次确认】\n"
        f"• 发起人：{sender or '群友'}\n"
        f"• 操作类型：{action_label}\n"
        f"• 风险提示：{reason_text}\n"
        f"• 待执行内容：{clean_preview}\n"
        f"• 确认码：{code}（{ttl} 秒内有效）\n\n"
        f"👉 群内任意成员回复 `/确认 {code}` 或 `确认 {code}` 即可批准执行；回复 `/取消 {code}` 或 `取消 {code}` 放弃。"
    )
    return code, card


def list_pending_actions(group_id: str) -> list[dict[str, Any]]:
    now = time.time()
    norm_group = (group_id or "").strip()
    with _LOCK:
        entries = _load_entries_locked(now)
        _save_entries_locked(entries)
    matched = [e for e in entries if (e.get("group_id") or "").strip() == norm_group]
    matched.sort(key=lambda e: float(e.get("created_at") or 0.0), reverse=True)
    return matched


def pop_pending_action(group_id: str, code: str = "") -> tuple[dict[str, Any] | None, str]:
    """取出并作废匹配的一次性待确认操作。"""
    now = time.time()
    norm_group = (group_id or "").strip()
    norm_code = (code or "").strip().upper()

    with _LOCK:
        entries = _load_entries_locked(now)
        group_items = [
            (idx, e)
            for idx, e in enumerate(entries)
            if (e.get("group_id") or "").strip() == norm_group
        ]

        if not group_items:
            _save_entries_locked(entries)
            return None, "当前群里没有待确认的高危操作（或已过期）"

        target_idx: int | None = None
        if norm_code:
            for idx, e in group_items:
                if str(e.get("code") or "").upper() == norm_code:
                    target_idx = idx
                    break
            if target_idx is None:
                _save_entries_locked(entries)
                codes_hint = "、".join(str(e.get("code") or "") for _, e in group_items)
                return None, f"未找到确认码 `{norm_code}`（或已过期）。当前待确认码：{codes_hint}"
        else:
            if len(group_items) > 1:
                _save_entries_locked(entries)
                codes_hint = "、".join(str(e.get("code") or "") for _, e in group_items)
                return None, f"当前有多个待确认操作，请带上确认码（例如 `/确认 {group_items[-1][1].get('code')}`）。可选：{codes_hint}"
            target_idx = group_items[0][0]

        chosen = entries.pop(target_idx)
        _save_entries_locked(entries)
        return chosen, "OK"


def cancel_pending_action(
    group_id: str,
    code: str = "",
    requester: str = "",
    is_requester_owner: bool = True,
) -> tuple[bool, str]:
    """取消并作废本群匹配的待确认操作。"""
    now = time.time()
    norm_group = (group_id or "").strip()
    norm_code = (code or "").strip().upper()
    norm_req = (requester or "").strip().lower()

    with _LOCK:
        entries = _load_entries_locked(now)
        group_items = [
            (idx, e)
            for idx, e in enumerate(entries)
            if (e.get("group_id") or "").strip() == norm_group
        ]

        if not group_items:
            _save_entries_locked(entries)
            return False, "当前群里没有可取消的待确认操作"

        target_idx: int | None = None
        if norm_code:
            for idx, e in group_items:
                if str(e.get("code") or "").upper() == norm_code:
                    target_idx = idx
                    break
            if target_idx is None:
                _save_entries_locked(entries)
                return False, f"未找到确认码 `{norm_code}`（可能已执行或已过期）"
        else:
            if len(group_items) > 1:
                _save_entries_locked(entries)
                codes_hint = "、".join(str(e.get("code") or "") for _, e in group_items)
                return False, f"当前有多个待确认操作，请指定确认码 `/取消 确认码`（可选：{codes_hint}）"
            target_idx = group_items[0][0]

        item = entries[target_idx]
        item_sender = str(item.get("sender") or "").strip().lower()
        if not is_requester_owner and norm_req and item_sender and norm_req != item_sender:
            _save_entries_locked(entries)
            return False, f"🛑 只有主人或原发起人（{item.get('sender')}）可以取消操作 `{item.get('code')}`"

        entries.pop(target_idx)
        _save_entries_locked(entries)
        return True, f"已取消高危操作 `{item.get('code')}`（{item.get('preview', '')[:40]}）"
