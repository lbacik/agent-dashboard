"""Docker-backed integration test: skipped where no Docker CLI is available."""

import shutil

import pytest

docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker CLI not available"
)


@docker
def test_discovery_lists_labeled_containers():
    # Requires a Docker daemon with agent containers present; CI provides it.
    import asyncio

    from agent_dashboard.gateway import DockerGateway

    rows = asyncio.run(DockerGateway(timeout=5.0).list_instances())
    assert isinstance(rows, list)
