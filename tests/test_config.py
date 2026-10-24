from pathlib import Path

import pytest

from discord_dfy.config import ConfigurationError, Settings

VALID = {
    "DISCORD_BOT_TOKEN": "private-discord-token",
    "DEEPSEEK_API_KEY": "private-deepseek-key",
    "ALLOWED_CHANNEL_IDS": "100, 200,100",
}


def test_valid_config_and_safe_repr():
    settings = Settings.from_env(VALID)
    assert settings.allowed_channel_ids == frozenset({100, 200})
    assert settings.store_messages is True
    assert settings.database_path == Path("data/bot.sqlite3")
    assert VALID["DISCORD_BOT_TOKEN"] not in repr(settings)
    assert VALID["DEEPSEEK_API_KEY"] not in repr(settings)


@pytest.mark.parametrize("name", VALID)
def test_required_values(name):
    env = dict(VALID)
    del env[name]
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env(env)


@pytest.mark.parametrize("value", ["", "*", "100,", "0", "-100", "abc", str(2**63), "１２３"])
def test_fail_closed_on_invalid_channel_list(value):
    with pytest.raises(ConfigurationError, match="ALLOWED_CHANNEL_IDS"):
        Settings.from_env({**VALID, "ALLOWED_CHANNEL_IDS": value})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("STORE_MESSAGES", "yes"),
        ("LOG_LEVEL", "trace"),
        ("DATABASE_PATH", " "),
        ("TEST_GUILD_ID", "invalid"),
    ],
)
def test_bad_optional_values(name, value):
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env({**VALID, name: value})


def test_optional_values():
    settings = Settings.from_env({**VALID, "STORE_MESSAGES": " FALSE ", "TEST_GUILD_ID": "42"})
    assert not settings.store_messages
    assert settings.test_guild_id == 42


def test_dotenv_is_literal_and_environment_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (*VALID, "STORE_MESSAGES", "DATABASE_PATH", "LOG_LEVEL", "TEST_GUILD_ID"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        'DISCORD_BOT_TOKEN="literal-${SHOULD_NOT_EXPAND}"\n'
        'DEEPSEEK_API_KEY="file-key"\nALLOWED_CHANNEL_IDS=100\nSTORE_MESSAGES=false\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-key")
    settings = Settings.from_env()
    assert settings.discord_bot_token == "literal-${SHOULD_NOT_EXPAND}"
    assert settings.deepseek_api_key == "environment-key"
    assert not settings.store_messages
