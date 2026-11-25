import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STICKERS_DIR = BASE_DIR / "stickers"
UPLOADS_DIR = DATA_DIR / "uploads"

DATA_DIR.mkdir(parents=True, exist_ok=True)
STICKERS_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# 加载 .env（如不存在则回退环境变量）
load_dotenv(BASE_DIR / ".env")


def _parse_bool(val: str | None, default: bool = True) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _parse_list(val: str | None, default: list[str]) -> list[str]:
    if not val:
        return default
    items = [x.strip() for x in val.split(",") if x.strip()]
    return items or default


class Settings:
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
    DEEPSEEK_BASE_URL: str = (
        os.getenv("BOT_DEEPSEEK_BASE_URL")
        or os.getenv("DEEPSEEK_BASE_URL")
        or "https://api.deepseek.com"
    ).strip()
    DEEPSEEK_CHAT_MODEL: str = os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-flash").strip()
    DEEPSEEK_VISION_MODEL: str = os.getenv("DEEPSEEK_VISION_MODEL", "deepseek-flash").strip()

    VISION_API_KEY: str = os.getenv("VISION_API_KEY", "").strip()
    VISION_BASE_URL: str = os.getenv("VISION_BASE_URL", "").strip()

    BOT_NAME: str = os.getenv("BOT_NAME", "大肥鱼").strip()
    DEFAULT_PERSONA: str = os.getenv("DEFAULT_PERSONA", "fengge").strip().lower() or "fengge"
    # 群聊唤醒词：L1 硬触发只认 @ 唤名（2026-11-25 收紧，原为 ["@大肥鱼","大肥鱼","肥鱼"]）
    # 如需恢复"不 @ 也叫得醒"，在 .env 里显式配置 TRIGGER_KEYWORDS=@大肥鱼,大肥鱼,肥鱼
    TRIGGER_KEYWORDS: list[str] = _parse_list(
        os.getenv("TRIGGER_KEYWORDS"), ["@" + BOT_NAME]
    )
    GROUP_AT_ONLY: bool = _parse_bool(os.getenv("GROUP_AT_ONLY"), True)
    AUTO_ROAST_IMAGE_PROB: float = float(os.getenv("AUTO_ROAST_IMAGE_PROB", "0.35"))
    STICKER_REPLY_PROB: float = float(os.getenv("STICKER_REPLY_PROB", "0.75"))

    # 自主意识与联网搜索配置
    ENABLE_WEB_SEARCH: bool = _parse_bool(os.getenv("ENABLE_WEB_SEARCH"), True)
    ZHIPU_API_KEY: str = os.getenv("ZHIPU_API_KEY", "").strip()
    BOCHA_API_KEY: str = os.getenv("BOCHA_API_KEY", "").strip()
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "").strip()
    SERPER_API_KEY: str = os.getenv("SERPER_API_KEY", "").strip()
    # L2/L2.5 判定去抖：群里连续发言时先挂起，等这一波说完（停顿）再判定
    ENABLE_TRIGGER_DEBOUNCE: bool = _parse_bool(os.getenv("ENABLE_TRIGGER_DEBOUNCE"), True)
    # 停顿多少秒算「这一波说完了」，之后才执行判定
    TRIGGER_DEBOUNCE_SEC: float = float(os.getenv("TRIGGER_DEBOUNCE_SEC", "1.5"))
    # 群里一直不停顿时的最大等待：从第一句挂起算起超过这个秒数就强制判定，防止永远排不上
    TRIGGER_DEBOUNCE_MAX_WAIT_SEC: float = float(os.getenv("TRIGGER_DEBOUNCE_MAX_WAIT_SEC", "12"))

    # L2.5 冷场重启话题的吐槽判定（有人隔了很久重新开口时，先轻量判断值不值得接一句）
    ENABLE_TOPIC_ROAST: bool = _parse_bool(os.getenv("ENABLE_TOPIC_ROAST"), True)
    # 距上一条群消息超过多少秒算「开新话题」，默认 10 分钟
    # 注意：这是 L2.5 唯一的频控，不再有同群冷却和每日上限（用户要求去掉）
    TOPIC_GAP_SEC: int = int(os.getenv("TOPIC_GAP_SEC", "600"))
    # 判定器认为「有意思」的最低把握度（0~1）。模型输出 0-10 分，低于该值不接话
    TOPIC_ROAST_MIN_SCORE: float = float(os.getenv("TOPIC_ROAST_MIN_SCORE", "0.7"))
    # 一波连续发言最多合并几句送给判定器（防止长串刷屏把 prompt 撑爆）
    TRIGGER_BURST_MAX_ITEMS: int = int(os.getenv("TRIGGER_BURST_MAX_ITEMS", "4"))

    ENABLE_PROACTIVE_CHIME: bool = _parse_bool(os.getenv("ENABLE_PROACTIVE_CHIME"), True)
    PROACTIVE_MSG_THRESHOLD: int = int(os.getenv("PROACTIVE_MSG_THRESHOLD", "6"))
    PROACTIVE_COOLDOWN_SEC: int = int(os.getenv("PROACTIVE_COOLDOWN_SEC", "480"))
    ENABLE_SCHEDULED_BUBBLE: bool = _parse_bool(os.getenv("ENABLE_SCHEDULED_BUBBLE"), True)
    QUIET_START_HOUR: int = int(os.getenv("QUIET_START_HOUR", "23"))
    QUIET_END_HOUR: int = int(os.getenv("QUIET_END_HOUR", "8"))

    # 常规桌面 Agent 电脑操作权限
    ENABLE_AGENT_TOOLS: bool = _parse_bool(os.getenv("ENABLE_AGENT_TOOLS"), True)
    # 允许操纵电脑的主人昵称（逗号分隔，留空表示专用电脑/虚拟机内不限制；在多人大群里建议填你自己的微信名）
    OWNER_NAMES: list[str] = [
        x.strip() for x in os.getenv("OWNER_NAMES", "").split(",") if x.strip()
    ]

    # Git 自动拉取更新与看门狗守护配置
    AUTO_GIT_UPDATE: bool = _parse_bool(os.getenv("AUTO_GIT_UPDATE"), True)
    AUTO_GIT_POLL_SEC: int = max(15, int(os.getenv("AUTO_GIT_POLL_SEC", "60")))

    # DeepSeek Harness (DSH) CLI 独立后台异步执行配置
    ENABLE_DSH: bool = _parse_bool(os.getenv("ENABLE_DSH"), True)
    DSH_BIN: str = os.getenv("DSH_BIN", "dsh").strip() or "dsh"
    DSH_MODE: str = os.getenv("DSH_MODE", "danger-full-access").strip() or "danger-full-access"
    DSH_APPROVAL_POLICY: str = os.getenv("DSH_APPROVAL_POLICY", "never").strip() or "never"
    DSH_MODEL: str = os.getenv("DSH_MODEL", "").strip()
    DSH_WORKDIR: str = os.getenv("DSH_WORKDIR", "").strip()
    DSH_TIMEOUT_SEC: int = max(60, int(os.getenv("DSH_TIMEOUT_SEC", "900")))

    # 高危操作群内二次确认保险
    ENABLE_DANGER_CONFIRM: bool = _parse_bool(os.getenv("ENABLE_DANGER_CONFIRM"), True)
    DANGER_CONFIRM_TTL_SEC: int = max(30, int(os.getenv("DANGER_CONFIRM_TTL_SEC", "180")))

    RUN_MODE: str = os.getenv("RUN_MODE", "web").strip().lower()
    WX_GROUP_WHITELIST: list[str] = _parse_list(
        os.getenv("WX_GROUP_WHITELIST"), ["大肥鱼测试群"]
    )
    # QQ 群白名单（填 QQ 群号，多个用英文逗号分隔；留空表示允许所有已加入的 QQ 群）
    QQ_GROUP_WHITELIST: list[str] = [
        x.strip() for x in os.getenv("QQ_GROUP_WHITELIST", "").split(",") if x.strip()
    ]
    QQ_ONEBOT_TOKEN: str = os.getenv("QQ_ONEBOT_TOKEN", "").strip()

    WEB_HOST: str = os.getenv("WEB_HOST", "127.0.0.1").strip()
    WEB_PORT: int = int(os.getenv("WEB_PORT", "8765"))

    @property
    def has_valid_api_key(self) -> bool:
        return bool(self.DEEPSEEK_API_KEY) and not self.DEEPSEEK_API_KEY.startswith("sk-你的")

    @property
    def effective_vision_api_key(self) -> str:
        return self.VISION_API_KEY or self.DEEPSEEK_API_KEY

    @property
    def effective_vision_base_url(self) -> str:
        return self.VISION_BASE_URL or self.DEEPSEEK_BASE_URL


settings = Settings()
