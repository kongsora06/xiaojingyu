import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from openai import AsyncOpenAI

from discord_dfy import group_chat
from discord_dfy.ai import BASE_URL
from discord_dfy.group_chat import (
    FAILURE_TEXT,
    ChatLine,
    Controls,
    GroupChat,
    Plan,
    PlanError,
    parse_plan,
    pick_channel,
)
from discord_dfy.monitor_protocol import Publisher

CHANNEL = 100
GUILD = 10
OWNER = 20
FRIEND = 30


def plan(messages=(), reply_to=None, reaction=None):
    return {"messages": list(messages), "reply_to": reply_to, "reaction": reaction}


def completion(body, *, usage=None):
    content = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage or {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
    }


class Provider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        if self.gate is not None and len(self.bodies) == 1:
            await self.gate.wait()
        response = self.responses.pop(0)
        if isinstance(response, int):
            return httpx2.Response(response, json={"error": {"message": "boom"}})
        return httpx2.Response(200, json=completion(response))

    def payload(self, index=-1):
        return json.loads(self.bodies[index]["messages"][1]["content"])


class Transport:
    def __init__(self, history=()):
        self.sent = []
        self.reactions = []
        self.typing_calls = 0
        self.history_lines = list(history)
        self.history_calls = 0
        self.next_id = 1000
        self.owner_messages = []
        self.files = []

    async def send(self, channel_id, text, reply_to):
        self.next_id += 1
        self.sent.append((channel_id, text, reply_to))
        return self.next_id

    async def react(self, channel_id, message_id, emoji):
        self.reactions.append((channel_id, message_id, emoji))

    def typing(self, channel_id):
        self.typing_calls += 1
        return nullcontext()

    async def history(self, channel_id, limit, after):
        self.history_calls += 1
        return self.history_lines

    def channels(self):
        return [{"id": CHANNEL, "channel": "闲聊", "guild_id": GUILD, "guild": "测试服"}]

    async def message_owner(self, text):
        self.owner_messages.append(text)
        return True

    async def send_file(self, channel_id, path, reply_to=None):
        self.next_id += 1
        self.files.append((channel_id, path.name, reply_to))
        return self.next_id


class Recorder:
    def __init__(self):
        self.decisions = []

    async def record_social_decision(self, **values):
        self.decisions.append(values)


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch):
    monkeypatch.setattr(group_chat, "MUST_DELAY", 0.01)
    monkeypatch.setattr(group_chat, "MUST_MAX_WAIT", 0.05)
    monkeypatch.setattr(group_chat, "CALLED_DELAY", 0.01)
    monkeypatch.setattr(group_chat, "CALLED_MAX_WAIT", 0.05)
    monkeypatch.setattr(group_chat, "FOLLOWUP_DELAY", 0.01)
    monkeypatch.setattr(group_chat, "FOLLOWUP_MAX_WAIT", 0.05)
    monkeypatch.setattr(group_chat, "AMBIENT_MAX_WAIT", 0.2)
    monkeypatch.setattr(group_chat, "LEVELS", {1: (0.05, 0.0), 2: (0.02, 0.0), 3: (0.01, 0.0)})


@pytest.fixture
def group_settings(settings, tmp_path):
    return replace(
        settings,
        owner_discord_user_id=OWNER,
        social_state_path=tmp_path / "group_state.json",
        social_level=2,
    )


@pytest.fixture
async def make_group(group_settings):
    created = []

    def build(provider, transport=None, **overrides):
        client = AsyncOpenAI(
            api_key="fake-test-key",
            base_url=BASE_URL,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(provider)),
        )
        group = GroupChat(
            replace(group_settings, **overrides),
            client,
            transport or Transport(),
            storage=Recorder(),
            pace=0,
        )
        created.append((group, client))
        return group

    yield build
    for group, client in created:
        await group.close()
        await client.close()


_ids = iter(range(1, 1_000_000))


def line(text, author=FRIEND, name="阿明", minutes_ago=0, **fields):
    return ChatLine(
        id=fields.pop("id", next(_ids)),
        author_id=author,
        author_name=name,
        content=text,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        **fields,
    )


async def settle(group, channel=CHANNEL):
    await asyncio.sleep(0.05)
    window = group.windows[channel]
    for _ in range(400):
        if not window.pending and not window.lock.locked():
            await asyncio.sleep(0.02)
            if not window.pending and not window.lock.locked():
                return
        await asyncio.sleep(0.01)
    raise AssertionError("group chat did not settle")


