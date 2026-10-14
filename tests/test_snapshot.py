"""Snapshot reader: contract examples plus derived version-skew fixtures."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from agent_dashboard.presentation import (
    attention_rank,
    build_row_headline,
    reach_for_snapshot,
)
from agent_dashboard.snapshot import read_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "contract" / "v1"

NOW = 2000.0
FRESH_AT = 1998.0  # received 2 s ago: fresh against the default 5 s poll interval


def iso(ts: float) -> str:
    """An ISO-8601 timestamp for a fixed-clock moment in tests."""
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_bytes())


def read(snap: dict | None, **kwargs) -> object:
    args = {"snapshot_at": FRESH_AT, "now": NOW, "poll_interval": 5.0}
    args.update(kwargs)
    return read_snapshot(snap, **args)


def headline_for(snap: dict, reach: str = "live") -> str:
    detail = read(snap)
    return build_row_headline(
        reach, reason="", age_text=None, has_snapshot=True, detail=detail
    )


# Contract examples map to the fleet table.
def test_idle_snapshot_is_waiting_for_a_ready_issue():
    snap = load("snapshot-idle-with-last-finished.json")
    detail = read(snap)
    assert headline_for(snap) == "Waiting for a ready issue"
    assert detail.intake_text == "running"
    assert detail.attempt_text == "—"
    assert detail.budget_text == "n/a"
    assert detail.context_text == "n/a"
    assert detail.turns_text == "n/a"
    assert detail.cache_text == "n/a"
    assert detail.errors_text == "0 / 3"
    assert detail.repository_display == "example-owner/example-repo"


def test_attempt_in_progress_cells():
    snap = load("snapshot-attempt-in-progress.json")
    detail = read(snap)
    assert headline_for(snap) == "Working on #977 · model_execution"
    assert detail.intake_text == "running"
    assert detail.attempt_text == "#977 · model_execution"
    assert detail.budget_text == "640,000 / 4,000,000"
    assert detail.budget_hatched is True
    assert detail.budget_used_pct == 16.0
    assert detail.context_text == "148,000 (+2)"
    assert detail.turns_text == "18"
    assert detail.cache_text == "86%"
    assert detail.errors_text == "0 / 3"


def test_soft_threshold_handoff_headline_and_cells():
    snap = load("snapshot-soft-threshold-handoff.json")
    detail = read(snap)
    assert headline_for(snap) == "Soft threshold crossed, handoff delivering on #981"
    assert detail.intake_text == "stopping"
    assert detail.attempt_text == "#981 · model_execution"
    assert detail.soft_crossed is True
    assert detail.handoff_active is True
    assert detail.budget_text == "3,050,000 / 4,000,000"
    assert detail.budget_hatched is True
    assert detail.context_text == "150,000 (+1)"
    assert detail.turns_text == "47"
    assert detail.cache_text == "91%"


def test_recovery_hold_headline_and_cells():
    snap = load("snapshot-recovery-hold.json")
    detail = read(snap)
    assert headline_for(snap) == (
        "Recovery hold on #981: result comment confirmation is not known yet; "
        "retry unconfirmed publication steps"
    )
    assert detail.intake_text == "stopping"
    assert detail.attempt_text == "Recovery hold #981 · publication"
    assert detail.budget_text == "n/a"
    assert detail.errors_text == "0 / 3"


def test_truncated_snapshot_shows_errors_and_unavailable_cache():
    snap = load("snapshot-truncated.json")
    detail = read(snap)
    assert headline_for(snap) == "1/3 consecutive infrastructure errors"
    assert detail.budget_text == "1,500,000 / 4,000,000"
    assert detail.budget_hatched is False
    assert detail.context_text == "120,000 (+1)"
    assert detail.turns_text == "35"
    assert detail.cache_text == "unavailable"
    assert detail.errors_text == "1 / 3"


# Version-skew rules.
def test_unknown_fields_are_ignored():
    snap = load("snapshot-attempt-in-progress.json")
    skewed = copy.deepcopy(snap)
    skewed["future_top_level"] = {"nested": [1, 2, 3]}
    skewed["control"]["future_control_field"] = "new-agent"
    skewed["current_attempt"]["budget"]["future_figure"] = {"value": 1, "basis": "measured"}
    assert read(skewed) == read(snap)


def test_missing_fields_render_as_unavailable_never_zero():
    snap = load("snapshot-attempt-in-progress.json")
    del snap["current_attempt"]["budget"]
    del snap["current_attempt"]["turns"]
    del snap["current_attempt"]["cache"]
    del snap["current_attempt"]["context"]
    del snap["control"]["consecutive_errors"]
    del snap["control"]["intake"]
    detail = read(snap)
    assert detail.budget_text == "unavailable"
    assert detail.turns_text == "unavailable"
    assert detail.cache_text == "unavailable"
    assert detail.context_text == "unavailable"
    assert detail.errors_text == "unavailable"
    assert detail.intake_text == "unavailable"
    for text in (
        detail.budget_text,
        detail.turns_text,
        detail.cache_text,
        detail.context_text,
        detail.errors_text,
        detail.intake_text,
    ):
        assert text == "unavailable"
        assert "0" not in text


def test_unsupported_schema_uses_only_stable_fields():
    snap = load("snapshot-attempt-in-progress.json")
    snap["schema_version"] = 2
    detail = read(snap)
    assert detail.unsupported_schema == 2
    assert detail.repository_display == "example-owner/example-repo"
    assert detail.agent_version == "0.4.5"
    assert detail.generated_at == "2026-09-30T10:15:00Z"
    assert detail.budget_text == "unavailable"
    assert detail.attempt_text == "unavailable"
    assert detail.intake_text == "unavailable"
    assert reach_for_snapshot("running", "good", snap) == "unsupported"
    headline = build_row_headline(
        "unsupported", reason="", age_text=None, has_snapshot=True, detail=detail
    )
    assert headline == "unsupported schema v2"


def test_string_schema_version_above_supported_is_unsupported():
    snap = load("snapshot-attempt-in-progress.json")
    snap["schema_version"] = "2"
    detail = read(snap)
    assert detail.unsupported_schema == 2
    assert reach_for_snapshot("running", "good", snap) == "unsupported"
    assert detail.budget_text == "unavailable"


def test_absent_sections_render_as_unavailable():
    snap = load("snapshot-attempt-in-progress.json")
    del snap["control"]
    detail = read(snap)
    assert detail.intake_text == "unavailable"
    assert detail.errors_text == "unavailable"
    # The attempt still comes from the live current_attempt section.
    assert detail.attempt_text == "#977"
    both_gone = load("snapshot-attempt-in-progress.json")
    del both_gone["control"]
    del both_gone["current_attempt"]
    missing = read(both_gone)
    assert missing.attempt_text == "unavailable"
    assert missing.intake_text == "unavailable"
    assert missing.budget_text == "unavailable"


def test_estimated_used_without_breakdown_is_hatched():
    snap = load("snapshot-attempt-in-progress.json")
    del snap["current_attempt"]["budget"]["measured"]
    del snap["current_attempt"]["budget"]["estimated"]
    detail = read(snap)
    assert detail.budget_text == "640,000 / 4,000,000"
    assert detail.budget_hatched is True


def test_unknown_basis_renders_as_estimated():
    snap = load("snapshot-attempt-in-progress.json")
    snap["current_attempt"]["budget"]["used"]["basis"] = "telepathic"
    snap["current_attempt"]["turns"]["basis"] = "telepathic"
    detail = read(snap)
    assert detail.budget_used_basis == "estimated"
    assert detail.turns_basis == "estimated"
    assert detail.budget_text == "640,000 / 4,000,000"


def test_null_control_means_not_applicable():
    snap = load("snapshot-attempt-in-progress.json")
    snap["control"] = None
    detail = read(snap)
    assert detail.intake_text == "n/a"
    assert detail.errors_text == "n/a"
    # The attempt still comes from the live current_attempt section.
    assert detail.attempt_text == "#977"


def test_null_current_attempt_means_not_applicable():
    snap = load("snapshot-attempt-in-progress.json")
    snap["current_attempt"] = None
    snap["control"]["active_attempt"] = None
    detail = read(snap)
    assert detail.budget_text == "n/a"
    assert detail.context_text == "n/a"
    assert detail.turns_text == "n/a"
    assert detail.cache_text == "n/a"
    assert headline_for(snap) == "Waiting for a ready issue"


def test_no_snapshot_means_unavailable():
    detail = read(None, snapshot_at=None)
    assert detail.has_snapshot is False
    assert detail.budget_text == "unavailable"
    assert detail.attempt_text == "unavailable"


# Control staleness.
def test_stale_control_marks_control_derived_cells():
    snap = load("snapshot-attempt-in-progress.json")
    # Control built 60 s ago by the wall clock: older than one 5 s poll
    # interval while the snapshot itself just arrived.
    snap["control"]["control_observed_at"] = iso(NOW - 60.0)
    detail = read(snap)
    assert detail.control_stale is True
    assert detail.intake_stale is True
    assert detail.attempt_stale is True
    assert detail.errors_stale is True


def test_fresh_control_is_not_stale():
    snap = load("snapshot-attempt-in-progress.json")
    snap["control"]["control_observed_at"] = iso(NOW - 2.0)
    detail = read(snap)
    assert detail.control_stale is False
    assert detail.intake_stale is False
    assert detail.attempt_stale is False
    assert detail.errors_stale is False


def test_old_snapshot_does_not_mark_control_stale():
    snap = load("snapshot-attempt-in-progress.json")
    snap["control"]["control_observed_at"] = iso(NOW - 60.0)
    detail = read(snap, snapshot_at=1000.0, now=NOW)
    assert detail.control_stale is False


# Attention rank: unreachable, recovery hold, errors and handoff first, then
# approaching the soft threshold, then working, idle and offline.
def rank_of(snap: dict | None, reach: str) -> int:
    detail = read(snap)
    return attention_rank(reach, detail, has_snapshot=snap is not None)


def test_attention_rank_orders_fleet():
    ranks = {
        "unreachable": rank_of(None, "unreachable"),
        "recovery": rank_of(load("snapshot-recovery-hold.json"), "live"),
        "errors": rank_of(load("snapshot-truncated.json"), "live"),
        "handoff": rank_of(load("snapshot-soft-threshold-handoff.json"), "live"),
        "working": rank_of(load("snapshot-attempt-in-progress.json"), "live"),
        "idle": rank_of(load("snapshot-idle-with-last-finished.json"), "live"),
        "offline": rank_of(None, "not running"),
    }
    assert ranks["unreachable"] < ranks["recovery"]
    assert ranks["recovery"] < ranks["errors"]
    assert ranks["errors"] < ranks["handoff"]
    assert ranks["handoff"] < ranks["working"]
    assert ranks["working"] < ranks["idle"]
    assert ranks["idle"] < ranks["offline"]


def test_approaching_soft_threshold_outranks_working():
    snap = load("snapshot-attempt-in-progress.json")
    # 2.9 M of a 3.2 M soft threshold: close, but not crossed.
    snap["current_attempt"]["budget"]["used"]["value"] = 2900000
    detail = read(snap)
    assert detail.approaching_soft is True
    assert detail.soft_crossed is False
    working = rank_of(load("snapshot-attempt-in-progress.json"), "live")
    assert attention_rank("live", detail, has_snapshot=True) < working


def test_consecutive_errors_headline_without_max():
    snap = load("snapshot-truncated.json")
    del snap["control"]["consecutive_errors"]["max_consecutive_errors"]
    snap["control"]["consecutive_errors"]["count"] = 2
    assert headline_for(snap) == "2 consecutive infrastructure errors"
