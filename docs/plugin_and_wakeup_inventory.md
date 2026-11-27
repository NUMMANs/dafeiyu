# 插件与 LLM 唤醒条件盘点（改动前基线）

> 基线：`HEAD = 94a2f5e`｜盘点时间：2026-11-25｜方式：全量静态阅读
> 本文只做"现状清单"，不含任何代码改动。行号以当前文件为准。

---

## 0. 三条前置结论

1. **本项目没有 `plugins/` 目录**。"插件"实际是三样东西：LLM function calling 工具 **25 个**、群指令 **23 条**、通道适配器 **3 套**。
2. **"LLM 唤醒"是 4 层串联门**，不是单一判断。一条普通群消息最坏会打 **3 次模型请求**（正式回复 + 指代判定 + 插嘴判定）。
3. **当前 `.env` 是 `OWNER_NAMES=`（空）**，而 `agent_tools.is_owner()` 在空名单时直接 `return True`（`agent_tools.py:45-46`）。所以那 15 个能执行命令 / 改源码 / 派 DSH 的工具，**现在对群里任何人开放**，`max_tokens` 也跟着放大到 4096。

---

## 1. 扩展点 A：LLM 工具（25 个）

### 1.1 挂载逻辑（`brain.py:617-626`）

```python
available_tools = SEARCH_TOOLS  # ENABLE_WEB_SEARCH=false 时剔除 web_search
if is_owner(sender):            # 空 OWNER_NAMES 时恒为 True
    available_tools.extend(AGENT_TOOL_SCHEMAS)
if not used_vision and available_tools:
    kwargs["tools"] = available_tools
    kwargs["tool_choice"] = "auto"
```

| 事实 | 说明 |
|---|---|
| 挂载时机 | **每一轮**请求都带全量 schema（含闲聊），约 3k token 输入 |
| 视觉消息 | `used_vision=True` 时不挂工具 |
| 前置搜索命中 | `used_search=True` 时剔除 `web_search` |
| `tool_choice` | `auto`，模型可自行决定调用，无强制 |
| `max_tokens` | owner+非视觉 **4096** / 视觉 800 / 其他 **260**（`brain.py:615`） |
| 循环上限 | 最多 4 轮，每轮最多执行前 3 个 `tool_calls`（`brain.py:632-644`） |

### 1.2 内容类工具 10 个（`brain.SEARCH_TOOLS`，所有人可用）

| # | 工具名 | 作用 | 分发位置 | 副作用 |
|---|---|---|---|---|
| 1 | `web_search` | 联网搜索（多引擎串行） | `brain.py:670` | 最坏 40s+ 阻塞 |
| 2 | `remember_fact` | 写 L1 识海记忆 | `:675` | 永久写入 `state/mind_graph.json` |
| 3 | `learn_slang` | 记群黑话 | `:689` | 写 `state/slang.json` |
| 4 | `evolve_persona` | **自我改写人格** | `:693` | 永久写 `data/persona_evolutions_*.json`，第二天仍生效 |
| 5 | `add_schedule` | 建定时任务 | `:697` | 写 `state/schedules.json`；`ai_task` 模式会再触发整条大脑链 |
| 6 | `list_schedules` | 列定时任务 | `:709` | 只读 |
| 7 | `delete_schedule` | 删定时任务 | `:717` | 删任务 |
| 8 | `inspect_group_image` | 重看历史图片 | `:719` | 调智谱/DeepSeek 视觉，回写历史 |
| 9 | `search_and_send_image` | 联网搜图并发群 | `:739` | 同步网络 IO，最坏数十秒 |
| 10 | `download_image_from_url` | 下图片直链并发群 | `:746` | 任意 URL，无内网黑名单（SSRF 面） |

> 注：`SEARCH_TOOLS` 这个名字有误导性，它其实包含记忆 / 人格 / 定时 / 图片等，不只是搜索。

### 1.3 Agent 类工具 15 个（`agent_tools.AGENT_TOOL_SCHEMAS`，`is_owner` 才挂）

| # | 工具名 | 作用 | 二次确认 | 分发位置 |
|---|---|---|---|---|
| 11 | `run_shell` | PowerShell 执行 | ✅ | `:806` |
| 12 | `read_local_file` | 读本机任意文件 | ✅ | `:809` |
| 13 | `write_local_file` | 写本机任意文件 | ✅ | `:811` |
| 14 | `open_url_or_app` | 打开 URL / 启动程序 | ✅ | `:825` |
| 15 | `send_existing_file` | 发本机文件到群 | ✅ | `:843` |
| 16 | `self_edit_code` | **改自己源码** | ✅ | `:859` |
| 17 | `delegate_to_dsh` | 派 DSH 后台任务 | ✅ | `:870` |
| 18 | `take_screenshot` | 截屏 | ❌ | `:828` |
| 19 | `list_directory` | 列目录 | ❌ | `:823` |
| 20 | `get_system_info` | 系统信息 | ❌ | `:833` |
| 21 | `fetch_webpage` | 抓任意网页 | ❌ | `:835` |
| 22 | `create_and_send_file` | 生成文件并发群 | ❌ | `:837` |
| 23 | `self_pull_and_restart` | 拉代码 + 重启 | ❌ | `:848` |
| 24 | `self_restart` | 重启 | ❌ | `:866` |
| 25 | `check_dsh_jobs` | 查 DSH 任务 | ❌ | `:890` |

**二次确认只覆盖 7 个**（`brain.py:772-780`）。另外 8 个（含 `self_restart` / `self_pull_and_restart` / `create_and_send_file`）直接执行，且确认本身"群内任意成员可批准"。

补充：`run_shell` 另有正则黑名单 `DANGEROUS_CMD_RE`（`agent_tools.py:31-34`），只拦 `format` / `rd /s` / `del /f /s` / `rm -rf /` / `shutdown` / `diskpart` / `bcdedit`。

---

## 2. 扩展点 B：群指令（23 条，`bot_core._handle_command`）

| # | 指令（含别名） | 权限 | 行号 | 与工具重复 | 备注 |
|---|---|---|---|---|---|
| 1 | `/状态` `查鱼` `/鱼状态` | 所有人 | 220 | — | |
| 2 | `/投喂` `喂白饭` `/喂白饭` `投喂` | 所有人 | 242 | — | |
| 3 | `/摸鱼` `摸摸鱼` `/撸鱼` | 所有人 | 268 | — | |
| 4 | `/鱼塘榜` `鱼塘榜` `/排行榜` | 所有人 | 302 | — | 会显示"群聊潜水"假用户 |
| 5 | `/帮助` `帮助` `/help` | 所有人 | 325 | — | 文案硬编码，改能力要同步改 |
| 6 | `/记住` | **所有人** | 343 | ↔ `remember_fact` | 固定 `level=important` + `source=owner`（权重×1.8），记忆投毒面 |
| 7 | `/记忆` `查记忆` | 所有人 | 359 | — | |
| 8 | `/日记` `看日记` | 所有人 | 371 | — | 当前 `data/日记/` 为空 |
| 9 | `/学黑话` | 所有人 | 383 | ↔ `learn_slang` | |
| 10 | `/黑话` `查黑话` | 所有人 | 392 | — | |
| 11 | `/人设` `查人设` `/进化` | 所有人 | 400 | ↔ `evolve_persona`（只读） | |
| 12 | `/当前人设` `/人设列表` `人设列表` | 所有人 | 407 | — | |
| 13 | `/切换人设 <名>` `/换人设` | **所有人** | 416 | — | 任何人可切换全群说话风格 |
| 14 | `/定时` `查定时` `/定时任务` | 所有人 | 435 | ↔ `list_schedules` | 仅 wxauto 模式生效 |
| 15 | `/取消定时 <id>` | 所有人 | 451 | ↔ `delete_schedule` | 不校验任务归属 |
| 16 | `/更新` `/拉代码` `/gitpull` | Owner | 459 | ↔ `self_pull_and_restart` | |
| 17 | `/重启` `/restart` | Owner | 469 | ↔ `self_restart` | |
| 18 | `/dsh状态` `/后台任务` `/dsh任务` | Owner | 476 | ↔ `check_dsh_jobs` | |
| 19 | `/dsh <任务>` `/dsh 续` | Owner | 491 | ↔ `delegate_to_dsh` | 派发前过 `assess_danger` |
| 20 | `/搜图` `/找图` `/发图` | 所有人 | 534 | ↔ `search_and_send_image` + `download_image_from_url` | 同步网络 IO |
| 21 | `/待确认` | 所有人 | 560 | — | |
| 22 | `/确认 [码]` | **所有人** | 575 | — | **允许不带码**，只有一个待确认项时直接执行 |
| 23 | `/取消 [码]` | 所有人 | 594 | — | 传 `is_requester_owner=True`，把校验绕开了 |

