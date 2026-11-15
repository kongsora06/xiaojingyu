import asyncio
import io
import json
from dataclasses import replace

import httpx2
import pytest
from openai import AsyncOpenAI
from PIL import Image

from discord_dfy.ai import BASE_URL
from discord_dfy.screen import Frame, difference, encode, primary_monitor
from discord_dfy.stream_watch import StreamWatch

CHANNEL = 100
JPEG = b"\xff\xd8\xff" + b"frame"


def completion(text):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 900, "completion_tokens": 12, "total_tokens": 912},
    }


class Provider:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.bodies = []

    async def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=completion(self.texts.pop(0)))


class Recorder:
    def __init__(self):
        self.decisions = []

    async def record_social_decision(self, **values):
        self.decisions.append(values)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def frame(level: int) -> Frame:
    return Frame(JPEG + bytes([level]), bytes([level]) * 16)


@pytest.fixture
async def make_watch(settings):
    clients = []

    def build(provider, frames, **overrides):
        client = AsyncOpenAI(
            api_key="fake-test-key",
            base_url=BASE_URL,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(provider)),
        )
        clients.append(client)
        shots = list(frames)
        grabs = []

        def grab():
            grabs.append(1)
            return shots.pop(0) if len(shots) > 1 else shots[0]

        watch = StreamWatch(
            replace(settings, watch_stream=True, **overrides),
            client,
            storage=Recorder(),
            grab=grab,
            clock=Clock(),
        )
        watch.grabs = grabs
        return watch

    yield build
    for client in clients:
        await client.close()


def test_primary_monitor_is_the_marked_one_or_the_one_at_the_origin():
    everything = {"left": -1920, "top": 0, "width": 4480, "height": 1440}
    side = {"left": -1920, "top": 235, "width": 1920, "height": 1200}
    main = {"left": 0, "top": 0, "width": 2560, "height": 1440}
    assert primary_monitor([everything, side, main]) is main
    marked = {**side, "is_primary": True}
    assert primary_monitor([everything, main, marked]) is marked


def test_encode_shrinks_to_a_small_jpeg_and_notices_change():
    dark = encode(Image.new("RGB", (2560, 1440), (10, 10, 10)))
    bright = encode(Image.new("RGB", (2560, 1440), (240, 240, 240)))
    assert Image.open(io.BytesIO(dark.jpeg)).size == (1280, 720)
    assert dark.jpeg.startswith(b"\xff\xd8\xff")
    assert difference(dark.thumb, dark.thumb) == 0
    assert difference(dark.thumb, bright.thumb) > 0.8
    assert difference(b"", dark.thumb) == 1.0


async def test_nothing_is_captured_unless_he_is_live(make_watch):
    watch = make_watch(Provider(), [frame(0)])
    assert await watch.capture() is None
    assert await watch.frame_url() is None
    assert watch.grabs == []


async def test_a_changed_picture_gets_one_line_and_same_ones_are_skipped(make_watch):
    provider = Provider("在打 Boss，血还剩一半", "同上", "Boss 打死了，在开宝箱")
    watch = make_watch(provider, [frame(0), frame(0), frame(100), frame(200)])
    watch.channel_id = CHANNEL
    await watch.step(CHANNEL)  # First picture: described.
    await watch.step(CHANNEL)  # Same picture: no call.
    await watch.step(CHANNEL)  # Changed, but the model says nothing new.
    await watch.step(CHANNEL)  # Changed again: a new line.
    assert len(provider.bodies) == 3
    body = provider.bodies[0]
    assert body["thinking"] == {"type": "disabled"}
    content = body["messages"][1]["content"]
    assert content[0]["text"] == "这是开播后的第一张。"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert (
        provider.bodies[2]["messages"][1]["content"][0]["text"] == "上一句：在打 Boss，血还剩一半"
    )
    assert [line.split(" ", 1)[1] for line in watch.seen()] == [
        "在打 Boss，血还剩一半",
        "Boss 打死了，在开宝箱",
    ]
    outcomes = [(d["trigger"], d["outcome"], d["prompt_tokens"]) for d in watch.storage.decisions]
    assert outcomes == [("watch", "noted", 900), ("watch", "same", 900), ("watch", "noted", 900)]


async def test_she_takes_her_own_look_only_after_something_new_and_a_while(make_watch):
    provider = Provider("在看一个视频", "换了一个视频")
    watch = make_watch(provider, [frame(0), frame(0), frame(150)], watch_comment_minutes=5)
    looks = []
    watch.on_look = looks.append
    watch.channel_id = CHANNEL
    watch.looked_at = watch.clock()
    await watch.step(CHANNEL)
    assert looks == []  # Something new, but she only just started watching.
    watch.clock.now += 301
    await watch.step(CHANNEL)
    assert looks == [CHANNEL]
    watch.clock.now += 301
    await watch.step(CHANNEL)  # A new line, and enough time: another look.
    assert looks == [CHANNEL, CHANNEL]
    watch.clock.now += 301
    await watch.step(CHANNEL)  # Nothing new since: no look.
    assert looks == [CHANNEL, CHANNEL]


async def test_a_fresh_frame_is_taken_when_asked_and_everything_goes_when_he_stops(make_watch):
    watch = make_watch(Provider("在打游戏"), [frame(0), frame(50)])
    watch.channel_id = CHANNEL
    await watch.step(CHANNEL)
    assert len(watch.grabs) == 1
    assert await watch.frame_url() is not None
    assert len(watch.grabs) == 1  # The frame from a moment ago is fresh enough.
    watch.clock.now += 5
    await watch.frame_url()
    assert len(watch.grabs) == 2  # Older than that: a new capture.
    watch.stop()
    assert watch.seen() == [] and watch.frame is None
    assert await watch.frame_url() is None
    assert not watch.live_in(CHANNEL)


async def test_start_watches_until_stopped(make_watch):
    watch = make_watch(Provider("在打游戏"), [frame(0)], watch_seconds=10)
    pauses = asyncio.Event()

    async def sleep(seconds):
        pauses.set()
        await asyncio.Event().wait()

    watch.sleep = sleep
    watch.start(CHANNEL)
    await asyncio.wait_for(pauses.wait(), 1)
    assert watch.live_in(CHANNEL) and len(watch.seen()) == 1
    task = watch.task
    await watch.close()
    assert task.cancelled() and watch.channel_id is None


async def test_no_watching_while_group_chat_is_off(make_watch):
    provider = Provider("在打游戏")
    watch = make_watch(provider, [frame(0)])
    watch.channel_id = CHANNEL
    enabled = False
    watch.active = lambda: enabled
    await watch.step(CHANNEL)
    assert watch.grabs == [] and provider.bodies == []
    enabled = True
    await watch.step(CHANNEL)
    assert len(watch.grabs) == 1 and len(provider.bodies) == 1


def test_error_kind_names_the_cause_but_never_the_message():
    from discord_dfy.ai import error_kind

    try:
        try:
            raise ConnectionResetError("secret detail")
        except ConnectionResetError as cause:
            raise RuntimeError("also secret") from cause
    except RuntimeError as error:
        assert error_kind(error) == "RuntimeError(ConnectionResetError)"
    assert error_kind(ValueError("x")) == "ValueError"
