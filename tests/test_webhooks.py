"""The GitHub ``star`` webhook receiver.

The application is built through its real factory, against a mongomock
database rather than a stub, because two of the things under test are
properties of the database and not of this module: ``claim_delivery``
deduplicates by inserting against a unique index, and ``record_star_event``
raises ``role_sync_pending`` only when the star state actually moved.

Every signature here is computed over the exact bytes the request carries.
Signing a re-serialised dict would prove nothing, because a dict that round
trips through ``json.dumps`` is not what GitHub signed and not what the
endpoint verifies.
"""

# Test names document the behaviour under test.
# pylint: disable=missing-function-docstring

import hashlib
import hmac
import json

import pytest
from pymongo.errors import PyMongoError

from common.config import ConfigError
from common.storage import (
    clear_role_sync_pending,
    deliveries_for,
    ensure_delivery_indexes,
    ensure_indexes,
    find_link,
    link_account,
)
from server.config import WEBHOOK_SECRET_ENV, load_server_config, optional_secret
from server.server import create_app
from server.webhooks import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    EXTENSION_KEY,
    HOOK_ID_HEADER,
    MAX_REQUEST_BODY_BYTES,
    SIGNATURE_HEADER,
    SIGNATURE_PREFIX,
    WEBHOOK_PATH,
)
from tests.test_server_routes import ENVIRONMENT, make_config

mongomock = pytest.importorskip("mongomock")

SECRET = "webhook-secret-that-is-long-enough"
KNOWN_GITHUB_ID = 4242
REPO_URL = "https://github.com/owner/repo/"

# "sign this body with the configured secret", as opposed to None for no
# header at all or a string for a signature of the test's own choosing.
SIGN = object()


@pytest.fixture(name="users")
def users_fixture():
    collection = mongomock.MongoClient()["starguard"]["users"]
    ensure_indexes(collection)
    ensure_delivery_indexes(deliveries_for(collection))
    return collection


@pytest.fixture(name="client")
def client_fixture(users):
    return make_client(users)


def make_client(users, **overrides):
    config = make_config(webhook_secret=SECRET, **overrides)
    app = create_app(config, users=users)
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return app.test_client()


def link(users, discord_id="123456789", github_id=KNOWN_GITHUB_ID, starred=False):
    return link_account(
        users,
        discord_id=discord_id,
        discord_username="someone",
        github_id=github_id,
        github_username="SomeOne",
        linked_repo=REPO_URL,
        starred_repo=starred,
    )


def sign(body, secret=SECRET):
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def encode(payload):
    return json.dumps(payload).encode()


def star_body(action="created", github_id=KNOWN_GITHUB_ID, full_name="owner/repo"):
    return encode(
        {
            "action": action,
            # Null for "deleted", which is why the endpoint times the event
            # by its own clock rather than by this field.
            "starred_at": "2026-09-19T10:00:00Z" if action == "created" else None,
            "repository": {"id": 1, "full_name": full_name},
            "sender": {"id": github_id, "login": "someone"},
        }
    )


def post(client, body, *, event="star", delivery="delivery-1", signature=SIGN):
    headers = {
        EVENT_HEADER: event,
        "User-Agent": "GitHub-Hookshot/044aadd",
        "Content-Type": "application/json",
    }
    if delivery is not None:
        headers[DELIVERY_HEADER] = delivery
    if signature is SIGN:
        signature = sign(body)
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    return client.post(WEBHOOK_PATH, data=body, headers=headers)


# --- signature verification ----------------------------------------------


def test_a_valid_signature_is_accepted(client, users):
    link(users)
    assert post(client, star_body()).status_code == 202


def test_a_signature_made_with_another_secret_is_refused(client, users):
    link(users)
    body = star_body()
    assert post(client, body, signature=sign(body, "some-other-secret")).status_code == 401
    assert find_link(users, "123456789")["starred_repo"] is False


def test_a_missing_signature_header_is_refused(client):
    assert post(client, star_body(), signature=None).status_code == 401


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "deadbeef",  # no algorithm prefix
        "sha1=" + "a" * 40,  # the header GitHub sends alongside, not this one
        SIGNATURE_PREFIX,  # prefix and nothing else
        SIGNATURE_PREFIX + "not-hexadecimal" * 4,
        SIGNATURE_PREFIX + "ab" * 31,  # right alphabet, wrong length
        SIGNATURE_PREFIX + "é" * 64,  # would make compare_digest raise
    ],
)
def test_a_malformed_signature_header_is_refused(client, malformed):
    assert post(client, star_body(), signature=malformed).status_code == 401


