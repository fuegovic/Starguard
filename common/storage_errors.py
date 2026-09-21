"""One exception for "the database could not answer", and the translation to it.

Every module that talks to the database used to catch ``PyMongoError``, the
driver's own base class, so eight files outside this package named pymongo in
their error handling. The data access itself was already behind named
functions: nothing outside :mod:`common.storage` and :mod:`common.deliveries`
ever called a collection method. Only the failures leaked, which is the half
of an abstraction that costs nothing at all until the day the store changes,
and then costs an edit in every file that caught the wrong thing.

The decorators below are that seam. A function that reaches the database
wears one, and whatever the driver raises inside it comes back out as
:class:`StorageError`, carrying the driver's own message and the original
exception as ``__cause__`` so a traceback still names the real cause.

Two decorators rather than one that inspects what it is given, because a
generator function needs the ``try`` to wrap the iteration and not the call.
A plain wrapper around ``iter_links`` would only ever see failures raised
while the generator object was being constructed, which is never: the body
does not run until the first ``next``, so every error from the cursor behind
it would sail straight past a wrapper that looked correct.

The seam also carries one distinction, because the alternative is for
callers to reach back through ``__cause__`` and name the driver's classes
again, which is the leak this module exists to stop.
:class:`StorageUnavailableError` is a ``StorageError`` raised when the
driver could not reach a server at all, as opposed to one the store
answered. Only :func:`common.storage.connect` acts on it today.

Two things are deliberately left alone.

:class:`common.storage.AccountAlreadyLinkedError` is a rule declining a write,
not a database failing to perform one, and a caller wants to tell those apart.
It needs no exemption to survive: it is not a driver error, so it passes
through a decorated function untouched.

``common.config.require_mongo_host`` uses the driver to parse a connection
string and already reports a bad one as ``ConfigError``. It leaks nothing to
its callers, and validating a connection string is specific to the store in a
way that reading a row is not.
"""

import functools
from collections.abc import Callable, Iterator
from typing import ParamSpec, TypeVar

from pymongo.errors import ConnectionFailure, PyMongoError

_P = ParamSpec("_P")
_R = TypeVar("_R")
_T = TypeVar("_T")


class StorageError(RuntimeError):
    """Raised when the database could not answer.

    The one exception the rest of Starguard catches around anything that
    reads or writes. It says the store failed, and nothing about which store
    it was.
    """


class StorageUnavailableError(StorageError):
    """Raised when the database could not be reached at all.

    A :class:`StorageError`, so every existing caller keeps catching it
    without knowing it exists. The distinction is for the one caller that
    has several operations to attempt and should stop after the first: a
    failure to reach the store says the next operation will fail the same
    way, and pay the same wait to find out, whereas a failure the store
    answered with says nothing about the next one. See
    :func:`common.storage.connect`.
    """


def _translate(exc: PyMongoError) -> StorageError:
    """Pick the exception that says what kind of failure this was.

    ``ConnectionFailure`` is the driver's own base class for "no usable
    server", and ``ServerSelectionTimeoutError`` (the one a wholly
    unreachable database raises, after the full server-selection deadline)
    is a subclass of it. Anything else is the store declining or failing an
    operation it actually received.
    """
    if isinstance(exc, ConnectionFailure):
        return StorageUnavailableError(str(exc))
    return StorageError(str(exc))


def translates_driver_errors(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Re-raise whatever the driver raises inside ``func`` as a StorageError.

    The message is kept, because that is what every caller logs, and the
    original is chained so a traceback still names the driver's own class.
    """

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return func(*args, **kwargs)
        except PyMongoError as exc:
            raise _translate(exc) from exc

    return wrapper


def translates_driver_errors_while_iterating(
    func: Callable[_P, Iterator[_T]],
) -> Callable[_P, Iterator[_T]]:
    """The same, for a function whose work happens as its result is consumed.

    ``yield from`` rather than ``return``, which is what puts the ``try``
    around the iteration instead of around the call that sets it up. See the
    module docstring for why the difference is not cosmetic.
    """

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> Iterator[_T]:
        try:
            yield from func(*args, **kwargs)
        except PyMongoError as exc:
            raise _translate(exc) from exc

    return wrapper
