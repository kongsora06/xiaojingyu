"""Discord-independent input records; no Discord objects reach the AI layer."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    id: int
    guild_id: int | None
    channel_id: int
    author_id: int
    author_is_bot: bool
    content: str
    created_at: datetime
    source: Literal["message", "ask"] = "message"
    webhook_id: int | None = None
    reply_to_message_id: int | None = None
    reply_to_author_id: int | None = None


@dataclass(frozen=True, slots=True)
class IncomingReaction:
    message_id: int
    guild_id: int | None
    channel_id: int
    author_id: int
    author_is_bot: bool
    emoji: str
    created_at: datetime
