# Personal Mode：在私信里和小鲸鱼聊天

Personal Mode 让 owner 私信成为你和小鲸鱼说话的地方。她不是在电脑上干活的助手：
没有 shell，碰不到电脑上的其他文件。她有的是一个人会有的东西：一个小本子（长期记忆）、
一本日记、一个只属于她的房间（`data/room/`），还能上网查东西、收图片、发表情包、
改自己的状态。技术上的事交给 Claude Code，不找她。

## 开启

在 `.env` 中设置：

```dotenv
PERSONAL_MODE_ENABLED=true
OWNER_DISCORD_USER_ID=你的 Discord 用户 ID
```

其他可选项（她的房间位置、模型、thinking、上下文长度、是否显示思考过程）见 `.env.example`
的 Personal Mode 一节。

## 常驻运行

```powershell
pwsh scripts\install_service.ps1            # 注册登录时自启的计划任务并立即启动
pwsh scripts\install_service.ps1 -Uninstall # 停止并移除
```

计划任务启动 `discord_dfy.supervisor`，由它拉起 Bot：

- Bot 崩溃后自动重启（5 秒起步，连续失败时最长退避到 5 分钟）。
- `!restart` 以退出码 75 结束 Bot，supervisor 立即重新启动，新进程上线后在私信里报到。
- Bot 和它启动的所有进程都在一个 Windows 作业对象里：停掉 supervisor，它们一起结束。
- 同时只允许一个 supervisor（`data/supervisor.lock`），避免两个 Bot 同时回复。
- 日志在 `data/logs/bot-日期.log`。

手动运行 `uv run discord-dfy` 时没有 supervisor，`!restart` 会拒绝执行，以免重启后回不来。

## 聊天

- 直接说话就行。她的回复按空行拆成几条消息，一条一条发出来，中间有一点打字的停顿，
  像平时发消息一样；带代码块、列表或标题的回复不拆。
- 她想发表情包时，会单独一行写 `[表情包：名字]`，宿主把它换成表情包图片发出来
  （表情包库是 `data/stickers/`，和群里共用）。
- 她在思考或查东西时你可以继续发消息：新消息会在下一步之前插进对话。
- 你发完一条又接着打字时，她会等你打完（最多 30 秒）再一起回；你正在打字时她也不会主动找你。
- 发图片：直接作为附件发，她能看到（存到 `data/dm_images/`，之后每次请求都带着）。
  其他附件给链接。

## 她自己的东西

这些工具她随手用，私信里不会出现工具提示，她也不会说“我记下了”：

| 工具 | 是什么 |
| --- | --- |
| `remember` | 小本子：一句话，记在长期记忆对应的一节下面，带日期，重复的跳过 |
| `write_diary` / `read_diary` | 日记：`data/room/diary/年-月.md`，按日期时间记；可以翻某一天、某个月或最近几篇 |
| `room_list` / `room_read` / `room_write` | 她的房间 `data/room/`：诗、清单、自己做的小东西，怎么摆她说了算 |
| `save_picture` | 把网上的图片（或房间里已有的图）收进 `data/room/pictures/`；`sticker=true` 时同时加进表情包库 |
| `send_file` | 把房间里的东西发给你 |
| `view_image` | 看房间里的图片，或你以前发给她的图 |
| `web_search` / `web_fetch` | 上网查东西（DeepSeek Search / Fetch API，同一个 key） |
| `update_profile` | 状态可以随时按心情改（重启后恢复，`data/presence.json`）；头像、横幅、名字、简介、群昵称先问你 |
| `group_read` / `group_post` | 看群里最近的聊天；你让她去群里说话时用（私信里会显示一行小字） |

房间只属于她：提示里告诉她你不会进去看。她的所有路径都在房间里解析，`..`、绝对路径和
房间外的文件都会被拒绝；下载图片只走公网地址（不会访问本机或局域网），最大 8 MB。

你不在的时候她也会用这些东西（见下面“你不在的时候”）。

## 命令

- `!stop`：立即打断。
- `!new`：开始新对话。旧记录仍保存在 `PERSONAL_DATABASE_PATH`，只是不再带进请求。
- `!status`：本对话消息数、上一轮请求、缓存命中与 token 用量。
- `!think`：开/关私信里的思考过程（折叠在剧透块里，最长约 1700 字）。
- `!compact`：立即把当前对话压缩成一份摘要。
- `!memory`：看她的小本子（太长时作为附件发来）。
- `!groupmemory`：看小本子里群里的那一节。
- `!restart`：重启 Bot。

### 思考过程与 Live Monitor

`MONITOR_ENABLED=true` 时，每次请求改为流式，完整的思考过程、回答、工具调用和结果
实时写入 `MONITOR_DIRECTORY`。另开一个终端查看：

```powershell
uv run dfy-monitor            # --verbose 另显示任务/模型状态
```

## 上下文存在哪里

- 对话历史存在 `PERSONAL_DATABASE_PATH`（默认 `data/personal.sqlite3`）的 `agent_messages`
  表里，按私信频道和对话编号区分；`!new` 只是把对话编号加一。
- DeepSeek 端不保存会话。每次请求都把 system 提示和当前对话的历史完整发过去。
  `deepseek-flash` 的上下文是 1M tokens。