async def test_mention_must_be_answered_with_quote_reaction_and_pacing(make_group):
    first = line("今天吃啥", author=OWNER, name="老爹")
    second = line("@小鲸鱼 你说呢", minutes_ago=0)
    provider = Provider(
        plan(["火锅！", "不接受反驳"], reply_to=1, reaction={"n": 2, "emoji": "😂"})
    )
    transport = Transport()
    group = make_group(provider, transport)
    group.observe(CHANNEL, first, title=("测试服", "#闲聊"))
    group.observe(CHANNEL, second, mentioned=True)
    await settle(group)
    assert len(provider.bodies) == 1
    body = provider.bodies[0]
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"} and "reasoning_effort" not in body
    assert "测试服" in body["messages"][0]["content"] and "#闲聊" in body["messages"][0]["content"]
    payload = provider.payload()
    assert payload["trigger"] == {"kind": "must", "n": 2}
    assert [(m["author"], m["role"]) for m in payload["messages"]] == [
        ("老爹", "owner"),
        ("阿明", "member"),
    ]
    assert transport.sent == [
        (CHANNEL, "火锅！", first.id),
        (CHANNEL, "不接受反驳", None),
    ]
    assert transport.reactions == [(CHANNEL, second.id, "😂")]
    assert transport.typing_calls >= 2
    decision = group.storage.decisions[-1]
    assert decision["trigger"] == "must" and decision["outcome"] == "spoke"
    assert decision["sent"] == 2 and decision["reaction"] == "😂"
    assert decision["prompt_tokens"] == 50


async def test_bot_lines_appear_in_order_as_you(make_group):
    provider = Provider(plan(["在呢"]), plan(["嗯"]))
    group = make_group(provider)
    group.observe(CHANNEL, line("小鲸鱼在吗"))
    await settle(group)
    group.observe(CHANNEL, line("你刚才说啥"), replied_to_bot=True)
    await settle(group)
    messages = provider.payload()["messages"]
    assert [m["role"] for m in messages] == ["member", "you", "member"]
    assert messages[1]["text"] == "在呢" and messages[1]["author"] == "小鲸鱼"


async def test_name_mention_is_called_and_ambient_waits_for_level(make_group):
    provider = Provider(plan(), plan(["插一句"]))
    group = make_group(provider)
    group.observe(CHANNEL, line("deepseek 是不是又更新了"))
    await settle(group)
    assert provider.payload(0)["trigger"]["kind"] == "called"
    group.observe(CHANNEL, line("今天好热"))
    await settle(group)
    assert provider.payload(1)["trigger"]["kind"] == "ambient"
    assert provider.payload(1)["chattiness"] == 2
    assert group.storage.decisions[0]["outcome"] == "silent"
    assert group.transport.sent[-1][1] == "插一句"


async def test_level_zero_and_quiet_only_answer_when_called(make_group):
    provider = Provider(plan(["我在"]))
    group = make_group(provider, social_level=0)
    group.observe(CHANNEL, line("随便聊聊"))
    await settle(group)
    assert provider.bodies == []
    group.set_level(2)
    group.quiet(600)
    group.observe(CHANNEL, line("还是随便聊聊"))
    await settle(group)
    assert provider.bodies == []
    assert "安静模式" in group.status()
    group.observe(CHANNEL, line("@小鲸鱼"), mentioned=True)
    await settle(group)
    assert len(provider.bodies) == 1 and group.transport.sent[-1][1] == "我在"
    assert provider.payload()["chattiness"] == 0


async def test_conversation_continues_after_the_bot_speaks(make_group, monkeypatch):
    # Level 0 and a long ambient gap: only being in a conversation can explain a reply.
    monkeypatch.setattr(
        group_chat, "LEVELS", {1: (60.0, 600.0), 2: (60.0, 600.0), 3: (60.0, 600.0)}
    )
    provider = Provider(plan(["在呢"]), plan(["火锅吧"]), plan(["那就这么定了"]))
    group = make_group(provider, social_level=0)
    group.observe(CHANNEL, line("@小鲸鱼", author=OWNER, name="老爹"), mentioned=True)
    await settle(group)
    trigger = group.observe(CHANNEL, line("晚上吃啥好", author=OWNER, name="老爹"))
    assert trigger == "followup"
    await settle(group)
    assert provider.payload(1)["trigger"]["kind"] == "followup"
    assert group.transport.sent[-1][1] == "火锅吧"
    # Each reply keeps the conversation going; a friend joining in counts too.
    assert group.observe(CHANNEL, line("我也想吃火锅")) == "followup"
    await settle(group)
    assert [text for _, text, _ in group.transport.sent] == ["在呢", "火锅吧", "那就这么定了"]


