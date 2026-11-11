"""Contract artifact previews and downloads: rendering, verification, torn reads.

From a contract Attempt page an operator can preview any archive member
inline, rendered by its ``media_type`` (JSON with a formatted/raw toggle,
NDJSON as rows, ``text/*`` as text, anything else download only) and
download it whole. Previews fetch one file with ``attempts export --path``,
stop after 256 KiB with a truncation marker and the manifest's true size;
every member is verified (size and ``sha256``) against the manifest before
it renders. A torn read of an unsealed archive is fetched once more, then
reported as still changing; a sealed mismatch is an error. The export bound
is 30 s without progress and is configurable.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import sys
import tarfile
import time

import pytest
from contract_replay import ReplayProcessRunner
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import (
    DEFAULT_CONTRACT_EXPORT_TIMEOUT,
    Settings,
    load_settings,
)
from agent_dashboard.contract_attempt import (
    ARTIFACT_PREVIEW_CAP,
    CHANGING_MESSAGE,
    ContractManifestEntry,
    artifact_render_kind,
    fetch_verified_artifact,
    format_artifact_json,
    format_manifest_size,
    slice_artifact_preview,
    split_ndjson_rows,
    verify_artifact_member,
)
from agent_dashboard.contract_history import (
    clear_known_attempt_ids,
    remember_attempt_ids,
)
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    CONTRACT_PATH,
    AsyncSubprocessRunner,
    ControlExec,
    ControlTimeoutError,
    DockerGateway,
    ManagedInstance,
)

ATTEMPT = "#41/1"
ENCODED_ATTEMPT = "%2341%2F1"
CID = "abc123"
USER = "agent"
INST = "web-1"


@pytest.fixture(autouse=True)
def _forget_ids():
    clear_known_attempt_ids()
    yield
    clear_known_attempt_ids()


def _attempt_summary(*, sealed: bool = True) -> dict:
    entry: dict = {
        "attempt_id": ATTEMPT,
        "issue": {"number": 41, "title": "Issue 41"},
        "started_at": "2026-11-06T08:01:10Z",
        "finished_at": "2026-11-06T08:44:52Z",
        "lifecycle_state": "finished",
        "attention_required": False,
        "sealed": sealed,
    }
    if sealed:
        entry["sealed_at"] = "2026-11-06T08:45:01Z"
        entry["outcome"] = {"label": "Delivered", "native": "delivered",
                            "reason": None}
        entry["delivery"] = {
            "pull_request": {
                "number": 77,
                "url": "https://github.com/example-owner/example-repo/pull/77",
            }
        }
    else:
        entry["sealed_at"] = None
        entry["outcome"] = None
        entry["finished_at"] = None
        entry["delivery"] = None
    return entry


def _manifest_entry(kind: str, path: str, data: bytes, media: str) -> dict:
    return {
        "kind": kind,
        "path": path,
        "size": len(data),
        "media_type": media,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _events_lines() -> bytes:
    lines = [
        {"at": "2026-11-06T08:01:10Z", "level": "info",
         "event": "claim_opened", "text": "claimed"},
    ]
    return b"\n".join(json.dumps(line).encode() for line in lines) + b"\n"


def _show_payload(entries: list[dict], *, sealed: bool = True) -> dict:
    return {
        "contract": "1.0",
        "ok": True,
        "result": {
            "attempt": _attempt_summary(sealed=sealed),
            "manifest": entries,
        },
    }


def _show_fixture(
    entries: list[dict], *, sealed: bool = True, attempt: str = ATTEMPT
) -> dict:
    """A synthetic ``attempts show`` recording (str stdout, package shape)."""
    return {
        "argv": ["--contract", "1", "attempts", "show", attempt],
        "exit_code": 0,
        "stdout": json.dumps(_show_payload(entries, sealed=sealed)),
        "stderr": "",
    }


def _export_tar(entries: list[dict], members: dict[str, bytes]) -> bytes:
    """A synthetic ``attempts export`` tar: manifest.json first, then members."""
    manifest = {
        "contract": "1.0",
        "attempt": _attempt_summary(),
        "files": entries,
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        raw_manifest = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw_manifest)
        tar.addfile(info, io.BytesIO(raw_manifest))
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _export_fixture(path: str, tar_bytes: bytes, *, attempt: str = ATTEMPT) -> dict:
    """A synthetic ``attempts export`` recording (bytes stdout, replayed raw)."""
    return {
        "argv": ["--contract", "1", "attempts", "export", attempt,
                 "--path", path],
        "exit_code": 0,
        "stdout": tar_bytes,
        "stderr": b"",
    }


def _status_result() -> dict:
    from agent_operator_contract import documents

    return json.loads(documents.load_fixture("status-idle")["stdout"])["result"]


def _replay_client(
    show_entries: list[dict],
    export_fixtures: list[dict],
    *,
    sealed: bool = True,
    num_show_fixtures: int = 1,
) -> tuple[TestClient, ReplayProcessRunner]:
    """A client whose contract ops replay show plus export tars in order.

    Each recording answers once, so pass ``num_show_fixtures`` equal to the
    number of HTTP requests the test makes: every preview/download re-reads
    the header with ``attempts show`` before its ``attempts export`` read.
    """
    from agent_operator_contract import documents

    fixtures = [documents.load_fixture("status-idle")]
    fixtures += [_show_fixture(show_entries, sealed=sealed)
                 for _ in range(num_show_fixtures)]
    fixtures += list(export_fixtures)
    runner = ReplayProcessRunner(fixtures)
    gateway = DockerGateway(process_runner=runner)

    async def _list() -> list[ManagedInstance]:
        return [ManagedInstance(
            INST, CID, "running", path=CONTRACT_PATH,
            instance=INST, exec_user=USER, backend="coding-agent",
        )]

    gateway.list_instances = _list  # type: ignore[method-assign]
    state = FleetState()
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=time.time()))
    remember_attempt_ids(INST, [ATTEMPT])
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), runner


def _preview_url(path: str, *, raw: bool = False) -> str:
    query = f"path={path}"
    if raw:
        query += "&raw=1"
    return f"/instances/{INST}/attempt/{ENCODED_ATTEMPT}/artifact?{query}"


def _download_url(path: str) -> str:
    return (f"/instances/{INST}/attempt/{ENCODED_ATTEMPT}"
            f"/artifact/download?path={path}")


def _export_calls(runner: ReplayProcessRunner) -> list[list[str]]:
    return [call for call in runner.calls if "export" in call]


def _entry_for(path: str, data: bytes, media: str) -> ContractManifestEntry:
    return ContractManifestEntry(
        kind="other",
        path=path,
        size=len(data),
        media_type=media,
        sha256=hashlib.sha256(data).hexdigest(),
        size_text=format_manifest_size(len(data)),
    )


# --- rendering by media type -------------------------------------------------


def test_render_kind_json_variants():
    assert artifact_render_kind("application/json") == "json"
    assert artifact_render_kind("application/problem+json") == "json"
    assert artifact_render_kind("Application/JSON") == "json"
    assert artifact_render_kind(" application/json ") == "json"


def test_render_kind_ndjson():
    assert artifact_render_kind("application/x-ndjson") == "ndjson"


def test_render_kind_text():
    assert artifact_render_kind("text/plain") == "text"
    assert artifact_render_kind("text/csv") == "text"
    assert artifact_render_kind("TEXT/PLAIN") == "text"


def test_render_kind_binary_for_anything_else():
    assert artifact_render_kind("application/octet-stream") == "binary"
    assert artifact_render_kind("image/png") == "binary"
    assert artifact_render_kind("") == "binary"
    assert artifact_render_kind(None) == "binary"
    assert artifact_render_kind(123) == "binary"


def test_format_json_pretty_prints_compact_input():
    pretty = format_artifact_json(b'{"b":1,"a":2}', False)
    assert '"a": 2' in pretty
    assert json.loads(pretty) == {"a": 2, "b": 1}
    assert "\n" in pretty


def test_format_json_invalid_falls_back_to_raw():
    assert format_artifact_json(b"not json {", False) == "not json {"
    assert format_artifact_json(b"\xff\xfe binary", False) != ""


def test_format_json_truncated_falls_back_to_raw():
    raw = b'{"a": 1}'
    assert format_artifact_json(raw, True) == '{"a": 1}'


def test_split_ndjson_rows():
    assert split_ndjson_rows(b"alpha\nbeta\n") == ["alpha", "beta"]
    assert split_ndjson_rows(b"one") == ["one"]
    assert split_ndjson_rows(b"") == []
    assert split_ndjson_rows(None) == []  # type: ignore[arg-type]


# --- verification ------------------------------------------------------------


def test_verify_ok():
    data = b'{"a": 1}'
    assert verify_artifact_member(data, _entry_for("r.json", data, "m")) is None


def test_verify_size_mismatch():
    data = b"hello"
    entry = _entry_for("r.txt", data, "text/plain")
    short = ContractManifestEntry(
        kind=entry.kind, path=entry.path, size=len(data) + 1,
        media_type=entry.media_type, sha256=entry.sha256,
        size_text=entry.size_text,
    )
    message = verify_artifact_member(data, short)
    assert message is not None and "size" in message


def test_verify_sha_mismatch():
    data = b"hello"
    entry = _entry_for("r.txt", data, "text/plain")
    wrong = ContractManifestEntry(
        kind=entry.kind, path=entry.path, size=entry.size,
        media_type=entry.media_type, sha256="0" * 64,
        size_text=entry.size_text,
    )
    message = verify_artifact_member(data, wrong)
    assert message is not None and "sha256" in message


def test_manifest_sha256_is_required():
    from agent_dashboard.contract_attempt import parse_contract_attempts_show

    events = _events_lines()
    entries = [
        {"kind": "events", "path": "events.jsonl", "size": len(events),
         "media_type": "application/x-ndjson"},
    ]
    stdout = json.dumps(_show_payload(entries)).encode()
    show = parse_contract_attempts_show(stdout, 0, attempt_id=ATTEMPT)
    assert not show.ok
    assert "manifest" in (show.error or "").lower()


def _fetch_of(*tars: tuple[bytes, int] | bytes):
    """A ``fetch`` returning the given tars (bytes, or (bytes, returncode))."""
    remaining = list(tars)
    calls: list[None] = []

    async def fetch():
        calls.append(None)
        item = remaining.pop(0)
        if isinstance(item, bytes):
            return ControlExec(stdout=item, stderr=b"", returncode=0)
        tar, code = item
        return ControlExec(stdout=tar, stderr=b"", returncode=code)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _tar_with(path: str, data: bytes, media: str = "text/plain") -> bytes:
    entry = _manifest_entry("other", path, data, media)
    return _export_tar([entry], {path: data})


def test_fetch_ok_first_try():
    data = b"hello verified"
    entry = _entry_for("r.txt", data, "text/plain")
    fetch = _fetch_of(_tar_with("r.txt", data))
    result = asyncio.run(
        fetch_verified_artifact(fetch, entry, sealed=True, attempt_id=ATTEMPT)
    )
    assert result.ok and result.data == data and not result.changing
    assert len(fetch.calls) == 1  # type: ignore[attr-defined]


def test_fetch_unsealed_retries_once_then_ok():
    good = b"good bytes"
    entry = _entry_for("r.txt", good, "text/plain")
    fetch = _fetch_of(_tar_with("r.txt", b"bad bytes!"), _tar_with("r.txt", good))
    result = asyncio.run(
        fetch_verified_artifact(fetch, entry, sealed=False, attempt_id=ATTEMPT)
    )
    assert result.ok and result.data == good
    assert len(fetch.calls) == 2  # type: ignore[attr-defined]


def test_fetch_unsealed_twice_bad_reports_changing():
    entry = _entry_for("r.txt", b"good bytes", "text/plain")
    fetch = _fetch_of(_tar_with("r.txt", b"bad bytes!"),
                      _tar_with("r.txt", b"other bad!"))
    result = asyncio.run(
        fetch_verified_artifact(fetch, entry, sealed=False, attempt_id=ATTEMPT)
    )
    assert not result.ok and result.changing
    assert CHANGING_MESSAGE.split(":")[0] in (result.error or "")
    assert len(fetch.calls) == 2  # type: ignore[attr-defined]


def test_fetch_sealed_mismatch_is_an_error_without_retry():
    entry = _entry_for("r.txt", b"good bytes", "text/plain")
    fetch = _fetch_of(_tar_with("r.txt", b"bad bytes!"))
    result = asyncio.run(
        fetch_verified_artifact(fetch, entry, sealed=True, attempt_id=ATTEMPT)
    )
    assert not result.ok and not result.changing
    assert "sealed" in (result.error or "").lower()
    assert len(fetch.calls) == 1  # type: ignore[attr-defined]


def test_fetch_export_error_passes_through():
    entry = _entry_for("r.txt", b"good bytes", "text/plain")
    error = json.dumps({
        "contract": "1.0", "ok": False,
        "error": {"code": "not_found", "message": "gone", "retryable": False},
    }).encode()
    fetch = _fetch_of((error, 5))
    result = asyncio.run(
        fetch_verified_artifact(fetch, entry, sealed=False, attempt_id=ATTEMPT)
    )
    assert not result.ok and not result.changing
    assert "was not found" in (result.error or "")


def test_fetch_retry_export_error_passes_through():
    entry = _entry_for("r.txt", b"good bytes", "text/plain")
    error = json.dumps({
        "contract": "1.0", "ok": False,
        "error": {"code": "internal", "message": "boom", "retryable": True},
    }).encode()
    fetch = _fetch_of(_tar_with("r.txt", b"bad bytes!"), (error, 1))
    result = asyncio.run(
        fetch_verified_artifact(fetch, entry, sealed=False, attempt_id=ATTEMPT)
    )
    assert not result.ok and not result.changing


def test_slice_preview_boundaries():
    short = b"x" * 10
    data, truncated = slice_artifact_preview(short)
    assert (data, truncated) == (short, False)
    exact = b"y" * ARTIFACT_PREVIEW_CAP
    data, truncated = slice_artifact_preview(exact)
    assert (data, truncated) == (exact, False)
    over = b"z" * (ARTIFACT_PREVIEW_CAP + 1)
    data, truncated = slice_artifact_preview(over)
    assert (data, truncated) == (over[:ARTIFACT_PREVIEW_CAP], True)


# --- export timeout: default, settings, gateway -------------------------------


def test_default_export_timeout_is_30s_without_progress():
    from agent_operator_contract import client as contract_client

    assert DEFAULT_CONTRACT_EXPORT_TIMEOUT == 30.0
    assert contract_client.timeout_for("attempts-export", None) == 30.0


def test_export_timeout_settings_default_flag_env():
    assert load_settings([]).contract_export_timeout == 30.0
    assert load_settings(
        ["--contract-export-timeout", "7.5"]).contract_export_timeout == 7.5


def test_export_timeout_settings_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_CONTRACT_EXPORT_TIMEOUT", "9.5")
    assert load_settings([]).contract_export_timeout == 9.5


def test_export_timeout_settings_flag_beats_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_CONTRACT_EXPORT_TIMEOUT", "9.5")
    settings = load_settings(["--contract-export-timeout", "7.5"])
    assert settings.contract_export_timeout == 7.5


@pytest.mark.parametrize("bad", ["0", "-1", "nan"])
def test_export_timeout_settings_reject_non_positive(bad, monkeypatch):
    with pytest.raises(ValueError, match="contract export timeout must be positive"):
        load_settings(["--contract-export-timeout", bad])
    monkeypatch.setenv("AGENT_DASHBOARD_CONTRACT_EXPORT_TIMEOUT", bad)
    with pytest.raises(ValueError, match="contract export timeout must be positive"):
        load_settings([])


@pytest.mark.parametrize("bad", [0, -1.0, float("nan")])
def test_gateway_export_timeout_rejects_non_positive(bad):
    with pytest.raises(ValueError, match="contract export timeout must be positive"):
        DockerGateway(contract_export_timeout=bad)


def test_gateway_export_timeout_defaults_to_none():
    assert DockerGateway().contract_export_timeout is None
    assert DockerGateway(contract_export_timeout=7.5).contract_export_timeout == 7.5


def test_contract_attempt_export_timeout_policy():
    seen: list[float] = []

    class CapturingRunner:
        async def run(self, argv, *, timeout, timeout_error):
            seen.append(timeout)
            return ControlExec(stdout=b"{}", stderr=b"", returncode=0)

    gateway = DockerGateway(process_runner=CapturingRunner())
    asyncio.run(gateway.contract_attempt_export(CID, USER, ATTEMPT, "events.jsonl"))
    assert seen == [30.0]

    seen.clear()
    gateway = DockerGateway(
        process_runner=CapturingRunner(), contract_export_timeout=7.5
    )
    asyncio.run(gateway.contract_attempt_export(CID, USER, ATTEMPT, "events.jsonl"))
    assert seen == [7.5]

    seen.clear()
    asyncio.run(gateway.contract_attempt_export(
        CID, USER, ATTEMPT, "events.jsonl", timeout=2.5))
    assert seen == [2.5]


# --- runner progress bound with real subprocesses ------------------------------


def _trickle_argv() -> list[str]:
    script = (
        "import sys, time\n"
        "for _ in range(6):\n"
        "    sys.stdout.write('x' * 5000 + chr(10))\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.1)\n"
    )
    return [sys.executable, "-c", script]


def test_runner_trickle_passes_a_progress_bound():
    """Chunks every 0.1 s for ~0.6 s total beat a 0.4 s progress bound."""
    runner = AsyncSubprocessRunner()
    result = asyncio.run(runner.run(
        _trickle_argv(), timeout=0.4,
        timeout_error=ControlTimeoutError("stalled"),
    ))
    assert result.returncode == 0
    assert result.stdout.count(b"x") == 6 * 5000


def test_runner_stall_trips_the_bound_and_reaps():
    runner = AsyncSubprocessRunner()
    started = time.monotonic()
    with pytest.raises(ControlTimeoutError, match="stalled"):
        asyncio.run(runner.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.2,
            timeout_error=ControlTimeoutError("stalled"),
        ))
    assert time.monotonic() - started < 10


# --- previews over HTTP through the replay runner ------------------------------


def _page_entries(extra: list[dict], events: bytes | None = None) -> list[dict]:
    lines = events if events is not None else _events_lines()
    return [_manifest_entry("events", "events.jsonl", lines,
                            "application/x-ndjson")] + extra


def test_preview_json_formatted_and_raw_toggle():
    data = b'{"b":1,"a":2}'
    entries = _page_entries([_manifest_entry("other", "report.json", data,
                                             "application/json")])
    tar = _export_tar([entries[1]], {"report.json": data})
    client, runner = _replay_client(
        entries,
        [_export_fixture("report.json", tar),
         _export_fixture("report.json", tar)],
        num_show_fixtures=2,
    )
    body = client.get(_preview_url("report.json")).text
    assert html.unescape(body).find('"a": 2') != -1
    assert "raw" in body
    assert "truncated" not in body
    raw_body = client.get(_preview_url("report.json", raw=True)).text
    assert '{"b":1,"a":2}' in html.unescape(raw_body)
    assert "formatted" in raw_body


def test_preview_problem_json_renders_like_json():
    data = b'{"title":"oops"}'
    entries = _page_entries([_manifest_entry("other", "problem.json", data,
                                             "application/problem+json")])
    tar = _export_tar([entries[1]], {"problem.json": data})
    client, _ = _replay_client(entries, [_export_fixture("problem.json", tar)])
    assert "oops" in html.unescape(client.get(_preview_url("problem.json")).text)


def test_preview_ndjson_renders_rows():
    data = b"alpha\nbeta\n"
    entries = _page_entries([_manifest_entry("other", "rows.ndjson", data,
                                             "application/x-ndjson")])
    tar = _export_tar([entries[1]], {"rows.ndjson": data})
    client, _ = _replay_client(entries, [_export_fixture("rows.ndjson", tar)])
    body = client.get(_preview_url("rows.ndjson")).text
    assert "alpha" in body and "beta" in body


def test_preview_text_renders_text():
    data = b"hello text\nsecond line\n"
    entries = _page_entries([_manifest_entry("other", "notes.txt", data,
                                             "text/plain")])
    tar = _export_tar([entries[1]], {"notes.txt": data})
    client, _ = _replay_client(entries, [_export_fixture("notes.txt", tar)])
    body = client.get(_preview_url("notes.txt")).text
    assert "hello text" in body and "second line" in body


def test_preview_binary_is_download_only_without_export():
    data = b"\x89PNG binary bytes"
    entries = _page_entries([_manifest_entry("other", "shot.png", data,
                                             "application/octet-stream")])
    client, runner = _replay_client(entries, [])
    before = len(_export_calls(runner))
    body = client.get(_preview_url("shot.png")).text
    assert "No inline preview" in body
    assert "download" in body
    assert len(_export_calls(runner)) == before


def test_preview_truncation_marker_and_true_size():
    data = b"z" * (ARTIFACT_PREVIEW_CAP + 100)
    entries = _page_entries([_manifest_entry("other", "big.txt", data,
                                             "text/plain")])
    tar = _export_tar([entries[1]], {"big.txt": data})
    client, _ = _replay_client(entries, [_export_fixture("big.txt", tar)])
    body = client.get(_preview_url("big.txt")).text
    assert "truncated at 256 KB" in body
    assert f"{len(data)} B total" in body


def test_download_serves_verified_bytes():
    data = b'{"full": "file contents here"}'
    entries = _page_entries([_manifest_entry("other", "report.json", data,
                                             "application/json")])
    tar = _export_tar([entries[1]], {"report.json": data})
    client, _ = _replay_client(entries, [_export_fixture("report.json", tar)])
    response = client.get(_download_url("report.json"))
    assert response.status_code == 200
    assert response.content == data
    assert "attachment" in response.headers["content-disposition"]
    assert "report.json" in response.headers["content-disposition"]


def test_download_sealed_mismatch_is_an_error():
    good = b"good bytes"
    entries = _page_entries([_manifest_entry("other", "r.txt", good, "text/plain")])
    tar = _export_tar([entries[1]], {"r.txt": b"bad bytes!"})
    client, runner = _replay_client(entries, [_export_fixture("r.txt", tar)])
    response = client.get(_download_url("r.txt"))
    assert response.status_code == 200
    assert "sealed" in response.text.lower()
    assert response.content != good
    assert len(_export_calls(runner)) == 1


def test_preview_torn_unsealed_retries_once_then_shows():
    good = b"steady content"
    entries = _page_entries([_manifest_entry("other", "note.txt", good,
                                             "text/plain")], )
    entry = entries[1]
    first = _export_tar([entry], {"note.txt": b"torn bytes!!"})
    second = _export_tar([entry], {"note.txt": good})
    client, runner = _replay_client(
        entries,
        [_export_fixture("note.txt", first), _export_fixture("note.txt", second)],
        sealed=False,
    )
    body = client.get(_preview_url("note.txt")).text
    assert "steady content" in body
    assert len(_export_calls(runner)) == 2


def test_preview_torn_unsealed_twice_shows_changing():
    good = b"steady content"
    entries = _page_entries([_manifest_entry("other", "note.txt", good,
                                             "text/plain")])
    entry = entries[1]
    first = _export_tar([entry], {"note.txt": b"torn bytes!!"})
    second = _export_tar([entry], {"note.txt": b"still torn!!"})
    client, runner = _replay_client(
        entries,
        [_export_fixture("note.txt", first), _export_fixture("note.txt", second)],
        sealed=False,
    )
    body = client.get(_preview_url("note.txt")).text
    assert "still changing" in body
    assert len(_export_calls(runner)) == 2


def test_preview_torn_sealed_is_an_error_with_single_fetch():
    good = b"steady content"
    entries = _page_entries([_manifest_entry("other", "note.txt", good,
                                             "text/plain")])
    tar = _export_tar([entries[1]], {"note.txt": b"torn bytes!!"})
    client, runner = _replay_client(
        entries, [_export_fixture("note.txt", tar)], sealed=True
    )
    body = client.get(_preview_url("note.txt")).text
    assert "still changing" not in body
    assert "sealed" in body.lower()
    assert len(_export_calls(runner)) == 1


def test_download_torn_unsealed_retries_then_serves():
    good = b"download bytes"
    entries = _page_entries([_manifest_entry("other", "bin.dat", good,
                                             "application/octet-stream")])
    entry = entries[1]
    first = _export_tar([entry], {"bin.dat": b"torn bytes!!"})
    second = _export_tar([entry], {"bin.dat": good})
    client, runner = _replay_client(
        entries,
        [_export_fixture("bin.dat", first), _export_fixture("bin.dat", second)],
        sealed=False,
    )
    response = client.get(_download_url("bin.dat"))
    assert response.status_code == 200
    assert response.content == good
    assert len(_export_calls(runner)) == 2


def test_preview_unknown_path_is_404():
    data = b"hello"
    entries = _page_entries([_manifest_entry("other", "note.txt", data,
                                             "text/plain")])
    tar = _export_tar([entries[1]], {"note.txt": data})
    client, runner = _replay_client(entries, [_export_fixture("note.txt", tar)])
    before = runner.remaining
    response = client.get(_preview_url("missing.txt"))
    assert response.status_code == 404
    assert "Unknown artifact" in response.text
    # The header lookup ran (one show consumed); no export ran for it.
    assert runner.remaining == before - 1
    assert len(_export_calls(runner)) == 0


def test_preview_traversal_is_404_without_gateway_calls():
    data = b"hello"
    entries = _page_entries([_manifest_entry("other", "note.txt", data,
                                             "text/plain")])
    tar = _export_tar([entries[1]], {"note.txt": data})
    client, runner = _replay_client(entries, [_export_fixture("note.txt", tar)])
    before = len(runner.calls)
    response = client.get(_preview_url("../evil.txt"))
    assert response.status_code == 404
    assert len(runner.calls) == before


def test_unknown_attempt_preview_is_404_without_show_call():
    data = b"hello"
    entries = _page_entries([_manifest_entry("other", "note.txt", data,
                                             "text/plain")])
    tar = _export_tar([entries[1]], {"note.txt": data})
    client, runner = _replay_client(entries, [_export_fixture("note.txt", tar)])
    before = runner.remaining
    response = client.get(
        f"/instances/{INST}/attempt/%2342%2F9/artifact?path=note.txt"
    )
    assert response.status_code == 404
    assert runner.remaining == before


# --- guards via the fake gateway ----------------------------------------------


def _fake_client(*, remember: bool = True) -> tuple[TestClient, FakeGateway]:
    fleet_state = FleetState()
    rows = [ManagedInstance(
        INST, CID, "running", path=CONTRACT_PATH,
        instance=INST, exec_user=USER,
    )]
    gateway = FakeGateway(rows)
    gateway.set_contract_snapshot(CID, _status_result())
    asyncio.run(run_cycle(fleet_state, gateway, now=1000.0))
    if remember:
        remember_attempt_ids(INST, [ATTEMPT])
    app = create_app(
        state=fleet_state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), gateway


def test_preview_guards_answer_404_without_gateway_calls():
    client, gateway = _fake_client()
    response = client.get(_preview_url("../evil.txt"))
    assert response.status_code == 404
    assert gateway.contract_show_calls == []
    assert gateway.contract_export_calls == []


def test_download_guards_answer_404_without_gateway_calls():
    client, gateway = _fake_client(remember=False)
    response = client.get(
        f"/instances/{INST}/attempt/%2342%2F9/artifact/download?path=note.txt"
    )
    assert response.status_code == 404
    assert gateway.contract_show_calls == []
    assert gateway.contract_export_calls == []


# --- LAN-mode viewers ----------------------------------------------------------


def test_lan_viewer_can_preview_and_download(tmp_path, monkeypatch):
    from test_passwords import CheapHasher
    from test_signin import Clock, add_account, add_session, cookie_header

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clock = Clock()
    add_account("vera", role="viewer", now=clock())
    add_session("vera", "tok-viewer-1", now=clock())

    data = b"viewer visible text"
    entries = _page_entries([_manifest_entry("other", "note.txt", data,
                                             "text/plain")])
    tar = _export_tar([entries[1]], {"note.txt": data})

    from agent_operator_contract import documents

    fixtures = [documents.load_fixture("status-idle")]
    fixtures += [_show_fixture(entries) for _ in range(2)]
    fixtures += [_export_fixture("note.txt", tar),
                 _export_fixture("note.txt", tar)]
    runner = ReplayProcessRunner(fixtures)
    gateway = DockerGateway(process_runner=runner)

    async def _list() -> list[ManagedInstance]:
        return [ManagedInstance(
            INST, CID, "running", path=CONTRACT_PATH,
            instance=INST, exec_user=USER, backend="coding-agent",
        )]

    gateway.list_instances = _list  # type: ignore[method-assign]
    state = FleetState()
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=time.time()))
    remember_attempt_ids(INST, [ATTEMPT])
    settings = Settings(
        lan_mode=True,
        allowed_hosts=frozenset({"example.test"}),
        poll_interval=5.0,
    )
    app = create_app(
        state=state,
        settings=settings,
        gateway=gateway,
        start_poller=False,
        clock=clock,
        password_hasher=CheapHasher(),
    )
    client = TestClient(app, base_url="http://localhost")
    headers = cookie_header("agent-dashboard-sign-in", "tok-viewer-1")

    preview = client.get(_preview_url("note.txt"), headers=headers)
    assert preview.status_code == 200
    assert "viewer visible text" in preview.text

    download = client.get(_download_url("note.txt"), headers=headers)
    assert download.status_code == 200
    assert download.content == data


def test_attempt_page_links_previews_and_downloads():
    data = b"hello"
    entries = _page_entries([_manifest_entry("other", "note.txt", data,
                                             "text/plain")])
    lines = _events_lines()
    events_tar = _export_tar(
        [entries[0]], {"events.jsonl": lines},
    )
    note_tar = _export_tar([entries[1]], {"note.txt": data})
    from agent_operator_contract import documents

    show = _show_entries_with_events(entries, lines)
    fixtures = [documents.load_fixture("status-idle"), show,
                _export_fixture("events.jsonl", events_tar),
                _export_fixture("note.txt", note_tar)]
    runner = ReplayProcessRunner(fixtures)
    gateway = DockerGateway(process_runner=runner)

    async def _list() -> list[ManagedInstance]:
        return [ManagedInstance(
            INST, CID, "running", path=CONTRACT_PATH,
            instance=INST, exec_user=USER, backend="coding-agent",
        )]

    gateway.list_instances = _list  # type: ignore[method-assign]
    state = FleetState()
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=time.time()))
    remember_attempt_ids(INST, [ATTEMPT])
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    body = client.get(f"/instances/{INST}/attempt/{ENCODED_ATTEMPT}").text
    assert "Archive manifest" in body
    assert "note.txt" in body
    assert "Preview" in body
    assert "download" in body
    assert "artifact?path=note.txt" in body
    assert "artifact/download?path=note.txt" in body


def _show_entries_with_events(
    entries: list[dict], lines: bytes
) -> dict:
    fixed = [dict(entries[0], size=len(lines),
                  sha256=hashlib.sha256(lines).hexdigest())] + entries[1:]
    return _show_fixture(fixed)
