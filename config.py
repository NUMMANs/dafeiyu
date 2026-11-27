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
    # 人设只剩大肥鱼一个（峰哥已于 2026-11-26 删除），此项保留只为兼容旧 .env
    DEFAULT_PERSONA: str = os.getenv("DEFAULT_PERSONA", "dafeiyu").strip().lower() or "dafeiyu"
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
    # 停顿多少秒算「这一波说完了」，之后才执行判定。
    # 必须大于群友两句之间的常见间隔，否则 3 秒一条的连发会被拆成两次判定（各判一次）。
    TRIGGER_DEBOUNCE_SEC: float = float(os.getenv("TRIGGER_DEBOUNCE_SEC", "4.0"))
    # 群里一直不停顿时的最大等待：从第一句挂起算起超过这个秒数就强制判定，防止永远排不上
    TRIGGER_DEBOUNCE_MAX_WAIT_SEC: float = float(os.getenv("TRIGGER_DEBOUNCE_MAX_WAIT_SEC", "12"))

    # L2.5 冷场重启话题的吐槽判定（有人隔了很久重新开口时，先轻量判断值不值得接一句）
    ENABLE_TOPIC_ROAST: bool = _parse_bool(os.getenv("ENABLE_TOPIC_ROAST"), True)
    # 距上一条群消息超过多少秒算「开新话题」，默认 10 分钟
    # 注意：这是 L2.5 唯一的频控，不再有同群冷却和每日上限（用户要求去掉）
    TOPIC_GAP_SEC: int = int(os.getenv("TOPIC_GAP_SEC", "600"))
    # 判定器认为「有意思」的最低把握度（0~1）。模型输出 0-10 分，低于该值不接话
    TOPIC_ROAST_MIN_SCORE: float = float(os.getenv("TOPIC_ROAST_MIN_SCORE", "0.7"))
    # 一波连续发言最多合并几句送给判定器（攒满就立刻整批送出，不等停顿）
    TRIGGER_BURST_MAX_ITEMS: int = int(os.getenv("TRIGGER_BURST_MAX_ITEMS", "5"))

    ENABLE_PROACTIVE_CHIME: bool = _parse_bool(os.getenv("ENABLE_PROACTIVE_CHIME"), True)
    PROACTIVE_MSG_THRESHOLD: int = int(os.getenv("PROACTIVE_MSG_THRESHOLD", "6"))
    PROACTIVE_COOLDOWN_SEC: int = int(os.getenv("PROACTIVE_COOLDOWN_SEC", "480"))
    ENABLE_SCHEDULED_BUBBLE: bool = _parse_bool(os.getenv("ENABLE_SCHEDULED_BUBBLE"), True)
    # 冷场找话题：群里长时间没人说话时，自己上网找个感兴趣的东西分享到群里。
    # 不设每日次数上限，只按「安静多久触发」「两次之间隔多久」控频（下面两个值可在
    # data/tuning.json 里热调，归 0:15 感官任务管）。
    ENABLE_TOPIC_SHARE: bool = _parse_bool(os.getenv("ENABLE_TOPIC_SHARE"), True)
    TOPIC_SHARE_SILENCE_MIN: int = int(os.getenv("TOPIC_SHARE_SILENCE_MIN", "90"))
    TOPIC_SHARE_COOLDOWN_MIN: int = int(os.getenv("TOPIC_SHARE_COOLDOWN_MIN", "120"))
    # 分享时带原帖链接的概率（0~1，默认 0.35）：带链接更像「随手转发」，
    # 但每次都贴就像营销号，所以按概率来。热调键同名，归 0:15 感官任务管。
    TOPIC_SHARE_LINK_PROB: float = float(os.getenv("TOPIC_SHARE_LINK_PROB", "0.35"))
    # 今日生活：给她一条「今天也在过日子」的线（见 daily_life.py）。冷场时除了上网找话题，
    # 也会讲讲今天自己身上发生的事——上午出门看见什么、晚上打游戏翻车。
    # 每天的剧情是懒生成的：整天没机会冒泡就一次模型调用都不花。
    ENABLE_DAILY_LIFE: bool = _parse_bool(os.getenv("ENABLE_DAILY_LIFE"), True)
    # 冷场分享时走「今天生活」的概率（另一部分是上网找话题）。0.45 = 差不多一半一半。
    LIFE_SHARE_PROB: float = float(os.getenv("LIFE_SHARE_PROB", "0.45"))
    # 生活分享正文最多多少字（超出按句截断）
    LIFE_MAX_CHARS: int = int(os.getenv("LIFE_MAX_CHARS", "60"))
    # 静默期：默认只从凌晨 1 点睡到 8 点，0 点这段留着发每日统计
    QUIET_START_HOUR: int = int(os.getenv("QUIET_START_HOUR", "1"))
    QUIET_END_HOUR: int = int(os.getenv("QUIET_END_HOUR", "8"))

    # 四层记忆：每轮是否把最近 1~2 条日记当「心情底色」注入（约 +40 tk，嫌贵可关）
    MEMORY_DIARY_INJECT: bool = _parse_bool(os.getenv("MEMORY_DIARY_INJECT"), True)
    # 四层记忆：夜间是否用一次轻量调用从当天群里提炼 L1 事实（否则只靠模型自己想起来记）
    MEMORY_NIGHTLY_EXTRACT: bool = _parse_bool(os.getenv("MEMORY_NIGHTLY_EXTRACT"), True)
    # 夜间 L1/L2 的内容由谁生成：
    #   bot      = 机器人自己调 DeepSeek 提炼/写日记（原来的做法）
    #   external = 只由 WorkBuddy 夜间定时任务写，机器人一行 L1/L2 内容都不生成，
    #              夜间只做巩固（合并/淘汰）+ 备份（2026-11-27 起默认）
    MEMORY_NIGHTLY_WRITER: str = (os.getenv("MEMORY_NIGHTLY_WRITER", "external").strip().lower() or "external")
    # 外部写作为主时，机器人是否还在凌晨兜底补写 L1/L2。
    # 默认关：00:02 的兜底会抢在 0:15/0:45 两个任务之前写掉一条日记和 6 条 L1，
    # 导致任务再写就撞去重和自然日上限，写出来的东西也不如任务复盘过的。要兜底设 true。
    MEMORY_NIGHTLY_FALLBACK: bool = _parse_bool(os.getenv("MEMORY_NIGHTLY_FALLBACK"), False)
    # 夜间提炼：一天最多落几条 L1（写库还要过 memory.remember 的每日 8 条上限）
    NIGHTLY_FACT_MAX: int = max(1, int(os.getenv("NIGHTLY_FACT_MAX", "6")))
    # 夜间提炼：每段多少条消息（全天切成多段逐段提炼，避免只看到睡前 40 条）
    NIGHTLY_EXTRACT_CHUNK: int = max(20, int(os.getenv("NIGHTLY_EXTRACT_CHUNK", "60")))
    # 夜间提炼：最多切几段（极端长的一天按均匀抽样降段，防止一晚上调几十遍模型）
    NIGHTLY_EXTRACT_MAX_CHUNKS: int = max(1, int(os.getenv("NIGHTLY_EXTRACT_MAX_CHUNKS", "12")))
    # 夜间日记：全天消息按时间均匀抽样到这个条数再喂给模型（保证上午下午晚上都有代表）
    NIGHTLY_DIARY_LINES: int = max(20, int(os.getenv("NIGHTLY_DIARY_LINES", "90")))
    # 夜间素材：单群单日最多取多少条消息进视野（防一次性拉爆内存/SQL）
    NIGHTLY_MATERIAL_MAX: int = max(100, int(os.getenv("NIGHTLY_MATERIAL_MAX", "1500")))

    # L2 追认窗口：我在群里说完话后，多少秒内的群友发言才算「可能是在接我的话」。
    # 超过这个时间窗口，L2 让位给 L2.5 冷场吐槽雷达（否则 L2 会长期霸占判定机会）
    FOLLOWUP_WINDOW_SEC: int = max(30, int(os.getenv("FOLLOWUP_WINDOW_SEC", "300")))
    # L3 会话层：是否把整层历史全部注入（推荐开，配合 token 安全阀使用）
    L3_INJECT_ALL: bool = _parse_bool(os.getenv("L3_INJECT_ALL"), True)
    # L3 全量注入时的 token 安全阀：从最新往回累加，超了就砍最旧的（防撑爆上下文）
    L3_INJECT_MAX_TOKENS: int = max(2_000, int(os.getenv("L3_INJECT_MAX_TOKENS", "90000")))
    # L3_INJECT_ALL=0 时才生效：每次回复只带最近 N 条（原写死 20），0 表示一条都不带
    L3_HISTORY_LIMIT: int = max(0, int(os.getenv("L3_HISTORY_LIMIT", "20")))
    # L3 会话层：整层占用超过多少 token 就压缩（每天 0 点后检查一次，按群判断）
    L3_CONTEXT_LIMIT_TOKENS: int = max(10_000, int(os.getenv("L3_CONTEXT_LIMIT_TOKENS", "300000")))
    # 压缩时无论如何都保留的最近消息条数（绝不参与压缩，防「刚聊的就被删」）
    L3_KEEP_RECENT: int = max(20, int(os.getenv("L3_KEEP_RECENT", "120")))
    # 一次压缩最多送给模型多少条旧消息（太多会撑爆摘要请求本身）
    L3_COMPRESS_CHUNK: int = max(20, int(os.getenv("L3_COMPRESS_CHUNK", "200")))

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

    # 工具路由：每条要回复的消息先让小模型判一次「要不要派活给 DSH」
    ENABLE_TOOL_ROUTER: bool = _parse_bool(os.getenv("ENABLE_TOOL_ROUTER"), True)
    # 短于这么多字的消息不做判定（「在吗」「哈哈」「+1」不可能是活，省一次调用）
    # 夜间复盘可通过 data/tuning.json 热调（见 tuning.BOUNDS），这里只是默认值
    TOOL_ROUTER_MIN_CHARS: int = max(2, int(os.getenv("TOOL_ROUTER_MIN_CHARS", "8")))
    # 工具全部交给 DSH：True 时大肥鱼不再挂 13 个电脑工具（DSH 不可用也不挂），
    # 干活一律走后台执行助手。False 时退回旧行为（DSH 不可用才挂电脑工具兜底）。
    TOOLS_ALL_TO_DSH: bool = _parse_bool(os.getenv("TOOLS_ALL_TO_DSH"), True)
    # DSH 任务跑完是否由大肥鱼用自己的口吻播报（False = 直接发机械汇报文本）
    DSH_REPORT_BY_BOT: bool = _parse_bool(os.getenv("DSH_REPORT_BY_BOT"), True)
    # 每个任务最多回传几个产出文件（0 = 不回传，只发机械汇报 txt）
    DSH_ARTIFACT_MAX: int = max(0, int(os.getenv("DSH_ARTIFACT_MAX", "5")))
    # 单个产出文件大小上限（MB），超过只发报告不发文件
    DSH_ARTIFACT_MAX_MB: int = max(1, int(os.getenv("DSH_ARTIFACT_MAX_MB", "20")))

    # 工具执行后端：dsh（默认，行为不变）| codebuddy（本机 CodeBuddy CLI，走订阅额度）
    # 只切换「谁来跑活」，派发/播报/回传产出这套外壳完全复用，出问题把值改回 dsh 即可。
    TOOL_BACKEND: str = (os.getenv("TOOL_BACKEND", "dsh").strip().lower() or "dsh")
    # CodeBuddy CLI 入口：.js/.mjs 走 node 直跑，.cmd/.bat 走 cmd /c；留空则自动探测
    CBC_BIN: str = os.getenv("CBC_BIN", "").strip()
    CBC_MODEL: str = os.getenv("CBC_MODEL", "deepseek-v4.1-flash").strip()
    # 无人值守必须跳过权限确认，否则工具调用会被挂起
    CBC_PERMISSION_MODE: str = os.getenv("CBC_PERMISSION_MODE", "bypassPermissions").strip()

    # ---- 工具 LLM 的写权限边界（2026-11-26）----
    # 派出去干活的工具只许在沙箱里生成文件，外加一个受控通道：写 l3_inject.json 给
    # 大肥鱼的上下文补几句话。项目代码 / .env / roles 人格文件 / data 记忆库一律不许碰。
    # 沙箱默认放在家目录（项目外面），工具从 cwd 出发根本看不到项目。
    TOOL_SANDBOX_ROOT: str = os.getenv("TOOL_SANDBOX_ROOT", "").strip()
    # 家目录下最多留多少个历史沙箱（任务产物投递完就没用了，留着只方便事后查）
    TOOL_SANDBOX_KEEP: int = max(3, int(os.getenv("TOOL_SANDBOX_KEEP", "30")))
    # 任务前后比对项目文件：被改/被删的原样还原，新建的挪进 quarantine 并记账
    TOOL_GUARD_ENABLED: bool = _parse_bool(os.getenv("TOOL_GUARD_ENABLED"), True)
    # 发现越界时是否自动还原。关掉就只告警不动手（排查误报时用）
    TOOL_GUARD_RESTORE: bool = _parse_bool(os.getenv("TOOL_GUARD_RESTORE"), True)
    # 一次任务最多往 L3 注入几条（0 = 关闭这条通道）
    TOOL_L3_INJECT_MAX: int = max(0, int(os.getenv("TOOL_L3_INJECT_MAX", "5")))
    # 注入内容的存活时间（小时），过期自动失效，不污染上下文
    TOOL_L3_INJECT_TTL_HOURS: float = max(0.5, float(os.getenv("TOOL_L3_INJECT_TTL_HOURS", "6")))

    # ---- 群内链接理解（2026-11-26）----
    # 群里甩链接不要求大肥鱼当场回复：入口静默派一个 link 任务让 CodeBuddy 读懂，
    # 结果写进 L3 注文，等后面聊到自然接得上。关掉就完全退回「只有派活才用工具」。
    TOOL_LINK_ENABLED: bool = _parse_bool(os.getenv("TOOL_LINK_ENABLED"), True)
    # 一条消息最多读几个链接（多个会合并进同一个任务，防止一次烧太久）
    TOOL_LINK_MAX_PER_MSG: int = max(1, int(os.getenv("TOOL_LINK_MAX_PER_MSG", "2")))
    # 同群 + 同 URL 多久内不重复读（秒）
    TOOL_LINK_SEEN_TTL_SEC: int = max(60, int(os.getenv("TOOL_LINK_SEEN_TTL_SEC", "7200")))
    # link 任务单独超时（比干活短，读个网页不该拖很久）
    TOOL_LINK_TIMEOUT_SEC: int = max(60, int(os.getenv("TOOL_LINK_TIMEOUT_SEC", "180")))

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
