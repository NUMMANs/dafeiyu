import logging
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from bot_core import bot_core
from config import UPLOADS_DIR, settings
from dsh_bridge import format_job_delivery, pop_finished_jobs
from pet_engine import pet_engine
from scheduler import scheduler
from self_ops import check_remote_update, consume_restart_notice, git_pull_and_check, request_restart

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
        self._prestart_img_count: dict[str, int] = {}
        # 已经告警过的群名，避免每个巡检周期刷同一条日志
        self._warned_groups: set[str] = set()
        # 启动后真的收到过消息的群。用于判断群名是否正确：
        # 从没收到过消息的群，SendMsg 很可能因为「找不到会话」而漂移到当前打开的聊天窗口。
        self._seen_groups: set[str] = set()

    # ------------------------------------------------------------------
    # 群白名单：所有「主动发消息」都必须先过这一关
    # ------------------------------------------------------------------

    def _is_allowed_group(self, name: str) -> bool:
        """
        群名是否在配置白名单内（精确匹配）。

        为什么要这么严：wxauto 的 SendMsg(who=群名) 在某些版本里找不到目标会话时，
        会退化成「发到当前打开的聊天窗口」。白名单里只要有一个拼错/改名/乱码的群名，
        定时冒泡、自主问候、去抖接话就可能落到用户当时正在看的任意群或私聊。
        """
        n = (name or "").strip()
        if not n:
            return False
        return n in {(g or "").strip() for g in self.groups if (g or "").strip()}

    def _is_live_group(self, name: str) -> bool:
        """
        这个群启动后是否真的收到过消息。

        白名单只能保证「配置里有」，保证不了「微信里真的有」。
        群名写错/被改名时 AddListenChat 可能静默不报错，此后每次 SendMsg 都在赌版本行为。
        所以主动冒泡额外要求「这个群确实有过真实收发」，宁可少说一句，也不发错地方。
        """
        return (name or "").strip() in self._seen_groups

    def _reject_group(self, name: str, action: str) -> None:
        n = (name or "").strip() or "<空群名>"
        if n not in self._warned_groups:
            self._warned_groups.add(n)
            logger.warning(
                "⛔ 已忽略非白名单会话 [%s]（动作=%s）。当前白名单：%s；"
                "若这确实是目标群，请核对 WX_GROUP_WHITELIST 里的群名是否与微信里完全一致",
                n,
                action,
                self.groups,
            )

    def _safe_send(self, wx, text: str, who: str, action: str = "发送") -> bool:
        """带白名单校验的文本发送。返回是否真的发出去了。"""
        target = (who or "").strip()
        if not self._is_allowed_group(target):
            self._reject_group(target, action)
            return False
        try:
            wx.SendMsg(text, who=target)
            return True
        except Exception as exc:
            logger.warning("[%s] %s失败: %s", target, action, exc)
            return False

    def _safe_send_file(self, wx, path: str, who: str, action: str = "发送附件") -> bool:
        """带白名单校验的文件/图片发送。"""
        target = (who or "").strip()
        if not self._is_allowed_group(target):
            self._reject_group(target, action)
            return False
        try:
            wx.SendFiles(path, who=target)
            return True
        except Exception as exc:
            logger.warning("[%s] %s失败: %s", target, action, exc)
            return False

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

    def _parse_wechat_xml_message(self, raw: str) -> tuple[str, str]:
        """解析微信引用回复(type=57)与合并转发聊天记录(type=19) XML，返回 (可读文本, quoted_sender)。"""
        xml_str = (raw or "").strip()
        if not xml_str:
            return "", ""
        first_lt = xml_str.find("<")
        if first_lt > 0:
            xml_str = xml_str[first_lt:]
        try:
            root = ET.fromstring(xml_str)
        except Exception:
            return "", ""

        appmsg = root if root.tag == "appmsg" else root.find("appmsg")
        if appmsg is None:
            return "", ""

        msg_type_str = (appmsg.findtext("type") or "").strip()

        if msg_type_str == "57":
            title = (appmsg.findtext("title") or "").strip()
            refer = appmsg.find("refermsg")
            if refer is None:
                return title, ""
            ref_type = (refer.findtext("type") or "").strip()
            disp = (refer.findtext("displayname") or refer.findtext("chatusr") or "群友").strip()
            if ref_type == "3":
                return f"[引用了 {disp} 发的一张图片] {title}".strip(), disp
            if ref_type == "1":
                ref_text = self._clean_group_text(refer.findtext("content") or "")
                ref_preview = " ".join(ref_text.split())[:80]
                return f"[引用 {disp}：「{ref_preview}」] {title}".strip(), ""
            return f"[引用 {disp} 的消息] {title}".strip(), ""

        if msg_type_str == "19":
            title = (appmsg.findtext("title") or "聊天记录").strip()
            des = (appmsg.findtext("des") or "").strip()
            record_xml = (appmsg.findtext("recorditem") or "").strip()
            snippets: list[str] = []
            img_count = 0
            if record_xml.startswith("<"):
                try:
                    rec_root = ET.fromstring(record_xml)
                    for item in rec_root.findall(".//dataitem"):
                        dtype = (item.attrib.get("datatype") or "").strip()
                        src_name = (item.findtext("sourcename") or "").strip()
                        desc = (item.findtext("datadesc") or item.findtext("datatitle") or "").strip()
                        if dtype == "2":
                            img_count += 1
                            if len(snippets) < 6:
                                snippets.append(f"{src_name}: [图片]" if src_name else "[图片]")
                        elif desc and len(snippets) < 6:
                            clean_d = " ".join(desc.split())[:80]
                            snippets.append(f"{src_name}: {clean_d}" if src_name else clean_d)
                except Exception:
                    pass

            body = "\n".join(snippets) if snippets else des[:400]
            img_note = f"（含 {img_count} 张图片）" if img_count > 0 else ""
            header = f"[合并转发聊天记录：{title}{img_note}]"
            return (f"{header}\n{body}" if body else header)[:700], ""

        return "", ""

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

    def _extract_image_path(self, group_name: str, msg, chat, wx=None, quick: bool = False) -> Path | None:
        # 1. 优先用 wechatauto MediaDownloader 直接本地解密 DB 图片（先等高清/压缩版 full，避免拿到糊成马赛克的 _t 缩略图）
        local_id = getattr(msg, "local_id", None)
        if local_id is not None:
            downloader = self._get_media_downloader(chat, wx)
            if downloader is not None:
                user_key = getattr(chat, "_wxid", None) or group_name
                tiers = ("best",) if quick else ("full", "full", "full", "best")
                for attempt, tier in enumerate(tiers):
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
                    if not quick and attempt < len(tiers) - 1:
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
        # 白名单硬校验：不在配置里的会话一律不处理（不回复、不记历史、不冒泡）
        if not self._is_allowed_group(group_name):
            self._reject_group(group_name, "接收消息")
            return
        # 收到过消息 = 这个群名在微信里确实存在，可以放心主动发言
        self._seen_groups.add(group_name.strip())

        # 启动前就已存在的历史消息：不触发回复，但静默写入短期记忆，让大肥鱼刚启动就懂群里刚才在聊啥！
        create_time = getattr(msg, "create_time", None)
        if isinstance(create_time, (int, float)) and create_time > 0:
            if create_time < self._start_ts - 3:
                hist_sender = str(getattr(msg, "sender", "群友") or "群友")
                is_me = getattr(msg, "is_self", False) or str(getattr(msg, "attr", "")).lower() == "self"
                hist_type = str(getattr(msg, "type", "")).lower()
                hist_cls = msg.__class__.__name__.lower()
                if (hist_type == "image" or "image" in hist_cls) and not is_me:
                    cnt = self._prestart_img_count.get(group_name, 0)
                    if cnt < 3:
                        p = self._extract_image_path(group_name, msg, chat, wx=wx, quick=True)
                        if p is not None:
                            self._prestart_img_count[group_name] = cnt + 1
                            pet_engine.append_history(
                                group_id=group_name,
                                sender=hist_sender,
                                role="user",
                                content="",
                                image_path=str(p),
                            )
                    return

                raw_hist = self._clean_group_text(str(getattr(msg, "content", "") or ""))
                if raw_hist and self._is_non_text_xml(raw_hist):
                    raw_hist, _ = self._parse_wechat_xml_message(raw_hist)
                if raw_hist and not self._is_non_text_xml(raw_hist):
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
            quoted_sender = ""
            if content and self._is_non_text_xml(content):
                parsed_text, quoted_sender = self._parse_wechat_xml_message(content)
                if not parsed_text:
                    return
                content = parsed_text
            if not content or self._is_non_text_xml(content):
                return
            logger.info("[%s] 收到 %s: %s", group_name, sender, content[:80])
            resp = bot_core.handle_message(
                group_id=group_name,
                sender=sender,
                text=content,
                force_trigger=force_trigger,
                quoted_sender=quoted_sender,
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

            # 只保留「真的监听成功」的群：配置里拼错/已改名/乱码的群名会在这里被剔除，
            # 否则每 5 分钟的自主冒泡会一直拿它调 SendMsg，被 wxauto 退化发到当前打开的会话。
            listening: list[str] = []
            for group in self.groups:
                try:
                    res = wx.AddListenChat(nickname=group, callback=_make_callback(group))
                    if res is False:
                        raise RuntimeError("AddListenChat 返回 False（通常是找不到该群）")
                    listening.append(group)
                    logger.info("已添加微信群监听 [%s]: %s（历史消息已自动过滤，仅响应此刻之后的新消息）", group, res)
                except Exception as exc:
                    logger.error("添加微信群监听 [%s] 失败，已从冒泡名单剔除（请检查群名是否写错/被改名）: %s", group, exc)

            if listening:
                self.groups = listening
            else:
                # 一个都没监听成功：清空发言名单，宁可全程静默也不能让 SendMsg 落到别处
                self.groups = []
                logger.error("⚠️ 没有任何群监听成功，已禁止一切主动发言！请检查 WX_GROUP_WHITELIST 里的群名是否与微信里完全一致")
            logger.info("主动发言白名单已收窄为: %s", self.groups)

            if hasattr(wx, "StartListening"):
                wx.StartListening()

            notice = consume_restart_notice()
            if notice and notice.get("msg"):
                time.sleep(0.6)
                target_group = (notice.get("group_id") or "").strip()
                notify_targets = [target_group] if target_group else (list(self.groups[:1]) if self.groups else [])
                for tg in notify_targets:
                    if self._safe_send(wx, notice["msg"], tg, "发送重启通知"):
                        logger.info("📢 已向 [%s] 发送重启回归通知: %s", tg, notice["msg"][:80])

            # 后台巡检循环：DSH完工投递(5s)、定时任务(15s)、Git远程更新(AUTO_GIT_POLL_SEC)、自主冒泡与夜间养护(300s)
            last_heartbeat = time.time()
            last_schedule_check = time.time()
            last_git_check = time.time()
            last_dsh_check = 0.0
            while not self._stop_event.is_set():
                time.sleep(1.0)
                now_loop = time.time()

                # 0. 去抖队列：群里停顿够久就把挂起的 L2/L2.5 判定执行掉并发言（每秒一次）
                try:
                    for target_group, d_resp in bot_core.pop_due_triggers(now_loop):
                        if not d_resp.should_reply:
                            continue
                        if not self._is_allowed_group(target_group):
                            self._reject_group(target_group, "停顿后接话")
                            continue
                        logger.info("[%s] 停顿后接话: %s", target_group, (d_resp.text or "")[:80])
                        bubbles = d_resp.get_bubbles()
                        for idx, b in enumerate(bubbles):
                            self._safe_send(wx, b, target_group, "停顿后接话")
                            if idx < len(bubbles) - 1:
                                time.sleep(0.15)
                        if d_resp.sticker_path and d_resp.sticker_path.is_file() and d_resp.sticker_path.stat().st_size > 0:
                            time.sleep(0.15)
                            self._safe_send_file(wx, str(d_resp.sticker_path.resolve()), target_group)
                except Exception as exc:
                    logger.warning("去抖判定发送异常: %s", exc)

                # 1. 每 15 秒检查到点的用户定时任务
                if (now_loop - last_schedule_check) >= 15:
                    last_schedule_check = now_loop
                    for task in scheduler.pop_due_tasks():
                        target_group = task.get("group_id")
                        if not target_group:
                            continue
                        if not self._is_allowed_group(target_group):
                            # 任务可能是旧群名/改名前的群，宁可丢弃也不能发到别的会话
                            self._reject_group(target_group, f"定时任务 #{task.get('id')}")
                            continue
                        try:
                            logger.info("⏰ 触发定时任务 #%s -> [%s]: %s", task.get("id"), target_group, task.get("content"))
                            t_resp = bot_core.execute_scheduled_task(task)
                            if t_resp and t_resp.should_reply:
                                bubbles = t_resp.get_bubbles()
                                for idx, b in enumerate(bubbles):
                                    self._safe_send(wx, b, target_group, "定时任务")
                                    if idx < len(bubbles) - 1:
                                        time.sleep(0.15)
                                if (
                                    t_resp.sticker_path
                                    and t_resp.sticker_path.is_file()
                                    and t_resp.sticker_path.stat().st_size > 0
                                ):
                                    time.sleep(0.15)
                                    ok = self._safe_send_file(wx, str(t_resp.sticker_path.resolve()), target_group)
                                    logger.info("⏰ 定时任务发送附件 [%s] 结果: %s", t_resp.sticker_path.name, ok)
                        except Exception as exc:
                            logger.warning("执行定时任务 #%s 异常: %s", task.get("id"), exc)

                # 2. 每 5 分钟检查自主冒泡 & 23:50 夜间四层记忆养护
                if (now_loop - last_heartbeat) >= 300:
                    last_heartbeat = now_loop
                    for group in self.groups:
                        # 群名配错/被改名时 wxauto 可能不报错，但 SendMsg 会漂移到当前窗口。
                        # 启动后从没收到过这个群的消息，就不主动开口。
                        if not self._is_live_group(group):
                            if group not in self._warned_groups:
                                self._warned_groups.add(group)
                                logger.warning(
                                    "[%s] 启动后从未收到该群消息，暂不主动冒泡（请确认群名与微信里完全一致）",
                                    group,
                                )
                            continue
                        try:
                            hb = bot_core.check_autonomous_heartbeat(group)
                            if hb and hb.should_reply:
                                logger.info(
                                    "[%s] 自主动作: %s",
                                    group,
                                    (hb.text or (hb.file_path.name if hb.file_path else ""))[:80],
                                )
                                if hb.text and not self._safe_send(wx, hb.text, group, "自主冒泡"):
                                    continue
                                # 每日统计 txt 之类需要以文件形式发出的附件
                                if hb.file_path and hb.file_path.is_file():
                                    time.sleep(0.2)
                                    logger.info("[%s] 发送文件: %s", group, hb.file_path.name)
                                    self._safe_send_file(wx, str(hb.file_path.resolve()), group)
                                if hb.sticker_path and hb.sticker_path.is_file():
                                    time.sleep(0.2)
                                    self._safe_send_file(wx, str(hb.sticker_path.resolve()), group)
                        except Exception as exc:
                            logger.warning("[%s] 自主冒泡发送异常: %s", group, exc)

                # 3. 定期检测 GitHub 是否有新提交（推送即自动拉取 + 语法校验 + 重启）
                if settings.AUTO_GIT_UPDATE and (now_loop - last_git_check) >= settings.AUTO_GIT_POLL_SEC:
                    last_git_check = now_loop
                    try:
                        has_update, update_info = check_remote_update()
                        if has_update:
                            logger.info("🛰️ 检测到远程新提交: %s", update_info)
                            ok_pull, pull_detail = git_pull_and_check()
                            first_group = self.groups[0] if self.groups else ""
                            if ok_pull:
                                pre_notice = f"🛰️ 检测到 Git 新提交（{update_info}），代码校验通过，即将重启更新..."
                                if first_group:
                                    self._safe_send(wx, pre_notice, first_group, "发送更新预告")
                                request_restart(
                                    reason=f"GitHub 自动更新 ({update_info})",
                                    notify_group=first_group,
                                    delay_sec=2.0,
                                )
                                self._stop_event.set()
                            else:
                                logger.warning("自动拉取未通过检查: %s", pull_detail)
                                if first_group and "语法错误" in pull_detail:
                                    self._safe_send(
                                        wx,
                                        f"⚠️ 检测到 Git 新提交（{update_info}），但 {pull_detail}",
                                        first_group,
                                        "发送回滚告警",
                                    )
                    except Exception as exc:
                        logger.warning("自动检测 Git 更新异常: %s", exc)

                # 4. 每 5 秒检查并投递已完成的后台 DSH 任务结果
                if (now_loop - last_dsh_check) >= 5:
                    last_dsh_check = now_loop
                    try:
                        for done_job in pop_finished_jobs():
                            target_group = (done_job.get("group_id") or "").strip() or (
                                self.groups[0] if self.groups else ""
                            )
                            if not target_group:
                                continue
                            msg_text, attach_path = format_job_delivery(done_job)
                            if not self._safe_send(wx, msg_text, target_group, "DSH 任务投递"):
                                continue
                            logger.info("🤖 已投递 DSH 任务 #%s 结果到 [%s]", done_job.get("job_id"), target_group)
                            if (
                                attach_path is not None
                                and attach_path.is_file()
                                and attach_path.stat().st_size > 0
                            ):
                                time.sleep(0.2)
                                self._safe_send_file(wx, str(attach_path.resolve()), target_group)
                    except Exception as exc:
                        logger.warning("检查并投递 DSH 后台任务异常: %s", exc)

            if hasattr(wx, "StopListening"):
                wx.StopListening()
            self.running = False
            return

        # 兼容旧版 wxauto 3.9 轮询模式
        listening: list[str] = []
        for group in self.groups:
            try:
                wx.AddListenChat(who=group, savepic=True)
                listening.append(group)
                logger.info("已添加微信群监听(3.9): [%s]", group)
            except Exception as exc:
                logger.error("添加微信群监听(3.9) [%s] 失败，已从冒泡名单剔除: %s", group, exc)
        if listening:
            self.groups = listening
        else:
            self.groups = []
            logger.error("⚠️ 没有任何群监听成功，已禁止一切主动发言！请检查 WX_GROUP_WHITELIST 里的群名是否与微信里完全一致")
        logger.info("主动发言白名单已收窄为: %s", self.groups)

        while not self._stop_event.is_set():
            try:
                # 去抖队列：停顿够久就把挂起的 L2/L2.5 判定执行掉并发言
                now_poll = time.time()
                for target_group, d_resp in bot_core.pop_due_triggers(now_poll):
                    if not d_resp.should_reply:
                        continue
                    if not self._is_allowed_group(target_group):
                        self._reject_group(target_group, "停顿后接话")
                        continue
                    logger.info("[%s] 停顿后接话: %s", target_group, (d_resp.text or "")[:80])
                    bubbles = d_resp.get_bubbles()
                    for idx, b in enumerate(bubbles):
                        self._safe_send(wx, b, target_group, "停顿后接话")
                        if idx < len(bubbles) - 1:
                            time.sleep(0.15)
                    if d_resp.sticker_path and d_resp.sticker_path.is_file() and d_resp.sticker_path.stat().st_size > 0:
                        time.sleep(0.15)
                        self._safe_send_file(wx, str(d_resp.sticker_path.resolve()), target_group)

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