### 2.1 指令 ↔ 工具 重复矩阵（同一能力两套实现）

| 能力 | 指令侧 | 工具侧 |
|---|---|---|
| DSH 派发 | `/dsh` (491-532) | `delegate_to_dsh` (870) |
| DSH 查询 | `/dsh状态` (476) | `check_dsh_jobs` (890) |
| 定时任务 | `/定时` `/取消定时` (435/451) | `add/list/delete_schedule` (697-718) |
| 记忆写入 | `/记住` (343) | `remember_fact` (675) |
| 黑话 | `/学黑话` (383) | `learn_slang` (689) |
| 人格进化 | `/进化` (400, 只读) | `evolve_persona` (693, 写入) |
| 搜图 | `/搜图` (534) | `search_and_send_image` (739) |
| 高危确认后执行 | `_execute_confirmed_action` (85-138) | 工具分支内联 (753-905) |

> 这意味着：**删掉工具里的某个能力，不等于删掉了这个能力** —— 指令那条路还在。

---

## 3. 扩展点 C：通道（3 套）

| 通道 | 文件 | 群聊触发方式 | 后台循环 |
|---|---|---|---|
| wxauto（PC 微信） | `adapters/wxauto_adapter.py` | 群聊 `force_trigger=False`，私聊 `True`（`:235`） | ✅ 唯一实现（定时/冒泡/DSH/Git） |
| QQ OneBot | `adapters/qq_onebot_adapter.py` | 群聊 `at_me`，私聊 `True`（`:159-162`） | ❌ |
| iLink | `adapters/ilink_adapter.py` | 恒 `True`（`:46`） | ❌ |
| Web 控制台 | `web_server.py:74` | `Form(True)`，默认每条都触发 | ❌ |

---

## 4. 唤醒条件全景（4 层门）

### L1 硬触发 —— `_is_triggered`（`bot_core.py:197-210`）

按顺序短路，命中任一即 100% 调模型：

| 序 | 条件 | 代码 | 问题 |
|---|---|---|---|
| 1 | `force_trigger` 或 `not GROUP_AT_ONLY` | `:198` | 私聊/iLink/Web 恒真 |
| 2 | 以 `/` 开头，或裸词 `查鱼`/`喂白饭`/`摸摸鱼`/`鱼塘榜`/`帮助` | `:201` | — |
| 3 | **命中 `LOL_KEYWORDS` 任一子串** | `:204` | 见 4.1 |
| 4 | 命中 `TRIGGER_KEYWORDS`（`@大肥鱼`/`大肥鱼`/`肥鱼`） | `:207` | 子串匹配，"肥鱼"出现在任何句子都算 |
| 5 | `_is_answerable_question()` 为真 | `:210` | 见 4.2 |

#### 4.1 `LOL_KEYWORDS`（`bot_core.py:37-45`，共 68 个词）的歧义项

整表：`lol` `英雄联盟` `联盟` `排位` `大乱斗` `海斗` `海克斯` `极地大乱斗` `云顶` `金铲铲` `峡谷` `召唤师峡谷` `上单` `打野` `中单` `下路` `辅助` `adc` **`ad`** **`ap`** `开黑` `五排` `双排` `单排` `掉分` `上分` `连跪` `连胜` `战绩` `kda` `出装` `符文` `天赋` `对线` `打团` `五杀` `掌盟` `补刀` `ban` `bp` `黑铁` `青铜` `白银` `黄金` `铂金` `翡翠` `钻石` `大师` `王者` `lpl` `lck` `msi` `s赛` `faker` `uzi` `theshy` **`bin`** `knight` `亚索` `劫` `盲僧` `剑圣` `提莫` `诺手` `剑魔` `卡莎` `ez` `vn` `锤石` `机器人`

匹配方式是 `kw in text.lower()`（`:179`），**纯子串、无词边界**。已确认的误命中：

| 真实输入 | 命中词 | 结果 |
|---|---|---|
| `happy birthday` | `ap` | 100% 回复 |
| `download 好了` | `ad` | 100% 回复 |
| `admin 没权限` | `ad` | 100% 回复 |
| `binary 文件` | `bin` | 100% 回复 |
| `机器人` | `机器人` | 正常（这条是对的，但"扫地机器人"也会中） |

#### 4.2 `_is_answerable_question`（`:181-195`）

```
len(text) < 4                        → False
命中 PRIVATE_LOGISTICS_CUES          → False
命中 QUESTION_CUES 任一子串          → True
或 len>=6 且以 ? / ？ / 吗 / 嘛 结尾 → True
```

- `QUESTION_CUES`（`:48-54`，34 个）：`怎么` `咋办` `咋弄` `咋样` `为啥` `为什么` `什么意思` `是什么` `是啥` `哪个好` `推荐` `区别` `原理` `报错` `怎么办` `怎么解决` `多少钱` `有必要` `好玩吗` `值得` `氪吗` `肝吗` `怎么选` `哪款` `有人知道` `谁知道` `请问` `教程` `攻略` `怎么买` `怎么用` `怎么装` `为啥会` `是什么意思` `怎么回事` `哪里下` `在哪下`
- `PRIVATE_LOGISTICS_CUES`（`:57-60`，15 个）：`回家` `买票` `候补` `转车` `出去玩` `值班` `请假` `打电话` `今晚怎么说` `老地方` `几点去` `哪天吃饭` `咱几个` `咱4个` `咱两`
- **`group_id` 参数收了但从未使用**（`:181`），现在无法按群差异化。

实测误触发：`"我今天怎么这么累"`（含"怎么"、无问号）→ 100% 回复。

### L2 软触发 —— 粘性窗口 / 代词（`bot_core.py:746-752`）

```python
if image_path is None and not triggered:
    in_active_window = (now - 上次机器人发言时间) <= 120
    mentions_pronoun = 含 ABOUT_ME_CUES
    if (in_active_window and len>=2) or (mentions_pronoun and len>=4):
        → brain.is_talking_to_or_about_me(...)  # 第 2 次模型调用
```

