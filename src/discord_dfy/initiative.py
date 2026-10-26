"""She messages the owner first: after a quiet spell, the model decides whether it has
something it actually wants to say (its own news, a whim, wanting something, missing him,
how his day went, a reminder he asked for). The same look may also leave a few lines in
her diary.

The host decides only *when* it may ask: never during quiet hours, never while a turn is
running, and only after the gap since they last talked (AGENT_INITIATIVE_IDLE_MINUTES).
Each message of hers he hasn't answered yet doubles that gap, so she may text again but
won't keep texting all night while he sleeps. A daily cap is optional. What she sends
reaches the DM conversation the same way as group-side DMs: with the owner's next message.
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime

from openai import APIError

from .config import parse_quiet_hours
from .host_agent import fit, loads, request_messages, save_json

log = logging.getLogger(__name__)
CHECK_SECONDS = 600
# The gap stops doubling here (unanswered messages).
MAX_GAP_SECONDS = 8 * 3600
WEEKDAYS = "一二三四五六日"
NUDGE = """[宿主] 这不是老爹发的消息，是宿主在问你。现在是 {now}（周{weekday}）。
老爹上次跟你说话是 {ago} 前。你过去 24 小时主动找过他 {count} 次。{pending}{group}
你这会儿想找老爹吗？女儿给爸爸发消息，多半是因为自己这边有事，或者惦记着他：
群里谁逗到你或者惹到你了、突然想到一件事、想要个什么、想让他评评理、闲得发慌、想他了，
或者记着他那头的事（他说过几点下班、今天要干嘛），想知道后来怎样了。
想发就照你这会儿的心情直接说，几个字、一句话、几句话都行；
不用为了盯他吃饭睡觉找他，开口也不用报时间。他托你到点提醒的事，到点了就提醒一句。
他说了去睡、在上班、在写作业，就别隔一阵戳他一次，等他差不多回来了再说。
看看你前几次主动找他说的是什么，这次别又是同一种；没什么想说的就不发。
他没回你，多半是在忙或者睡了；要不要再找他、说什么，你自己看着办。
这会儿你也可以给自己写几句日记（今天怎么样、心情、在琢磨什么），想写才写，不写就是 null。
只输出 JSON：{{"message": "要发的话或 null", "diary": "日记或 null"}}。
用你平时私聊的口吻，别提到宿主。"""


def in_quiet_hours(spec: str, moment: datetime) -> bool:
    hours = parse_quiet_hours(spec) if spec else None
    if hours is None:
        return False
    start, end = hours
    minute = moment.hour * 60 + moment.minute
    return start <= minute < end if start <= end else minute >= start or minute < end


def ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    if minutes < 48 * 60:
        return f"{minutes // 60} 小时"
    return f"{minutes // 1440} 天"


class Initiative:
    def __init__(self, agent, reach_owner, *, clock=time.time, now=None, sleep=asyncio.sleep):
        self.agent = agent
        self.settings = agent.settings
        # Async callable(text): delivers to the owner's DM and records it.
        self.reach_owner = reach_owner
        self.clock = clock
        self.now = now or (lambda: datetime.now().astimezone())
        self.sleep = sleep
        self.path = self.settings.personal_database_path.parent / "initiative_state.json"
        state = self._load()
        self.enabled = bool(state.get("enabled", True))
        self.sent: list[float] = [float(t) for t in state.get("sent", [])]
        self.last_look = float(state.get("last_look", 0.0))
        self.task: asyncio.Task | None = None

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        save_json(
            self.path,
            {"enabled": self.enabled, "sent": self.sent, "last_look": self.last_look},
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self._save()

    def sent_today(self) -> int:
        cutoff = self.clock() - 86400
        self.sent = [t for t in self.sent if t > cutoff]
        return len(self.sent)

    def status(self) -> str:
        if not self.settings.agent_initiative:
            return "主动找你：在 .env 里关掉了（AGENT_INITIATIVE）"
        if not self.enabled:
            return "主动找你：关着"
        quiet = self.settings.agent_quiet_hours or "无"
        cap = self.settings.agent_initiative_per_day
        return (
            f"主动找你：开着 · 过去 24 小时 {self.sent_today()}"
            f"{f'/{cap}' if cap else ''} 次 · 间隔至少 "
            f"{self.settings.agent_initiative_idle_minutes} 分钟（你没回就翻倍）· 免打扰 {quiet}"
        )

    def gap(self, unanswered: int) -> float:
        """Seconds of quiet needed before she may look: doubles per unanswered message."""
        base = self.settings.agent_initiative_idle_minutes * 60
        return min(base * 2 ** min(unanswered, 10), max(base, MAX_GAP_SECONDS))

    def start(self) -> None:
        if self.settings.agent_initiative and self.task is None:
            self.task = asyncio.create_task(self.run())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def run(self) -> None:
        while True:
            await self.sleep(CHECK_SECONDS + random.uniform(0, 120))
            try:
                await self.consider()
            except Exception:
                log.exception("Initiative check failed")

    async def consider(self) -> bool:
        """One check; returns whether she sent something."""
        settings = self.settings
        if not (settings.agent_initiative and self.enabled) or self.agent.closing:
            return False
        if in_quiet_hours(settings.agent_quiet_hours, self.now()):
            return False
        cap = settings.agent_initiative_per_day
        if cap and self.sent_today() >= cap:
            return False
        channel = await self.agent.latest_channel()
        if channel is None:
            return False
        worker = self.agent.workers.get(channel)
        if worker is not None and not worker.done():
            return False
        # What she sent him (here or from the group) that he hasn't answered yet.
        pending = self.agent.outgoing.get(channel, [])
        gap = self.gap(len(pending))
        if self.clock() - self.last_look < gap:
            return False  # She passed on it recently; each look is one model request.
        store = self.agent.store
        last = max(
            [
                await store.last_message_at(channel, "user") or 0.0,
                await store.last_message_at(channel, "assistant") or 0.0,
                *self.sent[-1:],
            ]
        )
        idle = self.clock() - last
        if idle < gap:
            return False
        self.last_look = self.clock()
        self._save()
        text = await self._ask(channel, idle, pending)
        if not text:
            return False
        await self.reach_owner(text)
        self.sent.append(self.clock())
        self._save()
        log.info("Initiative message sent")
        return True

    async def _ask(self, channel, idle, pending) -> str | None:
        agent = self.agent
        history = await agent.store.load(channel)
        system = await agent.system_message(channel)
        moment = self.now()
        nudge = NUDGE.format(
            now=moment.strftime("%Y-%m-%d %H:%M"),
            weekday=WEEKDAYS[moment.weekday()],
            ago=ago(idle),
            count=self.sent_today(),
            pending=(
                "\n他还没回的、你之前发给他的私信：\n" + "\n".join(pending) if pending else ""
            ),
            group=(
                f"\n你最近在群里的经历：\n{digest}"
                if agent.group is not None
                and (digest := agent.group.digest_since(agent.group_seen))
                else ""
            ),
        )
        messages = [
            system,
            *request_messages(fit(history, self.settings.agent_context_chars)),
            {"role": "user", "content": nudge},
        ]
        try:
            async with asyncio.timeout(180):
                response = await agent.client.chat.completions.create(
                    model=self.settings.agent_model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=1024,
                    extra_body={"thinking": {"type": "disabled"}},
                )
        except (TimeoutError, APIError) as error:
            log.warning("Initiative request failed; error_type=%s", type(error).__name__)
            return None
        content = (response.choices[0].message.content or "") if response.choices else ""
        data = loads(content) or {}
        diary = data.get("diary")
        if isinstance(diary, str) and diary.strip():
            try:
                await asyncio.to_thread(agent.room.write_diary, diary[:4000], moment)
            except OSError:
                log.warning("Could not write the diary")
        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            return None
        return agent.sanitize(message.strip())[:1800]


SELF_TAG = "[宿主记录] 你主动"
