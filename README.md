# 🐋 大肥鱼（DeepSeek）微信群养成 + 识图机器人

一套从零定制的 **「养大肥鱼」** 微信群 AI 宠物系统（Windows 本地运行），支持：

- **🍚 养成数值系统（SQLite 持久化）**：饱食度（白饭值）、心情值、成长等级（Lv.1 小鱼苗 → Lv.6 吞噬 Token 之神）、每个群友独立好感度与本群饲主排行榜；每次对话消耗的 Token 自动折算成“吃白饭”。
- **🖼️ 群聊看图整活（DeepSeek 多模态）**：群里直接发图、或“先发图再 `@大肥鱼` 提问”都能自动关联识别，按大肥鱼人设吐槽、锐评、OCR 或解析截图。
- **🎭 情绪联动表情包回复**：内置 4 张定制 Q 版大肥鱼表情包（开心干饭 / 饿扁求饭 / 看图吃瓜 / 摸鱼打盹），也支持往 `stickers/<情绪>/` 里自由添加你喜欢的表情包。
- **💬 多通道接入**：
  - `web`：本地「养鱼控制台 + 模拟微信群」网页，开箱即玩；
  - `wxauto`：接入 Windows 桌面版个人微信群（支持群白名单、仅 `@` 触发、自动存图识图、发送文字与表情包）；
  - `both`：同时启动 Web 控制台 + Windows 微信群监听（推荐）；
  - `ilink`：可选的微信官方 iLink 协议扫码私聊模式。

---

## 快速上手

### 1. 安装依赖

```powershell
cd C:\Users\MECHREVO\.gemini\antigravity\scratch\dafeiyu-wechat-bot
pip install -r requirements.txt
```

如需接入 **Windows PC 微信群**，再安装 `wxauto`：

```powershell
pip install wxauto
```

### 2. 配置 DeepSeek API Key

复制 `.env.example` 为 `.env`：

```powershell
Copy-Item .env.example .env
```

打开 `.env` 填入你的 `DEEPSEEK_API_KEY`（以及你要监听的微信群名称 `WX_GROUP_WHITELIST`）。

### 3. 启动运行

- **先在网页版控制台试玩 & 测识图**：
  ```powershell
  python main.py --mode web
  ```
  浏览器打开 `http://127.0.0.1:8765`。

- **正式接入 Windows 个人微信群（同时保留 Web 面板看状态）**：
  1. 在电脑上登录桌面版微信，并保持微信窗口**不要最小化**；
  2. 在 `.env` 中把 `WX_GROUP_WHITELIST` 改成你的真实微信群名（多个群用英文逗号分隔）；
  3. 运行：
     ```powershell
     python main.py --mode both
     ```

---

## 群聊玩法指令

| 指令 | 效果 |
| --- | --- |
| `@大肥鱼 + 任意内容` | 正常聊天，语气随饱食度/心情/好感度实时变化 |
| 发图后 `@大肥鱼`（或直接发图） | 触发 DeepSeek 视觉模型看图吐槽 / OCR / 解析 |
| `/投喂` 或 `喂白饭` | 投喂一碗白饭，涨饱食度、心情和好感度 |
| `/摸鱼` 或 `摸摸鱼` | 撸一把大肥鱼，随机触发撒娇或拍手要饭 |
| `/状态` 或 `查鱼` | 查看大肥鱼等级、饱食度、吃掉的 Token 数 |
| `/鱼塘榜` | 查看本群好感度前 5 名饲主 |
| `/帮助` | 查看全部玩法说明 |

## 自定义大肥鱼表情包

把你喜欢的 `.png` / `.jpg` / `.gif` / `.webp` 表情包直接丢进对应目录即可自动随机发送：
- `stickers/happy/`（开心、干饭）
- `stickers/hungry/`（饿扁、求白饭）
- `stickers/shocked/`（看图吃瓜、震惊）
- `stickers/sleepy/`（摸鱼、犯困）