- `ABOUT_ME_CUES`（`:64-66`）：`你` `他` `它` `她` `这鱼` `那鱼` `机器人` `ai` `小号` `头像` `换了` `塔菲`
- 判定实现（`brain.py:998-1031`）：`temperature=0.0`、`max_tokens=256`、返回含 `YES` 即唤醒，异常返回 `False`
- 成本：**每条含"你/他/她/它"且 ≥4 字的消息都会多一次完整 API 往返**。"你"是中文最高频字之一。

### L3 主动冒泡 —— HeartFlow（`bot_core.py:754-755` → `843-891`）

前置：`ENABLE_PROACTIVE_CHIME=true` 且 非静默时段（`QUIET_START_HOUR=23` / `QUIET_END_HOUR=8`）

热力模型（`persona_runtime.py:189-212`）：

| 参数 | 值 |
|---|---|
| 初始热力 | 15.0 |
| 每条消息增益 | `9.0 + min(10.0, (2分钟内不同发言人数-1) * 4.5)` |
| 笑点词加成 | 含 `哈哈`/`笑死`/`卧槽`/`逆天`/`牛`/`草`/`离谱`/`离大谱` → +6.0 |
| 自然衰减 | `exp(-elapsed_min / 12.0)`（约每分钟 -6%） |
| 上限 | 100.0 |

判定链：

1. `energy < 65.0` → 静默返回（`:849`）
2. 距上次冒泡 `< PROACTIVE_COOLDOWN_SEC (480s)` → 静默（`:852`）
3. 通过 → `drain(60.0)` + 记录时间戳
4. 调 `brain.judge_and_chime_in()`（第 3 次模型调用）：取最近 8 条，少于 3 条直接 `None`；`temperature=0.8`、`max_tokens=160`；输出含 `[PASS]` 则不出声

### L4 定时 / 自主冒泡 —— `check_autonomous_heartbeat`（`:893-971`）

| 触发 | 条件 | 频率 |
|---|---|---|
| 夜间养护 | 23:45 之后，且当天未做 | 每群每天 1 次（实际只有第一个群生效，见 `:160`） |
| 饥饿催饭 | `hunger <= 25` | 每群 3 小时最多 1 次 |
| 晚间问候 | `20:00~20:59` 且群内安静 ≥5400s | 每群每天 1 次 |
| 外部定时任务 | `pop_due_tasks` 每 15s（仅 wxauto 循环） | 按任务定义 |

静默时段 23:00~08:00 内，上述主动冒泡全部跳过。

### L5 图片例外路径（`:720-728`）

| 情况 | 行为 |
|---|---|
| 纯图片 + `force_trigger` | 直接锐评 |
| 纯图片 + `random() < AUTO_ROAST_IMAGE_PROB (0.35)` | 锐评 |
| 纯图片 + 未命中 | **不回复**，后台调智谱写摘要 |
| 图片 + 文字 | 跳过 L2 / L3（`image_path is None` 不成立），但仍走 L1 |

---

## 5. 一次消息的最大模型调用数

| 场景 | 调用次数 |
|---|---|
| 只 @ 了一下 | 1 次（`reply`） |
| 含"你"、未 @ | 2 次（`is_talking_to_or_about_me` + `reply`） |
| 未触发但群热闹 | 2 次（`judge_and_chime_in` + `reply`，若 PASS 则 1 次） |
| 含"你" + 群热闹 | **3 次** |
| `ai_task` 定时任务 | 3 次（外加前一次入队时的判定） |

---

## 6. 可改点清单（供提需求时直接引用编号）

### 6.1 插件侧（P）

| 编号 | 动作 | 位置 | 影响 |
|---|---|---|---|
| **P1** | 摘掉全部 15 个 Agent 工具 | `agent_tools.py:230-448` + `brain.py:753-905` | 群内彻底失去执行命令/读写文件能力 |
| **P2** | 只留只读 4 个（`list_directory`/`get_system_info`/`take_screenshot`/`fetch_webpage`） | 同上 | 保留"帮我看下电脑"能力，去掉写入 |
| **P3** | 删自运维链（`self_edit_code`/`self_restart`/`self_pull_and_restart`） | 同上 | 去掉 AI 自改代码风险 |
| **P4** | 删 `evolve_persona` | `brain.py:693` + schema `:130` | 人格不再被模型自动改写 |
| **P5** | 删定时三件套（`add/list/delete_schedule`） | `:697-718` | 仅 web/qq 模式无感（那里本来不生效） |
| **P6** | 删搜图二件套（`search_and_send_image`/`download_image_from_url`） | `:739-752` | 表情包链不受影响 |
| **P7** | 删 `download_image_from_url`（保留关键词搜图） | `:746` | 去掉任意 URL 下载面 |
| **P8** | 指令瘦身：删 `/记住` `/进化` `/切换人设` `/dsh*` | `bot_core.py:343/400/416/476/491` | 同步改 `/帮助` 文案（`:325-341`） |
| **P9** | 修 `is_owner` 空名单语义（改 `False` 或启动即拒绝） | `agent_tools.py:45-46` | 收紧 4096 max_tokens 与工具挂载 |
| **P10** | 工具收敛成 `TOOLS` 注册表（结构性重构） | 新增 `tools_registry.py` | 工作量大，但之后增删工具只改一处 |

### 6.2 唤醒侧（W）

| 编号 | 动作 | 位置 | 效果 |
|---|---|---|---|
| **W1** | 整体删掉 LOL 规则 | `bot_core.py:204` | 游戏话题不再 100% 必回 |
| **W2** | LOL 改词边界匹配 + 去掉 `ad`/`ap`/`bin` | `:177-179`、`:37-45` | 消掉 happy/download/admin/binary 误触发 |
| **W3** | 删掉答疑门（只留 @ / 名字 / 指令） | `:210` | 最严，群里几乎不再自动接话 |
| **W4** | 答疑门加严：必须带问号 + 长度≥6 + 未命中私人词 | `:181-195` | 保留"真提问才答"，误触发大幅下降 |
| **W5** | 答疑门按群开关 / 群白名单 | `:181`（用起来 `group_id`） | 有的群爱答、有的群闭嘴 |
| **W6** | `ABOUT_ME_CUES` 去掉 `你`/`他`/`她`/`它` | `:64-66` | 砍掉大部分 L2 模型调用 |
| **W7** | L2 加硬前置（必须 120s 内有交互 + 每群 60s 冷却） | `:746-752` | 降低 token 消耗 |
| **W8** | 关掉 L3 主动冒泡 | `.env` 的 `ENABLE_PROACTIVE_CHIME=false` | 零代码改动即可生效 |
| **W9** | 热力阈值 65 / 冷却 480 抽成配置 | `:849`、`:852` | 可调松紧 |
| **W10** | 纯图自动锐评概率 0.35 → 0 或仅 @ 时 | `:725` | 不再随机吐槽图片 |
| **W11** | 晚间问候 / 饥饿催饭独立开关 | `:922`、`:948` | 分开控制 |
| **W12** | 4 层门各自做成 `ENABLE_*` 开关 | `config.py` + 上述各处 | 行为全交给 `.env` |

### 6.3 结构性选项（工作量大，但一劳永逸）

