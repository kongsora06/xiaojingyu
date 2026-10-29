"""Application composition and graceful resource cleanup."""

import asyncio
import logging
import os

import aiosqlite
import discord
from openai import AsyncOpenAI

from .ai import BASE_URL
from .config import ConfigurationError, Settings
from .conversation_store import ConversationStore
from .discord_bot import DiscordAgent
from .group_chat import GroupChat
from .host_agent import HostAgent
from .initiative import Initiative
from .journal import consolidate
from .logging_setup import configure_logging
from .observability import MonitorBus
from .policy import ChannelPolicy
from .service import AgentService
from .storage import Storage
from .supervisor import RESTART_EXIT_CODE
from .web import Web

log = logging.getLogger(__name__)
# How often old weeks are checked for folding into the archive.
CONSOLIDATE_SECONDS = 3600


async def keep_memory(settings: Settings, client, bot) -> None:
    """Fold weeks older than the recent window into the archive; tell the owner what moved."""
    await bot.wait_until_ready()
    while True:
        try:
            report = await consolidate(client, settings.agent_model, settings.agent_memory_path)
        except Exception as error:
            log.warning("Memory consolidation failed; error_type=%s", type(error).__name__)
            report = None
        if report and settings.personal_mode_enabled:
            lines = "\n".join(f"-# {line}" for line in report.splitlines())
            await bot.notice_owner("-# 🗂️ 她回头翻了翻两个星期前的事\n" + lines)
        await asyncio.sleep(CONSOLIDATE_SECONDS)


async def run(settings: Settings) -> bool:
    """Run until shutdown; True means the owner asked for a restart."""
    policy = ChannelPolicy(settings.allowed_channel_ids)
    storage = Storage(settings.database_path, policy, store_messages=settings.store_messages)
    await storage.open()
    personal_store = None
    personal = None
    group = None
    initiative = None
    monitor = None
    restart = False
    try:
        if settings.monitor_enabled:
            monitor = MonitorBus(
                settings.monitor_directory, (settings.discord_bot_token, settings.deepseek_api_key)
            )
            try:
                await monitor.open()
            except OSError:
                log.warning("Local monitor unavailable; observation disabled")
                monitor = None
        # Agent turns think and call tools, so allow long requests; group chat sets its own.
        async with AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=BASE_URL,
            timeout=600.0,
            max_retries=2,
        ) as client:
            service = AgentService(storage, policy)
            web = Web(client)
            try:
                async with DiscordAgent(settings, service) as bot:
                    group = GroupChat(
                        settings,
                        client,
                        bot.transport,
                        storage=storage,
                        observer=monitor,
                        web=web,
                    )
                    bot.group = group
                    group.profile = bot.profile
                    if settings.personal_mode_enabled:
                        personal_store = ConversationStore(settings.personal_database_path)
                        await personal_store.open()
                        supervised = os.environ.get("DFY_SUPERVISED") == "1"
                        personal = HostAgent(
                            settings,
                            client,
                            personal_store,
                            observer=monitor,
                            restarter=bot.request_restart if supervised else None,
                            group=group,
                            web=web,
                        )
                        bot.personal = personal
                        personal.profile = bot.profile
                        # Never double-text: a DM to the owner waits while one is unanswered.
                        group.owner_waiting = lambda: any(personal.outgoing.values())
                        initiative = Initiative(
                            personal, lambda text: bot.message_owner(text, source="self")
                        )
                        personal.initiative = initiative
                        initiative.start()
                    await service.record_event("started")
                    memory_task = asyncio.create_task(keep_memory(settings, client, bot))
                    try:
                        await bot.start(settings.discord_bot_token)
                    finally:
                        memory_task.cancel()
                        await asyncio.gather(memory_task, return_exceptions=True)
                    restart = bot.restart_requested
            finally:
                await service.record_event("stopped")
    finally:
        if initiative is not None:
            await initiative.close()
        if personal is not None:
            await personal.close()
        if group is not None:
            await group.close()
        if personal_store is not None:
            await personal_store.close()
        if monitor is not None:
            await monitor.close()
        await storage.close()
    return restart


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        # All configuration exceptions contain variable names and fixed instructions only.
        configure_logging()
        log.error("Configuration error: %s", error)
        raise SystemExit(2) from None
    configure_logging(
        level=settings.log_level,
        secrets=(settings.discord_bot_token, settings.deepseek_api_key),
    )
    try:
        if asyncio.run(run(settings)):
            log.info("Restarting at the owner's request")
            raise SystemExit(RESTART_EXIT_CODE)
    except KeyboardInterrupt:
        log.info("Bot stopped")
    except discord.LoginFailure:
        log.error("Discord login failed. Check DISCORD_BOT_TOKEN.")
        raise SystemExit(1) from None
    except discord.PrivilegedIntentsRequired:
        log.error("Enable Message Content Intent in the Discord Developer Portal.")
        raise SystemExit(1) from None
    except (discord.DiscordException, aiosqlite.Error, OSError) as error:
        log.error("Startup/runtime failure; error_type=%s", type(error).__name__)
        raise SystemExit(1) from None
    except Exception as error:
        log.error("Unexpected failure; error_type=%s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
