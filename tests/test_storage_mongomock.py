"""Storage tests against mongomock, which implements the real pymongo API.

The stub in test_storage.py keeps those tests readable; these confirm the same
behaviour against index enforcement, ``$unset`` and projections as MongoDB
actually implements them.
"""

# pylint: disable=missing-function-docstring

import pytest
from pymongo.errors import DuplicateKeyError

from common.storage import (
    AccountAlreadyLinkedError,
    all_links,
    ensure_indexes,
    find_link,
    link_account,
    purge_legacy_secrets,
    set_starred,
)

mongomock = pytest.importorskip("mongomock")

REPO = "https://github.com/owner/repo/"


@pytest.fixture(name="users")
def users_fixture():
    collection = mongomock.MongoClient()["starguard"]["users"]
    ensure_indexes(collection)
    return collection


def link(users, discord_id, github_id, github_username, starred=True):
    return link_account(
        users,
        discord_id=discord_id,
        discord_username=f"user{discord_id}",
        github_id=github_id,
        github_username=github_username,
        linked_repo=REPO,
        starred_repo=starred,
    )


def test_indexes_are_created(users):
    names = set(users.index_information())
    assert {"discord_id_unique", "github_id_unique"} <= names


def test_unique_github_id_is_enforced_by_the_database(users):
    link(users, "1", 100, "Alice")
    with pytest.raises(DuplicateKeyError):
        users.insert_one({"discord_id": "9", "github_id": 100})


def test_legacy_rows_lose_their_tokens_but_keep_their_link(users):
    users.insert_one({
        "discord_username": "old",
        "discord_id": "1",
        "github_username": "OldUser",
        "github_email": "old@example.com",
        "linked_repo": REPO,
        "starred_repo": True,
        "github_token": {"access_token": "gho_leaked", "scope": "repo"},
    })

    assert purge_legacy_secrets(users) == 1

    row = find_link(users, "1")
    assert "github_token" not in row
    assert "github_email" not in row
    assert row["github_username"] == "OldUser"
    assert row["starred_repo"] is True


def test_partial_index_tolerates_legacy_rows_without_a_github_id(users):
    # Several old rows have no github_id at all; a plain unique index would
    # treat them as duplicate nulls and reject the second one.
    users.insert_one({"discord_id": "1", "github_username": "one"})
    users.insert_one({"discord_id": "2", "github_username": "two"})
    assert len(all_links(users)) == 2


def test_relink_updates_in_place(users):
    link(users, "1", 100, "Alice")
    link(users, "1", 100, "Alice-Renamed")
    assert users.count_documents({}) == 1
    assert find_link(users, "1")["github_username"] == "Alice-Renamed"


def test_one_github_account_cannot_serve_two_discord_users(users):
    link(users, "1", 100, "Alice")
    with pytest.raises(AccountAlreadyLinkedError):
        link(users, "2", 100, "Alice")
    assert users.count_documents({}) == 1
    assert find_link(users, "1") is not None


def test_set_starred_round_trip(users):
    link(users, "1", 100, "Alice", starred=True)
    set_starred(users, "1", False)
    assert find_link(users, "1")["starred_repo"] is False
