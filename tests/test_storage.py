"""Tests for the linking rules, against an in-memory stand-in for MongoDB."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument
#
# Over pylint's default module length, and deliberately so. This file is
# where the lost-update class this project keeps rediscovering is pinned
# down, one interleaving per test with the comment that says which release
# shipped it, and every one of them is driven through the same fakes at the
# top. Splitting it would put a race in one module and the collection that
# reproduces it in another, which is how one of these regressions came back
# the first time.
# pylint: disable=too-many-lines

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
    clear_role_sync_pending_by_id,
    connect,
    find_link,
    find_link_by_github_id,
    iter_links,
    iter_pending_role_syncs,
    link_account,
    purge_legacy_secrets,
    read_updated_at,
    record_star_event,
    release_delivery,
    set_starred,
    star_event_is_newer,
)

REPO = "https://github.com/owner/repo/"
WHEN = datetime(2026, 1, 1, tzinfo=UTC)

# When the sweep's stargazer listing was taken. Every write the sweep makes
# is conditional on no webhook having spoken since, so a test that is not
# about that race passes an instant later than any star event it set up.
SWEPT_AT = datetime(2026, 6, 1, tzinfo=UTC)

# When /authorize's OAuth star check was answered. link_account orders its
# star state against the webhook from this instant exactly as the sweep does
# from the one above.
OBSERVED_AT = datetime(2026, 3, 1, tzinfo=UTC)


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
            # Only on this path, which is the whole point of the operator:
            # a link that already exists keeps the star state the row
            # holds rather than the one the caller arrived with.
            new.update(update.get("$setOnInsert", {}))
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


def link(collection, discord_id, github_id, github_username, starred=True, observed_at=None):
    return link_account(
        collection,
        discord_id=discord_id,
        discord_username=f"user{discord_id}",
        github_id=github_id,
        github_username=github_username,
        linked_repo=REPO,
        starred_repo=starred,
        observed_at=observed_at,
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


def test_a_legacy_row_with_no_id_still_reserves_its_github_account():
    # Rows written before github_id was recorded cannot be found by it,
    # and one of them is still a link and still holds the role. Without a
    # second lookup the same GitHub account authenticates for another
    # Discord ID and gets a row of its own, so one star earns the role
    # twice for as long as the original member does not re-verify.
    collection = FakeCollection(
        [{"discord_id": "1", "github_username": "Octocat", "github_username_lower": "octocat"}]
    )

    with pytest.raises(AccountAlreadyLinkedError) as excinfo:
        link(collection, "2", 100, "Octocat")

    assert excinfo.value.existing_discord_id == "1"
    assert len(collection.documents) == 1


def test_the_owner_of_a_legacy_row_re_verifies_into_it():
    # The same lookup must not lock somebody out of their own row, which
    # is how a legacy row gets its id and stops being ambiguous at all.
    collection = FakeCollection(
        [{"discord_id": "1", "github_username": "Octocat", "github_username_lower": "octocat"}]
    )

    link(collection, "1", 100, "Octocat")

    assert len(collection.documents) == 1
    assert find_link(collection, "1")["github_id"] == 100


@pytest.mark.parametrize(
    "orphan",
    # Null is the shape production actually holds: every server from
    # 2023-10-28 until the rebuild read the Discord ID off the query
    # string unvalidated and wrote it, so a bare GET to /login with no
    # id, followed by OAuth, stored one of these. Missing is the
    # one-day schema before that field existed at all.
    [{}, {"discord_id": None}, {"discord_id": ""}],
    ids=["missing", "null", "empty"],
)
def test_a_legacy_row_that_names_no_discord_user_does_not_block_a_link(orphan):
    # The other half of the lookup above, and the reason it checks the
    # Discord ID rather than trusting the name match. Nothing can hold a
    # role for a row that names no Discord user, whichever of the three
    # shapes it is, so it is not a competing claim on the star. Blocking
    # would
    # hold the GitHub account hostage to a row that redeems nothing, and
    # tell its rightful owner it is already linked to Discord ID None.
    collection = FakeCollection(
        [{**orphan, "github_username": "Octocat", "github_username_lower": "octocat"}]
    )

    document = link(collection, "1", 100, "Octocat")

    assert document["discord_id"] == "1"
    assert find_link(collection, "1")["github_id"] == 100
    # The orphan is left where it was. Nothing here can say whose it was.
    assert len(collection.documents) == 2


def test_a_login_its_previous_owner_gave_up_is_not_refused():
    # The false rejection the lookup has to avoid. A login is not
    # immutable: renaming a GitHub account leaves the old one free for
    # somebody else to register. The row that still records that spelling
    # has a github_id, so it has already been ruled out on the one field
    # that cannot change, and matching it by name would refuse the new
    # owner a link they are entitled to.
    collection = FakeCollection()
    link(collection, "1", 999, "Octocat")

    link(collection, "2", 100, "Octocat")

    assert len(collection.documents) == 2
    assert find_link(collection, "2")["github_id"] == 100


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


def test_a_delivery_that_arrives_behind_a_newer_one_is_not_recorded():
    # Deliveries are not ordered, and GitHub retries the ones whose
    # response it did not see, so an older event can reach this after a
    # newer one has already been recorded. It reports the row as it
    # stands rather than winding it back.
    collection = FakeCollection()
    link(collection, "1", 100, "Octocat", starred=False)
    later = WHEN + timedelta(seconds=1)
    record_star_event(collection, 100, False, STAR_SOURCE_WEBHOOK, later)

    outcome = record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    assert outcome["starred_repo"] is False
    assert outcome["star_event_at"] == later
    assert not list(iter_pending_role_syncs(collection))


def test_an_overtaken_delivery_does_not_write_its_older_state():
    # The interleaving itself, driven rather than hoped for. From a row
    # that says un-starred, the star delivery is the older event and the
    # un-star the newer one, and the star's write lands last. Deciding
    # from a read taken before the un-star ran left the row saying
    # starred with work queued, and the bot then handed out the role for
    # a star that had already been taken back.
    collection = InterleavingCollection()
    link(collection, "1", 100, "Octocat", starred=False)
    later = WHEN + timedelta(seconds=1)
    collection.before_first_write(
        lambda: record_star_event(collection, 100, False, STAR_SOURCE_WEBHOOK, later)
    )

    outcome = record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    assert outcome["starred_repo"] is False
    row = find_link(collection, "1")
    assert row["starred_repo"] is False
    assert row["star_event_at"] == later
    assert not list(iter_pending_role_syncs(collection))


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


@pytest.mark.parametrize(
    "orphan",
    # The two shapes that are genuinely stuck. Null is the one released
    # code produced for nearly three years, from an unvalidated Discord
    # ID written straight through; missing is the schema of the day
    # before that field existed. The empty string is not here on
    # purpose: see the test below for why it proves nothing.
    [{}, {"discord_id": None}],
    ids=["missing", "null"],
)
def test_a_row_with_no_discord_id_is_cleared_by_its_mongo_id(orphan):
    collection = FakeCollection(
        [{**orphan, "_id": "row-1", "starred_repo": True, "role_sync_pending": True}]
    )
    queued = next(iter_pending_role_syncs(collection))

    # What the drain can reach with the Discord-keyed clear: nothing. It
    # stringifies what it is handed, so the filter asks for the literal
    # "None" and the row is read and reported again on every poll for as
    # long as the process runs. It says so, at least: the refusal is the
    # answer, and a caller that reads it knows not to act as though the
    # row had come off the queue.
    assert clear_role_sync_pending(collection, queued.get("discord_id"), True) is False
    assert [row["_id"] for row in iter_pending_role_syncs(collection)] == ["row-1"]

    clear_role_sync_pending_by_id(collection, queued["_id"])

    assert not list(iter_pending_role_syncs(collection))


def test_an_empty_discord_id_was_never_the_row_that_got_stuck():
    # Why the case above excludes it, written down rather than left to
    # be rediscovered. An empty string round-trips through str(), so the
    # Discord-keyed clear matches and the row comes off the queue on its
    # own. A regression test built on this shape passes without the by-id
    # clear existing at all, which is how the real bug survived one.
    collection = FakeCollection(
        [{"_id": "row-1", "discord_id": "", "starred_repo": True, "role_sync_pending": True}]
    )

    assert clear_role_sync_pending(collection, "", True) is True

    assert not list(iter_pending_role_syncs(collection))


def test_clearing_by_id_does_not_need_the_star_state_to_match():
    # The guard the Discord-keyed clear carries is against a webhook
    # moving the row while the bot talked to Discord. Nothing talked to
    # Discord about a member that does not exist, so here it would only
    # be a second way to match nothing.
    collection = FakeCollection([{"_id": "row-1", "role_sync_pending": True}])

    clear_role_sync_pending_by_id(collection, "row-1")

    assert not list(iter_pending_role_syncs(collection))


def test_clearing_the_flag_takes_the_row_off_the_queue():
    collection = FakeCollection()
    link(collection, "1", 100, "one", starred=False)
    record_star_event(collection, 100, True, STAR_SOURCE_WEBHOOK, WHEN)

    assert clear_role_sync_pending(collection, 1, True) is True

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

    # The bot acted on the star it read, which is no longer what the row
    # says. The refusal is reported, because the caller cannot see the
    # filter and a silent no-op reads exactly like a clear that landed.
    assert clear_role_sync_pending(collection, "1", True) is False

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


def test_a_relink_does_not_write_over_a_star_event_it_did_not_see():
    # The interleaving the sixth instance of this bug class hid in, driven
    # rather than hoped for. /authorize asks GitHub, is told the member
    # stars the repository, and writes that down; the member un-stars in
    # between, and the webhook records it and queues the role change. The
    # relink used to put the stale True back while deliberately leaving
    # the flag raised, so the drain read the restored value off the row,
    # reconciled the role to it and lowered the flag, and the un-star was
    # lost until the next full sweep.
    collection = InterleavingCollection()
    link(collection, "1", 100, "Octocat", starred=True)
    unstarred_at = OBSERVED_AT + timedelta(seconds=1)
    collection.before_write(
        1, lambda: record_star_event(collection, 100, False, STAR_SOURCE_WEBHOOK, unstarred_at)
    )

    document = link(collection, "1", 100, "Octocat-Renamed", starred=True, observed_at=OBSERVED_AT)

    row = find_link(collection, "1")
    assert row["starred_repo"] is False
    assert row["star_event_at"] == unstarred_at
    # The work stays queued, and the drain now finds the state the webhook
    # recorded rather than the one the relink used to restore under it.
    assert [queued["discord_id"] for queued in iter_pending_role_syncs(collection)] == ["1"]
    # Only the star state is held back. Everything /authorize actually
    # established about this person is still written.
    assert row["github_username"] == "Octocat-Renamed"
    assert row["github_id"] == 100
    # And the caller is told what the row holds, not what it offered.
    assert document["starred_repo"] is False
    assert document["github_username"] == "Octocat-Renamed"


def test_a_row_a_relink_creates_carries_its_star_state_from_the_first_instant():
    # Why the insert sets starred_repo with $setOnInsert rather than
    # leaving it to the conditional write. The row is visible to the
    # webhook by its github_id as soon as it exists, and record_star_event
    # reads a row with no starred_repo as neither state, so an event
    # landing in between would be recorded without the role change it came
    # with ever being queued.
    collection = InterleavingCollection()
    unstarred_at = OBSERVED_AT + timedelta(seconds=1)
    collection.before_write(
        2, lambda: record_star_event(collection, 100, False, STAR_SOURCE_WEBHOOK, unstarred_at)
    )

    link(collection, "1", 100, "Octocat", starred=True, observed_at=OBSERVED_AT)

    assert find_link(collection, "1")["starred_repo"] is False
    assert [queued["discord_id"] for queued in iter_pending_role_syncs(collection)] == ["1"]


def test_a_relink_writes_over_a_star_event_older_than_the_oauth_check():
    # The other side of the guard, and the reason it is a timestamp rather
    # than "has a webhook ever touched this row". A stale event ages out of
    # the way, so re-verifying still repairs a row whose webhook was never
    # delivered instead of deferring to it forever.
    collection = FakeCollection()
    link(collection, "1", 100, "Octocat", starred=True)
    record_star_event(collection, 100, False, STAR_SOURCE_WEBHOOK, OBSERVED_AT - timedelta(days=1))

    document = link(collection, "1", 100, "Octocat", starred=True, observed_at=OBSERVED_AT)

    assert document["starred_repo"] is True
    assert find_link(collection, "1")["starred_repo"] is True
    # The flag is left exactly as it was: only the bot ever lowers it.
    assert [queued["discord_id"] for queued in iter_pending_role_syncs(collection)] == ["1"]


def test_a_first_link_is_created_however_old_the_observation_is():
    # The ordering condition must not reach the insert. There is no row to
    # be newer than one, so a condition on the upsert would turn a
    # first-time link into a silent no-op, or upsert the condition itself
    # into the document.
    collection = FakeCollection()

    document = link(collection, "1", 100, "Octocat", observed_at=datetime(2020, 1, 1, tzinfo=UTC))

    row = find_link(collection, "1")
    assert row["starred_repo"] is True
    assert document["starred_repo"] is True
    assert "$or" not in row


@pytest.mark.parametrize(
    "stored,expected",
    [
        (None, False),
        (SWEPT_AT - timedelta(seconds=1), False),
        # The last stored instant that can only have happened before the
        # listing was taken. Anything after this shares a millisecond with
        # it, which the database cannot tell apart from after it.
        (SWEPT_AT - timedelta(milliseconds=1), False),
        (SWEPT_AT, True),
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


def test_a_star_event_in_the_listings_own_millisecond_counts_as_newer():
    # BSON holds a datetime as whole milliseconds, so a star event stamped
    # a few hundred microseconds after the listing was taken is stored as
    # having happened before it. Comparing that stored value against the
    # sweep's own microseconds read it as older, and the sweep then took
    # the role and wrote its stale state over the newer one.
    observed_at = SWEPT_AT + timedelta(microseconds=500)
    # What the database kept of a webhook that landed at .000900.
    assert star_event_is_newer({"star_event_at": SWEPT_AT}, observed_at) is True
    # A whole millisecond earlier is unambiguous, and still the sweep's.
    earlier = SWEPT_AT - timedelta(milliseconds=1)
    assert star_event_is_newer({"star_event_at": earlier}, observed_at) is False


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


def test_a_released_delivery_is_new_work_again():
    # The receiver claims a delivery before it records anything, so a
    # delivery whose recording failed is claimed and unrecorded at once.
    # Leaving the claim standing turns the operator's manual redelivery,
    # which is the documented recovery, into "already handled".
    deliveries = FakeDeliveryCollection()
    assert claim_delivery(deliveries, "abc", WHEN) is True

    release_delivery(deliveries, "abc")

    assert claim_delivery(deliveries, "abc", WHEN) is True


def test_releasing_one_delivery_leaves_the_others_claimed():
    deliveries = FakeDeliveryCollection()
    claim_delivery(deliveries, "abc", WHEN)
    claim_delivery(deliveries, "def", WHEN)

    release_delivery(deliveries, "abc")

    assert claim_delivery(deliveries, "def", WHEN) is False
    assert claim_delivery(deliveries, "abc", WHEN) is True


def test_releasing_a_delivery_nobody_claimed_does_nothing():
    # The caller releases on a failure path and cannot always know which
    # side of the claim it failed on.
    deliveries = FakeDeliveryCollection()
    release_delivery(deliveries, "never-seen")
    assert deliveries.documents == []


def test_a_delivery_id_is_released_as_the_string_it_was_claimed_as():
    # Both ends normalise here rather than at the call site, so the
    # release names the row the claim wrote whatever the caller holds.
    deliveries = FakeDeliveryCollection()
    claim_delivery(deliveries, 17, WHEN)

    release_delivery(deliveries, "17")

    assert deliveries.documents == []


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


class UnpurgeableCollection(RecordingCollection):
    """A collection the credential purge is not allowed to write to."""

    def update_many(self, query, update):
        raise PyMongoError("not authorized on starguard to execute update")


class RacingCollection(FakeCollection):
    """A collection that loses the race to a concurrent link."""

    def update_one(self, query, update, upsert=False):
        raise DuplicateKeyError("github_id_unique")


class InterleavingCollection(FakeCollection):
    """A collection that lets one writer run inside another.

    Two deliveries for the same account racing each other is not a thing
    to hope a test reproduces. The hook runs once, immediately before a
    chosen write statement, which is the point the losing writer used to
    have already decided what it was going to write.

    The position is counted from the moment the hook is registered rather
    than from the start of the test, because setting the row up takes
    writes of its own. A writer that takes two statements, which
    ``link_account`` does, is reached by asking for the second one.
    """

    def __init__(self, documents=None):
        super().__init__(documents)
        self._hook = None
        self._at = 1
        self._writes = 0

    def before_first_write(self, hook):
        self.before_write(1, hook)

    def before_write(self, position, hook):
        self._at, self._hook, self._writes = position, hook, 0

    def _run_hook(self):
        # The hook writes too, and those writes count here as well; it is
        # cleared before it runs, so it cannot fire inside itself.
        self._writes += 1
        if self._hook is not None and self._writes == self._at:
            hook, self._hook = self._hook, None
            hook()

    def update_one(self, query, update, upsert=False):
        self._run_hook()
        return super().update_one(query, update, upsert)

    def find_one_and_update(self, query, update, projection=None, return_document=None):
        self._run_hook()
        return super().find_one_and_update(query, update, projection, return_document)


class FakeDeliveryCollection(FakeCollection):
    """A deliveries collection with the unique index actually enforced."""

    def insert_one(self, document):
        if any(d["delivery_id"] == document["delivery_id"] for d in self.documents):
            raise DuplicateKeyError("delivery_id_unique")
        self.documents.append(dict(document))

    def delete_one(self, query):
        for index, document in enumerate(self.documents):
            if matches(document, query):
                del self.documents[index]
                return None
        return None


class FakeDatabase(dict):
    """Enough of a pymongo database to walk to a collection by name."""

    def __missing__(self, name):
        created = self[name] = RecordingCollection()
        return created


def mongo_factory(collection, deliveries=None):
    """A client_factory that hands connect() the collections given here."""

    class FakeMongoClient:
        """Enough of a MongoClient for connect() to walk to a collection."""

        def __init__(self, host=None):
            self.host = host
            self.database_name = None
            self.database = FakeDatabase({COLLECTION_NAME: collection})
            if deliveries is not None:
                self.database[DELIVERY_COLLECTION_NAME] = deliveries

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


def legacy_row_holding_a_token():
    """One row from the version that stored OAuth tokens in clear text."""
    return {
        "_id": "row-1",
        "discord_id": "1",
        "github_username": "OldUser",
        "github_token": {"access_token": "gho_leaked", "scope": "repo"},
    }


def test_an_index_that_cannot_be_created_does_not_skip_the_purge(caplog):
    # The pairing that makes this more than bookkeeping. A collection
    # carrying rows from the version that keyed on the GitHub email holds
    # several rows per Discord account, so the unique discord_id index is
    # exactly what fails, on exactly the upgrade that also has to delete
    # that version's stored tokens. One shared try meant the index error
    # skipped the purge and the tokens stayed in the database.
    collection = RecordingCollection(
        [legacy_row_holding_a_token()],
        index_error=DuplicateKeyError("discord_id_unique"),
    )

    with caplog.at_level("WARNING", logger="common.storage"):
        connect("mongodb://db/", "starguard", mongo_factory(collection))

    assert "github_token" not in collection.documents[0]
    assert collection.documents[0]["schema_version"] == SCHEMA_VERSION
    # And the operator is told which step it was, rather than that
    # something about the collection did not work.
    assert "index the users collection" in caplog.text


def test_a_delivery_index_that_cannot_be_created_does_not_skip_the_purge(caplog):
    # The deliveries collection is new in this version, so the permission
    # to create it is the one an upgraded deployment is most likely to be
    # missing. Preparing it must not cost the users collection the
    # maintenance the upgrade is for.
    collection = RecordingCollection([legacy_row_holding_a_token()])
    deliveries = RecordingCollection(index_error=PyMongoError("not authorized"))

    with caplog.at_level("WARNING", logger="common.storage"):
        connect("mongodb://db/", "starguard", mongo_factory(collection, deliveries))

    assert "discord_id_unique" in collection.indexes
    assert "github_token" not in collection.documents[0]
    assert collection.documents[0]["schema_version"] == SCHEMA_VERSION
    assert "index the deliveries collection" in caplog.text


def test_a_purge_that_cannot_run_does_not_skip_the_upgrade(caplog):
    # The general property, rather than the two pairs above: every step
    # is attempted, whichever of them the database refuses.
    collection = UnpurgeableCollection([{"_id": "row-1", "discord_id": "1"}])

    with caplog.at_level("WARNING", logger="common.storage"):
        connect("mongodb://db/", "starguard", mongo_factory(collection))

    assert collection.documents[0]["schema_version"] == SCHEMA_VERSION
    assert "purge credentials written by older versions" in caplog.text


def test_losing_the_race_to_link_a_github_account_is_not_a_crash():
    # Two people can reach the callback at the same moment with the same
    # GitHub account; the loser is told, rather than seeing a traceback.
    with pytest.raises(AccountAlreadyLinkedError) as excinfo:
        link(RacingCollection(), "1", 100, "Octocat")

    assert excinfo.value.existing_discord_id == 100
