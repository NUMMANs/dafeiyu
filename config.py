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
    TRIGGER_KEYWORDS: list[str] = _parse_list(
        os.getenv("TRIGGER_KEYWORDS"), ["@" + BOT_NAME]
    )
    GROUP_AT_ONLY: bool = _parse_bool(os.getenv("GROUP_AT_ONLY"), True)
    # 单发图片无文字时偶尔主动锐评的概率；Grok 模式默认关闭装饰性萌宠表情包（如需开启设 ENABLE_GROK_STICKERS=true）
    AUTO_ROAST_IMAGE_PROB: float = float(os.getenv("AUTO_ROAST_IMAGE_PROB", "0.25"))
    ENABLE_GROK_STICKERS: bool = _parse_bool(os.getenv("ENABLE_GROK_STICKERS"), False)
    STICKER_REPLY_PROB: float = float(os.getenv("STICKER_REPLY_PROB", "0.0"))

    # 联网核查与搜索引擎配置
    ENABLE_WEB_SEARCH: bool = _parse_bool(os.getenv("ENABLE_WEB_SEARCH"), True)
    ZHIPU_API_KEY: str = os.getenv("ZHIPU_API_KEY", "").strip()
    BOCHA_API_KEY: str = os.getenv("BOCHA_API_KEY", "").strip()
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "").strip()
    SERPER_API_KEY: str = os.getenv("SERPER_API_KEY", "").strip()

    # 半主动唤醒（L2 上下文追认 + L2.5 兴趣点锐评 + HeartFlow 群聊热闹补刀）
    ENABLE_TRIGGER_DEBOUNCE: bool = _parse_bool(os.getenv("ENABLE_TRIGGER_DEBOUNCE"), True)
    TRIGGER_DEBOUNCE_SEC: float = float(os.getenv("TRIGGER_DEBOUNCE_SEC", "4.0"))
    TRIGGER_DEBOUNCE_MAX_WAIT_SEC: float = float(os.getenv("TRIGGER_DEBOUNCE_MAX_WAIT_SEC", "12"))
    TRIGGER_BURST_MAX_ITEMS: int = int(os.getenv("TRIGGER_BURST_MAX_ITEMS", "5"))

    FOLLOWUP_WINDOW_SEC: int = max(30, int(os.getenv("FOLLOWUP_WINDOW_SEC", "300")))
    ENABLE_TOPIC_INTEREST: bool = _parse_bool(
        os.getenv("ENABLE_TOPIC_INTEREST", os.getenv("ENABLE_TOPIC_ROAST")), True
    )
    TOPIC_GAP_SEC: int = int(os.getenv("TOPIC_GAP_SEC", "600"))
    TOPIC_INTEREST_MIN_SCORE: float = float(
        os.getenv("TOPIC_INTEREST_MIN_SCORE", os.getenv("TOPIC_ROAST_MIN_SCORE") or "0.75")
    )

    ENABLE_PROACTIVE_CHIME: bool = _parse_bool(os.getenv("ENABLE_PROACTIVE_CHIME"), True)
    PROACTIVE_COOLDOWN_SEC: int = int(os.getenv("PROACTIVE_COOLDOWN_SEC", "600"))

    # 夜间静默期（默认凌晨 1:00 ~ 8:00 不做半主动插嘴，被 @ 仍正常响应）
    QUIET_START_HOUR: int = int(os.getenv("QUIET_START_HOUR", "1"))
    QUIET_END_HOUR: int = int(os.getenv("QUIET_END_HOUR", "8"))

    # 轻量群友记忆与会话历史
    MEMORY_DIARY_INJECT: bool = _parse_bool(os.getenv("MEMORY_DIARY_INJECT"), False)
    MEMORY_NIGHTLY_EXTRACT: bool = _parse_bool(os.getenv("MEMORY_NIGHTLY_EXTRACT"), True)
    NIGHTLY_FACT_MAX: int = max(1, int(os.getenv("NIGHTLY_FACT_MAX", "6")))
    NIGHTLY_EXTRACT_CHUNK: int = max(20, int(os.getenv("NIGHTLY_EXTRACT_CHUNK", "60")))
    NIGHTLY_EXTRACT_MAX_CHUNKS: int = max(1, int(os.getenv("NIGHTLY_EXTRACT_MAX_CHUNKS", "10")))
    NIGHTLY_DIARY_LINES: int = max(20, int(os.getenv("NIGHTLY_DIARY_LINES", "80")))
    NIGHTLY_MATERIAL_MAX: int = max(100, int(os.getenv("NIGHTLY_MATERIAL_MAX", "1200")))

    L3_INJECT_ALL: bool = _parse_bool(os.getenv("L3_INJECT_ALL"), True)
    L3_INJECT_MAX_TOKENS: int = max(2_000, int(os.getenv("L3_INJECT_MAX_TOKENS", "90000")))
    L3_HISTORY_LIMIT: int = max(0, int(os.getenv("L3_HISTORY_LIMIT", "25")))
    L3_CONTEXT_LIMIT_TOKENS: int = max(10_000, int(os.getenv("L3_CONTEXT_LIMIT_TOKENS", "300000")))
    L3_KEEP_RECENT: int = max(20, int(os.getenv("L3_KEEP_RECENT", "120")))
    L3_COMPRESS_CHUNK: int = max(20, int(os.getenv("L3_COMPRESS_CHUNK", "200")))

    # 主人白名单与 Git 热更守护
    ENABLE_AGENT_TOOLS: bool = _parse_bool(os.getenv("ENABLE_AGENT_TOOLS"), True)
    OWNER_NAMES: list[str] = [
        x.strip() for x in os.getenv("OWNER_NAMES", "").split(",") if x.strip()
    ]
    AUTO_GIT_UPDATE: bool = _parse_bool(os.getenv("AUTO_GIT_UPDATE"), True)
    AUTO_GIT_POLL_SEC: int = max(15, int(os.getenv("AUTO_GIT_POLL_SEC", "60")))
    # 分支守护：有人在本地切了分支 / HEAD 变了，就按新代码重启。
    # 和 AUTO_GIT_UPDATE 分开：那个管「去远程拉更新」，这个管「本地工作区换了分支」，
    # 所以关掉自动拉取也照样能切分支。
    AUTO_BRANCH_WATCH: bool = _parse_bool(os.getenv("AUTO_BRANCH_WATCH"), True)

    # 后台工兵（DSH / CodeBuddy 沙箱执行）
    ENABLE_DSH: bool = _parse_bool(os.getenv("ENABLE_DSH"), True)
    DSH_BIN: str = os.getenv("DSH_BIN", "dsh").strip() or "dsh"
    DSH_MODE: str = os.getenv("DSH_MODE", "danger-full-access").strip() or "danger-full-access"
    DSH_APPROVAL_POLICY: str = os.getenv("DSH_APPROVAL_POLICY", "never").strip() or "never"
    DSH_MODEL: str = os.getenv("DSH_MODEL", "").strip()
    DSH_WORKDIR: str = os.getenv("DSH_WORKDIR", "").strip()
    DSH_TIMEOUT_SEC: int = max(60, int(os.getenv("DSH_TIMEOUT_SEC", "900")))

    ENABLE_TOOL_ROUTER: bool = _parse_bool(os.getenv("ENABLE_TOOL_ROUTER"), True)
    TOOL_ROUTER_MIN_CHARS: int = max(2, int(os.getenv("TOOL_ROUTER_MIN_CHARS", "8")))
    TOOLS_ALL_TO_DSH: bool = _parse_bool(os.getenv("TOOLS_ALL_TO_DSH"), True)
    DSH_REPORT_BY_BOT: bool = _parse_bool(os.getenv("DSH_REPORT_BY_BOT"), True)
    DSH_ARTIFACT_MAX: int = max(0, int(os.getenv("DSH_ARTIFACT_MAX", "5")))
    DSH_ARTIFACT_MAX_MB: int = max(1, int(os.getenv("DSH_ARTIFACT_MAX_MB", "20")))

    TOOL_BACKEND: str = (os.getenv("TOOL_BACKEND", "dsh").strip().lower() or "dsh")
    CBC_BIN: str = os.getenv("CBC_BIN", "").strip()
    CBC_MODEL: str = os.getenv("CBC_MODEL", "deepseek-v4.1-flash").strip()
    CBC_PERMISSION_MODE: str = os.getenv("CBC_PERMISSION_MODE", "bypassPermissions").strip()

    # 工兵沙箱与写权限护栏
    TOOL_SANDBOX_ROOT: str = os.getenv("TOOL_SANDBOX_ROOT", "").strip()
    TOOL_SANDBOX_KEEP: int = max(3, int(os.getenv("TOOL_SANDBOX_KEEP", "30")))
    TOOL_GUARD_ENABLED: bool = _parse_bool(os.getenv("TOOL_GUARD_ENABLED"), True)
    TOOL_GUARD_RESTORE: bool = _parse_bool(os.getenv("TOOL_GUARD_RESTORE"), True)
    TOOL_L3_INJECT_MAX: int = max(0, int(os.getenv("TOOL_L3_INJECT_MAX", "5")))
    TOOL_L3_INJECT_TTL_HOURS: float = max(0.5, float(os.getenv("TOOL_L3_INJECT_TTL_HOURS", "6")))

    # 群内链接静默理解
    TOOL_LINK_ENABLED: bool = _parse_bool(os.getenv("TOOL_LINK_ENABLED"), True)
    TOOL_LINK_MAX_PER_MSG: int = max(1, int(os.getenv("TOOL_LINK_MAX_PER_MSG", "2")))
    TOOL_LINK_SEEN_TTL_SEC: int = max(60, int(os.getenv("TOOL_LINK_SEEN_TTL_SEC", "7200")))
    TOOL_LINK_TIMEOUT_SEC: int = max(60, int(os.getenv("TOOL_LINK_TIMEOUT_SEC", "180")))

    # 高危操作群内二次确认保险
    ENABLE_DANGER_CONFIRM: bool = _parse_bool(os.getenv("ENABLE_DANGER_CONFIRM"), True)
    DANGER_CONFIRM_TTL_SEC: int = max(30, int(os.getenv("DANGER_CONFIRM_TTL_SEC", "180")))

    RUN_MODE: str = os.getenv("RUN_MODE", "web").strip().lower()
    WX_GROUP_WHITELIST: list[str] = _parse_list(
        os.getenv("WX_GROUP_WHITELIST"), ["大肥鱼测试群"]
    )
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
