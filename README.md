# 小鲸鱼 (discord-dfy)

A Discord companion bot on DeepSeek. She is one character with one memory who lives in two
places:

- **Group chat** (the channels in `ALLOWED_CHANNEL_IDS`): she reads the conversation like a
  member, always answers when @'d or replied to, and joins in on her own as often as the
  owner allows. She can quote, react, send stickers, look at recent images, run one web
  search before answering, change her Discord status, note things worth remembering, and
  privately DM the owner. In groups she only chats: no computer tools, no writing code.
  See the [group chat guide](docs/group.md).
- **Owner DMs** (Personal Mode, opt-in): a DeepSeek tool loop running directly on this Windows
  computer — PowerShell, file read/write/edit, sending files, viewing images, web search and
  fetch, posting to the group, editing her Discord profile with the owner's OK. She can also
  message the owner first after a quiet spell. See the [Personal Mode guide](docs/personal.md).

Both sides share the short starting persona in [`persona.md`](src/discord_dfy/persona.md) and one
long-term memory file (`data/memory.md`) that she keeps growing herself. Private DM talk is not
handed to the group side. The optional [Live Monitor](docs/monitor.md) shows her thinking,
decisions and tool calls as they stream.

## Setup (Windows, PowerShell)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then from the
repository root:

```powershell
uv sync --locked --group dev
Copy-Item .env.example .env
notepad .env
uv run --locked discord-dfy
```

To keep her running, install the logon task (a supervisor restarts her after crashes and after
`!restart`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_service.ps1
```

## Configuration

Required in `.env`:

```dotenv
DISCORD_BOT_TOKEN=your_bot_token
DEEPSEEK_API_KEY=your_deepseek_api_key
ALLOWED_CHANNEL_IDS=123456789012345678,234567890123456789
```

For owner DMs also set `PERSONAL_MODE_ENABLED=true`, `OWNER_DISCORD_USER_ID` and
`AGENT_WORKDIR`. Every other setting (chattiness, memory, reaching out, quiet hours, context
compaction, monitor, paths) is documented with its default in [`.env.example`](.env.example).
Model calls, image input and web search all use the one DeepSeek key (`deepseek-flash`, Search /
Fetch API).

### Discord application

1. Create an application in the [Developer Portal](https://discord.com/developers/applications),
   add a bot and copy its token. Enable **Message Content Intent**.
2. Install it to your server with the scopes **bot** and **applications.commands**.
3. In the approved channels grant **View Channel**, **Send Messages**, **Read Message History**
   (history after a restart, quoted replies), **Add Reactions** and **Attach Files** (stickers).
   **Change Nickname** lets her change her own server nickname. Do not grant Administrator,
   Manage Messages, Mention Everyone or member-management permissions.
4. Enable Developer Mode in Discord, copy the channel IDs into `ALLOWED_CHANNEL_IDS` and your own
   user ID into `OWNER_DISCORD_USER_ID`.

## Owner commands (in DMs)

| Command | What it does |
| --- | --- |
| `!stop` / `!new` | Interrupt the current task / start a fresh conversation |
| `!status` | Context size, token usage, settings |
| `!compact` | Summarize the older part of the conversation |
| `!memory` / `!groupmemory` | Show her memory / its group section |
| `!think` | Show or hide her thinking in DMs |
| `!group`, `!group on/off`, `!group level 0-3`, `!quiet 30m` | Group chat controls |
| `!proactive on/off` | Let her message you first |
| `!restart` | Restart the bot (when run under the supervisor) |

## Data

Everything personal stays local and out of Git (`.gitignore` covers `.env` and `data/`):
`data/memory.md` (her memory), `data/personal.sqlite3` (DM history), `data/bot.sqlite3`
(group messages and decision metadata), `data/stickers/`, `data/dm_images/`, logs and monitor
events. Set `STORE_MESSAGES=false` to stop storing group messages; the bot still reads recent
chat in memory to follow the conversation. Logs never include message content and redact
configured secrets.

## Layout

```text
src/discord_dfy/
  __main__.py            Wire everything up, start and stop
  supervisor.py          Keep the bot running; restart on crash or request
  config.py              Validate .env
  discord_bot.py         Discord events, slash commands, delivery
  group_chat.py          Group chat brain: timing, plans, stickers, search, status
  host_agent.py          Owner-DM tool loop, commands, looking back into memory
  host_tools.py          PowerShell and file tools on this computer
  initiative.py          Reaching out to the owner first
  memory.py              The shared memory file: notes, sections, tidying
  persona.md, persona.py Starting persona and host identity rules
  profile.py             Discord status and profile changes
  stickers.py            Sticker library (data/stickers/stickers.json)
  images.py, web.py      Image input and DeepSeek Search / Fetch
  conversation_store.py  DM conversation history (SQLite)
  storage.py, service.py, policy.py, models.py   Group message storage and filtering
  monitor.py, monitor_protocol.py, observability.py, streaming.py   Live Monitor
docs/                    Group chat, Personal Mode and Live Monitor guides
scripts/                 Logon task installer, monitor demo
tests/                   Offline tests (no credentials, no network)
```

## Tests

```powershell
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Tests use temporary SQLite databases, mocked Discord objects and the real OpenAI SDK over an
in-memory HTTP transport; they make no external calls.
