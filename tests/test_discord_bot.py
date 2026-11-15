import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_dfy import discord_bot
from discord_dfy.discord_bot import (
    DENIED,
    DiscordAgent,
    GroupTransport,
    chat_line,
    split_reply,
)
from discord_dfy.models import IncomingMessage
from discord_dfy.service import AgentService

BOT = 999


def interaction(*, channel_id=100, guild_id=10, is_bot=False):
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=555)))
    return SimpleNamespace(
        id=123,
        guild_id=guild_id,
        channel_id=channel_id,
        guild=SimpleNamespace(name="测试服"),
        channel=SimpleNamespace(name="闲聊"),
        user=SimpleNamespace(id=20, bot=is_bot, display_name="主人"),
        created_at=datetime.now(UTC),
        response=response,
        followup=followup,
    )


@pytest.fixture
def bot(settings, policy):
    agent = DiscordAgent(settings, AgentService(AsyncMock(), policy))
    agent._connection.user = SimpleNamespace(id=BOT)
    agent.group = SimpleNamespace(
        controls=SimpleNamespace(enabled=True),
        ask=AsyncMock(return_value=["答案", "@everyone " + "x" * 2100]),
        note_self=MagicMock(),
        observe=MagicMock(),
    )
    return agent


async def test_ping(bot):
    event = interaction()
    await bot.ping(event)
    event.response.send_message.assert_awaited_once_with("Pong!", ephemeral=True)


@pytest.mark.parametrize("changes", [{"channel_id": 101}, {"guild_id": None}, {"is_bot": True}])
@pytest.mark.parametrize("command", ["ping", "ask"])
async def test_commands_reject_outside_approved_channels(bot, changes, command):
    event = interaction(**changes)
    if command == "ask":
        await bot.ask(event, "Question")
    else:
        await bot.ping(event)
    event.response.send_message.assert_awaited_once_with(DENIED, ephemeral=True)
    event.response.defer.assert_not_awaited()
    bot.group.ask.assert_not_awaited()
    bot.service._storage.save_message.assert_not_awaited()


async def test_ask_goes_through_group_brain(bot):
    event = interaction()
    await bot.ask(event, "什么是 uv")
    event.response.defer.assert_awaited_once_with(thinking=True)
    stored = bot.service._storage.save_message.await_args.args[0]
    assert stored.source == "ask" and stored.content == "什么是 uv"
    channel_id, line = bot.group.ask.await_args.args
    assert channel_id == 100 and line.content == "/ask 什么是 uv" and line.author_name == "主人"
    assert bot.group.ask.await_args.kwargs["title"] == ("测试服", "#闲聊")
    sends = event.followup.send.await_args_list
    assert sends[0].args == ("答案",) and len(sends) == 3
    assert all(call.kwargs["allowed_mentions"].everyone is False for call in sends)
    assert all(len(call.args[0]) <= 1900 for call in sends)
    noted = bot.group.note_self.call_args_list
    assert len(noted) == 3 and noted[0].args[1].is_self and noted[0].args[1].id == 555


async def test_ask_when_group_is_off(bot):
    bot.group.controls.enabled = False
    event = interaction()
    await bot.ask(event, "Question")
    assert "关着" in event.response.send_message.await_args.args[0]
    bot.group.ask.assert_not_awaited()


def test_intents_and_command_metadata(bot, settings, policy):
    assert bot.intents.guilds and bot.intents.guild_messages and bot.intents.message_content
    assert not bot.intents.dm_messages and not bot.intents.guild_reactions
    assert not bot.intents.members and not bot.intents.presences and not bot.intents.typing
    assert {command.name for command in bot.tree.get_commands()} == {"ping", "ask"}
    assert all(command.guild_only for command in bot.tree.get_commands())
    personal = DiscordAgent(replace(settings, personal_mode_enabled=True), bot.service)
    assert personal.intents.dm_messages


async def test_server_scoped_command_sync(settings, bot):
    bot.settings = replace(settings, test_guild_id=42)
    bot.tree.sync = AsyncMock()
    await bot.setup_hook()
    assert {cmd.name for cmd in bot.tree.get_commands(guild=discord.Object(id=42))} == {
        "ping",
        "ask",
    }
    assert bot.tree.sync.await_args.kwargs["guild"].id == 42


async def test_global_command_sync(bot):
    bot.tree.sync = AsyncMock()
    await bot.setup_hook()
    bot.tree.sync.assert_awaited_once_with()


def message(content="hello", *, author_id=20, bot_author=False, **fields):
    defaults = dict(
        id=1,
        guild=SimpleNamespace(id=10, name="测试服"),
        channel=SimpleNamespace(id=100, name="闲聊"),
        author=SimpleNamespace(id=author_id, bot=bot_author, display_name="阿明"),
        webhook_id=None,
        content=content,
        created_at=datetime.now(UTC),
        mentions=[],
        attachments=[],
        stickers=[],
        reference=None,
    )
    defaults.update(fields)
    return SimpleNamespace(**defaults)


