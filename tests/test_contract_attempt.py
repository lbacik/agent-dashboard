"""Contract Attempt page: header, archive events and manifest (contract major 1).

One contract Attempt has its own page, addressed by its opaque Attempt ID
(URL-encoded) and reached from the Attempt history row: the header comes
from ``attempts show <attempt-id>``, the Attempt's Contract Events come
from the ``events.jsonl`` member (kind ``events``) of its archive, fetched
with ``attempts export <attempt-id> --path <path>``, and the archive
manifest is listed by kind with size and media type. ``native.`` events are
labelled native; the event stream reuses the shared level, event and text
filters with 200-row server paging. Malformed or unknown replies render a
clear message, never an error. Vocabulary: Attempt log, Managed instance.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile

import pytest
from contract_replay import ReplayProcessRunner
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.attempt_log import LOG_PAGE_SIZE, filter_events, paginate
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.contract_attempt import (
    ATTEMPT_ID_MAX_LENGTH,
    ContractArchiveEvent,
    extract_export_member,
    parse_contract_attempt_export,
    parse_contract_attempts_show,
    parse_contract_events,
    validate_attempt_id_shape,
    validate_contract_attempt_id,
    validate_export_path,
)
from agent_dashboard.contract_history import (
    clear_known_attempt_ids,
    known_attempt_ids,
    remember_attempt_ids,
)
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    AGENTCTL_PATH,
    CONTRACT_PATH,
    ControlExec,
    DockerGateway,
    ManagedInstance,
    argv_contract_attempts_export,
    argv_contract_attempts_show,
)


@pytest.fixture(autouse=True)
def _forget_ids():
    clear_known_attempt_ids()
    yield
    clear_known_attempt_ids()


def _package_fixture(name: str) -> dict:
    """One pinned consumer fixture from the agent-operator-contract package."""
    from agent_operator_contract import documents

    return documents.load_fixture(name)


def _reply(name: str) -> tuple[bytes, int]:
    fixture = _package_fixture(name)
    return fixture["stdout"].encode("utf-8"), fixture["exit_code"]


def _error_envelope(code: str, message: str) -> bytes:
    return json.dumps(
        {
            "contract": "1.0",
            "ok": False,
            "error": {"code": code, "message": message, "retryable": False},
        }
    ).encode()


def _summary(
    attempt_id: str = "#41/1",
    issue: int = 41,
    *,
    sealed: bool = True,
    label: str = "Delivered",
    lifecycle: str = "finished",
) -> dict:
    entry: dict = {
        "attempt_id": attempt_id,
        "issue": {"number": issue, "title": f"Issue {issue}"},
        "started_at": "2026-11-06T08:01:10Z",
        "finished_at": "2026-11-06T08:44:52Z",
        "lifecycle_state": lifecycle,
        "attention_required": False,
        "sealed": sealed,
    }
    if sealed:
        entry["sealed_at"] = "2026-11-06T08:45:01Z"
        entry["outcome"] = {"label": label, "native": "delivered", "reason": None}
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


def _manifest(*entries: dict) -> list[dict]:
    return list(entries)


def _events_entry(size: int = 18342) -> dict:
    return {
        "kind": "events",
        "path": "events.jsonl",
        "size": size,
        "media_type": "application/x-ndjson",
        "sha256": "9f2c" + "0" * 60,
    }


def _transcript_entry() -> dict:
    return {
        "kind": "transcript",
        "path": "transcript.jsonl",
        "size": 2210431,
        "media_type": "application/x-ndjson",
        "sha256": "4b1d" + "0" * 60,
    }


def _show_payload(attempt_id: str = "#41/1", *, sealed: bool = True) -> dict:
    return {
        "contract": "1.0",
        "ok": True,
        "result": {
            "attempt": _summary(attempt_id, sealed=sealed),
            "manifest": _manifest(_events_entry(), _transcript_entry()),
        },
    }


def _export_manifest(files: list[dict]) -> dict:
    return {
        "contract": "1.0",
        "attempt": _summary(),
        "files": files,
    }


def _export_tar(manifest: dict, members: dict[str, bytes]) -> bytes:
    """A synthetic ``attempts export`` tar: manifest.json first, then members."""
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


def _event(
    seq: int = 1,
    event: str = "claim_opened",
    *,
    level: str = "info",
    text: str = "claimed",
    extra: dict | None = None,
) -> dict:
    doc: dict = {
        "at": f"2026-11-06T08:0{seq}:10Z",
        "level": level,
        "event": event,
        "text": text,
    }
    if extra:
        doc.update(extra)
    return doc


def _claim(seq: int = 1, attempt_id: str = "#41/1") -> dict:
    return _event(
        seq, "claim_opened", extra={"attempt_id": attempt_id, "issue": 41}
    )


# --- argv pinning ----------------------------------------------------------


def test_attempts_show_argv_pinned():
    assert argv_contract_attempts_show("#41/1") == ["attempts", "show", "#41/1"]


def test_attempts_show_argv_matches_package_fixture():
    fixture = _package_fixture("attempts-show-sealed")
    assert fixture["argv"] == ["--contract", "1", "attempts", "show", "#41/1"]
    assert fixture["exit_code"] == 0
    stdout, _ = _reply("attempts-show-sealed")
    show = parse_contract_attempts_show(stdout, 0, attempt_id="#41/1")
    assert show.ok


def test_attempts_export_argv_pinned():
    assert argv_contract_attempts_export("#41/1", "events.jsonl") == [
        "attempts",
        "export",
        "#41/1",
        "--path",
        "events.jsonl",
    ]


def test_attempts_export_argv_matches_package_error_fixture():
    fixture = _package_fixture("attempts-export-not-found")
    assert fixture["argv"] == ["--contract", "1", "attempts", "export", "#99/9"]
    assert fixture["exit_code"] == 5


def test_attempt_id_shape_refusals_before_argv():
    bad_ids = [
        "-sneaky",
        "--contract",
        "has\nnewline",
        "has\ttab",
        "null\x00byte",
        "del\x7fchar",
        "",
        "x" * (ATTEMPT_ID_MAX_LENGTH + 1),
        None,
        42,
        b"bytes",
    ]
    for bad in bad_ids:
        with pytest.raises(ValueError):
            argv_contract_attempts_show(bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            argv_contract_attempts_export(bad, "events.jsonl")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            validate_attempt_id_shape(bad)
    assert ATTEMPT_ID_MAX_LENGTH == 256
    # Spaces are not control characters: an opaque ID may hold them, and the
    # argv list carries them without any shell.
    assert argv_contract_attempts_show("has space") == ["attempts", "show", "has space"]
    assert argv_contract_attempts_show("x" * ATTEMPT_ID_MAX_LENGTH) == [
        "attempts",
        "show",
        "x" * ATTEMPT_ID_MAX_LENGTH,
    ]


def test_export_path_refusals_before_argv():
    for bad in ["", "/absolute", "../up", "a/../../b", "a/..", "../", "a/../.."]:
        with pytest.raises(ValueError):
            argv_contract_attempts_export("#41/1", bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            validate_export_path(bad)
    assert validate_export_path("events.jsonl") == "events.jsonl"


def test_unknown_attempt_id_refused_before_gateway():
    remember_attempt_ids("web-1", ["#41/1"])
    assert validate_contract_attempt_id("#41/1", known_attempt_ids("web-1")) == "#41/1"
    with pytest.raises(ValueError):
        validate_contract_attempt_id("#42/1", known_attempt_ids("web-1"))
    with pytest.raises(ValueError):
        # Shape is checked before membership, so no argv can carry it.
        validate_contract_attempt_id("-x", known_attempt_ids("web-1"))
    assert known_attempt_ids("other") == frozenset()


def test_attempts_show_runs_online_with_15s_default_timeout():
    from agent_operator_contract import client as contract_client

    assert contract_client.timeout_for("attempts-show", None) == 15.0

    seen = {}

    class CapturingRunner:
        async def run(self, argv, *, timeout, timeout_error):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            return ControlExec(stdout=b"{}", stderr=b"", returncode=0)

    gateway = DockerGateway(process_runner=CapturingRunner())
    asyncio.run(gateway.contract_attempt_show("abc123", "someone", "#41/1"))
    assert seen["timeout"] == 15.0
    assert seen["argv"] == [
        "docker",
        "exec",
        "--user",
        "someone",
        "abc123",
        AGENTCTL_PATH,
        "--contract",
        "1",
        "attempts",
        "show",
        "#41/1",
    ]


def test_attempts_export_runs_online_with_30s_default_timeout():
    from agent_operator_contract import client as contract_client

    assert contract_client.timeout_for("attempts-export", None) == 30.0

    seen = {}

    class CapturingRunner:
        async def run(self, argv, *, timeout, timeout_error):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            return ControlExec(stdout=b"{}", stderr=b"", returncode=0)

    gateway = DockerGateway(process_runner=CapturingRunner())
    asyncio.run(
        gateway.contract_attempt_export("abc123", "someone", "#41/1", "events.jsonl")
    )
    assert seen["timeout"] == 30.0
    assert seen["argv"] == [
        "docker",
        "exec",
        "--user",
        "someone",
        "abc123",
        AGENTCTL_PATH,
        "--contract",
        "1",
        "attempts",
        "export",
        "#41/1",
        "--path",
        "events.jsonl",
    ]


def test_attempts_show_replays_the_pinned_fixture():
    runner = ReplayProcessRunner.from_package(["attempts-show-sealed"])
    gateway = DockerGateway(process_runner=runner)
    exec_result = asyncio.run(
        gateway.contract_attempt_show("abc123", "agent", "#41/1")
    )
    assert exec_result.returncode == 0
    assert runner.remaining == 0
    (call,) = runner.calls
    assert call == [
        "docker",
        "exec",
        "--user",
        "agent",
        "abc123",
        AGENTCTL_PATH,
        "--contract",
        "1",
        "attempts",
        "show",
        "#41/1",
    ]


# --- attempts show parsing -------------------------------------------------


def test_show_sealed_header_and_manifest():
    stdout, code = _reply("attempts-show-sealed")
    show = parse_contract_attempts_show(stdout, code, attempt_id="#41/1")
    assert show.ok and show.error is None and not show.not_found
    assert show.summary is not None
    assert show.summary.attempt_id == "#41/1"
    assert show.summary.issue == 41
    assert show.summary.sealed is True
    assert show.still_changing is False
    assert [entry.kind for entry in show.manifest] == ["events", "transcript"]
    events = next(entry for entry in show.manifest if entry.kind == "events")
    assert events.path == "events.jsonl"
    assert events.size == 18342
    assert events.media_type == "application/x-ndjson"


def test_show_unsealed_marks_still_changing():
    stdout, code = _reply("attempts-show-unsealed")
    show = parse_contract_attempts_show(stdout, code, attempt_id="#42/1")
    assert show.ok
    assert show.summary is not None and show.summary.sealed is False
    assert show.still_changing is True


def test_show_not_found_pins_404():
    stdout = _error_envelope("not_found", "no attempt #99/9 is recorded")
    show = parse_contract_attempts_show(stdout, 5, attempt_id="#99/9")
    assert not show.ok and show.not_found
    assert "#99/9" in (show.error or "")


def test_show_malformed_is_not_recognised():
    for stdout, code in [(b"", 0), (b"not json", 0), (b"{}", 0)]:
        show = parse_contract_attempts_show(stdout, code)
        assert not show.ok and not show.not_found


def test_show_manifest_paths_refused():
    evil = dict(_events_entry())
    for bad_path in ("../escape.jsonl", "/absolute.jsonl"):
        evil["path"] = bad_path
        payload = {
            "contract": "1.0",
            "ok": True,
            "result": {"attempt": _summary(), "manifest": [evil]},
        }
        show = parse_contract_attempts_show(
            json.dumps(payload).encode(), 0, attempt_id="#41/1"
        )
        assert not show.ok


def test_show_manifest_without_events_refused():
    payload = {
        "contract": "1.0",
        "ok": True,
        "result": {"attempt": _summary(), "manifest": [_transcript_entry()]},
    }
    show = parse_contract_attempts_show(
        json.dumps(payload).encode(), 0, attempt_id="#41/1"
    )
    assert not show.ok


# --- archive tar extraction ----------------------------------------------


def _good_tar() -> tuple[bytes, bytes]:
    lines = "\n".join(
        json.dumps(_claim(seq)) for seq in (1, 2, 3)
    ).encode() + b"\n"
    manifest = _export_manifest([_events_entry(size=len(lines)), _transcript_entry()])
    stream = _export_tar(manifest, {"events.jsonl": lines, "transcript.jsonl": b"x"})
    return stream, lines


def test_extract_member_reads_manifest_first_then_member():
    stream, lines = _good_tar()
    data, manifest = extract_export_member(stream, "events.jsonl")
    assert data == lines
    assert manifest["files"][0]["kind"] == "events"


def test_extract_member_refuses_bad_first_member():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in ("events.jsonl", "manifest.json"):
            raw = b"{}" if name.endswith(".json") else b"x"
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    with pytest.raises(ValueError):
        extract_export_member(buf.getvalue(), "events.jsonl")


def test_extract_member_checks_names_against_manifest():
    manifest = _export_manifest([_events_entry(size=1)])
    stream = _export_tar(manifest, {"events.jsonl": b"x", "extra.bin": b"y"})
    with pytest.raises(ValueError):
        extract_export_member(stream, "events.jsonl")


def test_extract_member_refuses_unknown_and_missing_paths():
    manifest = _export_manifest([_events_entry(size=1)])
    stream = _export_tar(manifest, {"events.jsonl": b"x"})
    with pytest.raises(ValueError):
        extract_export_member(stream, "transcript.jsonl")
    with pytest.raises(ValueError):
        extract_export_member(stream, "../escape.jsonl")
    with pytest.raises(ValueError):
        extract_export_member(b"not a tar", "events.jsonl")
    with pytest.raises(ValueError):
        extract_export_member(b"", "events.jsonl")


def test_extract_member_refuses_manifest_with_bad_paths():
    evil = dict(_events_entry(size=1))
    evil["path"] = "../escape.jsonl"
    manifest = _export_manifest([evil])
    stream = _export_tar(manifest, {"events.jsonl": b"x"})
    with pytest.raises(ValueError):
        extract_export_member(stream, "events.jsonl")


def test_export_nonzero_after_streaming_discards_output():
    stream, _ = _good_tar()
    export = parse_contract_attempt_export(
        stream, 1, attempt_id="#41/1", wanted="events.jsonl"
    )
    assert not export.ok and not export.not_found and export.data is None
    assert "discarded" in (export.error or "")


def test_export_not_found_without_streaming_pins_404():
    export = parse_contract_attempt_export(
        _error_envelope("not_found", "no attempt #99/9 is recorded"),
        5,
        attempt_id="#99/9",
        wanted="events.jsonl",
    )
    assert not export.ok and export.not_found and export.data is None


def test_export_ok_returns_member_bytes():
    stream, lines = _good_tar()
    export = parse_contract_attempt_export(
        stream, 0, attempt_id="#41/1", wanted="events.jsonl"
    )
    assert export.ok and export.data == lines


# --- archive events parsing ----------------------------------------------


def test_parse_events_marks_native_and_levels():
    lines = b"\n".join(
        [
            json.dumps(_claim(1)).encode(),
            json.dumps(
                _event(2, "native.phase_tick", level="debug", text="tick")
            ).encode(),
            json.dumps(
                _event(
                    3,
                    "sealed",
                    level="warning",
                    text="sealed now",
                    extra={"attempt_id": "#41/1"},
                )
            ).encode(),
            b"not json",
            json.dumps({"at": "bad", "level": "info", "event": "x", "text": "y"}).encode(),
        ]
    ) + b"\n"
    parsed = parse_contract_events(lines)
    assert parsed.ok
    assert parsed.unreadable == 2
    assert [ev.seq for ev in parsed.events] == [1, 2, 3]
    by_seq = {ev.seq: ev for ev in parsed.events}
    assert by_seq[1].event == "claim_opened" and not by_seq[1].is_native
    assert by_seq[1].time_text == "08:01:10"
    assert by_seq[2].is_native and by_seq[2].detail == "tick"
    assert by_seq[2].level_known is False
    assert by_seq[3].level_known is True


def test_parse_events_empty_or_garbage_is_not_recognised():
    assert not parse_contract_events(b"").ok
    assert not parse_contract_events(b"\n  \n").ok
    assert not parse_contract_events(b"\xff\xfe binary").ok


def test_archive_events_reuse_shared_filters_and_200_row_paging():
    assert LOG_PAGE_SIZE == 200
    rows = tuple(
        ContractArchiveEvent(
            seq=number,
            timestamp_text="2026-11-06T08:01:10Z",
            time_text="08:01:10",
            level="info" if number % 2 else "warning",
            level_known=True,
            event="claim_opened" if number % 3 else "sealed",
            detail=f"row {number}",
            is_native=False,
        )
        for number in range(1, 251)
    )
    assert len(filter_events(rows, level="info")) == 125
    assert len(filter_events(rows, event="sealed")) == 83
    assert len(filter_events(rows, q="row 7")) == 11
    page, number, pages = paginate(rows, "2")
    assert (number, pages) == (2, 2)
    assert [ev.seq for ev in page] == list(range(201, 251))


# --- Attempt page over HTTP ----------------------------------------------


def _status_result(name: str = "status-idle") -> dict:
    return json.loads(_package_fixture(name)["stdout"])["result"]


def make_attempt_client(
    attempt_id: str = "#41/1",
    *,
    instance: str = "web-1",
    state: str = "running",
    exec_user: str = "agent",
    show: ControlExec | Exception | None = None,
    export: ControlExec | Exception | None = None,
    remember: bool = True,
) -> tuple[TestClient, FakeGateway]:
    """A client with one contract instance and scripted show/export replies."""
    fleet_state = FleetState()
    rows = [
        ManagedInstance(
            instance,
            f"abc{instance}",
            state,
            path=CONTRACT_PATH,
            instance=instance,
            exec_user=exec_user,
        )
    ]
    gateway = FakeGateway(rows)
    gateway.set_contract_snapshot(f"abc{instance}", _status_result())
    asyncio.run(run_cycle(fleet_state, gateway, now=1000.0))
    if remember:
        remember_attempt_ids(instance, [attempt_id])
    if show is not None:
        if isinstance(show, Exception):
            gateway.set_contract_attempt_show(f"abc{instance}", attempt_id, show)
        else:
            gateway.set_contract_attempt_show(f"abc{instance}", attempt_id, show)
    if export is not None:
        gateway.set_contract_attempt_export(
            f"abc{instance}", attempt_id, "events.jsonl", export
        )
    app = create_app(
        state=fleet_state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), gateway


def _scripted_page(
    attempt_id: str = "#41/1", *, sealed: bool = True
) -> tuple[ControlExec, ControlExec]:
    stream, _ = _good_tar()
    return (
        ControlExec(
            stdout=json.dumps(_show_payload(attempt_id, sealed=sealed)).encode(),
            stderr=b"",
            returncode=0,
        ),
        ControlExec(stdout=stream, stderr=b"", returncode=0),
    )


def test_history_row_links_to_the_attempt_page():
    client, gateway = make_attempt_client(remember=False)
    payload = {
        "contract": "1.0",
        "ok": True,
        "result": {"attempts": [_summary("#41/1")], "next_cursor": None},
    }
    gateway.set_contract_attempts("abcweb-1", {None: payload})
    body = client.get("/instances/web-1/history?preset=all").text
    assert "/instances/web-1/attempt/%2341%2F1" in body


def test_attempt_page_shows_header_events_and_manifest():
    show, export = _scripted_page()
    client, gateway = make_attempt_client(show=show, export=export)
    response = client.get("/instances/web-1/attempt/%2341%2F1")
    assert response.status_code == 200
    body = response.text
    # Header from ``attempts show``.
    assert "Attempt #41/1" in body
    assert "#41" in body and "Issue 41" in body
    assert "2026-11-06T08:01:10Z" in body
    assert "Delivered" in body
    assert "sealed" in body
    assert "still changing" not in body
    # Manifest listed by kind with size and media type.
    assert "events.jsonl" in body
    assert "transcript.jsonl" in body
    assert "application/x-ndjson" in body
    # Archive events with the shared filters and paging.
    assert "claim_opened" in body
    assert 'name="level"' in body and 'name="event"' in body
    assert 'name="q"' in body
    assert "page 1/1, 3 events" in body
    # The pinned argv ran once per operation.
    assert gateway.contract_show_calls == [("abcweb-1", "agent", "#41/1")]
    assert gateway.contract_export_calls == [
        ("abcweb-1", "agent", "#41/1", "events.jsonl")
    ]
    assert gateway.contract_argvs == [
        [
            "docker",
            "exec",
            "--user",
            "agent",
            "abcweb-1",
            AGENTCTL_PATH,
            "--contract",
            "1",
            "attempts",
            "show",
            "#41/1",
        ],
        [
            "docker",
            "exec",
            "--user",
            "agent",
            "abcweb-1",
            AGENTCTL_PATH,
            "--contract",
            "1",
            "attempts",
            "export",
            "#41/1",
            "--path",
            "events.jsonl",
        ],
    ]


def test_attempt_page_marks_unsealed_still_changing():
    show, export = _scripted_page(sealed=False)
    client, _ = make_attempt_client(show=show, export=export)
    body = client.get("/instances/web-1/attempt/%2341%2F1").text
    assert "open" in body
    assert "still changing" in body


def test_attempt_page_labels_native_events():
    lines = (
        json.dumps(_claim(1)).encode()
        + b"\n"
        + json.dumps(_event(2, "native.tick", level="debug", text="tick")).encode()
        + b"\n"
    )
    manifest = _export_manifest([_events_entry(size=len(lines))])
    stream = _export_tar(manifest, {"events.jsonl": lines})
    show = ControlExec(
        stdout=json.dumps(_show_payload()).encode(), stderr=b"", returncode=0
    )
    client, _ = make_attempt_client(
        show=show, export=ControlExec(stdout=stream, stderr=b"", returncode=0)
    )
    body = client.get("/instances/web-1/attempt/%2341%2F1").text
    assert "native.tick" in body
    assert "native-tag" in body and ">native<" in body


def test_attempt_page_filters_and_paging():
    lines = b"\n".join(
        json.dumps(
            _event(
                seq,
                "claim_opened" if seq % 2 else "sealed",
                level="info" if seq % 2 else "warning",
                text=f"row {seq}",
                extra=(
                    {"attempt_id": "#41/1", "issue": 41}
                    if seq % 2
                    else {"attempt_id": "#41/1"}
                ),
            )
        ).encode()
        for seq in range(1, 6)
    ) + b"\n"
    manifest = _export_manifest([_events_entry(size=len(lines))])
    stream = _export_tar(manifest, {"events.jsonl": lines})
    show = ControlExec(
        stdout=json.dumps(_show_payload()).encode(), stderr=b"", returncode=0
    )
    client, _ = make_attempt_client(
        show=show, export=ControlExec(stdout=stream, stderr=b"", returncode=0)
    )
    assert "row 2" in client.get(
        "/instances/web-1/attempt/%2341%2F1?level=warning"
    ).text
    assert "row 1" not in client.get(
        "/instances/web-1/attempt/%2341%2F1?level=warning"
    ).text
    assert "sealed" in client.get(
        "/instances/web-1/attempt/%2341%2F1?event=sealed"
    ).text
    assert "claim_opened" not in client.get(
        "/instances/web-1/attempt/%2341%2F1?event=sealed"
    ).text
    assert "row 3" in client.get("/instances/web-1/attempt/%2341%2F1?q=row+3").text
    assert "row 4" not in client.get("/instances/web-1/attempt/%2341%2F1?q=row+3").text


def test_attempt_page_reached_from_history_lists_then_shows():
    """The history visit remembers the ID, so the linked page resolves."""
    from agent_dashboard.contract_history import remember_attempt_ids as _remember

    fleet_state = FleetState()
    rows = [
        ManagedInstance(
            "web-1", "abcweb-1", "running", path=CONTRACT_PATH,
            instance="web-1", exec_user="agent",
        )
    ]
    gateway = FakeGateway(rows)
    gateway.set_contract_snapshot("abcweb-1", _status_result())
    asyncio.run(run_cycle(fleet_state, gateway, now=1000.0))
    show, export = _scripted_page()
    gateway.set_contract_attempt_show("abcweb-1", "#41/1", show)
    gateway.set_contract_attempt_export("abcweb-1", "#41/1", "events.jsonl", export)
    payload = {
        "contract": "1.0",
        "ok": True,
        "result": {"attempts": [_summary("#41/1")], "next_cursor": None},
    }
    gateway.set_contract_attempts("abcweb-1", {None: payload})
    app = create_app(
        state=fleet_state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    history = client.get("/instances/web-1/history?preset=all").text
    assert "/instances/web-1/attempt/%2341%2F1" in history
    assert _remember is not None and "#41/1" in known_attempt_ids("web-1")
    assert client.get("/instances/web-1/attempt/%2341%2F1").status_code == 200


# --- refusal paths -------------------------------------------------------


def test_unknown_attempt_id_answers_404_without_gateway_calls():
    show, export = _scripted_page()
    client, gateway = make_attempt_client(show=show, export=export)
    response = client.get("/instances/web-1/attempt/%2342%2F9")
    assert response.status_code == 404
    assert "Unknown Attempt" in response.text
    assert gateway.contract_show_calls == []
    assert gateway.contract_export_calls == []


def test_invalid_attempt_id_answers_404_without_gateway_calls():
    client, gateway = make_attempt_client()
    for raw in ("-sneaky", "has%0Anewline", "x" * (ATTEMPT_ID_MAX_LENGTH + 1)):
        response = client.get(f"/instances/web-1/attempt/{raw}")
        assert response.status_code == 404
    assert gateway.contract_show_calls == []


def test_attempt_page_on_legacy_instance_answers_404():
    from agent_dashboard.gateway import LEGACY_PATH

    fleet_state = FleetState()
    rows = [ManagedInstance("old-1", "abc123", "running", path=LEGACY_PATH)]
    gateway = FakeGateway(rows)
    app = create_app(
        state=fleet_state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    response = client.get("/instances/old-1/attempt/%2341%2F1")
    assert response.status_code == 404
    assert "legacy instances" in response.text
    assert gateway.contract_show_calls == []


def test_legacy_attempt_route_on_contract_instance_answers_404():
    client, _ = make_attempt_client()
    response = client.get("/instances/web-1/attempts/42/2026-11-06T08-50-00Z")
    assert response.status_code == 404
    assert "contract instances" in response.text
    response = client.get("/instances/web-1/attempts/42/2026-11-06T08-50-00Z/progress")
    assert response.status_code == 404
    assert "contract instances" in response.text


def test_attempt_page_unknown_instance_answers_404():
    client, _ = make_attempt_client()
    response = client.get("/instances/gone/attempt/%2341%2F1")
    assert response.status_code == 404
    assert "Unknown Managed instance" in response.text


def test_attempt_page_not_running_or_missing_user_is_a_banner():
    show, export = _scripted_page()
    client, _ = make_attempt_client(
        instance="down-1", state="exited", show=show, export=export
    )
    response = client.get("/instances/down-1/attempt/%2341%2F1")
    assert response.status_code == 200
    assert "not running" in response.text
    client, _ = make_attempt_client(
        instance="nouser-1", exec_user="", show=show, export=export
    )
    response = client.get("/instances/nouser-1/attempt/%2341%2F1")
    assert response.status_code == 200
    assert "exec user is missing" in response.text


def test_show_not_found_answers_404():
    client, _ = make_attempt_client(
        show=ControlExec(
            stdout=_error_envelope("not_found", "no attempt #41/1 is recorded"),
            stderr=b"",
            returncode=5,
        )
    )
    response = client.get("/instances/web-1/attempt/%2341%2F1")
    assert response.status_code == 404
    assert "was not found" in response.text


def test_show_gateway_error_is_a_banner():
    from agent_dashboard.gateway import GatewayError

    client, _ = make_attempt_client(show=GatewayError("boom"))
    response = client.get("/instances/web-1/attempt/%2341%2F1")
    assert response.status_code == 200
    assert "Could not read the Attempt" in response.text


def test_torn_export_keeps_header_with_events_error():
    show, _ = _scripted_page()
    stream, _ = _good_tar()
    client, _ = make_attempt_client(
        show=show,
        export=ControlExec(stdout=stream, stderr=b"", returncode=1),
    )
    response = client.get("/instances/web-1/attempt/%2341%2F1")
    assert response.status_code == 200
    body = response.text
    assert "Attempt #41/1" in body
    assert "discarded" in body
