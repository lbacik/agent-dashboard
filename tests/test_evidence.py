"""Evidence file inline preview: gateway read, reference parser, fragment route.

Covers the spec's ``read_file`` gateway operation (exact ``docker cp``
argv, tar stream handling with the 256 KiB cap, kill after the cap, missing
file mapping, every allow-list rejection), the evidence reference parser
(thread tag optional, no ``->`` stays plain, non-allow-listed names never
link, skill events stay plain), the formatted/raw preview rendering, and the
htmx fragment route beside the attempt page. Vocabulary: Attempt log,
Evidence file, Managed instance, Implementation attempt.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient
from test_attempt_log import STARTED_COLON, STARTED_DASH, _ev, _payload

from agent_dashboard import gateway as gateway_module
from agent_dashboard.app import create_app
from agent_dashboard.attempt_log import (
    EvidenceRef,
    format_preview,
    parse_evidence_ref,
    read_attempt_log,
)
from agent_dashboard.config import load_settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    EVIDENCE_PREVIEW_CAP,
    AttemptLogTimeoutError,
    DockerGateway,
    DockerNotAvailableError,
    FileResult,
    GatewayError,
    ManagedInstance,
    argv_read_file,
    validate_log_file,
)

FILE_JSON = "0003_tool_call.json"
FILE_RESULT = "0004_tool_result.json"
FILE_MODEL = "0001_model_response.txt"


def _tar_bytes(entries) -> bytes:
    """Build a real tar archive: ``entries`` maps name to (bytes, typeflag).

    A ``typeflag`` of ``None`` means a regular file.
    """
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


def _single_file_tar(name: str, data: bytes) -> bytes:
    return _tar_bytes({name: (data, None)})


# --- allow-list ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "0003_tool_call.json",
        "0004_tool_result.json",
        "0001_model_response.txt",
        "123456_tool_call.json",
        "agent_output.json",
        "attempt.json",
        "setup_stdout.log",
        "setup_stderr.log",
        "baseline_check_stdout.log",
        "baseline_check_stderr.log",
        "check_stdout.log",
        "check_stderr.log",
    ],
)
def test_validate_log_file_accepts_the_allow_list(name):
    assert validate_log_file(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "/",
        "%2e%2e",
        "a\nb",
        "",
        "-0003_tool_call.json",
        "0003_tool_call.json -> /etc/passwd",
        "evil_link",
        "12_tool_call.json",
        "0003_tool_call.json.bak",
        "0003_tool_call.json ",
        " 0003_tool_call.json",
        "notes.txt",
        "0003_tool_call.JSON",
        "transcripts/0003_tool_call.json",
        "/data/logs/98/x/0003_tool_call.json",
        "../attempt.json",
        "setup_stdout.log ",
        "check_middle_stderr.log",
        "agent_output.json\n",
    ],
)
def test_validate_log_file_rejects_everything_else(name):
    with pytest.raises(ValueError):
        validate_log_file(name)


def test_validate_log_file_rejects_non_strings():
    for bad in (None, 0, b"0003_tool_call.json", ["0003_tool_call.json"]):
        with pytest.raises(ValueError):
            validate_log_file(bad)


# --- argv ----------------------------------------------------------------------


def test_read_file_argv_pinned():
    assert argv_read_file("abc123", 98, STARTED_COLON, FILE_JSON) == [
        "docker",
        "cp",
        f"abc123:/data/logs/98/{STARTED_DASH}/{FILE_JSON}",
        "-",
    ]


def test_read_file_argv_accepts_dash_started():
    argv = argv_read_file("abc123", "98", STARTED_DASH, FILE_MODEL)
    assert argv == [
        "docker",
        "cp",
        f"abc123:/data/logs/98/{STARTED_DASH}/{FILE_MODEL}",
        "-",
    ]


def test_read_file_argv_is_never_follow_or_list():
    argv = argv_read_file("abc123", 98, STARTED_COLON, FILE_JSON)
    assert "-L" not in argv
    assert "--follow-link" not in argv
    assert argv[-1] == "-"
    assert argv[1] == "cp"


@pytest.mark.parametrize(
    "issue,started,filename",
    [
        ("..", STARTED_DASH, FILE_JSON),
        ("98/..", STARTED_DASH, FILE_JSON),
        ("%2e%2e", STARTED_DASH, FILE_JSON),
        ("", STARTED_DASH, FILE_JSON),
        ("-1", STARTED_DASH, FILE_JSON),
        (98, "..", FILE_JSON),
        (98, "/", FILE_JSON),
        (98, "%2e%2e", FILE_JSON),
        (98, "", FILE_JSON),
        (98, "../state/attempt.json", FILE_JSON),
        (98, "not-a-timestamp", FILE_JSON),
        (98, STARTED_DASH, ".."),
        (98, STARTED_DASH, "/"),
        (98, STARTED_DASH, "%2e%2e"),
        (98, STARTED_DASH, ""),
        (98, STARTED_DASH, "-0003_tool_call.json"),
        (98, STARTED_DASH, "0003_tool_call.json -> /etc/passwd"),
        (98, STARTED_DASH, "evil_link"),
        (98, STARTED_DASH, "12_tool_call.json"),
        (98, STARTED_DASH, "notes.txt"),
        (98, STARTED_DASH, "a\nb"),
    ],
)
def test_read_file_argv_rejects_bad_values(issue, started, filename):
    with pytest.raises(ValueError):
        argv_read_file("abc123", issue, started, filename)


def test_read_file_rejects_bad_container_id():
    with pytest.raises(ValueError):
        argv_read_file("not a container!", 98, STARTED_DASH, FILE_JSON)


def test_read_file_validation_happens_before_any_spawn(monkeypatch):
    def explode(*argv, **kwargs):
        raise AssertionError("gateway spawned a process for an invalid file")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", explode)
    for filename in ("..", "", "-x", "notes.txt", "a\nb"):
        with pytest.raises(ValueError):
            asyncio.run(
                DockerGateway().read_file("abc123", 98, STARTED_DASH, filename)
            )
    with pytest.raises(ValueError):
        asyncio.run(DockerGateway().read_file("abc123", 98, STARTED_DASH, FILE_JSON, cap=0))


# --- tar stream handling -------------------------------------------------------


class _ChunkReader:
    """Async stdout stub: yields the tar stream in small chunks."""

    def __init__(self, data: bytes, chunk_size: int = 7000):
        self._data = data
        self._chunk_size = chunk_size
        self.bytes_read = 0

    async def read(self, n: int) -> bytes:
        if not self._data:
            return b""
        chunk, self._data = self._data[:n], self._data[n:]
        piece, chunk = chunk[: self._chunk_size], chunk[self._chunk_size :]
        self._data = chunk + self._data
        self.bytes_read += len(piece)
        return piece


class _FakeCpProcess:
    """Fake ``docker cp`` process over canned tar bytes."""

    def __init__(
        self,
        stdout: bytes,
        returncode: int = 0,
        stderr: bytes = b"",
        chunk_size: int = 7000,
    ):
        self.stdout = _ChunkReader(stdout, chunk_size)
        self._stderr = stderr
        self.returncode = returncode
        self.killed: list[bool] = []
        self.reaped: list[bool] = []

    def kill(self):
        self.killed.append(True)

    async def wait(self):
        self.reaped.append(True)
        return self.returncode

    @property
    def stderr(self):
        parent = self

        class _Reader:
            async def read(self, n: int = -1) -> bytes:
                return parent._stderr

        return _Reader()


def _run_read_file(proc, filename=FILE_JSON, cap=EVIDENCE_PREVIEW_CAP, issue=98):
    seen = {}

    async def fake_create(*argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return proc

    async def main(monkeypatch):
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
        return await DockerGateway().read_file(
            "abc123", issue, STARTED_DASH, filename, cap=cap
        )

    return seen, main


def test_read_file_small_file_returns_content_and_header_size(monkeypatch):
    content = b'{"tool": "Bash", "ok": true}'
    proc = _FakeCpProcess(_single_file_tar(FILE_JSON, content))
    seen, main = _run_read_file(proc)
    result = asyncio.run(main(monkeypatch))
    assert seen["argv"] == argv_read_file("abc123", 98, STARTED_DASH, FILE_JSON)
    assert "shell" not in seen["kwargs"]
    assert result.content == content
    assert result.total_size == len(content)
    assert result.truncated is False
    assert proc.killed == []
    assert proc.reaped == [True]


def test_read_file_over_cap_truncates_kills_and_reaps(monkeypatch):
    content = b"y" * (EVIDENCE_PREVIEW_CAP + 200_000)
    tar = _single_file_tar(FILE_JSON, content)
    proc = _FakeCpProcess(tar)
    seen, main = _run_read_file(proc)
    result = asyncio.run(main(monkeypatch))
    assert result.content == content[:EVIDENCE_PREVIEW_CAP]
    assert result.total_size == len(content)
    assert result.truncated is True
    # The process is killed once the cap is reached, not drained.
    assert proc.killed == [True]
    assert proc.reaped == [True]
    assert proc.stdout.bytes_read <= 512 + EVIDENCE_PREVIEW_CAP + 65536
    assert proc.stdout.bytes_read < len(tar)


def test_read_file_one_megabyte_reports_true_total(monkeypatch):
    content = b"z" * (1024 * 1024)
    proc = _FakeCpProcess(_single_file_tar(FILE_JSON, content))
    _, main = _run_read_file(proc)
    result = asyncio.run(main(monkeypatch))
    assert result.content == content[:EVIDENCE_PREVIEW_CAP]
    assert result.total_size == 1024 * 1024
    assert result.truncated is True
    assert proc.killed == [True]


def test_read_file_exact_cap_is_not_truncated(monkeypatch):
    content = b"q" * EVIDENCE_PREVIEW_CAP
    proc = _FakeCpProcess(_single_file_tar(FILE_JSON, content))
    _, main = _run_read_file(proc)
    result = asyncio.run(main(monkeypatch))
    assert result.content == content
    assert result.total_size == EVIDENCE_PREVIEW_CAP
    assert result.truncated is False
    assert proc.killed == []


def test_read_file_without_cap_reads_the_whole_file(monkeypatch):
    content = b"w" * (EVIDENCE_PREVIEW_CAP + 5000)
    proc = _FakeCpProcess(_single_file_tar(FILE_JSON, content))
    _, main = _run_read_file(proc, cap=None)
    result = asyncio.run(main(monkeypatch))
    assert result.content == content
    assert result.total_size == len(content)
    assert result.truncated is False


def test_read_file_non_tar_input_is_layout_not_recognised(monkeypatch):
    proc = _FakeCpProcess(b"this is not a tar stream, just text" * 40)
    _, main = _run_read_file(proc)
    with pytest.raises(GatewayError, match="layout not recognised"):
        asyncio.run(main(monkeypatch))
    assert proc.reaped == [True]


def test_read_file_empty_stream_is_layout_not_recognised(monkeypatch):
    proc = _FakeCpProcess(b"")
    _, main = _run_read_file(proc)
    with pytest.raises(GatewayError, match="layout not recognised"):
        asyncio.run(main(monkeypatch))


def test_read_file_directory_member_is_layout_not_recognised(monkeypatch):
    proc = _FakeCpProcess(_tar_bytes({"some_dir": (b"", tarfile.DIRTYPE)}))
    _, main = _run_read_file(proc)
    with pytest.raises(GatewayError, match="layout not recognised"):
        asyncio.run(main(monkeypatch))
    assert proc.reaped == [True]


def test_read_file_symlink_member_is_layout_not_recognised(monkeypatch):
    proc = _FakeCpProcess(_tar_bytes({FILE_JSON: (b"", tarfile.SYMTYPE)}))
    _, main = _run_read_file(proc)
    with pytest.raises(GatewayError, match="layout not recognised"):
        asyncio.run(main(monkeypatch))
    assert proc.reaped == [True]


def test_read_file_several_members_is_layout_not_recognised(monkeypatch):
    tar = _tar_bytes(
        {FILE_JSON: (b"first", None), FILE_RESULT: (b"second", None)}
    )
    proc = _FakeCpProcess(tar)
    _, main = _run_read_file(proc)
    with pytest.raises(GatewayError, match="layout not recognised"):
        asyncio.run(main(monkeypatch))


def test_read_file_missing_file_maps_exit_status(monkeypatch):
    proc = _FakeCpProcess(
        b"",
        returncode=1,
        stderr=b"Error: Could not find the file 0003_tool_call.json in container abc123\n",
    )
    _, main = _run_read_file(proc)
    with pytest.raises(FileNotFoundError, match="not found"):
        asyncio.run(main(monkeypatch))
    assert proc.reaped == [True]


def test_read_file_nonzero_exit_without_the_message_is_layout(monkeypatch):
    proc = _FakeCpProcess(b"garbage", returncode=1, stderr=b"daemon exploded\n")
    _, main = _run_read_file(proc)
    with pytest.raises(GatewayError, match="layout not recognised"):
        asyncio.run(main(monkeypatch))


def test_read_file_timeout_kills_and_reaps(monkeypatch):
    class SlowReader:
        async def read(self, n: int) -> bytes:
            await asyncio.sleep(10)
            return b""

    class SlowProcess(_FakeCpProcess):
        def __init__(self):
            super().__init__(b"")
            self.stdout = SlowReader()

    async def fake_create(*argv, **kwargs):
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    proc_holder: dict = {}

    async def main():
        gateway = DockerGateway(log_timeout=0.05)
        original_run = gateway._run

        async def spy_run(argv):
            proc = await original_run(argv)
            proc_holder["proc"] = proc
            return proc

        monkeypatch.setattr(gateway, "_run", spy_run)
        return await gateway.read_file("abc123", 98, STARTED_DASH, FILE_JSON)

    with pytest.raises(AttemptLogTimeoutError, match="docker cp timed out"):
        asyncio.run(main())
    assert proc_holder["proc"].killed == [True]
    assert proc_holder["proc"].reaped == [True]


def test_read_file_missing_cli(monkeypatch):
    async def fake_create(*argv, **kwargs):
        raise FileNotFoundError("no docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(DockerNotAvailableError):
        asyncio.run(DockerGateway().read_file("abc123", 98, STARTED_DASH, FILE_JSON))


def test_read_file_shares_the_log_semaphore(monkeypatch):
    in_flight = 0
    peak = 0

    class SlowProcess:
        def __init__(self):
            self.returncode = 0
            self._stderr = b""

            class _Reader:
                async def read(inner_self, n: int) -> bytes:
                    nonlocal in_flight, peak
                    in_flight += 1
                    peak = max(peak, in_flight)
                    try:
                        await asyncio.sleep(0.02)
                        return b""
                    finally:
                        in_flight -= 1

            self.stdout = _Reader()

        def kill(self):
            pass

        async def wait(self):
            return self.returncode

        @property
        def stderr(self):
            parent = self

            class _Reader:
                async def read(self, n: int = -1) -> bytes:
                    return parent._stderr

            return _Reader()

    async def fake_create(*argv, **kwargs):
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    async def main():
        gateway = DockerGateway()
        await asyncio.gather(
            *[
                gateway.read_file("abc123", 98, STARTED_DASH, FILE_JSON)
                for _ in range(8)
            ]
        )

    with pytest.raises(GatewayError):
        # Empty streams are layout errors; the semaphore bound is the point.
        asyncio.run(main())
    assert peak <= gateway_module.LOG_MAX_CONCURRENT == 4
    assert peak > 1


# --- evidence reference parser -------------------------------------------------


@pytest.mark.parametrize(
    "event,detail,tool,thread,file",
    [
        ("tool_call", "Bash [main] -> 0003_tool_call.json", "Bash", "main", FILE_JSON),
        (
            "tool_result",
            "Read [subagent:abc123] -> 0004_tool_result.json",
            "Read",
            "subagent:abc123",
            FILE_RESULT,
        ),
        ("tool_call", "Bash -> 0002_tool_call.json", "Bash", None, "0002_tool_call.json"),
        (
            "model_response",
            "-> 0001_model_response.txt (chars=85)",
            "",
            None,
            FILE_MODEL,
        ),
        (
            "model_response",
            "reply -> 0123_model_response.txt",
            "reply",
            None,
            "0123_model_response.txt",
        ),
        (
            "tool_call",
            "Bash [subagent:worker-1] -> 123456_tool_call.json",
            "Bash",
            "subagent:worker-1",
            "123456_tool_call.json",
        ),
        # A second ``->`` suffix is ignored like any other suffix: the first
        # reference wins and the file is validated again in the route.
        (
            "tool_call",
            "Bash [main] -> 0003_tool_call.json -> /tmp/x",
            "Bash",
            "main",
            FILE_JSON,
        ),
    ],
)
def test_parse_evidence_ref_links_allow_listed_files(event, detail, tool, thread, file):
    ref = parse_evidence_ref(event, detail)
    assert ref == EvidenceRef(tool=tool, thread=thread, file=file)


@pytest.mark.parametrize(
    "event,detail",
    [
        ("tool_call", 'Bash [main] {"command": "ls"}'),
        ("tool_result", "no arrow here"),
        ("model_response", ""),
        ("tool_call", "Bash [main] -> notes.txt"),
        ("tool_call", "Bash [main] -> 12_tool_call.json"),
        ("tool_call", "Bash [main] -> ../../../etc/passwd"),
        ("tool_call", "Bash [main] -> 0003_tool_call.json.bak"),
        ("tool_result", "Read [main] -> agent_output.json"),
        ("skill_call", "Bash [main] -> 0003_tool_call.json"),
        ("skill_result", "Bash [main] -> 0004_tool_result.json"),
        ("token_budget_reconciled", "Bash [main] -> 0003_tool_call.json"),
        ("unknown", "Bash [main] -> 0003_tool_call.json"),
        ("tool_call", None),
        ("tool_call", 42),
        (None, "Bash [main] -> 0003_tool_call.json"),
        ("", "Bash [main] -> 0003_tool_call.json"),
    ],
)
def test_parse_evidence_ref_stays_plain_otherwise(event, detail):
    assert parse_evidence_ref(event, detail) is None


def test_event_rows_carry_their_evidence_reference():
    payload = {
        "layout": "attempt-log/v1",
        "issue": 98,
        "started": STARTED_DASH,
        "attempt": {"outcome": "complete"},
        "events": [
            _ev("tool_call", "Bash [main] -> 0003_tool_call.json"),
            _ev("tool_call", "Bash [main] inline payload"),
            _ev("skill_call", "Bash [main] -> 0003_tool_call.json"),
        ],
        "total_events": 3,
        "unreadable": 0,
        "truncated": False,
        "file_size": 0,
    }
    result = read_attempt_log(json.dumps(payload).encode(), 0, issue=98, started=STARTED_DASH)
    assert result.ok
    assert result.events[0].evidence == EvidenceRef(
        tool="Bash", thread="main", file=FILE_JSON
    )
    assert result.events[1].evidence is None
    assert result.events[2].evidence is None


# --- preview formatting --------------------------------------------------------


def test_format_preview_pretty_prints_json():
    assert (
        format_preview(FILE_JSON, b'{"b":2,"a":[1,2]}', False)
        == '{\n  "b": 2,\n  "a": [\n    1,\n    2\n  ]\n}'
    )


def test_format_preview_falls_back_to_raw_text():
    assert format_preview(FILE_JSON, b"not json{", False) == "not json{"
    assert format_preview(FILE_JSON, b'{"a": 1}', True) == '{"a": 1}'
    assert format_preview(FILE_MODEL, b'{"a": 1}', False) == '{"a": 1}'
    assert format_preview(FILE_JSON, b"", False) == ""


def test_format_preview_decodes_as_read():
    assert format_preview(FILE_MODEL, "héllo wörld".encode(), False) == "héllo wörld"
    assert format_preview(FILE_JSON, b"\xff\xfe binary", False) == "�� binary"


# --- fragment route ------------------------------------------------------------


def make_file_client(rows, logs=None, files=None, files_error=None):
    state = FleetState()
    instances = []
    for name, container_state in rows:
        raw_state = "running" if container_state == "running" else "exited"
        instances.append(ManagedInstance(name, f"id-{name}", raw_state))
    asyncio.run(state.apply_cycle(instances, {}, now=1000.0))
    gateway = FakeGateway()
    for container_id, payload in (logs or {}).items():
        gateway.set_log(container_id, payload)
    for key, result in (files or {}).items():
        gateway.set_file(*key, result)
    if files_error is not None:
        for key, error in files_error.items():
            gateway.set_file(*key, error)
    app = create_app(
        state=state,
        settings=load_settings([]),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def _log_with(*events):
    return _payload(list(events))


def _file_result(content: bytes, total: int | None = None, truncated: bool = False):
    return FileResult(content, len(content) if total is None else total, truncated)


def test_fragment_renders_formatted_json_with_size_bar():
    content = b'{"tool":"Bash","ok":true}'
    client, _, gateway = make_file_client(
        [("work", "running")],
        files={("id-work", "98", STARTED_DASH, FILE_JSON): _file_result(content)},
    )
    response = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/files/{FILE_JSON}")
    assert response.status_code == 200
    body = response.text
    assert "evidence-preview" in body
    assert f"{len(content)} B" in body
    assert "raw" in body
    assert "?raw=1" in body
    # Formatted: pretty-printed JSON.
    assert "&#34;tool&#34;: &#34;Bash&#34;" in body
    assert f"truncated at {EVIDENCE_PREVIEW_CAP // 1024} KB" not in body
    assert gateway.file_calls == [("id-work", "98", STARTED_DASH, FILE_JSON)]


def test_fragment_raw_query_shows_text_as_read():
    content = b'{"tool":"Bash","ok":true}'
    client, _, _ = make_file_client(
        [("work", "running")],
        files={("id-work", "98", STARTED_DASH, FILE_JSON): _file_result(content)},
    )
    body = client.get(
        f"/instances/work/attempts/98/{STARTED_DASH}/files/{FILE_JSON}?raw=1"
    ).text
    assert "{&#34;tool&#34;:&#34;Bash&#34;,&#34;ok&#34;:true}" in body
    assert "formatted" in body


def test_fragment_over_cap_shows_truncation_marker_with_true_total():
    total = 5_000_000
    client, _, _ = make_file_client(
        [("work", "running")],
        files={
            ("id-work", "98", STARTED_DASH, FILE_JSON): FileResult(
                b"y" * EVIDENCE_PREVIEW_CAP, total, True
            )
        },
    )
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/files/{FILE_JSON}").text
    assert f"truncated at {EVIDENCE_PREVIEW_CAP // 1024} KB, {total} B total" in body
    assert f"{EVIDENCE_PREVIEW_CAP} B" in body


def test_fragment_renders_file_text_as_text_not_html():
    content = b"<script>alert(1)</script>"
    client, _, _ = make_file_client(
        [("work", "running")],
        files={
            ("id-work", "98", STARTED_DASH, FILE_MODEL): _file_result(content)
        },
    )
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/files/{FILE_MODEL}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<script>alert(1)</script>" not in body


def test_fragment_model_response_uses_raw_text():
    content = b"the model said hello"
    client, _, _ = make_file_client(
        [("work", "running")],
        files={
            ("id-work", "98", STARTED_DASH, FILE_MODEL): _file_result(content)
        },
    )
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/files/{FILE_MODEL}").text
    assert "the model said hello" in body


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
def test_fragment_outside_allow_list_is_404_without_docker_call(filename):
    client, _, gateway = make_file_client([("work", "running")])
    response = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/files/{filename}")
    assert response.status_code == 404
    assert "Unknown Evidence file" in response.text
    assert gateway.file_calls == []
    assert gateway.log_calls == []


def test_fragment_allow_list_checked_before_instance_lookup():
    client, _, gateway = make_file_client([])
    response = client.get(f"/instances/nope/attempts/98/{STARTED_DASH}/files/nope.txt")
    assert response.status_code == 404
    assert gateway.file_calls == []


def test_fragment_unknown_and_stopped_instances_are_200_not_500():
    content = b'{"tool": "Bash", "ok": true}'
    client, _, gateway = make_file_client([("work", "running")])
    unknown = client.get(f"/instances/nope/attempts/98/{STARTED_DASH}/files/{FILE_JSON}")
    assert unknown.status_code == 200
    assert "Unknown Managed instance" in unknown.text
    stopped, _, stopped_gateway = make_file_client(
        [("down", "not running")],
        files={("id-down", "98", STARTED_DASH, FILE_JSON): _file_result(content)},
    )
    response = stopped.get(f"/instances/down/attempts/98/{STARTED_DASH}/files/{FILE_JSON}")
    assert response.status_code == 200
    assert "evidence-preview" in response.text
    assert stopped_gateway.file_calls == [("id-down", "98", STARTED_DASH, FILE_JSON)]
    missing, _, missing_gateway = make_file_client([("down", "not running")])
    body = missing.get(f"/instances/down/attempts/98/{STARTED_DASH}/files/{FILE_JSON}").text
    assert "was not found in this attempt" in body
    assert missing_gateway.file_calls == [("id-down", "98", STARTED_DASH, FILE_JSON)]
    assert gateway.file_calls == []


def test_fragment_invalid_reference_and_gateway_error_make_no_500():
    client, _, gateway = make_file_client([("work", "running")])
    bad = client.get(f"/instances/work/attempts/98/not-a-timestamp/files/{FILE_JSON}")
    assert bad.status_code == 200
    assert "Invalid attempt reference" in bad.text
    assert gateway.file_calls == []
    err_client, _, _ = make_file_client(
        [("work", "running")],
        files_error={
            ("id-work", "98", STARTED_DASH, FILE_JSON): GatewayError("docker exploded")
        },
    )
    response = err_client.get(
        f"/instances/work/attempts/98/{STARTED_DASH}/files/{FILE_JSON}"
    )
    assert response.status_code == 200
    assert "Could not show the Evidence file" in response.text


def test_fragment_missing_file_is_a_message_not_500():
    client, _, _ = make_file_client([("work", "running")])
    response = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/files/{FILE_JSON}")
    assert response.status_code == 200
    assert "was not found in this attempt" in response.text


def test_fragment_accepts_colon_form_started():
    content = b"{}"
    client, _, gateway = make_file_client(
        [("work", "running")],
        files={("id-work", "98", STARTED_DASH, FILE_JSON): FileResult(content, 2, False)},
    )
    response = client.get(f"/instances/work/attempts/98/{STARTED_COLON}/files/{FILE_JSON}")
    assert response.status_code == 200
    assert gateway.file_calls == [("id-work", "98", STARTED_DASH, FILE_JSON)]


def test_attempt_page_renders_plain_rows_without_references():
    events = [
        _ev("tool_call", 'Bash [main] {"command": "ls"}'),
        _ev("tool_call", "Bash [main] -> notes.txt"),
        _ev("skill_call", "Bash [main] -> 0003_tool_call.json"),
        _ev("model_response", "-> 0001_model_response.txt (chars=85)"),
    ]
    client, _, _ = make_file_client([("work", "running")], {"id-work": _log_with(*events)})
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}").text
    # Plain rows keep their detail text and get no slot.
    assert "{&#34;command&#34;: &#34;ls&#34;}" in body
    assert "Bash [main] -&gt; notes.txt" in body
    assert "Bash [main] -&gt; 0003_tool_call.json" in body
    assert body.count('id="evidence-slot-') == 1
    assert '<span class="ev-file">0001_model_response.txt</span>' in body
    assert ">reply<" in body


def test_attempt_page_open_link_carries_slot_wiring_for_toggle():
    events = [
        _ev("tool_call", "Bash [main] -> 0003_tool_call.json"),
        _ev("model_response", "-> 0001_model_response.txt (chars=85)"),
    ]
    client, _, _ = make_file_client([("work", "running")], {"id-work": _log_with(*events)})
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}").text
    # Each evidence row's open action points at the fragment route and at
    # the slot div directly under its row; the toggle script loads the
    # fragment on first activation and clears the slot on the second.
    assert (
        'href="/instances/work/attempts/98/'
        f'{STARTED_DASH}/files/{FILE_JSON}"' in body
    )
    assert 'data-slot="evidence-slot-1"' in body
    assert 'data-label="open"' in body
    assert 'data-label="reply"' in body
    assert 'id="evidence-slot-1"' in body
    assert 'id="evidence-slot-2"' in body
    assert "toggleEvidence" in body
    assert 'slot.innerHTML = ""' in body
    assert 'data-open' in body
