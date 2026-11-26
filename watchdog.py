"""大肥鱼守护看门狗进程：支持自重启、快速崩溃回滚与退避拉起。

2026-11-26 事故备忘：同一台机器上跑起了两个 watchdog，第二个拉起的子进程
因 8765 端口被第一个占用而以 exit=3 退出，被误判成「刚启动即崩溃」，于是
反复 git reset --hard 把本地 HEAD 打回旧提交，还一路往群里播报「已回滚」。
现在三层防护：
  1. 单例锁：同一时刻只允许一个 watchdog 拉起子进程；
  2. 端口预检：端口被占用属于环境冲突，等它释放，不回滚、不播报；
  3. 回滚闸门：工作区脏 / 稳定版本落后于 origin/main 时绝不 reset --hard。
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from config import BASE_DIR, DATA_DIR, settings
from self_ops import (
    PORT_BUSY_EXIT_CODE,
    RESTART_EXIT_CODE,
    git_current_commit,
    is_port_in_use,
    load_watchdog_state,
    save_restart_notice,
    save_watchdog_state,
)

HEALTH_STABLE_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 0.5
RESTART_DELAY_SECONDS = 1.5
PORT_WAIT_SECONDS = 60.0
LOCK_FILE = DATA_DIR / "watchdog.lock"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dafeiyu.watchdog")


@contextlib.contextmanager
def _single_instance() -> Iterator[bool]:
    """跨进程单例：拿不到锁说明已经有一个 watchdog 在跑。

    用操作系统文件锁而不是「锁文件在不在」：进程被强杀 / 崩溃后锁由内核
    释放，不会留下需要人工清理的陈旧锁文件。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+", encoding="utf-8")
    locked = False
    try:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write("dafeiyu-watchdog\n")
            fh.flush()
        fh.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                pass
        yield locked
    finally:
        if locked:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(BASE_DIR), *args],
            cwd=str(BASE_DIR),
            env=_git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        return proc.returncode, (proc.stdout or proc.stderr or "").strip()
    except Exception as exc:
        return 1, str(exc)


def _needs_web_port(args: list[str]) -> bool:
    """wxauto / ilink 模式不占用 Web 端口，无需做端口冲突检查。"""
    mode = settings.RUN_MODE
    if "--mode" in args:
        idx = args.index("--mode")
        if idx + 1 < len(args):
            mode = args[idx + 1]
    return mode in ("web", "qq", "both")