def test_a_body_tampered_with_after_signing_is_refused(client, users):
    link(users)
    signed = star_body()
    # One byte of the account number changed, which is the whole attack: the
    # signature is still a real one, for a different body.
    tampered = signed.replace(b'"id": 4242', b'"id": 4243')
    assert tampered != signed
    assert post(client, tampered, signature=sign(signed)).status_code == 401


def test_an_uppercase_digest_still_verifies(client, users):
    # GitHub sends lower case, but the two spellings are the same digest and
    # refusing one of them would be a 401 nobody could debug.
    link(users)
    body = star_body()
    shouted = SIGNATURE_PREFIX + sign(body).removeprefix(SIGNATURE_PREFIX).upper()
    assert post(client, body, signature=shouted).status_code == 202


# --- event dispatch -------------------------------------------------------


def test_a_ping_is_answered_with_200(client):
    body = encode({"zen": "Keep it logically awesome.", "hook_id": 1})
    response = client.post(
        WEBHOOK_PATH,
        data=body,
        headers={
            EVENT_HEADER: "ping",
            DELIVERY_HEADER: "delivery-ping",
            HOOK_ID_HEADER: "12345678",
            SIGNATURE_HEADER: sign(body),
        },
    )
    assert response.status_code == 200
    assert response.data == b"pong"


def test_an_unsubscribed_event_is_accepted_and_ignored(client):
    response = post(client, encode({"action": "opened"}), event="issues")
    assert response.status_code == 204
    assert response.data == b""


def test_the_route_only_takes_post(client):
    assert client.get(WEBHOOK_PATH).status_code == 405


# --- payload shape --------------------------------------------------------


@pytest.mark.parametrize("body", [b"", b"{", b'{"action": }', b"\xff\xfe", b"[]", b'"star"'])
def test_a_body_that_is_not_a_json_object_is_refused(client, body):
    assert post(client, body).status_code == 400


@pytest.mark.parametrize(
    "repository",
    [
        {"full_name": "someone/else"},
        {"full_name": 42},
        {},
        "owner/repo",
        None,
    ],
)
def test_an_event_for_another_repository_is_refused(client, users, repository):
    link(users)
    body = encode(
        {
            "action": "created",
            "repository": repository,
            "sender": {"id": KNOWN_GITHUB_ID},
        }
    )
    assert post(client, body).status_code == 404
    assert find_link(users, "123456789")["starred_repo"] is False


def test_the_repository_name_is_matched_case_insensitively(users):
    # GitHub keeps the canonical capitalisation and treats the name as
    # case-insensitive everywhere else, so "Owner/Repo" in the environment
    # is a working configuration and not somebody else's repository.
    client = make_client(users, owner="Owner", repo="Repo")
    link(users)
    assert post(client, star_body(full_name="owner/repo")).status_code == 202


def test_a_delivery_without_an_id_is_refused(client, users):
    link(users)
    assert post(client, star_body(), delivery=None).status_code == 400


@pytest.mark.parametrize("action", [None, 5, ["created"]])
def test_an_event_with_no_usable_action_is_refused(client, users, action):
    link(users)
    body = encode(
        {
            "action": action,
            "repository": {"full_name": "owner/repo"},
            "sender": {"id": KNOWN_GITHUB_ID},
        }
    )
    assert post(client, body).status_code == 400


def test_an_unknown_action_is_accepted_and_ignored(client, users):
    link(users)
    response = post(client, star_body(action="edited"))
    assert response.status_code == 204
    assert find_link(users, "123456789")["starred_repo"] is False


@pytest.mark.parametrize(
    "sender",
    [
        None,
        {},
        {"login": "someone"},  # the mutable one, which is never enough
        {"id": "4242"},
        {"id": True},  # bool is an int in Python; account 1 is a real account
        {"id": None},
    ],
)
def test_an_event_with_no_usable_sender_id_is_refused(client, users, sender):
    link(users)
    body = encode(
        {
            "action": "created",
            "repository": {"full_name": "owner/repo"},
            "sender": sender,
        }
    )
    assert post(client, body).status_code == 400
    assert find_link(users, "123456789")["starred_repo"] is False


def test_a_body_larger_than_the_limit_is_refused(client):
    oversized = b"x" * (MAX_REQUEST_BODY_BYTES + 1)
    assert post(client, oversized).status_code == 413


# --- recording ------------------------------------------------------------


