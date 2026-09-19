"""MongoDB access for the ``users`` and ``webhook_deliveries`` collections.

Kept in one place so the bot and the server cannot drift apart on the document
shape, and so the linking rules can be tested against a stub collection.

A link document looks like::

    {
        "schema_version":        3,               # see SCHEMA_VERSION below
        "discord_id":            "123456789",     # primary identity, a string
        "discord_username":      "someone",
        "github_id":             42,              # immutable GitHub account id
        "github_username":       "SomeOne",
        "github_username_lower": "someone",       # for case-insensitive lookup
        "linked_repo":           "https://github.com/owner/repo/",
        "starred_repo":          True,
        "updated_at":            datetime(2026, 1, 1, tzinfo=utc),

        # Optional; see the invariant below.
        "star_event_at":         datetime(2026, 1, 1, tzinfo=utc),
        "role_sync_pending":     False,
        "star_source":           "webhook",       # or "sweep"
    }

The last three fields exist because the GitHub webhook arrives at the server
process and only the bot process can change a Discord role. The two never talk
to each other, so this collection is the whole channel between them: the
server records what changed and raises ``role_sync_pending``, and the bot
polls for the rows that are still raised.

Those three are optional, and absent is their normal state. A document only
grows them once something has actually happened to it, and absent reads
exactly as the default for every consumer: `star_event_at` and `star_source`
read as None, and the pending index is partial on ``True``, so a row without
the field is simply not in the queue. `upgrade_documents` bumps an older
row's version without materialising them, so there is one shape rather than
two, and only `record_star_event` and `set_starred` ever write them.

`star_event_at` is load-bearing beyond bookkeeping. It is how the sweep and
the webhook are ordered against each other, since they observe the same fact
at different times and neither can see the other running. The rule is that
an observation is authoritative until something newer contradicts it: the
sweep carries the instant its stargazer listing was taken, and both skips
and refuses to write over any row a star event reached after that instant.
See `set_starred` and `star_event_is_newer`.

`link_account` in particular must never write `role_sync_pending`. It stores
the whole document with one ``$set`` and ``upsert=True``, so every key in it
overwrites on a re-link. If the flag were in that dict, this would happen: a
webhook records an un-star and raises the flag, the same person runs /verify
again and completes OAuth before the bot's next poll, and `link_account`
resets the flag to false. The bot never sees the queued work and the member
keeps a role they should have lost until the next full sweep. The field is
absent there on purpose, not by oversight.
"""

import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, Final, Literal

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger(__name__)

COLLECTION_NAME: Final = "users"

# Delivery ids seen recently, so a replayed webhook is dropped before it is
# acted on. Kept out of the users collection because the rows expire and
# nothing else joins against them.
DELIVERY_COLLECTION_NAME: Final = "webhook_deliveries"

# pymongo spells a BSON document ``dict[str, Any]``, because a document really
# can hold anything the driver can encode, and the driver's own generics are
# parameterised by that type. The aliases keep the Any in one named place
# instead of repeating it down every signature below.
#
# Readers that only look things up take ``Mapping[str, object]`` instead, which
# is strict enough to force the isinstance checks that schema version 1
# documents need.
MongoDocument = dict[str, Any]
UserCollection = Collection[MongoDocument]
DeliveryCollection = Collection[MongoDocument]

# Which path last moved `starred_repo`. Recorded so the two can be told
# apart: a row the webhook never touches while the sweep keeps correcting it
# is a webhook that is not being delivered, and that is invisible otherwise.
StarSource = Literal["webhook", "sweep"]
STAR_SOURCE_WEBHOOK: Final[StarSource] = "webhook"
STAR_SOURCE_SWEEP: Final[StarSource] = "sweep"

