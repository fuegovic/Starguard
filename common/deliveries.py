"""The webhook delivery ids seen recently, and the claim that dedupes them.

Split out of :mod:`common.storage`, which had reached the thousand lines
pylint allows a module, so the next change to it broke the build whatever the
change was. This is the natural seam: the deliveries collection shares nothing
with the users collection except the database handle it is reached through,
its rows expire on their own, and nothing ever joins the two.

The dependency runs one way on purpose. ``storage.connect`` prepares this
collection at startup, so storage imports this module; this module imports
nothing from storage, which is why it declares its own document alias below
and why :func:`deliveries_for` takes any collection rather than the
``UserCollection`` that storage names. Importing back for those two would be a
cycle, and the alias is one line.
"""

from datetime import datetime
from typing import Any, Final

from pymongo import ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from common.storage_errors import translates_driver_errors

# Delivery ids seen recently, so a replayed webhook is dropped before it is
# acted on. Kept out of the users collection because the rows expire and
# nothing else joins against them.
DELIVERY_COLLECTION_NAME: Final = "webhook_deliveries"

# The same ``dict[str, Any]`` storage names, declared here rather than
# imported from it; see the module docstring for why that direction is the
# only one available.
DeliveryDocument = dict[str, Any]
DeliveryCollection = Collection[DeliveryDocument]

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


def get_delivery_collection(database: Database[Any]) -> DeliveryCollection:
    """Return the seen-deliveries collection from ``database``."""
    return database[DELIVERY_COLLECTION_NAME]


def deliveries_for(collection: Collection[Any]) -> DeliveryCollection:
    """Return the deliveries collection that sits beside ``collection``.

    Both processes are handed the users collection and nothing else, so this
    is how the webhook route reaches the second one without a second
    connection or a second set of configuration.

    Takes any collection rather than storage's ``UserCollection``, because
    naming that type here would mean importing the module that imports this
    one. All it reads off the argument is the database it belongs to.
    """
    return get_delivery_collection(collection.database)


@translates_driver_errors
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


@translates_driver_errors
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

    The duplicate is caught here, inside the decorated function, so it
    answers False as it always did rather than reaching the translation and
    being reported as the database failing. A replay is this function working.
    """
    try:
        collection.insert_one({"delivery_id": str(delivery_id), "seen_at": seen_at})
    except DuplicateKeyError:
        return False
    return True


@translates_driver_errors
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
