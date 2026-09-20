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
    clear_role_sync_pending_by_id,
    deliveries_for,
    ensure_delivery_indexes,
    ensure_indexes,
    find_link,
    find_link_by_github_id,
    iter_pending_role_syncs,
    link_account,
    purge_legacy_secrets,
    read_datetime,
    read_updated_at,
    record_star_event,
    release_delivery,
    set_starred,
    star_event_is_newer,
    upgrade_documents,
)

mongomock = pytest.importorskip("mongomock")

REPO = "https://github.com/owner/repo/"
WHEN = datetime(2026, 1, 1, tzinfo=UTC)

# When the sweep's stargazer listing was taken. Every write the sweep makes
# is conditional on no webhook having spoken since, so a test that is not
# about that race passes an instant later than any star event it set up.
SWEPT_AT = datetime(2026, 6, 1, tzinfo=UTC)

# When /authorize's OAuth star check was answered; the horizon link_account
# orders its star state from.
OBSERVED_AT = datetime(2026, 3, 1, tzinfo=UTC)


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


def link(users, discord_id, github_id, github_username, starred=True, observed_at=None):
    return link_account(
        users,
        discord_id=discord_id,
        discord_username=f"user{discord_id}",
        github_id=github_id,
        github_username=github_username,
        linked_repo=REPO,
        starred_repo=starred,
        observed_at=observed_at,
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


def test_a_legacy_row_without_an_id_reserves_its_github_account(users):
    # The partial index above lets these rows exist, and a lookup by
    # github_id cannot see them, so the uniqueness rule has to reach them
    # by the only handle they carry. Otherwise the same GitHub account
    # links a second time and one star earns the role twice.
    users.insert_one(
        {"discord_id": "1", "github_username": "Alice", "github_username_lower": "alice"}
    )

    with pytest.raises(AccountAlreadyLinkedError):
        link(users, "2", 100, "Alice")

    assert users.count_documents({}) == 1


def test_the_owner_of_a_legacy_row_re_verifies_into_it(users):
    users.insert_one(
        {"discord_id": "1", "github_username": "Alice", "github_username_lower": "alice"}
    )

    link(users, "1", 100, "Alice")

    assert users.count_documents({}) == 1
    assert find_link(users, "1")["github_id"] == 100


@pytest.mark.parametrize(
    "orphan",
    [{}, {"discord_id": None}, {"discord_id": ""}],
    ids=["missing", "null", "empty"],
)
def test_a_legacy_row_that_names_no_discord_user_does_not_block_a_link(users, orphan):
    # A row nothing can hold a role for is not a competing claim on the
    # star, so the name match alone must not refuse its rightful owner.
    # Null is the shape released code actually wrote.
    users.insert_one({**orphan, "github_username": "Alice", "github_username_lower": "alice"})

    link(users, "1", 100, "Alice")

    assert find_link(users, "1")["github_id"] == 100
    assert users.count_documents({}) == 2


def test_a_login_a_renamed_account_gave_up_can_still_be_linked(users):
    # The row that records the old spelling has a github_id, so the
    # immutable field has already ruled it out and the name must not put
    # it back in. The index on github_username_lower is not unique for
    # this reason among others.
    link(users, "1", 999, "Alice")

    link(users, "2", 100, "Alice")

    assert users.count_documents({}) == 2
    assert find_link(users, "2")["github_id"] == 100


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


def test_the_pending_queue_returns_only_the_flagged_rows_with_their_id(users):
    link(users, "1", 100, "Alice", starred=False)
    link(users, "2", 200, "Bob", starred=False)
    record_star_event(users, 200, True, STAR_SOURCE_WEBHOOK, WHEN)

    pending = list(iter_pending_role_syncs(users))

    assert [row["discord_id"] for row in pending] == ["2"]
    # The one identity every row has, and for some of them the only one.
    assert all("_id" in row for row in pending)


@pytest.mark.parametrize("orphan", [{}, {"discord_id": None}], ids=["missing", "null"])
def test_a_queued_row_with_no_discord_id_is_cleared_by_its_mongo_id(users, orphan):
    # Null is the shape production holds: released servers wrote an
    # unvalidated Discord ID straight through for nearly three years, so
    # a /login without an id produced one of these. Against the real
    # filter semantics, because a query for null matches a missing field
    # too and the stub cannot show that.
    users.insert_one({**orphan, "starred_repo": True, "role_sync_pending": True})
    queued = list(iter_pending_role_syncs(users))
    assert not queued[0].get("discord_id")

    # What the drain can reach with the Discord-keyed clear: nothing, and
    # the answer says so rather than reading like a clear that landed.
    assert clear_role_sync_pending(users, queued[0].get("discord_id"), True) is False
    assert len(list(iter_pending_role_syncs(users))) == 1

    clear_role_sync_pending_by_id(users, queued[0]["_id"])

    assert not list(iter_pending_role_syncs(users))
    assert users.count_documents({}) == 1


def test_clearing_the_flag_empties_the_queue_but_keeps_the_state(users):
    link(users, "1", 100, "Alice", starred=False)
    record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    assert clear_role_sync_pending(users, "1", True) is True

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

    # Against the real matched_count, which is what the answer is read off.
    assert clear_role_sync_pending(users, "1", True) is False

    assert [row["discord_id"] for row in iter_pending_role_syncs(users)] == ["1"]
    # And the clear that matches the current state does land.
    assert clear_role_sync_pending(users, "1", False) is True
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


def test_a_star_event_inside_the_listings_millisecond_is_not_written_over(users):
    # The precision the stub cannot show. A BSON datetime is a whole
    # number of milliseconds, so a webhook stamped four hundred
    # microseconds after the listing was taken comes back looking earlier
    # than it. Comparing that against the sweep's own microseconds read
    # the event as older, and the sweep then took the role and wrote its
    # stale state over the newer one, which is the lost update the guard
    # exists to prevent.
    link(users, "1", 100, "Alice", starred=False)
    observed_at = SWEPT_AT + timedelta(microseconds=500)
    record_star_event(
        users, 100, True, STAR_SOURCE_WEBHOOK, observed_at + timedelta(microseconds=400)
    )

    row = find_link(users, "1")
    # What the database kept of an event that happened after the listing.
    assert read_datetime(row, "star_event_at") < observed_at
    assert star_event_is_newer(row, observed_at) is True
    assert set_starred(users, "1", False, observed_at) is False
    assert find_link(users, "1")["starred_repo"] is True

    # And a listing taken a whole millisecond later is unambiguously the
    # newer authority again, so the sweep writes as it always did.
    assert set_starred(users, "1", False, observed_at + timedelta(milliseconds=2)) is True
    assert find_link(users, "1")["starred_repo"] is False


def test_a_relink_does_not_write_over_a_star_event_it_did_not_see(users):
    # The same race as in the stub, against the real filter semantics. The
    # OAuth star check said this person stars the repository; they un-star
    # before the answer is written down, and the webhook records it and
    # queues the role change. The relink must not restore the state the
    # event replaced, because it leaves the flag raised and the drain then
    # reconciles the role to whatever the row says.
    link(users, "1", 100, "Alice", starred=True)
    unstarred_at = OBSERVED_AT + timedelta(seconds=1)
    record_star_event(users, 100, False, STAR_SOURCE_WEBHOOK, unstarred_at)

    document = link(users, "1", 100, "Alice-Renamed", starred=True, observed_at=OBSERVED_AT)

    row = find_link(users, "1")
    assert row["starred_repo"] is False
    assert row["star_source"] == STAR_SOURCE_WEBHOOK
    assert [queued["discord_id"] for queued in iter_pending_role_syncs(users)] == ["1"]
    # The identity the flow did establish is written all the same.
    assert row["github_username"] == "Alice-Renamed"
    assert document["starred_repo"] is False


def test_a_relink_writes_over_a_star_event_older_than_the_oauth_check(users):
    # A stale event ages out of the way, so re-verifying still repairs a
    # row whose webhook was never delivered.
    link(users, "1", 100, "Alice", starred=True)
    record_star_event(users, 100, False, STAR_SOURCE_WEBHOOK, OBSERVED_AT - timedelta(days=1))

    document = link(users, "1", 100, "Alice", starred=True, observed_at=OBSERVED_AT)

    assert document["starred_repo"] is True
    assert find_link(users, "1")["starred_repo"] is True


def test_a_star_event_inside_the_oauth_checks_millisecond_is_not_written_over(users):
    # The precision the stub cannot show, on link_account's side of it. A
    # BSON datetime is whole milliseconds, so an event stamped four hundred
    # microseconds after GitHub answered comes back looking earlier than
    # the answer. Comparing the stored value against the caller's own
    # microseconds reads the un-star as older and writes the stale star
    # state over it.
    link(users, "1", 100, "Alice", starred=True)
    observed_at = OBSERVED_AT + timedelta(microseconds=500)
    record_star_event(
        users, 100, False, STAR_SOURCE_WEBHOOK, observed_at + timedelta(microseconds=400)
    )

    assert read_datetime(find_link(users, "1"), "star_event_at") < observed_at

    superseded = link(users, "1", 100, "Alice", starred=True, observed_at=observed_at)

    assert superseded["starred_repo"] is False
    assert find_link(users, "1")["starred_repo"] is False

    # A check answered a whole millisecond later is unambiguously the newer
    # authority, and writes as it always did.
    later = observed_at + timedelta(milliseconds=2)
    written = link(users, "1", 100, "Alice", starred=True, observed_at=later)

    assert written["starred_repo"] is True
    assert find_link(users, "1")["starred_repo"] is True


def test_a_first_link_is_created_however_old_the_observation_is(users):
    # The upsert is unconditional for a reason: there is no row for an
    # ordering condition to be satisfied by, so putting one on the insert
    # would drop a first-time link on the floor. Against the real upsert,
    # which builds the new document out of the filter.
    document = link(users, "1", 100, "Alice", observed_at=datetime(2020, 1, 1, tzinfo=UTC))

    row = find_link(users, "1")
    assert row["starred_repo"] is True
    assert row["github_id"] == 100
    assert document["starred_repo"] is True


def test_a_relink_leaves_the_queue_to_the_bot_even_when_it_writes(users):
    # link_account still writes no role_sync_pending of its own, which is
    # the half of this that was already fixed. The star write lands here,
    # and the flag the webhook raised is still the bot's to lower.
    link(users, "1", 100, "Alice", starred=False)
    record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, OBSERVED_AT - timedelta(days=1))

    link(users, "1", 100, "Alice", starred=False, observed_at=OBSERVED_AT)

    assert find_link(users, "1")["starred_repo"] is False
    assert [queued["discord_id"] for queued in iter_pending_role_syncs(users)] == ["1"]


