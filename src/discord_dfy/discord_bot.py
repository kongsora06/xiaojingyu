"""Discord transport only: translate events, acknowledge commands, and deliver messages."""

import json
import logging
import os
import re
import sys
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime

import discord
from discord import app_commands

from .config import Settings
from .group_chat import ChatLine, GroupChat
from .host_agent import HostAgent
from .images import data_url, image_type, is_image_attachment, read_images
from .models import IncomingMessage
from .profile import Profile
from .service import AgentService

log = logging.getLogger(__name__)
DENIED = "This bot is available only in approved server test channels."
NO_MENTIONS = discord.AllowedMentions.none()


def split_reply(text: str, limit: int = 1900) -> list[str]:
    """Split by UTF-16 units as a conservative Discord message-length budget."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if size + width > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += width
    if current:
        chunks.append("".join(current))
    return chunks


def display_name(user) -> str:
    return getattr(user, "display_name", None) or getattr(user, "name", None) or "某人"


def chat_line(message: discord.Message, bot_id: int | None) -> ChatLine:
    """What the group brain sees of a message: names instead of raw IDs, notes for media."""
    text = message.content or ""
    for user in getattr(message, "mentions", None) or []:
        text = re.sub(rf"<@!?{user.id}>", "@" + display_name(user), text)
    for attachment in getattr(message, "attachments", None) or []:
        kind = (getattr(attachment, "content_type", None) or "").split("/")[0]
        text += f" [{'图片' if kind == 'image' else '文件'} {attachment.filename}]"
    for sticker in getattr(message, "stickers", None) or []:
        text += f" [贴纸 {sticker.name}]"
    reference = getattr(message, "reference", None)
    resolved = getattr(reference, "resolved", None)
    preview = None
    if isinstance(resolved, discord.Message):
        preview = f"{display_name(resolved.author)}：{(resolved.content or '')[:80]}"
    return ChatLine(
        id=message.id,
        author_id=message.author.id,
        author_name=display_name(message.author),
        content=text.strip() or "[空消息]",
        created_at=message.created_at,
        reply_to=getattr(reference, "message_id", None),
        reply_preview=preview,
        is_self=bot_id is not None and message.author.id == bot_id,
    )


async def read_line(message: discord.Message, bot_id: int | None) -> ChatLine:
    """chat_line plus the message's images, downloaded so the model can look at them."""
    line = chat_line(message, bot_id)
    if line.is_self:
        return line
    urls = tuple(url for _, data in await read_images(message) if (url := data_url(data)))
    return replace(line, images=urls) if urls else line


class DMChat:
    """What the host agent may do in the owner's DM: talk, attach files, show typing."""

    def __init__(self, channel: discord.abc.Messageable) -> None:
        self.channel = channel

    async def send(self, text: str) -> None:
        for chunk in split_reply(text):
            await self.channel.send(chunk, allowed_mentions=NO_MENTIONS, suppress_embeds=True)

    async def send_file(self, path) -> None:
        await self.channel.send(file=discord.File(path), allowed_mentions=NO_MENTIONS)

    def typing(self):
        return self.channel.typing()


