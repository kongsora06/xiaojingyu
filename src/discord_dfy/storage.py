"""Async SQLite repository with its own allowlist and storage-off enforcement."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite

from .models import IncomingMessage
from .policy import ChannelPolicy

EventName = Literal[
    "started", "ready", "stopped", "ai_error", "storage_error", "discord_error", "internal_error"
]

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    source TEXT NOT NULL CHECK(source IN ('message', 'ask')),
    message_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_created ON messages(channel_id, created_at);
CREATE TABLE IF NOT EXISTS bot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS social_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    outcome TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    reaction TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_social_decisions_time ON social_decisions(created_at);
"""


class Storage:
    def __init__(self, path: Path, policy: ChannelPolicy, *, store_messages: bool) -> None:
        self._path = path
        self._policy = policy
        self._store_messages = store_messages
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        await asyncio.to_thread(self._path.parent.mkdir, parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path, timeout=5)
        try:
            await db.executescript(SCHEMA)
            await db.commit()
        except BaseException:
            await db.close()
            raise
        self._db = db

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage is not open.")
        return self._db

    async def save_message(self, message: IncomingMessage) -> None:
        if not self._store_messages or not self._policy.allows(
            guild_id=message.guild_id,
            channel_id=message.channel_id,
            is_bot=message.author_is_bot or message.webhook_id is not None,
        ):
            return
        timestamp = message.created_at.astimezone(UTC).isoformat()
        async with self._lock:
            db = self._connection()
            try:
                await db.execute(
                    """INSERT INTO users(user_id, first_seen_at, last_seen_at) VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                    first_seen_at = MIN(first_seen_at, excluded.first_seen_at),
                    last_seen_at = MAX(last_seen_at, excluded.last_seen_at)""",
                    (message.author_id, timestamp, timestamp),
                )
                await db.execute(
                    """INSERT OR IGNORE INTO messages
                    (source, message_id, guild_id, channel_id, user_id, content, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        message.source,
                        message.id,
                        message.guild_id,
                        message.channel_id,
                        message.author_id,
                        message.content,
                        timestamp,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def record_event(self, event: EventName) -> None:
        # Deliberately no free-form error text, identifiers, credentials, or prompts.
        async with self._lock:
            db = self._connection()
            try:
                await db.execute(
                    "INSERT INTO bot_events(event_type, created_at) VALUES (?, ?)",
                    (event, datetime.now(UTC).isoformat()),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def close(self) -> None:
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    async def record_social_decision(
        self,
        *,
        channel_id: int,
        trigger: str,
        outcome: str,
        sent: int = 0,
        reaction: str | None = None,
        prompt_tokens: int = 0,
        cached_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        # Metadata only: what happened and what it cost, never the conversation itself.
        async with self._lock:
            db = self._connection()
            try:
                await db.execute(
                    """INSERT INTO social_decisions
                    (created_at, channel_id, trigger, outcome, sent, reaction,
                     prompt_tokens, cached_tokens, completion_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        datetime.now(UTC).isoformat(),
                        channel_id,
                        trigger,
                        outcome,
                        sent,
                        reaction,
                        prompt_tokens,
                        cached_tokens,
                        completion_tokens,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
