import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from discord_dfy.service import AgentService
from discord_dfy.storage import Storage


async def rows(path, table):
    # Table names are test-controlled, never application input.
    async with aiosqlite.connect(path) as db:
        async with db.execute(f"SELECT * FROM {table}") as cursor:
            return await cursor.fetchall()


async def test_schema_persistence_deduplication_and_user_updates(settings, storage, incoming):
    await storage.save_message(incoming)
    await storage.save_message(incoming)
    later = replace(incoming, id=2, created_at=incoming.created_at + timedelta(hours=1))
    await storage.save_message(later)
    await storage.record_event("ready")
    await storage.close()
    await storage.open()
    messages = await rows(settings.database_path, "messages")
    users = await rows(settings.database_path, "users")
    assert len(messages) == 2
    assert messages[0][5] == incoming.content
    assert len(users) == 1
    assert users[0][1] == incoming.created_at.isoformat()
    assert users[0][2] == later.created_at.isoformat()
    events = await rows(settings.database_path, "bot_events")
    assert len(events) == 1
    assert events[0][1] == "ready"


@pytest.mark.parametrize(
    "changes",
    [
        {"guild_id": None},
        {"channel_id": 101},
        {"author_is_bot": True},
        {"webhook_id": 5},
    ],
)
async def test_storage_and_service_each_reject_unapproved(
    settings, storage, policy, incoming, changes
):
    message = replace(incoming, **changes)
    service = AgentService(storage, policy)
    await storage.save_message(message)  # Repository itself must enforce the boundary.
    assert not await service.record_message(message)
    assert not await rows(settings.database_path, "messages")
    assert not await rows(settings.database_path, "users")


async def test_disabled_storage_has_no_message_or_user_writes(settings, policy, incoming):
    storage = Storage(settings.database_path, policy, store_messages=False)
    await storage.open()
    try:
        service = AgentService(storage, policy)
        assert await service.record_message(incoming)
        assert await service.record_message(replace(incoming, source="ask"))
        await storage.record_event("started")
        assert not await rows(settings.database_path, "messages")
        assert not await rows(settings.database_path, "users")
        assert len(await rows(settings.database_path, "bot_events")) == 1
    finally:
        await storage.close()


async def test_ask_is_stored_with_its_source(settings, storage, policy, incoming):
    service = AgentService(storage, policy)
    assert await service.record_message(replace(incoming, content="Question", source="ask"))
    assert (await rows(settings.database_path, "messages"))[0][0] == "ask"


async def test_storage_failure_does_not_stop_the_message(policy, incoming):
    storage = AsyncMock()
    storage.save_message.side_effect = aiosqlite.OperationalError("private DB details")
    service = AgentService(storage, policy)
    assert await service.record_message(incoming)
    storage.record_event.assert_awaited_once_with("storage_error")


async def test_concurrent_writes_are_committed(settings, storage, incoming):
    await asyncio.gather(
        *(storage.save_message(replace(incoming, id=index)) for index in range(1, 21))
    )
    assert len(await rows(settings.database_path, "messages")) == 20
    assert len(await rows(settings.database_path, "users")) == 1


async def test_social_decisions_store_metadata_only(settings, storage):
    await storage.record_social_decision(
        channel_id=100,
        trigger="must",
        outcome="spoke",
        sent=2,
        reaction="😂",
        prompt_tokens=50,
        cached_tokens=40,
        completion_tokens=5,
    )
    (row,) = await rows(settings.database_path, "social_decisions")
    assert row[2:] == (100, "must", "spoke", 2, "😂", 50, 40, 5)
