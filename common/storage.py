"""MongoDB access for the ``users`` collection.

Kept in one place so the bot and the server cannot drift apart on the document
shape, and so the linking rules can be tested against a stub collection.

A link document looks like::

    {
        "discord_id":            "123456789",     # primary identity, a string
        "discord_username":      "someone",
        "github_id":             42,              # immutable GitHub account id
        "github_username":       "SomeOne",
        "github_username_lower": "someone",       # for case-insensitive lookup
        "linked_repo":           "https://github.com/owner/repo/",
        "starred_repo":          True,
        "updated_at":            "2026-01-01T00:00:00+00:00",
    }
"""

import logging
from datetime import datetime, timezone

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger(__name__)

COLLECTION_NAME = "users"

# Fields earlier versions wrote that must never be stored again. The OAuth
# access token in particular was kept in clear text alongside a `repo` scope,
# which made the collection a set of credentials for every verified user's
# private repositories.
LEGACY_SECRET_FIELDS = ("github_token", "github_email")


class AccountAlreadyLinkedError(RuntimeError):
    """Raised when a GitHub account is already linked to another Discord user."""

    def __init__(self, existing_discord_id):
        super().__init__(
            f"GitHub account already linked to Discord ID {existing_discord_id}."
        )
        self.existing_discord_id = existing_discord_id


def get_collection(database):
    """Return the users collection from ``database``."""
    return database[COLLECTION_NAME]


def ensure_indexes(collection):
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
    collection.create_index(
        [("github_username_lower", ASCENDING)], name="github_username_lower"
    )


def purge_legacy_secrets(collection):
    """Delete credentials written by older versions. Returns rows changed.

    Upgrading the code stops new tokens being stored, but any already in the
    database stay valid until they are removed, so this runs at startup.
    """
    query = {"$or": [{field: {"$exists": True}} for field in LEGACY_SECRET_FIELDS]}
    update = {"$unset": {field: "" for field in LEGACY_SECRET_FIELDS}}
    result = collection.update_many(query, update)
    modified = getattr(result, "modified_count", 0)
    if modified:
        log.warning(
            "Removed stored OAuth tokens/emails from %s existing user record(s). "
            "Any GitHub tokens previously issued to this app should be revoked.",
            modified,
        )
    return modified


def find_link(collection, discord_id):
    """Return the link document for ``discord_id``, or None."""
    return collection.find_one({"discord_id": str(discord_id)})


def all_links(collection):
    """Return every link document, without the Mongo ``_id``."""
    return list(collection.find({}, {"_id": 0}))


def link_account(
    collection,
    discord_id,
    discord_username,
    github_id,
    github_username,
    linked_repo,
    starred_repo,
):
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

    document = {
        "discord_id": discord_id,
        "discord_username": str(discord_username or ""),
        "github_id": github_id,
        "github_username": github_username,
        "github_username_lower": github_username.lower(),
        "linked_repo": linked_repo,
        "starred_repo": bool(starred_repo),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        collection.update_one(
            {"discord_id": discord_id}, {"$set": document}, upsert=True
        )
    except DuplicateKeyError as exc:
        # Lost a race against a concurrent link of the same GitHub account.
        raise AccountAlreadyLinkedError(github_id) from exc

    return document


def set_starred(collection, discord_id, starred):
    """Record the current star state for ``discord_id``."""
    collection.update_one(
        {"discord_id": str(discord_id)},
        {
            "$set": {
                "starred_repo": bool(starred),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )


def connect(mongo_host, mongo_database, client_factory):
    """Connect to MongoDB and prepare the users collection.

    Returns ``(client, collection)``. Index creation and the legacy purge are
    best effort: a replica that is briefly unavailable should not stop the
    process from starting.
    """
    client = client_factory(host=mongo_host)
    database = client.get_database(mongo_database)
    collection = get_collection(database)

    try:
        ensure_indexes(collection)
        purge_legacy_secrets(collection)
    except PyMongoError as exc:
        log.warning("Could not prepare the users collection: %s", exc)

    return client, collection
