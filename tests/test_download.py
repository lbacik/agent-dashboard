"""Attempt file downloads and the command-log header links.

Covers the spec's ``list_run_files`` gateway operation (exact ``ls -1``
argv, validation rejections before any spawn, run-not-found mapping), the
download route (the same ``read_file`` call with no cap, streamed to the
client chunk by chunk with the attachment headers, 404 without a Docker
call outside the allow-list, a clear message instead of a 500 otherwise),
and the attempt page header (only the command logs that exist in the run,
each opening in the same inline viewer, plus the ``agent_output.json``
download for streams past the timeline cap). Vocabulary: Attempt log,
Evidence file, Managed instance, Implementation attempt.
"""

from __future__ import annotations

import asyncio

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient
from test_attempt_log import STARTED_COLON, STARTED_DASH, _ev, _payload

from agent_dashboard.app import create_app
from agent_dashboard.config import load_settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    AttemptLogTimeoutError,
    DockerGateway,
    DockerNotAvailableError,
    FileResult,
    GatewayError,
    ManagedInstance,
    argv_list_run_files,
)

COMMAND_LOGS = ["setup_stdout.log", "check_stderr.log"]


# --- gateway: list_run_files argv ------------------------------------------------


def test_list_run_files_argv_pinned():
    assert argv_list_run_files("abc123", 98, STARTED_COLON) == [
        "docker",
        "exec",
        "--user",
        "agent",
        "abc123",
        "ls",
        "-1",
        f"/data/logs/98/{STARTED_DASH}",
    ]


def test_list_run_files_argv_accepts_dash_started():
    assert argv_list_run_files("abc123", "98", STARTED_DASH) == [
        "docker",
        "exec",
        "--user",
        "agent",
        "abc123",
        "ls",
        "-1",
        f"/data/logs/98/{STARTED_DASH}",
    ]


def test_list_run_files_argv_has_no_shell_or_path_from_request():
    argv = argv_list_run_files("abc123", 98, STARTED_COLON)
    assert argv[1:3] == ["exec", "--user"]
    assert argv[-2:] == ["-1", f"/data/logs/98/{STARTED_DASH}"]
    assert ".." not in argv[-1]


@pytest.mark.parametrize(
    "issue,started",
    [
        ("..", STARTED_DASH),
        ("98/..", STARTED_DASH),
        ("%2e%2e", STARTED_DASH),
        ("", STARTED_DASH),
        ("-1", STARTED_DASH),
        (98, ".."),
        (98, "/"),
        (98, "%2e%2e"),
        (98, ""),
        (98, "../state/attempt.json"),
        (98, "not-a-timestamp"),
        (98, "2026-09-27T16:33:43Z"),
        (98, None),
        (None, STARTED_DASH),
        ("a\nb", STARTED_DASH),
        ("latest", STARTED_DASH),
        (98, "a\nb"),
        (98, "latest"),
    ],
)
def test_list_run_files_argv_rejects_bad_values(issue, started):
    with pytest.raises(ValueError):
        argv_list_run_files("abc123", issue, started)


def test_list_run_files_rejects_bad_container_id():
    with pytest.raises(ValueError):
        argv_list_run_files("not a container!", 98, STARTED_DASH)


def test_list_run_files_validation_happens_before_any_spawn(monkeypatch):
    def explode(*argv, **kwargs):
        raise AssertionError("gateway spawned a process for an invalid reference")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", explode)
    for issue, started in [("..", STARTED_DASH), (98, ".."), ("", "")]:
        with pytest.raises(ValueError):
            asyncio.run(DockerGateway().list_run_files("abc123", issue, started))
    with pytest.raises(ValueError):
        asyncio.run(DockerGateway().list_run_files("not a container!", 98, STARTED_DASH))


# --- gateway: list_run_files behaviour -------------------------------------------


