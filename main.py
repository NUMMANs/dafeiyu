import argparse
import logging
import threading

import uvicorn

from adapters.ilink_adapter import ILinkAdapter
from adapters.wxauto_adapter import WxAutoAdapter
from config import settings
from web_server import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dafeiyu.main")


def main() -> None:
    parser = argparse.ArgumentParser(description="大肥鱼 (DeepSeek) 微信群养成机器人")
    parser.add_argument(
        "--mode",
        choices=["web", "qq", "wxauto", "both", "ilink"],
        default=settings.RUN_MODE,
        help="运行模式: web(本地控制台) | qq(QQ NapCat OneBot11 + 控制台) | wxauto(Windows PC微信群) | both(微信+控制台) | ilink(微信官方扫码私聊)",
    )
    args = parser.parse_args()
    mode = args.mode

    logger.info("🐋 大肥鱼启动中... 运行模式: %s | DeepSeek API 已配置: %s", mode, settings.has_valid_api_key)

    if mode == "ilink":
        ILinkAdapter().run_forever()
        return

    if mode == "wxauto":
        WxAutoAdapter().run_forever()
        return

    if mode == "both":
        wx_adapter = WxAutoAdapter()
        t = threading.Thread(target=wx_adapter.run_forever, daemon=True, name="wxauto-thread")
        t.start()
        logger.info("已后台拉起 Windows PC 微信群监听线程，监听群聊: %s", settings.WX_GROUP_WHITELIST)

    logger.info("🌐 本地养鱼控制台地址: http://%s:%d", settings.WEB_HOST, settings.WEB_PORT)
    logger.info("🐧 QQ NapCat OneBot 11 反向 WS 地址: ws://%s:%d/onebot/ws", settings.WEB_HOST, settings.WEB_PORT)
    uvicorn.run(app, host=settings.WEB_HOST, port=settings.WEB_PORT, log_level="info")


if __name__ == "__main__":
    main()
