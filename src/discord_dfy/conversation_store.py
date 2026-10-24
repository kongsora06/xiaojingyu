"""Private SQLite history for owner DM conversations. `!new` starts a fresh one; old rows stay."""

import asyncio
import json
import time
from pathlib import Path

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS agent_conversations (
    channel_id INTEGER PRIMARY KEY,
    current INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    conversation INTEGER NOT NULL,
    role TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_messages_lookup ON agent_messages(channel_id, conversation, id);
"""


class ConversationStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.db: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self):
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def _current(self, channel_id: int) -> int:
        cursor = await self.db.execute(
            "SELECT current FROM agent_conversations WHERE channel_id=?", (channel_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 1

    async def load(self, channel_id: int) -> list[dict]:
        async with self.lock:
            conversation = await self._current(channel_id)
            cursor = await self.db.execute(
                "SELECT message FROM agent_messages WHERE channel_id=? AND conversation=? "
                "ORDER BY id",
                (channel_id, conversation),
            )
            return [json.loads(row[0]) for row in await cursor.fetchall()]

    async def append(self, channel_id: int, messages: list[dict]) -> None:
        if not messages:
            return
        async with self.lock:
            conversation = await self._current(channel_id)
            now = time.time()
            await self.db.executemany(
                "INSERT INTO agent_messages(channel_id,conversation,role,message,created_at) "
                "VALUES(?,?,?,?,?)",
                [
                    (channel_id, conversation, m["role"], json.dumps(m, ensure_ascii=False), now)
                    for m in messages
                ],
            )
            await self.db.commit()

    async def new_conversation(self, channel_id: int, messages: list[dict] = ()) -> None:
        """Switch to a fresh conversation, optionally seeded (compaction); old rows stay."""
        async with self.lock:
            conversation = await self._current(channel_id) + 1
            await self.db.execute(
                "INSERT INTO agent_conversations(channel_id,current) VALUES(?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET current=excluded.current",
                (channel_id, conversation),
            )
            now = time.time()
            await self.db.executemany(
                "INSERT INTO agent_messages(channel_id,conversation,role,message,created_at) "
                "VALUES(?,?,?,?,?)",
                [
                    (channel_id, conversation, m["role"], json.dumps(m, ensure_ascii=False), now)
                    for m in messages
                ],
            )
            await self.db.commit()

    async def current(self, channel_id: int) -> int:
        async with self.lock:
            return await self._current(channel_id)

    async def channels(self) -> list[int]:
        """Channels that have ever had a conversation (for the owner: their DM)."""
        async with self.lock:
            cursor = await self.db.execute("SELECT DISTINCT channel_id FROM agent_messages")
            return [row[0] for row in await cursor.fetchall()]

    async def last_message_at(self, channel_id: int, role: str = "user") -> float | None:
        async with self.lock:
            cursor = await self.db.execute(
                "SELECT MAX(created_at) FROM agent_messages WHERE channel_id=? AND role=?",
                (channel_id, role),
            )
            row = await cursor.fetchone()
            return row[0] if row else None
