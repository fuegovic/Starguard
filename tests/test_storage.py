"""Tests for the linking rules, against an in-memory stand-in for MongoDB."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

from datetime import UTC, datetime

import pytest
from pymongo.errors import DuplicateKeyError, PyMongoError

from common.storage import (
    COLLECTION_NAME,
    SCHEMA_VERSION,
    AccountAlreadyLinkedError,
    all_links,
    connect,
    find_link,
    iter_links,
    link_account,
    purge_legacy_secrets,
    read_updated_at,
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
            results = [{k: v for k, v in d.items() if k not in dropped} for d in results]
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
    collection = FakeCollection(
        [
            {
                "discord_id": "1",
                "github_username": "one",
                "github_token": {"access_token": "gho_secret"},
                "github_email": "one@example.com",
            },
            {"discord_id": "2", "github_username": "two"},
        ]
    )

    assert purge_legacy_secrets(collection) == 1
    assert "github_token" not in collection.documents[0]
    assert "github_email" not in collection.documents[0]
    assert collection.documents[0]["discord_id"] == "1"
    # Nothing else is disturbed.
    assert purge_legacy_secrets(collection) == 0


def test_all_links_drops_the_mongo_id():
    collection = FakeCollection([{"_id": "x", "discord_id": "1"}])
    assert all_links(collection) == [{"discord_id": "1"}]


def test_iter_links_yields_rather_than_building_a_list():
    # The star check walks this while awaiting Discord between documents, so
    # its memory must not grow with the number of verified members.
    collection = FakeCollection([{"discord_id": "1"}, {"discord_id": "2"}])
    links = iter_links(collection)
    assert next(links)["discord_id"] == "1"
    assert next(links)["discord_id"] == "2"
    with pytest.raises(StopIteration):
        next(links)


def test_a_new_link_records_its_schema_version():
    document = link(FakeCollection(), "1", 100, "Octocat")
    assert document["schema_version"] == SCHEMA_VERSION


def test_updated_at_is_stored_as_a_real_date():
    # Version 1 stored an ISO string, which the database can neither compare
    # nor index as a date.
    document = link(FakeCollection(), "1", 100, "Octocat")
    assert isinstance(document["updated_at"], datetime)
    assert document["updated_at"].tzinfo is not None


def test_set_starred_stores_a_real_date_too():
    collection = FakeCollection()
    link(collection, "1", 100, "one")
    set_starred(collection, "1", False)
    assert isinstance(find_link(collection, "1")["updated_at"], datetime)


@pytest.mark.parametrize(
    "stored,expected",
    [
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
        # What the driver hands back: BSON dates are UTC but arrive naive.
        # The naive value is the input under test, so it stays naive here.
        (datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=UTC)),  # noqa: DTZ001
        # Written by schema version 1.
        ("2026-01-01T00:00:00+00:00", datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_updated_at_is_readable_in_both_spellings(stored, expected):
    assert read_updated_at({"updated_at": stored}) == expected


@pytest.mark.parametrize("stored", [None, "", "not a date", 17])
def test_an_unreadable_updated_at_is_none(stored):
    assert read_updated_at({"updated_at": stored}) is None


class RecordingCollection(FakeCollection):
    """A collection that remembers the indexes it was asked for."""

    def __init__(self, documents=None, index_error=None):
        super().__init__(documents)
        self.indexes = []
        self.index_error = index_error

    def create_index(self, *args, **kwargs):
        if self.index_error is not None:
            raise self.index_error
        self.indexes.append(kwargs.get("name"))
        return kwargs.get("name")


class RacingCollection(FakeCollection):
    """A collection that loses the race to a concurrent link."""

    def update_one(self, query, update, upsert=False):
        raise DuplicateKeyError("github_id_unique")


def mongo_factory(collection):
    """A client_factory that hands connect() the collection given here."""

    class FakeMongoClient:
        """Enough of a MongoClient for connect() to walk to a collection."""

        def __init__(self, host=None):
            self.host = host
            self.database_name = None

        def get_database(self, name):
            self.database_name = name
            return {COLLECTION_NAME: collection}

    return FakeMongoClient


def test_connect_walks_to_the_users_collection_and_prepares_it():
    collection = RecordingCollection(
        [
            {
                "_id": "row-1",
                "discord_id": "1",
                "github_username": "OldUser",
                "github_token": {"access_token": "gho_leaked"},
            }
        ]
    )
    client, users = connect("mongodb://db:27017/", "starguard", mongo_factory(collection))

    assert users is collection
    assert client.host == "mongodb://db:27017/"
    assert client.database_name == "starguard"
    assert "discord_id_unique" in collection.indexes
    assert "github_id_unique" in collection.indexes
    # Startup is also when tokens written by older versions are removed and
    # older documents are brought forward.
    assert "github_token" not in collection.documents[0]
    assert collection.documents[0]["schema_version"] == SCHEMA_VERSION
    assert collection.documents[0]["github_username_lower"] == "olduser"


def test_a_database_that_refuses_the_preparation_still_yields_a_collection(caplog):
    # A replica that is briefly unavailable should not stop the process from
    # starting: every reader still accepts a document that was not upgraded.
    collection = RecordingCollection(index_error=PyMongoError("not primary"))

    with caplog.at_level("WARNING", logger="common.storage"):
        _, users = connect("mongodb://db/", "starguard", mongo_factory(collection))

    assert users is collection
    assert "not primary" in caplog.text


def test_losing_the_race_to_link_a_github_account_is_not_a_crash():
    # Two people can reach the callback at the same moment with the same
    # GitHub account; the loser is told, rather than seeing a traceback.
    with pytest.raises(AccountAlreadyLinkedError) as excinfo:
        link(RacingCollection(), "1", 100, "Octocat")

    assert excinfo.value.existing_discord_id == 100
