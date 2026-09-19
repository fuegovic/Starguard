"""Tests for the signed verification links."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import base64
import json
import time

import pytest

from common.linktoken import (
    DEFAULT_MAX_AGE_SECONDS,
    LinkTokenError,
    issue_link_token,
    read_link_token,
)

SECRET = "a-test-secret-key-long-enough"
OTHER_SECRET = "a-different-secret-key-value"


def test_round_trip_preserves_identity():
    token = issue_link_token(SECRET, 123456789, "someone")
    assert read_link_token(SECRET, token) == ("123456789", "someone")


def test_integer_and_string_ids_are_equivalent():
    assert read_link_token(SECRET, issue_link_token(SECRET, 42, "a")) == read_link_token(
        SECRET, issue_link_token(SECRET, "42", "a")
    )


def test_token_signed_with_another_key_is_rejected():
    token = issue_link_token(OTHER_SECRET, 123456789, "someone")
    with pytest.raises(LinkTokenError):
        read_link_token(SECRET, token)


def test_tampered_payload_is_rejected():
    # Rewriting the payload is the attack that matters: it is how someone would
    # swap in another user's Discord ID.
    payload, timestamp, signature = issue_link_token(
        SECRET, 123456789, "someone"
    ).split(".")
    forged_payload = base64.urlsafe_b64encode(
        json.dumps({"id": "987654321", "name": "someone"}).encode()
    ).rstrip(b"=").decode()
    assert forged_payload != payload

    with pytest.raises(LinkTokenError):
        read_link_token(SECRET, f"{forged_payload}.{timestamp}.{signature}")


def test_tampered_signature_is_rejected():
    payload, timestamp, signature = issue_link_token(
        SECRET, 123456789, "someone"
    ).split(".")
    flipped = "".join("A" if c != "A" else "B" for c in signature[:4])
    with pytest.raises(LinkTokenError):
        read_link_token(SECRET, f"{payload}.{timestamp}.{flipped}{signature[4:]}")


def test_expired_token_is_rejected(monkeypatch):
    token = issue_link_token(SECRET, 123456789, "someone")
    real_time = time.time
    monkeypatch.setattr(
        time, "time", lambda: real_time() + DEFAULT_MAX_AGE_SECONDS + 60
    )
    with pytest.raises(LinkTokenError, match="expired"):
        read_link_token(SECRET, token)


@pytest.mark.parametrize("bad", [None, "", "not-a-token", "a.b.c"])
def test_malformed_tokens_are_rejected(bad):
    with pytest.raises(LinkTokenError):
        read_link_token(SECRET, bad)


def test_non_numeric_discord_id_is_rejected():
    # A token whose payload is well-formed but whose id is not a snowflake must
    # not be accepted, since the bot looks users up by that value.
    forged = issue_link_token(SECRET, "; DROP", "someone")
    with pytest.raises(LinkTokenError):
        read_link_token(SECRET, forged)


def test_missing_secret_key_is_an_error():
    with pytest.raises(LinkTokenError):
        issue_link_token("", 1, "a")