@pytest.mark.parametrize(
    "changes",
    [
        {"guild": None},
        {"channel": SimpleNamespace(id=101, name="x")},
        {"author": SimpleNamespace(id=BOT, bot=True, display_name="小D")},
    ],
)
async def test_message_adapter_filters_before_storage_and_group(bot, changes):
    await bot.on_message(message(**changes))
    bot.service._storage.save_message.assert_not_awaited()
    bot.group.observe.assert_not_called()


async def test_other_bots_reach_the_group_brain_but_are_never_stored(bot):
    me = SimpleNamespace(id=BOT, display_name="小D")
    await bot.on_message(message(f"<@{BOT}> 我是AI", author_id=5, bot_author=True, mentions=[me]))
    await bot.on_message(message("webhook 转发", webhook_id=9))
    await bot.on_message(
        message("别的频道", author_id=5, bot_author=True, channel=SimpleNamespace(id=101, name="x"))
    )
    bot.service._storage.save_message.assert_not_awaited()
    seen = bot.group.observe.call_args_list
    assert [(c.args[0], c.args[1].content, c.args[1].is_bot) for c in seen] == [
        (100, "@小D 我是AI", True),
        (100, "webhook 转发", True),
    ]
    assert seen[0].kwargs["mentioned"] and not seen[1].kwargs["mentioned"]
    assert seen[0].kwargs["title"] == ("测试服", "#闲聊")


async def test_group_message_is_stored_and_observed(bot):
    me = SimpleNamespace(id=BOT, display_name="小D")
    event = message(f"<@{BOT}> 看图", mentions=[me])
    await bot.on_message(event)
    stored = bot.service._storage.save_message.await_args.args[0]
    assert isinstance(stored, IncomingMessage) and stored.content == f"<@{BOT}> 看图"
    channel_id, line = bot.group.observe.call_args.args
    kwargs = bot.group.observe.call_args.kwargs
    assert channel_id == 100 and line.content == "@小D 看图" and line.author_name == "阿明"
    assert kwargs["mentioned"] and not kwargs["replied_to_bot"]
    assert kwargs["title"] == ("测试服", "#闲聊")


async def test_reply_to_bot_is_detected(bot):
    resolved = MagicMock(spec=discord.Message)
    resolved.author = SimpleNamespace(id=BOT, display_name="小D")
    resolved.content = "我刚说的"
    event = message("真的吗", reference=SimpleNamespace(message_id=77, resolved=resolved))
    await bot.on_message(event)
    line = bot.group.observe.call_args.args[1]
    assert bot.group.observe.call_args.kwargs["replied_to_bot"]
    assert line.reply_to == 77 and line.reply_preview == "小D：我刚说的"


def test_chat_line_describes_media_and_self():
    event = message(
        "",
        author_id=BOT,
        attachments=[
            SimpleNamespace(filename="a.png", content_type="image/png"),
            SimpleNamespace(filename="b.zip", content_type=None),
        ],
        stickers=[SimpleNamespace(name="猫猫")],
    )
    line = chat_line(event, BOT)
    assert line.content == "[图片 a.png] [文件 b.zip] [贴纸 猫猫]" and line.is_self
    assert chat_line(message("  "), BOT).content == "[空消息]"