| 编号 | 动作 | 说明 |
|---|---|---|
| **S1** | `TriggerPolicy` 单一判定类 | 把 `_is_triggered` / `_is_answerable_question` / L2 / L3 收进一个类，规则表驱动 + 按群配置 |
| **S2** | `TOOLS` 注册表 | schema + handler + 权限 + 危险标记 + 超时放在一个 dataclass，`brain.reply` 不再写 200 行 elif |
| **S3** | 指令注册表 | `COMMANDS[name] = handler`，`/帮助` 自动生成，避免文案漂移 |

---

## 7. 改动时必须连带检查的点

1. **删工具要同步删 elif 分支**。只删 schema 不删分支 → 模型不会再调，但死代码留着；只删分支不删 schema → 模型调了会走到 `else` 兜底、返回空结果。
2. **删指令要同步改 `/帮助` 文案**（`:325-341`），否则用户看到的和实际能力不一致。
3. **改 `is_owner` 会连带影响 `max_tokens`**（`brain.py:615` 同一个判断），闲聊长度会从 4096 掉到 260。
4. **改 L1 不会停掉 HeartFlow 累加**。热力在 `_maybe_proactive_chime_in` 里的 `record_group_pulse` 才累加，只要还有消息走到 L3 就会涨。
5. **`/记住` 和 `remember_fact` 是两条路**：删工具不会阻止群友用 `/记住` 写 `source=owner` 的高权重记忆。
6. **本机 Python 环境没有 `openai` 依赖**，改动后只能 `python -m py_compile` 做语法校验，跑不了真实对话回归。
7. 唤醒变严后，`generate_autonomous_bubble`（L4）仍会在 20:00 和饥饿时主动说话 —— 如果目标是"彻底不主动开口"，L4 也要一起关。

---

## 8. 如果想用最小改动拿到最大效果（供参考，不是建议）

`P9 + P1 + W3 + W8` 四处：
- P9 收紧权限 → 群友不再能执行命令（安全）
- P1 摘 Agent 工具 → 每轮省约 1.5k token（成本）
- W3 删答疑门 → 消掉"随口一句就插嘴"（体验）
- W8 关冒泡 → 群里彻底安静，只在被叫时说话（体验）

预计 diff 规模：`agent_tools.py` 删约 220 行 schema、`brain.py` 删约 150 行分支、`bot_core.py` 改约 10 行。

---

## 9. 改动记录

### 2026-11-25｜L1 硬触发收紧为「只认 @大肥鱼」

**需求**：L1 硬触发的词仅保留 `@大肥鱼`。

**改动文件**

| 文件 | 改动 |
|---|---|
| `config.py:46-50` | `TRIGGER_KEYWORDS` 默认值 `["@大肥鱼","大肥鱼","肥鱼"]` → `["@" + BOT_NAME]`；仍支持 `.env` 覆盖 |
| `bot_core.py:200-214` | `_is_triggered` 收敛为：`force_trigger / GROUP_AT_ONLY` 守卫 + 单一唤醒词匹配；删除 `/` 前缀判定、`LOL_KEYWORDS` 判定、答疑门判定 |
| `bot_core.py:180-198` | `_is_lol_related` / `_is_answerable_question` 标记【已停用】，函数体保留以便回滚 |
| `bot_core.py:35-38` | 三张词表（`LOL_KEYWORDS` / `QUESTION_CUES` / `PRIVATE_LOGISTICS_CUES`）加停用注释 |
| `bot_core.py:216-221` | `_strip_trigger` 追加剥离微信 @ 后的窄空格 `U+2005` 与全角空格 |
| `bot_core.py:334` | `/帮助` 文案由「直接叫大肥鱼、问英雄联盟都会回」改为「只有 @ 才会回复」 |
| `.env.example:22-23` | 示例值同步为 `TRIGGER_KEYWORDS=@大肥鱼`，避免新部署复制回旧行为 |

**改动后的 L1 判定表**

| 输入 | 唤醒 | 说明 |
|---|---|---|
| `@大肥鱼 你好` | ✅ | 唯一唤醒路径 |
| `@大肥鱼` 单独一条 | ✅ | 剥离后 prompt 为空，模型自行回应 |
| `大肥鱼你好` | ❌ | 不 @ 不再触发 |
| `肥鱼呢` | ❌ | 同上 |
| `/状态` `/投喂` … | ✅（另一条路） | `_handle_command` 在 L1 之前处理并返回 |
| `/未知指令` | ❌ | 原先靠 `/` 前缀唤醒，现不再唤醒 |
| `happy birthday` / `问一下亚索出装` / `我今天怎么这么累` | ❌ | 原 LOL / 答疑误触发已消除 |

**仍然存在的非 @ 唤醒路径（本次未改）**

| 层 | 位置 | 行为 |
|---|---|---|
| L2 代词判定 | `bot_core.py:746-752` | 文本含 `你/他/她/它/这鱼/小号/头像…` 且 ≥4 字，或 120s 粘性窗口内，仍会调模型判断是否接话 |
| L3 主动冒泡 | `bot_core.py:843-891` | HeartFlow ≥65 + 冷却到，仍会调模型判断是否插嘴 |
| L4 定时冒泡 | `bot_core.py:893-971` | 20:00 安静问候、饥饿催饭、23:45 养护照旧 |
| L5 纯图锐评 | `bot_core.py:725` | 未 @ 也有 35% 概率锐评纯图片 |
| 私聊 / iLink / Web | 各适配器 | `force_trigger=True`，每条必回（符合预期） |

若目标是"群里完全只在被 @ 时说话"，还需 `W6`（代词表去掉你/他/她/它）+ `W8`（`ENABLE_PROACTIVE_CHIME=false`）+ `W10`（`AUTO_ROAST_IMAGE_PROB=0`），三者都无需改代码即可生效或仅一行改动。

**回滚方式**

- 恢复旧关键词：`.env` 加 `TRIGGER_KEYWORDS=@大肥鱼,大肥鱼,肥鱼`
- 恢复 LOL / 答疑判定：在 `_is_triggered` 里加回 `if self._is_lol_related(...)` 与 `return self._is_answerable_question(...)`
- 恢复 `/` 前缀：加回 `if probe.startswith("/"): return True`

**验证情况**

- `python -m compileall` 全仓通过。
- 触发逻辑用独立脚本验证了 8 组输入（见上表）。
- **未做真实微信会话回归**：本机 Python 环境缺 `openai` / `python-dotenv`，无法导入完整链路。需在群里实测确认 `@` 文本形态（微信 @ 后跟 `U+2005`，已兼容）。

### 2026-11-25｜L2 软触发重构（按条数窗口 + 零成本快路）

**需求**：取消 120 秒时间窗，改为"大肥鱼发言后 5 条内"判断；引用大肥鱼的消息直接唤醒；@ 别人的消息仍交给模型判断；减少 token 消耗。

**为什么不能用人格.md 做前置提示词**（实测结论，未采纳）

| 项 | 数值 |
|---|---|
| `roles/大肥鱼.人格.md` | 1405 字 ≈ 936 tokens |
| 重构前 L2 单次输入 | ≈ 917 tokens |
| 若加人格前缀后单次输入 | ≈ 1552 tokens |
| 对"是否在跟我说话"的判定增益 | 无（人格已在主提示词里，`brain.py:377-399` 注入 `load_card()`） |

结论：加人格会让单次输入翻倍、把全部优化吃掉，且模型容易转去扮演角色而吐不出 `YES`，导致 `raw.startswith("YES")` 判定失效。

**改动文件**

