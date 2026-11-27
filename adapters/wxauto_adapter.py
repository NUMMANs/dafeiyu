import logging
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from bot_core import bot_core
from config import BASE_DIR, UPLOADS_DIR, settings
import gitmeta
from dsh_bridge import (
    build_job_report_file,
    collect_artifacts,
    format_job_delivery,
    pop_finished_jobs,
)
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
            # 深夜不播报重启：夜间人格复盘 0:30 会推一次代码，自动更新随即拉取重启，
            # 凌晨往群里甩一句「鱼重启回来啦」比不说话还假。白天照旧。
            if notice and notice.get("msg"):
                _hour = datetime.now().hour
                if _hour >= 23 or _hour < settings.QUIET_END_HOUR:
                    logger.info("🌙 深夜启动，跳过重启播报: %s", notice["msg"][:40])
                    notice = None
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
            # 启动时跑在哪个分支/提交上。之后只要它变了，就说明有人改过工作区。
            run_branch = gitmeta.branch(BASE_DIR)
            run_commit = gitmeta.commit(BASE_DIR)
            last_branch_check = time.time()
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

                # 0.5 冷场找话题：后台线程搜好的分享取回来发送（搜索不阻塞这个 1 秒巡检）
                try:
                    for t_group, t_resp in bot_core.poll_topic_share():
                        if not t_resp.should_reply:
                            continue
                        if not self._is_allowed_group(t_group):
                            self._reject_group(t_group, "冷场找话题")
                            continue
                        logger.info("[%s] 🌐 冷场分享: %s", t_group, (t_resp.text or "")[:80])
                        bubbles = t_resp.get_bubbles()
                        for idx, b in enumerate(bubbles):
                            self._safe_send(wx, b, t_group, "冷场找话题")
                            if idx < len(bubbles) - 1:
                                time.sleep(0.15)
                        if (
                            t_resp.sticker_path
                            and t_resp.sticker_path.is_file()
                            and t_resp.sticker_path.stat().st_size > 0
                        ):
                            time.sleep(0.15)
                            self._safe_send_file(wx, str(t_resp.sticker_path.resolve()), t_group)
                except Exception as exc:
                    logger.warning("冷场找话题发送异常: %s", exc)

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

                # 2.9 分支守护：有人在本地 checkout 了别的分支 → 按新分支的代码重启。
                # 只提交了热加载文件（roles/judges/*.md）则不重启，否则夜间热调每提交一次
                # 就要把机器人重启一遍。独立于 AUTO_GIT_UPDATE：那个管去远程拉更新，
                # 这个管本地工作区换了分支，所以关掉自动拉取也照样能切分支。
                if settings.AUTO_BRANCH_WATCH and (now_loop - last_branch_check) >= settings.AUTO_GIT_POLL_SEC:
                    last_branch_check = now_loop
                    try:
                        branch_now = gitmeta.branch(BASE_DIR)
                        if branch_now and run_branch and branch_now != run_branch:
                            logger.info("🪾 检测到分支切换: %s → %s，按新分支代码重启", run_branch, branch_now)
                            first_group = self.groups[0] if self.groups else ""
                            if first_group:
                                self._safe_send(
                                    wx,
                                    f"🪾 已切到分支 {branch_now}，正在按该分支的代码重启...",
                                    first_group,
                                    "发送分支切换预告",
                                )
                            request_restart(
                                reason=f"分支切换 {run_branch} → {branch_now}",
                                notify_group=first_group,
                                delay_sec=2.0,
                            )
                            self._stop_event.set()
                            continue
                        commit_now = gitmeta.commit(BASE_DIR)
                        if commit_now and run_commit and commit_now != run_commit:
                            if gitmeta.touches_code(BASE_DIR, run_commit, commit_now):
                                logger.info(
                                    "🪾 本地代码更新 %s → %s（含 .py），重启生效",
                                    run_commit[:7], commit_now[:7],
                                )
                                first_group = self.groups[0] if self.groups else ""
                                request_restart(
                                    reason=f"本地代码更新 ({commit_now[:7]})",
                                    notify_group=first_group,
                                    delay_sec=2.0,
                                )
                                self._stop_event.set()
                                continue
                            # 只动了热加载文件（判定规则/文档），下一句就生效，不必重启
                            logger.info(
                                "🪾 热加载文件已更新 %s → %s，无需重启",
                                run_commit[:7], commit_now[:7],
                            )
                            run_commit = commit_now
                    except Exception as exc:
                        logger.warning("分支守护异常: %s", exc)

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
                            # 链接理解任务是静默的：结果只走 L3 注文，不回占位、不生成群内报告、
                            # 不让大肥鱼播报「我读了个链接」。现场仍留在 job 目录备查。
                            if str(done_job.get("kind") or "").strip().lower() == "link":
                                try:
                                    bot_core.on_link_job_done(str(done_job.get("group_id") or ""))
                                except Exception:
                                    pass
                                logger.info(
                                    "🔗 链接理解任务 #%s 已静默完成（注入 %s 条｜状态 %s）",
                                    done_job.get("job_id"),
                                    done_job.get("l3_inject") or 0,
                                    done_job.get("status"),
                                )
                                continue
                            target_group = (done_job.get("group_id") or "").strip() or (
                                self.groups[0] if self.groups else ""
                            )
                            if not target_group:
                                continue

                            # 1) 先把「机械信息」准备好：产出文件清单 + 完整报告 txt（都不经过模型）
                            artifacts: list[Path] = []
                            try:
                                artifacts = collect_artifacts(
                                    done_job,
                                    limit=settings.DSH_ARTIFACT_MAX,
                                    max_mb=settings.DSH_ARTIFACT_MAX_MB,
                                )
                            except Exception as art_exc:
                                logger.warning("收集 DSH 产出文件失败: %s", art_exc)
                            report_path: Path | None = None
                            try:
                                report_path = build_job_report_file(done_job, artifacts)
                            except Exception as rep_exc:
                                logger.warning("生成 DSH 机械汇报失败: %s", rep_exc)
                            done_job["_artifacts"] = [str(p) for p in artifacts]
                            done_job["_report_path"] = str(report_path) if report_path else ""

                            # 2) 聊天部分：让大肥鱼用自己的口吻播报；失败就退回机械汇报文本
                            spoken = ""
                            if settings.DSH_REPORT_BY_BOT:
                                try:
                                    resp = bot_core.report_job_result(target_group, done_job)
                                    spoken = (resp.text or "").strip() if resp else ""
                                except Exception as report_exc:
                                    logger.warning("DSH 结果播报异常: %s", report_exc)
                            mechanical_text, _legacy_attach = format_job_delivery(done_job)
                            if not spoken:
                                spoken = mechanical_text
                            # 写权限护栏有结论就带一句：越界改动已经还原，别让它悄悄过去
                            guard_info = (
                                done_job.get("guard") if isinstance(done_job.get("guard"), dict) else {}
                            )
                            guard_violations = guard_info.get("violations") or []
                            if guard_violations:
                                restored = int(guard_info.get("restored") or 0)
                                handled = (
                                    "已经按干活前的内容还原了"
                                    if restored >= len(guard_violations)
                                    else f"还原了 {restored} 个，其余的记在报告里等你看"
                                )
                                spoken += (
                                    f"\n\n（另外：这次干活越界动了项目里 {len(guard_violations)} 个文件，"
                                    f"{handled}。）"
                                )
                            if not self._safe_send(wx, spoken, target_group, "DSH 任务投递"):
                                continue
                            logger.info(
                                "🤖 已投递 DSH 任务 #%s 结果到 [%s]（播报 %d 字｜产出文件 %d 个｜报告 %s）",
                                done_job.get("job_id"),
                                target_group,
                                len(spoken),
                                len(artifacts),
                                report_path.name if report_path else "无",
                            )

                            # 3) 附件：机械汇报 txt 优先，然后是 DSH 真正产出的文件。
                            #    这些都是原样上传，不经过 LLM，避免模型把路径/数字说错。
                            if report_path is not None and report_path.is_file() and report_path.stat().st_size > 0:
                                time.sleep(0.3)
                                self._safe_send_file(wx, str(report_path.resolve()), target_group, "任务报告")
                            for art in artifacts:
                                if not art.is_file() or art.stat().st_size <= 0:
                                    continue
                                time.sleep(0.3)
                                self._safe_send_file(wx, str(art.resolve()), target_group, "任务产出文件")

                            # 4) 记一笔「这个群刚干完什么活」：之后半小时内大肥鱼每轮回复都会带上
                            #    归属提示——活是她自己在后台干的，不是群友干的。缺了这条，模型会把
                            #    「整理 249 条消息」当成群友当天的经历，别人一喊累就接错人。
                            try:
                                bot_core.note_job_delivered(target_group, done_job, spoken)
                            except Exception as note_exc:
                                logger.warning("记录 DSH 任务完成状态失败: %s", note_exc)

                            # 5) 补写 L3：大肥鱼下次得知道自己干过这件事，群友追问才能接上。
                            #    记两条——原始结果（事实，含路径数字）和她当时说的话（口吻）。
                            #    归属标记必须放开头：L3 注入时每条只留 260 字（_clean_l3_content），
                            #    写在尾巴上的话必然被截掉，等于没写。
                            try:
                                summary_txt = str(done_job.get("summary") or "").strip()
                                task_txt = str(done_job.get("task") or "").strip()[:80]
                                pet_engine.append_history(
                                    group_id=target_group,
                                    sender=settings.BOT_NAME,
                                    role="assistant",
                                    content=(
                                        f"[我的后台任务·这活是我自己干的，不是群友干的] {task_txt} → {summary_txt}"
                                    )[:800],
                                )
                                if spoken != mechanical_text:
                                    pet_engine.append_history(
                                        group_id=target_group,
                                        sender=settings.BOT_NAME,
                                        role="assistant",
                                        content=spoken[:600],
                                    )
                            except Exception as hist_exc:
                                logger.warning("DSH 结果写入 L3 失败: %s", hist_exc)
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
