"""Storage tests against mongomock, which implements the real pymongo API.

The stub in test_storage.py keeps those tests readable; these confirm the same
behaviour against index enforcement, ``$unset`` and projections as MongoDB
actually implements them.
"""

# pylint: disable=missing-function-docstring

from datetime import UTC, datetime, timedelta

import pytest
from pymongo.errors import DuplicateKeyError

from common.storage import (
    DELIVERY_RETENTION_SECONDS,
    SCHEMA_VERSION,
    STAR_SOURCE_SWEEP,
    STAR_SOURCE_WEBHOOK,
    AccountAlreadyLinkedError,
    all_links,
    claim_delivery,
    clear_role_sync_pending,
    deliveries_for,
    ensure_delivery_indexes,
    ensure_indexes,
    find_link,
    find_link_by_github_id,
    iter_pending_role_syncs,
    link_account,
    purge_legacy_secrets,
    read_updated_at,
    record_star_event,
    set_starred,
    upgrade_documents,
)

mongomock = pytest.importorskip("mongomock")

REPO = "https://github.com/owner/repo/"
WHEN = datetime(2026, 1, 1, tzinfo=UTC)

# When the sweep's stargazer listing was taken. Every write the sweep makes
# is conditional on no webhook having spoken since, so a test that is not
# about that race passes an instant later than any star event it set up.
SWEPT_AT = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture(name="users")
def users_fixture():
    collection = mongomock.MongoClient()["starguard"]["users"]
    ensure_indexes(collection)
    return collection


@pytest.fixture(name="deliveries")
def deliveries_fixture(users):
    collection = deliveries_for(users)
    ensure_delivery_indexes(collection)
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


def now():
    # The expiry window is measured against the clock, so a delivery record
    # has to be written with a real timestamp rather than a fixed one.
    return datetime.now(UTC)


def test_indexes_are_created(users):
    names = set(users.index_information())
    assert {"discord_id_unique", "github_id_unique"} <= names


def test_unique_github_id_is_enforced_by_the_database(users):
    link(users, "1", 100, "Alice")
    with pytest.raises(DuplicateKeyError):
        users.insert_one({"discord_id": "9", "github_id": 100})


def test_legacy_rows_lose_their_tokens_but_keep_their_link(users):
    users.insert_one(
        {
            "discord_username": "old",
            "discord_id": "1",
            "github_username": "OldUser",
            "github_email": "old@example.com",
            "linked_repo": REPO,
            "starred_repo": True,
            "github_token": {"access_token": "gho_leaked", "scope": "repo"},
        }
    )

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
    set_starred(users, "1", False, SWEPT_AT)
    assert find_link(users, "1")["starred_repo"] is False


def test_updated_at_round_trips_through_the_database_as_a_date(users):
    link(users, "1", 100, "Alice")
    assert isinstance(find_link(users, "1")["updated_at"], datetime)


def test_updated_at_can_be_queried_as_a_date(users):
    # The reason for the change: an ISO string can only be compared lexically,
    # and only by luck.
    link(users, "1", 100, "Alice")
    horizon = datetime(2100, 1, 1, tzinfo=UTC)
    assert users.count_documents({"updated_at": {"$lt": horizon}}) == 1


def test_a_version_one_row_is_upgraded_in_place(users):
    users.insert_one(
        {
            "discord_id": "1",
            "github_username": "OldUser",
            "starred_repo": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert upgrade_documents(users) == 1

    row = find_link(users, "1")
    assert row["schema_version"] == SCHEMA_VERSION
    # The oldest rows never had the lower-cased name the star check looks up.
    assert row["github_username_lower"] == "olduser"
    assert isinstance(row["updated_at"], datetime)
    assert row["starred_repo"] is True


def test_upgrading_is_idempotent(users):
    users.insert_one({"discord_id": "1", "github_username": "OldUser"})
    assert upgrade_documents(users) == 1
    assert upgrade_documents(users) == 0


def test_current_rows_are_left_alone(users):
    link(users, "1", 100, "Alice")
    assert upgrade_documents(users) == 0


def test_an_unparseable_timestamp_does_not_block_the_upgrade(users):
    users.insert_one({"discord_id": "1", "updated_at": "whenever"})
    assert upgrade_documents(users) == 1
    row = find_link(users, "1")
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["updated_at"] == "whenever"


def test_a_row_that_only_lacks_its_version_keeps_the_date_it_has(users):
    # Half-upgraded rows exist: the date was already written as a date, so
    # the upgrade must leave it alone rather than rewrite it.
    when = datetime(2026, 1, 1, tzinfo=UTC)
    users.insert_one(
        {
            "discord_id": "1",
            "github_username": "OldUser",
            "github_username_lower": "olduser",
            "updated_at": when,
        }
    )

    assert upgrade_documents(users) == 1

    row = find_link(users, "1")
    assert row["schema_version"] == SCHEMA_VERSION
    # Read back through the reader both processes use, because the driver
    # returns a BSON date without the timezone it was given.
    assert read_updated_at(row) == when


def test_the_pending_sync_index_is_partial(users):
    # Restricted to the true case, so the bot's poll reads an empty index
    # while nothing is outstanding instead of one entry per member.
    information = users.index_information()
    assert "role_sync_pending_partial" in information
    assert information["role_sync_pending_partial"]["partialFilterExpression"] == {
        "role_sync_pending": True
    }


def test_a_webhook_star_event_round_trips_through_the_database(users):
    link(users, "1", 100, "Alice", starred=False)

    updated = record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    assert updated["discord_id"] == "1"
    # The bot polls the queue and needs the link fields, not the Mongo id.
    assert "_id" not in updated
    assert updated["starred_repo"] is True
    assert updated["star_source"] == STAR_SOURCE_WEBHOOK
    assert updated["role_sync_pending"] is True
    assert isinstance(find_link(users, "1")["star_event_at"], datetime)


def test_a_star_event_is_found_by_id_rather_than_by_login(users):
    # The person renamed their GitHub account after verifying. A lookup by
    # login would miss the row and read as "never verified".
    link(users, "1", 100, "Alice", starred=False)
    users.update_one(
        {"discord_id": "1"},
        {"$set": {"github_username": "Renamed", "github_username_lower": "renamed"}},
    )

    assert find_link_by_github_id(users, 100)["discord_id"] == "1"
    assert record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, WHEN)["starred_repo"] is True


def test_a_replayed_delivery_leaves_the_queue_empty(users):
    link(users, "1", 100, "Alice", starred=True)

    record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    assert not list(iter_pending_role_syncs(users))
    assert users.count_documents({"role_sync_pending": True}) == 0


def test_a_star_event_for_an_unknown_account_writes_nothing(users):
    assert record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, WHEN) is None
    assert users.count_documents({}) == 0


