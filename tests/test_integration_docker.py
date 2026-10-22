"""Docker-backed integration test: skipped where Docker is not available.

Builds a minimal image carrying the agent discovery label whose
``/usr/local/bin/agentctl`` is a stub script, then runs one container
printing a good snapshot fixture and one behaving like an old agent
without the ``telemetry`` subcommand. A second image carries canned
Attempt log files so the history exec is covered against a real daemon.
A third image carries one attempt with a small and an over-cap Evidence
file so the Attempt log view's read paths (``docker exec`` listing plus
``docker cp`` capped preview, full download, and allow-list rejection)
are proven against a real daemon.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from agent_dashboard.collector import GOOD, NOT_SUPPORTED, classify_telemetry
from agent_dashboard.gateway import (
    AGENT_ROLE_LABEL,
    EVIDENCE_PREVIEW_CAP,
    DockerGateway,
)
from agent_dashboard.history import read_history

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "contract"
    / "v1"
    / "snapshot-idle-with-last-finished.json"
)

AGENTCTL_STUB = """\
#!/bin/sh
# Stub agentctl: good snapshot unless AGENTCTL_MODE=unsupported, in which
# case it behaves like an older image whose argparse rejects the subcommand
# (exit 2, usage on stderr, empty stdout).
if [ "$AGENTCTL_MODE" = "unsupported" ]; then
    echo "usage: agentctl [--help] {status}" >&2
    echo "agentctl: error: argument command: invalid choice: 'telemetry'" >&2
    exit 2
