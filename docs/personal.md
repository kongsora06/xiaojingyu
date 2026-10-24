# Personal Mode：在私信里和小鲸鱼一起干活

Personal Mode 让 owner 私信变成一个能动手的对话：小鲸鱼直接在这台 Windows 电脑上
执行 PowerShell、读写和编辑文件、把文件发回私信，像结对编程一样边做边汇报。

## 开启

在 `.env` 中设置：

```dotenv
PERSONAL_MODE_ENABLED=true
OWNER_DISCORD_USER_ID=你的 Discord 用户 ID
AGENT_WORKDIR=D:\Dev
```

其他可选项（模型、thinking、步数上限、命令超时、上下文长度、是否显示工具调用和
思考过程）见 `.env.example` 的 Personal Mode 一节。

## 常驻运行

```powershell
pwsh scripts\install_service.ps1            # 注册登录时自启的计划任务并立即启动
pwsh scripts\install_service.ps1 -Uninstall # 停止并移除
```

计划任务启动 `discord_dfy.supervisor`，由它拉起 Bot：

- Bot 崩溃后自动重启（5 秒起步，连续失败时最长退避到 5 分钟）。
- `!restart` 或 `restart_bot` 工具以退出码 75 结束 Bot，supervisor 立即重新启动，
  新进程上线后在私信里报到。小鲸鱼改完自己的代码，就能这样让改动生效。
- Bot 和它启动的所有进程都在一个 Windows 作业对象里：停掉 supervisor，它们一起结束。
- 同时只允许一个 supervisor（`data/supervisor.lock`），避免两个 Bot 同时回复。
- 日志在 `data/logs/bot-日期.log`。

手动运行 `uv run discord-dfy` 时没有 supervisor，`!restart` 会拒绝执行，以免重启后回不来。

## 用法

- 直接说话：闲聊或交代任务都行。它会先看现状、再动手、改完自己验证，然后汇报。
- 做事过程中可以继续发消息：新消息会在下一个工具步骤后插进对话，用来纠正或追加要求。
- `!stop`：立即打断，正在运行的命令连同子进程一起终止（shell 随之重置）。
- `!new`：开始新对话并重置 shell。旧记录仍保存在 `PERSONAL_DATABASE_PATH`，只是不再带进请求。
- `!status`：查看是否在干活、本对话消息数、上一轮请求、缓存命中与 token 用量。
- `!think`：开/关私信里的思考过程（折叠在剧透块里，最长约 1700 字）。
- `!compact`：立即把当前对话压缩成一份摘要（正在干活时，等这一步做完再压缩）。
- `!memory`：看长期记忆的内容（太长时作为附件发来）。
- `!groupmemory`：看群记忆；说“去群里说一声……”它会用 `group_post` 在群里发言（见 docs/group.md）。
- `!restart`：重启 Bot。
- 发附件时，附件链接会附在消息里，需要时它会自己下载。

- 发图片：直接作为附件发，它能看到（存到 `data/dm_images/`，对话里引用文件，
  之后每次请求都带着，同样的字节能吃到缓存）。其他附件仍然给链接。
- 工具里还有 `view_image`（看本机图片，比如自己截图看屏幕）、`web_search` / `web_fetch`
  （DeepSeek Search / Fetch API，同一个 API key，每千次约 $0.75）。

- `update_profile`：改她自己的 Discord 资料。状态（一句状态文字 + 在线/离开/请勿打扰）她可以随时
  按心情改，重启后会恢复（`data/presence.json`）。头像、横幅、全局名字（一小时最多两次）、
  About Me 简介、群昵称要先问你，你同意了才改；群昵称需要服务器给 bot **Change Nickname** 权限。

每次工具调用会在私信里显示一行小字（`AGENT_SHOW_TOOLS=false` 可关闭）。

### 常驻 shell

`powershell` 工具在同一个 pwsh 进程里执行，`cd`、变量、函数会一直保留。超时、`!stop`、
`!new`、命令里执行 `exit` 时会重置 shell，并结束它启动的子进程——包括用 `Start-Process`
放到后台的程序。要长期运行的服务请另行安排（例如计划任务）。

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
  最近约 6 万字符原样保留，更早的内容由模型（不开 thinking）写成一份摘要，作为新对话的
  第一条消息。压缩前的消息仍留在数据库里（属于上一个对话编号）。压缩失败时照常继续，
  等上下文再涨 25% 才重试。
- `AGENT_COMPACT_TOKENS=0` 关掉自动压缩（现在的 `.env` 就是这样）：只有你发 `!compact` 才压缩。
  上下文超过 30 万、50 万、70 万、85 万 tokens 时各提醒一次；`!status` 也会显示当前大小。
- 万一压缩一直失败，历史超过 `AGENT_CONTEXT_CHARS`（默认 200 万字符）时，
  最早的轮次不再发送。
- 思考过程（`reasoning_content`）只在当前这一轮里随工具结果传回，进入下一轮时去掉，
  也不写入数据库。