def test_the_pending_queue_returns_only_the_flagged_rows_without_their_id(users):
    link(users, "1", 100, "Alice", starred=False)
    link(users, "2", 200, "Bob", starred=False)
    record_star_event(users, 200, True, STAR_SOURCE_WEBHOOK, WHEN)

    pending = list(iter_pending_role_syncs(users))

    assert [row["discord_id"] for row in pending] == ["2"]
    assert all("_id" not in row for row in pending)


def test_clearing_the_flag_empties_the_queue_but_keeps_the_state(users):
    link(users, "1", 100, "Alice", starred=False)
    record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    clear_role_sync_pending(users, "1", True)

    assert not list(iter_pending_role_syncs(users))
    row = find_link(users, "1")
    assert row["role_sync_pending"] is False
    assert row["starred_repo"] is True
    assert row["star_source"] == STAR_SOURCE_WEBHOOK


def test_a_clear_is_refused_once_the_star_state_has_moved_on(users):
    # The bot reads a row, talks to Discord, and a webhook records the
    # opposite state in between. Clearing on the state the bot acted on
    # leaves the newer work queued instead of silently dropping it.
    link(users, "1", 100, "Alice", starred=False)
    record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, WHEN)
    record_star_event(users, 100, False, STAR_SOURCE_WEBHOOK, WHEN)

    clear_role_sync_pending(users, "1", True)

    assert [row["discord_id"] for row in iter_pending_role_syncs(users)] == ["1"]
    # And the clear that matches the current state does land.
    clear_role_sync_pending(users, "1", False)
    assert not list(iter_pending_role_syncs(users))


def test_the_sweep_records_its_own_source(users):
    link(users, "1", 100, "Alice")
    set_starred(users, "1", False, SWEPT_AT)
    row = find_link(users, "1")
    assert row["star_source"] == STAR_SOURCE_SWEEP
    # The sweep moved the role itself, so there is nothing to queue.
    assert not list(iter_pending_role_syncs(users))


def test_the_sweep_does_not_write_over_a_star_event_it_did_not_see(users):
    # The same race as in the stub, against the real filter semantics: the
    # guard is an $or of $exists and $lte, and getting either arm wrong
    # fails in a direction the stub cannot show.
    link(users, "1", 100, "Alice", starred=False)
    record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, SWEPT_AT + timedelta(seconds=30))

    assert set_starred(users, "1", False, SWEPT_AT) is False

    row = find_link(users, "1")
    assert row["starred_repo"] is True
    assert row["star_source"] == STAR_SOURCE_WEBHOOK
    assert [queued["discord_id"] for queued in iter_pending_role_syncs(users)] == ["1"]


def test_a_star_event_older_than_the_listing_is_still_swept(users):
    # The regression guard for gating on role_sync_pending instead: a flag
    # stays raised forever when the drain is off, so the sweep would skip
    # those members permanently. A timestamp ages out of the way.
    link(users, "1", 100, "Alice", starred=False)
    record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, SWEPT_AT - timedelta(days=1))

    assert set_starred(users, "1", False, SWEPT_AT) is True

    row = find_link(users, "1")
    assert row["starred_repo"] is False
    assert row["star_source"] == STAR_SOURCE_SWEEP


