"""MongoDB access for the ``users`` collection.

Kept in one place so the bot and the server cannot drift apart on the document
shape, and so the linking rules can be tested against a stub collection.

A link document looks like::

    {
        "schema_version":        2,               # see SCHEMA_VERSION below
        "discord_id":            "123456789",     # primary identity, a string
        "discord_username":      "someone",
        "github_id":             42,              # immutable GitHub account id
        "github_username":       "SomeOne",
        "github_username_lower": "someone",       # for case-insensitive lookup
        "linked_repo":           "https://github.com/owner/repo/",
        "starred_repo":          True,
        "updated_at":            datetime(2026, 1, 1, tzinfo=utc),
    }
"""

import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, Final

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger(__name__)

COLLECTION_NAME: Final = "users"

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

# 1: the original shape. `updated_at` was an ISO 8601 string, and the oldest
#    rows have neither `github_id` nor `github_username_lower`.
# 2: `updated_at` is a BSON datetime, so it can be compared, sorted and
#    indexed in the database rather than only lexically by accident.
#
# Every document carries the version it was written with, and connect() brings
# older ones forward at startup. Readers still accept version 1 values, so a
# rollback to the previous release does not lose anybody's link.
SCHEMA_VERSION: Final = 2

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


def read_updated_at(document: Mapping[str, object]) -> datetime | None:
    """Return ``updated_at`` as an aware datetime, or None when unreadable.

    Schema version 1 stored an ISO 8601 string, so both spellings are
    accepted. Values the database hands back have no timezone attached, since
    BSON datetimes are UTC by definition, and they are labelled as such here
    rather than left naive for a caller to get wrong.
    """
    value = document.get("updated_at")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


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


def set_starred(collection: UserCollection, discord_id: object, starred: object) -> None:
    """Record the current star state for ``discord_id``."""
    collection.update_one(
        {"discord_id": str(discord_id)},
        {
            "$set": {
                "starred_repo": bool(starred),
                "updated_at": datetime.now(UTC),
            }
        },
    )


def connect(
    mongo_host: str,
    mongo_database: str,
    client_factory: Callable[..., MongoClient[MongoDocument]],
) -> tuple[MongoClient[MongoDocument], UserCollection]:
    """Connect to MongoDB and prepare the users collection.

    Returns ``(client, collection)``. Index creation, the legacy purge and the
    schema upgrade are best effort: a replica that is briefly unavailable
    should not stop the process from starting. A document that was not
    upgraded is still readable, because every reader accepts version 1.
    """
    client = client_factory(host=mongo_host)
    database = client.get_database(mongo_database)
    collection = get_collection(database)

    try:
        ensure_indexes(collection)
        purge_legacy_secrets(collection)
        upgrade_documents(collection)
    except PyMongoError as exc:
        log.warning("Could not prepare the users collection: %s", exc)

    return client, collection
