"""Tests for the shared logging configuration."""

# Test names document the behaviour under test, and the fakes below
# deliberately mirror signatures they do not use.
# pylint: disable=missing-function-docstring,unused-argument

import json
import logging
import sys

import pytest

from common import logging_setup
from common.logging_setup import (
    REQUEST_ID,
    JsonFormatter,
    TextFormatter,
    configure_logging,
    resolve_format,
    resolve_level,
)


def record(message="hello", **extra):
    made = logging.LogRecord("starguard.test", logging.INFO, __file__, 1, message, (), None)
    for key, value in extra.items():
        setattr(made, key, value)
    return made


@pytest.fixture(name="no_request_id", autouse=True)
def no_request_id_fixture():
    token = REQUEST_ID.set("")
    yield
    REQUEST_ID.reset(token)


@pytest.mark.parametrize(
    "raw,expected",
    [("debug", logging.DEBUG), ("WARNING", logging.WARNING), (None, logging.INFO)],
)
def test_levels_are_resolved(raw, expected):
    assert resolve_level(raw) == expected


def test_an_unusable_level_falls_back_instead_of_raising():
    # basicConfig used to raise out of the logging setup itself, so a typo in
    # LOG_LEVEL stopped the process with a traceback about logging.
    assert resolve_level("chatty") == logging.INFO


@pytest.mark.parametrize(
    "raw,expected", [("json", "json"), ("JSON", "json"), ("", "text"), ("yaml", "text")]
)
def test_formats_are_resolved(raw, expected):
    assert resolve_format(raw) == expected


def test_json_output_is_one_object_per_record():
    payload = json.loads(JsonFormatter().format(record("hello")))
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "starguard.test"
    assert "timestamp" in payload


def test_extra_fields_stay_typed_in_json():
    # The point of the JSON format: the star check summary is queryable
    # rather than a string a log search has to pick apart.
    payload = json.loads(JsonFormatter().format(record("done", examined=12, api_calls=3)))
    assert payload["examined"] == 12
    assert payload["api_calls"] == 3


def test_a_value_json_cannot_encode_does_not_break_logging():
    payload = json.loads(JsonFormatter().format(record("done", thing=object())))
    assert isinstance(payload["thing"], str)


def test_the_request_id_reaches_both_formats():
    token = REQUEST_ID.set("abc123")
    try:
        assert "abc123" in TextFormatter("%(message)s").format(record())
        assert json.loads(JsonFormatter().format(record()))["request_id"] == "abc123"
    finally:
        REQUEST_ID.reset(token)


def test_no_request_id_means_no_noise():
    assert TextFormatter("%(message)s").format(record()) == "hello"
    assert "request_id" not in json.loads(JsonFormatter().format(record()))


def test_an_exception_is_carried_into_the_json_object():
    try:
        raise ValueError("boom")
    except ValueError:
        made = record("the cycle failed")
        made.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(made))
    assert "ValueError: boom" in payload["exception"]


def test_a_stack_trace_is_carried_into_the_json_object():
    made = record("where am i")
    made.stack_info = "Stack (most recent call last):\n  somewhere"
    payload = json.loads(JsonFormatter().format(made))
    assert "somewhere" in payload["stack"]


@pytest.fixture(name="root_logger")
def root_logger_fixture():
    """Put the root logger back however configure_logging leaves it."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    installed = logging_setup._INSTALLED_HANDLER  # pylint: disable=protected-access
    yield root
    root.handlers = handlers
    root.setLevel(level)
    logging_setup._INSTALLED_HANDLER = installed  # pylint: disable=protected-access


def test_configuring_twice_replaces_the_handler_rather_than_stacking_them(
    root_logger,
):
    # Every line would otherwise be printed once per call, and the tests
    # themselves call this more than once.
    assert configure_logging({"LOG_FORMAT": "json", "LOG_LEVEL": "DEBUG"}) == "json"
    assert root_logger.level == logging.DEBUG
    after_first = len(root_logger.handlers)

    assert configure_logging({"LOG_FORMAT": "text"}) == "text"
    assert len(root_logger.handlers) == after_first
    assert isinstance(root_logger.handlers[-1].formatter, TextFormatter)
    assert root_logger.level == logging.INFO


def test_the_environment_chooses_the_format_when_nothing_is_passed(monkeypatch, root_logger):
    monkeypatch.setenv("LOG_FORMAT", "json")
    assert configure_logging() == "json"
    assert isinstance(root_logger.handlers[-1].formatter, JsonFormatter)
