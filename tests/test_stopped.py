"""Stopped-instance tree-copy readers: ``docker cp`` archives parsed dashboard-side.

Covers the safety contract (traversal and symlink members never become data,
non-tar input reads as layout-not-recognised) and the parity rules shared
with the exec collectors (caps, unreadable counting, kept events, file
listing). Vocabulary: Attempt log, Managed instance.
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest
from fake_gateway import make_logs_tree, make_run_tree

from agent_dashboard.attempt_log import read_stopped_attempt_log
from agent_dashboard.history import read_stopped_history
from agent_dashboard.progress import read_stopped_progress
from agent_dashboard.stopped import (
    events_payload_from_run_tree,
    parse_logs_tree,
    parse_run_tree,
)

STARTED_DASH = "2026-09-27T16-33-43.385090Z"

ATTEMPT = {
    "outcome": "complete",
    "started_at": "2026-09-27T16:33:43.385090Z",
    "duration_seconds": 60,
}


def _tar(entries: dict[str, tuple[bytes, str | None]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, (data, kind) in entries.items():
            info = tarfile.TarInfo(name)
            if kind is None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == tarfile.DIRTYPE:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.type = kind
                info.linkname = "/tmp/evil"
                info.size = 0
                tar.addfile(info)
    return buf.getvalue()


def _event(name, detail="", timestamp="2026-09-27T16:34:00Z"):
    return json.dumps(
        {"timestamp": timestamp, "level": "INFO", "event": name, "detail": detail}
    )


def test_logs_tree_lists_runs_and_flags_events():
    data = make_logs_tree(
        {
            f"logs/98/{STARTED_DASH}/attempt.json": json.dumps(ATTEMPT).encode(),
            f"logs/98/{STARTED_DASH}/agent_output.json": b"",
            "logs/99/2026-09-28T10-00-00.000000Z/attempt.json": b"not json",
        }
    )
    payload = parse_logs_tree(data)
    assert payload["layout"] == "attempt-logs/v1"
    assert payload["unreadable"] == 1
    by_issue = {(e["issue"], e["started"]): e for e in payload["attempts"]}
    assert by_issue[(98, STARTED_DASH)]["has_events"] is True
    assert by_issue[(98, STARTED_DASH)]["attempt"]["outcome"] == "complete"


def test_logs_tree_missing_attempt_stays_reachable():
    data = make_logs_tree({f"logs/98/{STARTED_DASH}/agent_output.json": b""})
    payload = parse_logs_tree(data)
    assert len(payload["attempts"]) == 1
    assert payload["attempts"][0]["attempt"] is None
    assert payload["unreadable"] == 0


def test_logs_tree_ignores_traversal_symlinks_and_outside_dirs():
    data = _tar(
        {
            f"logs/98/{STARTED_DASH}/attempt.json": (json.dumps(ATTEMPT).encode(), None),
            "../../evil": (b"x", None),
            f"logs/98/{STARTED_DASH}/link.json": (b"", tarfile.SYMTYPE),
            "other/98/other/attempt.json": (json.dumps(ATTEMPT).encode(), None),
            "logs/nope/attempt.json": (json.dumps(ATTEMPT).encode(), None),
        }
    )
    payload = parse_logs_tree(data)
    assert len(payload["attempts"]) == 1
    assert payload["attempts"][0]["issue"] == 98


def test_logs_tree_non_tar_reads_as_empty_list():
    result = read_stopped_history(b"not a tar stream", now=1000.0)
    assert result.ok is True
    assert result.total == 0
    run_result, _ = read_stopped_attempt_log(
        b"not a tar stream", issue=98, started=STARTED_DASH
    )
    assert run_result.ok is False
    assert run_result.error == "layout not recognised"


def test_logs_tree_oversize_attempt_counts_unreadable():
    big = b"x" * (1_000_001)
    data = make_logs_tree({f"logs/98/{STARTED_DASH}/attempt.json": big})
    payload = parse_logs_tree(data)
    assert payload["unreadable"] == 1
    assert payload["attempts"][0]["attempt"] is None


def test_run_tree_header_events_and_names():
    data = make_run_tree(
        {
            f"{STARTED_DASH}/attempt.json": json.dumps(ATTEMPT).encode(),
            f"{STARTED_DASH}/agent_output.json": (
                _event("tool_call", "Bash -> 0003_tool_call.json") + "\n"
                + "garbage{\n"
            ).encode(),
            f"{STARTED_DASH}/setup_stdout.log": b"ok",
        }
    )
    result, names = read_stopped_attempt_log(data, issue=98, started=STARTED_DASH)
    assert result.ok is True
    assert result.issue == 98
    assert len(result.events) == 2
    assert result.events[1].event == "unparsable_line"
    assert "setup_stdout.log" in names
    assert "attempt.json" in names


def test_run_tree_wrong_dir_ignored_and_invalid_ref_rejected():
    data = make_run_tree(
        {"2026-09-28T10-00-00.000000Z/attempt.json": json.dumps(ATTEMPT).encode()}
    )
    result, _ = read_stopped_attempt_log(data, issue=98, started=STARTED_DASH)
    assert result.ok is False
    with pytest.raises(ValueError):
        parse_run_tree(data, issue=98, started="../evil")


def test_events_tree_keeps_only_progress_events():
    data = make_run_tree(
        {
            f"{STARTED_DASH}/attempt.json": json.dumps(ATTEMPT).encode(),
            f"{STARTED_DASH}/agent_output.json": (
                _event("tool_call", "x") + "\n"
                + _event("limits_checked", "thread=main; turns=3; elapsed_seconds=10")
                + "\n"
                + _event("token_soft_threshold_handoff_note_committed", "") + "\n"
            ).encode(),
        }
    )
    payload = events_payload_from_run_tree(data, issue=98, started=STARTED_DASH)
    assert payload["layout"] == "attempt-events/v1"
    assert [e["event"] for e in payload["events"]] == [
        "limits_checked",
        "token_soft_threshold_handoff_note_committed",
    ]
    result = read_stopped_progress(data, issue=98, started=STARTED_DASH)
    assert result.ok is True
    assert len(result.readings) == 1
    assert result.readings[0].turns == 3


def test_events_tree_truncated_past_cap():
    big = b"x" * (2_000_001)
    data = make_run_tree({f"{STARTED_DASH}/agent_output.json": big})
    payload = events_payload_from_run_tree(data, issue=98, started=STARTED_DASH)
    assert payload["truncated"] is True
    assert payload["events"] == []
