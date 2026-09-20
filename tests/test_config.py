"""Tests for the environment configuration helpers."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import pytest

from common.config import (
    MAX_PORT,
    MIN_PORT,
    ConfigError,
    env_bool,
    env_int,
    env_port,
    optional_env,
    require_env,
    require_https_url,
    require_secret_key,
    require_snowflake,
)

GOOD_KEY = "0123456789abcdef-a-real-looking-key"


def test_require_env_returns_the_value(monkeypatch):
    monkeypatch.setenv("THING", "  value  ")
    assert require_env("THING") == "value"


@pytest.mark.parametrize("value", ["", "   "])
def test_require_env_rejects_blank(monkeypatch, value):
    monkeypatch.setenv("THING", value)
    with pytest.raises(ConfigError, match="THING"):
        require_env("THING")


def test_require_env_rejects_missing(monkeypatch):
    monkeypatch.delenv("THING", raising=False)
    with pytest.raises(ConfigError, match="THING"):
        require_env("THING")


def test_optional_env_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("THING", raising=False)
    assert optional_env("THING", "fallback") == "fallback"
    monkeypatch.setenv("THING", "  ")
    assert optional_env("THING", "fallback") == "fallback"


def test_env_int_clamps_to_the_minimum(monkeypatch):
    monkeypatch.setenv("DELAY", "10")
    assert env_int("DELAY", 3600, minimum=300) == 300


def test_env_int_uses_the_default_when_unset(monkeypatch):
    monkeypatch.delenv("DELAY", raising=False)
    assert env_int("DELAY", 3600, minimum=300) == 3600


def test_env_int_rejects_nonsense(monkeypatch):
    monkeypatch.setenv("DELAY", "one hour")
    with pytest.raises(ConfigError, match="whole number"):
        env_int("DELAY", 3600)


@pytest.mark.parametrize("raw", [str(MIN_PORT), "8080", str(MAX_PORT)])
def test_env_port_accepts_the_whole_range_including_its_ends(monkeypatch, raw):
    monkeypatch.setenv("PORT", raw)
    assert env_port("PORT", 5000) == int(raw)


def test_env_port_uses_the_default_when_unset(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert env_port("PORT", 5000) == 5000


@pytest.mark.parametrize("raw", [str(MIN_PORT - 1), str(MAX_PORT + 1), "70000", "-1"])
def test_env_port_refuses_a_number_that_is_not_a_port(monkeypatch, raw):
    # Refused rather than clamped, which is the difference from env_int's
    # minimum. A clamped port is not a smaller version of what was asked
    # for, it is a different address: 70000 would bind 65535 and answer
    # there, and the operator hunting the typo would find a working
    # service on a port they never named.
    monkeypatch.setenv("PORT", raw)
    with pytest.raises(ConfigError, match="PORT") as excinfo:
        env_port("PORT", 5000)

    # The message has to name the variable and the range, because it is
    # the only thing the operator gets.
    assert "TCP port" in str(excinfo.value)
    assert f"{MIN_PORT} and {MAX_PORT}" in str(excinfo.value)
    assert raw in str(excinfo.value)


def test_env_port_rejects_nonsense_the_way_env_int_does(monkeypatch):
    monkeypatch.setenv("PORT", "http")
    with pytest.raises(ConfigError, match="whole number"):
        env_port("PORT", 5000)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("off", False),
    ],
)
def test_env_bool_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("FLAG", raw)
    assert env_bool("FLAG", default=True) is expected


def test_env_bool_rejects_nonsense(monkeypatch):
    monkeypatch.setenv("FLAG", "maybe")
    with pytest.raises(ConfigError, match="boolean"):
        env_bool("FLAG")


def test_require_snowflake_returns_an_int(monkeypatch):
    # Discord's cache is keyed by int; returning the raw string silently
    # missed every lookup.
    monkeypatch.setenv("ROLE_ID", "123456789012345678")
    value = require_snowflake("ROLE_ID")
    assert value == 123456789012345678
    assert isinstance(value, int)


def test_require_snowflake_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("ROLE_ID", "@Supporters")
    with pytest.raises(ConfigError, match="Discord ID"):
        require_snowflake("ROLE_ID")


def test_secret_key_accepts_a_real_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", GOOD_KEY)
    assert require_secret_key() == GOOD_KEY


@pytest.mark.parametrize("placeholder", ["SecretKey", "secretkey", "changeme", "secret"])
def test_secret_key_rejects_placeholders(monkeypatch, placeholder):
    # .env.example shipped SECRET_KEY=SecretKey; a guessable key lets anyone
    # forge both session cookies and verification links.
    monkeypatch.setenv("SECRET_KEY", placeholder)
    with pytest.raises(ConfigError, match="placeholder"):
        require_secret_key()


def test_secret_key_rejects_short_values(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "tooshort")
    with pytest.raises(ConfigError, match="at least"):
        require_secret_key()


def test_secret_key_rejects_missing(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        require_secret_key()


def test_require_https_url_returns_it_without_a_trailing_slash(monkeypatch):
    monkeypatch.setenv("DOMAIN", "https://starguard.example.com/")
    assert require_https_url("DOMAIN") == "https://starguard.example.com"


@pytest.mark.parametrize(
    "raw", ["example.com", "http://example.com", "https:///login", "not a url"]
)
def test_require_https_url_rejects_anything_a_button_cannot_use(monkeypatch, raw):
    # Discord refuses to render a button whose URL has no scheme, and the
    # failure is silent unless the variable is named here.
    monkeypatch.setenv("DOMAIN", raw)
    with pytest.raises(ConfigError, match="absolute https URL"):
        require_https_url("DOMAIN")


@pytest.mark.parametrize(
    "raw",
    ["https://example.com/?next=/admin", "https://example.com/#fragment"],
)
def test_require_https_url_rejects_a_query_string_or_fragment(monkeypatch, raw):
    # The server appends its own query string to this value, so anything
    # already there would produce a URL with two of them.
    monkeypatch.setenv("DOMAIN", raw)
    with pytest.raises(ConfigError, match="no query string or fragment"):
        require_https_url("DOMAIN")
