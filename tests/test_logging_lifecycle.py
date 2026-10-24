import io
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from discord_dfy.__main__ import run
from discord_dfy.logging_setup import SafeFormatter, configure_logging


def test_formatter_redacts_secrets_and_exception_text():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeFormatter(("my-bot-token", "my-api-key")))
    logger = logging.Logger("test-safe-logging")
    logger.addHandler(handler)
    try:
        raise RuntimeError("private-prompt and my-bot-token")
    except RuntimeError:
        logger.exception("key=my-api-key token=my-bot-token Authorization: Bearer other-token")
    result = stream.getvalue()
    assert "my-bot-token" not in result
    assert "my-api-key" not in result
    assert "private-prompt" not in result
    assert "other-token" not in result
    assert "[REDACTED]" in result


def test_debug_logging_cannot_enable_provider_body_logs():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        configure_logging(level="DEBUG", secrets=("token",))
        for name in ("openai", "httpx2", "discord", "aiosqlite"):
            assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)
    finally:
        root.handlers, root.level = handlers, level


@pytest.mark.parametrize("failure", [None, discord.LoginFailure("test invalid token")])
async def test_application_closes_resources_on_stop_and_login_failure(settings, failure):
    storage = AsyncMock()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.with_options = MagicMock(return_value=client)
    bot = AsyncMock()
    bot.__aenter__.return_value = bot
    bot.start.side_effect = failure
    group = MagicMock(close=AsyncMock())
    with (
        patch("discord_dfy.__main__.Storage", return_value=storage),
        patch("discord_dfy.__main__.AsyncOpenAI", return_value=client) as sdk,
        patch("discord_dfy.__main__.DiscordAgent", return_value=bot),
        patch("discord_dfy.__main__.GroupChat", return_value=group) as group_chat,
    ):
        if failure:
            with pytest.raises(discord.LoginFailure):
                await run(settings)
        else:
            await run(settings)
    storage.open.assert_awaited_once()
    storage.close.assert_awaited_once()
    client.__aexit__.assert_awaited_once()
    bot.__aexit__.assert_awaited_once()
    bot.start.assert_awaited_once_with(settings.discord_bot_token)
    assert sdk.call_args.kwargs["base_url"] == "https://api.deepseek.com"
    assert sdk.call_args.kwargs["max_retries"] == 2
    assert group_chat.call_args.args[2] is bot.transport and bot.group is group
    group.close.assert_awaited_once()
    assert [call.args[0] for call in storage.record_event.await_args_list] == ["started", "stopped"]