# 1: the original shape. `updated_at` was an ISO 8601 string, and the oldest
#    rows have neither `github_id` nor `github_username_lower`.
# 2: `updated_at` is a BSON datetime, so it can be compared, sorted and
#    indexed in the database rather than only lexically by accident.
# 3: `star_event_at`, `role_sync_pending` and `star_source`, which carry a
#    webhook-driven star change from the server process to the bot process.
#    They are optional, so this version is a bump and nothing else; see the
#    invariant in the module docstring.
#
# Every document carries the version it was written with, and connect() brings
# older ones forward at startup. Readers still accept version 1 values, so a
# rollback to the previous release does not lose anybody's link.
SCHEMA_VERSION: Final = 3

# How long a delivery id is remembered. This is deliberately short. GitHub
# reuses the same delivery id when an operator redelivers a failed webhook by
# hand, which is the documented way to recover after this server was down, so
# deduplicating forever would silently swallow exactly the recovery an
# operator is reaching for. Ten minutes drops the accidental duplicates (the
# retry of a delivery that was processed but whose response was lost) and
# still lets a deliberate redelivery minutes or hours later through.
#
# MongoDB's TTL remover is a background job that runs about once a minute, so
# a row can outlive the window briefly; nothing here depends on the deletion
# being prompt.
DELIVERY_RETENTION_SECONDS: Final = 600

# Fields earlier versions wrote that must never be stored again. The OAuth
# access token in particular was kept in clear text alongside a `repo` scope,
# which made the collection a set of credentials for every verified user's
# private repositories.
LEGACY_SECRET_FIELDS: Final[tuple[str, ...]] = ("github_token", "github_email")


class AccountAlreadyLinkedError(RuntimeError):
    """Raised when a GitHub account is already linked to another Discord user."""

    def __init__(self, existing_discord_id: object) -> None:
        super().__init__(f"GitHub account already linked to Discord ID {existing_discord_id}.")
        self.existing_discord_id = existing_discord_id


def get_collection(database: Database[MongoDocument]) -> UserCollection:
    """Return the users collection from ``database``."""
    return database[COLLECTION_NAME]


def get_delivery_collection(database: Database[MongoDocument]) -> DeliveryCollection:
    """Return the seen-deliveries collection from ``database``."""
    return database[DELIVERY_COLLECTION_NAME]


def deliveries_for(collection: UserCollection) -> DeliveryCollection:
    """Return the deliveries collection that sits beside ``collection``.

    Both processes are handed the users collection and nothing else, so this
    is how the webhook route reaches the second one without a second
    connection or a second set of configuration.
    """
    return get_delivery_collection(collection.database)


def ensure_indexes(collection: UserCollection) -> None:
    """Create the uniqueness constraints the linking rules rely on.

    ``github_id`` is indexed with a partial filter so documents written by
    older versions, which have no ``github_id``, do not all collide on null.
    """
    collection.create_index([("discord_id", ASCENDING)], unique=True, name="discord_id_unique")
    collection.create_index(
        [("github_id", ASCENDING)],
        unique=True,
        name="github_id_unique",
        partialFilterExpression={"github_id": {"$exists": True}},
    )
    collection.create_index([("github_username_lower", ASCENDING)], name="github_username_lower")
    # Partial on purpose, restricted to the true case. The bot polls this
    # queue roughly every thirty seconds and it is empty almost every time.
    # A full index over a boolean that is false for nearly every verified
    # member would hold one entry per member, all of them false, and the
    # poll would pay for the false ones on every pass. Restricted to true,
    # an idle poll reads an empty index and touches nothing, however many
    # members the collection grows to.
    collection.create_index(
        [("role_sync_pending", ASCENDING)],
        name="role_sync_pending_partial",
        partialFilterExpression={"role_sync_pending": True},
    )


def ensure_delivery_indexes(collection: DeliveryCollection) -> None:
    """Create the uniqueness constraint and the expiry the dedupe relies on.

    The unique index is what makes :func:`claim_delivery` atomic, and the TTL
    index is what keeps the collection from growing without bound and what
    reopens the window for a manual redelivery. See
    :data:`DELIVERY_RETENTION_SECONDS` for why that window is short.
    """
    collection.create_index(
        [("delivery_id", ASCENDING)],
        unique=True,
        name="delivery_id_unique",
    )
    collection.create_index(
        [("seen_at", ASCENDING)],
        name="delivery_seen_at_ttl",
        expireAfterSeconds=DELIVERY_RETENTION_SECONDS,
    )


