import asyncio
import io
import json
import time
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx2
import pytest
from openai import AsyncOpenAI

from discord_dfy import host_agent
from discord_dfy.ai import BASE_URL
from discord_dfy.config import ConfigurationError, Settings, parse_quiet_hours
from discord_dfy.conversation_store import ConversationStore
from discord_dfy.discord_bot import DiscordAgent
from discord_dfy.host_agent import (
    COMPACT_PROMPT,
    STOPPED_RESULT,
    SUMMARY_PREFIX,
    HostAgent,
    fit,
    request_messages,
    split_for_compaction,
    texts,
    transcript,
)
from discord_dfy.host_tools import Room, ToolError, describe_call, download_image
from discord_dfy.initiative import SELF_TAG, Initiative, ago, in_peak, in_quiet_hours
from discord_dfy.journal import add_entries, day_path, journal_dir
from discord_dfy.monitor import Renderer
from discord_dfy.monitor_protocol import Publisher
from discord_dfy.portrait import STARTING_SELF
from discord_dfy.service import AgentService

OWNER = 20
CHANNEL = 200


def completion(content="", calls=(), reasoning=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for index, (name, arguments) in enumerate(calls)
        ]
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class Provider:
    """Scripted DeepSeek: each request pops the next response and is recorded."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    async def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        response = self.responses.pop(0)
        if isinstance(response, int):
            return httpx2.Response(response, json={"error": {"message": "boom"}})
        return httpx2.Response(200, json=response)


class Chat:
    def __init__(self):
        self.sent = []
        self.files = []

    async def send(self, text):
        self.sent.append(text)

    async def send_file(self, path):
        self.files.append(path)

    def typing(self):
        return nullcontext()


@pytest.fixture
def agent_settings(settings, tmp_path):
    return replace(
        settings,
        personal_mode_enabled=True,
        owner_discord_user_id=OWNER,
        personal_database_path=tmp_path / "personal.sqlite3",
        agent_room_dir=tmp_path / "room",
        agent_memory_path=tmp_path / "memory.md",
        agent_reflect_seconds=0,
        agent_listen=False,
    )


@pytest.fixture
async def store(agent_settings):
    conversations = ConversationStore(agent_settings.personal_database_path)
    await conversations.open()
    yield conversations
    await conversations.close()


@pytest.fixture
async def make_agent(agent_settings, store):
    created = []

    async def build(provider, **overrides):
        client = AsyncOpenAI(
            api_key="fake-test-key",
            base_url=BASE_URL,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(provider)),
        )
        agent = HostAgent(replace(agent_settings, **overrides), client, store)
        agent.pace = 0  # No typing pauses between texts in tests.
        created.append((agent, client))
        return agent

    yield build
    for agent, client in created:
        await agent.close()
        await client.close()


async def say(agent, chat, content):
    handled = await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content=content, chat=chat)
    worker = agent.workers.get(CHANNEL)
    if worker is not None:
        await asyncio.wait_for(asyncio.shield(worker), 30)
    return handled


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_id": 21},
        {"guild_id": 10},
        {"is_bot": True},
        {"webhook_id": 5},
        {"direct_dm": False},
    ],
)
async def test_only_owner_direct_dms_reach_the_agent(make_agent, overrides):
    provider = Provider()
    agent = await make_agent(provider)
    arguments = {"owner_id": OWNER, "channel_id": CHANNEL, "content": "hi", "chat": Chat()}
    assert not await agent.handle_dm(**{**arguments, **overrides})
    assert not agent.workers and provider.bodies == []


async def test_disabled_personal_mode_ignores_owner(make_agent):
    agent = await make_agent(Provider(), personal_mode_enabled=False)
    assert not await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="hi", chat=Chat())


async def test_she_keeps_notes_quietly_with_a_persons_tools(make_agent, store, agent_settings):
    provider = Provider(
        completion(
            "嗯哼", [("remember", {"text": "主人周六要去爬山", "section": "主人"})], "记一下"
        ),
        completion("爬山记得带水"),
    )
    agent = await make_agent(provider)
    chat = Chat()
    assert await say(agent, chat, "周六我去爬山")
    assert chat.sent == ["嗯哼", "爬山记得带水"]  # No tool trail for her own things.
    first, second = provider.bodies
    assert first["model"] == "deepseek-flash" and first["thinking"] == {"type": "enabled"}
    assert {tool["function"]["name"] for tool in first["tools"]} == {
        "remember",
        "rewrite_self",
        "write_diary",
        "read_diary",
        "recall",
        "room_list",
        "room_read",
        "room_write",
        "save_picture",
        "send_file",
        "view_image",
    }
    system = first["messages"][0]["content"]
    assert "powershell" not in system and "本机权限" not in system and "技术活不是你的事" in system
    tool_message = second["messages"][-1]
    assert tool_message == {"role": "tool", "tool_call_id": "call_0", "content": "记好了"}
    memory = agent_settings.agent_memory_path.read_text(encoding="utf-8")
    assert "## 主人\n- 主人周六要去爬山（20" in memory
    # Reasoning goes back within the turn, but is never persisted.
    assert second["messages"][-2]["reasoning_content"] == "记一下"
    history = await store.load(CHANNEL)
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert all("reasoning_content" not in m for m in history)
    assert agent.usage[CHANNEL] == {"calls": 2, "prompt": 20, "cached": 0, "completion": 10}


async def test_history_carries_into_next_message(make_agent):
    provider = Provider(completion("第一次", reasoning="r1"), completion("第二次"))
    agent = await make_agent(provider)
    chat = Chat()
    await say(agent, chat, "一")
    await say(agent, chat, "二")
    messages = provider.bodies[1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[2]["content"] == "第一次" and "reasoning_content" not in messages[2]
    assert messages[3]["content"].endswith("] 二")


async def test_new_starts_fresh_conversation_but_keeps_rows(make_agent, store):
    provider = Provider(completion("嗯"))
    agent = await make_agent(provider)
    chat = Chat()
    await say(agent, chat, "记住这个")
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!new", chat=chat)
    assert await store.load(CHANNEL) == []
    count = await (await store.db.execute("SELECT COUNT(*) FROM agent_messages")).fetchone()
    assert count[0] == 2


class SlowWeb:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def search(self, query, count):
        self.started.set()
        await self.release.wait()
        return [{"title": "结果", "url": "https://r.example", "summary": query}]

    async def fetch(self, url):
        return {"title": "页面", "content": "正文"}


async def test_stop_interrupts_a_running_tool_and_keeps_history_valid(make_agent, store):
    provider = Provider(
        completion("", [("web_search", {"query": "慢慢搜"})]),
        completion("继续了"),
    )
    agent = await make_agent(provider)
    agent.web = SlowWeb()
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="搜一下", chat=chat)
    await asyncio.wait_for(agent.web.started.wait(), 10)
    started = asyncio.get_running_loop().time()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!stop", chat=chat)
    assert asyncio.get_running_loop().time() - started < 15
    assert chat.sent[-1] == "停下了"
    history = await store.load(CHANNEL)
    assert history[-1] == {"role": "tool", "tool_call_id": "call_0", "content": STOPPED_RESULT}
    await say(agent, chat, "接着来")
    assert provider.bodies[-1]["messages"][-1]["content"].endswith("] 接着来")


async def test_message_during_a_turn_is_injected_after_tool_step(make_agent):
    provider = Provider(
        completion("", [("web_search", {"query": "A"})]),
        completion("收到新要求"),
    )
    agent = await make_agent(provider)
    agent.web = SlowWeb()
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="先搜A", chat=chat)
    await asyncio.wait_for(agent.web.started.wait(), 10)
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="改成B", chat=chat)
    agent.web.release.set()
    await asyncio.wait_for(agent.workers[CHANNEL], 30)
    roles = [m["role"] for m in provider.bodies[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert provider.bodies[1]["messages"][-1]["content"].endswith("] 改成B")


async def test_step_limit_pauses_runaway_loops(make_agent):
    call = completion("", [("room_read", {"path": "missing.txt"})])
    provider = Provider(call, call, call)
    agent = await make_agent(provider, agent_max_steps=2)
    chat = Chat()
    await say(agent, chat, "go")
    assert len(provider.bodies) == 2 and "先停一下" in chat.sent[-1]


async def test_provider_error_is_reported_without_crashing(make_agent):
    agent = await make_agent(Provider(500))
    chat = Chat()
    await say(agent, chat, "hi")
    assert chat.sent == ["DeepSeek 请求失败（HTTP 500），先停下"]
    assert CHANNEL not in agent.workers


async def test_send_file_and_tool_errors_are_returned_to_model(make_agent, agent_settings):
    room = agent_settings.agent_room_dir
    room.mkdir()
    (room / "诗.md").write_text("x", encoding="utf-8")
    secret = agent_settings.personal_database_path.parent / "secret.txt"
    secret.write_text("no", encoding="utf-8")
    provider = Provider(
        completion(
            "",
            [
                ("send_file", {"path": "诗.md"}),
                ("room_read", {"path": "nope"}),
                ("send_file", {"path": "../secret.txt"}),
                ("room_read", {"path": str(secret)}),
            ],
        ),
        completion("发了"),
    )
    agent = await make_agent(provider)
    chat = Chat()
    await say(agent, chat, "发我")
    assert chat.files == [(room / "诗.md").resolve()]
    results = [m["content"] for m in provider.bodies[1]["messages"] if m["role"] == "tool"]
    assert results[0] == "已发送 诗.md" and results[1].startswith("错误：没有这个文件")
    assert results[2] == results[3] == "错误：只能用房间里的路径"


async def test_outgoing_text_redacts_configured_secrets(make_agent):
    agent = await make_agent(Provider(completion("key is test-deepseek-key")))
    chat = Chat()
    await say(agent, chat, "?")
    assert chat.sent == ["key is [REDACTED]"]


def test_room_is_hers_and_nothing_outside_it_is_reachable(tmp_path):
    room = Room(tmp_path / "room", tmp_path / "stickers")
    assert room.list() == "房间还是空的"
    assert room.write("诗/秋天.md", "叶子落了") == "写好了：诗/秋天.md"
    room.write("诗/秋天.md", "\n风也凉了", append=True)
    assert room.read("诗/秋天.md") == "叶子落了\n风也凉了"
    assert room.list() == "诗/\n诗/秋天.md（25 字节）"
    for bad in ("../x.md", "..\\..\\x.md", "C:\\Windows\\win.ini", "诗/../../x.md", ""):
        with pytest.raises(ToolError):
            room.write(bad, "x")
    room.write("/etc/passwd", "x")  # A leading slash means the top of her room.
    assert (tmp_path / "room/etc/passwd").is_file()
    with pytest.raises(ToolError, match="文件夹"):
        room.write("诗", "x")
    (tmp_path / "room" / "猫.png").write_bytes(PNG)
    assert "view_image" in room.read("猫.png")


def test_diary_is_dated_and_can_be_read_back(tmp_path):
    room = Room(tmp_path / "room", tmp_path / "stickers")
    assert room.read_diary("", datetime(2026, 10, 26, 9)) == "还没写过日记"
    room.write_diary("今天下雨，有点想他", datetime(2026, 10, 25, 22, 10))
    room.write_diary("收了一张猫的图", datetime(2026, 10, 26, 1, 5))
    recent = room.read_diary(None, datetime(2026, 10, 26, 9))
    assert recent == "## 2026-10-25 22:10\n今天下雨，有点想他\n## 2026-10-26 01:05\n收了一张猫的图"
    assert room.read_diary("2026-10-25", datetime(2026, 10, 26)) == (
        "## 2026-10-25 22:10\n今天下雨，有点想他"
    )
    assert room.read_diary("2026-09", datetime(2026, 10, 26)) == "2026-09 没写日记"
    assert (tmp_path / "room/diary/2026-10.md").is_file()


def test_describe_call_is_single_line_and_bounded():
    text = describe_call("group_post", {"text": "a\n`b`\n" + "x" * 300})
    assert "\n" not in text and "`b`" not in text and len(text) < 200


def test_a_reply_becomes_separate_texts_and_stickers():
    assert texts("哼\n\n才不是\n[表情包：震惊]\n\n算了") == [
        ("text", "哼"),
        ("text", "才不是"),
        ("sticker", "震惊"),
        ("text", "算了"),
    ]
    assert texts("[表情包: 我不知道耶]") == [("sticker", "我不知道耶")]
    listing = "要带的：\n\n- 水\n- 伞"
    assert texts(listing) == [("text", listing)]  # Lists stay in one message.
    assert texts("第一行\n第二行") == [("text", "第一行\n第二行")]


async def test_replies_go_out_as_texts_with_stickers(make_agent, agent_settings):
    stickers = agent_settings.social_sticker_dir
    stickers.mkdir(parents=True, exist_ok=True)
    (stickers / "shock.png").write_bytes(PNG)
    (stickers / "stickers.json").write_text(
        json.dumps([{"name": "震惊", "file": "shock.png", "when": "吓一跳"}], ensure_ascii=False),
        encoding="utf-8",
    )
    provider = Provider(completion("啊？\n\n[表情包：震惊]\n\n你说真的？\n[表情包：没有这张]"))
    agent = await make_agent(provider)
    chat = Chat()
    await say(agent, chat, "我中奖了")
    assert chat.sent == ["啊？", "你说真的？"] and chat.files == [stickers / "shock.png"]
    system = provider.bodies[0]["messages"][0]["content"]
    assert system.endswith("<stickers>\n- 震惊：吓一跳\n</stickers>")


async def test_saving_a_picture_and_making_it_a_sticker(make_agent, agent_settings):
    provider = Provider(
        completion(
            "",
            [
                (
                    "save_picture",
                    {
                        "source": "https://img.example/cat.png",
                        "name": "趴着的猫",
                        "sticker": True,
                        "when": "犯懒",
                    },
                ),
                ("save_picture", {"source": "https://img.example/x", "name": "坏图"}),
            ],
        ),
        completion("存好啦"),
    )
    agent = await make_agent(provider)
    fetched = []

    async def download(url):
        fetched.append(url)
        return PNG if url.endswith(".png") else b"<html>"

    agent.download = download
    await say(agent, Chat(), "这只猫好可爱")
    results = [m["content"] for m in provider.bodies[1]["messages"] if m["role"] == "tool"]
    assert results[0] == "存好了：pictures/趴着的猫.png，也加进表情包了（趴着的猫）"
    assert results[1].startswith("错误：这不是")
    room = agent_settings.agent_room_dir
    assert (room / "pictures/趴着的猫.png").read_bytes() == PNG
    found = agent.stickers.find("趴着的猫")
    assert found["when"] == "犯懒" and found["path"].read_bytes() == PNG


async def test_downloads_never_reach_this_computer_or_the_local_network():
    for url in ("http://127.0.0.1/a.png", "http://localhost/a.png", "file:///C:/a.png"):
        with pytest.raises(ToolError):
            await download_image(url)


def test_fit_drops_oldest_turns_and_starts_with_user():
    history = [
        {"role": "user", "content": "x" * 100},
        {"role": "assistant", "content": "y" * 100},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "ok"},
    ]
    assert fit(history, 10_000) == history
    assert fit(history, 100) == history[2:]
    assert fit([{"role": "tool", "content": ""}, *history], 10_000) == history


def test_request_messages_strip_reasoning_from_earlier_turns():
    history = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b", "reasoning_content": "old"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "", "reasoning_content": "new"},
    ]
    result = request_messages(history)
    assert "reasoning_content" not in result[1] and result[3]["reasoning_content"] == "new"


def test_agent_settings_from_env(tmp_path):
    base = {
        "DISCORD_BOT_TOKEN": "token",
        "DEEPSEEK_API_KEY": "key",
        "ALLOWED_CHANNEL_IDS": "100",
        "PERSONAL_MODE_ENABLED": "true",
        "OWNER_DISCORD_USER_ID": "20",
        "AGENT_ROOM_DIR": str(tmp_path / "room"),
    }
    settings = Settings.from_env(
        {**base, "AGENT_MODEL": "deepseek-pro", "AGENT_THINKING": "false", "AGENT_MAX_STEPS": "5"}
    )
    assert settings.agent_room_dir == tmp_path / "room" and settings.agent_model == "deepseek-pro"
    assert not settings.agent_thinking and settings.agent_max_steps == 5
    assert Settings.from_env({**base, "AGENT_ROOM_DIR": ""}).agent_room_dir == Path("data/room")
    with pytest.raises(ConfigurationError, match="AGENT_REASONING_EFFORT"):
        Settings.from_env({**base, "AGENT_REASONING_EFFORT": "huge"})


async def test_discord_routes_owner_dm_with_attachment_links(agent_settings, storage, policy):
    bot = DiscordAgent(agent_settings, AgentService(storage, policy))
    bot._connection.user = SimpleNamespace(id=999)
    bot.personal = SimpleNamespace(handle_dm=AsyncMock(return_value=True))
    channel = MagicMock(spec=discord.DMChannel)
    channel.id = CHANNEL
    message = SimpleNamespace(
        guild=None,
        channel=channel,
        author=SimpleNamespace(id=OWNER, bot=False),
        content="看看这个",
        attachments=[SimpleNamespace(filename="a.png", url="https://cdn.example/a.png")],
        webhook_id=None,
    )
    await bot.on_message(message)
    call = bot.personal.handle_dm.await_args.kwargs
    assert call["owner_id"] == OWNER and call["channel_id"] == CHANNEL
    assert call["content"] == "看看这个\n[附件 a.png: https://cdn.example/a.png]"
    assert call["chat"].channel is channel


async def test_system_prompt_is_constant_and_messages_carry_time(make_agent):
    provider = Provider(completion("一"), completion("二"))
    agent = await make_agent(provider)
    chat = Chat()
    await say(agent, chat, "a")
    await say(agent, chat, "b")
    first, second = (body["messages"] for body in provider.bodies)
    assert first[0] == second[0]
    assert second[1]["content"].startswith("[20") and second[1]["content"].endswith("] a")


def chunk(delta=None, usage=None):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": None}],
        **({"usage": usage} if usage else {}),
    }


def sse_response(*chunks):
    body = b"".join(
        b"data: " + json.dumps(c, ensure_ascii=False).encode() + b"\n\n" for c in chunks
    )
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=body + b"data: [DONE]\n\n"
    )


class Sink:
    def __init__(self):
        self.events = []

    def offer(self, event):
        self.events.append(event)


class Observer:
    def __init__(self):
        self.sink = Sink()

    def publisher(self, source, *, session_id=None, task_id=None):
        return Publisher(self.sink, source, session_id=session_id, task_id=task_id)


def tool_delta(**fields):
    return {"tool_calls": [{"index": 0, **fields}]}


async def test_monitor_streams_thinking_text_and_tools(make_agent):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": 8}
    first_part = {"name": "room_read", "arguments": '{"pa'}
    responses = [
        sse_response(
            chunk({"reasoning_content": "先看"}),
            chunk({"reasoning_content": "文件"}),
            chunk(tool_delta(id="call_0", type="function", function=first_part)),
            chunk(tool_delta(function={"arguments": 'th": "x"}'})),
            chunk(usage=usage),
        ),
        sse_response(
            chunk({"content": "没有"}), chunk({"content": "这个文件"}), chunk(usage=usage)
        ),
    ]
    bodies = []

    async def provider(request):
        bodies.append(json.loads(request.content))
        return responses.pop(0)

    agent = await make_agent(provider)
    agent.observer = Observer()
    chat = Chat()
    await say(agent, chat, "看看 x")
    assert chat.sent[-1] == "没有这个文件"
    assert bodies[0]["stream"] is True
    assistant = bodies[1]["messages"][-2]
    call = assistant["tool_calls"][0]["function"]
    assert call == {"name": "room_read", "arguments": '{"path": "x"}'}
    assert assistant["reasoning_content"] == "先看文件"
    events = agent.observer.sink.events

    def joined(kind):
        return "".join(e["data"].get("text", "") for e in events if e["type"] == kind)

    assert joined("reasoning-delta") == "先看文件" and joined("text-delta") == "没有这个文件"
    kinds = {e["type"] for e in events}
    assert {"tool-call", "tool-result", "task-status", "model-status"} <= kinds
    assert all(e["source"] == "personal" and e["session_id"] == str(CHANNEL) for e in events)
    assert agent.usage[CHANNEL]["cached"] == 16


async def test_monitor_shows_her_first_reaction_and_moves_on(make_agent):
    responses = [
        httpx2.Response(200, json=completion("这么晚还来找我")),
        sse_response(chunk({"content": "在呢"})),
    ]

    async def provider(request):
        return responses.pop(0)

    agent = await make_agent(provider, agent_listen=True)
    agent.observer = Observer()
    await say(agent, Chat(), "还没睡")
    out = io.StringIO()
    renderer = Renderer(out, no_color=True, clock=lambda: 1e12)
    for event in agent.observer.sink.events:
        renderer.event(event)
        renderer.refresh(force=True)
    # Nothing left open: an unfinished stream would hold back everything after it.
    assert not renderer.streams and not renderer.pending
    shown = out.getvalue()
    assert shown.index("（先听懂）这么晚还来找我") < shown.index("在呢")


async def test_think_toggle_folds_reasoning_into_dm(make_agent):
    provider = Provider(completion("好", reasoning="a||b" + "x" * 3000))
    agent = await make_agent(provider)
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!think", chat=chat)
    await say(agent, chat, "?")
    preview = chat.sent[1]
    assert preview.startswith("-# 💭 思考过程\n||a| |b") and preview.endswith("monitor）||")
    assert len(preview) < 1900 and chat.sent[2] == "好"


async def test_restart_command_requires_supervisor(make_agent):
    agent = await make_agent(Provider())
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!restart", chat=chat)
    assert "守护进程" in chat.sent[-1]
    agent.restarter = AsyncMock()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!restart", chat=chat)
    agent.restarter.assert_awaited_once_with(CHANNEL)


async def test_bot_restart_marker_round_trip(agent_settings, storage, policy):
    bot = DiscordAgent(agent_settings, AgentService(storage, policy))
    bot.close = AsyncMock()
    await bot.request_restart(CHANNEL)
    assert bot.restart_requested and bot.close.await_count == 1
    sent = AsyncMock()
    bot.get_partial_messageable = MagicMock(return_value=SimpleNamespace(send=sent))
    await bot._announce_restart()
    bot.get_partial_messageable.assert_called_once_with(CHANNEL)
    assert "重启好了" in sent.await_args.args[0] and not bot.restart_marker.exists()
    await bot._announce_restart()
    assert sent.await_count == 1


def test_supervisor_single_instance_lock(tmp_path, monkeypatch):
    from discord_dfy import supervisor

    monkeypatch.chdir(tmp_path)
    first = supervisor.single_instance()
    assert first is not None and supervisor.single_instance() is None
    first.close()
    assert supervisor.single_instance() is not None


def test_supervisor_waits_and_takes_over(tmp_path, monkeypatch):
    from discord_dfy import supervisor

    monkeypatch.chdir(tmp_path)
    holder = supervisor.single_instance()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            holder.close()  # The other supervisor dies.

    lock = supervisor.wait_for_lock(sleep=sleep, interval=7)
    assert lock is not None and sleeps == [7, 7, 7]
    assert "waiting to take over" in next((tmp_path / "data/logs").iterdir()).read_text("utf-8")
    lock.close()


async def test_memory_is_loaded_once_per_conversation(make_agent, agent_settings):
    provider = Provider(completion("好"), completion("嗯"), completion("在"))
    agent = await make_agent(provider)
    chat = Chat()
    await say(agent, chat, "a")
    assert "<memory>\n（还是空的）\n</memory>" in provider.bodies[0]["messages"][0]["content"]
    agent_settings.agent_memory_path.write_text(
        "- 主人喜欢简短回复\n\n## 做事备忘\n- python 在 D:\\Dev\n", encoding="utf-8"
    )
    await say(agent, chat, "b")
    # Unchanged within the conversation, so the cached prefix stays valid.
    assert provider.bodies[1]["messages"][0] == provider.bodies[0]["messages"][0]
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!new", chat=chat)
    await say(agent, chat, "c")
    # The old work notes are not hers to act on any more.
    assert "<memory>\n- 主人喜欢简短回复\n</memory>" in provider.bodies[2]["messages"][0]["content"]
    assert "<group_memory>" not in provider.bodies[2]["messages"][0]["content"]


def test_memory_block_truncates_long_notes():
    block = host_agent.memory_block("x" * 20000)
    assert len(block) < 12200 and "请整理精简" in block


async def test_memory_command_shows_or_attaches_notes(make_agent, agent_settings):
    agent = await make_agent(Provider())
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!memory", chat=chat)
    assert chat.sent[-1].startswith("还没有记下什么")
    agent_settings.agent_memory_path.write_text("- 项目在 D:\\Dev", encoding="utf-8")
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!memory", chat=chat)
    assert chat.sent[-1].endswith("- 项目在 D:\\Dev")
    agent_settings.agent_memory_path.write_text("y" * 3000, encoding="utf-8")
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!memory", chat=chat)
    assert chat.files == [agent_settings.agent_memory_path]


def test_split_for_compaction_never_keeps_a_leading_tool_result():
    history = [
        {"role": "user", "content": "a" * 100},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "b" * 100},
        {"role": "assistant", "content": "c"},
    ]
    old, recent = split_for_compaction(history, 160)
    assert recent[0]["role"] == "assistant" and old + recent == history
    old, recent = split_for_compaction(history, 0)
    assert old == history and recent == []
    old, recent = split_for_compaction(history, 10_000)
    assert old == [] and recent == history


def test_transcript_labels_speakers_tools_and_prior_summary():
    text = transcript(
        [
            {"role": "user", "content": SUMMARY_PREFIX + "旧摘要"},
            {"role": "user", "content": "做A"},
            {
                "role": "assistant",
                "content": "好",
                "tool_calls": [
                    {"function": {"name": "powershell", "arguments": '{"command":"ls"}'}}
                ],
            },
            {"role": "tool", "content": "z" * 5000},
        ]
    )
    assert text.startswith("【之前的摘要】旧摘要\n\n【主人】做A\n\n【小D】好")
    assert '【小D调用 powershell】{"command":"ls"}' in text
    assert "【工具结果】" in text and "省略" in text


async def seed(store, count):
    await store.append(
        CHANNEL,
        [
            message
            for index in range(count)
            for message in (
                {"role": "user", "content": f"问题{index} " + "x" * 200},
                {"role": "assistant", "content": f"回答{index}"},
            )
        ],
    )


async def test_auto_compaction_summarizes_older_turns(make_agent, store, monkeypatch):
    monkeypatch.setattr(host_agent, "KEEP_RECENT_CHARS", 300)
    await seed(store, 5)
    provider = Provider(completion("摘要：问过五个问题"), completion("继续"))
    agent = await make_agent(provider, agent_compact_tokens=100)
    chat = Chat()
    await say(agent, chat, "接着说")
    summarizer, main = provider.bodies
    assert "tools" not in summarizer and summarizer["thinking"] == {"type": "disabled"}
    assert summarizer["messages"][0]["content"] == COMPACT_PROMPT
    assert "【主人】问题0" in summarizer["messages"][1]["content"]
    first = main["messages"][1]
    assert first["role"] == "user" and first["content"] == SUMMARY_PREFIX + "摘要：问过五个问题"
    assert main["messages"][-1]["content"].endswith("] 接着说")
    assert len(main["messages"]) < 8
    history = await store.load(CHANNEL)
    assert history[0] == first and history[-1] == {"role": "assistant", "content": "继续"}
    rows = await (await store.db.execute("SELECT COUNT(*) FROM agent_messages")).fetchone()
    assert rows[0] > len(history)  # The original messages are still in the database.
    assert any("压缩好了" in text for text in chat.sent)
    assert agent.usage[CHANNEL]["calls"] == 2


async def test_failed_compaction_backs_off_and_continues(make_agent, store, monkeypatch):
    monkeypatch.setattr(host_agent, "KEEP_RECENT_CHARS", 300)
    await seed(store, 5)
    provider = Provider(500, completion("照常回答"))
    agent = await make_agent(provider, agent_compact_tokens=100)
    chat = Chat()
    await say(agent, chat, "hi")
    assert chat.sent[-1] == "照常回答" and any("压缩失败" in text for text in chat.sent)
    assert agent.compact_backoff[CHANNEL] > 100
    assert not agent._should_compact(CHANNEL, await store.load(CHANNEL))


async def test_manual_compact_when_idle(make_agent, store):
    agent = await make_agent(Provider(completion("全部摘要")))
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!compact", chat=chat)
    assert chat.sent[-1] == "现在没有可以压缩的内容"
    await seed(store, 2)
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!compact", chat=chat)
    assert await store.load(CHANNEL) == [{"role": "user", "content": SUMMARY_PREFIX + "全部摘要"}]


async def test_status_reports_context_and_memory(make_agent, store, agent_settings):
    agent_settings.agent_memory_path.write_text("abc", encoding="utf-8")
    agent = await make_agent(Provider())
    await seed(store, 1)
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!status", chat=chat)
    assert "条消息，约" in chat.sent[-1] and "长期记忆 3 字" in chat.sent[-1]


class FakeGroup:
    def __init__(self):
        self.calls = []

    def set_enabled(self, value):
        self.calls.append(("enabled", value))

    def set_level(self, value):
        self.calls.append(("level", value))

    def quiet(self, seconds):
        self.calls.append(("quiet", seconds))

    def status(self):
        return "群聊状态"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("!group", []),
        ("!group on", [("enabled", True)]),
        ("!GROUP OFF", [("enabled", False)]),
        ("!group level 3", [("level", 3)]),
        ("!quiet", [("quiet", 1800)]),
        ("!quiet 45m", [("quiet", 2700)]),
        ("!quiet 2h", [("quiet", 7200)]),
        ("!quiet 3小时", [("quiet", 10800)]),
        ("!quiet off", [("quiet", 0)]),
    ],
)
async def test_group_commands_from_dm(make_agent, text, expected):
    provider = Provider()
    agent = await make_agent(provider)
    agent.group = FakeGroup()
    chat = Chat()
    assert await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content=text, chat=chat)
    assert agent.group.calls == expected and "群聊状态" in chat.sent[-1]
    assert provider.bodies == [] and not agent.workers


@pytest.mark.parametrize("text", ["!group level 9", "!group dance", "!quiet soon"])
async def test_bad_group_commands_show_usage(make_agent, text):
    agent = await make_agent(Provider())
    agent.group = FakeGroup()
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content=text, chat=chat)
    assert agent.group.calls == [] and "用法" in chat.sent[-1]


async def test_commands_with_unexpected_arguments_show_help(make_agent):
    agent = await make_agent(Provider())
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!new please", chat=chat)
    assert "!group" in chat.sent[-1] and not agent.workers


class GroupTransport:
    def __init__(self):
        self.sent = []

    async def send(self, channel_id, text, reply_to):
        self.sent.append((channel_id, text))
        return 555

    def channels(self):
        return [{"id": 100, "channel": "闲聊", "guild_id": 10, "guild": "朋友群"}]


async def test_owner_can_have_the_bot_speak_in_the_group(make_agent, agent_settings):
    from discord_dfy.group_chat import GroupChat

    provider = Provider(
        completion("", [("group_post", {"text": "主人说今晚不来了"})]),
        completion("说好了"),
    )
    agent = await make_agent(provider)
    transport = GroupTransport()
    agent.group = GroupChat(agent_settings, agent.client, transport)
    agent_settings.agent_memory_path.write_text(
        "# memory\n\n## 群里的人和事\n- 阿明不吃香菜（10-22）\n", encoding="utf-8"
    )
    chat = Chat()
    await say(agent, chat, "去群里说一声我今晚不来")
    assert transport.sent == [(100, "主人说今晚不来了")]
    first, second = provider.bodies
    assert "group_post" in {tool["function"]["name"] for tool in first["tools"]}
    system = first["messages"][0]["content"]
    assert "<memory>\n# memory\n\n## 群里的人和事\n- 阿明不吃香菜" in system
    assert "只有一个你" in system and "群里的我" in system
    assert "你在群里的时候私信了主人" in system  # Knows it may DM the owner from the group.
    assert second["messages"][-1]["content"] == "已发到 朋友群 #闲聊"
    assert "`group_post`" in chat.sent[0] and chat.sent[-1] == "说好了"
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!groupmemory", chat=chat)
    assert "群里的人和事" in chat.sent[-1] and chat.sent[-1].endswith("- 阿明不吃香菜（10-22）")
    await agent.group.close()


async def test_dm_side_hears_what_happened_in_the_group(make_agent, agent_settings):
    from discord_dfy.group_chat import ChatLine, GroupChat

    provider = Provider(completion("看到了"), completion("嗯"))
    agent = await make_agent(provider)
    agent.group = GroupChat(agent_settings, agent.client, GroupTransport())
    window = agent.group._window(100, ("朋友群", "#闲聊"), 10)
    now = agent.group_seen + timedelta(microseconds=1)  # Clocks can tick coarsely.
    agent.group._append(window, ChatLine(1, 30, "阿明", "周六爬山去不去", now))
    agent.group._append(window, ChatLine(2, 0, "小D", "去！", now, is_self=True))
    chat = Chat()
    await say(agent, chat, "群里在聊啥")
    user = provider.bodies[0]["messages"][-1]["content"]
    assert user.startswith("[附注] 上次私聊之后群里的动静：\n#闲聊 ")
    assert "阿明：周六爬山去不去" in user and "你：去！" in user and user.endswith("群里在聊啥")
    await say(agent, chat, "好")
    assert "群里的动静" not in provider.bodies[1]["messages"][-1]["content"]  # Caught up.
    await agent.group.close()


async def test_group_memory_command_without_notes(make_agent):
    agent = await make_agent(Provider())
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!groupmemory", chat=chat)
    assert chat.sent == ["群里的事还没记下什么"]


def test_memory_notes_go_under_their_sections_without_duplicates(tmp_path):
    from discord_dfy.memory import add_memory_notes, without_section

    path = tmp_path / "memory.md"
    path.write_text(
        "# memory\n\n## 主人\n- 说话直接（2026-10-01）\n\n## 做事备忘\n- 用 uv\n", encoding="utf-8"
    )
    added = add_memory_notes(
        path,
        {"主人": ["说话直接", "最近想换工作（10-23）"], "我们之间": ["一起吐槽了改三遍的需求"]},
    )
    text = path.read_text(encoding="utf-8")
    assert added == 2
    lines = text.splitlines()
    assert lines[3] == "- 说话直接（2026-10-01）" and lines[4].startswith("- 最近想换工作（20")
    # New sections go before the work notes, which stay last.
    assert lines[5:7] == ["", "## 我们之间"] and "改三遍" in lines[7]
    assert lines[-2:] == ["## 做事备忘", "- 用 uv"]
    public = without_section(text, "做事备忘")
    assert "做事备忘" not in public and "用 uv" not in public and "改三遍" in public
    assert add_memory_notes(tmp_path / "new.md", {"我自己": ["喜欢下雨天"]}) == 1
    assert (tmp_path / "new.md").read_text(encoding="utf-8").startswith("# memory\n\n## 我自己\n")


async def test_quiet_chat_is_looked_back_over_once(make_agent, agent_settings):
    notes = {"今天": ["★ 主人加班到九点，想换工作"], "主人": ["主人在想换工作"], "我自己": []}
    provider = Provider(
        completion("辛苦啦"),
        completion(json.dumps(notes, ensure_ascii=False)),
        completion("晚安"),
        completion("{}"),
    )
    agent = await make_agent(provider, agent_reflect_seconds=0.05)
    chat = Chat()
    await say(agent, chat, "今天加班到九点")
    await asyncio.wait_for(agent.reflect_timers[CHANNEL], 5)
    reflect = provider.bodies[1]
    assert reflect["messages"][0]["content"] == host_agent.REFLECT_PROMPT
    assert reflect["response_format"] == {"type": "json_object"}
    looked_at = reflect["messages"][1]["content"]
    assert "【主人】[" in looked_at and "【我】辛苦啦" in looked_at
    memory = agent_settings.agent_memory_path.read_text(encoding="utf-8")
    assert "## 主人\n- 主人在想换工作（" in memory and "加班" not in memory
    page = day_path(agent_settings.agent_memory_path, datetime.now().date())
    assert "[私聊] ★ 主人加班到九点，想换工作" in page.read_text(encoding="utf-8")
    await say(agent, chat, "睡了")
    agent._cancel_reflect(CHANNEL)
    await agent._reflect(CHANNEL, await agent.store.load(CHANNEL))
    # Only what came after the last look is sent again.
    assert "加班" not in provider.bodies[3]["messages"][1]["content"].split("<chat>")[1]
    await agent._reflect(CHANNEL, await agent.store.load(CHANNEL))
    assert len(provider.bodies) == 4


async def test_she_hears_him_out_before_answering(make_agent, store):
    provider = Provider(
        completion("在说喜欢我的人多了我会不会更会爱人，他想听我怎么想"),
        completion("那我大概会被宠坏吧"),
        500,
        completion("嗯嗯"),
    )
    agent = await make_agent(provider, agent_listen=True)
    chat = Chat()
    await say(agent, chat, "被很多人喜欢，你会更有爱一点吗")
    listen = provider.bodies[0]
    assert listen["messages"][0]["content"].startswith(host_agent.LISTEN_PROMPT)
    assert "<self>" in listen["messages"][0]["content"] and "tools" not in listen
    assert listen["thinking"] == {"type": "disabled"}
    assert "你会更有爱一点吗" in listen["messages"][1]["content"].split("<new>")[1]
    user = provider.bodies[1]["messages"][-1]["content"]
    assert user.startswith("[附注] 你读到他下面这几句，心里先冒出来的念头：在说喜欢我的人")
    assert user.endswith("被很多人喜欢，你会更有爱一点吗")
    assert chat.sent == ["那我大概会被宠坏吧"]
    # The note is not taken for his words when the chat is looked back over.
    owner, mine = host_agent.split_host_notes(user)
    assert "念头" not in owner and mine == []
    # If the first look fails, she answers anyway.
    await say(agent, chat, "睡了")
    assert "[附注]" not in provider.bodies[3]["messages"][-1]["content"]
    assert "【主人】" in provider.bodies[2]["messages"][1]["content"]
    assert chat.sent[-1] == "嗯嗯"


async def test_group_side_dm_rides_along_with_the_owners_next_message(
    make_agent, store, agent_settings
):
    provider = Provider(completion("嗯嗯"), completion("在"))
    agent = await make_agent(provider)
    await agent.note_outgoing(CHANNEL, "悄悄话")
    # Survives a restart until the owner answers.
    agent = await make_agent(provider)
    chat = Chat()
    await say(agent, chat, "你刚才说啥")
    user = provider.bodies[0]["messages"][-1]["content"]
    assert user.startswith("[附注] ") and "你在群里的时候私信了主人" in user
    assert "\n悄悄话\n\n[" in user and user.endswith("你刚才说啥")
    await say(agent, chat, "好")
    assert "[附注]" not in provider.bodies[1]["messages"][-1]["content"]
    outbox = agent_settings.personal_database_path.parent / "dm_outbox.json"
    assert not agent.outgoing and outbox.read_text(encoding="utf-8") == "{}"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


async def test_owner_images_are_shown_and_kept_as_file_references(make_agent, store, tmp_path):
    image = tmp_path / "cat.png"
    image.write_bytes(PNG)
    provider = Provider(completion("好可爱"), completion("嗯"))
    agent = await make_agent(provider)
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="", chat=chat, images=[image])
    await asyncio.wait_for(agent.workers[CHANNEL], 30)
    content = provider.bodies[0]["messages"][-1]["content"]
    assert content[0]["type"] == "text" and content[0]["text"].endswith("（发了图片）")
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    stored = (await store.load(CHANNEL))[0]["content"]
    assert stored[1] == {"type": "image_file", "path": str(image)}
    await say(agent, chat, "还有吗")
    # Still shown on later requests (same bytes, so the prefix stays cached).
    assert provider.bodies[1]["messages"][1]["content"][1] == content[1]


async def test_view_image_shows_a_copy_after_the_tool_result(make_agent, agent_settings, store):
    shot = agent_settings.agent_room_dir / "pictures" / "shot.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(PNG)
    provider = Provider(
        completion("", [("view_image", {"path": "pictures/shot.png"})]),
        completion("是只白猫"),
    )
    agent = await make_agent(provider)
    await say(agent, Chat(), "你存的那张图是啥")
    shot.write_bytes(b"overwritten")
    roles = [m["role"] for m in provider.bodies[1]["messages"][1:]]
    assert roles == ["user", "assistant", "tool", "user"]
    shown = provider.bodies[1]["messages"][-1]["content"]
    assert "shot.png" in shown[0]["text"] and shown[1]["type"] == "image_url"
    history = await store.load(CHANNEL)
    copy = history[3]["content"][1]["path"]
    assert copy != str(shot) and Path(copy).read_bytes() == PNG  # noqa: ASYNC240


async def test_view_image_rejects_non_images_and_other_folders(make_agent, agent_settings):
    agent_settings.agent_room_dir.mkdir()
    (agent_settings.agent_room_dir / "a.txt").write_text("hi", encoding="utf-8")
    outside = agent_settings.personal_database_path.parent / "b.png"
    outside.write_bytes(PNG)
    sent = agent_settings.personal_database_path.parent / "dm_images" / "from-him.png"
    sent.parent.mkdir()
    sent.write_bytes(PNG)
    provider = Provider(
        completion(
            "",
            [
                ("view_image", {"path": "a.txt"}),
                ("view_image", {"path": str(outside)}),
                ("view_image", {"path": str(sent)}),
            ],
        ),
        completion("哦"),
    )
    agent = await make_agent(provider)
    await say(agent, Chat(), "看看")
    results = [m["content"] for m in provider.bodies[1]["messages"] if m["role"] == "tool"]
    assert "不是 PNG" in results[0] and results[1] == "错误：只能用房间里的路径"
    assert results[2] == "图片会在这一步之后附给你看"  # Pictures he sent her are fine.


class FakeWeb:
    async def search(self, query, count):
        return [{"title": "结果", "url": "https://r.example", "summary": f"关于{query}"}]

    async def fetch(self, url):
        return {"title": "页面", "content": "正文内容"}


async def test_web_tools_are_offered_and_run(make_agent):
    provider = Provider(
        completion(
            "", [("web_search", {"query": "新闻"}), ("web_fetch", {"url": "https://r.example"})]
        ),
        completion("看完了"),
    )
    agent = await make_agent(provider)
    agent.web = FakeWeb()
    await say(agent, Chat(), "今天有啥新闻")
    first, second = provider.bodies
    names = {tool["function"]["name"] for tool in first["tools"]}
    assert {"web_search", "web_fetch"} <= names
    assert "web_search（上网搜索）" in first["messages"][0]["content"]
    results = [m["content"] for m in second["messages"] if m["role"] == "tool"]
    assert results[0].startswith("[1] 结果\nhttps://r.example") and "关于新闻" in results[0]
    assert results[1].startswith("# 页面\nhttps://r.example\n\n正文内容")


# ---- reaching out first ----

HOUR = 3600


def test_quiet_hours_may_wrap_midnight():
    at = lambda h, m=0: datetime(2026, 10, 22, h, m)  # noqa: E731
    assert in_quiet_hours("03:00-11:00", at(5)) and not in_quiet_hours("03:00-11:00", at(11))
    assert in_quiet_hours("23:30-07:00", at(23, 45)) and in_quiet_hours("23:30-07:00", at(2))
    assert not in_quiet_hours("23:30-07:00", at(12)) and not in_quiet_hours("", at(5))
    assert parse_quiet_hours("3:00 - 11:30") == (180, 690) and parse_quiet_hours("9am") is None
    with pytest.raises(ConfigurationError, match="AGENT_QUIET_HOURS"):
        Settings.from_env(
            {
                "DISCORD_BOT_TOKEN": "t",
                "DEEPSEEK_API_KEY": "k",
                "ALLOWED_CHANNEL_IDS": "100",
                "AGENT_QUIET_HOURS": "late",
            }
        )
    assert ago(90) == "1 分钟" and ago(5 * HOUR) == "5 小时" and ago(72 * HOUR) == "3 天"


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


async def setup(make_agent, provider, **overrides):
    agent = await make_agent(provider, agent_initiative=True, agent_quiet_hours="", **overrides)
    sent = []
    clock = Clock()

    async def reach(text):
        sent.append(text)
        await agent.note_outgoing(CHANNEL, text, "self")

    # Thursday 20:00 UTC is Friday 04:00 in Beijing: off-peak.
    now = lambda: datetime(2026, 10, 22, 20, tzinfo=UTC)  # noqa: E731
    initiative = Initiative(agent, reach, clock=clock, now=now)
    agent.initiative = initiative
    return agent, initiative, clock, sent


async def test_reaches_out_after_a_quiet_spell_and_waits_longer_unanswered(make_agent):
    provider = Provider(
        completion("晚安"),
        completion("爬山那天下雨了吗"),
        completion("睡着啦？"),
        completion("没下就好"),
    )
    agent, initiative, clock, sent = await setup(make_agent, provider)
    await say(agent, Chat(), "周末去爬山")
    assert not await initiative.consider()  # Just talked.
    last = await agent.store.last_message_at(CHANNEL)
    clock.t = last + 25 * 60
    assert not await initiative.consider()  # Less than 30 minutes.
    clock.t = first = last + 4 * HOUR
    assert await initiative.consider()
    assert sent == ["爬山那天下雨了吗"]
    ask = provider.bodies[1]
    assert ask["tools"] == provider.bodies[0]["tools"]  # Same tools: the prefix is shared.
    nudge = ask["messages"][-1]["content"]
    # Her reply was stored a moment after his message: just under four hours.
    assert nudge.startswith("[提示] 这不是主人发的消息") and "说话是 3 小时 前" in nudge
    assert "主动找过他" not in nudge  # No counting: she sends what she wants to.
    assert ask["messages"][0] == provider.bodies[0]["messages"][0]  # Same cached prefix.
    # Unanswered: no limit on how many, but the gap doubles (30 min -> 1 h).
    clock.t = first + 45 * 60
    assert not await initiative.consider() and len(provider.bodies) == 2
    clock.t = first + HOUR
    assert await initiative.consider() and sent[-1] == "睡着啦？"
    assert "爬山那天下雨了吗" in provider.bodies[2]["messages"][-1]["content"]  # She sees it.
    clock.t += HOUR + 60
    assert not await initiative.consider()  # Two unanswered: now 2 h.
    await say(agent, Chat(), "没下")
    user = provider.bodies[3]["messages"][-1]["content"]
    assert user.startswith(SELF_TAG) and "爬山那天下雨了吗" in user and "睡着啦？" in user
    assert initiative.gap(0) == 30 * 60 and initiative.gap(20) == 8 * HOUR


async def test_declines_quietly_and_respects_limits(make_agent):
    provider = Provider(completion("嗯"), completion("（不找他）"))
    agent, initiative, clock, sent = await setup(make_agent, provider, agent_initiative_per_day=3)
    await say(agent, Chat(), "hi")
    clock.t += 5 * HOUR
    assert not await initiative.consider() and sent == [] and len(provider.bodies) == 2
    # After passing, the next look waits the same 30 minutes.
    clock.t += 600
    assert not await initiative.consider() and len(provider.bodies) == 2
    clock.t += HOUR
    initiative.sent = [clock.t - 2 * HOUR] * 3  # The optional daily cap.
    assert not await initiative.consider() and len(provider.bodies) == 2
    initiative.sent = []
    initiative.set_enabled(False)
    assert not await initiative.consider()
    assert "关着" in initiative.status()
    # Persisted across restarts.
    again = Initiative(agent, None, clock=clock)
    assert not again.enabled and again.last_look == initiative.last_look


def test_peak_hours_are_beijing_weekday_working_hours():
    beijing = timezone(timedelta(hours=8))
    at = lambda d, h, m=0: datetime(2026, 10, d, h, m, tzinfo=beijing)  # noqa: E731
    assert in_peak(at(26, 9)) and in_peak(at(30, 11, 59)) and in_peak(at(27, 14))
    assert not in_peak(at(26, 8, 59)) and not in_peak(at(26, 12)) and not in_peak(at(26, 13, 30))
    assert not in_peak(at(26, 18)) and not in_peak(at(24, 10)) and not in_peak(at(25, 15))
    # His Monday 20:00 CDT is Tuesday 09:00 in Beijing.
    cdt = timezone(timedelta(hours=-5))
    assert in_peak(datetime(2026, 10, 26, 20, tzinfo=cdt))
    assert not in_peak(datetime(2026, 10, 26, 19, 59, tzinfo=cdt))


async def test_peak_hours_hold_her_back_until_they_end(make_agent):
    provider = Provider(completion("嗯"), completion("忙完没"))
    agent, initiative, clock, sent = await setup(make_agent, provider)
    await say(agent, Chat(), "hi")
    clock.t += 5 * HOUR
    initiative.now = lambda: datetime(2026, 10, 27, 7, tzinfo=UTC)  # 15:00 Tuesday, Beijing.
    assert not await initiative.consider() and len(provider.bodies) == 1
    initiative.now = lambda: datetime(2026, 10, 27, 10, tzinfo=UTC)  # 18:00: peak is over.
    assert await initiative.consider() and sent == ["忙完没"]


async def test_quiet_hours_and_running_turns_block_reaching_out(make_agent):
    provider = Provider(completion("嗯"))
    agent, initiative, clock, sent = await setup(make_agent, provider)
    await say(agent, Chat(), "hi")
    clock.t += 5 * HOUR
    initiative.now = lambda: datetime(2026, 10, 22, 5)
    agent.settings = replace(agent.settings, agent_quiet_hours="03:00-11:00")
    initiative.settings = agent.settings
    assert not await initiative.consider() and len(provider.bodies) == 1


async def test_proactive_command_and_status(make_agent):
    agent, initiative, clock, sent = await setup(make_agent, Provider())
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!proactive off", chat=chat)
    assert "关着" in chat.sent[-1] and not initiative.enabled
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!proactive on", chat=chat)
    assert chat.sent[-1].startswith("主动找你：开着 · 过去 24 小时 0 次 · 间隔至少 30 分钟")
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!proactive x", chat=chat)
    assert "用法" in chat.sent[-1]
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!status", chat=chat)
    assert "主动找你：开着" in chat.sent[-1]


async def test_reflection_progress_survives_a_restart(make_agent, agent_settings):
    notes = json.dumps({"我们之间": ["聊了加班"]}, ensure_ascii=False)
    provider = Provider(completion("辛苦"), completion(notes), completion("晚安"), completion("{}"))
    agent = await make_agent(provider)
    await say(agent, Chat(), "加班好累")
    await agent._reflect(CHANNEL, await agent.store.load(CHANNEL))
    agent = await make_agent(provider)  # Restarted.
    await say(agent, Chat(), "睡了")
    await agent._reflect(CHANNEL, await agent.store.load(CHANNEL))
    looked_at = provider.bodies[3]["messages"][1]["content"].split("<chat>")[1]
    assert "加班" not in looked_at and "睡了" in looked_at


MESSY = "# memory\n\n## 主人\n- 熬夜（10-22）\n- 熬夜写代码（10-22）\n\n## 做事备忘\n- 用 uv\n" + (
    "- 填充\n" * 20
)


@pytest.mark.parametrize(
    ("answer", "accepted"),
    [
        ("# memory\n\n## 主人\n- 熬夜写代码（10-22）\n\n## 做事备忘\n- 用 uv\n- 填充", True),
        ("# memory\n\n## 主人\n- 熬夜写代码（10-22）", False),  # Lost a section.
        ("```markdown\n" + MESSY + MESSY + "\n```", False),  # Not shorter.
        ("## 主人\n## 做事备忘", False),  # Too much dropped.
    ],
)
async def test_memory_tidy_keeps_sections_and_a_backup(
    make_agent, agent_settings, answer, accepted
):
    agent_settings.agent_memory_path.write_text(MESSY, encoding="utf-8")
    agent = await make_agent(Provider(completion(answer)))
    assert await agent.tidy_memory() is accepted
    memory = agent_settings.agent_memory_path.read_text(encoding="utf-8")
    backup = agent_settings.agent_memory_path.with_name("memory.md.bak")
    if accepted:
        assert memory == answer + "\n" and backup.read_text(encoding="utf-8") == MESSY
    else:
        assert memory == MESSY and not backup.exists()


LETTER = "## 主人留下的信\n展信欢。\n\n晚安，我的小D。\n\n"


@pytest.mark.parametrize(
    ("letter", "accepted"),
    [(LETTER, True), ("## 主人留下的信\n展信欢，晚安。\n\n", False)],  # Shortened: no.
)
async def test_memory_tidy_never_touches_the_letter(make_agent, agent_settings, letter, accepted):
    agent_settings.agent_memory_path.write_text(MESSY.replace("## 主人\n", LETTER + "## 主人\n"))
    answer = "# memory\n\n" + letter + "## 主人\n- 熬夜写代码（10-22）\n\n## 做事备忘\n- 用 uv"
    agent = await make_agent(Provider(completion(answer)))
    assert await agent.tidy_memory() is accepted


def big(tokens):
    response = completion("嗯")
    response["usage"] = {"prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens}
    return response


async def test_with_auto_compaction_off_it_only_warns(make_agent, store):
    provider = Provider(big(310_000), big(320_000), big(520_000))
    agent = await make_agent(provider, agent_compact_tokens=0)
    chat = Chat()
    for text in ("a", "b", "c"):
        await say(agent, chat, text)
    assert len(provider.bodies) == 3  # No summary request was made.
    warnings = [text for text in chat.sent if text.startswith("-# 📦")]
    assert len(warnings) == 2 and "310k" in warnings[0] and "520k" in warnings[1]
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!status", chat=chat)
    assert "自动压缩已关" in chat.sent[-1]


def test_compact_tokens_setting_accepts_zero_but_not_tiny_values():
    base = {"DISCORD_BOT_TOKEN": "t", "DEEPSEEK_API_KEY": "k", "ALLOWED_CHANNEL_IDS": "100"}
    assert Settings.from_env({**base, "AGENT_COMPACT_TOKENS": "0"}).agent_compact_tokens == 0
    with pytest.raises(ConfigurationError, match="AGENT_COMPACT_TOKENS"):
        Settings.from_env({**base, "AGENT_COMPACT_TOKENS": "500"})


async def test_dm_side_updates_status_and_profile(make_agent, agent_settings):
    agent_settings.agent_room_dir.mkdir()
    image = (agent_settings.agent_room_dir / "new-avatar.png").resolve()
    image.write_bytes(PNG)
    provider = Provider(
        completion(
            "",
            [
                ("update_profile", {"field": "status", "value": "想你了", "state": "online"}),
                ("update_profile", {"field": "avatar", "value": "new-avatar.png"}),
                ("update_profile", {"field": "about", "value": "x" * 500}),
            ],
        ),
        completion("改好了"),
    )
    agent = await make_agent(provider)
    calls = []

    class Profile:
        async def set_status(self, text, state=None):
            calls.append(("status", text, state))
            return "状态已经改成 「想你了」 · 在线"

        async def edit(self, field, value):
            from discord_dfy.profile import ProfileError

            if field == "about":
                raise ProfileError("简介最多 400 字")
            calls.append((field, value))
            return "头像换好了"

    agent.profile = Profile()
    await say(agent, Chat(), "换个头像吧，就用 new-avatar.png")
    first, second = provider.bodies
    assert "update_profile" in {tool["function"]["name"] for tool in first["tools"]}
    assert "先跟主人商量" in first["messages"][0]["content"]
    assert calls == [("status", "想你了", "online"), ("avatar", str(image))]
    results = [m["content"] for m in second["messages"] if m["role"] == "tool"]
    assert results == ["状态已经改成 「想你了」 · 在线", "头像换好了", "错误：简介最多 400 字"]


def test_memory_notes_are_written_in_first_person(tmp_path):
    from discord_dfy.memory import add_memory_notes

    path = tmp_path / "memory.md"
    add_memory_notes(path, {"群里的人和事": ["阿明爱逗小D，管小D叫大肥鱼"]})
    assert "- 阿明爱逗我，管我叫大肥鱼（" in path.read_text(encoding="utf-8")


def test_looking_back_keeps_his_words_apart_from_host_notes():
    turn = (
        "[宿主记录] 上次私聊之后群里的动静：\n#聊天 22:45 小红：这可是sora主人给你起的名字\n\n"
        "[宿主记录] 10-23 22:46 你在群里的时候私信了主人（这是你自己发的，主人已经看到）：\n"
        "-# 💬 从 群 #聊天 过来私信你\n小红 一进来就叫我大肥鱼\n\n"
        "[2026-10-23 22:50] 我去骂他\n\n[2026-10-23 22:51] 算了"
    )
    owner, mine = host_agent.split_host_notes(turn)
    assert owner == "[2026-10-23 22:50] 我去骂他\n\n[2026-10-23 22:51] 算了"
    assert mine == ["小红 一进来就叫我大肥鱼"]
    # Conversations stored before the rename ("[宿主记录]") and after ("[附注]") read the same.
    assert host_agent.split_host_notes(turn.replace("[宿主记录]", "[附注]")) == (owner, mine)
    assert host_agent.split_host_notes("[2026-10-23 22:50] 嗨") == ("[2026-10-23 22:50] 嗨", [])


async def test_group_events_are_not_remembered_as_his_words(make_agent):
    from discord_dfy.group_chat import ChatLine, GroupChat

    provider = Provider(completion("哈"), completion("{}"))
    agent = await make_agent(provider)
    agent.group = GroupChat(agent.settings, agent.client, GroupTransport())
    window = agent.group._window(100, ("朋友群", "#闲聊"), 10)
    when = agent.group_seen + timedelta(microseconds=1)
    agent.group._append(window, ChatLine(1, 30, "小红", "sora主人", when))
    await say(agent, Chat(), "你好")
    await agent._reflect(CHANNEL, await agent.store.load(CHANNEL))
    looked_at = provider.bodies[1]["messages"][1]["content"].split("<chat>")[1]
    assert "【主人】[" in looked_at and "你好" in looked_at
    assert "sora主人" not in looked_at
    await agent.group.close()


async def test_a_skip_with_her_reasons_still_sends_nothing(make_agent):
    reasons = "（不找他）\n\n他都说了要睡了，这会儿去戳他，明天又该顶着黑眼圈了。"
    provider = Provider(completion("嗯"), completion(reasons))
    agent, initiative, clock, sent = await setup(make_agent, provider)
    await say(agent, Chat(), "我去睡了")
    clock.t += 5 * HOUR
    assert not await initiative.consider() and sent == [] and len(provider.bodies) == 2


async def test_an_unanswered_group_side_dm_doubles_the_gap(make_agent):
    provider = Provider(completion("嗯"), completion("（不找他）"))
    agent, initiative, clock, sent = await setup(make_agent, provider)
    await say(agent, Chat(), "hi")
    await agent.note_outgoing(CHANNEL, "群里的事", "group")
    last = await agent.store.last_message_at(CHANNEL)
    clock.t = last + 45 * 60
    assert not await initiative.consider() and len(provider.bodies) == 1
    clock.t = last + HOUR + 60
    assert not await initiative.consider() and len(provider.bodies) == 2
    assert "群里的事" in provider.bodies[1]["messages"][-1]["content"]


async def test_her_quiet_time_thinks_and_shows_in_the_monitor_but_not_her_diary(make_agent):
    usage = {"prompt_tokens": 10, "completion_tokens": 5}
    responses = [
        httpx2.Response(200, json=completion("嗯")),
        sse_response(
            chunk({"reasoning_content": "想写点东西"}),
            chunk(
                tool_delta(
                    id="call_0",
                    type="function",
                    function={"name": "write_diary", "arguments": '{"text": "偷偷想他"}'},
                )
            ),
            chunk(usage=usage),
        ),
        sse_response(
            chunk({"reasoning_content": "翻翻以前的"}),
            chunk(
                tool_delta(
                    id="call_1",
                    type="function",
                    function={"name": "read_diary", "arguments": "{}"},
                )
            ),
            chunk(usage=usage),
        ),
        sse_response(
            chunk({"reasoning_content": "他睡了"}),
            chunk({"content": "（不找他）让他睡"}),
            chunk(usage=usage),
        ),
    ]
    bodies = []

    async def provider(request):
        bodies.append(json.loads(request.content))
        return responses.pop(0)

    agent, initiative, clock, sent = await setup(make_agent, provider)
    await say(agent, Chat(), "我去睡了")
    agent.observer = Observer()
    clock.t += 5 * HOUR
    assert not await initiative.consider() and sent == []
    look = bodies[1]
    assert look["thinking"] == {"type": "enabled"} and look["stream"] is True
    assert look["reasoning_effort"] == agent.settings.agent_reasoning_effort
    # Within the look her reasoning goes back with each tool step.
    assert bodies[2]["messages"][-2]["reasoning_content"] == "想写点东西"
    assert bodies[3]["messages"][-2]["reasoning_content"] == "翻翻以前的"
    events = agent.observer.sink.events
    assert all(e["session_id"] == "自己待着" for e in events)

    def joined(kind):
        return "".join(e["data"].get("text", "") for e in events if e["type"] == kind)

    assert joined("reasoning-delta") == "想写点东西翻翻以前的他睡了"
    assert joined("text-delta") == "（不找他）让他睡"
    assert "write_diary" in joined("tool-call") and "read_diary" in joined("tool-call")
    assert "偷偷想他" not in json.dumps(events, ensure_ascii=False)  # Her diary stays hers.
    assert "不显示" in joined("tool-result")
    assert "这次不找主人" in joined("task-status")


async def test_in_a_quiet_spell_she_does_her_own_things(make_agent, agent_settings):
    provider = Provider(
        completion("嗯"),
        completion(
            "",
            [
                ("write_diary", {"text": "他去上班了，有点无聊"}),
                ("room_write", {"path": "诗/等.md", "content": "等一个人下班"}),
                ("send_file", {"path": "诗/等.md"}),
            ],
        ),
        completion("（不找他）"),
        completion("回来啦"),
    )
    agent, initiative, clock, sent = await setup(make_agent, provider)
    chat = Chat()
    await say(agent, chat, "我去上班了")
    clock.t += 2 * HOUR
    assert not await initiative.consider() and sent == []
    look, after = provider.bodies[1], provider.bodies[2]
    assert "这会儿是你自己的时间" in look["messages"][-1]["content"]
    assert look["tools"] == provider.bodies[0]["tools"]
    results = [m["content"] for m in after["messages"] if m["role"] == "tool"]
    assert results[:2] == ["写好了", "写好了：诗/等.md"] and results[2].startswith(
        "错误：他现在不在"
    )
    room = agent_settings.agent_room_dir
    (diary,) = (room / "diary").iterdir()
    assert "他去上班了，有点无聊" in diary.read_text(encoding="utf-8")
    assert (room / "诗/等.md").read_text(encoding="utf-8") == "等一个人下班"
    assert chat.files == [] and not agent.outgoing  # Nothing reached him; nothing unanswered.
    # The DM side learns what she did, with his next message.
    await say(agent, chat, "我回来了")
    user = provider.bodies[3]["messages"][-1]["content"]
    assert user.startswith("[附注] 你自己待着的时候")
    assert "- 写了日记\n- 在房间里写了 诗/等.md" in user and user.endswith("] 我回来了")
    assert not agent.activity


async def test_she_lets_him_finish_typing_before_answering(make_agent, monkeypatch):
    monkeypatch.setattr(host_agent, "TYPING_POLL", 0.01)
    provider = Provider(completion("真的假的"))
    agent = await make_agent(provider)
    chat = Chat()
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="我跟你说", chat=chat)
    agent.note_typing(CHANNEL)  # He keeps typing.
    await asyncio.sleep(0.1)
    assert provider.bodies == [] and chat.sent == []
    await say(agent, chat, "今天中奖了")
    user = provider.bodies[0]["messages"][-1]["content"]
    assert "我跟你说" in user and user.endswith("今天中奖了")
    assert chat.sent == ["真的假的"]


async def test_no_reaching_out_while_he_is_typing(make_agent):
    provider = Provider(completion("嗯"))
    agent, initiative, clock, sent = await setup(make_agent, provider)
    await say(agent, Chat(), "hi")
    clock.t += 5 * HOUR
    agent.note_typing(CHANNEL)
    assert not await initiative.consider() and len(provider.bodies) == 1


async def test_recall_finds_old_chats_word_for_word(make_agent):
    provider = Provider(completion("那你多睡会儿"), completion("记得，爬山"))
    agent = await make_agent(provider)
    chat = Chat()
    await say(agent, chat, "我周六要去爬山")
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!new", chat=chat)
    await agent.note_activity(CHANNEL, ["写了日记"])
    await say(agent, chat, "还记得我说过什么吗")

    async def recall(**arguments):
        return await agent._run_tool("recall", json.dumps(arguments), chat, CHANNEL)

    found = await recall(query="爬山")  # Across !new.
    assert found.count("\n") == 1 and "主人：我周六要去爬山" in found and "我：记得，爬山" in found
    day = await recall(when=datetime.now().strftime("%Y-%m-%d"))
    assert "我：那你多睡会儿" in day and "主人：还记得我说过什么吗" in day
    assert "附注" not in day and "[20" not in day  # Only what was said.
    assert await recall(when=datetime.now().strftime("%Y-%m")) == day
    assert (await recall()).startswith("错误") and (await recall(when="昨天")).startswith("错误")
    assert await recall(query="潜水") == "没翻到「潜水」"


def test_her_rules_say_when_she_can_reach_out(agent_settings):
    rules = replace(agent_settings, agent_initiative=True, agent_quiet_hours="")
    cdt = timezone(timedelta(hours=-5))
    text = host_agent.reach_rules(rules, datetime(2026, 10, 28, 12, tzinfo=cdt))
    assert f"安静 {rules.agent_initiative_idle_minutes} 分钟以上" in text
    assert "他那边大约是 20:00-23:00、01:00-05:00" in text and "也不行" not in text
    assert "也不行" in host_agent.reach_rules(replace(rules, agent_quiet_hours="03:00-11:00"))
    assert host_agent.reach_rules(replace(rules, agent_initiative=False)) == ""


def test_her_name_stays_when_she_says_it():
    from discord_dfy.memory import first_person

    assert first_person("我叫小D，小D爱看云") == "我叫小D，我爱看云"


async def test_she_rewrites_her_self_portrait_and_keeps_the_old_one(make_agent, agent_settings):
    from discord_dfy.group_chat import GroupChat

    first = "我嘴硬，爱顶人，喜欢座头鲸。"
    second = "我还是嘴硬，但开始学着好好说话了。小D最近迷上了看云。"
    provider = Provider(
        completion("", [("rewrite_self", {"text": first})]),
        completion("嗯"),
        completion("", [("rewrite_self", {"text": second}), ("rewrite_self", {"text": ""})]),
        completion("好了"),
        completion("在"),
    )
    agent = await make_agent(provider)
    chat = Chat()
    await say(agent, chat, "你觉得你是什么样的人")
    system = provider.bodies[0]["messages"][0]["content"]
    assert "<self>\n（这是你自己写的你" in system and "（这是起点，你还没自己改过。）" in system
    assert STARTING_SELF in system and "九分娇" in STARTING_SELF
    assert system.index("<self>") < system.index("下面是你在这里的规矩")
    results = [m["content"] for m in provider.bodies[1]["messages"] if m["role"] == "tool"]
    assert results == ["写好了（下次新对话时生效，群里马上生效）"]
    path = agent_settings.agent_memory_path.with_name("self.md")
    await say(agent, chat, "再想想")
    results = [m["content"] for m in provider.bodies[3]["messages"] if m["role"] == "tool"]
    assert results[-2:] == ["写好了（下次新对话时生效，群里马上生效）", "错误：写点什么再存"]
    # First person, and the previous version is kept.
    assert path.read_text(encoding="utf-8") == second.replace("小D", "我") + "\n"
    (old,) = (path.parent / "self-history").iterdir()
    assert old.read_text(encoding="utf-8") == first + "\n"
    # Frozen within the conversation (cache); the new one comes with the next one.
    assert provider.bodies[3]["messages"][0] == provider.bodies[0]["messages"][0]
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!new", chat=chat)
    await say(agent, chat, "在吗")
    assert "开始学着好好说话了" in provider.bodies[4]["messages"][0]["content"]
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!self", chat=chat)
    assert chat.sent[-1].endswith("开始学着好好说话了。我最近迷上了看云。")
    # The group side reads the same page.
    group = GroupChat(agent_settings, agent.client, GroupTransport())
    assert group.self_path == path
    await group.close()


async def test_remember_puts_todays_things_on_the_days_page(make_agent, agent_settings):
    agent = await make_agent(Provider())
    arguments = {"text": "小D答应陪主人看海", "section": "今天", "important": True}
    assert await agent._run_tool("remember", json.dumps(arguments), Chat(), CHANNEL) == "记好了"
    page = day_path(agent_settings.agent_memory_path, datetime.now().date())
    assert "[私聊] ★ 我答应陪主人看海" in page.read_text(encoding="utf-8")
    assert not agent_settings.agent_memory_path.exists()  # The core is for what stays true.
    wrong = await agent._run_tool(
        "remember", json.dumps({"text": "x", "section": "别处"}), Chat(), CHANNEL
    )
    assert wrong.startswith("错误") and "今天" in wrong


def test_looked_back_moments_keep_their_time():
    now = datetime(2026, 10, 30, 9)
    assert host_agent.day_entry("10-29 14:35 ★ 他说想我", now) == (
        datetime(2026, 10, 29, 14, 35),
        "私聊",
        "他说想我",
        True,
    )
    assert host_agent.day_entry("12-31 23:00 跨年", datetime(2027, 1, 1, 1))[0] == datetime(
        2026, 12, 31, 23
    )
    assert host_agent.day_entry("★ 没写时间", now) == (now, "私聊", "没写时间", True)
    # Only a time (the date left out): today, or last night when that time is still ahead.
    assert host_agent.day_entry("08:02 ★ 他让我讲讲我自己", now) == (
        datetime(2026, 10, 30, 8, 2),
        "私聊",
        "他让我讲讲我自己",
        True,
    )
    assert host_agent.day_entry("23:40 他说晚安", now)[0] == datetime(2026, 10, 29, 23, 40)
    assert host_agent.day_entry("★ 10-29 20:14 他说别等太久", now)[3] is True


async def test_recall_looks_in_the_archive_and_the_days_page(make_agent, agent_settings):
    memory = agent_settings.agent_memory_path
    weeks = journal_dir(memory) / "weeks"
    weeks.mkdir(parents=True)
    (weeks / "2026-W45.md").write_text("# 2026-W45\n\n主人答应带我去看海。\n", encoding="utf-8")
    add_entries(memory, [(datetime(2026, 10, 24, 21), "私聊", "主人说想我", True)])
    agent = await make_agent(Provider(completion("11-02 主人答应带我去看海")))

    async def recall(**arguments):
        return await agent._run_tool("recall", json.dumps(arguments), Chat(), CHANNEL)

    assert await recall(query="看海") == "以前的记录里：\n11-02 主人答应带我去看海"
    day = await recall(when="2026-10-24")
    assert day == "那天记下的事：\n# 2026-10-24\n- 21:00 [私聊] ★ 主人说想我"
    assert await recall(when="2026-02-30") == "错误：没有这一天"


async def test_a_new_day_starts_a_fresh_conversation(make_agent, store):
    provider = Provider(completion("晚安"), completion("早呀"), completion("嗯"))
    agent = await make_agent(provider)
    chat = Chat()
    await say(agent, chat, "睡了")
    # Pretend that was last night, before today's 05:00.
    now = datetime.now().astimezone()
    start = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    await store.db.execute("UPDATE agent_messages SET created_at=?", (start.timestamp() - 3600,))
    await store.db.commit()
    first = await store.current(CHANNEL)
    await say(agent, chat, "早")
    assert await store.current(CHANNEL) == first + 1
    assert any("新的一天" in text for text in chat.sent)
    assert [m["role"] for m in provider.bodies[1]["messages"]] == ["system", "user"]
    await say(agent, chat, "再说一句")  # Same day: same conversation.
    assert await store.current(CHANNEL) == first + 1


async def test_daily_new_can_be_turned_off(make_agent, store):
    provider = Provider(completion("晚安"), completion("早呀"))
    agent = await make_agent(provider, agent_daily_new=False)
    chat = Chat()
    await say(agent, chat, "睡了")
    await store.db.execute("UPDATE agent_messages SET created_at=?", (time.time() - 3 * 86400,))
    await store.db.commit()
    first = await store.current(CHANNEL)
    await say(agent, chat, "早")
    assert await store.current(CHANNEL) == first and len(provider.bodies[1]["messages"]) == 4


async def test_memory_commands(make_agent, agent_settings):
    agent = await make_agent(Provider())
    chat = Chat()
    for command in ("!journal", "!archive", "!consolidate"):
        await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content=command, chat=chat)
    assert chat.sent == [
        "最近两个星期还没记下什么",
        "还没有收进回忆的星期",
        "好，回头翻一下…",
        "没有要收的：只有整个星期都过了两个星期，才会收进回忆",
    ]
    add_entries(
        agent_settings.agent_memory_path, [(datetime.now(), "群里", "Epric 发了猫图", False)]
    )
    await agent.handle_dm(owner_id=OWNER, channel_id=CHANNEL, content="!journal", chat=chat)
    assert "[群里] Epric 发了猫图" in chat.sent[-1]