| 文件 | 改动 |
|---|---|
| `bot_core.py:750-757` | L2 入口改为调用 `_is_followup_to_me`，删除 120s 时间窗与代词表判定 |
| `bot_core.py:228-281` | 新增 `_is_followup_to_me`：按条数窗口 + 引用快路 + 透传 @他人信号 |
| `bot_core.py:69-73` | `ABOUT_ME_CUES` 标记【已停用】 |
| `bot_core.py:36,38` | 新增 `logging` / `logger`，便于线上观察判定结果 |
| `brain.py:998-1063` | `is_talking_to_or_about_me` 重写：新提示词、`at_others` 参数、`max_tokens` 256→24、上下文按角色标注 |

**判定规则（改动后）**

1. 文本长度 < 2 → 不判定
2. 引用了 `大肥鱼` 的消息 → **直接唤醒，0 token**
3. 找不到机器人最近发言 → 不判定
4. 机器人最后一次发言之后已过 5 条群友消息 → 不判定
5. 以上都通过 → 调用判定模型，输入 =「我的上一句 + 之后这几条 + 当前这句 + 是否 @ 了别人」

窗口内文本出现 `@` 即视为"@ 了别人"（因为 L1 已排除 `@大肥鱼`），只作为提示信号，不硬性否决。

**新提示词要点**（对比旧版）

| 维度 | 旧版 | 新版 |
|---|---|---|
| 我在上下文中的标识 | `[大肥鱼]:`（需模型自己猜是我） | `[我]:`（显式声明） |
| 判定目标 | "跟你说话 / 接话茬 / 议论你" 三合一 | 只判"是不是冲我来的" |
| @他人 / 引用信号 | 无 | 有独立字段 |
| 反例约束 | 无 | 明确列出"跟别人聊、@别人、新话题、自言自语、议论第三人"→ NO |
| 输出约束 | `max_tokens=256` | `max_tokens=24`，`startswith("YES")` |
| 单条文本上限 | 无（长消息会灌爆输入） | 截断 300 字 |

**效果（用 `data/dafeiyu.db` 687 条真实消息回放）**

| 指标 | 旧 | 新 |
|---|---|---|
| L2 判定次数 | 358（52.1%） | 370（53.9%） |
| 单次输入 | 917 tokens | ≈ 300 tokens |
| 每百条消息 L2 输入量 | 47.8k | **15.9k** |
| 降幅 | — | **-67%** |

判定次数略增（5 条窗口在冷清群比 120s 更宽），但单次输入降到 1/3，总量仍降 2/3。

**回滚**

- 恢复时间窗：在 `_is_followup_to_me` 里把 `turns_after >= max_turns` 换成时间差判断
- 恢复代词前置：加回 `mentions_pronoun` 条件
- 恢复旧提示词：`git diff brain.py` 取回 `is_talking_to_or_about_me` 原实现

**未改动**

L3 主动冒泡、L4 定时冒泡、L5 纯图 35% 锐评 —— 仍会在未 @ 时出声。若要"群里只在被 @ 时说话"，还需 `ENABLE_PROACTIVE_CHIME=false` + `AUTO_ROAST_IMAGE_PROB=0`。


### 2026-11-25｜新增 L2.5「冷场重启话题」的吐槽判定

**动机**

L1 收紧到只认 @ 之后，群里重新开话题时它完全沉默。这里补一个"有乐子才接一句"的中间层，既不违反"只有 @ 才唤醒"，也不至于全程装死。

**链路位置**

```
L1 @唤名 → L2 我发言后 5 条内追认 → L2.5 冷场开话题吐槽判定 → L3 HeartFlow 冒泡 → L4 定时
```

**两道成本闸门**

1. 本地零成本规则（`bot_core._maybe_topic_roast`）先过 7 条过滤，全过才可能花钱：

| 过滤 | 目的 |
|---|---|
| 长度 >= 6 字 | 「在吗」「哈哈哈」没有话题量 |
| 不含 `@` | L1 已排除 @我，此时 @ 必是 @ 别人 |
| 不以 `/` 开头 | 未识别指令不是话题 |
| 不以 `<` 开头 | 适配器偶尔漏出的原始 XML |
| 至少 2 个汉字 | 过滤 wxid、纯表情、纯数字 |
| 开头不是 `大肥鱼`/唤醒词 | 「叫了名字但没 @」按 L1 决定保持沉默 |
| `prev_activity_ts > 0` | 进程刚启动时无法判断冷场，宁可不说话 |

再叠加三条频控：距上条消息 ≥ `TOPIC_GAP_SEC`（600s）、同群冷却 ≥ 1800s、同群每日 ≤ 4 次。

> **2026-11-27 变更**：这一层已从「吐槽判定」改为「兴趣判定」——`brain.judge_topic_interest`，阈值键 `TOPIC_INTEREST_MIN_SCORE`（旧名仍兼容），判定依据换成人格卡里的「我的兴趣区」，输出 `分数|她想说的点`；主回复的提示词从「接一句吐槽」改成「你自己对这话题有兴趣，主动接一句」。下面这段保留为当时的记录。

2. 轻量判定器（`brain.judge_topic_roast`）：不挂人格、不挂工具、`max_tokens=40`，输入只有「冷场多久 + 最近 6 句 + 当前这句」，输出 `分数|槽点`。分数低于 `TOPIC_ROAST_MIN_SCORE × 10` 则不接话。

**主回复的接入方式**

判定通过后走正常 `brain.reply`，但多传一个 `roast_hint`：

- 用户消息里显式写明【任务：吐槽】，说明"没人叫你，是有人重新开了话题"；
- 附带判定器给出的「可下嘴的点」，让吐槽有靶子；
- 限制 1~2 句、不要问好/分析/总结/Markdown；
- `available_tools = []`（关掉搜索与 Agent 工具，避免吐槽时误调用）、`max_tokens=160`、跳过前置联网、跳过历史图片关联。

**实测（687 条真实消息回放）**

| 指标 | 值 |
|---|---|
| 判定器调用 | 4 次（0.6%） |
| 放行吐槽 | 4 次 |
| 主要拦截 | 间隔 < 10 分钟 438、太短 147、含 @ 81、冷却/日上限 17 |

**回滚**

`.env` 设 `ENABLE_TOPIC_ROAST=false` 即可整层关闭，代码无需回退；只想更保守就调大 `TOPIC_GAP_SEC` 或调高 `TOPIC_ROAST_MIN_SCORE`。
### 2026-11-25｜唤醒放宽 + L2/L2.5 去抖（第 2 轮）

**1. L1 新增「以名字开头」强制唤醒**

`_is_triggered` 现在有两条路径：文本含唤醒词（`@大肥鱼`），或**以机器人名字开头**（`大肥鱼，BLG夺冠了`）。

- `大肥鱼今天怎么没动静` → 醒（开头是名字）
- `今天大肥鱼没来` → 不醒（名字在句中，属于议论而非叫它）
- `_strip_trigger` 同步剥掉开头的称呼，prompt 里不会重复出现「大肥鱼」

**2. L2.5 取消两条过滤**

| 原过滤 | 现状 |
|---|---|
| 含 `@` 就不判 | **删除**。@ 别人也进判定，交给判定器看话题本身 |
| 以名字开头就不判 | **删除**。这条已被 L1 的「开头唤醒」接管，走不到这里 |