def permissions(**overrides):
    values = dict(
        send_messages=True,
        send_messages_in_threads=True,
        read_message_history=True,
        add_reactions=True,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def text_channel(perms, history=()):
    channel = MagicMock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(me=SimpleNamespace(id=BOT))
    channel.permissions_for.return_value = perms
    channel.send = AsyncMock(return_value=SimpleNamespace(id=4242))
    partial = SimpleNamespace(add_reaction=AsyncMock())
    channel.get_partial_message.return_value = partial

    async def iterate(**kwargs):
        channel.history_kwargs = kwargs
        for item in history:
            yield item

    channel.history = iterate
    return channel


async def test_transport_sends_with_quote_and_falls_back(bot):
    channel = text_channel(permissions())
    bot.get_channel = MagicMock(return_value=channel)
    transport = GroupTransport(bot)
    assert await transport.send(100, "hi", 7) == 4242
    kwargs = channel.send.await_args.kwargs
    assert kwargs["reference"] is channel.get_partial_message.return_value
    assert kwargs["allowed_mentions"].replied_user is False
    channel.send.side_effect = [
        discord.HTTPException(SimpleNamespace(status=400, reason="Bad"), "gone"),
        SimpleNamespace(id=1),
    ]
    assert await transport.send(100, "hi", 7) == 1
    assert "reference" not in channel.send.await_args.kwargs


async def test_transport_respects_channel_and_permissions(bot):
    transport = GroupTransport(bot)
    with pytest.raises(PermissionError):
        await transport.send(101, "hi", None)
    channel = text_channel(permissions(read_message_history=False, add_reactions=False))
    bot.get_channel = MagicMock(return_value=channel)
    await transport.send(100, "hi", 7)
    assert channel.send.await_args.kwargs["reference"] is None
    with pytest.raises(PermissionError):
        await transport.react(100, 7, "😂")
    assert await transport.history(100, 10, datetime.now(UTC)) == []
    bot.get_channel.return_value = text_channel(permissions(send_messages=False))
    with pytest.raises(PermissionError):
        await transport.send(100, "hi", None)


async def test_transport_history_marks_other_bots_and_orders_oldest_first(bot):
    now = datetime.now(UTC)
    newest = message("新", id=3, created_at=now)
    mine = message("我说的", id=2, author_id=BOT, bot_author=True, created_at=now)
    other_bot = message("广告", id=4, author_id=5, bot_author=True)
    hooked = message("hook", id=5, webhook_id=9)
    oldest = message("旧", id=1, created_at=now - timedelta(minutes=5))
    channel = text_channel(permissions(), history=[newest, other_bot, mine, hooked, oldest])
    bot.get_channel = MagicMock(return_value=channel)
    lines = await GroupTransport(bot).history(100, 50, now - timedelta(hours=2))
    assert [line.content for line in lines] == ["旧", "hook", "我说的", "广告", "新"]
    assert lines[2].is_self and not lines[2].is_bot
    assert [line.is_bot for line in lines] == [False, True, False, True, False]
    assert channel.history_kwargs["limit"] == 50 and channel.history_kwargs["oldest_first"] is False
    await GroupTransport(bot).react(100, 7, "👍")
    channel.get_partial_message.return_value.add_reaction.assert_awaited_once_with("👍")


@pytest.mark.parametrize("text", ["abc", "x" * 5000, "🙂" * 2000, "你好" * 2000])
def test_unicode_reply_chunks_preserve_text_and_discord_budget(text):
    chunks = split_reply(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 1900 for chunk in chunks)


async def test_reaching_out_is_delivered_like_her_replies(bot, settings):
    bot.settings = replace(settings, owner_discord_user_id=20)
    channel = SimpleNamespace(id=77, send=AsyncMock())
    bot.get_user = MagicMock(return_value=SimpleNamespace(dm_channel=channel))
    bot.personal = SimpleNamespace(deliver=AsyncMock(), note_outgoing=AsyncMock())
    text = "主人，我查到个东西\n\n[表情包：我坐好了]"
    assert await bot.message_owner(text, source="self")
    chat, sent = bot.personal.deliver.await_args.args
    assert chat.channel is channel and sent == text  # Split into texts and stickers there.
    bot.personal.note_outgoing.assert_awaited_once_with(77, text, "self")
    channel.send.assert_not_awaited()


def test_voice_states_are_followed_only_when_watching(bot, settings):
    assert not bot.intents.voice_states
    watching = DiscordAgent(replace(settings, watch_stream=True), bot.service)
    assert watching.intents.voice_states


async def test_transport_talks_in_a_voice_channels_text_chat(bot):
    channel = text_channel(permissions())
    channel.__class__ = discord.VoiceChannel
    bot.get_channel = MagicMock(return_value=channel)
    assert await GroupTransport(bot).send(100, "我也在看", None) == 4242


def voice_channel(channel_id=100, owner_state=None, name="大厅"):
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.name = name
    channel.voice_states = {20: owner_state} if owner_state is not None else {}
    channel.guild = SimpleNamespace(me=SimpleNamespace(id=BOT), voice_client=None)
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True)

    async def connect(**kwargs):
        channel.connect_kwargs = kwargs
        channel.guild.voice_client = SimpleNamespace(channel=channel)

    channel.connect = AsyncMock(side_effect=connect)
    return channel


class FakeWatch:
    def __init__(self):
        self.channel_id = None

    def start(self, channel_id):
        self.channel_id = channel_id

    def stop(self):
        self.channel_id = None


@pytest.fixture
def voice_bot(settings, policy, tmp_path):
    agent = DiscordAgent(
        replace(
            settings,
            watch_stream=True,
            owner_discord_user_id=20,
            allowed_channel_ids=frozenset({100, 200}),
            personal_database_path=tmp_path / "personal.sqlite3",
        ),
        AgentService(AsyncMock(), policy),
    )
    agent._connection.user = SimpleNamespace(id=BOT)
    agent.stream = FakeWatch()
    hall = voice_channel(100)
    games = voice_channel(200, name="游戏")
    agent.get_channel = MagicMock(side_effect=lambda cid: {100: hall, 200: games}.get(cid))
    agent.hall, agent.games = hall, games
    return agent