def test_a_delivery_that_arrives_behind_a_newer_one_is_not_recorded(users):
    # Against the real filter semantics: the ordering condition is an $or
    # of $exists and $lte over the same field the write sets, and getting
    # it wrong lets the older of two racing deliveries win.
    link(users, "1", 100, "Alice", starred=False)
    later = WHEN + timedelta(seconds=1)
    record_star_event(users, 100, False, STAR_SOURCE_WEBHOOK, later)

    outcome = record_star_event(users, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    assert outcome["starred_repo"] is False
    assert read_datetime(outcome, "star_event_at") == later
    assert not list(iter_pending_role_syncs(users))
    assert find_link(users, "1")["starred_repo"] is False


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


def test_a_released_delivery_is_claimed_again_by_a_redelivery(deliveries):
    # The claim is taken before anything is recorded, so a delivery whose
    # recording failed is claimed and unrecorded at the same time. The row
    # has to go, or the manual redelivery an operator is told to use is
    # answered with "already handled" for an event nothing acted on.
    assert claim_delivery(deliveries, "delivery-1", now()) is True

    release_delivery(deliveries, "delivery-1")

    assert deliveries.count_documents({"delivery_id": "delivery-1"}) == 0
    assert claim_delivery(deliveries, "delivery-1", now()) is True


def test_releasing_a_delivery_nobody_claimed_is_quiet(deliveries):
    # The caller releases on a failure path and cannot always know which
    # side of the claim it failed on.
    release_delivery(deliveries, "never-seen")
    assert deliveries.count_documents({}) == 0


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