async def test_blank_thinking_answer_is_retried_without_thinking_when_addressed(make_group):
    provider = Provider("   ", plan(["想好了"]), "  ")
    group = make_group(provider, social_thinking=True)
    group.observe(CHANNEL, line("@小鲸鱼 在吗"), mentioned=True)
    await settle(group)
    first, second = provider.bodies
    assert first["thinking"] == {"type": "enabled"} and first["reasoning_effort"] == "low"
    assert second["thinking"] == {"type": "disabled"} and "reasoning_effort" not in second
    assert group.transport.sent[-1][1] == "想好了"
    # Unaddressed chatter keeps a blank answer as silence: no second request.
    group.windows[CHANNEL].last_bot_at = float("-inf")
    group.observe(CHANNEL, line("随便说说"))
    await settle(group)
    assert len(provider.bodies) == 3 and group.storage.decisions[-1]["outcome"] == "silent"


async def test_broken_json_is_retried_only_when_addressed(make_group):
    broken = '{"messages": ["坏引号”，"后半"]}'
    provider = Provider(broken, plan(["修好了"]), broken)
    group = make_group(provider)
    group.observe(CHANNEL, line("@小鲸鱼 说点啥"), mentioned=True)
    await settle(group)
    assert len(provider.bodies) == 2 and group.transport.sent[-1][1] == "修好了"
    group.windows[CHANNEL].last_bot_at = float("-inf")
    group.observe(CHANNEL, line("随便说说"))
    await settle(group)
    assert len(provider.bodies) == 3 and group.storage.decisions[-1]["outcome"] == "invalid"


async def test_followup_window_expires_and_quiet_mode_ends_it(make_group):
    provider = Provider(plan(["在"]))
    group = make_group(provider, social_level=0)
    group.observe(CHANNEL, line("@小鲸鱼"), mentioned=True)
    await settle(group)
    window = group.windows[CHANNEL]
    window.last_bot_at = group.clock() - group_chat.ENGAGED_SECONDS - 1
    assert group.observe(CHANNEL, line("过了很久")) == "ambient"
    window.last_bot_at = group.clock()
    group.quiet(600)
    assert group.observe(CHANNEL, line("安静模式里")) == "ambient"
    await settle(group)
    assert len(provider.bodies) == 1


async def test_disabled_group_ignores_everything_but_keeps_context(make_group, group_settings):
    provider = Provider(plan(["回来了"]))
    group = make_group(provider)
    group.set_enabled(False)
    group.observe(CHANNEL, line("@小鲸鱼 在吗"), mentioned=True)
    await asyncio.sleep(0.1)
    assert provider.bodies == [] and "关着" in group.status()
    assert Controls.load(group_settings.social_state_path, 1).enabled is False
    group.set_enabled(True)
    group.observe(CHANNEL, line("@小鲸鱼 现在呢"), mentioned=True)
    await settle(group)
    assert len(provider.payload()["messages"]) == 2


async def test_controls_persist_across_restarts(make_group, group_settings):
    group = make_group(Provider())
    group.set_level(3)
    group.quiet(120)
    restored = Controls.load(group_settings.social_state_path, 1)
    assert restored.level == 3 and restored.quiet_until > 0
    group.quiet(0)
    assert Controls.load(group_settings.social_state_path, 1).quiet_until == 0
    (group_settings.social_state_path).write_text("not json", encoding="utf-8")
    assert Controls.load(group_settings.social_state_path, 2) == Controls(level=2)


async def test_ambient_plan_is_dropped_when_chat_moves_on(make_group):
    provider = Provider(plan(["过时的话"]), plan(["新的话"]))
    provider.gate = asyncio.Event()
    group = make_group(provider)
    group.observe(CHANNEL, line("有点无聊"))
    for _ in range(100):
        if provider.bodies:
            break
        await asyncio.sleep(0.01)
    group.observe(CHANNEL, line("我去打游戏了"))
    provider.gate.set()
    await settle(group)
    await settle(group)
    assert [text for _, text, _ in group.transport.sent] == ["新的话"]
    assert group.storage.decisions[0]["outcome"] == "stale"
    assert len(provider.payload()["messages"]) == 2


async def test_forced_reply_is_sent_even_if_chat_moves_on(make_group):
    provider = Provider(plan(["回答你"]), plan())
    provider.gate = asyncio.Event()
    group = make_group(provider)
    group.observe(CHANNEL, line("@小鲸鱼 几点了"), mentioned=True)
    for _ in range(100):
        if provider.bodies:
            break
        await asyncio.sleep(0.01)
    group.observe(CHANNEL, line("算了"))
    provider.gate.set()
    await settle(group)
    await settle(group)
    assert group.transport.sent[0][1] == "回答你"


