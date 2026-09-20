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

That ordering is done at millisecond resolution rather than at the
microsecond resolution the instants are made with, because BSON stores a
datetime as a whole number of milliseconds. The stored side of every one of
those comparisons has been truncated and the in-memory side has not, so an
event inside the listing's own millisecond cannot be placed either side of
it and is treated as newer. See `_last_millisecond_before`.

`link_account` in particular must never write `role_sync_pending`. It
upserts with a ``$set``, so every key in that dict overwrites on a re-link.
If the flag were in it, this would happen: a webhook records an un-star and
raises the flag, the same person runs /verify again and completes OAuth
before the bot's next poll, and `link_account` resets the flag to false.
The bot never sees the queued work and the member keeps a role they should
have lost until the next full sweep. The field is absent there on purpose,
not by oversight.

Keeping the flag out of that dict was not enough, and the reason is worth
recording next to it: `starred_repo` was still in it, and that is the fact
the flag is about. The same re-link put the stale star state back over the
un-star, the drain then read the restored value off the row and reconciled
the role to it, and the queued work was spent confirming what the webhook
had just contradicted. So the star state is written on its own now, under
the same ordering condition every other writer of it carries, and
`link_account` takes the instant its caller asked GitHub. See the two
writes there.
"""

import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
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

# The smallest interval a stored timestamp can tell apart. BSON holds a
# datetime as a whole number of milliseconds since the epoch, so the
# microseconds a Python datetime carries are dropped on the way in.
BSON_RESOLUTION: Final = timedelta(milliseconds=1)


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


def release_delivery(collection: DeliveryCollection, delivery_id: object) -> None:
    """Forget ``delivery_id``, so a redelivery of it is new work again.

    The inverse of :func:`claim_delivery`, for a caller that claimed a
    delivery and then could not process it. Without this the claim stands
    for the whole retention window although nothing was recorded, and an
    operator following the documented recovery, redelivering the event by
    hand, is told it has already been handled.

    This makes a claim deliberately non-idempotent, which is the opposite
    of what the collection is for, so the distinction is worth stating
    plainly: a row here means "this delivery is being handled", not "this
    delivery has been handled". A claim that never became a recorded
    change is not a fact worth keeping, and the only thing keeping it can
    achieve is swallowing the retry that would have fixed it.

    Errors are left to propagate and the caller swallows them. That is not
    an oversight to be tidied up later: the database this has to reach is
    the one that has just refused a write, so failing here is likely in
    exactly the case this is needed, and a failed release leaves the claim
    standing, which is the outcome there was anyway. Raising would replace
    the failure the caller is already reporting with a less useful one.
    """
    collection.delete_one({"delivery_id": str(delivery_id)})


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
    observed_at: datetime | None = None,
) -> MongoDocument:
    """Create or update the link between a Discord user and a GitHub account.

    The document is keyed on ``discord_id``. Earlier versions keyed on the
    GitHub email address, so re-linking overwrote the previous user's row and
    left them holding the role with no record for the un-star check to find.

    Raises :class:`AccountAlreadyLinkedError` when the GitHub account is
    already bound to a different Discord user, which stops one star being
    redeemed for the role by several Discord accounts.

    That check has to look twice, because the rows this version upgrades
    from were written before ``github_id`` was recorded and the lookup by
    id cannot see them. One of them is still a link, and still holds the
    role, so without the second lookup the same GitHub account could
    authenticate for a second Discord ID and be given a row of its own
    until whenever the original member happened to re-verify.

    The second lookup is by the lower-cased login, which is the only handle
    those rows carry, and it is restricted to rows with no ``github_id`` at
    all. A row that has one has already been matched or ruled out on it,
    and a login is not immutable: a member who renamed their account left
    their old login free for somebody else to take, and matching that row
    on its stored name would refuse the new owner a link they are entitled
    to. Restricting the lookup means re-verifying once puts a row
    permanently beyond this ambiguity.

    A legacy row that names no Discord user at all blocks nobody; see the
    comment on that branch.

    ``observed_at`` is when the caller's OAuth star check was answered, and
    it orders ``starred_repo`` against the webhook exactly as the sweep's
    listing instant does in :func:`set_starred`. /authorize asks GitHub
    whether this person stars the repository and then writes the answer
    down, and in between the webhook can record an un-star and raise the
    flag for it. Writing unconditionally put the stale ``True`` back while
    deliberately leaving that flag raised, so the drain read the restored
    value off the row, reconciled the role to it and lowered the flag, and
    the un-star was lost until the next full sweep. Keeping
    ``role_sync_pending`` out of the write protects the flag and not the
    value the flag is about.

    That ordering is why the row is written in two statements rather than
    one. The identity fields are upserted unconditionally, because a
    first-time link has to create the row whatever an ordering condition
    would have said about a row that does not exist yet, and
    ``starred_repo`` is then written on its own under the condition
    :func:`_not_newer_than` builds. ``$setOnInsert`` carries the star state
    into a row this call creates, so no row this function writes is ever
    without one, which is the shape :func:`record_star_event` relies on.

    The two statements are not atomic together, and they do not need to
    be. In between, and after a failure of the second, the row holds the
    new identity and the star state it already had, which is exactly what
    it holds when the second is refused as stale; the caller is told the
    save failed, and the webhook or the next sweep settles the state
    either way.

    Passing ``observed_at`` is how a caller gets the whole of that
    ordering. Left out, the horizon is this call's own clock, which is
    later than the answer it stands for by however long the caller took to
    get here, so a webhook that landed inside that window is still written
    over. The default narrows the race to this process; only the caller can
    close it.

    The returned document is what was written, with ``starred_repo`` as the
    row actually holds it, so nobody can read back a star state this call
    declined to store.
    """
    discord_id = str(discord_id)
    github_id = int(github_id)
    written_at = datetime.now(UTC)
    horizon = _last_millisecond_before(observed_at if observed_at is not None else written_at)

    existing = collection.find_one({"github_id": github_id})
    if existing is None:
        legacy = collection.find_one(
            {
                "github_username_lower": github_username.lower(),
                "github_id": {"$exists": False},
            }
        )
        # A row that names no Discord user is not a competing claim on the
        # star, so it must not block anybody. There are more of these than
        # the word legacy suggests; see `clear_role_sync_pending_by_id`
        # for where they come from. Nothing can hold a role for one:
        # the sweep and the drain both give up on a row with no Discord ID
        # to act for. Blocking would hold the GitHub account hostage to a
        # row that redeems nothing, and tell its rightful owner their
        # account is already linked to Discord ID None.
        existing = legacy if legacy and legacy.get("discord_id") else None
    if existing and str(existing.get("discord_id")) != discord_id:
        raise AccountAlreadyLinkedError(existing.get("discord_id"))

    starred = bool(starred_repo)
    identity: MongoDocument = {
        "schema_version": SCHEMA_VERSION,
        "discord_id": discord_id,
        "discord_username": str(discord_username or ""),
        "github_id": github_id,
        "github_username": github_username,
        "github_username_lower": github_username.lower(),
        "linked_repo": linked_repo,
        "updated_at": written_at,
    }
    document: MongoDocument = {**identity, "starred_repo": starred}

    try:
        collection.update_one(
            {"discord_id": discord_id},
            # The star state rides along only on an insert. A row this call
            # creates is visible to the webhook by its github_id the moment
            # it exists, and one without ``starred_repo`` is a shape
            # record_star_event has to treat as neither starred nor
            # un-starred, so it would record the event without queueing the
            # role change it came with. On a row that already exists the
            # write below owns the field instead.
            {"$set": identity, "$setOnInsert": {"starred_repo": starred}},
            upsert=True,
        )
    except DuplicateKeyError as exc:
        # Lost a race against a concurrent link of the same GitHub account.
        raise AccountAlreadyLinkedError(github_id) from exc

    wrote_star = collection.update_one(
        # Named by the identity above as well as the Discord ID, because
        # this is a second write and the row can change owner between the
        # two. Two callbacks for one Discord account carrying different
        # GitHub accounts interleave exactly there: the other one replaces
        # the identity, and a filter that knows only the Discord ID would
        # then write this call's star state onto that account, which never
        # had its star checked. Not matching is the correct outcome, and
        # the fall-through below already reads the row back for it.
        _not_newer_than(horizon, discord_id=discord_id, github_id=github_id),
        {"$set": {"starred_repo": starred}},
    )
    matched: int = getattr(wrote_star, "matched_count", 0)
    if not matched:
        # A star event this observation cannot speak for got there first,
        # so the row keeps it and the flag that event raised stays up for
        # the drain. Reading it back is what stops this reporting a star
        # state it has just declined to store; a row deleted in between
        # reads as no star, which is the direction that hands out no role.
        superseded = collection.find_one({"discord_id": discord_id}, {"_id": 0})
        document["starred_repo"] = bool(superseded and superseded.get("starred_repo"))

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
        _not_newer_than(_last_millisecond_before(observed_at), discord_id=str(discord_id)),
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


def _not_newer_than(horizon: datetime, **keys: object) -> MongoDocument:
    """Build a filter for rows carrying no star event later than ``horizon``.

    The ``$exists`` arm is what keeps every ordinary row matching: a link
    that no star event has ever reached has no ``star_event_at`` at all,
    which is its normal state, and a bare ``$lte`` would exclude all of
    them and stop the sweep writing anything.

    The sweep passes a horizon one millisecond back from its listing and
    :func:`link_account` one millisecond back from the OAuth star check;
    the webhook passes the instant it received the event. See
    :func:`_last_millisecond_before` for why only the readers step back.

    The ``$lte`` here is mirrored in ``server/webhooks.py``, which reads
    the row back to tell a write that landed from one that was refused as
    stale. It has to mirror it, because the answer is whether this filter
    matched and only this filter knows; so changing the comparison means
    changing it there too, or the receiver starts calling successful
    writes superseded. It cannot notice on its own.
    """
    return {
        **keys,
        "$or": [
            {"star_event_at": {"$exists": False}},
            {"star_event_at": {"$lte": horizon}},
        ],
    }


def _last_millisecond_before(observed_at: datetime) -> datetime:
    """The newest stored instant that is certainly earlier than ``observed_at``.

    Written in terms of the sweep's listing, because that is where the cost
    of getting it wrong is highest, but it holds for anything that observed
    the star state in memory and then compared itself against a stored
    event: :func:`link_account` reaches it with the instant the OAuth star
    check was answered, which is the same shape of comparison.

    ``observed_at`` is made by :func:`datetime.now` and never leaves memory,
    so it keeps its microseconds. Every ``star_event_at`` it is compared
    against has been through BSON, which holds whole milliseconds, so the
    stored value has been rounded down and the two cannot be compared as
    they stand. A webhook that landed four hundred microseconds after the
    listing was taken comes back looking earlier than it, and the guards
    built on that comparison then wave through the exact write they exist
    to refuse: the sweep removes the role and writes its stale state over
    the newer one.

    Truncating to the millisecond the database can hold and stepping back
    one puts every event inside the listing's own millisecond on the newer
    side, where the ambiguity belongs. Being wrong that way costs one sweep
    cycle skipping a row it could have written; being wrong the other way
    is the lost update :func:`set_starred` describes.
    """
    truncated = observed_at.replace(microsecond=observed_at.microsecond // 1000 * 1000)
    return truncated - BSON_RESOLUTION


def star_event_is_newer(document: Mapping[str, object], observed_at: datetime) -> bool:
    """Whether a webhook has spoken about ``document`` since ``observed_at``.

    The read-side half of the guard in :func:`set_starred`. The sweep checks
    this before it acts at all, so a member a webhook has newer information
    about keeps their role rather than having it taken and then restored;
    the guard on the write closes the microseconds between this check and
    the write landing.

    Both halves are measured from the same horizon, so they agree on which
    side of the listing an event falls; see :func:`_last_millisecond_before`
    for why that horizon is not ``observed_at`` itself.

    That stepped-back horizon is what makes this the sweep's predicate and
    nobody else's, and the name does not say so loudly enough on its own.
    It is not "has anything happened since my own write", and a writer that
    reuses it against the instant it just wrote is told yes every single
    time: the stored value is that instant truncated, which is later than
    the horizon this steps back to. A caller asking whether its own write
    landed wants its own unshifted instant and the comparison in
    :func:`_not_newer_than`, not this one.
    """
    star_event_at = read_datetime(document, "star_event_at")
    return star_event_at is not None and star_event_at > _last_millisecond_before(observed_at)


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

    Whether the state moved is decided by the filter rather than by a read
    taken a moment earlier, and the write is refused outright once the row
    carries a star event newer than this one. Both are needed, and the
    interleaving they close starts from a row that says un-starred:

    1. A ``created`` delivery reads the row and sees un-starred.
    2. A ``deleted`` delivery reads it, sees un-starred as well, concludes
       nothing moved, and writes un-starred with no flag.
    3. The ``created`` delivery writes starred, and raises the flag.

    The row is left saying starred with work queued, on the older of the
    two events, and the bot hands out a role for a star that was taken
    back. The compare-and-swap alone does not close it, because the two
    statements then simply swap places; the ordering condition is what
    stops step 3 landing at all.

    Two deliveries stamped inside the same millisecond are the one case
    left, since a stored timestamp cannot tell them apart and there is no
    other clock they share. The sweep corrects that row on its next cycle.
    """
    github_id = int(github_id)
    changes: MongoDocument = {
        "starred_repo": starred,
        "star_event_at": occurred_at,
        "star_source": source,
        "updated_at": datetime.now(UTC),
    }

    # The state moved. "The row does not already say this" is the filter
    # rather than something concluded from an earlier read, so the test and
    # the write are one statement. Keyed on the unique github_id, which is
    # the single index hit the separate read used to cost.
    moved = collection.find_one_and_update(
        _not_newer_than(occurred_at, github_id=github_id, starred_repo=not starred),
        {"$set": {**changes, "role_sync_pending": True}},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if moved is not None:
        return moved

    # The state did not move, so the delivery is recorded and the bot is
    # left alone. This filter is the one above without the state condition,
    # so between them they match every row the first could have and no
    # delivery falls through both over what the row happens to say. A row
    # with no ``starred_repo`` at all is not a shape link_account can
    # write, and it lands here rather than being read as un-starred.
    unchanged = collection.find_one_and_update(
        _not_newer_than(occurred_at, github_id=github_id),
        {"$set": changes},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if unchanged is not None:
        return unchanged

    # Neither matched, so either nothing is linked to this account or a
    # newer event reached the row first. Reading it tells the two apart and
    # keeps None meaning "not a verified member", which is the only thing
    # the caller reads off this. A delivery that arrived late reports the
    # row as it now stands, which is the state the bot will act on.
    return collection.find_one({"github_id": github_id}, {"_id": 0})


def iter_pending_role_syncs(collection: UserCollection) -> Iterator[MongoDocument]:
    """Yield the links waiting for the bot to move a role, ``_id`` included.

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

    The Mongo ``_id`` is kept here where :func:`iter_links` drops it,
    because for some of these rows it is the only identity there is. A
    row can carry no usable ``discord_id`` at all, and the drain then has
    no way to lower the flag: the
    filter :func:`clear_role_sync_pending` builds matches nothing, so the
    row is read again, reported again and left queued on every poll for
    the life of the process. See :func:`clear_role_sync_pending_by_id`.
    """
    yield from collection.find({"role_sync_pending": True})


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

    The only writer here with no ordering condition, and it is right that
    it has none, because raising is monotone and lowering is the dangerous
    direction. This can add work and cannot drop any: a needless raise
    costs one poll, since the drain reads ``starred_repo`` off the row
    when it gets there rather than trusting anything the sweep believed,
    and there is no upsert, so a row deleted in between is a no-op. A
    condition would also be self-defeating, because the only one available
    is the one :func:`set_starred` has just refused, so it would skip
    exactly the rows this exists for. It becomes an instance of the
    lost-update class the moment it writes star state, lowers the flag, or
    the drain starts trusting a value handed to it instead of the row.
    """
    collection.update_one(
        {"discord_id": str(discord_id)},
        {"$set": {"role_sync_pending": True}},
    )


def clear_role_sync_pending(
    collection: UserCollection,
    discord_id: object,
    starred: bool,
) -> bool:
    """Take ``discord_id`` off the pending queue, once the bot has acted.

    True when the clear landed, which is the caller's only way to learn
    that the row moved under it and is still queued; the answer is the
    filter's, exactly as in :func:`set_starred`, because only the filter
    knows whether it matched.

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
    result = collection.update_one(
        {"discord_id": str(discord_id), "starred_repo": starred},
        {"$set": {"role_sync_pending": False}},
    )
    matched: int = getattr(result, "matched_count", 0)
    return matched > 0


def clear_role_sync_pending_by_id(collection: UserCollection, document_id: object) -> None:
    """Lower the flag on one row by its Mongo ``_id``, unconditionally.

    For the rows :func:`clear_role_sync_pending` cannot reach at all. A
    queued row with no ``discord_id`` has no member to move a role for
    and no Discord ID to name it by. Lowering the flag is the whole of
    the work; leaving it raised is the same line logged every thirty
    seconds until the process is restarted.

    These are not as rare as the word legacy suggests, and guessing at
    where they come from has already produced one wrong fix, so it is
    worth recording. Every released server from 2023-10-28 until the
    rebuild read the Discord ID straight off the query string, put it in
    the session and wrote it into the document without validating it
    anywhere on the path. An unauthenticated GET to ``/login`` with no
    ``id``, followed by a completed OAuth, therefore stored a row whose
    ``discord_id`` is null, and that held for nearly three years of
    releases. Null is the shape to expect; the field being absent
    entirely is the one-day schema from before it existed. Nothing has
    ever backfilled either, because no Discord ID can be recovered from
    what the row holds.

    An empty string is a third shape and the odd one out: it round-trips
    through ``str()``, so :func:`clear_role_sync_pending` reaches it and
    it was never among the rows that got stuck. A regression test built
    on that shape asserts nothing about this function.

    Unconditional on purpose, where the call above guards on the star
    state it acted on. That guard protects against a webhook moving the
    row while the bot was talking to Discord, and nothing here talked to
    Discord about a member that does not exist, so on these rows it would
    only be a second way to match nothing.
    """
    collection.update_one({"_id": document_id}, {"$set": {"role_sync_pending": False}})


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

    Each one is attempted on its own, and it is worth saying why rather
    than leaving the loop looking like decoration. Sharing one ``try``
    made them look independent when they are not, and the pairing was the
    worst available: a collection carrying rows from the version that keyed
    on the GitHub email holds several rows per Discord account, so the
    unique ``discord_id`` index raises on exactly the upgrade that also has
    to delete that version's stored OAuth tokens. The first failure took
    the purge with it, and the tokens SECURITY.md tells operators are
    removed at startup stayed in the database, with only a line about
    indexes to say so. Anything added here gets the same isolation without
    anyone having to notice the coupling again.
    """
    client = client_factory(host=mongo_host)
    database = client.get_database(mongo_database)
    collection = get_collection(database)

    preparations: tuple[tuple[str, Callable[[], object]], ...] = (
        ("index the users collection", lambda: ensure_indexes(collection)),
        (
            "index the deliveries collection",
            lambda: ensure_delivery_indexes(get_delivery_collection(database)),
        ),
        ("purge credentials written by older versions", lambda: purge_legacy_secrets(collection)),
        ("upgrade user records", lambda: upgrade_documents(collection)),
    )
    for description, prepare in preparations:
        try:
            prepare()
        except PyMongoError as exc:
            log.warning("Could not %s: %s", description, exc)

    return client, collection