def claim_delivery(
    collection: DeliveryCollection,
    delivery_id: object,
    seen_at: datetime,
) -> bool:
    """Remember ``delivery_id``, and say whether this is its first sighting.

    True means the caller has the delivery and should act on it; False means
    another request already claimed it and this one is a replay.

    The insert against the unique index is the whole mechanism, and it is
    what makes the test and the record one step. A read followed by a write
    would let two concurrent deliveries of the same id both find nothing and
    both proceed, which is the case that matters: GitHub retries a delivery
    whose response it did not get, and the retry can overlap the first
    attempt that is still running.
    """
    try:
        collection.insert_one({"delivery_id": str(delivery_id), "seen_at": seen_at})
    except DuplicateKeyError:
        return False
    return True


def purge_legacy_secrets(collection: UserCollection) -> int:
    """Delete credentials written by older versions. Returns rows changed.

    Upgrading the code stops new tokens being stored, but any already in the
    database stay valid until they are removed, so this runs at startup.
    """
    query = {"$or": [{field: {"$exists": True}} for field in LEGACY_SECRET_FIELDS]}
    update = {"$unset": dict.fromkeys(LEGACY_SECRET_FIELDS, "")}
    result = collection.update_many(query, update)
    modified: int = getattr(result, "modified_count", 0)
    if modified:
        log.warning(
            "Removed stored OAuth tokens/emails from %s existing user record(s). "
            "Any GitHub tokens previously issued to this app should be revoked.",
            modified,
        )
    return modified


def read_datetime(document: Mapping[str, object], field: str) -> datetime | None:
    """Return ``field`` as an aware datetime, or None when unreadable.

    Schema version 1 stored ``updated_at`` as an ISO 8601 string, so both
    spellings are accepted. Values the database hands back have no timezone
    attached, since BSON datetimes are UTC by definition, and they are
    labelled as such here rather than left naive for a caller to get wrong.
    A naive value compared against an aware one raises, which is exactly the
    mistake the sweep's freshness check would make on every row.
    """
    value = document.get(field)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def read_updated_at(document: Mapping[str, object]) -> datetime | None:
    """Return ``updated_at`` as an aware datetime, or None when unreadable."""
    return read_datetime(document, "updated_at")


def upgrade_documents(collection: UserCollection) -> int:
    """Bring documents written by older versions up to SCHEMA_VERSION.

    Returns how many were changed. The rewrite is done a document at a time
    rather than with one ``update_many``, because turning a string into a
    date needs the old value; it is a one-off cost paid at startup, against a
    collection with one row per verified member.
    """
    query = {
        "$or": [
            {"schema_version": {"$exists": False}},
            {"schema_version": {"$lt": SCHEMA_VERSION}},
        ]
    }

    upgraded = 0
    for document in collection.find(query):
        changes: dict[str, object] = {"schema_version": SCHEMA_VERSION}

        username = document.get("github_username")
        if username and not document.get("github_username_lower"):
            changes["github_username_lower"] = str(username).lower()

        if not isinstance(document.get("updated_at"), datetime):
            timestamp = read_updated_at(document)
            if timestamp is not None:
                changes["updated_at"] = timestamp

        # Version 3 is a bump and nothing else. Its three fields are
        # optional and absent reads as the default, so writing them here
        # would only make every row bigger and give the collection two
        # shapes to reason about instead of one. It also keeps this loop
        # from having any opinion about `role_sync_pending`: a webhook can
        # land between the restart and this loop reaching a given row, and
        # a default written over it would drop that queued work.
        collection.update_one({"_id": document["_id"]}, {"$set": changes})
        upgraded += 1

    if upgraded:
        log.info(
            "Upgraded %s user record(s) to schema version %s",
            upgraded,
            SCHEMA_VERSION,
        )
    return upgraded


