import logging
import tempfile
from pathlib import Path

from bot_core import bot_core

logger = logging.getLogger("dafeiyu.ilink")


class ILinkAdapter:
    """
    微信官方 iLink 协议适配器（基于 weixin-ilink）。
    说明：官方 iLink 协议扫码即用、风控极低，适合 1v1 私聊养大肥鱼；
    如需在个人微信群里运行，请使用 wxauto_adapter。
    """

    def run_forever(self) -> None:
        try:
            from weixin_ilink import WeChatBot
        except ImportError:
            logger.error('未安装 weixin-ilink，请先运行: pip install "weixin-ilink[qr]"')
            return

        bot = WeChatBot()

        @bot.on_message
        def handle_msg(msg):
            sender = getattr(msg, "sender", "微信好友")
            text = getattr(msg, "text", "") if getattr(msg, "is_text", False) else ""
            image_path = None

            if getattr(msg, "is_image", False):
                try:
                    raw_bytes = msg.download_image()
                    tmp = Path(tempfile.gettempdir()) / f"dafeiyu_ilink_{id(msg)}.png"
                    tmp.write_bytes(raw_bytes)
                    image_path = tmp
                except Exception as exc:
                    logger.warning("下载 iLink 图片失败: %s", exc)

            resp = bot_core.handle_message(
                group_id=f"ilink:{sender}",
                sender=str(sender),
                text=str(text or ""),
                image_path=image_path,
                force_trigger=True,
            )
            if resp.should_reply:
                if resp.text:
                    bot.reply(msg, resp.text)
                if resp.sticker_path and resp.sticker_path.is_file() and hasattr(bot, "reply_image"):
                    try:
                        bot.reply_image(msg, str(resp.sticker_path.resolve()))
                    except Exception as exc:
                        logger.warning("iLink 发送表情包失败: %s", exc)

        logger.info("正在启动微信 iLink 扫码登录...")
        bot.run()
