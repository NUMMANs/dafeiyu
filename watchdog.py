"""大肥鱼守护看门狗进程：支持自重启、快速崩溃回滚与退避拉起。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from config import BASE_DIR
from self_ops import (
    RESTART_EXIT_CODE,
    git_current_commit,
    load_watchdog_state,
    save_restart_notice,
    save_watchdog_state,
)

HEALTH_STABLE_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 0.5
RESTART_DELAY_SECONDS = 1.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dafeiyu.watchdog")


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _rollback_to_commit(last_good_commit: str) -> tuple[bool, str]:
    """安全回滚受 Git 跟踪的代码文件到指定提交，绝不清理 .env / data / state。"""
    if not last_good_commit or not (BASE_DIR / ".git").exists():
        return False, "无可用 Git 稳定提交记录"

    for git_cmd in (
        ["git", "reset", "--hard", last_good_commit],
        ["git", "checkout", last_good_commit, "--", "."],
    ):
        try:
            proc = subprocess.run(
                git_cmd,
                cwd=str(BASE_DIR),
                env=_git_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
            )
            if proc.returncode == 0:
                return True, f"已回滚至 {last_good_commit[:7]}"
        except Exception as exc:
            logger.warning("执行 %s 失败: %s", " ".join(git_cmd), exc)

    return False, f"回滚至 {last_good_commit[:7]} 失败"


def _stop_child(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3.0)
        except Exception:
            pass


def run_watchdog(child_args: list[str] | None = None, entry_script: Path | None = None) -> int:
    args = list(child_args) if child_args is not None else []
    target_script = entry_script or (BASE_DIR / "main.py")

    state = load_watchdog_state()
    initial_commit = git_current_commit()
    if initial_commit and not state.get("last_good_commit"):
        state["last_good_commit"] = initial_commit
        save_watchdog_state(state)

    crash_streak = int(state.get("crash_streak") or 0)

    while True:
        state = load_watchdog_state()
        start_commit = git_current_commit()
        state["last_start_ts"] = time.time()
        save_watchdog_state(state)

        child_env = os.environ.copy()
        child_env["PYTHONUTF8"] = "1"
        child_env["DAFEIYU_UNDER_WATCHDOG"] = "1"

        cmd = [sys.executable, str(target_script), *args]
        logger.info("启动主程序: %s (commit=%s)", " ".join(cmd), start_commit[:7] if start_commit else "local")
        proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), env=child_env)

        start_mono = time.monotonic()
        marked_healthy = False

        try:
            while True:
                rc = proc.poll()
                if rc is not None:
                    break
                if not marked_healthy and (time.monotonic() - start_mono) >= HEALTH_STABLE_SECONDS:
                    marked_healthy = True
                    crash_streak = 0
                    state = load_watchdog_state()
                    if start_commit:
                        state["last_good_commit"] = start_commit
                    state["failed_commit"] = ""
                    state["crash_streak"] = 0
                    state["last_healthy_ts"] = time.time()
                    save_watchdog_state(state)
                    logger.info(
                        "子进程稳定运行满 %.0fs，记录健康版本: %s",
                        HEALTH_STABLE_SECONDS,
                        start_commit[:7] if start_commit else "local",
                    )
                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，正在关闭子进程...")
            _stop_child(proc)
            return 0

        uptime = time.monotonic() - start_mono

        if rc == 0:
            logger.info("主程序正常退出 (exit=0)，看门狗停止。")
            return 0

        if rc == RESTART_EXIT_CODE:
            crash_streak = 0
            state = load_watchdog_state()
            state["crash_streak"] = 0
            save_watchdog_state(state)
            logger.info("收到重启信号 (exit=%d)，%.1fs 后拉起新进程...", RESTART_EXIT_CODE, RESTART_DELAY_SECONDS)
            try:
                time.sleep(RESTART_DELAY_SECONDS)
            except KeyboardInterrupt:
                return 0
            continue

        # 异常崩溃处理
        crash_streak += 1
        state = load_watchdog_state()
        last_good = str(state.get("last_good_commit") or "").strip()
        rolled_back_msg = ""

        if uptime < HEALTH_STABLE_SECONDS and last_good:
            if start_commit and start_commit != last_good:
                state["failed_commit"] = start_commit
            ok_rb, rb_detail = _rollback_to_commit(last_good)
            if ok_rb:
                rolled_back_msg = (
                    f"⚠️ 检测到刚启动即崩溃（退出码 {rc}，运行 {uptime:.1f}s），"
                    f"已自动回滚至稳定版本 {last_good[:7]} 并重新启动。"
                )
            else:
                rolled_back_msg = (
                    f"⚠️ 检测到刚启动即崩溃（退出码 {rc}，运行 {uptime:.1f}s），"
                    f"{rb_detail}，正在尝试退避重启。"
                )
            save_restart_notice("", rolled_back_msg)
        elif uptime < HEALTH_STABLE_SECONDS:
            save_restart_notice(
                "",
                f"⚠️ 进程启动异常退出（退出码 {rc}，运行 {uptime:.1f}s），看门狗已自动重新拉起。",
            )

        state["crash_streak"] = crash_streak
        state["last_crash_reason"] = f"exit={rc}, uptime={uptime:.1f}s"
        save_watchdog_state(state)

        backoff = min(30, 3 * crash_streak)
        logger.warning(
            "主程序异常退出 (exit=%s, uptime=%.1fs, crash_streak=%d)%s，%ds 后退避重启...",
            rc,
            uptime,
            crash_streak,
            f" [{rolled_back_msg}]" if rolled_back_msg else "",
            backoff,
        )
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            return 0


def main() -> None:
    sys.exit(run_watchdog(sys.argv[1:]))


if __name__ == "__main__":
    main()
