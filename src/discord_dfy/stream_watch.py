"""Watching the owner's stream.

While the owner shares his screen (Go Live) in an approved voice channel, the main screen
is captured every WATCH_SECONDS. When the picture changed, a cheap call (no persona, no
thinking) writes one line about what is on it. The group brain reads those lines, gets a
fresh capture whenever someone talks to her there, and now and then
(WATCH_COMMENT_MINUTES) takes a look on her own and decides whether to say anything.

Frames and lines live in memory only and are dropped when the stream ends. Nothing is
captured while he is not live.
"""

import asyncio
import logging
import time
from collections import deque
from datetime import datetime

from openai import APIError, AsyncOpenAI

from .ai import error_kind
from .config import Settings
from .images import data_url
from .redaction import redact
from .screen import Frame, difference, grab_primary

log = logging.getLogger(__name__)
# A picture this different from the last described one gets a new line.
CHANGED = 0.04
# A frame this recent counts as "just now" when someone asks her to look.
FRESH = 2.0
MAX_NOTES, SHOWN_NOTES = 12, 8

LOOK_RULES = """你在帮朋友记下直播画面。看这张截图，用一句中文写画面上正在发生什么：
在玩什么游戏、看什么视频或网页、在做什么，看得出的具体东西（关卡、比分、角色、标题、明显的字）就写上。
不超过 50 个字，只描述不评价，不加前缀。
跟上一句说的是同一件事、没有什么新变化，就只回答：同上"""


class StreamWatch:
    def __init__(
        self,
        settings: Settings,
        client: AsyncOpenAI,
        *,
        storage=None,
        grab=grab_primary,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.settings = settings
        self.client = client.with_options(max_retries=1, timeout=60.0)
        self.storage = storage
        self.grab = grab
        self.clock = clock
        self.sleep = sleep
        # Set by the app: called with the channel when she should take a look on her own,
        # and whether she may talk in the group at all (no point watching otherwise).
        self.on_look = None
        self.active = None
        self.channel_id: int | None = None
        self.notes: deque[tuple[datetime, str]] = deque(maxlen=MAX_NOTES)
        self.frame: Frame | None = None
        self.frame_at = float("-inf")
        # The thumbnail of the picture the last line was written about.
        self.noted = b""
        self.new_notes = 0
        self.looked_at = float("-inf")
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    def live_in(self, channel_id: int) -> bool:
        return self.channel_id is not None and channel_id == self.channel_id

    def start(self, channel_id: int) -> None:
        if self.channel_id == channel_id and self.task is not None and not self.task.done():
            return
        self.stop()
        self.channel_id = channel_id
        self.looked_at = self.clock()
        self.task = asyncio.create_task(self._run(channel_id))
        log.info("Watching the owner's stream")

    def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None
        if self.channel_id is not None:
            log.info("Stopped watching the stream")
        self.channel_id = None
        self.notes.clear()
        self.frame, self.frame_at = None, float("-inf")
        self.noted = b""
        self.new_notes = 0

    async def close(self) -> None:
        task = self.task
        self.stop()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def capture(self) -> Frame | None:
        async with self.lock:
            if self.channel_id is None:
                return None
            try:
                frame = await asyncio.to_thread(self.grab)
            except Exception as error:
                log.warning("Screen capture failed; error_type=%s", type(error).__name__)
                return None
            if self.channel_id is None:
                return None  # The stream ended while we were capturing.
            self.frame, self.frame_at = frame, self.clock()
            return frame

    async def frame_url(self, max_age: float = FRESH) -> str | None:
        """The stream as it is now (or as of max_age seconds ago), for the model."""
        if self.channel_id is None:
            return None
        if self.frame is not None and self.clock() - self.frame_at <= max_age:
            frame = self.frame
        else:
            frame = await self.capture()
        return data_url(frame.jpeg) if frame is not None else None

    def seen(self) -> list[str]:
        secrets = (self.settings.discord_bot_token, self.settings.deepseek_api_key)
        recent = list(self.notes)[-SHOWN_NOTES:]
        return [f"{at:%H:%M} {redact(text, secrets)}" for at, text in recent]

    async def _run(self, channel_id: int) -> None:
        while self.channel_id == channel_id:
            try:
                await self.step(channel_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Stream watch step failed")
            await self.sleep(self.settings.watch_seconds)

    async def step(self, channel_id: int) -> None:
        """One frame: a line about it when it changed, then maybe her own look."""
        if self.active is not None and not self.active():
            return  # Group chat is off: she could not say anything about it anyway.
        frame = await self.capture()
        if frame is not None and difference(frame.thumb, self.noted) >= CHANGED:
            await self._note(channel_id, frame)
        if self._look_due():
            self.looked_at = self.clock()
            self.new_notes = 0
            self.on_look(channel_id)

    def _look_due(self) -> bool:
        minutes = self.settings.watch_comment_minutes
        return (
            minutes > 0
            and self.on_look is not None
            and self.new_notes > 0
            and self.clock() - self.looked_at >= minutes * 60
        )

    async def _note(self, channel_id: int, frame: Frame) -> None:
        previous = self.notes[-1][1] if self.notes else None
        content = [
            {"type": "text", "text": f"上一句：{previous}" if previous else "这是开播后的第一张。"},
            {"type": "image_url", "image_url": {"url": data_url(frame.jpeg)}},
        ]
        try:
            async with asyncio.timeout(60):
                response = await self.client.chat.completions.create(
                    model=self.settings.agent_model,
                    messages=[
                        {"role": "system", "content": LOOK_RULES},
                        {"role": "user", "content": content},
                    ],
                    max_tokens=120,
                    extra_body={"thinking": {"type": "disabled"}},
                )
        except (TimeoutError, APIError) as error:
            log.warning("Stream look failed; error_type=%s", error_kind(error))
            return
        text = response.choices[0].message.content if response.choices else ""
        lines = (text or "").strip().splitlines()
        text = lines[0].strip()[:120] if lines else ""
        self.noted = frame.thumb
        same = not text or text.startswith("同上")
        await self._record(channel_id, "same" if same else "noted", response.usage)
        if not same and self.channel_id == channel_id:
            self.notes.append((datetime.now().astimezone(), text))
            self.new_notes += 1

    async def _record(self, channel_id: int, outcome: str, usage) -> None:
        if self.storage is None:
            return
        try:
            await self.storage.record_social_decision(
                channel_id=channel_id,
                trigger="watch",
                outcome=outcome,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                cached_tokens=getattr(usage, "prompt_cache_hit_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            )
        except Exception:
            log.warning("Could not record a stream look")
