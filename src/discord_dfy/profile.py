"""Her Discord presence and profile: status any time, the rest when the owner agrees.

Status (a custom status line plus online / idle / do-not-disturb) she may change on her
own, from DMs or from the group. Avatar, banner, username, "About Me" and server
nickname change only through the DM tool, and the DM rules say to ask the owner first.
"""

import json
import logging
import time
from pathlib import Path

import discord

from .images import MAX_IMAGE_BYTES, image_type

log = logging.getLogger(__name__)
STATES = {"online": discord.Status.online, "idle": discord.Status.idle, "dnd": discord.Status.dnd}
STATE_NAMES = {"online": "在线", "idle": "离开", "dnd": "请勿打扰"}
MAX_STATUS_CHARS = 128
FIELDS = ("avatar", "banner", "username", "about", "nickname")


class ProfileError(Exception):
    """A change that could not be made, worded for the model."""


class Profile:
    def __init__(self, bot, state_path: Path, clock=time.monotonic):
        self.bot = bot
        self.state_path = state_path
        self.clock = clock
        self.text, self.state = self._load()
        self.changed_at = float("-inf")

    def _load(self) -> tuple[str, str]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            state = data.get("state") if data.get("state") in STATES else "online"
            return str(data.get("text") or "")[:MAX_STATUS_CHARS], state
        except (OSError, ValueError, AttributeError):
            return "", "online"

    def describe(self) -> str:
        line = f"「{self.text}」" if self.text else "（没有状态文字）"
        return f"{line} · {STATE_NAMES[self.state]}"

    async def restore(self) -> None:
        """Presence does not survive a reconnect; put hers back."""
        await self._apply()

    async def set_status(self, text: str | None, state: str | None = None) -> str:
        if state is not None and state not in STATES:
            raise ProfileError("state 只能是 online、idle 或 dnd")
        self.text = " ".join(str(text or "").split())[:MAX_STATUS_CHARS]
        self.state = state or self.state
        await self._apply()
        self.changed_at = self.clock()
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"text": self.text, "state": self.state}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            log.warning("Could not save presence")
        return f"状态已经改成 {self.describe()}"

    async def _apply(self) -> None:
        activity = discord.CustomActivity(name=self.text) if self.text else None
        await self.bot.change_presence(status=STATES[self.state], activity=activity)

    async def edit(self, field: str, value: str | None) -> str:
        bot = self.bot
        value = (value or "").strip()
        try:
            if field in {"avatar", "banner"}:
                data = read_image(value)
                await bot.user.edit(**{field: data})
                return "头像换好了" if field == "avatar" else "横幅换好了"
            if field == "username":
                if not 2 <= len(value) <= 32:
                    raise ProfileError("名字要 2～32 个字符")
                await bot.user.edit(username=value)
                return f"名字改成了 {value}"
            if field == "about":
                if len(value) > 400:
                    raise ProfileError("简介最多 400 字")
                app = await bot.application_info()
                await app.edit(description=value or None)
                return "简介（About Me）改好了"
            if field == "nickname":
                if len(value) > 32:
                    raise ProfileError("群昵称最多 32 个字符")
                done = []
                for guild in self._guilds():
                    await guild.me.edit(nick=value or None)
                    done.append(guild.name)
                if not done:
                    raise ProfileError("没有找到可以改昵称的服务器")
                return f"在 {'、'.join(done)} 的昵称改成了 {value or '（默认名字）'}"
        except discord.HTTPException as error:
            reason = {
                429: "Discord 说改得太频繁了，过一阵再试（名字一小时最多改两次）",
                403: "没有权限（群昵称需要服务器给 Change Nickname 权限）",
            }.get(error.status, f"Discord 拒绝了（HTTP {error.status}）")
            raise ProfileError(reason) from None
        raise ProfileError(f"不能改 {field}；可以改的有 status、{'、'.join(FIELDS)}")

    def _guilds(self):
        seen = {}
        for channel_id in self.bot.settings.allowed_channel_ids:
            channel = self.bot.get_channel(channel_id)
            guild = getattr(channel, "guild", None)
            if guild is not None and guild.me is not None:
                seen[guild.id] = guild
        return list(seen.values())


def read_image(path: str) -> bytes:
    file = Path(path)
    if not path or not file.is_file():
        raise ProfileError(f"图片文件不存在：{path}")
    data = file.read_bytes()
    if len(data) > MAX_IMAGE_BYTES or image_type(data) is None:
        raise ProfileError("要 8 MB 以内的 PNG、JPEG、GIF 或 WebP 图片")
    return data
