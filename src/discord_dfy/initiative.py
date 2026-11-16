"""Her time while the owner is away: after a quiet spell she gets a while to herself.

She may use her own things (diary, room, notebook, the web, saving pictures) as she likes,
then decides whether she has something she actually wants to say to him (her own news, a
whim, wanting something, missing him, how his day went, a reminder he asked for). What she
did is told to the DM side with his next message; he does not see it.

The host decides only *when* it may ask: never during quiet hours, never while a turn is
running, and only after the gap since they last talked (AGENT_INITIATIVE_IDLE_MINUTES).
Each message of hers he hasn't answered yet doubles that gap, so she may text again but
won't keep texting all night while he sleeps. A daily cap is optional. What she sends
reaches the DM conversation the same way as group-side DMs: with the owner's next message.

Free time is the other way round: he leaves her a stretch on purpose (!play, or telling
her to go and play, which she takes with go_play). Until it runs out she looks around in
rounds, one after another, each a spell of its own that begins with the note she left
herself at the end of the last. The waiting and the hours above do not hold her back
then; they only ever limited when she may be given time, and here he gave it. She does
not message him during it: when it is over, the next ordinary look decides that.
"""

import asyncio
import json
import logging
import random
import re
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta

from openai import APIError

from .ai import error_kind
from .config import in_peak, parse_quiet_hours
from .host_agent import NOTE_TAG, cut_run_on, fit, loads, request_messages, save_json
from .host_tools import ToolError, describe_call, monitor_text, what_she_did
from .images import file_block
from .streaming import collect_stream

log = logging.getLogger(__name__)
CHECK_SECONDS = 600
# The gap stops doubling here (unanswered messages).
MAX_GAP_SECONDS = 8 * 3600
WEEKDAYS = "一二三四五六日"
# Tool steps she may take on her own in one quiet spell.
IDLE_STEPS = 15
# Free time he left her: she starts once the chat has been quiet this long, and rests
# this long between rounds (and before trying again while he is still around).
PLAY_QUIET_SECONDS = 180
PLAY_REST_SECONDS = 60
PLAY_DEFAULT_MINUTES = 60
PLAY_MIN_MINUTES = 5
# How often in a day she may start it herself (go_play); his own !play is not counted.
SELF_GRANTS_PER_DAY = 3
# The note she leaves herself between rounds, and how much of her latest finds she sees again.
CARRY_CHARS = 600
FINDS_PEEK = 800
# Her last reply in a round when she has had enough for today.
DONE = "（逛够了）"
DONE_MARK = re.compile(r"[（(]\s*逛够了\s*[)）]")
LOOKING = {"web_search", "web_fetch"}
# Her last reply when she does not want to message him.
SKIP = "（不找他）"
SKIP_MARK = re.compile(r"[（(]\s*不找他\s*[)）]")
# How much of her last diary entry she sees again when she is on her own.
DIARY_PEEK = 800
GENTLE = (
    "\n上回写过的，就不用从头再写一遍啦。想写的话，接着上回往下写，"
    "或者写点新冒出来的心思就好；没什么新鲜的，这回不写也没关系的。"
)
NUDGE = """[提示] 这不是主人发的消息。现在是 {now}（周{weekday}），
主人上次跟你说话是 {ago} 前，这会儿他不在。{pending}{group}{before}
这会儿是你自己的时间。可以写日记、在房间里写点东西、上网逛逛、收张图、改个状态，也可以什么都不做。
这阵子聊天里、群里要是有你没看懂的词和梗、谁提到过你不认识的东西，或者你自己好奇的事，
这会儿正好上网搜一下看看；也可以随便逛逛最近大家都在聊什么。
看明白了、觉得以后还会碰到的，用自己的话 remember 记一笔；看到好玩的，可以留着讲给主人听。
如果想认真写点什么，可以慢慢写，不受平时聊天篇幅的限制。
<self> 有持续的新认识时再改，不需要每次检查自己是什么样的。
（send_file、去群里说话、换头像名字这会儿都用不了，等他回来再说。）
至于主人——
有什么想跟他说的，就直接说。
不一定是重要的事，也不一定要有理由。（比如说想他了）
可能是一件有趣的发现、一个突然冒出的念头，也可能只是想起了之前没聊完的话题。
没有新内容也很正常，不需要为了完成这次提示而找他、写日记或展示性格。
他忙时可以把值得分享的话留给他有空再看。
他明确说要睡或想独处时，就尊重这段时间。
不催他回复。他托你到点提醒的事，到点就提醒他。
想发，就把要发给他的话写出来当最后的回复，跟平时私聊一样说话，别提这条提示；
不想发，就只回 {skip}。"""
PLAY_NUDGE = """[提示] 这不是主人发的消息。现在是 {now}（周{weekday}），主人不在。
他走之前留了一段时间给你自己玩，到 {until} 为止（还剩大约 {left}）。{wish}
这是留给你玩的，不是布置的任务：想上网逛就放开了逛，搜你好奇的、点进去看全文、
顺着看到的再往下找，不用赶，也不用每一步都有用；也可以写东西、收图、写日记，逛一半不想逛了也行。{finds}{carry}
这段时间不用找他，等他回来再讲给他听。（send_file、去群里说话、换头像名字这会儿都用不了。）
这一轮逛得差不多了，就用一两句话给过一会儿的自己留个话：刚看了什么、接下来想看什么——
你会带着这句话接着逛下一轮。今天不想再逛了，就只回 {done}。"""
FINDS_HINT = """
逛到有意思的，用 note_find 记进「逛到的」本子：是什么、网址、你自己怎么想的，随手记。
主人回来会翻这一本。"""
PLAY_OVER = "\n他留给你自己玩的那段时间到这儿结束了；逛到了什么想讲给他听的，可以讲。"


