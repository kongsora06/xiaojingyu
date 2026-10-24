"""Channel policy plus storage for incoming messages; replies come from the group brain."""

import logging

import aiosqlite

from .models import IncomingMessage
from .policy import ChannelPolicy
from .storage import EventName, Storage

log = logging.getLogger(__name__)


class AgentService:
    def __init__(self, storage: Storage, policy: ChannelPolicy) -> None:
        self._storage = storage
        self.policy = policy

    def accepts(self, message: IncomingMessage) -> bool:
        return self.policy.allows(
            guild_id=message.guild_id,
            channel_id=message.channel_id,
            is_bot=message.author_is_bot or message.webhook_id is not None,
        )

    async def record_event(self, event: EventName) -> None:
        try:
            await self._storage.record_event(event)
        except (aiosqlite.Error, OSError):
            log.error("Event storage failed; event=%s", event)

    async def record_message(self, message: IncomingMessage) -> bool:
        """Store an approved human message; False means it must be ignored entirely."""
        if not self.accepts(message):
            return False
        try:
            await self._storage.save_message(message)
        except (aiosqlite.Error, OSError):
            # A disk failure should not stop the bot from chatting.
            log.error("Message storage failed")
            await self.record_event("storage_error")
        return True