同时改 `brain.judge_topic_roast` 的评分规则：把「在 @ 某个具体的人」从 0 分项里去掉，改为给模型一句中性提示「这条 @ 的是群里其他人，不是叫你，照常只看话题有没有槽点」。

**3. L2 / L2.5 去抖（debounce）**

问题：群里连着发话时，第一条就到了判定，判定完立刻回，等于插在人家话中间。

现在的行为：

```
消息1 → 入队(due = now + 2.5s)，不回复
消息2 → 续期(due 再 +2.5s)，并把这句并进 burst
消息3 → 同上
（停顿 2.5s）
适配器巡检 → pop_due_triggers() 执行判定 → 通过才回复
```

实现要点：

- `bot_core._pending_trigger`：每群一条挂起记录，`_pending_lock` 保证跨线程安全（消息回调在 wxauto 线程，drain 在主巡检线程）。
- `_defer_trigger` 登记，`_touch_pending` 续期并累积 `burst`（最多 3 条），`_cancel_pending` 在被 @/指令/引用快路回复时作废，避免二次发言。
- `pop_due_triggers()` 由适配器每秒调用，返回 `[(group_id, BotResponse)]` 并直接发送。
- **探针用整波合并文本**：`我昨天钓了条三十斤的\n结果回家一称六斤半`。只取第一条会丢掉包袱（「现在又好了草」这类反转常在最后一句）。
- 合并文本与历史里已落库的单条重复，`_compose_reply(history_strip=...)` 会先把这些单条从历史尾部摘掉，避免模型以为群友说了两遍。
- 回复流程抽成 `_compose_reply`，`handle_message` 与 `pop_due_triggers` 共用，养成数值/表情包/历史落库行为一致。
- 「引用我的消息」是零成本强信号，**不参与去抖**，立刻醒。

**新增配置**

```ini
ENABLE_TRIGGER_DEBOUNCE=true
TRIGGER_DEBOUNCE_SEC=2.5
TRIGGER_DEBOUNCE_MAX_WAIT_SEC=12
```

防饿死：如果群里一直不停顿，挂起的判定最多等 `TRIGGER_DEBOUNCE_MAX_WAIT_SEC`（从第一句算起）就强制出队判定，否则热闹群里它会永远排不上（实测：每 2 秒一句的连续聊天，第 12 秒强制判定；说完就停的则 3 秒内判定）。

**适配器改动**

`adapters/wxauto_adapter.py` 两处巡检循环各加一段 drain（新版回调模式的 1s 循环、旧版 3.9 轮询循环）。其它通道未接（iLink 走 `force_trigger=True` 永远命中 L1，QQ 未启用）。

**回滚**

- 关闭去抖：`ENABLE_TRIGGER_DEBOUNCE=false`，回到「来一条判一条」
- 关闭名字开头唤醒：把 `_is_triggered` 里 `return any(probe.startswith(p) ...)` 一行删掉

### 2026-11-25｜停顿收紧 + L2.5 探针纠错 + 冒泡发错群修复（第 3 轮）

**1. 停顿 2.5s → 1.5s**

`TRIGGER_DEBOUNCE_SEC` 默认值改为 `1.5`。带上一次 1 秒巡检，实际接话延迟约 2~2.6 秒。

**2. L2.5 合并探针记录说话人（修「送错数据」）**

原实现 `burst` 只存文本，判定时却用「最后一条的发送者」拼成 `现在 [某人] 重新开口说：A句\nB句\nC句`。
多人接话时这句话是错的：三个人的话被说成一个人讲的，判定器还会张冠李戴地找槽点。

现在的行为：

| 场景 | 处理 |
|---|---|
| 同一人连发 | 按行拼接，保持「一口气说完」的语义 |
| 多人接话 | 每行加 `[昵称]: ` 前缀，并把 `multi_speaker=True` 传给判定器 |
| 判定器 prompt | 多人时改成「然后这几个人陆续开口（每行是谁说的都标在前面）」，不再用「[某人] 重新开口说」 |
| 垃圾消息 | `/指令`、原始 XML、无汉字内容不并入探针（但仍续期，因为群确实在说话） |
| 历史去重 | 探针里的句子从「更早的几句」中整体移除（剥掉 `[昵称]: ` 前缀后比对原文） |

另外：**L2 的判定对象改成只取最后一句**（「现在这句是不是在接我话」），前几句已经落库成历史上下文，再拼进探针只会重复。

**3. 去掉 L2.5 的同群冷却与每日上限**

删除 `TOPIC_ROAST_COOLDOWN_SEC`、`TOPIC_ROAST_MAX_PER_DAY` 两个配置，以及对应的两个状态字典。
`TOPIC_GAP_SEC` 成为唯一频控：只要群里冷场超过 10 分钟，下一次开话题就会进判定。

> 注意：这意味着一个活跃群一天可能吐槽多次。嫌多就调大 `TOPIC_GAP_SEC`（如 1800）或调高 `TOPIC_ROAST_MIN_SCORE`。

**4. 自主冒泡发到非定义群的修复**

三层原因，逐层堵：

| 层 | 问题 | 修法 |
|---|---|---|
| 发送点 | 所有 `wx.SendMsg(who=...)` 无条件发出，群名不存在时 wxauto 可能退化发到当前打开的会话 | 统一走 `_safe_send` / `_safe_send_file`，先校验白名单 |
| 接收点 | `_dispatch_message` 不校验群名，白名单外的会话也被处理（回消息、记历史、算冒泡热力） | 入口硬校验，非白名单直接丢弃 |
| 群名有效性 | `AddListenChat` 失败只 log，群仍留在 `self.groups` 里继续被冒泡 | 只保留监听成功的群；一个都没成功则清空 `self.groups`（全程静默，宁可不说也不发错） |
| 群名写错 | 监听静默失败时 `self.groups` 仍含错名，白名单挡不住 | 新增 `_is_live_group`：启动后收到过消息的群才允许主动冒泡 |

改动点：

- `_is_allowed_group` / `_reject_group` / `_safe_send` / `_safe_send_file` / `_is_live_group` / `_seen_groups`
- 覆盖全部 8 处发送：去抖接话、定时任务、自主冒泡、Git 更新预告、Git 回滚告警、DSH 投递、重启回归通知、消息回复
- 定时任务的目标群若已不在白名单，直接丢弃并告警（不能发到别的会话）

**为什么白名单不放 bot_core**

`RUN_MODE=both` 时微信和 QQ 通道同时在跑，而 bot_core 是共享单例。把白名单放进 bot_core 会连带拦掉 QQ 群的冒泡，所以只放在微信适配器的发送点。

**回滚**

- 接话太快：`TRIGGER_DEBOUNCE_SEC=2.5`
- 吐槽太频繁：`TOPIC_GAP_SEC=1800` 或 `TOPIC_ROAST_MIN_SCORE=0.85`
- 想退回旧的多行合并（不带昵称）：把 `_compose_burst` 里 `multi` 分支改成 `probe = "\n".join(b["text"]...)`
- 冒泡完全不发：检查日志里的「从未收到该群消息」与「不在 WX_GROUP_WHITELIST」两类告警

---

## 10. 日记不生成：思考链吃光 max_tokens（2026-11-25 排查）

**现象**：开机两天，`data/日记/` 一直是空的，`/日记` 永远回「还没写过日记」。

**定位链路**

