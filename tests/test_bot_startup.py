"""Startup checks for the bot.

Most of these now run in process. The bot used to validate its configuration
and connect to MongoDB as a side effect of being imported, so each case had to
be a subprocess with a doctored environment; the one subprocess left is the
test that proves the import really is inert.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import asyncio
import contextlib
import os
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pymongo.errors import PyMongoError

from bot.bot import connect_users, create_client, main
from bot.config import load_bot_config
from bot.health import stale_after_seconds
from common.config import ConfigError
from tests.test_starcheck import make_config as make_bot_config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_ENVIRONMENT = {
    "TOKEN": "fake.token.value",
    "REPO_OWNER": "owner",
    "GITHUB_REPO": "repo",
    "ROLE_ID": "111111111111111111",
    "GUILD_ID": "222222222222222222",
    "CHANNEL_ID": "333333333333333333",
    "DOMAIN": "https://example.com/",
    "SECRET_KEY": "0123456789abcdef-a-real-looking-key",
    "MONGO_HOST": "mongodb://127.0.0.1:27017/?serverSelectionTimeoutMS=1",
    "MONGO_DATABASE": "starguard_test",
}

# Read by load_bot_config but not part of a minimal configuration. A value
# left over in the developer's own environment would otherwise change what
# these tests see.
OPTIONAL_VARIABLES = (
    "CLIENT_ID",
    "GITHUB_TOKEN",
    "AUTOMATIC_CHECK",
    "AUTOMATIC_CHECK_DELAY",
    "COMMAND_NAME",
    "COMMAND_DESCRIPTION",
    "COMMAND_EXTENDED_DESCRIPTION",
    "BOT_HEALTH_ENABLED",
    "BOT_HEALTH_HOST",
    "BOT_HEALTH_PORT",
    "ROLE_SYNC_ENABLED",
    "ROLE_SYNC_INTERVAL",
    *[f"BTN{i}" for i in range(1, 5)],
    *[f"URL{i}" for i in range(1, 5)],
)


@pytest.fixture(name="environment")
def environment_fixture(monkeypatch):
    """Put a minimal, valid configuration in the environment."""

    def configure(**overrides):
        # load_dotenv must not pull a developer's real .env into the test run.
        monkeypatch.setattr("bot.bot.load_dotenv", lambda *a, **k: False)
        for name in OPTIONAL_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        settings = dict(BASE_ENVIRONMENT)
        settings.update(overrides)
        for name, value in settings.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    return configure


def test_config_loads_with_a_valid_environment(environment):
    environment()
    config = load_bot_config()

    # Discord IDs must reach the library as ints or every cache lookup misses.
    assert config.role_id == 111111111111111111
    assert isinstance(config.role_id, int)
    # A trailing slash on DOMAIN must not produce a '//login' URL.
    assert config.domain == "https://example.com"
    assert config.repo_url == "https://github.com/owner/repo/"


def test_missing_required_variable_exits_with_a_named_error(environment, caplog):
    environment(ROLE_ID=None)
    with pytest.raises(ConfigError, match="ROLE_ID"):
        load_bot_config()

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1
    assert "ROLE_ID" in caplog.text


def test_placeholder_secret_key_aborts_startup(environment):
    environment(SECRET_KEY="SecretKey")
    with pytest.raises(ConfigError, match="placeholder"):
        load_bot_config()


def test_non_numeric_role_id_aborts_startup(environment):
    environment(ROLE_ID="@Supporters")
    with pytest.raises(ConfigError, match="Discord ID"):
        load_bot_config()


@pytest.mark.parametrize(
    "domain", ["example.com", "http://example.com", "https:///login", "not a url"]
)
def test_domain_must_be_an_absolute_https_url(environment, domain):
    # Members follow this address from a Discord button, so a value Discord
    # or GitHub will reject has to fail here, where the variable is named.
    environment(DOMAIN=domain)
    with pytest.raises(ConfigError, match="DOMAIN"):
        load_bot_config()


def test_check_delay_is_clamped_to_the_minimum(environment):
    environment(AUTOMATIC_CHECK_DELAY="5")
    assert load_bot_config().check_delay == 300


def test_links_command_is_skipped_when_unconfigured(environment):
    # Previously the bot registered a command literally named "None" and
    # crashed on buttons with empty URLs.
    environment()
    config = load_bot_config()
    assert config.command_name == ""
    assert not config.link_buttons
    assert isinstance(config.link_buttons, tuple)
    assert config.links_command_enabled is False


def test_links_command_only_keeps_fully_configured_buttons(environment):
    environment(
        COMMAND_NAME="Hyperlinks",
        BTN1="GitHub",
        URL1="https://github.com/",
        BTN2="Discord",
        URL3="https://example.org/",
    )
    config = load_bot_config()
    assert config.command_name == "hyperlinks"
    assert config.link_buttons == (("GitHub", "https://github.com/"),)
    assert config.links_command_enabled is True


def test_create_client_registers_every_command(environment):
    environment(COMMAND_NAME="Hyperlinks", BTN1="GitHub", URL1="https://github.com/")
    client, checker = create_client(load_bot_config(), users=None)

    names = {str(command.name) for command in client.application_commands}
    assert names == {
        "ping",
        "help",
        "verify",
        "starcount",
        "checkstars",
        "hyperlinks",
    }
    assert "startup" in client.listeners
    assert checker.running is False


@pytest.mark.parametrize("module", ["bot.bot", "server.server"])
def test_importing_the_entry_point_has_no_side_effects(module):
    # The point of the factory refactor. With nothing in the environment the
    # old modules logged a configuration error and called sys.exit(1) from
    # the import itself.
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": REPO_ROOT},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


class FakeHealth:
    """Records what the startup listener reported about readiness."""

    def __init__(self):
        self.ready = []

    def mark_ready(self, stale_after=None):
        self.ready.append(stale_after)


class FakeClient:
    """Only what main() does with the client it built."""

    def __init__(self):
        self.started = False

    def start(self):
        self.started = True


def run_startup(client):
    """Fire the Startup event the way the gateway would, and clean up.

    Returns the background check task, or None when automatic checks are off.
    """

    async def scenario():
        await client.listeners["startup"][0].callback()
        task = getattr(client, "starguard_check_task", None)
        if task is not None:
            # The loop sleeps for the check interval after its first cycle,
            # so it is still pending here, which is the point.
            assert not task.done()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return task

    return asyncio.run(scenario())


def test_startup_starts_the_periodic_check_and_marks_the_bot_ready(environment):
    environment(AUTOMATIC_CHECK_DELAY="600")
    health = FakeHealth()
    client, _ = create_client(load_bot_config(), users=None, health=health)

    task = run_startup(client)

    # Holding the reference is what keeps asyncio from collecting the task
    # mid-run, so the attribute is part of the contract, not a detail.
    assert task is getattr(client, "starguard_check_task", None)
    assert health.ready == [stale_after_seconds(600)]


def test_startup_with_automatic_checks_off_starts_nothing(environment, caplog):
    environment(AUTOMATIC_CHECK="false")
    health = FakeHealth()
    client, _ = create_client(load_bot_config(), users=None, health=health)

    with caplog.at_level("INFO", logger="starguard.bot"):
        assert run_startup(client) is None

    assert not hasattr(client, "starguard_check_task")
    # Nothing can be late when nothing is scheduled, so the endpoint is told
    # there is no staleness to report rather than a deadline it will miss.
    assert health.ready == [None]
    assert "disabled" in caplog.text


def test_startup_without_a_health_endpoint_still_runs(environment):
    environment()
    client, _ = create_client(load_bot_config(), users=None)
    assert run_startup(client) is not None


@pytest.mark.parametrize("client_id,expected", [("4242", True), (None, False)])
def test_the_invite_link_is_logged_only_when_the_client_id_is_known(
    environment, caplog, client_id, expected
):
    environment(CLIENT_ID=client_id)
    client, _ = create_client(load_bot_config(), users=None)

    with caplog.at_level("INFO", logger="starguard.bot"):
        run_startup(client)

    assert ("oauth2/authorize" in caplog.text) is expected


def test_connect_users_returns_the_collection(monkeypatch):
    monkeypatch.setattr("bot.bot.connect", lambda host, database, factory: ("client", "users"))
    assert connect_users(make_bot_config()) == "users"


def test_an_unreachable_database_does_not_stop_the_bot(monkeypatch, caplog):
    # The bot still answers /ping and /help, and /verify says the database is
    # unavailable, which is more use than a process that refuses to start.
    def refuse(host, database, factory):
        raise PyMongoError("no route to host")

    monkeypatch.setattr("bot.bot.connect", refuse)
    with caplog.at_level("ERROR", logger="starguard.bot"):
        assert connect_users(make_bot_config()) is None
    assert "no route to host" in caplog.text


def wire_main(monkeypatch, users="the-users"):
    """Patch out everything main() would otherwise really do."""
    built = {}
    served = []
    client = FakeClient()
    checker = SimpleNamespace(last_completed=123.0)

    def fake_create_client(config, collection, health):
        built.update(config=config, users=collection, health=health)
        return client, checker

    monkeypatch.setattr("bot.bot.configure_logging", lambda *a, **k: "text")
    monkeypatch.setattr("bot.bot.connect_users", lambda config: users)
    monkeypatch.setattr("bot.bot.create_client", fake_create_client)
    monkeypatch.setattr("bot.bot.serve_health", lambda *args: served.append(args))
    return built, served, client


def test_main_builds_the_client_and_connects_to_discord(environment, monkeypatch):
    environment(BOT_HEALTH_PORT="9123")
    built, served, client = wire_main(monkeypatch)

    main()

    assert built["users"] == "the-users"
    assert client.started is True
    # The health endpoint reads the checker's current value rather than a
    # copy taken at startup, so it is handed a callable.
    state, host, port, last_completed = served[0]
    assert (host, port) == ("127.0.0.1", 9123)
    assert state is built["health"]
    assert last_completed() == 123.0


def test_main_skips_the_health_endpoint_when_it_is_turned_off(environment, monkeypatch):
    environment(BOT_HEALTH_ENABLED="false")
    _, served, client = wire_main(monkeypatch)

    main()

    assert not served
    assert client.started is True


@pytest.mark.parametrize("token,expected", [(None, True), ("ghp_token", False)])
def test_a_missing_github_token_is_called_out_at_startup(
    environment, monkeypatch, caplog, token, expected
):
    # 60 unauthenticated requests an hour is not enough for a repository with
    # more than a few thousand stargazers, and the failure looks like a
    # GitHub outage rather than a missing setting.
    environment(GITHUB_TOKEN=token)
    wire_main(monkeypatch)

    with caplog.at_level("WARNING", logger="starguard.bot"):
        main()

    assert ("GITHUB_TOKEN is not set" in caplog.text) is expected


@pytest.mark.parametrize("module", ["bot.bot", "server.server"])
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_running_the_module_as_a_script_calls_main(monkeypatch, module):
    # This is what the containers run. With nothing configured, main() has to
    # report the problem and exit non-zero rather than raise a traceback.
    for name in (
        *BASE_ENVIRONMENT,
        *OPTIONAL_VARIABLES,
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("common.logging_setup.configure_logging", lambda *a, **k: "text")

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module(module, run_name="__main__")

    assert excinfo.value.code == 1
