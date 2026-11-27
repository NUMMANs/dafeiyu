import argparse
import logging
import logging.handlers
import threading

import uvicorn

from adapters.ilink_adapter import ILinkAdapter
from adapters.wxauto_adapter import WxAutoAdapter
from config import BASE_DIR, settings
from self_ops import PORT_BUSY_EXIT_CODE, is_port_in_use
from web_server import app

# 日志同时落盘：从 start_wechat.bat 启动时控制台一关就什么都没有，
# 想回头查「那句话到底怎么发出去的」只能翻数据库。轮转 5MB × 3 份够用。
_LOG_DIR = BASE_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            _LOG_DIR / "bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
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

    # 先于任何 adapter 线程检查端口：若已经被别的大肥鱼实例占着，就别抢，
    # 用专用退出码告诉看门狗「这是环境冲突，不是代码崩了，别 reset --hard」。
    if mode in ("web", "qq", "both") and is_port_in_use(settings.WEB_HOST, settings.WEB_PORT):
        logger.error(
            "🌐 端口 %s:%d 已被占用（多半是另一个大肥鱼实例还在跑），本进程不抢占，"
            "直接退出 (exit=%d)。",
            settings.WEB_HOST,
            settings.WEB_PORT,
            PORT_BUSY_EXIT_CODE,
        )
        raise SystemExit(PORT_BUSY_EXIT_CODE)

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
    try:
        uvicorn.run(app, host=settings.WEB_HOST, port=settings.WEB_PORT, log_level="info")
    except SystemExit as exc:
        # uvicorn 绑定失败时会自己打印 10048 并 sys.exit(3)，异常传不到外层。
        # 这时若端口确实被占，翻译成专用退出码，别让看门狗当成代码崩溃去回滚。
        if exc.code == 3 and is_port_in_use(settings.WEB_HOST, settings.WEB_PORT):
            logger.error(
                "🌐 uvicorn 绑定 %s:%d 失败（端口被占用），按环境冲突退出 (exit=%d)。",
                settings.WEB_HOST,
                settings.WEB_PORT,
                PORT_BUSY_EXIT_CODE,
            )
            raise SystemExit(PORT_BUSY_EXIT_CODE) from exc
        raise


if __name__ == "__main__":
    main()