1. `data/日记/` 目录被 `memory.py` 建出来了，但 `大肥鱼.jsonl` 不存在 → `write_diary` 从未被成功调用。
2. 11-24 23:45 的日志里有 `L1 夜间巩固完成` + `记忆备份完成`，**但没有** `📔 L2 日记已写入`。
   `consolidate()` 是纯 Python 不调 LLM，所以那次 23:45:34 的 API 请求就是写日记，它返回了但没写盘。
3. 复现请求，拿到决定性证据：

| 字段 | 值 |
|---|---|
| `finish_reason` | `length` |
| `content` | `''`（空串） |
| `reasoning_content` | 200+ 字，正在推敲拍哪个细节 |
| `completion_tokens` | 220，其中 `reasoning_tokens=220` |

**根因**：`deepseek-flash` 是带思考链的模型，reasoning token 与正文**共用** `max_tokens`。
日记请求只给了 `max_tokens=220`，思考写完额度就归零，正文一个字都没轮上。
`write_daily_diary` 拿到空正文 → `memory_store.write_diary("")` 返回 False → 静默失败。

**同一原因波及的功能**（都是小 `max_tokens` 的轻量调用）

| 调用 | 原 max_tokens | 表现 |
|---|---|---|
| `write_daily_diary` | 220 | 日记永远空 |
| `generate_autonomous_bubble` | 140 | 冒泡只能发兜底话术，日志里 20:03「在干嘛呢喵」、20:39「晚上好喵」就是兜底 |
| `is_talking_to_or_about_me` | 256（HEAD）/ 24（改造后） | 思考超限时返回空 → 判定为 NO，L2 静默失效（今日日志 12 条空结果） |
| `judge_topic_roast` | 40 | 分数恒为 0，吐槽永不触发 |
| `judge_and_chime_in` | 160 | 同上 |

**修法**

1. `brain.py` 新增统一入口 `_create(messages, max_tokens, temperature, thinking=False)`：
   轻量任务默认带 `extra_body={"thinking": {"type": "disabled"}}`。
   实测同一句输出：**关思考 6 token / 开思考 62 token**，又便宜又不会空。
2. 网关不认该参数（400/422 且报文点名 thinking）时自动降级为「加大 `max_tokens` 给思考留空间」，并打一次告警，之后全程走降级路径（`_thinking_off_supported=False`）。
3. 主回复 `reply()` 只在 `is_code_or_file_task` 时保留思考；闲聊/吐槽/看图一律关掉，
   顺带解决「非主人 260 上限被思考吃光 → 回复变空」的隐患。
4. `write_daily_diary` 增加空内容告警（打印 `finish_reason` 与完成 token），下次再空能一眼看出。
5. 夜间养护从 `except Exception: pass` 改为 `run_nightly_maintenance()` 分步落日志；
   **某群当天没有聊天记录时不占用当天名额**（原来单例 `_last_nightly_date` 会被第一个空群吃掉一整天）；
   支持凌晨 0~4 点补跑（逻辑日算前一天），进程半夜重启也不丢当天日记。
6. 新增 `/写日记` 命令：不等 23:45，随时手动触发一篇，便于验证。

**验证结果**

- `data/日记/大肥鱼.jsonl` 已真实写入 1 篇（心情「懵」，正文见文件）。
- L2 判定 `直接给他下命令就行` → `True`（修复前为空 → False）。
- L2.5 吐槽雷达：`5.0/10`（低于阈值 7）不接话；`8.0/10` 正常给出槽点「把鱼缸当显示器还养死了鱼」。
- 夜间养护端到端：日记成功 → L1 巩固 3 条 → 备份 `dafeiyu_memory_20261125_2018.zip`。

**注意**：当前运行的进程（11-25 02:32 启动）跑的还是 HEAD 旧代码，需重启才会加载以上改动。

---

## 11. 每日 L2/L2.5 子代理调用统计（2026-11-25）

**需求**：每天 0:00 输出一份 txt，含当天 L2 / L2.5 子代理的调用次数与回复率，纯代码统计，不走 LLM。

### 埋点位置

只在两个「唯一收口」处计数，直连路径与去抖路径都会经过，不存在漏记或双计：

| 计数键 | 埋点 | 时机 |
|---|---|---|
| `l2` | `bot_core._followup_judge` 里 `brain.is_talking_to_or_about_me(...)` 调用之后 | 确认真的发出了判定请求才计数，闸门挡下的不算 |
| `l2_5` | `bot_core._roast_judge` 里 `brain.judge_topic_roast(...)` 调用之后 | 同上 |

口径：

- `call` = 真正发给模型的请求数
- `hit` = 判定通关、触发大肥鱼开口的次数
- **回复率 = hit / call**

### 存储

`metrics.py`（纯标准库）按天落盘到 `data/统计/counters_YYYY-MM-DD.json`：

```json
{"l2": {"call": 12, "hit": 4, "groups": {"橸䒜鎵㘣": {"call": 10, "hit": 3}}}, "l2_5": {...}}
```

每次记录都是「读盘 → 累加 → 原子写回（tmp + os.replace）」，进程随时重启都不丢计数。

### 触发与产出

- `check_autonomous_heartbeat` 里调 `_maybe_write_daily_metrics()`，位置在**静默期判断之前**，所以 0 点（静默期内）也会照常执行。
- 逻辑日 = 前一天；0~4 点之间都可以补跑，进程半夜重启也不丢这份日报。
- 输出 `data/统计/子代理调用统计_YYYY-MM-DD.txt`，同时打一条日志。
- 报告内容：L2 / L2.5 各自的调用、接通、回复率，合计行，分群明细，以及「当日群消息 N 条 / 每百条消息几次判定」的参考值（`pet_engine.count_history_between` 直读历史表）。
- 报告里固定标注「纯代码统计，未调用模型」。

### 随时查看

- `/统计`：今天的实时累计（标注「截至 HH:MM，当日未结束」）
- `/统计 昨天`、`/统计 2026-11-24`：指定日期

### 注意

0:00 属于静默期（`QUIET_START_HOUR=23`），所以 txt 只落盘、**不往群里发消息**。
要看直接发 `/统计`，或去 `data/统计/` 取文件。若确实想让它在群里播报，需要单独放开静默期或改走主人私聊。

> 2026-11-25 后续已调整：静默期改为 1:00~8:00，0 点专门留给日报，txt 会连文件一起发到群里。详见第 12 节。

---

## 12. 静默期改 1:00~8:00 + 日报发群 + 连发消息只判一次（2026-11-25）

### 12.1 静默期

`QUIET_START_HOUR` 23 → **1**，`QUIET_END_HOUR` 保持 8。0 点这一小时从静默期里挪出来，专门留给每日统计。
`is_quiet_hours()` 走的是 `start <= hour < end` 分支（1 < 8，不再跨界），逻辑无需改动。

### 12.2 0 点的 txt 现在会发到群里

- `BotResponse` 新增 `file_path` 字段（与 `sticker_path` 独立）。
- `_maybe_write_daily_metrics()` 改为返回生成的 txt 路径。
- `check_autonomous_heartbeat` 拿到路径且**不在静默期**时，直接返回一条带 `file_path` 的 `BotResponse`：
  文案「📊 YYYY-MM-DD 的 L2/L2.5 子代理调用统计出来了（见附件）」+ 附 txt。
- 适配器巡检循环里补上 `hb.file_path` 的发送分支（`_safe_send_file`，同样走白名单校验）。
- 若进程是凌晨 1~4 点补跑那份报告，仍**只落盘不推送**（仍在静默期），避免半夜扰民。