def find_link(collection: UserCollection, discord_id: object) -> MongoDocument | None:
    """Return the link document for ``discord_id``, or None."""
    return collection.find_one({"discord_id": str(discord_id)})


def find_link_by_github_id(
    collection: UserCollection, github_id: int | str
) -> MongoDocument | None:
    """Return the link document for ``github_id``, or None.

    The webhook's lookup key. A star event carries both ``sender.id`` and
    ``sender.login``, and only the id is stable: a login can be changed by
    its owner at any time, and a lookup by login would then miss the row and
    read as "this person never verified". The id already has a unique index,
    so this is one index hit.
    """
    return collection.find_one({"github_id": int(github_id)})


def iter_links(collection: UserCollection) -> Iterator[MongoDocument]:
    """Yield every link document, without the Mongo ``_id``.

    The caller sees one document at a time straight off the cursor, so the
    star check's memory does not grow with the number of verified members.
    """
    yield from collection.find({}, {"_id": 0})


def all_links(collection: UserCollection) -> list[MongoDocument]:
    """Return every link document as a list, without the Mongo ``_id``.

    Prefer :func:`iter_links` where the documents are consumed once.
    """
    return list(iter_links(collection))


def link_account(
    collection: UserCollection,
    discord_id: object,
    discord_username: object,
    github_id: int | str,
    github_username: str,
    linked_repo: str,
    starred_repo: object,
) -> MongoDocument:
    """Create or update the link between a Discord user and a GitHub account.

    The document is keyed on ``discord_id``. Earlier versions keyed on the
    GitHub email address, so re-linking overwrote the previous user's row and
    left them holding the role with no record for the un-star check to find.

    Raises :class:`AccountAlreadyLinkedError` when the GitHub account is
    already bound to a different Discord user, which stops one star being
    redeemed for the role by several Discord accounts.
    """
    discord_id = str(discord_id)
    github_id = int(github_id)

    existing = collection.find_one({"github_id": github_id})
    if existing and str(existing.get("discord_id")) != discord_id:
        raise AccountAlreadyLinkedError(existing.get("discord_id"))

    document: MongoDocument = {
        "schema_version": SCHEMA_VERSION,
        "discord_id": discord_id,
        "discord_username": str(discord_username or ""),
        "github_id": github_id,
        "github_username": github_username,
        "github_username_lower": github_username.lower(),
        "linked_repo": linked_repo,
        "starred_repo": bool(starred_repo),
        "updated_at": datetime.now(UTC),
    }

    try:
        collection.update_one({"discord_id": discord_id}, {"$set": document}, upsert=True)
    except DuplicateKeyError as exc:
        # Lost a race against a concurrent link of the same GitHub account.
        raise AccountAlreadyLinkedError(github_id) from exc

    return document


def set_starred(
    collection: UserCollection,
    discord_id: object,
    starred: object,
    observed_at: datetime,
) -> bool:
    """Record the star state the sweep observed. True when the write landed.

    The sweep's writer. It does not touch ``role_sync_pending``: the sweep
    has already moved the role itself by the time it gets here, so queueing
    the bot to do it again would be work for nothing. ``star_source`` is
    stamped so a row the sweep keeps correcting stands out against one the
    webhook keeps up to date.

    ``observed_at`` is the instant the sweep's stargazer listing was taken,
    and the write only lands on a row no webhook has spoken about since.
    Without it this is the worst of the lost updates in this file, because
    it destroys the evidence as well as the outcome:

    1. The listing is fetched. It does not have this member in it.
    2. Walking 45,000 stargazers takes minutes. During them, the member
       stars, and the webhook writes ``starred_repo: True``.
    3. The sweep reaches the row, removes the role, and writes
       ``starred_repo: False`` over the webhook's value.

    The member has starred, holds no role, and the row now says they never
    starred, so the next sweep agrees with itself and nothing self-corrects.
    Guarding the write means step 3 lands on nothing and the row keeps
    saying what the webhook observed, which is the newer fact.

    The comparison is against ``star_event_at`` rather than
    ``role_sync_pending`` on purpose. A flag says "the bot has not acted
    yet", which stays true forever when ROLE_SYNC_ENABLED is off, so
    gating on it would make the sweep skip exactly the members whose
    deployment most needs sweeping. A timestamp ages instead: once an
    observation is older than the current listing, the sweep is the newer
    authority again and this write lands as it always did.
    """
    result = collection.update_one(
        _not_newer_than(observed_at, discord_id=str(discord_id)),
        {
            "$set": {
                "starred_repo": bool(starred),
                "star_source": STAR_SOURCE_SWEEP,
                "updated_at": datetime.now(UTC),
            }
        },
    )
    matched: int = getattr(result, "matched_count", 0)
    return matched > 0