OWNER_MEMBER = SimpleNamespace(id=20)


async def test_going_live_alone_does_not_bring_her_in(voice_bot):
    voice_bot.hall.voice_states[20] = SimpleNamespace(self_stream=True)
    await voice_bot.on_voice_state_update(OWNER_MEMBER, None, None)
    await voice_bot.on_ready()
    assert voice_bot.stream.channel_id is None and not voice_bot.hall.connect.called


async def test_she_joins_his_channel_when_told_and_watches_only_while_he_is_live(voice_bot):
    voice_bot.games.voice_states[20] = SimpleNamespace(self_stream=False)
    assert await voice_bot.voice_act("join") is None
    assert voice_bot.games.connect_kwargs == {
        "timeout": 20.0,
        "self_mute": True,
        "self_deaf": True,
    }
    assert voice_bot.voice_where() == "游戏" and voice_bot.stream.channel_id is None
    assert json.loads(voice_bot.voice_marker.read_text()) == {"channel_id": 200}
    voice_bot.games.voice_states[20] = SimpleNamespace(self_stream=True)
    await voice_bot.on_voice_state_update(OWNER_MEMBER, None, None)
    assert voice_bot.stream.channel_id == 200
    voice_bot.games.voice_states.pop(20)  # He left: no more screenshots, but she stays.
    await voice_bot.on_voice_state_update(OWNER_MEMBER, None, None)
    assert voice_bot.stream.channel_id is None and voice_bot.voice_where() == "游戏"


async def test_asked_in_a_voice_chat_she_joins_that_channel(voice_bot):
    voice_bot.games.voice_states[20] = SimpleNamespace(self_stream=True)
    assert await voice_bot.voice_act("join", 100) is None
    assert voice_bot.hall.connect.called and not voice_bot.games.connect.called
    assert voice_bot.stream.channel_id is None  # He streams elsewhere.


async def test_she_cannot_join_when_he_is_not_in_voice_or_she_lacks_permission(voice_bot):
    assert "不在" in await voice_bot.voice_act("join")
    voice_bot.hall.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=False)
    assert "权限" in await voice_bot.voice_act("join", 100)
    assert voice_bot.voice_target is None and not voice_bot.voice_marker.exists()


async def test_she_leaves_when_told_or_when_someone_disconnects_her(voice_bot, monkeypatch):
    voice_bot.hall.voice_states[20] = SimpleNamespace(self_stream=True)
    await voice_bot.voice_act("join")
    assert voice_bot.stream.channel_id == 100
    voice = SimpleNamespace(channel=voice_bot.hall, disconnect=AsyncMock())
    voice_bot._connection._voice_clients = {10: voice}
    assert await voice_bot.voice_act("leave") is None
    voice.disconnect.assert_awaited_once()
    assert voice_bot.voice_where() is None and voice_bot.stream.channel_id is None
    assert not voice_bot.voice_marker.exists()

    voice_bot.hall.guild.voice_client = None
    await voice_bot.voice_act("join")
    monkeypatch.setattr(discord_bot, "VOICE_GRACE", 0.01)
    her = SimpleNamespace(id=BOT)
    await voice_bot.on_voice_state_update(her, None, SimpleNamespace(channel=None))
    assert voice_bot.stream.channel_id == 100  # It may be only a reconnect.
    await voice_bot.voice_drop  # Still out after the grace: someone disconnected her.
    assert voice_bot.voice_target is None and voice_bot.stream.channel_id is None
    assert not voice_bot.voice_marker.exists()


async def test_a_voice_reconnect_is_not_a_disconnect(voice_bot, monkeypatch):
    monkeypatch.setattr(discord_bot, "VOICE_GRACE", 0.05)
    voice_bot.hall.voice_states[20] = SimpleNamespace(self_stream=True)
    await voice_bot.voice_act("join")
    her = SimpleNamespace(id=BOT)
    await voice_bot.on_voice_state_update(her, None, SimpleNamespace(channel=None))
    voice_bot.hall.voice_states[BOT] = SimpleNamespace(self_stream=False)  # Back in.
    await voice_bot.on_voice_state_update(her, None, SimpleNamespace(channel=voice_bot.hall))
    await voice_bot.voice_drop
    assert voice_bot.voice_where() == "大厅" and voice_bot.stream.channel_id == 100
    assert json.loads(voice_bot.voice_marker.read_text()) == {"channel_id": 100}


async def test_after_a_restart_she_goes_back_where_he_put_her(voice_bot):
    voice_bot.voice_marker.write_text(json.dumps({"channel_id": 100}))
    voice_bot.hall.voice_states[20] = SimpleNamespace(self_stream=True)
    await voice_bot._restore_voice()
    assert voice_bot.hall.connect.called and voice_bot.voice_where() == "大厅"
    assert voice_bot.stream.channel_id == 100
