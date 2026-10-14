"""Reach, status headline and age formatting."""

import pytest

from agent_dashboard.collector import (
    AGENT_UNREACHABLE,
    GOOD,
    NOT_SUPPORTED,
    SNAPSHOT_FAILED,
    TIMEOUT,
    UNSUPPORTED_OPERATION,
)
from agent_dashboard.presentation import build_headline, derive_reach, format_age


@pytest.mark.parametrize(
    ("outcome_kind", "reach"),
    [
        (GOOD, "live"),
        (AGENT_UNREACHABLE, "unreachable"),
        (TIMEOUT, "unreachable"),
        (SNAPSHOT_FAILED, "stale"),
        (UNSUPPORTED_OPERATION, "unsupported"),
        (NOT_SUPPORTED, "unsupported"),
        (None, "stale"),
        ("some_future_kind", "stale"),
    ],
)
def test_derive_reach_running(outcome_kind, reach):
    assert derive_reach("running", outcome_kind) == reach


@pytest.mark.parametrize("raw", ["exited", "created", "paused", "dead", "", "Running"])
def test_derive_reach_not_running_wins_over_outcome(raw):
    assert derive_reach(raw, GOOD) == "not running"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0 s"), (5, "5 s"), (59, "59 s"), (60, "1 min"), (150, "2 min"), (7200, "2 h")],
)
def test_format_age(seconds, text):
    assert format_age(seconds) == text


def test_headline_live():
    assert build_headline("live", reason="", age_text=None, has_snapshot=True).startswith(
        "Live"
    )


def test_headline_unreachable_carries_reason():
    headline = build_headline(
        "unreachable",
        reason="docker exec timed out after 5 s",
        age_text="12 s",
        has_snapshot=True,
    )
    assert headline == "Unreachable: docker exec timed out after 5 s"


def test_headline_stale_carries_snapshot_age():
    headline = build_headline(
        "stale", reason="build broke", age_text="45 s", has_snapshot=True
    )
    assert "45 s" in headline
    assert "build broke" in headline


def test_headline_stale_without_snapshot():
    headline = build_headline(
        "stale", reason="build broke", age_text=None, has_snapshot=False
    )
    assert "build broke" in headline


def test_headline_not_running():
    assert (
        build_headline("not running", reason="", age_text=None, has_snapshot=False)
        == "Not running"
    )


def test_headline_unsupported_uses_spec_sentence():
    assert (
        build_headline("unsupported", reason="", age_text=None, has_snapshot=False)
        == "telemetry not supported by this agent version"
    )


def test_headline_never_polled():
    assert (
        build_headline("stale", reason="", age_text=None, has_snapshot=False)
        == "Waiting for the first telemetry poll"
    )