def _not_newer_than(observed_at: datetime, **keys: object) -> MongoDocument:
    """Build a filter for rows no webhook has touched since ``observed_at``.

    The ``$exists`` arm is what keeps every ordinary row matching: a link
    that no star event has ever reached has no ``star_event_at`` at all,
    which is its normal state, and a bare ``$lte`` would exclude all of
    them and stop the sweep writing anything.
    """
    return {
        **keys,
        "$or": [
            {"star_event_at": {"$exists": False}},
            {"star_event_at": {"$lte": observed_at}},
        ],
    }


def star_event_is_newer(document: Mapping[str, object], observed_at: datetime) -> bool:
    """Whether a webhook has spoken about ``document`` since ``observed_at``.

    The read-side half of the guard in :func:`set_starred`. The sweep checks
    this before it acts at all, so a member a webhook has newer information
    about keeps their role rather than having it taken and then restored;
    the guard on the write closes the microseconds between this check and
    the write landing.
    """
    star_event_at = read_datetime(document, "star_event_at")
    return star_event_at is not None and star_event_at > observed_at


def record_star_event(
    collection: UserCollection,
    github_id: int | str,
    starred: bool,
    source: StarSource,
    occurred_at: datetime,
) -> MongoDocument | None:
    """Record a star change that arrived out of band. Returns the new row.

    Returns None when no link exists for ``github_id``, which is the common
    case and not an error: a star event fires for everybody who stars the
    repository, and most of them have never verified with the bot. That path
    costs one indexed lookup and says nothing, because logging a line per
    star would turn a popular repository into a log flood.

    ``role_sync_pending`` is raised only when the star state actually moved.
    GitHub retries a delivery whose response it did not see, and an operator
    can redeliver one by hand, so the same event does arrive twice; a replay
    that reports what the row already says is not work for the bot, and
    queueing it would have the bot re-apply a role it has already applied.

    The read and the write are two operations rather than one, so two
    deliveries racing here can both conclude the state moved. That is the
    harmless direction: the flag is raised once too often and the bot
    reconciles a role that is already correct. It cannot go the other way,
    because the flag is only ever lowered by the bot after it has acted.
    """
    key = {"github_id": int(github_id)}
    document = collection.find_one(key)
    if document is None:
        return None

    changes: MongoDocument = {
        "starred_repo": starred,
        "star_event_at": occurred_at,
        "star_source": source,
        "updated_at": datetime.now(UTC),
    }
    if bool(document.get("starred_repo")) != starred:
        changes["role_sync_pending"] = True

    # Keyed on the unique github_id again, which is the same single index
    # hit as the read above and keeps the two statements talking about the
    # same row in the same terms.
    return collection.find_one_and_update(
        key,
        {"$set": changes},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )


def iter_pending_role_syncs(collection: UserCollection) -> Iterator[MongoDocument]:
    """Yield the links waiting for the bot to move a role, without ``_id``.

    Streamed off the cursor for the same reason as :func:`iter_links`: the
    bot awaits Discord between documents, so a burst of stars must not
    become a list the size of the burst. The query is exactly the partial
    index's filter, so a poll that finds nothing reads nothing.

    The consumer clears each flag as it goes, which modifies the very field
    this cursor walks. That is safe here, and it is worth saying why rather
    than leaving the next reader to wonder. Clearing removes the row's entry
    from the partial index rather than moving it, and the consumer only ever
    clears a row this cursor has already yielded, so the removals are always
    behind the cursor and nothing is skipped or seen twice because of them.

    A webhook raising a flag while the cursor is open lands a new entry that
    may fall on either side of the current position. Either outcome is
    correct: the row is picked up in this pass or in the next one, because
    the flag stays raised until the bot itself lowers it. The queue is
    eventually consistent by design, and the poll runs every thirty seconds.
    """
    yield from collection.find({"role_sync_pending": True}, {"_id": 0})