def _wait_port_free(timeout: float = PORT_WAIT_SECONDS) -> bool:
    host, port = settings.WEB_HOST, settings.WEB_PORT
    if not is_port_in_use(host, port):
        return True
    logger.warning(
        "端口 %s:%d 已被占用（多半是另一个大肥鱼实例或残留进程），等待其释放，不做版本回滚…",
        host,
        port,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            time.sleep(1.0)
        except KeyboardInterrupt:
            return False
        if not is_port_in_use(host, port):
            logger.info("端口 %s:%d 已释放，继续拉起主程序。", host, port)
            return True
    return False


def _refresh_stale_last_good(state: dict[str, Any], head: str) -> None:
    """稳定版本若已落后于远端主线，且当前正跑在远端最新版本上，就刷新它。

    防止 watchdog_state.json 里留着很旧的 last_good_commit，下次一崩溃就把
    已经推送过的代码 reset 回去。
    """
    if not head:
        return
    rc, remote = _git("rev-parse", "--verify", "origin/main")
    if rc != 0 or remote != head:
        return
    stale = str(state.get("last_good_commit") or "").strip()
    if not stale or stale == head:
        return
    rc2, behind = _git("rev-list", "--count", f"{stale}..{remote}")
    if rc2 == 0 and behind.isdigit() and int(behind) > 0:
        logger.info("稳定版本 %s 已落后于 origin/main，刷新为 %s", stale[:7], head[:7])
        state["last_good_commit"] = head
        state["failed_commit"] = ""
        state["crash_streak"] = 0
        save_watchdog_state(state)


def _rollback_to_commit(last_good_commit: str, start_commit: str) -> tuple[bool, str]:
    """安全回滚到指定提交，绝不清理 .env / data / state，且先过安全闸门。"""
    if not last_good_commit or not (BASE_DIR / ".git").exists():
        return False, "无可用 Git 稳定提交记录"

    # 闸门 1：崩溃的就是稳定版本本身，没有「新版本」可回滚；重复 reset 只会
    # 一次次把 HEAD 打回同一个旧点（本次事故里循环了十几轮）。
    if not start_commit or start_commit == last_good_commit:
        return False, "崩溃版本与稳定版本相同，无新版本可回滚"

    # 闸门 2：工作区不干净就别碰 git，夜间任务 / 工具可能正在写文件。
    rc, dirty = _git("status", "--porcelain")
    if rc != 0:
        return False, "无法确认工作区状态"
    if dirty:
        return False, "工作区有未提交改动，拒绝回滚（避免吞掉未落盘的修改）"

    # 闸门 3：稳定版本落后于已推送的 origin/main 时绝不回滚。否则一次环境
    # 故障就能把本地打回「behind N」，还会和自动 pull 来回拉锯。
    rc, remote = _git("rev-parse", "--verify", "origin/main")
    if rc == 0 and remote:
        rc2, behind = _git("rev-list", "--count", f"{last_good_commit}..origin/main")
        if rc2 == 0 and behind.isdigit() and int(behind) > 0:
            return False, (
                f"稳定版本 {last_good_commit[:7]} 比 origin/main 落后 {behind} 个提交，"
                "拒绝回滚（不能把已推送的代码打回去）"
            )

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


def _watchdog_loop(args: list[str], target_script: Path) -> int:
    web_mode = _needs_web_port(args)

    state = load_watchdog_state()
    initial_commit = git_current_commit()
    if initial_commit and not state.get("last_good_commit"):
        state["last_good_commit"] = initial_commit
        save_watchdog_state(state)
    _refresh_stale_last_good(state, initial_commit)
    state = load_watchdog_state()
    crash_streak = int(state.get("crash_streak") or 0)

    while True:
        # 启动前先看端口：占用属于环境冲突，等释放，绝不把它当代码崩溃
        if web_mode and not _wait_port_free():
            logger.error(
                "端口 %s:%d 持续被占用超过 %.0fs，看门狗停止。请检查是否还有残留的大肥鱼实例。",
                settings.WEB_HOST,
                settings.WEB_PORT,
                PORT_WAIT_SECONDS,
            )
            return 3

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

        # 端口被占用 = 环境冲突，不是代码问题：不回滚、不累加崩溃次数、不往群里播报
        if rc == PORT_BUSY_EXIT_CODE:
            logger.error(
                "主程序因 Web 端口被占用退出 (exit=%d)，属环境冲突，不做版本回滚。",
                rc,
            )
            state = load_watchdog_state()
            state["last_crash_reason"] = f"port_busy exit={rc}"
            save_watchdog_state(state)
            if not _wait_port_free():
                logger.error(
                    "端口 %s:%d 持续被占用，看门狗停止，请人工检查残留进程。",
                    settings.WEB_HOST,
                    settings.WEB_PORT,
                )
                return 3
            continue

        # 异常崩溃处理
        crash_streak += 1
        state = load_watchdog_state()
        last_good = str(state.get("last_good_commit") or "").strip()
        rolled_back_msg = ""

        if uptime < HEALTH_STABLE_SECONDS and last_good:
            if start_commit and start_commit != last_good:
                state["failed_commit"] = start_commit
            ok_rb, rb_detail = _rollback_to_commit(last_good, start_commit)
            if ok_rb:
                rolled_back_msg = (
                    f"⚠️ 检测到刚启动即崩溃（退出码 {rc}，运行 {uptime:.1f}s），"
                    f"已自动回滚至稳定版本 {last_good[:7]} 并重新启动。"
                )
                save_restart_notice("", rolled_back_msg)
            else:
                # 闸门拦下或回滚失败时只记日志，别往群里发「已回滚」这种误导消息
                logger.warning("启动即崩溃，但未执行回滚：%s", rb_detail)
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


def run_watchdog(child_args: list[str] | None = None, entry_script: Path | None = None) -> int:
    args = list(child_args) if child_args is not None else []
    target_script = entry_script or (BASE_DIR / "main.py")

    with _single_instance() as got_lock:
        if not got_lock:
            logger.error(
                "已有另一个 watchdog 在运行（锁文件 %s），本实例直接退出，避免双开抢端口。",
                LOCK_FILE,
            )
            print("已有大肥鱼看门狗在运行，本窗口无需重复启动。", flush=True)
            return 0
        return _watchdog_loop(args, target_script)


def main() -> None:
    sys.exit(run_watchdog(sys.argv[1:]))


if __name__ == "__main__":
    main()