fi
cat /snapshot.json
"""

DOCKERFILE = """\
FROM busybox:stable
LABEL io.github.lbacik.simple-coding-agent.role="agent"
COPY agentctl /usr/local/bin/agentctl
COPY snapshot.json /snapshot.json
RUN chmod +x /usr/local/bin/agentctl && adduser -D agent
CMD ["sh", "-c", "while true; do sleep 3600; done"]
"""


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


docker = pytest.mark.skipif(
    not docker_available(), reason="docker CLI/daemon not available"
)

NOW = 1000.0


def _run(*argv: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=180, **kwargs)


@docker
def test_telemetry_against_real_docker(tmp_path):
    tag = f"agent-dashboard-e2e:{uuid.uuid4().hex[:12]}"
    good_name = f"e2e-good-{uuid.uuid4().hex[:8]}"
    old_name = f"e2e-old-{uuid.uuid4().hex[:8]}"
    containers = [good_name, old_name]
    try:
        (tmp_path / "agentctl").write_text(AGENTCTL_STUB)
        (tmp_path / "snapshot.json").write_text(FIXTURE.read_text())
        (tmp_path / "Dockerfile").write_text(DOCKERFILE)
        build = _run("docker", "build", "-q", "-t", tag, str(tmp_path))
        assert build.returncode == 0, build.stderr

        for name, mode in ((good_name, "good"), (old_name, "unsupported")):
            started = _run(
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-e",
                f"AGENTCTL_MODE={mode}",
                tag,
            )
            assert started.returncode == 0, started.stderr
        # Give the containers a moment to start.
        import time

        deadline = time.time() + 30
        ids: dict[str, str] = {}
        while time.time() < deadline:
            found = asyncio.run(DockerGateway(timeout=10.0).list_instances())
            ids = {inst.name: inst.container_id for inst in found}
            if good_name in ids and old_name in ids:
                break
            time.sleep(1)
        assert good_name in ids and old_name in ids
        assert AGENT_ROLE_LABEL == "io.github.lbacik.simple-coding-agent.role=agent"

        async def poll_both():
            gateway = DockerGateway(timeout=10.0)
            good = await gateway.telemetry(ids[good_name])
            old = await gateway.telemetry(ids[old_name])
            return good, old

        good_exec, old_exec = asyncio.run(poll_both())

        good_outcome, good_snapshot = classify_telemetry(
            good_exec.stdout, good_exec.returncode, now=NOW
        )
        assert good_outcome.kind == GOOD
        assert good_snapshot is not None
        assert good_snapshot["schema_version"] == 1
        expected = json.loads(FIXTURE.read_text())
        assert good_snapshot == expected

        old_outcome, old_snapshot = classify_telemetry(
            old_exec.stdout, old_exec.returncode, now=NOW
        )
        assert old_exec.returncode == 2
        assert old_outcome.kind == NOT_SUPPORTED
        assert old_snapshot is None
    finally:
        for name in containers:
            _run("docker", "rm", "-f", name)
        _run("docker", "rmi", "-f", tag)


HISTORY_DOCKERFILE = """\
FROM python:3.13-slim
LABEL io.github.lbacik.simple-coding-agent.role="agent"
COPY logs /data/logs
COPY state /data/state
RUN useradd -m agent
CMD ["sleep", "infinity"]
"""

HISTORY_ATTEMPT_DONE = {
    "outcome": "complete",
    "started_at": "2026-09-27T16:33:43.385090Z",
    "completed_at": "2026-09-27T17:02:11.000000Z",
    "duration_seconds": 1712,
    "model_usage": {
        "claude-x": {
            "input_tokens": 120000,
            "cache_read_input_tokens": 350000,
            "cache_creation_input_tokens": 12000,
            "output_tokens": 42000,
            "cost_usd": 1.23,
        }
    },
    "model_stop_reason": "end_turn",
}

HISTORY_ACTIVE_STARTED = "2026-10-16T10:00:00.000000Z"


@docker
def test_attempt_history_against_real_docker(tmp_path):
    tag = f"agent-dashboard-history:{uuid.uuid4().hex[:12]}"
    name = f"e2e-history-{uuid.uuid4().hex[:8]}"
    try:
        logs = tmp_path / "logs"
        done_dir = logs / "98" / "2026-09-27T16-33-43.385090Z"
        active_dir = logs / "98" / "2026-10-16T10-00-00.000000Z"
        done_dir.mkdir(parents=True)
        active_dir.mkdir(parents=True)
        (done_dir / "attempt.json").write_text(json.dumps(HISTORY_ATTEMPT_DONE))
        (active_dir / "attempt.json").write_text(
            json.dumps({"started_at": HISTORY_ACTIVE_STARTED})
        )
        state = tmp_path / "state"
        state.mkdir()
        (state / "attempt.json").write_text(
            json.dumps(
                {
                    "issue_number": 98,
                    "branch": "agent/issue-98",
                    "phase": "model_running",
                    "started_at": HISTORY_ACTIVE_STARTED,
                    "updated_at": HISTORY_ACTIVE_STARTED,
                }
            )
        )
        (tmp_path / "Dockerfile").write_text(HISTORY_DOCKERFILE)
        build = _run("docker", "build", "-q", "-t", tag, str(tmp_path))
        assert build.returncode == 0, build.stderr

        started = _run("docker", "run", "-d", "--name", name, tag)
        assert started.returncode == 0, started.stderr

        import time

        deadline = time.time() + 30
        container_id = None
        while time.time() < deadline:
            found = asyncio.run(DockerGateway(timeout=10.0).list_instances())
            ids = {inst.name: inst.container_id for inst in found}
            if name in ids:
                container_id = ids[name]
                break
            time.sleep(1)
        assert container_id is not None

        exec_result = asyncio.run(DockerGateway(timeout=10.0).attempt_history(container_id))
        assert exec_result.returncode == 0
        result = read_history(exec_result.stdout, exec_result.returncode, now=NOW)
        assert result.ok
        assert result.total == 2
        assert result.unreadable == 0
        active, done = result.summaries
        assert active.issue == 98
        assert active.outcome_text == "active"
        assert active.cost_text == "n/a"
        assert done.outcome_text == "complete"
        assert done.cost_text == "$1.23"
        assert done.input_text == "120,000"
        assert done.duration_text == "28m 32s"
    finally:
        _run("docker", "rm", "-f", name)
        _run("docker", "rmi", "-f", tag)


LOG_DOCKERFILE = """\
FROM python:3.13-slim
LABEL io.github.lbacik.simple-coding-agent.role="agent"
COPY logs /data/logs
RUN useradd -m agent
CMD ["sleep", "infinity"]
"""

LOG_ISSUE = 41
LOG_STARTED_COLON = "2026-10-10T10:00:00.000000Z"
LOG_STARTED_DASH = "2026-10-10T10-00-00.000000Z"
LOG_SMALL_FILE = "0001_tool_call.json"
LOG_BIG_FILE = "0002_tool_result.json"
LOG_SMALL_CONTENT = b'{"tool": "read", "ok": true}\n'
LOG_BIG_CONTENT = b"0123456789ABCDEF" * 18750  # 300,000 B, over the 256 KiB cap


@docker
def test_attempt_log_against_real_docker(tmp_path):
    """Prove the Attempt log view's read paths against a real container.

    One throwaway container holds a single Implementation attempt with a
    small Evidence file and one file over the preview cap. The test lists
    attempts (``docker exec`` history collector), lists the run's files
    (``docker exec ls``), reads the small file in full, reads the big file
    over the cap (truncated preview with the true total), downloads the big
    file in full (``docker cp`` with no cap), and rejects traversal names
    before any Docker call.
    """
    assert len(LOG_BIG_CONTENT) > EVIDENCE_PREVIEW_CAP
    tag = f"agent-dashboard-logs:{uuid.uuid4().hex[:12]}"
    name = f"e2e-logs-{uuid.uuid4().hex[:8]}"
    try:
        run_dir = tmp_path / "logs" / str(LOG_ISSUE) / LOG_STARTED_DASH
        run_dir.mkdir(parents=True)
        (run_dir / "attempt.json").write_text(
            json.dumps(
                {
                    "outcome": "complete",
                    "started_at": LOG_STARTED_COLON,
                    "completed_at": "2026-10-10T10:31:00.000000Z",
                    "duration_seconds": 1860,
                }
            )
        )
        (run_dir / "agent_output.json").write_text(
            json.dumps(
                {
                    "timestamp": LOG_STARTED_COLON,
                    "level": "INFO",
                    "event": "tool_call",
                    "detail": f"read -> {LOG_SMALL_FILE}",
                }
            )
            + "\n"
        )
        (run_dir / LOG_SMALL_FILE).write_bytes(LOG_SMALL_CONTENT)
        (run_dir / LOG_BIG_FILE).write_bytes(LOG_BIG_CONTENT)
        (tmp_path / "Dockerfile").write_text(LOG_DOCKERFILE)
        build = _run("docker", "build", "-q", "-t", tag, str(tmp_path))
        assert build.returncode == 0, build.stderr

        started = _run("docker", "run", "-d", "--name", name, tag)
        assert started.returncode == 0, started.stderr

        import time

        deadline = time.time() + 30
        container_id = None
        while time.time() < deadline:
            found = asyncio.run(DockerGateway(timeout=10.0).list_instances())
            ids = {inst.name: inst.container_id for inst in found}
            if name in ids:
                container_id = ids[name]
                break
            time.sleep(1)
        assert container_id is not None

        gateway = DockerGateway(timeout=10.0)

        history_exec = asyncio.run(gateway.attempt_history(container_id))
        assert history_exec.returncode == 0
        history = read_history(history_exec.stdout, history_exec.returncode, now=NOW)
        assert history.ok
        assert history.total == 1
        (summary,) = history.summaries
        assert summary.issue == LOG_ISSUE
        assert summary.outcome_text == "complete"

        run_files = asyncio.run(
            gateway.list_run_files(container_id, LOG_ISSUE, LOG_STARTED_DASH)
        )
        assert LOG_SMALL_FILE in run_files
        assert LOG_BIG_FILE in run_files

        small = asyncio.run(
            gateway.read_file(container_id, LOG_ISSUE, LOG_STARTED_DASH, LOG_SMALL_FILE)
        )
        assert small.content == LOG_SMALL_CONTENT
        assert small.total_size == len(LOG_SMALL_CONTENT)
        assert not small.truncated

        big_preview = asyncio.run(
            gateway.read_file(container_id, LOG_ISSUE, LOG_STARTED_DASH, LOG_BIG_FILE)
        )
        assert big_preview.truncated
        assert big_preview.total_size == len(LOG_BIG_CONTENT)
        assert big_preview.content == LOG_BIG_CONTENT[:EVIDENCE_PREVIEW_CAP]

        big_full = asyncio.run(
            gateway.read_file(
                container_id, LOG_ISSUE, LOG_STARTED_DASH, LOG_BIG_FILE, cap=None
            )
        )
        assert big_full.content == LOG_BIG_CONTENT
        assert big_full.total_size == len(LOG_BIG_CONTENT)
        assert not big_full.truncated

        for traversal in (
            "..",
            "../escape.json",
            "transcripts/0001_tool_call.json",
            "0001_tool_call.json ",
            "/data/logs/41/x/0001_tool_call.json",
        ):
            with pytest.raises(ValueError):
                asyncio.run(
                    gateway.read_file(
                        container_id, LOG_ISSUE, LOG_STARTED_DASH, traversal
                    )
                )
    finally:
        _run("docker", "rm", "-f", name)
        _run("docker", "rmi", "-f", tag)


@docker
def test_stopped_instance_logs_via_docker_cp(tmp_path):
    """Stopped containers keep their Attempt logs readable via ``docker cp``.

    Reuses the LOG fixture image: after ``docker stop`` the exec readers
    fail, but ``copy_logs``/``copy_run`` still stream the trees and the
    stopped readers parse them (history list plus one attempt header).
    """
    from agent_dashboard.attempt_log import read_stopped_attempt_log
    from agent_dashboard.history import read_stopped_history

    tag = f"agent-dashboard-stopped:{uuid.uuid4().hex[:12]}"
    name = f"e2e-stopped-{uuid.uuid4().hex[:8]}"
    try:
        run_dir = tmp_path / "logs" / str(LOG_ISSUE) / LOG_STARTED_DASH
        run_dir.mkdir(parents=True)
        (run_dir / "attempt.json").write_text(
            json.dumps(
                {
                    "outcome": "complete",
                    "started_at": LOG_STARTED_COLON,
                    "duration_seconds": 1860,
                }
            )
        )
        (run_dir / "agent_output.json").write_text(
            json.dumps(
                {
                    "timestamp": LOG_STARTED_COLON,
                    "level": "INFO",
                    "event": "tool_call",
                    "detail": f"read -> {LOG_SMALL_FILE}",
                }
            )
            + "\n"
        )
        (run_dir / LOG_SMALL_FILE).write_bytes(LOG_SMALL_CONTENT)
        (tmp_path / "Dockerfile").write_text(LOG_DOCKERFILE)
        build = _run("docker", "build", "-q", "-t", tag, str(tmp_path))
        assert build.returncode == 0, build.stderr
        started = _run("docker", "run", "-d", "--name", name, tag)
        assert started.returncode == 0, started.stderr

        import time

        deadline = time.time() + 30
        container_id = None
        while time.time() < deadline:
            found = asyncio.run(DockerGateway(timeout=10.0).list_instances())
            ids = {inst.name: inst.container_id for inst in found}
            if name in ids:
                container_id = ids[name]
                break
            time.sleep(1)
        assert container_id is not None

        stopped = _run("docker", "stop", name)
        assert stopped.returncode == 0, stopped.stderr

        gateway = DockerGateway(timeout=10.0)
        tree = asyncio.run(gateway.copy_logs(container_id))
        history = read_stopped_history(tree, now=NOW)
        assert history.ok
        assert history.total == 1
        assert history.summaries[0].issue == LOG_ISSUE

        run_tree = asyncio.run(
            gateway.copy_run(container_id, LOG_ISSUE, LOG_STARTED_DASH)
        )
        result, names = read_stopped_attempt_log(
            run_tree, issue=LOG_ISSUE, started=LOG_STARTED_DASH
        )
        assert result.ok
        assert LOG_SMALL_FILE in names

        small = asyncio.run(
            gateway.read_file(container_id, LOG_ISSUE, LOG_STARTED_DASH, LOG_SMALL_FILE)
        )
        assert small.content == LOG_SMALL_CONTENT
    finally:
        _run("docker", "rm", "-f", name)
        _run("docker", "rmi", "-f", tag)