def queue_role_sync(collection: UserCollection, discord_id: object) -> None:
    """Hand ``discord_id`` to the drain, for the sweep to admit it was stale.

    The sweep's only reason to raise this flag, and it raises it exactly
    when :func:`set_starred` refuses its write. A refusal proves the row
    carries a star event newer than the listing this cycle acted on, which
    means the role change already committed to Discord was made on stale
    information. The sweep does not need to know what changed, only to hand
    the row to the thing that reconciles rows.

    This looks like a contradiction against :func:`record_star_event`,
    which sees that same event and declines to queue it, so it is worth
    being plain about the difference. That path declines because the star
    state in the row did not move, and a member who re-stars what the row
    already says they star needs no role change. Here the state still did
    not move, but Discord did, and the sweep is the only thing that knows
    it. The refusal is the signal; the event on its own was not one.

    It also belongs here rather than in ``record_star_event`` because that
    path runs for every star the repository receives. At 45,000 of them a
    queue entry each is the cost webhooks exist to remove, whereas this
    runs only when a write was actually refused.

    ``updated_at`` is left alone for the same reason as in
    :func:`clear_role_sync_pending`: this is bookkeeping about the role,
    not news about the star.
    """
    collection.update_one(
        {"discord_id": str(discord_id)},
        {"$set": {"role_sync_pending": True}},
    )


def clear_role_sync_pending(
    collection: UserCollection,
    discord_id: object,
    starred: bool,
) -> None:
    """Take ``discord_id`` off the pending queue, once the bot has acted.

    ``starred`` is the star state the bot just acted on, and the clear only
    lands while the row still says that. Without it this is a lost update: a
    webhook can record the opposite state in the window between the bot
    reading the row and finishing with Discord, and an unconditional clear
    would lower the flag that webhook had just raised. The member would keep
    a role they should have lost until the next full sweep, which is the
    same failure `link_account` avoids by never writing this field at all.
    When the state has moved on, the flag stays up and the next poll, thirty
    seconds later, acts on the newer value.

    Every queued row has ``starred_repo`` set, because the only writer that
    raises the flag sets both in one update, so the guard cannot miss a row
    by matching a field that is not there.

    ``updated_at`` is left alone on purpose: it says when the star state
    last changed, and lowering this flag is bookkeeping about the role, not
    news about the star.
    """
    collection.update_one(
        {"discord_id": str(discord_id), "starred_repo": starred},
        {"$set": {"role_sync_pending": False}},
    )


def connect(
    mongo_host: str,
    mongo_database: str,
    client_factory: Callable[..., MongoClient[MongoDocument]],
) -> tuple[MongoClient[MongoDocument], UserCollection]:
    """Connect to MongoDB and prepare the users collection.

    Returns ``(client, collection)``. The deliveries collection is prepared
    at the same time and reached later through :func:`deliveries_for`, so
    the return shape stays the pair both processes already unpack.

    Index creation, the legacy purge and the schema upgrade are best effort:
    a replica that is briefly unavailable should not stop the process from
    starting. A document that was not upgraded is still readable, because
    every reader accepts version 1.
    """
    client = client_factory(host=mongo_host)
    database = client.get_database(mongo_database)
    collection = get_collection(database)

    try:
        ensure_indexes(collection)
        ensure_delivery_indexes(get_delivery_collection(database))
        purge_legacy_secrets(collection)
        upgrade_documents(collection)
    except PyMongoError as exc:
        log.warning("Could not prepare the users collection: %s", exc)

    return client, collection
