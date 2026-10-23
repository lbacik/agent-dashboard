"""Control audit writer (spec section 7): path, modes, schema, mirror, failures.

The audit is append-only JSONL under ``$XDG_STATE_HOME/agent-dashboard/``
(falling back to ``~/.local/state/agent-dashboard/``), directory ``0700``,
file ``0600``, each entry mirrored to stdout as ``control-audit <json>``
with one process-wide lock and one write + flush + fsync per entry. A
failed write is reported to the caller.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime

import pytest

from agent_dashboard import control_audit
from agent_dashboard.control_audit import append_entry, audit_path, refused_entry


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("HOME", raising=False)  # path must not depend on HOME here
    # HOME is still needed by Path.home(); point it at the tmp dir instead.
    monkeypatch.setenv("HOME", str(tmp_path))


def read_lines():
    path = audit_path()
    assert path.exists()
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_audit_path_uses_xdg_state_home(tmp_path):
    assert audit_path() == tmp_path / "agent-dashboard" / "control-audit.jsonl"


def test_audit_path_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert audit_path() == tmp_path / ".local" / "state" / "agent-dashboard" / "control-audit.jsonl"


def test_audit_path_ignores_blank_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", "   ")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert audit_path() == tmp_path / ".local" / "state" / "agent-dashboard" / "control-audit.jsonl"


def test_refused_entry_schema():
    entry = refused_entry(
        instance="work",
        reason="origin",
        error="Request refused: missing or mismatched Origin header",
        action="stop",
        params={},
        command="stop",
        agent_version="0.4.5",
    )
    assert entry["outcome"] == "refused"
    assert entry["instance"] == "work"
    assert entry["reason"] == "origin"
    assert entry["error"].startswith("Request refused:")
    assert entry["request_id"] is None
    assert entry["container_id"] is None
    assert entry["expected_repository"] is None
    assert entry["action"] == "stop"
    assert entry["params"] == {}
    assert entry["command"] == "stop"
    assert entry["agent_version"] == "0.4.5"
    assert entry["expect_repo_guard"] is False
    assert entry["retry"] is False
    assert entry["exit_code"] is None
    assert entry["sequence"] is None
    assert entry["acknowledgement"] is None
    assert entry["reply_repository"] is None
    assert entry["repository_mismatch"] is None
    assert entry["duration_ms"] is None
    # ISO 8601 UTC with milliseconds.
    moment = datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
    assert moment.tzinfo is not None
    assert entry["at"].endswith("Z")
    assert "." in entry["at"]


def test_append_creates_dir_and_file_with_modes(capsys):
    entry = refused_entry(instance="work", reason="validation", error="Invalid action")
    path = append_entry(entry)
    assert path == audit_path()
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    lines = read_lines()
    assert len(lines) == 1
    assert lines[0]["outcome"] == "refused"
    # The stdout mirror carries the same JSON line.
    out = capsys.readouterr().out
    assert out.startswith("control-audit ")
    assert json.loads(out.removeprefix("control-audit ").strip()) == lines[0]


def test_append_is_one_line_per_entry():
    append_entry(refused_entry(instance="a", reason="origin", error="refused"))
    append_entry(refused_entry(instance="b", reason="busy", error="refused"))
    lines = audit_path().read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["instance"] for line in lines] == ["a", "b"]


def test_failing_write_is_reported_to_caller(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(control_audit.os, "open", boom)
    with pytest.raises(OSError, match="disk full"):
        append_entry(refused_entry(instance="work", reason="origin", error="refused"))
    # Nothing was mirrored: the write failed before stdout.
    assert not audit_path().exists()
