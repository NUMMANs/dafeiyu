"""
自然语言定时任务引擎（纯 Python 标准库 + state/schedules.json 持久化，重启不丢）：
支持 3 种调度类型：
- once:     一次性任务（如 10 分钟后、或指定 YYYY-MM-DD HH:MM）
- daily:    每日定时（如 08:30、21:00）
- interval: 固定间隔循环（每隔 N 分钟，最低 5 分钟防刷屏）

支持 2 种执行模式：
- remind:   到点直接发送提醒内容到群里
- ai_task:  到点让大肥鱼大脑实际执行指令（如联网查天气/查比赛/截屏/执行命令）再把结果发群里
"""
from datetime import datetime, timedelta
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from config import BASE_DIR

logger = logging.getLogger("dafeiyu.scheduler")

STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SCHEDULES_PATH = STATE_DIR / "schedules.json"


def _atomic_write(path: Path, data: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class TaskScheduler:
    def __init__(self, path: Path = SCHEDULES_PATH):
        self._lock = threading.Lock()
        self.path = path
        self._tasks: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return raw
            except Exception:
                pass
        return []

    def _save_locked(self) -> None:
        _atomic_write(self.path, self._tasks)

    def add_task(
        self,
        group_id: str,
        creator: str,
        task_type: str,
        content: str,
        mode: str = "remind",
        delay_minutes: int | None = None,
        run_at: str | None = None,
        daily_time: str | None = None,
        interval_minutes: int | None = None,
    ) -> tuple[bool, str]:
        """
        添加定时任务：
        - task_type='once':     传 delay_minutes (如 10) 或 run_at ('YYYY-MM-DD HH:MM')
        - task_type='daily':    传 daily_time ('HH:MM'，如 '08:30')
        - task_type='interval': 传 interval_minutes (>= 5)
        """
        clean_content = (content or "").strip()[:240]
        if not clean_content:
            return False, "任务内容不能为空"

        now_dt = datetime.now()
        now_ts = now_dt.timestamp()
        task_type = (task_type or "once").strip().lower()
        mode = "ai_task" if mode == "ai_task" else "remind"

        next_run_ts = 0.0
        desc = ""

        if task_type == "once":
            if delay_minutes is not None and delay_minutes > 0:
                target_dt = now_dt + timedelta(minutes=int(delay_minutes))
                next_run_ts = target_dt.timestamp()
                desc = f"一次性（{target_dt.strftime('%m-%d %H:%M')}）"
            elif run_at:
                try:
                    target_dt = datetime.strptime(run_at.strip(), "%Y-%m-%d %H:%M")
                except ValueError:
                    try:
                        hh, mm = map(int, run_at.strip().split(":"))
                        target_dt = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
                        if target_dt.timestamp() <= now_ts:
                            target_dt += timedelta(days=1)
                    except Exception:
                        return False, "时间格式不对，请用 YYYY-MM-DD HH:MM 或 HH:MM"
                next_run_ts = target_dt.timestamp()
                desc = f"一次性（{target_dt.strftime('%m-%d %H:%M')}）"
            else:
                return False, "一次性任务需要指定几分钟后（delay_minutes）或具体时间（run_at）"

        elif task_type == "daily":
            if not daily_time or ":" not in daily_time:
                return False, "每日任务需要指定时间，格式 HH:MM（如 08:30）"
            try:
                hh, mm = map(int, daily_time.strip().split(":")[:2])
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    raise ValueError
            except ValueError:
                return False, "每日时间格式错误，应为 00:00 ~ 23:59"
            target_dt = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target_dt.timestamp() <= now_ts:
                target_dt += timedelta(days=1)
            next_run_ts = target_dt.timestamp()
            daily_time = f"{hh:02d}:{mm:02d}"
            desc = f"每天 {daily_time}"

        elif task_type == "interval":
            mins = max(5, int(interval_minutes or 30))
            next_run_ts = now_ts + mins * 60
            interval_minutes = mins
            desc = f"每 {mins} 分钟"
        else:
            return False, f"不支持的任务类型: {task_type}"

        task_id = uuid.uuid4().hex[:6]
        task = {
            "id": task_id,
            "group_id": group_id,
            "creator": creator,
            "task_type": task_type,
            "mode": mode,
            "content": clean_content,
            "daily_time": daily_time,
            "interval_minutes": interval_minutes,
            "next_run_ts": round(next_run_ts, 1),
            "desc": desc,
            "created_at": now_dt.strftime("%Y-%m-%d %H:%M"),
        }

        with self._lock:
            if len(self._tasks) >= 50:
                return False, "定时任务数量已达上限（50条），请先删掉几个旧任务"
            self._tasks.append(task)
            self._save_locked()

        mode_label = "AI自动执行" if mode == "ai_task" else "直接提醒"
        logger.info("⏰ 新增定时任务 [%s] (%s/%s) -> %s", task_id, desc, mode_label, clean_content)
        return True, f"已创建定时任务 #{task_id}（{desc}｜{mode_label}）：{clean_content}"

    def list_tasks(self, group_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if not group_id:
                return list(self._tasks)
            return [t for t in self._tasks if t.get("group_id") == group_id]

    def delete_task(self, task_id: str) -> tuple[bool, str]:
        tid = (task_id or "").strip().lstrip("#")
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.get("id") != tid]
            if len(self._tasks) < before:
                self._save_locked()
                return True, f"已取消定时任务 #{tid}"
        return False, f"没找到定时任务 #{tid}"

    def pop_due_tasks(self) -> list[dict[str, Any]]:
        """取出所有已到点的任务，并自动推进 daily/interval 的下次执行时间或删除 once 任务"""
        now = time.time()
        due: list[dict[str, Any]] = []
        changed = False

        with self._lock:
            remaining: list[dict[str, Any]] = []
            for t in self._tasks:
                if float(t.get("next_run_ts", 0)) <= now:
                    due.append(dict(t))
                    changed = True
                    ttype = t.get("task_type")
                    if ttype == "daily" and t.get("daily_time"):
                        hh, mm = map(int, t["daily_time"].split(":"))
                        nxt = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
                        while nxt.timestamp() <= now:
                            nxt += timedelta(days=1)
                        t["next_run_ts"] = round(nxt.timestamp(), 1)
                        remaining.append(t)
                    elif ttype == "interval":
                        mins = max(5, int(t.get("interval_minutes") or 30))
                        t["next_run_ts"] = round(now + mins * 60, 1)
                        remaining.append(t)
                    # once 任务执行完自动移除
                else:
                    remaining.append(t)

            if changed:
                self._tasks = remaining
                self._save_locked()
        return due


scheduler = TaskScheduler()
