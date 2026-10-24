"""Validated, explicit environment configuration. Secrets are never represented."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .persona import DEFAULT_PERSONALITY


class ConfigurationError(ValueError):
    """A configuration variable is missing or invalid (without echoing its value)."""


def _snowflake(value: str, name: str) -> int:
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**63:
        raise ConfigurationError(f"{name} must contain positive Discord IDs.")
    return int(value)


@dataclass(frozen=True, slots=True)
class Settings:
    discord_bot_token: str = field(repr=False)
    deepseek_api_key: str = field(repr=False)
    allowed_channel_ids: frozenset[int]
    store_messages: bool = True
    database_path: Path = Path("data/bot.sqlite3")
    log_level: str = "INFO"
    test_guild_id: int | None = None
    # Persona override shared by every mode (name kept for existing .env files).
    autonomous_personality: str = DEFAULT_PERSONALITY
    social_level: int = 1
    social_max_calls_per_hour: int = 120
    social_thinking: bool = False
    social_reasoning_effort: str = "low"
    social_history_messages: int = 50
    social_history_minutes: int = 120
    social_state_path: Path = Path("data/group_state.json")
    social_sticker_dir: Path = Path("data/stickers")
    personal_mode_enabled: bool = False
    owner_discord_user_id: int | None = None
    personal_database_path: Path = Path("data/personal.sqlite3")
    agent_workdir: Path = field(default_factory=Path.home)
    agent_model: str = "deepseek-flash"
    agent_thinking: bool = True
    agent_reasoning_effort: str = "high"
    agent_max_steps: int = 60
    agent_command_timeout_seconds: int = 300
    agent_context_chars: int = 2000000
    agent_compact_tokens: int = 200000
    agent_memory_path: Path = Path("data/memory.md")
    agent_reflect_seconds: int = 600
    agent_initiative: bool = True
    agent_initiative_per_day: int = 3
    agent_initiative_idle_minutes: int = 180
    agent_quiet_hours: str = "03:00-11:00"
    agent_show_tools: bool = True
    agent_show_reasoning: bool = False
    monitor_enabled: bool = False
    monitor_directory: Path = Path("data/monitor")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            # Resolve only the current directory's .env, never a parent project's secrets.
            # No ${...} expansion: credentials must be used literally.
            load_dotenv(Path.cwd() / ".env", override=False, interpolate=False)
            env = os.environ

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value or value.startswith("replace_with_"):
                raise ConfigurationError(f"Set {name} in .env or the environment.")
            return value

        token = required("DISCORD_BOT_TOKEN")
        key = required("DEEPSEEK_API_KEY")
        raw_channels = required("ALLOWED_CHANNEL_IDS")
        channels = frozenset(
            _snowflake(part.strip(), "ALLOWED_CHANNEL_IDS") for part in raw_channels.split(",")
        )
        storage = env.get("STORE_MESSAGES", "true").strip().lower()
        if storage not in {"true", "false"}:
            raise ConfigurationError("STORE_MESSAGES must be true or false.")
        level = env.get("LOG_LEVEL", "INFO").strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
        raw_path = env.get("DATABASE_PATH", "data/bot.sqlite3").strip()
        if not raw_path:
            raise ConfigurationError("DATABASE_PATH must not be empty.")
        raw_guild = env.get("TEST_GUILD_ID", "").strip()

        def boolean(name: str, default: str) -> bool:
            value = env.get(name, default).strip().lower()
            if value not in {"true", "false"}:
                raise ConfigurationError(f"{name} must be true or false.")
            return value == "true"

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            raw = env.get(name, str(default)).strip()
            if not raw.isascii() or not raw.isdecimal() or not minimum <= int(raw) <= maximum:
                raise ConfigurationError(f"{name} must be an integer from {minimum} to {maximum}.")
            return int(raw)

        effort = env.get("SOCIAL_REASONING_EFFORT", "low").strip().lower()
        if effort not in {"low", "high", "max"}:
            raise ConfigurationError("SOCIAL_REASONING_EFFORT must be low, high, or max.")
        state_path = env.get("SOCIAL_STATE_PATH", "data/group_state.json").strip()
        if not state_path:
            raise ConfigurationError("SOCIAL_STATE_PATH must not be empty.")
        personality = env.get(
            "AUTONOMOUS_PERSONALITY", cls.__dataclass_fields__["autonomous_personality"].default
        ).strip()
        if not personality or len(personality) > 12000:
            raise ConfigurationError("AUTONOMOUS_PERSONALITY must contain 1 to 12000 characters.")
        personal = boolean("PERSONAL_MODE_ENABLED", "false")
        raw_owner = env.get("OWNER_DISCORD_USER_ID", "").strip()
        owner = _snowflake(raw_owner, "OWNER_DISCORD_USER_ID") if raw_owner else None
        if personal and owner is None:
            raise ConfigurationError("Set OWNER_DISCORD_USER_ID before enabling Personal Mode.")
        personal_path = env.get("PERSONAL_DATABASE_PATH", "data/personal.sqlite3").strip()
        workdir = env.get("AGENT_WORKDIR", "").strip()
        workdir_path = Path(workdir) if workdir else Path.home()
        if personal and not workdir_path.is_dir():
            raise ConfigurationError("AGENT_WORKDIR must be an existing directory.")
        agent_model = env.get("AGENT_MODEL", "deepseek-flash").strip()
        if not agent_model:
            raise ConfigurationError("AGENT_MODEL must not be empty.")
        memory_path = env.get("AGENT_MEMORY_PATH", "data/memory.md").strip()
        if not memory_path:
            raise ConfigurationError("AGENT_MEMORY_PATH must not be empty.")
        quiet_hours = env.get("AGENT_QUIET_HOURS", "03:00-11:00").strip()
        if quiet_hours and parse_quiet_hours(quiet_hours) is None:
            raise ConfigurationError("AGENT_QUIET_HOURS must look like 03:00-11:00 or be empty.")
        # 0 turns automatic compaction off (the owner compacts with !compact).
        compact_tokens = integer("AGENT_COMPACT_TOKENS", 200000, 0, 900000)
        if 0 < compact_tokens < 10000:
            raise ConfigurationError("AGENT_COMPACT_TOKENS must be 0 or from 10000 to 900000.")
        agent_effort = env.get("AGENT_REASONING_EFFORT", "high").strip().lower()
        if agent_effort not in {"low", "high", "max"}:
            raise ConfigurationError("AGENT_REASONING_EFFORT must be low, high, or max.")
        monitor_directory = env.get("MONITOR_DIRECTORY", "data/monitor").strip()
        if not monitor_directory:
            raise ConfigurationError("MONITOR_DIRECTORY must not be empty.")
        if not personal_path:
            raise ConfigurationError("PERSONAL_DATABASE_PATH must not be empty.")
        if personal and Path(personal_path).resolve() == Path(raw_path).resolve():
            raise ConfigurationError("PERSONAL_DATABASE_PATH must differ from DATABASE_PATH.")
        return cls(
            discord_bot_token=token,
            deepseek_api_key=key,
            allowed_channel_ids=channels,
            store_messages=storage == "true",
            database_path=Path(raw_path),
            log_level=level,
            test_guild_id=_snowflake(raw_guild, "TEST_GUILD_ID") if raw_guild else None,
            autonomous_personality=personality,
            social_level=integer("SOCIAL_LEVEL", 1, 0, 3),
            social_max_calls_per_hour=integer("SOCIAL_MAX_CALLS_PER_HOUR", 120, 1, 5000),
            social_thinking=boolean("SOCIAL_THINKING", "false"),
            social_reasoning_effort=effort,
            social_history_messages=integer("SOCIAL_HISTORY_MESSAGES", 50, 5, 200),
            social_history_minutes=integer("SOCIAL_HISTORY_MINUTES", 120, 5, 1440),
            social_state_path=Path(state_path),
            social_sticker_dir=Path(
                env.get("SOCIAL_STICKER_DIR", "data/stickers").strip() or "data/stickers"
            ),
            personal_mode_enabled=personal,
            owner_discord_user_id=owner,
            personal_database_path=Path(personal_path),
            agent_workdir=workdir_path,
            agent_model=agent_model,
            agent_thinking=boolean("AGENT_THINKING", "true"),
            agent_reasoning_effort=agent_effort,
            agent_max_steps=integer("AGENT_MAX_STEPS", 60, 1, 1000),
            agent_command_timeout_seconds=integer("AGENT_COMMAND_TIMEOUT_SECONDS", 300, 5, 3600),
            agent_context_chars=integer("AGENT_CONTEXT_CHARS", 2000000, 20000, 4000000),
            agent_compact_tokens=compact_tokens,
            agent_memory_path=Path(memory_path),
            agent_reflect_seconds=integer("AGENT_REFLECT_SECONDS", 600, 0, 86400),
            agent_initiative=boolean("AGENT_INITIATIVE", "true"),
            agent_initiative_per_day=integer("AGENT_INITIATIVE_PER_DAY", 3, 1, 24),
            agent_initiative_idle_minutes=integer("AGENT_INITIATIVE_IDLE_MINUTES", 180, 10, 10080),
            agent_quiet_hours=quiet_hours,
            agent_show_tools=boolean("AGENT_SHOW_TOOLS", "true"),
            agent_show_reasoning=boolean("AGENT_SHOW_REASONING", "false"),
            monitor_enabled=boolean("MONITOR_ENABLED", "false"),
            monitor_directory=Path(monitor_directory),
        )


def parse_quiet_hours(text: str):
    """Parse "03:00-11:00" into (180, 660) minutes of the day; None if malformed.

    The range may wrap midnight ("23:30-07:00").
    """
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text.strip())
    if match is None:
        return None
    start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
    if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59:
        return None
    return start_h * 60 + start_m, end_h * 60 + end_m