class GroupTransport:
    """The group brain's only way to act: approved channels, no pings, no other targets."""

    def __init__(self, bot: "DiscordAgent") -> None:
        self.bot = bot

    def _channel(self, channel_id: int):
        if channel_id not in self.bot.settings.allowed_channel_ids:
            return None
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            return None
        return channel

    def _permissions(self, channel):
        me = channel.guild.me
        return channel.permissions_for(me) if me is not None else None

    async def send(self, channel_id: int, text: str, reply_to: int | None) -> int | None:
        channel = self._channel(channel_id)
        permissions = self._permissions(channel) if channel is not None else None
        if permissions is None or not (
            permissions.send_messages_in_threads
            if isinstance(channel, discord.Thread)
            else permissions.send_messages
        ):
            raise PermissionError("cannot send here")
        reference = None
        if reply_to is not None and permissions.read_message_history:
            reference = channel.get_partial_message(reply_to)
        try:
            sent = await channel.send(
                text, reference=reference, allowed_mentions=NO_MENTIONS, suppress_embeds=True
            )
        except discord.HTTPException:
            if reference is None:
                raise
            # The quoted message may be gone; say it without the quote.
            sent = await channel.send(text, allowed_mentions=NO_MENTIONS, suppress_embeds=True)
        identifier = getattr(sent, "id", None)
        return identifier if type(identifier) is int else None

    async def send_file(self, channel_id: int, path, reply_to: int | None = None) -> int | None:
        channel = self._channel(channel_id)
        permissions = self._permissions(channel) if channel is not None else None
        if permissions is None or not permissions.attach_files:
            raise PermissionError("cannot attach files here")
        reference = None
        if reply_to is not None and permissions.read_message_history:
            reference = channel.get_partial_message(reply_to)
        sent = await channel.send(
            file=discord.File(path), reference=reference, allowed_mentions=NO_MENTIONS
        )
        identifier = getattr(sent, "id", None)
        return identifier if type(identifier) is int else None

    async def react(self, channel_id: int, message_id: int, emoji: str) -> None:
        channel = self._channel(channel_id)
        permissions = self._permissions(channel) if channel is not None else None
        if permissions is None or not (
            permissions.add_reactions and permissions.read_message_history
        ):
            raise PermissionError("cannot react here")
        await channel.get_partial_message(message_id).add_reaction(emoji)

    def typing(self, channel_id: int):
        channel = self._channel(channel_id)
        return channel.typing() if channel is not None else nullcontext()

    async def message_owner(self, text: str) -> bool:
        return await self.bot.message_owner(text)

    def channels(self) -> list[dict]:
        """Approved channels the bot can currently see, for posts the owner asks for."""
        found = []
        for channel_id in sorted(self.bot.settings.allowed_channel_ids):
            channel = self._channel(channel_id)
            if channel is not None:
                found.append(
                    {
                        "id": channel.id,
                        "channel": channel.name,
                        "guild_id": channel.guild.id,
                        "guild": channel.guild.name,
                    }
                )
        return found

    async def history(self, channel_id: int, limit: int, after: datetime) -> list[ChatLine]:
        channel = self._channel(channel_id)
        permissions = self._permissions(channel) if channel is not None else None
        if permissions is None or not permissions.read_message_history:
            return []
        bot_id = self.bot.user.id if self.bot.user is not None else None
        lines = []
        async for message in channel.history(limit=limit, after=after, oldest_first=False):
            # Other bots and webhooks stay out of the conversation, as they do live.
            if (message.author.bot and message.author.id != bot_id) or message.webhook_id:
                continue
            lines.append(await read_line(message, bot_id))
        return lines[::-1]


class SafeCommandTree(app_commands.CommandTree):
    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        # Do not invoke the library's default traceback logger for user-provided data.
        log.error("Slash command failed; error_type=%s", type(error).__name__)
        await self.client.service.record_event("internal_error")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "The command failed. Please try again.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "The command failed. Please try again.", ephemeral=True
                )
        except discord.HTTPException:
            log.warning("Could not deliver slash-command error")


