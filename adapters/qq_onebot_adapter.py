import asyncio
import base64
import json
import logging
import re
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from bot_core import bot_core
from config import UPLOADS_DIR, settings

logger = logging.getLogger("dafeiyu.qq")

CQ_IMAGE_RE = re.compile(r"\[CQ:image,[^\]]*url=([^,\]]+)[^\]]*\]")
CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)[^\]]*\]")
CQ_ANY_RE = re.compile(r"\[CQ:[^\]]+\]")


def _download_image_sync(url: str) -> Path | None:
    try:
        clean_url = url.replace("&amp;", "&").strip()
        req = urllib.request.Request(
            clean_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            ext = ".jpg"
            if "png" in ctype:
                ext = ".png"
            elif "gif" in ctype:
                ext = ".gif"
            elif "webp" in ctype:
                ext = ".webp"
            out = UPLOADS_DIR / f"qq_{uuid.uuid4().hex}{ext}"
            out.write_bytes(data)
            return out
    except Exception as exc:
        logger.warning("下载 QQ 图片失败 (%s): %s", url[:80], exc)
        return None


def _file_to_base64_uri(path: Path) -> str:
    raw = path.read_bytes()
    return "base64://" + base64.b64encode(raw).decode("ascii")


def _parse_onebot_message(event: dict[str, Any]) -> tuple[str, list[str], bool]:
    """
    解析 OneBot 11 消息（兼容数组段格式和 CQ 码字符串格式）。
    返回: (纯文本内容, 图片URL列表, 是否被 @ 了机器人自己)
    """
    self_id = str(event.get("self_id", ""))
    message = event.get("message")
    texts: list[str] = []
    image_urls: list[str] = []
    at_me = False

    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            data = seg.get("data") or {}
            if seg_type == "text":
                t = str(data.get("text", ""))
                if t:
                    texts.append(t)
            elif seg_type == "at":
                if str(data.get("qq", "")) == self_id:
                    at_me = True
            elif seg_type == "image":
                url = data.get("url") or data.get("file")
                if url and str(url).startswith(("http://", "https://")):
                    image_urls.append(str(url))
    else:
        raw = str(message or event.get("raw_message") or "")
        for m in CQ_AT_RE.finditer(raw):
            if m.group(1) == self_id:
                at_me = True
        for m in CQ_IMAGE_RE.finditer(raw):
            image_urls.append(m.group(1))
        cleaned = CQ_ANY_RE.sub("", raw).strip()
        if cleaned:
            texts.append(cleaned)

    return "".join(texts).strip(), image_urls, at_me


def _build_reply_segments(
    reply_text: str,
    sticker_path: Path | None,
    at_user_id: int | str | None = None,
) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    if at_user_id is not None:
        segs.append({"type": "at", "data": {"qq": str(at_user_id)}})
        segs.append({"type": "text", "data": {"text": " "}})
    if reply_text:
        segs.append({"type": "text", "data": {"text": reply_text}})
    if sticker_path and sticker_path.is_file():
        segs.append({"type": "image", "data": {"file": _file_to_base64_uri(sticker_path)}})
    return segs


async def handle_onebot_ws(websocket: WebSocket) -> None:
    """
    OneBot 11 反向 WebSocket 入口（供 NapCat / LLOneBot 连接）。
    NapCat WebUI -> 网络配置 -> 新建「WebSocket 客户端」-> URL 填:
        ws://127.0.0.1:8765/onebot/ws
    """
    if settings.QQ_ONEBOT_TOKEN:
        auth = websocket.headers.get("authorization", "")
        token_q = websocket.query_params.get("access_token", "")
        expected = f"Bearer {settings.QQ_ONEBOT_TOKEN}"
        if auth != expected and token_q != settings.QQ_ONEBOT_TOKEN:
            await websocket.close(code=1008, reason="Invalid OneBot token")
            return

    await websocket.accept()
    logger.info("🐧 NapCat / OneBot 11 QQ 通道已连接！")

    try:
        while True:
            raw_msg = await websocket.receive_text()
            try:
                event = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

            if event.get("post_type") != "message":
                continue

            msg_type = event.get("message_type")  # "group" 或 "private"
            user_id = event.get("user_id")
            self_id = event.get("self_id")
            if user_id == self_id:
                continue

            sender_info = event.get("sender") or {}
            sender_name = (
                sender_info.get("card")
                or sender_info.get("nickname")
                or str(user_id)
            )
            text, image_urls, at_me = _parse_onebot_message(event)

            # 群聊白名单检查
            if msg_type == "group":
                group_id = str(event.get("group_id", ""))
                if settings.QQ_GROUP_WHITELIST and group_id not in settings.QQ_GROUP_WHITELIST:
                    continue
                room_id = f"qq_group:{group_id}"
                force_trigger = at_me
            elif msg_type == "private":
                room_id = f"qq_private:{user_id}"
                force_trigger = True
            else:
                continue

            # 下载第一张图片（如有）
            image_path: Path | None = None
            if image_urls:
                image_path = await asyncio.to_thread(_download_image_sync, image_urls[0])

            if not text and image_path is None:
                continue

            logger.info("[%s] 收到 %s: %s (图片=%s, @我=%s)", room_id, sender_name, text[:60], bool(image_path), at_me)

            # 在线程池执行大肥鱼核心逻辑（避免阻塞 WebSocket 心跳）
            resp = await asyncio.to_thread(
                bot_core.handle_message,
                room_id,
                str(sender_name),
                text,
                image_path,
                force_trigger,
            )

            if not resp.should_reply:
                continue

            bubbles = resp.get_bubbles()
            for idx, bubble in enumerate(bubbles):
                is_last = idx == len(bubbles) - 1
                sticker = resp.sticker_path if is_last else None
                at_uid = user_id if (msg_type == "group" and idx == 0 and at_me) else None
                segments = _build_reply_segments(bubble, sticker, at_user_id=at_uid)
                if msg_type == "group":
                    action = {
                        "action": "send_group_msg",
                        "params": {
                            "group_id": int(event["group_id"]),
                            "message": segments,
                        },
                        "echo": f"dafeiyu_{uuid.uuid4().hex[:8]}",
                    }
                else:
                    action = {
                        "action": "send_private_msg",
                        "params": {
                            "user_id": int(user_id),
                            "message": segments,
                        },
                        "echo": f"dafeiyu_{uuid.uuid4().hex[:8]}",
                    }
                await websocket.send_text(json.dumps(action, ensure_ascii=False))
                if not is_last:
                    await asyncio.sleep(0.15)
            logger.info("[%s] 已分段回复 %s (%d条): %s", room_id, sender_name, len(bubbles), resp.text[:60])

    except WebSocketDisconnect:
        logger.info("🐧 NapCat / OneBot 11 QQ 通道已断开")
    except Exception as exc:
        logger.warning("QQ OneBot WebSocket 异常: %s", exc)