### 12.3 「连发只判一次」——去抖语义调整

**改动前**：停顿阈值 1.5s，两条消息间隔 3s 时，第一条在 1.5s 就被判掉，第二条再判一次 → **同一波连发触发 2 次判定**。

**改动后**：停顿阈值 4.0s（必须大于常见连发间隔才能合并），单波上限 5 条（与 L2 的 5 条上下文窗口一致）。

| 情况 | 行为 | 判定次数 |
|---|---|---|
| 两条消息间隔 3s，之后安静 | 第 2 条后满 4s 才判，两条并进同一探针 | **1 次** |
| 连发攒满 5 条 | 达到上下文上限，立刻整批送判，不等停顿 | 1 次 |
| 一直不停顿 | 从第一句起满 12s（`TRIGGER_DEBOUNCE_MAX_WAIT_SEC`）强制送判 | 1 次 |
| 一波里先像「冷场开话题」、后来像「接我话」 | kind 升级为 `l2`（信号更明确），burst 不丢 | 1 次 |

实现细节：

- `_append_burst_locked`：攒满 `TRIGGER_BURST_MAX_ITEMS` 时置 `flushed=True` 且 `due=0`，适配器下一次 1s 轮询就发车（原来超限是**静默丢弃**）。
- `_touch_pending` / `_defer_trigger`：发现 `flushed` 不再续期，避免「已经攒满的一批被后来的消息不断顺延」。
- `_defer_trigger`：kind 不同不再整条覆盖（原来 `roast` 挂起时来了 `l2` 会把前面的 burst 冲掉），改为保留 burst、kind 取更强的一方。
- L2 判定对象仍是**最后一句**（前面的几句已在历史上下文里，合并探针会把它们一起送进 prompt）；L2.5 判定对象是**整波合并探针**（多人时带 `[昵称]:` 前缀）。

### 12.4 验证

模拟时间轴（打桩判定器，不调模型）：

- 3s 间隔两条 → t=1.4 / t=5.0 均不触发，t=7.1 触发一次，探针含两条 ✔
- 5 条连发 → 攒满即触发，一次送 5 条 ✔
- 不停顿 → t=11.9 未触发，t=12.1 触发一次 ✔
- roast→l2 升级 → burst 两条都在 ✔
- 攒满后新消息 → `due` 仍为 0，不顺延 ✔

0 点链路：模拟 00:05 心跳 → `is_quiet_hours()=False`，返回 `BotResponse(text=..., file_path=...子代理调用统计_2026-11-25.txt)`，文件存在 ✔

---

## 13. 思考草稿污染历史 + 工具 schema 瘦身（2026-11-25）

### 13.1 修掉：视觉模型的 `<think>` 草稿被写进聊天历史

**现象**：`chat_history` 里有 13 条图片消息长这样——

```
[发了一张图片：<think>用户现在需要分析这张聊天截图的内容，然后按要求回答。首先看图片里的元素…
```

不是图片摘要，是智谱 `glm-4.1v-thinking-flash` 的思考草稿。群里当时就有人吐槽「这是思考过程没藏住 漏出来了」。

**根因**：`_inspect_image_with_zhipu` 用 `re.sub(r"<think>.*?</think>", "")` 剥离思考，
这个正则**必须要求闭合标签**。该模型 `max_tokens=1024` 一截断，`</think>` 根本没输出，
正则不匹配 → 整段草稿原样返回 → `[:220]` 截断 → 写进 `chat_history`。

后果不是一次的：这条记录会作为「群友说的话」**永久留在历史里，每一轮都喂给模型**。

**修法**

| 位置 | 改动 |
|---|---|
| `brain.py` | 新增 `strip_think()`：闭合块删除 → 未闭合 `<think` 之后整段丢弃 → 孤立 `</think>` 只取后半 → 清残留标签 |
| `brain.py` | `_inspect_image_with_zhipu`：剥完若不足 6 字，判定「这轮全是思考」→ 返回 `None`，回退 DeepSeek 原生视觉，并打告警带 `finish_reason` |
| `brain.py` | `inspect_image_detail` 的回退路径同样过一遍 `strip_think` |
| `pet_engine.py` | 新增 `scrub_think()`，在 `update_history_content_by_image` 落库前再洗一遍（双保险，别的路径写脏数据也拦得住）；洗空了就退化成 `[发了一张图片]` |
| 数据库 | 一次性清洗 13 条历史，全部归整为 `[发了一张图片]`；清洗前备份 `data/dafeiyu.db.bak_20261125_205513` |

**验证**（`strip_think` 8 条用例 + 智谱返回值 4 种形态）

| 输入 | 输出 |
|---|---|
| `<think>草稿</think>正式内容` | `正式内容` |
| `<think>用户现在需要分析这张聊天截图…`（未闭合） | 空 → 回退原生视觉 |
| `正式内容<think>还没写完` | `正式内容` |
| `草稿</think>正式内容` | `正式内容` |
| `<think>a</think>正文<think>b</think>` | `正文` |

### 13.2 工具 schema：3011 → 1100 tokens

**问题**：一次普通回复的输入里，25 个工具的 function schema 占 **3011 tokens（47%）**，
比 20 条聊天历史（653 tokens）贵 4.6 倍，而且每轮全量重发。

**两刀**

**第一刀：合并同类，压缩文案（25 → 20 个，8117 → 6155 字符）**

| 旧 | 新 | 说明 |
|---|---|---|
| `add_schedule` / `list_schedules` / `delete_schedule`（1329 字符） | `schedule(action=add\|list\|delete)` | 三合一 |
| `search_and_send_image` / `download_image_from_url` | `send_image(query\|url)` | 二合一 |
| `create_and_send_file` / `send_existing_file` | `send_file(path\|filename+content)` | 二合一 |
| `self_restart` / `self_pull_and_restart` | `self_ops(action=restart\|update)` | 二合一 |

老名字在执行分发里全部保留（`fn_name in (...)`），模型按旧习惯调用也不会失效。
合名工具进 `assess_danger` 前会**归一化回动作类型**（如 `send_file` 带 `path` → `send_existing_file`），
保证二次确认码与路径风险检查照常生效——已实测：`send_file(C:/Windows/.../hosts)` 正确弹出高危确认卡。

**第二刀：主人专用工具不再常驻（真正的杠杆）**

13 个电脑操纵工具（`run_shell` / `self_edit_code` / `delegate_to_dsh` …）平时群聊根本用不上，
新增 `AGENT_TOOL_CUES` + `_needs_agent_tools(text)`：句子里出现「文件/代码/报错/重启/截图/盘/链接」这类迹象才挂载。

**实测 prompt_tokens**

| 场景 | 改造前 | 改造后 | 变化 |
|---|---|---|---|
| 闲聊（7 个工具） | 6379 | **4254** | −33% |
| 干活（20 个工具） | 6379 | **5348** | −16% |
| 其中工具本身 | 3011 | **1100 / 2121** | −63% / −30% |

判定用例 8 条全部正确（「那今晚还打不打刺客信条」不挂、「帮我看看 D 盘还有多少空间」挂）。

**权衡**：闲聊时模型看不到电脑工具，纯聊天不受影响；但若有人用很隐晦的说法让它操作电脑
（完全不含上述关键词），它就不会去调工具。关键词表已放宽到 90 余个，覆盖常见说法。
