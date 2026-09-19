"""Tests for the linking rules, against an in-memory stand-in for MongoDB."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import pytest

from common.storage import (
    AccountAlreadyLinkedError,
    all_links,
    find_link,
    link_account,
    purge_legacy_secrets,
    set_starred,
)

REPO = "https://github.com/owner/repo/"


class FakeCollection:
    """Enough of a pymongo collection for the linking rules."""

    def __init__(self, documents=None):
        self.documents = [dict(d) for d in (documents or [])]

    def find_one(self, query, projection=None):
        for document in self.documents:
            if all(document.get(k) == v for k, v in query.items()):
                return dict(document)
        return None

    def find(self, query=None, projection=None):
        results = [dict(d) for d in self.documents]
        if projection:
            dropped = {k for k, v in projection.items() if not v}
            results = [
                {k: v for k, v in d.items() if k not in dropped} for d in results
            ]
        return results

    def update_one(self, query, update, upsert=False):
        for document in self.documents:
            if all(document.get(k) == v for k, v in query.items()):
                document.update(update.get("$set", {}))
                return
        if upsert:
            new = dict(query)
            new.update(update.get("$set", {}))
            self.documents.append(new)

    def update_many(self, query, update):
        unset = update.get("$unset", {})
        modified = 0
        for document in self.documents:
            if any(field in document for field in unset):
                for field in unset:
                    document.pop(field, None)
                modified += 1
        return type("Result", (), {"modified_count": modified})()

    def create_index(self, *args, **kwargs):
        return None


def link(collection, discord_id, github_id, github_username, starred=True):
    return link_account(
        collection,
        discord_id=discord_id,
        discord_username=f"user{discord_id}",
        github_id=github_id,
        github_username=github_username,
        linked_repo=REPO,
        starred_repo=starred,
    )


def test_link_stores_the_expected_shape():
    collection = FakeCollection()
    document = link(collection, "1", 100, "Octocat")

    assert document["discord_id"] == "1"
    assert document["github_id"] == 100
    assert document["github_username"] == "Octocat"
    assert document["github_username_lower"] == "octocat"
    assert document["starred_repo"] is True
    # The access token and email are never persisted.
    assert "github_token" not in document
    assert "github_email" not in document


def test_relinking_the_same_user_updates_in_place():
    collection = FakeCollection()
    link(collection, "1", 100, "Octocat")
    link(collection, "1", 100, "Octocat-Renamed")

    assert len(collection.documents) == 1
    assert find_link(collection, "1")["github_username"] == "Octocat-Renamed"


def test_a_github_account_cannot_be_reused_by_another_discord_user():
    # Keying on email meant the second link overwrote the first user's row,
    # leaving them with the role and no record for the un-star check to find.
    collection = FakeCollection()
    link(collection, "1", 100, "Octocat")

    with pytest.raises(AccountAlreadyLinkedError) as excinfo:
        link(collection, "2", 100, "Octocat")

    assert excinfo.value.existing_discord_id == "1"
    assert len(collection.documents) == 1
    assert find_link(collection, "1")["discord_id"] == "1"


def test_different_github_accounts_coexist():
    collection = FakeCollection()
    link(collection, "1", 100, "one")
    link(collection, "2", 200, "two")
    assert len(all_links(collection)) == 2


def test_discord_ids_are_normalised_to_strings():
    collection = FakeCollection()
    link(collection, 1, 100, "one")
    assert find_link(collection, "1") is not None
    assert find_link(collection, 1) is not None


def test_set_starred_updates_the_flag():
    collection = FakeCollection()
    link(collection, "1", 100, "one", starred=True)
    set_starred(collection, "1", False)
    assert find_link(collection, "1")["starred_repo"] is False


def test_purge_removes_tokens_written_by_older_versions():
    collection = FakeCollection([
        {
            "discord_id": "1",
            "github_username": "one",
            "github_token": {"access_token": "gho_secret"},
            "github_email": "one@example.com",
        },
        {"discord_id": "2", "github_username": "two"},
    ])

    assert purge_legacy_secrets(collection) == 1
    assert "github_token" not in collection.documents[0]
    assert "github_email" not in collection.documents[0]
    assert collection.documents[0]["discord_id"] == "1"
    # Nothing else is disturbed.
    assert purge_legacy_secrets(collection) == 0


def test_all_links_drops_the_mongo_id():
    collection = FakeCollection([{"_id": "x", "discord_id": "1"}])
    assert all_links(collection) == [{"discord_id": "1"}]
