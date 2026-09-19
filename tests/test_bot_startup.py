"""Startup checks for the bot.

The bot is run in a subprocess so each case gets a clean environment and the
Discord client from one case cannot leak into the next.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import os
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_ENVIRONMENT = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "PYTHONPATH": REPO_ROOT,
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


def run_bot(script, **overrides):
    """Import bot.bot in a subprocess and run ``script`` against it."""
    environment = dict(BASE_ENVIRONMENT)
    for key, value in overrides.items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    return subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


IMPORT_AND_PRINT = """
import bot.bot as b
print("ROLE", repr(b.CONFIG["role_id"]))
print("DOMAIN", b.CONFIG["domain"])
print("BUTTONS", b.CONFIG["link_buttons"])
print("DELAY", b.CONFIG["check_delay"])
print("COMMAND", repr(b.CONFIG["command_name"]))
b.register_links_command()
print("OK")
"""


def test_bot_imports_with_a_valid_configuration():
    result = run_bot(IMPORT_AND_PRINT)
    assert "OK" in result.stdout, result.stderr
    # Discord IDs must reach the library as ints or every cache lookup misses.
    assert "ROLE 111111111111111111" in result.stdout
    # A trailing slash on DOMAIN must not produce a '//login' URL.
    assert "DOMAIN https://example.com" in result.stdout


def test_missing_required_variable_exits_with_a_named_error():
    result = run_bot(IMPORT_AND_PRINT, ROLE_ID=None)
    assert result.returncode == 1
    assert "ROLE_ID" in result.stderr


def test_placeholder_secret_key_aborts_startup():
    result = run_bot(IMPORT_AND_PRINT, SECRET_KEY="SecretKey")
    assert result.returncode == 1
    assert "placeholder" in result.stderr


def test_non_numeric_role_id_aborts_startup():
    result = run_bot(IMPORT_AND_PRINT, ROLE_ID="@Supporters")
    assert result.returncode == 1
    assert "Discord ID" in result.stderr


def test_check_delay_is_clamped_to_the_minimum():
    result = run_bot(IMPORT_AND_PRINT, AUTOMATIC_CHECK_DELAY="5")
    assert "DELAY 300" in result.stdout, result.stderr


def test_links_command_is_skipped_when_unconfigured():
    # Previously the bot registered a command literally named "None" and
    # crashed on buttons with empty URLs.
    result = run_bot(IMPORT_AND_PRINT)
    assert "COMMAND ''" in result.stdout, result.stderr
    assert "BUTTONS []" in result.stdout
    assert "OK" in result.stdout


def test_links_command_only_keeps_fully_configured_buttons():
    result = run_bot(
        IMPORT_AND_PRINT,
        COMMAND_NAME="Hyperlinks",
        BTN1="GitHub",
        URL1="https://github.com/",
        BTN2="Discord",
        URL3="https://example.org/",
    )
    assert "COMMAND 'hyperlinks'" in result.stdout, result.stderr
    assert "BUTTONS [('GitHub', 'https://github.com/')]" in result.stdout
    assert "OK" in result.stdout