def test_an_ordinary_row_no_webhook_has_touched_is_written_normally(users):
    # The $exists arm. Absent is the normal state of star_event_at, and a
    # bare $lte would exclude every one of these rows and stop the sweep
    # writing anything at all.
    link(users, "1", 100, "Alice", starred=True)
    assert "star_event_at" not in find_link(users, "1")

    assert set_starred(users, "1", False, SWEPT_AT) is True
    assert find_link(users, "1")["starred_repo"] is False


def test_a_version_two_row_is_bumped_without_growing_the_new_fields(users):
    # Version 3's fields are optional, and absent is their normal state, so
    # the upgrade is a version bump and nothing else. Materialising them
    # would make every row bigger and leave the collection with two shapes
    # that every reader would then have to accept.
    users.insert_one(
        {
            "schema_version": 2,
            "discord_id": "1",
            "github_id": 100,
            "github_username": "Alice",
            "github_username_lower": "alice",
            "starred_repo": True,
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )

    assert upgrade_documents(users) == 1

    row = find_link(users, "1")
    assert row["schema_version"] == SCHEMA_VERSION
    assert "role_sync_pending" not in row
    assert "star_event_at" not in row
    assert "star_source" not in row
    # And an absent flag reads as "not queued", which is the whole point.
    assert not list(iter_pending_role_syncs(users))


def test_an_upgraded_row_matches_the_shape_a_new_link_is_written_with(users):
    # One shape, not two: whichever way a row got here, the optional
    # fields are absent until something happens to them.
    users.insert_one({"schema_version": 2, "discord_id": "1", "github_id": 100})
    upgrade_documents(users)
    link(users, "2", 200, "Bob")

    upgraded = set(find_link(users, "1"))
    fresh = set(find_link(users, "2"))
    assert not upgraded & {"role_sync_pending", "star_event_at", "star_source"}
    assert not fresh & {"role_sync_pending", "star_event_at", "star_source"}


def test_the_upgrade_does_not_drop_work_a_webhook_already_queued(users):
    # A webhook can land between the restart and this loop reaching the
    # row. The upgrade has no opinion about these fields, so the queued
    # work survives it.
    users.insert_one(
        {
            "schema_version": 2,
            "discord_id": "1",
            "github_id": 100,
            "starred_repo": True,
            "role_sync_pending": True,
            "star_event_at": WHEN,
            "star_source": STAR_SOURCE_WEBHOOK,
        }
    )

    assert upgrade_documents(users) == 1

    row = find_link(users, "1")
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["role_sync_pending"] is True
    assert row["star_source"] == STAR_SOURCE_WEBHOOK
    assert [pending["discord_id"] for pending in iter_pending_role_syncs(users)] == ["1"]


def test_the_deliveries_collection_sits_beside_the_users_one(users):
    assert deliveries_for(users).database is users.database
    assert deliveries_for(users).name == "webhook_deliveries"


def test_a_delivery_is_claimed_once_by_the_unique_index(deliveries):
    # The test and the record are one insert, so two concurrent deliveries
    # of the same id cannot both pass.
    assert claim_delivery(deliveries, "delivery-1", now()) is True
    assert claim_delivery(deliveries, "delivery-1", now()) is False
    assert deliveries.count_documents({"delivery_id": "delivery-1"}) == 1


def test_a_different_delivery_is_not_mistaken_for_a_replay(deliveries):
    assert claim_delivery(deliveries, "delivery-1", now()) is True
    assert claim_delivery(deliveries, "delivery-2", now()) is True
    assert deliveries.count_documents({}) == 2


def test_the_expiry_is_declared_on_the_field_the_record_carries(deliveries):
    ttl = deliveries.index_information()["delivery_seen_at_ttl"]
    assert ttl["expireAfterSeconds"] == DELIVERY_RETENTION_SECONDS
    assert ttl["key"] == [("seen_at", 1)]

    claim_delivery(deliveries, "delivery-1", now())
    assert isinstance(deliveries.find_one({"delivery_id": "delivery-1"})["seen_at"], datetime)


def test_a_redelivery_after_the_window_is_let_through(deliveries):
    # The window is short on purpose. GitHub reuses the delivery id when an
    # operator redelivers a failed webhook by hand, which is the documented
    # way to recover after this server was down, so remembering ids forever
    # would silently swallow the recovery.
    #
    # mongomock drops an expired row when it is read; MongoDB drops it in a
    # background pass that runs about once a minute, so on a real server the
    # same id stays claimed for a little longer than the window. Nothing
    # here depends on the removal being prompt, only on it happening, which
    # is the part both implement.
    stale = now() - timedelta(seconds=DELIVERY_RETENTION_SECONDS * 2)
    assert claim_delivery(deliveries, "delivery-1", stale) is True
    assert claim_delivery(deliveries, "delivery-1", now()) is True


def test_a_claim_inside_the_window_is_still_a_replay(deliveries):
    recent = now() - timedelta(seconds=DELIVERY_RETENTION_SECONDS // 2)
    assert claim_delivery(deliveries, "delivery-1", recent) is True
    assert claim_delivery(deliveries, "delivery-1", now()) is False