class _FakeExecProcess:
    """Fake ``docker exec`` process over canned stdout."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode
        self.killed: list[bool] = []
        self.reaped: list[bool] = []

    async def communicate(self):
        # Like the real ``communicate``: the process is reaped on return.
        self.reaped.append(True)
        return self._stdout, b""

    def kill(self):
        self.killed.append(True)

    async def wait(self):
        self.reaped.append(True)
        return self.returncode


def _run_list(proc, issue=98, started=STARTED_DASH):
    seen = {}

    async def fake_create(*argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return proc

    async def main(monkeypatch):
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
        return await DockerGateway().list_run_files("abc123", issue, started)

    return seen, main


def test_list_run_files_returns_stdout_lines(monkeypatch):
    proc = _FakeExecProcess(b"agent_output.json\nattempt.json\nsetup_stdout.log\n")
    seen, main = _run_list(proc)
    assert asyncio.run(main(monkeypatch)) == [
        "agent_output.json",
        "attempt.json",
        "setup_stdout.log",
    ]
    assert seen["argv"] == argv_list_run_files("abc123", 98, STARTED_DASH)
    assert "shell" not in seen["kwargs"]
    assert proc.reaped == [True]


def test_list_run_files_drops_empty_lines(monkeypatch):
    proc = _FakeExecProcess(b"check_stderr.log\n\n")
    _, main = _run_list(proc)
    assert asyncio.run(main(monkeypatch)) == ["check_stderr.log"]


def test_list_run_files_nonzero_exit_is_attempt_not_found(monkeypatch):
    proc = _FakeExecProcess(b"", returncode=1)
    _, main = _run_list(proc)
    with pytest.raises(FileNotFoundError, match="attempt not found"):
        asyncio.run(main(monkeypatch))
    assert proc.reaped == [True]


def test_list_run_files_timeout_kills_and_reaps(monkeypatch):
    class SlowProcess(_FakeExecProcess):
        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

    proc = SlowProcess()

    async def fake_create(*argv, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    async def main():
        return await DockerGateway(log_timeout=0.05).list_run_files(
            "abc123", 98, STARTED_DASH
        )

    with pytest.raises(AttemptLogTimeoutError, match="docker exec timed out"):
        asyncio.run(main())
    assert proc.killed == [True]
    assert proc.reaped == [True]


def test_list_run_files_missing_cli(monkeypatch):
    async def fake_create(*argv, **kwargs):
        raise FileNotFoundError("no docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(DockerNotAvailableError):
        asyncio.run(DockerGateway().list_run_files("abc123", 98, STARTED_DASH))


# --- web helpers -----------------------------------------------------------------


def make_client(rows, logs=None, run_files=None, files=None, errors=None):
    state = FleetState()
    instances = []
    for name, container_state in rows:
        raw_state = "running" if container_state == "running" else "exited"
        instances.append(ManagedInstance(name, f"id-{name}", raw_state))
    asyncio.run(state.apply_cycle(instances, {}, now=1000.0))
    gateway = FakeGateway()
    for container_id, payload in (logs or {}).items():
        gateway.set_log(container_id, payload)
    for key, names in (run_files or {}).items():
        gateway.set_run_files(*key, names)
    for key, result in (files or {}).items():
        gateway.set_file(*key, result)
    for key, error in (errors or {}).items():
        if len(key) == 3:
            gateway.set_run_files(*key, error)
        else:
            gateway.set_file(*key, error)
    app = create_app(
        state=state,
        settings=load_settings([]),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), gateway


def _file_result(content: bytes) -> FileResult:
    return FileResult(content, len(content), False)


def _page_url(name="work", started=STARTED_DASH):
    return f"/instances/{name}/attempts/98/{started}"


def _download_url(filename, name="work", started=STARTED_DASH):
    return f"{_page_url(name, started)}/files/{filename}/download"


# --- download route ---------------------------------------------------------------


def test_download_returns_the_whole_file_with_attachment_headers():
    content = b'{"tool": "Bash", "ok": true}' * 1000
    client, gateway = make_client(
        [("work", "running")],
        files={("id-work", "98", STARTED_DASH, "0003_tool_call.json"): _file_result(content)},
    )
    response = client.get(_download_url("0003_tool_call.json"))
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    # Streamed to the client, not one buffered body: no content-length.
    assert "content-length" not in response.headers
    # The same read_file operation with no cap.
    assert gateway.file_calls == [("id-work", "98", STARTED_DASH, "0003_tool_call.json")]


def test_download_returns_files_over_the_preview_cap_in_full():
    from agent_dashboard.gateway import EVIDENCE_PREVIEW_CAP

    content = b"y" * (EVIDENCE_PREVIEW_CAP + 5000)
    client, _ = make_client(
        [("work", "running")],
        files={("id-work", "98", STARTED_DASH, "check_stdout.log"): _file_result(content)},
    )
    response = client.get(_download_url("check_stdout.log"))
    assert response.status_code == 200
    assert response.content == content


@pytest.mark.parametrize(
    "filename",
    [
        "nope.txt",
        "%2e%2e",
        "12_tool_call.json",
        "-0003_tool_call.json",
        "0003_tool_call.json.bak",
        "notes.txt",
    ],
)
def test_download_outside_allow_list_is_404_without_docker_call(filename):
    client, gateway = make_client([("work", "running")])
    response = client.get(_download_url(filename))
    assert response.status_code == 404
    assert "Unknown Evidence file" in response.text
    assert gateway.file_calls == []
    assert gateway.run_files_calls == []
    assert gateway.log_calls == []


def test_download_allow_list_checked_before_instance_lookup():
    client, gateway = make_client([])
    response = client.get(_download_url("nope.txt", name="nope"))
    assert response.status_code == 404
    assert gateway.file_calls == []


def test_download_missing_file_is_a_message_not_500():
    client, _ = make_client([("work", "running")])
    response = client.get(_download_url("0003_tool_call.json"))
    assert response.status_code == 200
    assert "was not found in this attempt" in response.text
    assert response.headers["content-type"] != "application/octet-stream"


def test_download_unknown_and_stopped_instances_are_200_not_500():
    client, gateway = make_client([("work", "running")])
    unknown = client.get(_download_url("0003_tool_call.json", name="nope"))
    assert unknown.status_code == 200
    assert "Unknown Managed instance" in unknown.text
    stopped, stopped_gateway = make_client([("down", "not running")])
    body = stopped.get(_download_url("0003_tool_call.json", name="down"))
    assert body.status_code == 200
    assert "running managed instances only" in body.text
    assert gateway.file_calls == []
    assert stopped_gateway.file_calls == []


def test_download_invalid_reference_and_gateway_error_make_no_500():
    client, gateway = make_client([("work", "running")])
    bad = client.get(
        "/instances/work/attempts/98/not-a-timestamp/files/0003_tool_call.json/download"
    )
    assert bad.status_code == 200
    assert "Invalid attempt reference" in bad.text
    assert gateway.file_calls == []
    err_client, _ = make_client(
        [("work", "running")],
        errors={
            ("id-work", "98", STARTED_DASH, "0003_tool_call.json"): GatewayError(
                "docker exploded"
            )
        },
    )
    response = err_client.get(_download_url("0003_tool_call.json"))
    assert response.status_code == 200
    assert "Could not download the Evidence file" in response.text


def test_download_accepts_colon_form_started():
    content = b"hello"
    client, gateway = make_client(
        [("work", "running")],
        files={("id-work", "98", STARTED_DASH, "attempt.json"): _file_result(content)},
    )
    response = client.get(_download_url("attempt.json", started=STARTED_COLON))
    assert response.status_code == 200
    assert response.content == content
    assert gateway.file_calls == [("id-work", "98", STARTED_DASH, "attempt.json")]


# --- attempt page header: command logs --------------------------------------------


def _client_with_run_files(names, events=None):
    if events is None:
        events = [_ev("tool_call", "Bash [main] -> 0003_tool_call.json")]
    return make_client(
        [("work", "running")],
        logs={"id-work": _payload(events)},
        run_files={("id-work", "98", STARTED_DASH): names},
    )


def test_header_lists_only_the_command_logs_that_exist():
    client, gateway = _client_with_run_files(
        [
            "agent_output.json",
            "attempt.json",
            "setup_stdout.log",
            "check_stderr.log",
            "notes.txt",
            "0003_tool_call.json",
        ]
    )
    body = client.get(_page_url()).text
    assert gateway.run_files_calls == [("id-work", "98", STARTED_DASH)]
    for filename in COMMAND_LOGS:
        assert filename in body
        assert f"{_page_url()}/files/{filename}" in body
    assert "notes.txt" not in body
    assert "Command logs:" in body


def test_header_command_logs_open_in_the_inline_viewer():
    client, _ = _client_with_run_files(["setup_stdout.log"])
    body = client.get(_page_url()).text
    assert 'data-slot="command-log-slot"' in body
    assert 'id="command-log-slot"' in body
    assert "toggleEvidence" in body


def test_header_without_command_logs_shows_no_command_log_block():
    client, _ = _client_with_run_files(["agent_output.json", "attempt.json"])
    body = client.get(_page_url()).text
    assert "Command logs:" not in body


def test_header_lists_command_logs_even_when_the_stream_is_missing():
    client, _ = make_client(
        [("work", "running")],
        logs={
            "id-work": {
                "layout": "unknown",
                "reason": "missing agent_output.json",
            }
        },
        run_files={("id-work", "98", STARTED_DASH): ["setup_stderr.log"]},
    )
    body = client.get(_page_url()).text
    assert "layout not recognised" in body
    assert "setup_stderr.log" in body


def test_header_tolerates_a_run_listing_error():
    client, _ = make_client(
        [("work", "running")],
        logs={"id-work": _payload([_ev("tool_call", "plain detail")])},
        errors={("id-work", "98", STARTED_DASH): GatewayError("docker exploded")},
    )
    response = client.get(_page_url())
    assert response.status_code == 200
    assert "Command logs:" not in response.text


def test_attempt_page_links_the_full_stream_download():
    client, _ = _client_with_run_files(["agent_output.json"])
    body = client.get(_page_url()).text
    assert f"{_page_url()}/files/agent_output.json/download" in body


# --- preview bar: download link ----------------------------------------------------


def test_preview_bar_carries_the_download_link():
    content = b'{"tool": "Bash"}'
    client, _ = make_client(
        [("work", "running")],
        files={("id-work", "98", STARTED_DASH, "0003_tool_call.json"): _file_result(content)},
    )
    body = client.get(f"{_page_url()}/files/0003_tool_call.json").text
    expected = 'href="' + _download_url("0003_tool_call.json") + '"'
    assert expected in body
    assert ">download<" in body


def test_command_log_opens_in_the_same_inline_viewer():
    from agent_dashboard.gateway import EVIDENCE_PREVIEW_CAP

    content = b"ok\n" * 10
    client, _ = make_client(
        [("work", "running")],
        files={("id-work", "98", STARTED_DASH, "setup_stdout.log"): _file_result(content)},
    )
    body = client.get(f"{_page_url()}/files/setup_stdout.log").text
    assert "evidence-preview" in body
    assert "ok" in body
    assert f"truncated at {EVIDENCE_PREVIEW_CAP // 1024} KB" not in body


def test_command_log_preview_over_cap_shows_truncation_marker():
    from agent_dashboard.gateway import EVIDENCE_PREVIEW_CAP

    total = 12_800_000
    client, _ = make_client(
        [("work", "running")],
        files={
            ("id-work", "98", STARTED_DASH, "check_stdout.log"): FileResult(
                b"y" * EVIDENCE_PREVIEW_CAP, total, True
            )
        },
    )
    body = client.get(f"{_page_url()}/files/check_stdout.log").text
    assert f"truncated at {EVIDENCE_PREVIEW_CAP // 1024} KB, {total} B total" in body