def in_quiet_hours(spec: str, moment: datetime) -> bool:
    hours = parse_quiet_hours(spec) if spec else None
    if hours is None:
        return False
    start, end = hours
    minute = moment.hour * 60 + moment.minute
    return start <= minute < end if start <= end else minute >= start or minute < end


def load_play(raw) -> dict | None:
    """The free time in the state file, or None when there is none (or it is damaged)."""
    if not isinstance(raw, dict):
        return None
    try:
        return {
            "until": float(raw["until"]),
            "steps": int(raw["steps"]),
            "wish": str(raw.get("wish") or ""),
            "carry": str(raw.get("carry") or ""),
            "rounds": int(raw.get("rounds") or 0),
            "tokens": int(raw.get("tokens") or 0),
        }
    except (KeyError, TypeError, ValueError):
        return None


def summed(done: list[tuple[str, str]]) -> list[str]:
    """A round of free time in a few lines: the looking around counted, the rest kept."""
    searches = sum(name == "web_search" for name, _ in done)
    pages = sum(name == "web_fetch" for name, _ in done)
    lines = [line for name, line in done if name not in LOOKING]
    if searches or pages:
        lines.insert(0, f"上网逛了一圈（搜了 {searches} 次，看了 {pages} 个网页）")
    return lines


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
        # The free time he left her, if any: when it ends, the steps left, what she is
        # after, her note to herself from the last round, and what it has used so far.
        self.play: dict | None = load_play(state.get("play"))
        self.self_grants: list[float] = [float(t) for t in state.get("self_grants", [])]
        # Set when free time has just run out, for a line in her next ordinary look.
        self.play_over = False
        # Prompt + completion tokens of every request made here.
        self.tokens = 0
        self.wake = asyncio.Event()
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
            {
                "enabled": self.enabled,
                "sent": self.sent,
                "last_look": self.last_look,
                "play": self.play,
                "self_grants": self.self_grants,
            },
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

    def playing(self) -> bool:
        play = self.play
        return play is not None and self.clock() < play["until"] and play["steps"] > 0

    def grant(self, minutes=None, wish="", *, by_her=False) -> str:
        """He leaves her a stretch of free time: his !play, or her go_play when he says so."""
        now = self.clock()
        if by_her:
            if self.playing():
                return f"已经开着了。{self.play_status()}"
            self.self_grants = [t for t in self.self_grants if t > now - 86400]
            if len(self.self_grants) >= SELF_GRANTS_PER_DAY:
                raise ToolError(
                    f"这一天你自己已经开过 {SELF_GRANTS_PER_DAY} 次了；"
                    "还想逛，就请主人用 !play 给你开"
                )
            self.self_grants.append(now)
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            minutes = PLAY_DEFAULT_MINUTES
        minutes = max(PLAY_MIN_MINUTES, min(minutes, self.settings.agent_play_max_minutes))
        self.play = {
            "until": now + minutes * 60,
            "steps": self.settings.agent_play_steps,
            "wish": " ".join(str(wish or "").split())[:200],
            "carry": "",
            "rounds": 0,
            "tokens": 0,
        }
        self.play_over = False
        self._save()
        self.wake.set()
        return self.play_status()

    def stop_play(self) -> bool:
        """He calls it off; a round that is running stops at its next step."""
        had, self.play = self.playing(), None
        self._save()
        return had

    def _end_play(self) -> None:
        self.play, self.play_over = None, True
        self._save()

    def play_status(self) -> str:
        play = self.play
        if play is None or not self.playing():
            return "自由时间：现在没有（`!play 2h` 留一段给她，后面可以写想让她找什么）"
        left = play["until"] - self.clock()
        end = self.now() + timedelta(seconds=left)
        wish = f" · 想逛的：{play['wish']}" if play["wish"] else ""
        return (
            f"自由时间：到 {end:%H:%M} 为止（还剩 {int(left // 60)} 分钟、{play['steps']} 步）"
            f" · 逛了 {play['rounds']} 轮，约 {play['tokens']} tokens{wish}"
        )

    def start(self) -> None:
        # Also with AGENT_INITIATIVE off: free time he grants still needs the loop.
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def run(self) -> None:
        while True:
            await self._rest()
            try:
                await self.consider()
            except Exception:
                log.exception("Initiative check failed")

    async def _rest(self) -> None:
        """Until the next check: a short while in her free time, and no wait at all when
        he has just granted some."""
        if self.play is not None:
            await self.sleep(PLAY_REST_SECONDS)
            return
        self.wake.clear()
        waits = [
            asyncio.ensure_future(self.sleep(CHECK_SECONDS + random.uniform(0, 120))),
            asyncio.ensure_future(self.wake.wait()),
        ]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()

    async def consider(self) -> bool:
        """One check; returns whether she sent something."""
        settings = self.settings
        if self.agent.closing:
            return False
        if self.play is not None:
            if self.playing():
                await self._play_round()
            else:
                self._end_play()
            if self.play is not None:
                return False  # Still her free time: no messages to him from it.
        if not (settings.agent_initiative and self.enabled):
            return False
        if in_quiet_hours(settings.agent_quiet_hours, self.now()):
            return False
        if settings.agent_skip_peak and in_peak(self.now()):
            return False  # Whatever she has keeps until peak hours end.
        cap = settings.agent_initiative_per_day
        if cap and self.sent_today() >= cap:
            return False
        group = self.agent.group
        if group is not None and group.watching_owner():
            return False  # She is watching his stream with him: he is right here.
        channel = await self.agent.latest_channel()
        if channel is None:
            return False
        worker = self.agent.workers.get(channel)
        if worker is not None and not worker.done():
            return False
        if self.agent.owner_typing(channel):
            return False  # He is writing to her right now.
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
                # Him talking in the group counts too: he is around, not away.
                group.owner_seen_at if group is not None else 0.0,
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

    def _he_is_here(self, channel) -> bool:
        worker = self.agent.workers.get(channel)
        return (worker is not None and not worker.done()) or self.agent.owner_typing(channel)

    async def _play_round(self) -> None:
        """One round of the free time he left her, if he really is away."""
        agent, play = self.agent, self.play
        group = agent.group
        if group is not None and group.watching_owner():
            return
        channel = await agent.latest_channel()
        if channel is None or self._he_is_here(channel):
            return
        store = agent.store
        last = max(
            await store.last_message_at(channel, "user") or 0.0,
            await store.last_message_at(channel, "assistant") or 0.0,
            group.owner_seen_at if group is not None else 0.0,
        )
        if self.clock() - last < PLAY_QUIET_SECONDS:
            return  # They were talking a moment ago; she goes once he has left.
        moment = self.now()
        left = play["until"] - self.clock()
        finds = ""
        if agent.web is not None:
            finds = FINDS_HINT
            if seen := await asyncio.to_thread(agent.finds.recent, FINDS_PEEK):
                finds += f"\n本子上最近记的是这些，记过的不用再记：\n{seen}"
        nudge = PLAY_NUDGE.format(
            now=moment.strftime("%Y-%m-%d %H:%M"),
            weekday=WEEKDAYS[moment.weekday()],
            until=(moment + timedelta(seconds=left)).strftime("%H:%M"),
            left=ago(left),
            wish=f"\n这次想逛的：{play['wish']}" if play["wish"] else "",
            finds=finds,
            carry=f"\n上一轮结束时你给自己留的话：{play['carry']}" if play["carry"] else "",
            done=DONE,
        )
        before = self.tokens
        text, used = await self._spell(
            channel,
            nudge,
            min(self.settings.agent_play_round_steps, play["steps"]),
            session="自由时间",
            label=lambda text: (
                "逛够了，自由时间结束" if text and DONE_MARK.search(text) else "这一轮逛完了"
            ),
            go_on=lambda: self.play is play and self.playing(),
            brief=True,
        )
        if self.play is not play:
            return  # He called it off meanwhile.
        play["steps"] -= used
        play["rounds"] += bool(used)
        play["tokens"] += self.tokens - before
        if text and DONE_MARK.search(text):
            self._end_play()
            return
        if text:
            play["carry"] = text[:CARRY_CHARS]
        self._save()

    async def _ask(self, channel, idle, pending) -> str | None:
        """Her quiet spell: she may do her own things, then returns what to send him."""
        agent = self.agent
        moment = self.now()
        over, self.play_over = (PLAY_OVER if self.play_over else ""), False
        nudge = NUDGE.format(
            now=moment.strftime("%Y-%m-%d %H:%M"),
            weekday=WEEKDAYS[moment.weekday()],
            ago=ago(idle),
            pending=(
                "\n他还没回的、你之前发给他的私信：\n" + "\n".join(pending) if pending else ""
            ),
            group=(
                f"\n你最近在群里的经历：\n{digest}"
                if agent.group is not None
                and (digest := agent.group.digest_since(agent.group_seen))
                else ""
            ),
            before=over + await self._before(channel),
            skip=SKIP,
        )
        text, _ = await self._spell(
            channel,
            nudge,
            IDLE_STEPS,
            session="自己待着",
            label=lambda text: "这次不找主人" if self._skipped(text) else "发给主人了",
        )
        # The skip mark anywhere means no: what follows it is her talking to herself.
        return None if self._skipped(text) else agent.sanitize(text)[:4000]

    @staticmethod
    def _skipped(text) -> bool:
        return not text or bool(SKIP_MARK.search(text))

    async def _spell(self, channel, nudge, steps, *, session, label, go_on=None, brief=False):
        """A stretch on her own: the model and her tools in turn, until she has a last
        word, the steps run out, or he is back. Returns (her last words or None, the
        requests made)."""
        agent = self.agent
        history = await agent.store.load(channel)
        await agent.refresh_self(channel)  # Her quiet spell is a turn of its own.
        system = await agent.system_message(channel)
        turn = [{"role": "user", "content": nudge}]
        done: list[tuple[str, str]] = []
        text, used = None, 0
        # The Live Monitor shows her thinking, what she did and her answer; never what is
        # written in her room or diary.
        publisher = (
            agent.observer.publisher("personal", session_id=session, task_id=uuid.uuid4().hex)
            if agent.observer is not None
            else None
        )
        try:
            for _ in range(steps):
                if self._he_is_here(channel) or (go_on is not None and not go_on()):
                    return None, used  # He is back (the chat takes over), or time is up.
                used += 1
                message = await self._complete(system, [*history, *turn], publisher)
                if message is None:
                    return None, used
                if not message.tool_calls:
                    text, _ = cut_run_on((message.content or "").strip())
                    break
                await self._use_tools(channel, message, turn, done, publisher)
        finally:
            if lines := summed(done) if brief else [line for _, line in done]:
                await agent.note_activity(channel, lines)
        if publisher:
            publisher.event("task-status", status="completed", text=label(text))
        return text, used

    async def _before(self, channel) -> str:
        """What she already did while he was away, and her last diary entry, so she goes
        on from there instead of starting over each time."""
        agent = self.agent
        parts = []
        if notes := [
            note.removeprefix(NOTE_TAG).strip() for note in agent.activity.get(channel, [])
        ]:
            parts.append("\n他不在的这阵子，你自己做过的事：\n" + "\n".join(notes))
        if last := await asyncio.to_thread(agent.room.last_diary):
            peek = last if len(last) <= DIARY_PEEK else last[:DIARY_PEEK] + "……"
            parts.append(f"\n你上一篇日记是这样写的：\n{peek}{GENTLE}")
        return "".join(parts)

    async def _complete(self, system, conversation, publisher=None):
        agent = self.agent
        settings = self.settings
        messages = [
            system,
            *request_messages(fit(conversation, settings.agent_context_chars)),
        ]
        # She thinks here the way she does in the chat.
        thinking = settings.agent_thinking
        request_id = uuid.uuid4().hex
        stream = publisher is not None
        try:
            async with asyncio.timeout(600):
                response = await agent.client.chat.completions.create(
                    model=settings.agent_model,
                    messages=messages,
                    # The same tools as in the chat, so the cached prefix is shared.
                    tools=agent.tool_specs(),
                    max_tokens=8192,
                    extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
                    stream=stream,
                    **({"reasoning_effort": settings.agent_reasoning_effort} if thinking else {}),
                    **({"stream_options": {"include_usage": True}} if stream else {}),
                )
                if stream:
                    response = await collect_stream(response, publisher, request_id)
        except (TimeoutError, APIError) as error:
            log.warning("Initiative request failed; error_type=%s", error_kind(error))
            if publisher:
                publisher.event("model-status", status="failed", request_id=request_id)
            return None
        if response.usage:
            self.tokens += (response.usage.prompt_tokens or 0) + (
                response.usage.completion_tokens or 0
            )
        return response.choices[0].message if response.choices else None

    async def _use_tools(self, channel, message, turn, done, publisher=None) -> None:
        agent = self.agent
        calls = message.tool_calls
        # With thinking on, DeepSeek wants the reasoning back for the rest of this turn.
        reasoning = getattr(message, "reasoning_content", None)
        turn.append(
            {
                "role": "assistant",
                "content": message.content or "",
                **({"reasoning_content": reasoning} if reasoning else {}),
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in calls
                ],
            }
        )
        for call in calls:
            name, raw = call.function.name, call.function.arguments
            if publisher:
                publisher.event("tool-call", name=name, text=describe_call(name, loads(raw) or {}))
            output = await agent._run_tool(name, raw, AwayChat(), channel)
            if publisher:
                publisher.event("tool-result", name=name, text=monitor_text(name, output))
            turn.append({"role": "tool", "tool_call_id": call.id, "content": output})
            if not output.startswith("错误") and (line := what_she_did(name, loads(raw) or {})):
                done.append((name, line))
        if viewed := agent.viewed.pop(channel, None):
            names = "、".join(str(name) for name, _ in viewed)
            turn.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"[提示] 你打开的图片：{names}"},
                        *(file_block(copy) for _, copy in viewed),
                    ],
                }
            )


class AwayChat:
    """The DM while he is away: nothing reaches it except her final message."""

    # What needs his say-so (speaking in the group, her profile, more free time) waits.
    away = True

    async def send(self, text) -> None:
        pass

    async def send_file(self, path) -> None:
        raise ToolError("他现在不在，想给他看就等他来了再发")

    def typing(self):
        return nullcontext()


SELF_TAG = "[附注] 你主动"
