from datetime import UTC, datetime

import pytest

from discord_dfy.config import Settings
from discord_dfy.models import IncomingMessage
from discord_dfy.policy import ChannelPolicy
from discord_dfy.storage import Storage


@pytest.fixture
def settings(tmp_path):
    return Settings(
        discord_bot_token="test-discord-token",
        deepseek_api_key="test-deepseek-key",
        allowed_channel_ids=frozenset({100}),
        database_path=tmp_path / "bot.sqlite3",
        agent_memory_path=tmp_path / "memory.md",
        social_sticker_dir=tmp_path / "stickers",
    )


@pytest.fixture
def incoming():
    return IncomingMessage(
        id=1,
        guild_id=10,
        channel_id=100,
        author_id=20,
        author_is_bot=False,
        content="<@999> Hello!",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def policy(settings):
    return ChannelPolicy(settings.allowed_channel_ids)


@pytest.fixture
async def storage(settings, policy):
    repository = Storage(settings.database_path, policy, store_messages=True)
    await repository.open()
    yield repository
    await repository.close()