class DiscordAgent(discord.Client):
    def __init__(self, settings: Settings, service: AgentService) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = settings.personal_mode_enabled
        # "Someone is typing", so she waits for what they are writing instead of cutting in.
        intents.guild_typing = True
        intents.dm_typing = settings.personal_mode_enabled
        # The group brain reads the conversation, not only messages that mention it.
        intents.message_content = True
        super().__init__(
            intents=intents,
            allowed_mentions=NO_MENTIONS,
            member_cache_flags=discord.MemberCacheFlags.none(),
            max_messages=None,
        )
        self.settings = settings
        self.service = service
        self.group: GroupChat | None = None
        self.personal: HostAgent | None = None
        self.restart_requested = False
        self.transport = GroupTransport(self)
        self.profile = Profile(self, settings.personal_database_path.parent / "presence.json")
        self.tree = SafeCommandTree(self)
        self.tree.add_command(
            app_commands.Command(
                name="ping", description="Check whether the bot is online.", callback=self.ping
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="ask", description="Ask DeepSeek a question.", callback=self.ask
            )
        )

    async def setup_hook(self) -> None:
        # setup_hook runs once per login, rather than on every gateway reconnect.
        if self.settings.test_guild_id is not None:
            guild = discord.Object(id=self.settings.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        log.info("Slash commands synchronized")

    async def close(self) -> None:
        if self.personal is not None:
            await self.personal.close()
        if self.group is not None:
            await self.group.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info(
            "Bot ready; approved_channels=%d; store_messages=%s; group=%s",
            len(self.settings.allowed_channel_ids),
            self.settings.store_messages,
            self.group.status() if self.group is not None else "off",
        )
        await self.service.record_event("ready")
        if self.profile is not None:
            try:
                await self.profile.restore()
            except Exception:
                log.warning("Could not restore presence")
        await self._announce_restart()

    async def message_owner(self, text: str, source: str = "group") -> bool:
        """A private word with the owner (from the group, or reaching out); also kept for
        the DM conversation."""
        owner = self.settings.owner_discord_user_id
        if owner is None or self.user is None:
            return False
        user = self.get_user(owner) or await self.fetch_user(owner)
        channel = user.dm_channel or await user.create_dm()
        if self.personal is not None:
            # Like her replies: one message per paragraph, sticker lines as stickers.
            await self.personal.deliver(DMChat(channel), text)
            await self.personal.note_outgoing(channel.id, text, source)
        else:
            await DMChat(channel).send(text)
        return True

    @property
    def restart_marker(self):
        return self.settings.personal_database_path.parent / "restart.json"

    async def request_restart(self, channel_id: int) -> None:
        """Shut down cleanly; the supervisor sees the exit code and starts a new process."""
        try:
            self.restart_marker.parent.mkdir(parents=True, exist_ok=True)
            self.restart_marker.write_text(json.dumps({"channel_id": channel_id}))
        except OSError:
            log.warning("Could not record restart marker")
        self.restart_requested = True
        await self.close()

    async def _announce_restart(self) -> None:
        try:
            channel_id = json.loads(self.restart_marker.read_text())["channel_id"]
            os.remove(self.restart_marker)
        except (OSError, ValueError, KeyError, TypeError):
            return
        try:
            await self.get_partial_messageable(int(channel_id)).send(
                "重启好了，新代码已经生效", allowed_mentions=NO_MENTIONS
            )
        except (discord.HTTPException, ValueError):
            log.warning("Could not announce restart")

    async def on_error(self, event_method: str, *args: object, **kwargs: object) -> None:
        # Never stringify event args or the exception: either may contain message content.
        error = sys.exception()
        log.error(
            "Discord event failed; event=%s; error_type=%s", event_method, type(error).__name__
        )
        await self.service.record_event("internal_error")

    async def _allow_interaction(self, interaction: discord.Interaction) -> bool:
        allowed = self.service.policy.allows(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            is_bot=interaction.user.bot,
        )
        if not allowed:
            await interaction.response.send_message(DENIED, ephemeral=True)
        return allowed

    @app_commands.guild_only()
    async def ping(self, interaction: discord.Interaction) -> None:
        if await self._allow_interaction(interaction):
            await interaction.response.send_message("Pong!", ephemeral=True)

    @app_commands.guild_only()
    @app_commands.describe(question="Your question (up to 4000 characters).")
    async def ask(
        self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 4000]
    ) -> None:
        if not await self._allow_interaction(interaction):
            return
        if self.group is None or not self.group.controls.enabled:
            await interaction.response.send_message("群聊模式现在关着。", ephemeral=True)
            return
        # Acknowledge within Discord's short deadline, before any database or API work.
        await interaction.response.defer(thinking=True)
        await self.service.record_message(
            IncomingMessage(
                id=interaction.id,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                author_id=interaction.user.id,
                author_is_bot=interaction.user.bot,
                content=question,
                created_at=interaction.created_at,
                source="ask",
            )
        )
        line = ChatLine(
            id=interaction.id,
            author_id=interaction.user.id,
            author_name=display_name(interaction.user),
            content=f"/ask {question}",
            created_at=interaction.created_at,
        )
        answers = await self.group.ask(
            interaction.channel_id,
            line,
            title=self._title(interaction),
            guild_id=interaction.guild_id,
        )
        for text in answers:
            for chunk in split_reply(text):
                sent = await interaction.followup.send(
                    chunk, allowed_mentions=NO_MENTIONS, suppress_embeds=True, wait=True
                )
                identifier = getattr(sent, "id", None)
                self.group.note_self(
                    interaction.channel_id,
                    ChatLine(
                        id=identifier if type(identifier) is int else -interaction.id,
                        author_id=self.user.id if self.user else 0,
                        author_name="小鲸鱼",
                        content=chunk,
                        created_at=datetime.now().astimezone(),
                        is_self=True,
                    ),
                )

    async def _save_dm_images(self, message):
        """Keep the owner's DM images on disk; the conversation refers to the files."""
        if not any(is_image_attachment(a) for a in getattr(message, "attachments", None) or []):
            return []
        directory = self.settings.personal_database_path.parent / "dm_images"
        saved = []
        for index, (name, data) in enumerate(await read_images(message)):
            path = directory / f"{message.id}-{index}.{image_type(data)}"
            try:
                directory.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except OSError:
                log.warning("Could not save a DM image")
                continue
            saved.append((name, path.resolve()))
        return saved

    @staticmethod
    def _title(source) -> tuple[str, str]:
        guild = getattr(getattr(source, "guild", None), "name", "") or ""
        channel = getattr(getattr(source, "channel", None), "name", "") or ""
        return guild, f"#{channel}" if channel else ""

    async def on_raw_typing(self, payload: discord.RawTypingEvent) -> None:
        if self.user is None or payload.user_id == self.user.id:
            return
        if payload.guild_id is None:
            if self.personal is not None and payload.user_id == self.settings.owner_discord_user_id:
                self.personal.note_typing(payload.channel_id)
        elif self.group is not None and payload.channel_id in self.settings.allowed_channel_ids:
            if payload.user is None or not payload.user.bot:
                self.group.note_typing(payload.channel_id)

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            if (
                self.user is not None
                and self.settings.personal_mode_enabled
                and self.personal is not None
                and isinstance(message.channel, discord.DMChannel)
            ):
                content = message.content
                images = []
                if message.author.id == self.settings.owner_discord_user_id:
                    images = await self._save_dm_images(message)
                saved = {name for name, _ in images}
                for attachment in getattr(message, "attachments", None) or []:
                    if attachment.filename not in saved:
                        # The agent can download these itself when it needs them.
                        content += f"\n[附件 {attachment.filename}: {attachment.url}]"
                await self.personal.handle_dm(
                    owner_id=message.author.id,
                    channel_id=message.channel.id,
                    content=content,
                    images=[path for _, path in images],
                    chat=DMChat(message.channel),
                    is_bot=message.author.bot,
                    webhook_id=message.webhook_id,
                )
            return
        if self.user is None:
            return
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None)
        resolved_author = getattr(resolved, "author", None)
        incoming = IncomingMessage(
            id=message.id,
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_is_bot=message.author.bot,
            content=message.content,
            created_at=message.created_at,
            webhook_id=message.webhook_id,
            reply_to_message_id=getattr(reference, "message_id", None),
            reply_to_author_id=getattr(resolved_author, "id", None),
        )
        if not await self.service.record_message(incoming) or self.group is None:
            return
        mentions = getattr(message, "mentions", None) or []
        mentioned = any(user.id == self.user.id for user in mentions) or bool(
            re.search(rf"<@!?{self.user.id}>", message.content)
        )
        self.group.observe(
            message.channel.id,
            await read_line(message, self.user.id),
            mentioned=mentioned,
            replied_to_bot=incoming.reply_to_author_id == self.user.id,
            title=self._title(message),
            guild_id=message.guild.id,
        )