async def test_empty_forced_plan_and_failures(make_group):
    # Each failed request is retried once by the SDK, so it consumes two responses.
    provider = Provider(plan(), 500, 500, 500, 500)
    group = make_group(provider)
    group.observe(CHANNEL, line("@小鲸鱼"), mentioned=True)
    await settle(group)
    assert group.transport.sent[-1][1] == "？"
    group.observe(CHANNEL, line("@小鲸鱼 还在吗"), mentioned=True)
    await settle(group)
    assert group.transport.sent[-1][1] == FAILURE_TEXT
    count = len(group.transport.sent)
    group.observe(CHANNEL, line("没人叫你"))
    await settle(group)
    assert len(group.transport.sent) == count
    assert [d["outcome"] for d in group.storage.decisions][-2:] == ["failed", "failed"]


async def test_hourly_budget_limits_ambient_more_than_mentions(make_group):
    provider = Provider(plan(), plan(["回"]), plan(["再回"]))
    group = make_group(provider, social_max_calls_per_hour=1)
    group.observe(CHANNEL, line("第一句"))
    await settle(group)
    group.observe(CHANNEL, line("第二句"))
    await settle(group)
    assert len(provider.bodies) == 1
    assert group.storage.decisions[-1]["outcome"] == "rate_limited"
    group.observe(CHANNEL, line("@小鲸鱼"), mentioned=True)
    await settle(group)
    assert len(provider.bodies) == 2
    group.observe(CHANNEL, line("@小鲸鱼 again"), mentioned=True)
    await settle(group)
    assert len(provider.bodies) == 2


async def test_history_backfill_runs_once_and_merges(make_group):
    old_self = line("我之前说的话", author=999, name="小鲸鱼", minutes_ago=30, is_self=True)
    old_friend = line("昨天那个梗", minutes_ago=40)
    current = line("@小鲸鱼 记得吗")
    transport = Transport(history=[old_friend, old_self, current])
    provider = Provider(plan(["记得"]), plan(["嗯"]))
    group = make_group(provider, transport)
    group.observe(CHANNEL, current, mentioned=True)
    await settle(group)
    texts = [m["text"] for m in provider.payload()["messages"]]
    assert texts == ["昨天那个梗", "我之前说的话", "@小鲸鱼 记得吗"]
    assert provider.payload()["messages"][1]["role"] == "you"
    group.observe(CHANNEL, line("@小鲸鱼 再说一遍"), mentioned=True)
    await settle(group)
    assert transport.history_calls == 1


async def test_old_messages_fall_out_of_the_window(make_group):
    provider = Provider(plan(["嗯"]))
    group = make_group(provider, social_history_minutes=10, social_history_messages=5)
    group.observe(CHANNEL, line("很久以前", minutes_ago=30))
    for index in range(6):
        group.windows[CHANNEL].lines.append(line(f"填充{index}"))
    group.observe(CHANNEL, line("@小鲸鱼"), mentioned=True)
    await settle(group)
    texts = [m["text"] for m in provider.payload()["messages"]]
    assert "很久以前" not in texts and len(texts) <= 5


async def test_payload_maps_replies_and_redacts_secrets(make_group):
    target = line("原话")
    reply = line("回复原话", reply_to=target.id)
    older = line("回复很早的消息", reply_to=1, reply_preview="阿红：很早以前的话")
    secret = line("@小鲸鱼 key 是 test-deepseek-key")
    provider = Provider(plan(["好"]))
    group = make_group(provider)
    for item in (target, reply, older):
        group.observe(CHANNEL, item)
    group.observe(CHANNEL, secret, mentioned=True)
    await settle(group)
    messages = provider.payload()["messages"]
    assert messages[1]["reply_to"] == 1
    assert messages[2]["replying_to_older"] == "阿红：很早以前的话"
    assert "test-deepseek-key" not in json.dumps(messages, ensure_ascii=False)


async def test_ask_uses_same_brain_and_counts_budget(make_group):
    provider = Provider(plan(["答案一", "答案二"]), plan())
    group = make_group(provider)
    answers = await group.ask(CHANNEL, line("/ask 什么是 uv"), title=("服", "#问"))
    assert answers == ["答案一", "答案二"]
    assert provider.payload()["trigger"]["kind"] == "must" and len(group.calls) == 1
    assert await group.ask(CHANNEL, line("/ask 空的")) == ["？"]


def lines_for_plan(count=3):
    return [line(f"消息{index}") for index in range(count)]


def test_parse_plan_validates_and_maps_numbers():
    lines = lines_for_plan()
    result = parse_plan(
        json.dumps(
            {
                "messages": ["一", " ", "二", "三", "四", "五", "六"],
                "reply_to": 2,
                "reaction": {"n": "3", "emoji": "👍"},
                "remember": ["阿明  不吃\n香菜", 7, "", "b", "c", "d"],
            }
        ),
        lines,
    )
    assert result.messages == ("一", "二", "三", "四", "五")
    assert result.remember == ("阿明 不吃 香菜", "b", "c")
    assert parse_plan('{"remember": "单条"}', lines).remember == ("单条",)
    assert result.reply_to == lines[1].id and result.reaction == (lines[2].id, "👍")
    assert parse_plan('{"messages": "单条"}', lines).messages == ("单条",)
    bad = parse_plan(
        json.dumps({"messages": [], "reply_to": 9, "reaction": {"n": 1, "emoji": "ok"}}), lines
    )
    assert bad.reply_to is None and bad.reaction is None
    assert parse_plan(json.dumps({"reply_to": True}), lines).reply_to is None
    assert parse_plan("   ", lines) == parse_plan("", lines) == Plan()
    for broken in ("nope", "[]", '{"messages": 5}'):
        with pytest.raises(PlanError):
            parse_plan(broken, lines)