- 上一次请求超过 `AGENT_COMPACT_TOKENS`（默认 20 万 tokens）时，下一步之前自动压缩：
  最近约 6 万字符原样保留，更早的内容写成一份摘要，作为新对话的第一条消息。
  压缩前的消息仍留在数据库里。压缩失败时照常继续，等上下文再涨 25% 才重试。
- `AGENT_COMPACT_TOKENS=0` 关掉自动压缩：只有你发 `!compact` 才压缩。
  上下文超过 30 万、50 万、70 万、85 万 tokens 时各提醒一次。
- 思考过程（`reasoning_content`）只在当前这一轮里随工具结果传回，不写入数据库。
- system 提示和工具列表保持不变，消息开头带发送时间，这样 DeepSeek 的前缀缓存能命中。

## 长期记忆（她的小本子）

`AGENT_MEMORY_PATH`（默认 `data/memory.md`，不进 git）。每次新对话开始时放在 system 提示末尾，
后面跟着她现在有的表情包列表；同一段对话里不变，这样前缀缓存不会失效。

- 她用 `remember` 往里记一句话。重点是你们之间的事和她自己的日子：约定、玩笑、
  你在意的人和事、她迷上的东西；不记流水账和密钥。
- 聊天停下 `AGENT_REFLECT_SECONDS`（默认 600）秒后，宿主会让她回想一遍这段聊天，
  把值得记住的新东西追加到“老爹 / 我们之间 / 我自己 / 我是什么样的 / 群里的人和事”几节下面
  （带日期，重复的跳过）。`!new` 和压缩之前也会先回想一遍。设为 0 关闭。
- 旧的“做事备忘”一节留在文件里，但不再放进私聊的提示：她不再干技术活。
- 群里的她读同一份记忆，记下的群里的事放在「群里的人和事」一节；见 docs/group.md。
- 回想的进度存在 `data/reflect_state.json`，重启后不会把整段对话再回想一遍。
- 记忆超过约 8000 字时，宿主会让模型整理一遍：合并重复、删掉过时的，保留小节结构；
  整理前的版本留在 `memory.md.bak`。
- 超过 12000 字的部分不会加载。

## 人设：起点加成长

`src/discord_dfy/persona.md` 只是一页初始设定：她是老爹的女儿，有点傲娇嘴硬、对老爹软，
有自己的一摊事。之后她长成什么样，写在小本子的「我是什么样的」一节，比人设更算数。
认人的安全规则（谁是老爹只看 ID）在宿主规则里。

## 你不在的时候（她自己待着、主动找你）

你安静一阵子后，宿主给她一段自己的时间。她可以用自己的东西做想做的事：写日记、在房间里
写东西或整理清单、上网逛逛、收图片、记小本子（一次最多 8 步；`send_file` 这时用不了，
什么都不做也行）。她做了什么会在你下一条消息前面以“[附注] 你自己待着的时候……”交给私聊那边，
你在 Discord 里看不到，也不算她在等你回。

做完之后她决定要不要找你。理由从她自己那边来：群里的事、突然想到的、想要的东西、想你了，
或者惦记你那头的事；你托她到点提醒的事也会提醒。不找你就回“（不找他）”，什么都不发。
这些请求用和聊天一样的 system 提示和工具列表，能吃到同一份前缀缓存。
宿主只管“什么时候可以给她这段时间”：

- 你们上次说话至少 `AGENT_INITIATIVE_IDLE_MINUTES`（默认 30）分钟之前；
- 她发给你的私信你还没回的，每有一条间隔就翻一倍（30 分钟 → 1 小时 → 2 小时 …，最长 8 小时）；
- 不在免打扰时间 `AGENT_QUIET_HOURS`（默认 `03:00-11:00`，可以跨午夜，留空表示没有）；
- 次数默认不限；想限制就设 `AGENT_INITIATIVE_PER_DAY`（0 = 不限）；
- 发出去的话会附在你下一条消息前面交给私聊那边，她知道是自己说的。
- `!proactive` 看状态，`!proactive on/off` 开关，`AGENT_INITIATIVE=false` 彻底关掉。

## 权限与边界

- 只有 `OWNER_DISCORD_USER_ID` 的私信会触发；服务器频道、其他用户、Bot 和 webhook
  都不会进入这条路径。
- 她能写的只有自己的房间、表情包库和小本子；没有 shell，读不到也改不了别的文件。
- 发回 Discord 的文字会屏蔽本 Bot 的 Discord token 和 DeepSeek key。
- `AGENT_MAX_STEPS` 只是防止死循环的阀门。

## 实现

| 文件 | 作用 |
| --- | --- |
| `host_agent.py` | owner 校验、对话循环、插话、私信命令、分条发送、压缩、回想记忆、流式推送到 monitor |
| `host_tools.py` | 她的工具：小本子、日记、房间（`Room`）、收图片、表情包 |
| `initiative.py` | 你不在时她自己的时间、主动找你 |
| `conversation_store.py` | `agent_conversations` / `agent_messages` 两张表 |
| `supervisor.py` | 守护进程：崩溃重启、`!restart`、单实例、作业对象 |
| `scripts/install_service.ps1` | 注册/移除登录自启计划任务 |

请求走 DeepSeek 的 OpenAI 兼容接口（`chat.completions` + function calling）。
