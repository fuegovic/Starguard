"""Tests for the linking rules, against an in-memory stand-in for MongoDB."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

from datetime import UTC, datetime, timedelta

import pytest
from pymongo.errors import DuplicateKeyError, PyMongoError

from common.storage import (
    COLLECTION_NAME,
    DELIVERY_COLLECTION_NAME,
    SCHEMA_VERSION,
    STAR_SOURCE_SWEEP,
    STAR_SOURCE_WEBHOOK,
    AccountAlreadyLinkedError,
    all_links,
    claim_delivery,
    clear_role_sync_pending,
    connect,
    find_link,
    find_link_by_github_id,
    iter_links,
    iter_pending_role_syncs,
    link_account,
    purge_legacy_secrets,
    read_updated_at,
    record_star_event,
    set_starred,
    star_event_is_newer,
)

REPO = "https://github.com/owner/repo/"
WHEN = datetime(2026, 1, 1, tzinfo=UTC)

# When the sweep's stargazer listing was taken. Every write the sweep makes
# is conditional on no webhook having spoken since, so a test that is not
# about that race passes an instant later than any star event it set up.
SWEPT_AT = datetime(2026, 6, 1, tzinfo=UTC)


MISSING = object()


def matches(document, query):
    """Match a document against the query syntax common.storage actually uses.

    Plain equality, plus the ``$or``/``$exists``/``$lt``/``$lte`` that the
    schema upgrade's query and the sweep's conditional write are built from.
    Anything else is a query this stub has not been taught, and silently
    matching everything would make a test pass for the wrong reason.
    """
    for key, condition in query.items():
        if key == "$or":
            if not any(matches(document, sub) for sub in condition):
                return False
            continue
        value = document.get(key, MISSING)
        if not isinstance(condition, dict):
            if value != condition:
                return False
        elif "$exists" in condition:
            if (value is not MISSING) != condition["$exists"]:
                return False
        elif "$lt" in condition:
            if value is MISSING or not value < condition["$lt"]:
                return False
        elif "$lte" in condition:
            if value is MISSING or not value <= condition["$lte"]:
                return False
        else:
            raise AssertionError(f"unsupported query: {condition!r}")
    return True


def project(document, projection):
    """Drop the fields a projection excludes."""
    if not projection:
        return dict(document)
    dropped = {k for k, v in projection.items() if not v}
    return {k: v for k, v in document.items() if k not in dropped}


class UpdateResult:
    """What pymongo hands back, reduced to the field storage reads off it."""

    def __init__(self, matched_count):
        self.matched_count = matched_count


class FakeCollection:
    """Enough of a pymongo collection for the linking rules."""

    def __init__(self, documents=None):
        self.documents = [dict(d) for d in (documents or [])]

    def find_one(self, query, projection=None):
        for document in self.documents:
            if matches(document, query):
                return project(document, projection)
        return None

    def find(self, query=None, projection=None):
        return [project(d, projection) for d in self.documents if matches(d, query or {})]

    def update_one(self, query, update, upsert=False):
        for document in self.documents:
            if matches(document, query):
                document.update(update.get("$set", {}))
                return UpdateResult(1)
        if upsert:
            new = dict(query)
            new.update(update.get("$set", {}))
            self.documents.append(new)
        # Nothing matched either way: an upsert that inserted still reports
        # a matched_count of zero, which is what pymongo does.
        return UpdateResult(0)

    # return_document is accepted and ignored: common.storage only ever asks
    # for the document as it stands after the update, which is what this
    # returns.
    def find_one_and_update(self, query, update, projection=None, return_document=None):
        for document in self.documents:
            if matches(document, query):
                document.update(update.get("$set", {}))
                return project(document, projection)
        return None

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
    set_starred(collection, "1", False, SWEPT_AT)
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
    set_starred(collection, "1", False, SWEPT_AT)
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


def test_a_link_can_be_found_by_its_github_id():
    # The webhook's lookup key: sender.id is immutable, sender.login is not.
    collection = FakeCollection()
    link(collection, "1", 100, "Octocat")
    assert find_link_by_github_id(collection, 100)["discord_id"] == "1"
    assert find_link_by_github_id(collection, "100")["discord_id"] == "1"


def test_an_unknown_github_id_has_no_link():
    assert find_link_by_github_id(FakeCollection(), 100) is None


def test_a_webhook_star_event_records_the_change_and_queues_the_bot():
    collection = FakeCollection()
    link(collection, "1", 100, "Octocat", starred=False)

    updated = record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    assert updated["starred_repo"] is True
    assert updated["star_event_at"] == WHEN
    assert updated["star_source"] == STAR_SOURCE_WEBHOOK
    assert updated["role_sync_pending"] is True
    assert isinstance(updated["updated_at"], datetime)


def test_a_replayed_webhook_does_not_queue_redundant_work():
    # A duplicate or redelivered event reports what the row already says.
    # Queueing it would have the bot re-apply a role it already applied.
    collection = FakeCollection()
    link(collection, "1", 100, "Octocat", starred=True)

    updated = record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    assert updated["starred_repo"] is True
    assert updated.get("role_sync_pending") is not True
    assert not list(iter_pending_role_syncs(collection))
    # The delivery is still recorded, so an operator can see it landed.
    assert updated["star_event_at"] == WHEN


def test_an_event_that_does_move_the_state_queues_the_bot():
    collection = FakeCollection()
    link(collection, "1", 100, "Octocat", starred=True)

    updated = record_star_event(collection, 100, False, STAR_SOURCE_WEBHOOK, WHEN)

    assert updated["starred_repo"] is False
    assert updated["role_sync_pending"] is True


def test_a_star_event_from_somebody_who_never_verified_is_silent():
    # Most star events are these: no row, nothing written, nothing said.
    collection = FakeCollection()
    assert record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, WHEN) is None
    assert collection.documents == []


def test_the_pending_queue_yields_only_the_flagged_rows():
    collection = FakeCollection()
    link(collection, "1", 100, "one", starred=False)
    link(collection, "2", 200, "two", starred=False)
    record_star_event(collection, 200, True, STAR_SOURCE_WEBHOOK, WHEN)

    assert [row["discord_id"] for row in iter_pending_role_syncs(collection)] == ["2"]


def test_the_pending_queue_streams_rather_than_building_a_list():
    # The bot awaits Discord between rows, so a burst of stars must not
    # become a list the size of the burst.
    collection = FakeCollection([{"discord_id": "1", "role_sync_pending": True}])
    rows = iter_pending_role_syncs(collection)
    assert next(rows)["discord_id"] == "1"
    with pytest.raises(StopIteration):
        next(rows)


def test_clearing_the_flag_takes_the_row_off_the_queue():
    collection = FakeCollection()
    link(collection, "1", 100, "one", starred=False)
    record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    clear_role_sync_pending(collection, 1, True)

    assert not list(iter_pending_role_syncs(collection))
    # Only the flag moves: the star state and the event it came from stay.
    assert find_link(collection, "1")["starred_repo"] is True
    assert find_link(collection, "1")["star_event_at"] == WHEN


def test_clearing_the_flag_does_not_move_updated_at():
    # updated_at says when the star state last changed, and clearing the
    # flag is bookkeeping about the role rather than news about the star.
    collection = FakeCollection()
    link(collection, "1", 100, "one", starred=False)
    record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, WHEN)
    before = find_link(collection, "1")["updated_at"]

    clear_role_sync_pending(collection, "1", True)

    assert find_link(collection, "1")["updated_at"] == before


def test_a_clear_does_not_lower_a_flag_the_bot_has_not_acted_on():
    # The window this guards: a webhook records the opposite state while
    # the bot is still talking to Discord about the previous one. An
    # unconditional clear would lower the flag that webhook just raised,
    # and the member would keep a role they should have lost.
    collection = FakeCollection()
    link(collection, "1", 100, "one", starred=False)
    record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, WHEN)
    record_star_event(collection, 100, False, STAR_SOURCE_WEBHOOK, WHEN)

    # The bot acted on the star it read, which is no longer what the row says.
    clear_role_sync_pending(collection, "1", True)

    assert [row["discord_id"] for row in iter_pending_role_syncs(collection)] == ["1"]
    assert find_link(collection, "1")["starred_repo"] is False


def test_the_sweep_stamps_itself_as_the_source():
    collection = FakeCollection()
    link(collection, "1", 100, "one")
    set_starred(collection, "1", False, SWEPT_AT)
    assert find_link(collection, "1")["star_source"] == STAR_SOURCE_SWEEP


def test_the_sweep_does_not_write_over_a_star_event_it_did_not_see():
    # The sweep fetches one listing and then spends minutes walking 45,000
    # stargazers against it. A member who stars during that walk is not in
    # the listing, so the sweep would conclude they never starred and write
    # that over the webhook's record of it. The row would then agree with
    # the sweep forever and nothing would self-correct.
    collection = FakeCollection()
    link(collection, "1", 100, "one", starred=False)
    later = SWEPT_AT + timedelta(seconds=30)
    record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, later)

    assert set_starred(collection, "1", False, SWEPT_AT) is False

    row = find_link(collection, "1")
    assert row["starred_repo"] is True
    # The attribution survives too, so an operator can still tell which
    # path last moved this row.
    assert row["star_source"] == STAR_SOURCE_WEBHOOK
    # And the work is still queued for the bot.
    assert [pending["discord_id"] for pending in iter_pending_role_syncs(collection)] == ["1"]


def test_a_star_event_older_than_the_listing_is_still_swept():
    # The regression guard for the hole in gating on role_sync_pending
    # instead. A flag says "the bot has not acted yet", which stays true
    # forever when the drain is turned off, so gating on it would make the
    # sweep skip those members permanently. A timestamp ages: once the
    # observation predates the listing, the sweep is the newer authority.
    collection = FakeCollection()
    link(collection, "1", 100, "one", starred=False)
    earlier = SWEPT_AT - timedelta(days=1)
    record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, earlier)
    # A day-old observation with the flag still up, which is what a
    # deployment running ROLE_SYNC_ENABLED=false accumulates.
    assert find_link(collection, "1")["starred_repo"] is True
    assert [queued["discord_id"] for queued in iter_pending_role_syncs(collection)] == ["1"]

    assert set_starred(collection, "1", False, SWEPT_AT) is True

    row = find_link(collection, "1")
    assert row["starred_repo"] is False
    assert row["star_source"] == STAR_SOURCE_SWEEP
    # The flag is untouched, because only the bot ever lowers it.
    assert [queued["discord_id"] for queued in iter_pending_role_syncs(collection)] == ["1"]


def test_an_ordinary_row_no_webhook_has_touched_is_written_normally():
    # The common case by far, and the one a bare $lte would break: a link
    # that no star event has ever reached has no star_event_at at all.
    collection = FakeCollection()
    link(collection, "1", 100, "one", starred=True)

    assert "star_event_at" not in find_link(collection, "1")
    assert set_starred(collection, "1", False, SWEPT_AT) is True
    assert find_link(collection, "1")["starred_repo"] is False


@pytest.mark.parametrize(
    "stored,expected",
    [
        (None, False),
        (SWEPT_AT - timedelta(seconds=1), False),
        (SWEPT_AT, False),
        (SWEPT_AT + timedelta(seconds=1), True),
        # What the driver hands back: BSON dates are UTC but arrive naive.
        # Comparing one of those against an aware instant raises, which
        # would be an exception per row rather than a wrong answer.
        (datetime(2026, 7, 1), True),  # noqa: DTZ001
    ],
)
def test_star_event_is_newer_reads_the_boundary_the_sweep_depends_on(stored, expected):
    document = {"discord_id": "1"} if stored is None else {"star_event_at": stored}
    assert star_event_is_newer(document, SWEPT_AT) is expected


def test_the_sweep_does_not_queue_a_role_sync():
    # The sweep has already moved the role itself by the time it writes.
    collection = FakeCollection()
    link(collection, "1", 100, "one")
    set_starred(collection, "1", False, SWEPT_AT)
    assert not list(iter_pending_role_syncs(collection))


def test_a_delivery_id_is_claimed_once():
    deliveries = FakeDeliveryCollection()
    assert claim_delivery(deliveries, "abc", WHEN) is True
    assert claim_delivery(deliveries, "abc", WHEN) is False


def test_different_delivery_ids_are_claimed_independently():
    deliveries = FakeDeliveryCollection()
    assert claim_delivery(deliveries, "abc", WHEN) is True
    assert claim_delivery(deliveries, "def", WHEN) is True


def test_a_delivery_id_is_stored_as_a_string():
    # Whatever the caller holds, the same delivery must not produce a
    # second row that a later claim would then miss.
    deliveries = FakeDeliveryCollection()
    assert claim_delivery(deliveries, 17, WHEN) is True
    assert deliveries.documents[0]["delivery_id"] == "17"
    assert claim_delivery(deliveries, "17", WHEN) is False


class RecordingCollection(FakeCollection):
    """A collection that remembers the indexes it was asked for."""

    def __init__(self, documents=None, index_error=None):
        super().__init__(documents)
        self.indexes = []
        self.index_options = {}
        self.index_error = index_error

    def create_index(self, *args, **kwargs):
        if self.index_error is not None:
            raise self.index_error
        self.indexes.append(kwargs.get("name"))
        self.index_options[kwargs.get("name")] = kwargs
        return kwargs.get("name")


class RacingCollection(FakeCollection):
    """A collection that loses the race to a concurrent link."""

    def update_one(self, query, update, upsert=False):
        raise DuplicateKeyError("github_id_unique")


class FakeDeliveryCollection(FakeCollection):
    """A deliveries collection with the unique index actually enforced."""

    def insert_one(self, document):
        if any(d["delivery_id"] == document["delivery_id"] for d in self.documents):
            raise DuplicateKeyError("delivery_id_unique")
        self.documents.append(dict(document))


class FakeDatabase(dict):
    """Enough of a pymongo database to walk to a collection by name."""

    def __missing__(self, name):
        created = self[name] = RecordingCollection()
        return created


def mongo_factory(collection):
    """A client_factory that hands connect() the collection given here."""

    class FakeMongoClient:
        """Enough of a MongoClient for connect() to walk to a collection."""

        def __init__(self, host=None):
            self.host = host
            self.database_name = None
            self.database = FakeDatabase({COLLECTION_NAME: collection})

        def get_database(self, name):
            self.database_name = name
            return self.database

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


def test_the_pending_sync_index_is_restricted_to_the_true_case():
    # A full index over a mostly-false boolean would hold one entry per
    # verified member; restricted to true, the bot's poll reads an empty
    # index whenever there is no outstanding work.
    collection = RecordingCollection()
    connect("mongodb://db/", "starguard", mongo_factory(collection))

    options = collection.index_options["role_sync_pending_partial"]
    assert options["partialFilterExpression"] == {"role_sync_pending": True}
    assert "unique" not in options


def test_connect_prepares_the_deliveries_collection_too():
    client, _ = connect("mongodb://db/", "starguard", mongo_factory(RecordingCollection()))

    deliveries = client.database[DELIVERY_COLLECTION_NAME]
    # The unique index is what makes claiming a delivery atomic, and the
    # TTL index is what lets a manual redelivery through ten minutes later.
    assert "delivery_id_unique" in deliveries.indexes
    assert deliveries.index_options["delivery_id_unique"]["unique"] is True
    assert deliveries.index_options["delivery_seen_at_ttl"]["expireAfterSeconds"] == 600


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