def test_parse_plan_neutralizes_mentions_and_caps_length():
    result = parse_plan(
        json.dumps({"messages": ["@everyone 看 <@123> 和 <@&456> @here", "x" * 5000]}), []
    )
    first, second = result.messages
    assert "@everyone" not in first and "<@123>" not in first and "<@&456>" not in first
    assert "@here" not in first and len(second) == group_chat.MAX_MESSAGE_CHARS


def sse(*chunks):
    body = b"".join(
        b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n" for chunk in chunks
    )
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=body + b"data: [DONE]\n\n"
    )


def delta(**fields):
    return {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": fields, "finish_reason": None}],
    }


class Sink:
    def __init__(self):
        self.events = []

    def offer(self, event):
        self.events.append(event)


class Observer:
    def __init__(self):
        self.sink = Sink()

    def publisher(self, source, *, session_id=None, task_id=None):
        return Publisher(self.sink, source, session_id=session_id, task_id=task_id)


async def test_monitor_sees_group_thinking_and_decision(make_group):
    answer = json.dumps(plan(["来了"]), ensure_ascii=False)

    async def provider(request):
        assert json.loads(request.content)["stream"] is True
        return sse(
            delta(reasoning_content="有人叫我"),
            delta(content=answer[:10]),
            delta(content=answer[10:]),
        )

    group = make_group(provider)
    group.observer = Observer()
    group.observe(CHANNEL, line("@小鲸鱼"), mentioned=True)
    await settle(group)
    assert group.transport.sent[-1][1] == "来了"
    events = group.observer.sink.events
    assert all(e["source"] == "social" for e in events)
    thinking = "".join(e["data"].get("text", "") for e in events if e["type"] == "reasoning-delta")
    assert thinking == "有人叫我"
    decision = [e for e in events if e["type"] == "decision"][-1]
    assert decision["data"]["status"] == "spoke" and decision["data"]["text"] == "来了"


async def test_group_side_shares_her_one_memory_and_knows_the_dms(make_group, group_settings):
    group_settings.agent_memory_path.write_text(
        "# memory\n\n## 我们之间\n- 老爹说想换工作（10-22）\n\n## 做事备忘\n- 用 uv\n",
        encoding="utf-8",
    )
    provider = Provider(
        {**plan(["记住啦"]), "remember": ["阿明不吃香菜", "阿明 不吃香菜。"]},
        {**plan(["不放香菜"]), "remember": ["阿明不吃香菜"]},
    )
    group = make_group(provider)
    group.observe(CHANNEL, line("@小鲸鱼 我不吃香菜"), mentioned=True, guild_id=GUILD)
    await settle(group)
    system = provider.bodies[0]["messages"][0]["content"]
    assert "<memory>\n# memory\n\n## 我们之间\n- 老爹说想换工作" in system
    assert "用 uv" not in system  # Work notes stay on the DM side.
    assert "悄悄话" in system and "同一个你" in system
    assert "dm_recent" not in provider.payload(0)  # The DM text itself stays in the DMs.
    memory = group_settings.agent_memory_path.read_text(encoding="utf-8")
    assert memory.count("阿明不吃香菜") == 1
    assert "\n## 群里的人和事\n- 阿明不吃香菜（" in memory and memory.endswith(
        "## 做事备忘\n- 用 uv\n"
    )
    group.observe(CHANNEL, line("@小鲸鱼 点外卖"), mentioned=True, guild_id=GUILD)
    await settle(group)
    assert "- 阿明不吃香菜（" in provider.bodies[1]["messages"][0]["content"]
    assert group_settings.agent_memory_path.read_text(encoding="utf-8") == memory