def test_a_star_queues_the_role_for_the_bot(client, users):
    link(users, starred=False)
    assert post(client, star_body("created")).status_code == 202

    row = find_link(users, "123456789")
    assert row["starred_repo"] is True
    assert row["role_sync_pending"] is True
    assert row["star_source"] == "webhook"
    assert row["star_event_at"] is not None


def test_an_unstar_queues_the_role_for_the_bot(client, users):
    link(users, starred=True)
    assert post(client, star_body("deleted")).status_code == 202

    row = find_link(users, "123456789")
    assert row["starred_repo"] is False
    assert row["role_sync_pending"] is True


def test_an_event_that_changes_nothing_queues_nothing(client, users):
    # The bot polls this queue; asking it to re-apply a role it has already
    # applied is work for nothing.
    link(users, starred=True)
    assert post(client, star_body("created")).status_code == 202
    assert "role_sync_pending" not in find_link(users, "123456789")


@pytest.mark.parametrize("action", ["created", "deleted"])
def test_a_star_from_a_stranger_is_accepted_and_dropped(client, users, action):
    # Most people who star the repository have never used the bot.
    link(users, github_id=99)
    response = post(client, star_body(action, github_id=KNOWN_GITHUB_ID))
    assert response.status_code == 204
    assert find_link(users, "123456789")["starred_repo"] is False


def test_a_replayed_delivery_does_nothing(client, users):
    link(users, starred=False)
    assert post(client, star_body("created"), delivery="same-id").status_code == 202

    # The bot has since moved the role and taken the row off the queue.
    clear_role_sync_pending(users, "123456789", starred=True)

    replay = post(client, star_body("created"), delivery="same-id")
    assert replay.status_code == 200
    assert find_link(users, "123456789")["role_sync_pending"] is False


def test_a_different_delivery_of_the_same_event_is_not_a_replay(client, users):
    # Deduplication is by delivery id, not by content.
    link(users, starred=False)
    assert post(client, star_body("created"), delivery="first").status_code == 202
    assert post(client, star_body("created"), delivery="second").status_code == 202


def test_the_receiver_reports_a_database_it_cannot_reach():
    # A 5xx is a failed delivery in GitHub's log, which is the one place an
    # operator can redeliver it from once the database is back.
    client = make_client(None)
    assert post(client, star_body()).status_code == 503


@pytest.mark.parametrize("failing", ["claim_delivery", "record_star_event"])
def test_a_database_error_mid_request_is_reported(client, users, monkeypatch, failing):
    link(users)

    def explode(*_args, **_kwargs):
        raise PyMongoError("connection lost")

    monkeypatch.setattr(f"server.webhooks.{failing}", explode)
    assert post(client, star_body()).status_code == 503


def test_the_receiver_is_not_rate_limited(users):
    # Every real delivery arrives from a handful of GitHub addresses, so a
    # per-address limit would put the whole event stream in one bucket, and
    # GitHub never retries what it could not deliver.
    client = make_client(users, rate_limit=1, rate_limit_window=60)
    link(users)
    for delivery in range(5):
        response = post(client, star_body(), delivery=f"delivery-{delivery}")
        assert response.status_code in (202, 204)
    # The limiter really is installed on this application.
    client.get("/login")
    assert client.get("/login").status_code == 429


# --- configuration --------------------------------------------------------


def test_without_a_secret_there_is_no_route_at_all(users):
    app = create_app(make_config(), users=users)
    client = app.test_client()

    assert WEBHOOK_PATH not in {rule.rule for rule in app.url_map.iter_rules()}
    assert EXTENSION_KEY not in app.extensions
    assert client.post(WEBHOOK_PATH, data=star_body()).status_code == 404


def test_the_secret_is_read_from_the_environment(monkeypatch):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(WEBHOOK_SECRET_ENV, SECRET)
    assert load_server_config().webhook_secret == SECRET


def test_an_unset_secret_is_none(monkeypatch):
    monkeypatch.delenv(WEBHOOK_SECRET_ENV, raising=False)
    assert optional_secret(WEBHOOK_SECRET_ENV) is None


@pytest.mark.parametrize("placeholder", ["changeme", "SecretKey", "your-secret-key"])
def test_a_placeholder_secret_is_refused(monkeypatch, placeholder):
    monkeypatch.setenv(WEBHOOK_SECRET_ENV, placeholder)
    with pytest.raises(ConfigError, match="placeholder"):
        optional_secret(WEBHOOK_SECRET_ENV)


def test_a_short_secret_is_refused(monkeypatch):
    monkeypatch.setenv(WEBHOOK_SECRET_ENV, "short")
    with pytest.raises(ConfigError, match="at least"):
        optional_secret(WEBHOOK_SECRET_ENV)
