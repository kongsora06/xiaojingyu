from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_dfy.profile import Profile, ProfileError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def fake_bot(tmp_path):
    guild = SimpleNamespace(id=10, name="朋友群", me=SimpleNamespace(edit=AsyncMock()))
    app = SimpleNamespace(edit=AsyncMock())
    return SimpleNamespace(
        change_presence=AsyncMock(),
        user=SimpleNamespace(edit=AsyncMock()),
        application_info=AsyncMock(return_value=app),
        settings=SimpleNamespace(allowed_channel_ids=frozenset({100, 101})),
        get_channel=lambda channel_id: SimpleNamespace(guild=guild),
        guild=guild,
        app=app,
    )


async def test_status_is_applied_saved_and_restored(tmp_path):
    bot = fake_bot(tmp_path)
    path = tmp_path / "presence.json"
    profile = Profile(bot, path, clock=Clock())
    assert profile.describe() == "（没有状态文字） · 在线"
    assert (
        await profile.set_status("  想吃   白米饭 ", "idle")
        == "状态已经改成 「想吃 白米饭」 · 离开"
    )
    kwargs = bot.change_presence.await_args.kwargs
    assert kwargs["status"] is discord.Status.idle
    assert isinstance(kwargs["activity"], discord.CustomActivity)
    assert kwargs["activity"].name == "想吃 白米饭"
    again = Profile(bot, path)
    assert again.text == "想吃 白米饭" and again.state == "idle"
    await again.restore()
    assert bot.change_presence.await_count == 2
    await again.set_status("", None)  # Clears the text, keeps the state.
    assert bot.change_presence.await_args.kwargs == {
        "status": discord.Status.idle,
        "activity": None,
    }
    with pytest.raises(ProfileError):
        await again.set_status("x", "invisible")


async def test_profile_fields(tmp_path):
    bot = fake_bot(tmp_path)
    profile = Profile(bot, tmp_path / "presence.json")
    image = tmp_path / "a.png"
    image.write_bytes(PNG)
    assert await profile.edit("avatar", str(image)) == "头像换好了"
    assert bot.user.edit.await_args.kwargs == {"avatar": PNG}
    with pytest.raises(ProfileError, match="不存在"):
        await profile.edit("banner", str(tmp_path / "missing.png"))
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(ProfileError, match="PNG"):
        await profile.edit("avatar", str(tmp_path / "a.txt"))
    assert await profile.edit("username", "小鲸鱼") == "名字改成了 小鲸鱼"
    with pytest.raises(ProfileError, match="2～32"):
        await profile.edit("username", "x")
    assert await profile.edit("about", "爱吃白米饭的鲸鱼") == "简介（About Me）改好了"
    bot.app.edit.assert_awaited_with(description="爱吃白米饭的鲸鱼")
    assert await profile.edit("nickname", "鲸鱼") == "在 朋友群 的昵称改成了 鲸鱼"
    bot.guild.me.edit.assert_awaited_once_with(nick="鲸鱼")
    with pytest.raises(ProfileError, match="不能改"):
        await profile.edit("roles", "admin")


async def test_discord_refusals_are_explained(tmp_path):
    bot = fake_bot(tmp_path)
    response = MagicMock(status=429, reason="Too Many Requests")
    bot.user.edit.side_effect = discord.HTTPException(response, "slow down")
    with pytest.raises(ProfileError, match="太频繁"):
        await Profile(bot, tmp_path / "p.json").edit("username", "新名字")