async def test_long_memory_is_tidied_after_group_notes(make_group, group_settings, monkeypatch):
    monkeypatch.setattr(group_chat, "MEMORY_TIDY_CHARS", 10)
    group_settings.agent_memory_path.write_text(
        "# memory\n\n## 群里的人和事\n- 阿明不吃葱（10-22）\n- 阿明也不吃葱（10-22）\n"
        + "- 填充\n" * 10,
        encoding="utf-8",
    )
    provider = Provider(
        {**plan(["嗯"]), "remember": ["阿明不吃香菜"]},
        "# memory\n\n## 群里的人和事\n- 阿明不吃葱和香菜（10-22）\n- 填充",
    )
    group = make_group(provider)
    group.observe(CHANNEL, line("@小鲸鱼"), mentioned=True, guild_id=GUILD)
    await settle(group)
    await asyncio.wait_for(group.tidying, 5)
    assert provider.bodies[1]["messages"][0]["content"].startswith("下面是你（小鲸鱼）的记忆文件")
    tidied = group_settings.agent_memory_path.read_text(encoding="utf-8")
    assert tidied == "# memory\n\n## 群里的人和事\n- 阿明不吃葱和香菜（10-22）\n- 填充\n"


async def test_owner_post_from_dm_speaks_in_the_group_and_invites_followups(make_group):
    provider = Provider(plan(["那下周见"]))
    transport = Transport()
    group = make_group(provider, transport)
    assert await group.post(None, "老爹说他明天不来了 @everyone") == "已发到 测试服 #闲聊"
    assert transport.sent == [(CHANNEL, "老爹说他明天不来了 @​everyone", None)]
    window = group.windows[CHANNEL]
    assert window.guild_id == GUILD and window.lines[-1].is_self
    assert group.observe(CHANNEL, line("啊为啥")) == "followup"
    await settle(group)
    assert provider.payload()["messages"][0]["role"] == "you"
    with pytest.raises(ValueError):
        await group.post(None, "  ")


def test_pick_channel_by_name_id_or_server():
    channels = [
        {"id": 1, "channel": "闲聊", "guild_id": 9, "guild": "朋友群"},
        {"id": 2, "channel": "游戏", "guild_id": 9, "guild": "朋友群"},
    ]
    assert pick_channel(channels, "#游戏")["id"] == 2
    assert pick_channel(channels, "1")["id"] == 1
    for wanted in (None, "朋友群", "不存在"):
        with pytest.raises(ValueError, match="闲聊"):
            pick_channel(channels, wanted)
    assert pick_channel(channels[:1], None)["id"] == 1
    with pytest.raises(ValueError):
        pick_channel([], None)


async def test_recent_chat_for_the_dm_agent_includes_backfill_and_own_lines(make_group):
    transport = Transport(history=[line("刚才谁赢了", minutes_ago=3)])
    group = make_group(Provider(), transport)
    text = await group.recent("闲聊")
    assert text.startswith("测试服 #闲聊 最近的聊天：\n") and "阿明：刚才谁赢了" in text
    await group.post(None, "我赢了")
    assert (await group.recent(None)).endswith(" 你：我赢了")
    assert transport.history_calls == 1


async def test_private_word_to_owner_after_group_reply_with_rate_limit(make_group, monkeypatch):
    now = [1000.0]
    provider = Provider(
        {**plan(["才不是为你说的"]), "dm_owner": "其实刚才那句是真心的"},
        {**plan(["嗯"]), "dm_owner": "还有一件事"},
    )
    transport = Transport()
    group = make_group(provider, transport)
    group.clock = lambda: now[0]
    group.observe(
        CHANNEL, line("@小鲸鱼 你是不是在夸老爹"), mentioned=True, title=("测试服", "#闲聊")
    )
    await settle(group)
    assert provider.payload(0)["can_dm_owner"] is True
    assert transport.sent[-1][1] == "才不是为你说的"
    assert transport.owner_messages == ["-# 💬 从 测试服 #闲聊 过来私信你\n其实刚才那句是真心的"]
    # Too soon for another one: the model is told, and a DM it plans anyway is dropped.
    group.observe(CHANNEL, line("@小鲸鱼 还有呢"), mentioned=True)
    await settle(group)
    assert provider.payload(1)["can_dm_owner"] is False
    assert len(transport.owner_messages) == 1
    now[0] += group_chat.OWNER_DM_GAP
    assert group._can_dm_owner()
    group.owner_dms.extend([now[0]] * group_chat.OWNER_DM_PER_DAY)
    now[0] += group_chat.OWNER_DM_GAP
    assert not group._can_dm_owner()


async def test_no_owner_means_no_private_messages(make_group):
    group = make_group(Provider(), owner_discord_user_id=None)
    assert not group._can_dm_owner()


def test_parse_plan_reads_private_word():
    assert parse_plan('{"dm_owner": "  <@123> 偷偷说  "}', []).dm_owner == "<@​123> 偷偷说"
    assert parse_plan('{"dm_owner": 5}', []).dm_owner is None
    assert parse_plan('{"dm_owner": "  "}', []).dm_owner is None


