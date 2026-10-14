"""Docker-backed integration test: skipped where Docker is not available.

Builds a minimal image carrying the agent discovery label whose
``/usr/local/bin/agentctl`` is a stub script, then runs one container
printing a good snapshot fixture and one behaving like an old agent
without the ``telemetry`` subcommand.
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
from agent_dashboard.gateway import AGENT_ROLE_LABEL, DockerGateway

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
RUN chmod +x /usr/local/bin/agentctl
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
