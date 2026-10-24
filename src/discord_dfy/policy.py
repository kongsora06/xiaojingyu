"""The Phase 1 event boundary: exact approved server channels, human authors only."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChannelPolicy:
    allowed_channel_ids: frozenset[int]

    def allows(self, *, guild_id: int | None, channel_id: int | None, is_bot: bool) -> bool:
        return guild_id is not None and channel_id in self.allowed_channel_ids and not is_bot