async def test_recent_images_are_shown_to_the_model_up_to_a_cap(make_group):
    provider = Provider(plan(["好可爱"]))
    group = make_group(provider)
    for index in range(4):
        group.observe(
            CHANNEL,
            line(f"看图{index} [图片 {index}.png]", images=(f"data:image/png;base64,{index}",)),
        )
    group.observe(CHANNEL, line("@小鲸鱼 你看"), mentioned=True)
    await settle(group)
    content = provider.bodies[0]["messages"][1]["content"]
    payload = json.loads(content[0]["text"])
    assert [m.get("images") for m in payload["messages"]] == [1, 1, 1, 1, None]
    labels = [b["text"] for b in content[1:] if b["type"] == "text"]
    urls = [b["image_url"]["url"] for b in content if b["type"] == "image_url"]
    assert labels == ["第 2 条消息里的图片：", "第 3 条消息里的图片：", "第 4 条消息里的图片："]
    assert urls == [f"data:image/png;base64,{i}" for i in (1, 2, 3)]


class FakeWeb:
    def __init__(self):
        self.queries = []

    async def search(self, query, count):
        self.queries.append(query)
        return [{"title": "比分", "url": "https://s.example", "summary": "主队 2:1 获胜"}]


async def test_one_search_then_the_model_answers_with_results(make_group):
    provider = Provider({**plan(), "search": "昨晚 比赛 比分"}, plan(["2比1，主队赢了"]))
    group = make_group(provider)
    group.web = FakeWeb()
    group.observe(CHANNEL, line("@小鲸鱼 昨晚谁赢了"), mentioned=True)
    await settle(group)
    assert group.web.queries == ["昨晚 比赛 比分"]
    first, second = provider.payload(0), provider.payload(1)
    assert first["can_search"] is True and "search" not in first
    assert second["can_search"] is False
    assert second["search"]["query"] == "昨晚 比赛 比分" and "2:1" in second["search"]["results"]
    assert group.transport.sent[-1][1] == "2比1，主队赢了"


async def test_without_web_search_requests_are_ignored(make_group):
    provider = Provider({**plan(["我也不知道"]), "search": "x"})
    group = make_group(provider)
    group.observe(CHANNEL, line("@小鲸鱼 ?"), mentioned=True)
    await settle(group)
    assert provider.payload()["can_search"] is False and len(provider.bodies) == 1
    assert group.transport.sent[-1][1] == "我也不知道"


def test_group_rules_refuse_to_write_code():
    assert "不要给出代码" in group_chat.GROUP_RULES