- system 提示保持不变，消息开头带发送时间，这样 DeepSeek 的前缀缓存能命中；
  `!status` 会显示命中的 token 数。

## 长期记忆

`AGENT_MEMORY_PATH`（默认 `data/memory.md`，不进 git）是小鲸鱼自己维护的笔记。
每次新对话开始时把它放在 system 提示末尾（群记忆跟在后面），所以 `!new` 和压缩之后它仍然记得。
同一段对话里记忆是固定的，它改了文件要到 `!new`、压缩或重启之后才会出现在提示里，
这样记忆更新不会让整段对话的前缀缓存失效。

- 它用 `edit_file` 自己更新。重点是你们之间的事：你最近在忙和在意什么、聊过的话题、
  约定和玩笑、它自己的感受；做事相关的（环境、项目、习惯）放在单独一节，保持简短。
  不记一次性细节、能直接查到的东西和密钥。
- 聊天停下 `AGENT_REFLECT_SECONDS`（默认 600）秒后，宿主会让它回想一遍这段聊天，
  把值得记住的新东西追加到“老爹 / 我们之间 / 我自己”几节下面（带日期，重复的跳过）。
  `!new` 和压缩之前也会先回想一遍。设为 0 关闭。模型自己聊天时常常顾不上调用工具记东西，
  这一步保证日常的事能记下来。
- 群里的她读同一份记忆（除了“做事备忘”），记下的群里的事放在「群里的人和事」一节；见 docs/group.md。
- 你发消息时，宿主会把“上次私聊之后群里的动静”附在前面；想看更多她会用 `group_read`。
- 回想的进度存在 `data/reflect_state.json`，重启后不会把整段对话再回想一遍（以前会，导致重复）。
- 记忆超过约 5000 字时，宿主会让模型整理一遍：合并重复、删掉过时的，保留小节结构；
  整理前的版本留在 `memory.md.bak`。结果丢了小节、没变短或者删得太狠都会被拒绝。

## 人设：起点加成长

`src/discord_dfy/persona.md` 只是一页初始设定（v4）：她是老爹的女儿，有点傲娇嘴硬、对老爹软，
像普通女孩一样说话，不老提 AI、token 这些。之后她长成什么样，写在记忆的「我是什么样的」一节，
由她自己（回想聊天时、或者在群里用 `remember` 写 "我：…"）补充和修改，比人设更算数。
认人的安全规则（谁是老爹只看 ID）在宿主规则里，不再写进人设。

## 主动找你

你安静一阵子后，小鲸鱼可能会主动私信你：接着问之前聊过的事、提醒你在意的事、分享想法，
或者单纯想你了。发不发由模型决定，宿主只管“什么时候可以问”：

- 你上次说话（或它上次说话）至少 `AGENT_INITIATIVE_IDLE_MINUTES`（默认 180）分钟之前；
- 不在免打扰时间 `AGENT_QUIET_HOURS`（默认 `03:00-11:00`，可以跨午夜，留空表示没有）；
- 24 小时内最多 `AGENT_INITIATIVE_PER_DAY`（默认 3）条；符合条件时最多一小时问一次模型；
- 它上一条主动消息你还没回，就不会再发；正在干活时也不会打断。
- 发出去的话会附在你下一条消息前面交给私聊那边，它知道是自己说的。
- `!proactive` 看状态，`!proactive on/off` 开关（重启后保留），`AGENT_INITIATIVE=false` 彻底关掉。
- 你也可以直接编辑这个文件，或者在私信里说“记住……”“把……忘掉”。
- 超过 12000 字的部分不会加载，它会被提示去整理精简。

## 权限与边界

- 只有 `OWNER_DISCORD_USER_ID` 的私信会触发；服务器频道、其他用户、Bot 和 webhook
  都不会进入这条路径。群聊 Social Mode 没有任何工具。
- 工具以运行 Bot 的 Windows 账户权限执行，没有沙箱、目录白名单或每小时预算。
  人格规则要求它在删除重要数据、强推、对外发送/发布、付款等不可逆操作前先问你。
- 发回 Discord 的文字会屏蔽本 Bot 的 Discord token 和 DeepSeek key。
- `AGENT_MAX_STEPS` 只是防止死循环的阀门：到达上限会暂停并提示，回一句“继续”即可。

## 实现

| 文件 | 作用 |
| --- | --- |
| `host_agent.py` | owner 校验、对话循环、插话、私信命令、压缩、长期记忆、流式推送到 monitor |
| `host_tools.py` | 常驻 `powershell`、`read_file`、`write_file`、`edit_file`、`send_file`、`restart_bot` |
| `conversation_store.py` | `agent_conversations` / `agent_messages` 两张表 |
| `supervisor.py` | 守护进程：崩溃重启、`!restart`、单实例、作业对象 |
| `scripts/install_service.ps1` | 注册/移除登录自启计划任务 |

请求走 DeepSeek 的 OpenAI 兼容接口（`chat.completions` + function calling）。
