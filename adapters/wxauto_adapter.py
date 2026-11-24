import logging
import random
import re
import threading
import time
from pathlib import Path

from bot_core import bot_core
from config import UPLOADS_DIR, settings
from pet_engine import pet_engine
from scheduler import scheduler

logger = logging.getLogger("dafeiyu.wxauto")

WXID_PREFIX_RE = re.compile(r"^[a-zA-Z0-9_-]+:\s*\n?")


class WxAutoAdapter:
    """
    Windows PC 微信群适配器：
    - 优先使用 `wechatauto` (wechatauto-replica)，原生兼容微信 4.1 / 4.2
    - 自动跳过启动前的历史消息回放（基于 create_time）
    - 通过 MediaDownloader 本地解密微信 4.2 群图片并交给 DeepSeek 多模态识图
    - 回复文本（支持 @ 发送者）+ 自动发送大肥鱼表情包图片
    """

    def __init__(self, groups: list[str] | None = None, poll_interval: float = 1.0):
        self.groups = groups or settings.WX_GROUP_WHITELIST
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self.running = False
        self.last_error: str | None = None
        self._start_ts: float = time.time()
        self._media_downloader = None

    def _clean_group_text(self, raw: str) -> str:
        text = (raw or "").strip()
        # 微信 DB 群聊文本开头通常带有 "wxid_xxxx:\n"
        if text.startswith("wxid_"):
            text = WXID_PREFIX_RE.sub("", text, count=1).strip()
        return text

    def _is_non_text_xml(self, text: str) -> bool:
        t = text.lstrip()
        return (
            t.startswith("<?xml")
            or t.startswith("<msg>")
            or t.startswith("<appmsg")
            or t.startswith("<emoji")
            or t.startswith("<voicemsg")
            or t.startswith("<videomsg")
        )

    def _get_media_downloader(self, chat, wx=None):
        if self._media_downloader is not None:
            return self._media_downloader
        db = getattr(chat, "_db", None) or (getattr(wx, "_db", None) if wx is not None else None)
        if db is None:
            return None
        try:
            from wechatauto.media import MediaDownloader

            self._media_downloader = MediaDownloader(db, save_dir=str(UPLOADS_DIR))
            logger.info("已初始化 wechatauto MediaDownloader（支持微信 4.2 本地图片解密）")
            return self._media_downloader
        except Exception as exc:
            logger.warning("初始化 MediaDownloader 失败: %s", exc)
            return None

    def _extract_image_path(self, group_name: str, msg, chat, wx=None) -> Path | None:
        # 1. 优先用 wechatauto MediaDownloader 直接本地解密 DB 图片（先等高清/压缩版 full，避免拿到糊成马赛克的 _t 缩略图）
        local_id = getattr(msg, "local_id", None)
        if local_id is not None:
            downloader = self._get_media_downloader(chat, wx)
            if downloader is not None:
                user_key = getattr(chat, "_wxid", None) or group_name
                # 前 3 次优先等高清/标准图 full（绝不用极糊的缩略图），第 4 次兜底 best
                for attempt, tier in enumerate(("full", "full", "full", "best")):
                    try:
                        out_path = downloader.download_image(
                            user=user_key,
                            local_id=int(local_id),
                            save_dir=str(UPLOADS_DIR),
                            tier=tier,
                        )
                        if out_path:
                            p = Path(str(out_path))
                            if p.is_file() and p.stat().st_size > 1024:
                                logger.info("图片解密成功 (tier=%s, size=%dB): %s", tier, p.stat().st_size, p.name)
                                return p
                    except Exception as exc:
                        logger.warning("MediaDownloader 第 %d 次解密图片失败 (tier=%s): %s", attempt + 1, tier, exc)
                    if attempt < 3:
                        time.sleep(1.2)

        # 2. wxauto4: FriendImageMessage.download(dir_path=...)
        if hasattr(msg, "download"):
            try:
                dl_res = msg.download(dir_path=str(UPLOADS_DIR))
                candidate = getattr(dl_res, "data", dl_res)
                if candidate:
                    p = Path(str(candidate))
                    if p.is_file():
                        return p
            except Exception as exc:
                logger.warning("wxauto4 下载微信图片失败: %s", exc)

        # 3. 兜底：content 本身就是本地图片路径
        content = str(getattr(msg, "content", "") or "").strip()
        if content:
            p = Path(content)
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                return p
        return None

    def _dispatch_message(self, group_name: str, msg, chat, wx=None) -> None:
        # 启动前就已存在的历史消息：不触发回复，但静默写入短期记忆，让大肥鱼刚启动就懂群里刚才在聊啥！
        create_time = getattr(msg, "create_time", None)
        if isinstance(create_time, (int, float)) and create_time > 0:
            if create_time < self._start_ts - 3:
                raw_hist = self._clean_group_text(str(getattr(msg, "content", "") or ""))
                if raw_hist and not self._is_non_text_xml(raw_hist):
                    hist_sender = str(getattr(msg, "sender", "群友") or "群友")
                    is_me = getattr(msg, "is_self", False) or str(getattr(msg, "attr", "")).lower() == "self"
                    pet_engine.append_history(
                        group_id=group_name,
                        sender=settings.BOT_NAME if is_me else hist_sender,
                        role="assistant" if is_me else "user",
                        content=raw_hist[:200],
                    )
                return

        if getattr(msg, "is_self", False) or getattr(msg, "is_system", False):
            return

        attr = str(getattr(msg, "attr", "")).lower()
        msg_type = str(getattr(msg, "type", "")).lower()
        cls_name = msg.__class__.__name__.lower()

        if attr in ("self", "system", "time", "tickle") or msg_type in ("self", "sys", "time", "recall"):
            return
        if "self" in cls_name or "system" in cls_name or "time" in cls_name:
            return

        sender = str(getattr(msg, "sender", "群友") or "群友")
        content = self._clean_group_text(str(getattr(msg, "content", "") or ""))
        is_group_chat = "@chatroom" in str(getattr(chat, "_wxid", ""))
        force_trigger = not is_group_chat  # 1v1 私聊每条必回，群聊遵守 GROUP_AT_ONLY

        t0 = time.time()
        if msg_type == "image" or "image" in cls_name:
            img_path = self._extract_image_path(group_name, msg, chat, wx=wx)
            logger.info("[%s] 收到 %s 的新图片: %s", group_name, sender, img_path)
            if img_path is None:
                return
            resp = bot_core.handle_message(
                group_id=group_name,
                sender=sender,
                text="",
                image_path=img_path,
                force_trigger=force_trigger,
            )
        else:
            if not content or self._is_non_text_xml(content):
                return
            logger.info("[%s] 收到 %s: %s", group_name, sender, content[:80])
            resp = bot_core.handle_message(
                group_id=group_name,
                sender=sender,
                text=content,
                force_trigger=force_trigger,
            )

        if not resp.should_reply:
            return

        bubbles = resp.get_bubbles()
        logger.info("[%s] 大肥鱼准备分段回复 %s (%d条): %s", group_name, sender, len(bubbles), resp.text[:80])
        for idx, bubble in enumerate(bubbles):
            chat.SendMsg(bubble)
            if idx < len(bubbles) - 1:
                time.sleep(0.15)

        if resp.sticker_path and resp.sticker_path.is_file() and resp.sticker_path.stat().st_size > 0:
            time.sleep(0.15)
            sticker_abs = str(resp.sticker_path.resolve())
            if hasattr(chat, "SendFiles"):
                send_res = chat.SendFiles(sticker_abs)
                logger.info("[%s] 发送附件 [%s] 结果: %s", group_name, resp.sticker_path.name, send_res)
            elif hasattr(chat, "SendImage"):
                send_res = chat.SendImage(sticker_abs)
                logger.info("[%s] 发送图片 [%s] 结果: %s", group_name, resp.sticker_path.name, send_res)

    def run_forever(self) -> None:
        WeChat = None
        loaded_name = ""
        for mod_name in ("wechatauto", "wxauto4", "wxautox4", "wxauto"):
            try:
                mod = __import__(mod_name, fromlist=["WeChat"])
                WeChat = getattr(mod, "WeChat")
                loaded_name = mod_name
                logger.info("已加载微信自动化库: %s", mod_name)
                break
            except Exception:
                continue

        if WeChat is None:
            self.last_error = "未安装微信自动化库，请先运行: pip install wechatauto-replica"
            logger.error(self.last_error)
            return

        self._start_ts = time.time()
        try:
            wx = WeChat(ads=False) if loaded_name == "wxauto4" else WeChat()
            logger.info("Windows PC 微信连接成功！准备监听微信群: %s", self.groups)
        except Exception as exc:
            self.last_error = f"初始化 PC 微信失败（请确保微信主窗口已打开且已登录，不要最小化到托盘）: {exc}"
            logger.error(self.last_error)
            return

        self.running = True
        self.last_error = None

        # wechatauto / wxauto4 / wxautox4 回调监听模式
        if loaded_name in ("wechatauto", "wxauto4", "wxautox4"):
            def _make_callback(gname: str):
                def _cb(msg, chat):
                    try:
                        self._dispatch_message(gname, msg, chat, wx=wx)
                    except Exception as exc:
                        logger.warning("[%s] 处理消息异常: %s", gname, exc)
                return _cb

            for group in self.groups:
                try:
                    res = wx.AddListenChat(nickname=group, callback=_make_callback(group))
                    logger.info("已添加微信群监听 [%s]: %s（历史消息已自动过滤，仅响应此刻之后的新消息）", group, res)
                except Exception as exc:
                    logger.error("添加微信群监听 [%s] 失败: %s", group, exc)

            if hasattr(wx, "StartListening"):
                wx.StartListening()

            # 后台巡检循环：每 15 秒检查用户自定义定时任务，每 5 分钟检查自主冒泡与夜间记忆养护
            last_heartbeat = time.time()
            last_schedule_check = time.time()
            while not self._stop_event.is_set():
                time.sleep(1.0)
                now_loop = time.time()

                # 1. 每 15 秒检查到点的用户定时任务
                if (now_loop - last_schedule_check) >= 15:
                    last_schedule_check = now_loop
                    for task in scheduler.pop_due_tasks():
                        target_group = task.get("group_id")
                        if not target_group:
                            continue
                        try:
                            logger.info("⏰ 触发定时任务 #%s -> [%s]: %s", task.get("id"), target_group, task.get("content"))
                            t_resp = bot_core.execute_scheduled_task(task)
                            if t_resp and t_resp.should_reply:
                                for idx, b in enumerate(t_resp.get_bubbles()):
                                    wx.SendMsg(b, who=target_group)
                                    if idx < len(t_resp.get_bubbles()) - 1:
                                        time.sleep(0.15)
                                if (
                                    t_resp.sticker_path
                                    and t_resp.sticker_path.is_file()
                                    and t_resp.sticker_path.stat().st_size > 0
                                ):
                                    time.sleep(0.15)
                                    send_res = wx.SendFiles(str(t_resp.sticker_path.resolve()), who=target_group)
                                    logger.info("⏰ 定时任务发送附件 [%s] 结果: %s", t_resp.sticker_path.name, send_res)
                        except Exception as exc:
                            logger.warning("执行定时任务 #%s 异常: %s", task.get("id"), exc)

                # 2. 每 5 分钟检查自主冒泡 & 23:50 夜间四层记忆养护
                if (now_loop - last_heartbeat) >= 300:
                    last_heartbeat = now_loop
                    for group in self.groups:
                        try:
                            hb = bot_core.check_autonomous_heartbeat(group)
                            if hb and hb.should_reply:
                                logger.info("[%s] 大肥鱼自主冒泡: %s", group, hb.text[:80])
                                wx.SendMsg(hb.text, who=group)
                                if hb.sticker_path and hb.sticker_path.is_file():
                                    time.sleep(0.2)
                                    wx.SendFiles(str(hb.sticker_path.resolve()), who=group)
                        except Exception as exc:
                            logger.warning("[%s] 自主冒泡发送异常: %s", group, exc)

            if hasattr(wx, "StopListening"):
                wx.StopListening()
            self.running = False
            return

        # 兼容旧版 wxauto 3.9 轮询模式
        for group in self.groups:
            wx.AddListenChat(who=group, savepic=True)
            logger.info("已添加微信群监听(3.9): [%s]", group)

        while not self._stop_event.is_set():
            try:
                listen_msgs = wx.GetListenMessage()
                for chat, msgs in listen_msgs.items():
                    group_name = getattr(chat, "who", "未知群聊")
                    for msg in msgs:
                        self._dispatch_message(group_name, msg, chat, wx=wx)
            except Exception as exc:
                logger.warning("微信监听轮询异常: %s", exc)
            time.sleep(self.poll_interval)

        self.running = False

    def stop(self) -> None:
        self._stop_event.set()