def sticker_book(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (directory / "stickers.json").write_text(
        json.dumps(
            [
                {"name": "我不知道耶", "file": "a.png", "when": "装傻"},
                {"name": "丢了", "file": "missing.png", "when": "x"},
                {"name": "越狱", "file": "../../secret.png", "when": "x"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


async def test_sticker_is_sent_after_messages_and_seen_as_ours(make_group, group_settings):
    sticker_book(group_settings.social_sticker_dir)
    provider = Provider(
        {**plan(["写代码？"]), "sticker": "我不知道耶"},
        {**plan(), "sticker": "我不知道耶"},
        {**plan(), "sticker": "不存在"},
    )
    transport = Transport()
    group = make_group(provider, transport)
    group.observe(CHANNEL, line("@小鲸鱼 帮我写个排序"), mentioned=True)
    await settle(group)
    system = provider.bodies[0]["messages"][0]["content"]
    assert system.endswith("<stickers>\n- 我不知道耶：装傻\n</stickers>")
    assert transport.sent[-1][1] == "写代码？" and transport.files == [(CHANNEL, "a.png", None)]
    # A sticker alone answers a mention; it shows up in the record as ours.
    group.observe(CHANNEL, line("@小鲸鱼 ?"), mentioned=True)
    await settle(group)
    # The same sticker twice in a row is dropped; a mention still gets an answer.
    assert len(transport.files) == 1 and transport.sent[-1][1] == "？"
    you = [m for m in provider.payload(1)["messages"] if m["role"] == "you"]
    assert you[-1]["text"] == "[表情包：我不知道耶]"
    # Unknown sticker: a mention still gets an answer.
    group.observe(CHANNEL, line("@小鲸鱼 ??"), mentioned=True)
    await settle(group)
    assert len(transport.files) == 1 and transport.sent[-1][1] == "？"


async def test_no_sticker_library_means_no_sticker_rules(make_group):
    provider = Provider(plan(["嗯"]))
    group = make_group(provider)
    group.observe(CHANNEL, line("@小鲸鱼"), mentioned=True)
    await settle(group)
    assert "\n</stickers>" not in provider.bodies[0]["messages"][0]["content"]


class FakeProfile:
    def __init__(self):
        self.calls = []
        self.changed_at = float("-inf")
        self.now = 1000.0

    def clock(self):
        return self.now

    def describe(self):
        return "「摸鱼中」 · 在线"

    async def set_status(self, text, state=None):
        self.calls.append((text, state))
        self.changed_at = self.now


async def test_group_side_can_change_her_status_now_and_then(make_group):
    provider = Provider(
        {**plan(["困了"]), "status": {"text": "服务器繁忙", "state": "idle"}},
        {**plan(["还困"]), "status": {"text": "真的睡了", "state": "dnd"}},
    )
    group = make_group(provider)
    group.profile = FakeProfile()
    group.observe(CHANNEL, line("@小鲸鱼 困不困"), mentioned=True)
    await settle(group)
    first = provider.payload(0)
    assert first["your_status"] == "「摸鱼中」 · 在线" and first["can_set_status"] is True
    assert group.profile.calls == [("服务器繁忙", "idle")]
    group.observe(CHANNEL, line("@小鲸鱼 还困吗"), mentioned=True)
    await settle(group)
    assert provider.payload(1)["can_set_status"] is False
    assert group.profile.calls == [("服务器繁忙", "idle")]  # Too soon to change again.


def test_parse_plan_reads_status():
    assert parse_plan('{"status": {"text": " 摸鱼 ", "state": "idle"}}', []).status == (
        "摸鱼",
        "idle",
    )
    assert parse_plan('{"status": {"text": "x", "state": "invisible"}}', []).status == ("x", None)
    assert parse_plan('{"status": "摸鱼"}', []).status is None


async def test_no_dm_to_owner_while_one_is_unanswered(make_group):
    provider = Provider({**plan(["嗯"]), "dm_owner": "又有事"})
    transport = Transport()
    group = make_group(provider, transport)
    group.owner_waiting = lambda: True
    group.observe(CHANNEL, line("@小鲸鱼"), mentioned=True)
    await settle(group)
    assert provider.payload()["can_dm_owner"] is False and transport.owner_messages == []


async def test_follow_ups_stop_after_she_chimed_in_twice_uncalled(make_group):
    provider = Provider(plan(["嗯"]), plan(["对"]), plan(["哈"]), plan(["也热"]))
    group = make_group(provider)
    group.observe(CHANNEL, line("@小鲸鱼 在吗"), mentioned=True)
    await settle(group)
    window = group.windows[CHANNEL]
    assert group.observe(CHANNEL, line("今天好热")) == "followup"
    await settle(group)
    assert group.observe(CHANNEL, line("是啊")) == "followup"
    await settle(group)
    assert window.unprompted == 2
    # Twice uncalled in a row: plain messages wait for the normal pace again.
    assert group.trigger_for(window, line("热死了"), False) == "ambient"
    assert group.trigger_for(window, line("小鲸鱼你热吗"), False) == "called"
    group.observe(CHANNEL, line("@小鲸鱼 你呢"), mentioned=True)
    await settle(group)
    assert window.unprompted == 0 and group.trigger_for(window, line("哦"), False) == "followup"


async def test_with_thinking_the_plan_is_plain_json_and_may_be_fenced(make_group):
    fenced = "```json\n" + json.dumps(plan(["好啊"]), ensure_ascii=False) + "\n```"
    provider = Provider(fenced)
    transport = Transport()
    group = make_group(provider, transport, social_thinking=True)
    group.observe(CHANNEL, line("@小鲸鱼 吃火锅不"), mentioned=True)
    await settle(group)
    body = provider.bodies[0]
    assert body["thinking"] == {"type": "enabled"} and "response_format" not in body
    assert [text for _, text, _ in transport.sent] == ["好啊"]
    assert parse_plan("好的：" + json.dumps(plan(["嗯"])) + " 就这样", []).messages == ("嗯",)


async def test_she_waits_for_someone_who_is_typing(make_group):
    group = make_group(Provider())
    clock = [100.0]
    group.clock = lambda: clock[0]
    window = group._window(CHANNEL)
    window.pending = [(100.0, "followup", 1)]
    assert group._delay(window) == pytest.approx(group_chat.FOLLOWUP_DELAY)
    window.typing_at = 99.0  # Typing that ended in the message itself does not count.
    assert group._delay(window) == pytest.approx(group_chat.FOLLOWUP_DELAY)
    clock[0] = 100.001
    group.note_typing(CHANNEL)  # Someone is writing more.
    assert group._delay(window) == pytest.approx(group_chat.TYPING_SECONDS)
    clock[0] = 100.001 + group_chat.TYPING_SECONDS
    assert group._delay(window) == 0.0  # They stopped (no new typing event).
    window.pending = [(100.0, "must", 2)]
    group.note_typing(CHANNEL)
    clock[0] += 1
    must_cap = group_chat.TYPING_MAX_WAIT["must"]
    assert group._delay(window) == pytest.approx(100.0 + must_cap - clock[0])
