"""Tests for the seam between the driver's exceptions and Starguard's own.

The rest of the suite exercises this incidentally, through the real storage
functions. These are here because incidental coverage of a translation layer
is exactly the kind that passes while the translation does nothing: a decorator
that returned the function unchanged would keep every other test in this
repository green, because those tests reach it through fakes that could raise
either class and callers that used to catch the other one.
"""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import pytest
from pymongo.errors import (
    DuplicateKeyError,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from common.storage_errors import (
    StorageError,
    StorageUnavailableError,
    translates_driver_errors,
    translates_driver_errors_while_iterating,
)


def test_a_driver_error_comes_back_as_a_storage_error():
    @translates_driver_errors
    def read():
        raise PyMongoError("no primary available")

    with pytest.raises(StorageError) as caught:
        read()

    # The message survives, because every caller logs it and an operator
    # reading "no primary available" learns more than one reading "read
    # failed".
    assert "no primary available" in str(caught.value)


def test_the_driver_error_stays_reachable_as_the_cause():
    @translates_driver_errors
    def read():
        raise PyMongoError("no primary available")

    with pytest.raises(StorageError) as caught:
        read()

    # Chained rather than swallowed, so a traceback still names the driver's
    # own class and whoever is debugging is not left guessing which layer
    # actually failed.
    assert isinstance(caught.value.__cause__, PyMongoError)


def test_a_driver_subclass_is_translated_too():
    # DuplicateKeyError is the one the storage functions catch by hand, so it
    # matters that anything they do not catch still reaches the translation
    # rather than escaping as itself.
    @translates_driver_errors
    def write():
        raise DuplicateKeyError("duplicate")

    with pytest.raises(StorageError):
        write()


def test_the_return_value_is_passed_straight_through():
    @translates_driver_errors
    def read():
        return {"discord_id": "1"}

    assert read() == {"discord_id": "1"}


def test_an_error_that_is_not_the_drivers_is_left_alone():
    # AccountAlreadyLinkedError travels this path on every refused link. It is
    # a rule declining a write, not a database failing to perform one, and a
    # caller that cannot tell those apart shows the wrong page.
    @translates_driver_errors
    def write():
        raise ValueError("a rule said no")

    with pytest.raises(ValueError):
        write()


def test_the_wrapped_function_keeps_its_name():
    @translates_driver_errors
    def find_link():
        return None

    # Not cosmetic: the tests elsewhere monkeypatch these by name, and a
    # decorator that lost it would make those patches silently miss.
    assert find_link.__name__ == "find_link"


def test_an_iterator_that_fails_midway_raises_a_storage_error():
    # The case the second decorator exists for. A generator function's body
    # does not run until the first next(), so a wrapper that only guarded the
    # call would return the generator without incident and let every error
    # from the cursor behind it escape untranslated.
    @translates_driver_errors_while_iterating
    def iter_links():
        yield {"discord_id": "1"}
        raise PyMongoError("cursor not found")

    found = []
    with pytest.raises(StorageError) as caught:
        for document in iter_links():
            found.append(document)

    # It failed while being consumed, not when it was called, and what it
    # yielded before failing was really delivered.
    assert found == [{"discord_id": "1"}]
    assert "cursor not found" in str(caught.value)


def test_an_iterator_is_not_consumed_by_wrapping_it():
    @translates_driver_errors_while_iterating
    def iter_links():
        yield from ({"discord_id": "1"}, {"discord_id": "2"})

    assert list(iter_links()) == [{"discord_id": "1"}, {"discord_id": "2"}]


def test_wrapping_an_iterator_does_not_start_it():
    # next_batch in bot.starcheck pulls a page at a time from a generator
    # that stays open across awaits, so the work has to happen on demand
    # rather than when iter_links is called.
    started = []

    @translates_driver_errors_while_iterating
    def iter_links():
        started.append(True)
        yield {"discord_id": "1"}

    links = iter_links()
    assert not started
    assert next(links) == {"discord_id": "1"}
    assert started == [True]


def test_a_database_that_cannot_be_reached_is_told_apart():
    # connect() attempts four preparations in a row and has to know which
    # failures make the next attempt pointless. A wholly unreachable
    # database raises this one, after the full server-selection deadline.
    @translates_driver_errors
    def read():
        raise ServerSelectionTimeoutError("no replica set members available")

    with pytest.raises(StorageUnavailableError) as caught:
        read()

    assert "no replica set members available" in str(caught.value)


def test_the_unreachable_case_is_still_a_storage_error():
    # Every existing caller catches StorageError and none of them should
    # have to learn about the subclass to keep working.
    @translates_driver_errors
    def read():
        raise ServerSelectionTimeoutError("no primary available")

    with pytest.raises(StorageError):
        read()

    assert issubclass(StorageUnavailableError, StorageError)


def test_a_failure_the_database_answered_with_is_not_the_unreachable_one():
    # The half that makes the distinction worth having: a store that
    # refused one operation says nothing about the next, so this must not
    # be mistaken for the database being gone.
    @translates_driver_errors
    def write():
        raise DuplicateKeyError("discord_id_unique")

    with pytest.raises(StorageError) as caught:
        write()

    assert not isinstance(caught.value, StorageUnavailableError)


def test_an_iterator_reports_an_unreachable_database_as_such_too():
    # The cursor behind iter_links can lose the server mid-page, and the
    # second decorator has to make the same distinction as the first.
    @translates_driver_errors_while_iterating
    def iter_links():
        yield {"discord_id": "1"}
        raise ServerSelectionTimeoutError("connection pool paused")

    with pytest.raises(StorageUnavailableError):
        list(iter_links())
