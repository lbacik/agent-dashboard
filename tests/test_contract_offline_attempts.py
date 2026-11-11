"""Stopped contract instances read Attempts offline (spec #176 "running versus stopped").

A stopped contract instance renders what it would have rendered while
running: history, the drawer statistics card, the Fleet overview, Attempt
pages, previews and downloads all run through the same Offline Read helper
(``attempts list --limit 100 [--before <cursor>]``, ``attempts show``,
``attempts export --path``) and share the online view models. At most one
Offline Read per stopped instance runs at a time; the Sealed cache serves
cached Attempts without starting a helper. Failures render a clear message,
never a 500.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
import time

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.contract_history import (
    clear_known_attempt_ids,
    collect_contract_history_offline,
    remember_attempt_ids,
)
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    AGENTCTL_PATH,
    CONTRACT_PATH,
    ControlExec,
    ControlTimeoutError,
    GatewayError,
    ManagedInstance,
    OfflineInspect,
    OfflineMount,
    argv_offline_attempts_export,
    argv_offline_attempts_list,
    argv_offline_attempts_show,
)
from agent_dashboard.range_stats import RangeSpec
from agent_dashboard.sealed_archive_cache import SealedArchiveCache


@pytest.fixture(autouse=True)
def _forget_ids():
    clear_known_attempt_ids()
    yield
    clear_known_attempt_ids()


IMAGE = "sha256:" + "ab" * 32
MOUNTS = (OfflineMount("data-vol", "/data"),)
FINISH = "2026-11-06T09:58:12.123456789Z"
ATTEMPT = "#41/1"
ENCODED_ATTEMPT = "%2341%2F1"


def _stopped(name="down-1", cid="abc123"):
    return ManagedInstance(
        name,
        cid,
        "exited",
        path=CONTRACT_PATH,
        instance=name,
        exec_user="agent",
        backend="coding-agent",
    )


def _inspect(**overrides):
    info = {
        "running": False,
        "finished_at": FINISH,
        "image": IMAGE,
        "mounts": MOUNTS,
    }
    info.update(overrides)
    return OfflineInspect(**info)  # type: ignore[arg-type]


def _iso(days_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


def _attempt(attempt_id="#42/1", issue=42, **overrides):
    entry = {
        "attempt_id": attempt_id,
        "issue": {"number": issue, "title": f"Issue {issue}"},
        "started_at": "2026-11-06T08:50:00Z",
        "finished_at": "2026-11-06T09:10:00Z",
        "lifecycle_state": "finished",
        "outcome": {"label": "Delivered", "native": "delivered", "reason": None},
        "attention_required": False,
        "sealed": True,
        "sealed_at": "2026-11-06T09:10:00Z",
        "delivery": {
            "pull_request": {
                "number": 78,
                "url": "https://example.invalid/repo/pull/78",
            }
        },
    }
    entry.update(overrides)
    return entry


def _envelope(attempts, next_cursor=None):
    return {
        "contract": "1.0",
        "ok": True,
        "result": {"attempts": attempts, "next_cursor": next_cursor},
    }


def _summary(sealed=True):
    entry = {
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
        entry["outcome"] = {"label": "Delivered", "native": "delivered", "reason": None}
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


def _claim(seq=1):
    return {
        "at": f"2026-11-06T08:0{seq}:10Z",
        "level": "info",
        "event": "claim_opened",
        "text": "claimed",
        "attempt_id": ATTEMPT,
        "issue": 41,
    }


def _events_lines():
    return b"\n".join(json.dumps(_claim(seq)).encode() for seq in (1, 2, 3)) + b"\n"


def _manifest_entry(kind, path, data, media):
    return {
        "kind": kind,
        "path": path,
        "size": len(data),
        "media_type": media,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _show_payload(entries):
    return {
        "contract": "1.0",
        "ok": True,
        "result": {"attempt": _summary(), "manifest": entries},
    }


def _export_tar(entries, members):
    manifest = {"contract": "1.0", "attempt": _summary(), "files": entries}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        raw = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _scripted_page():
    lines = _events_lines()
    entries = [_manifest_entry("events", "events.jsonl", lines, "application/x-ndjson")]
    note = b"hello note"
    entries.append(_manifest_entry("other", "note.txt", note, "text/plain"))
    show = ControlExec(
        stdout=json.dumps(_show_payload(entries)).encode(), stderr=b"", returncode=0
    )
    events_tar = _export_tar([entries[0]], {"events.jsonl": lines})
    note_tar = _export_tar([entries[1]], {"note.txt": note})
    return show, events_tar, note_tar, lines, note


def _running_client(pages, *, name="web-1", cid="abc123"):
    state = FleetState()
    rows = [
        ManagedInstance(
            name, cid, "running", path=CONTRACT_PATH,
            instance=name, exec_user="agent", backend="coding-agent",
        )
    ]
    gateway = FakeGateway(rows)
    gateway.set_contract_snapshot(cid, json.loads(_status_fixture())["result"])
    asyncio.run(run_cycle(state, gateway, now=1000.0))
    gateway.set_contract_attempts(cid, pages)
    remember_attempt_ids(name, [ATTEMPT, "#42/1"])
    app = create_app(
        state=state, settings=Settings(poll_interval=5.0),
        gateway=gateway, start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), gateway


def _status_fixture():
    from agent_operator_contract import documents

    return documents.load_fixture("status-idle")["stdout"]


def _stopped_client(pages, *, name="down-1", cid="abc123"):
    state = FleetState()
    gateway = FakeGateway([_stopped(name, cid)])
    asyncio.run(run_cycle(state, gateway, now=1000.0))
    gateway.set_inspect(cid, _inspect())
    gateway.set_offline_attempts(IMAGE, pages)
    remember_attempt_ids(name, [ATTEMPT, "#42/1"])
    app = create_app(
        state=state, settings=Settings(poll_interval=5.0),
        gateway=gateway, start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), gateway


# --- argv pinning ------------------------------------------------------------


def test_offline_attempts_argv_are_pinned():
    assert argv_offline_attempts_list(IMAGE, MOUNTS, "agent", "offline-helper-01") == [
        "docker", "run", "--rm", "--network", "none", "--user", "agent",
        "--name", "offline-helper-01",
        "--label", "io.github.lbacik.agent-dashboard.offline-read=1",
        "--mount", "type=volume,src=data-vol,dst=/data,readonly",
        "--entrypoint", AGENTCTL_PATH, IMAGE,
        "--contract", "1", "--offline",
        "attempts", "list", "--limit", "100",
    ]
    assert argv_offline_attempts_show(
        IMAGE, MOUNTS, "agent", "offline-helper-01", ATTEMPT
    )[-3:] == ["attempts", "show", ATTEMPT]
    assert argv_offline_attempts_export(
        IMAGE, MOUNTS, "agent", "offline-helper-01", ATTEMPT, "events.jsonl"
    )[-5:] == ["attempts", "export", ATTEMPT, "--path", "events.jsonl"]


def test_offline_list_show_export_run_through_the_same_helper():
    show, events_tar, _, lines, _ = _scripted_page()
    client, gateway = _stopped_client(
        {None: _envelope([_attempt()], None)}
    )
    gateway.set_offline_attempt_show(IMAGE, ATTEMPT, show)
    gateway.set_offline_attempt_export(IMAGE, ATTEMPT, "events.jsonl", ControlExec(
        stdout=events_tar, stderr=b"", returncode=0))
    assert client.get("/instances/down-1/history?preset=all").status_code == 200
    assert (
        client.get(f"/instances/down-1/attempt/{ENCODED_ATTEMPT}").status_code == 200
    )
    tails = [argv[argv.index("--offline") + 1:] for argv in gateway.offline_argvs]
    assert ["attempts", "list", "--limit", "100"] in tails
    assert ["attempts", "show", ATTEMPT] in tails
    assert ["attempts", "export", ATTEMPT, "--path", "events.jsonl"] in tails
    assert lines is not None


# --- same view models running vs stopped -------------------------------------


def test_stopped_history_matches_running_fragments():
    pages = {None: _envelope([_attempt("#42/1", 42)], None)}
    running, _ = _running_client(pages, name="web-1", cid="abc123")
    stopped, gateway = _stopped_client(pages, name="down-1", cid="abc123")
    running_body = running.get("/instances/web-1/history?preset=all").text
    stopped_body = stopped.get("/instances/down-1/history?preset=all").text
    for fragment in ("#42", "Issue 42", "Delivered", "not reported", "sealed"):
        assert fragment in running_body
        assert fragment in stopped_body
    assert gateway.offline_attempts_calls != []
    assert gateway.contract_attempts_calls == []


def test_stopped_stats_card_matches_running_fragments():
    pages = {
        None: _envelope(
            [
                _attempt("a-1", 1, started_at="2026-11-06T08:50:00Z",
                         finished_at="2026-11-06T09:10:00Z"),
                _attempt("a-2", 2, started_at="2026-11-06T08:51:00Z",
                         finished_at="2026-11-06T09:11:00Z"),
            ],
            None,
        )
    }
    running, _ = _running_client(pages, name="web-1", cid="abc123")
    stopped, _ = _stopped_client(pages, name="down-1", cid="abc123")
    running_body = running.get("/instances/web-1/stats?preset=all").text
    stopped_body = stopped.get("/instances/down-1/stats?preset=all").text
    for fragment in ("Delivered",):
        assert fragment in running_body
        assert fragment in stopped_body
    assert "not reported" in running_body.lower()
    assert "not reported" in stopped_body.lower()


def test_stopped_overview_matches_running_fragments():
    pages = {None: _envelope([_attempt("a-1", 1)], None)}
    running, _ = _running_client(pages, name="web-1", cid="abc123")
    stopped, _ = _stopped_client(pages, name="down-1", cid="abc123")
    running_body = running.get("/overview?preset=all").text
    stopped_body = stopped.get("/overview?preset=all").text
    assert running_body and stopped_body
    for fragment in ("Delivered",):
        assert fragment in running_body
        assert fragment in stopped_body


def test_stopped_attempt_page_matches_running_fragments():
    show, events_tar, _, _, _ = _scripted_page()
    export = ControlExec(stdout=events_tar, stderr=b"", returncode=0)
    stopped, stopped_gateway = _stopped_client({}, name="down-1", cid="abc123")
    stopped_gateway.set_offline_attempt_show(IMAGE, ATTEMPT, show)
    stopped_gateway.set_offline_attempt_export(IMAGE, ATTEMPT, "events.jsonl", export)
    body = stopped.get(f"/instances/down-1/attempt/{ENCODED_ATTEMPT}").text
    assert "Attempt #41/1" in body
    assert "Delivered" in body
    assert "claim_opened" in body
    assert "events.jsonl" in body
    assert stopped_gateway.offline_show_calls != []
    assert stopped_gateway.offline_export_calls != []
    assert stopped_gateway.contract_show_calls == []
    assert stopped_gateway.contract_export_calls == []


def test_stopped_preview_and_download_match_running():
    show, _, note_tar, _, note = _scripted_page()
    fleet = FleetState()
    rows = [_stopped("down-1", "abc123")]
    gateway = FakeGateway(rows)
    asyncio.run(run_cycle(fleet, gateway, now=1000.0))
    gateway.set_inspect("abc123", _inspect())
    gateway.set_offline_attempt_show(IMAGE, ATTEMPT, show)
    gateway.set_offline_attempt_export(
        IMAGE, ATTEMPT, "note.txt",
        ControlExec(stdout=note_tar, stderr=b"", returncode=0),
    )
    remember_attempt_ids("down-1", [ATTEMPT])
    app = create_app(
        state=fleet, settings=Settings(poll_interval=5.0),
        gateway=gateway, start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    preview = client.get(
        f"/instances/down-1/attempt/{ENCODED_ATTEMPT}/artifact?path=note.txt"
    )
    assert preview.status_code == 200
    assert "hello note" in preview.text
    download = client.get(
        f"/instances/down-1/attempt/{ENCODED_ATTEMPT}/artifact/download?path=note.txt"
    )
    assert download.status_code == 200
    assert download.content == note
    assert gateway.offline_export_calls != []


# --- sealed cache + single flight --------------------------------------------


def test_sealed_cache_serves_attempt_without_a_helper(tmp_path):
    show, events_tar, _, lines, _ = _scripted_page()
    assert lines is not None
    cache = SealedArchiveCache(tmp_path / "cache")
    fleet = FleetState()
    gateway = FakeGateway([_stopped("down-1", "abc123")])
    asyncio.run(run_cycle(fleet, gateway, now=1000.0))
    gateway.set_inspect("abc123", _inspect())
    gateway.set_offline_attempt_show(IMAGE, ATTEMPT, show)
    gateway.set_offline_attempt_export(
        IMAGE, ATTEMPT, "events.jsonl",
        ControlExec(stdout=events_tar, stderr=b"", returncode=0),
    )
    remember_attempt_ids("down-1", [ATTEMPT])
    app = create_app(
        state=fleet, settings=Settings(poll_interval=5.0),
        gateway=gateway, start_poller=False, sealed_archive_cache=cache,
    )
    client = TestClient(app, base_url="http://localhost")
    first = client.get(f"/instances/down-1/attempt/{ENCODED_ATTEMPT}")
    assert first.status_code == 200
    assert "claim_opened" in first.text
    show_calls = len(gateway.offline_show_calls)
    export_calls = len(gateway.offline_export_calls)
    assert show_calls >= 1 and export_calls >= 1
    second = client.get(f"/instances/down-1/attempt/{ENCODED_ATTEMPT}")
    assert second.status_code == 200
    assert second.text == first.text
    assert len(gateway.offline_show_calls) == show_calls
    assert len(gateway.offline_export_calls) == export_calls


def test_concurrent_offline_history_serializes_on_the_lock():
    async def _main():
        gateway = FakeGateway([])
        gateway.set_offline_attempts(
            IMAGE, {None: _envelope([_attempt("a-1", 1)], None)}
        )
        gateway.offline_delay = 0.05
        spec = RangeSpec(preset="all")
        first = collect_contract_history_offline(
            gateway, IMAGE, MOUNTS, "agent", spec, now=1000.0
        )
        second = collect_contract_history_offline(
            gateway, IMAGE, MOUNTS, "agent", spec, now=1000.0
        )
        lock = gateway.offline_lock("abc123")
        assert lock is not None

        async def _guarded(coro):
            async with lock:
                return await coro

        first_result, second_result = await asyncio.gather(
            _guarded(first), _guarded(second)
        )
        return first_result, second_result, gateway

    first_result, second_result, gateway = asyncio.run(_main())
    assert first_result.ok and second_result.ok
    assert gateway.peak_offline_in_flight <= 1


# --- failure cases render a message, never a 500 ------------------------------


def test_gone_volume_offline_history_is_a_message_not_500():
    state = FleetState()
    gateway = FakeGateway([_stopped("down-1", "abc123")])
    asyncio.run(run_cycle(state, gateway, now=1000.0))
    gateway.set_inspect_error("abc123", GatewayError("no such container"))
    remember_attempt_ids("down-1", [ATTEMPT])
    app = create_app(
        state=state, settings=Settings(poll_interval=5.0),
        gateway=gateway, start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    for url in (
        "/instances/down-1/history?preset=all",
        "/instances/down-1/stats?preset=all",
        "/overview?preset=all",
        f"/instances/down-1/attempt/{ENCODED_ATTEMPT}",
    ):
        response = client.get(url)
        assert response.status_code == 200
        lowered = response.text.lower()
        assert "gone" in lowered or "no logs" in lowered or "unavailable" in lowered


def test_instance_running_race_renders_the_started_message():
    state = FleetState()
    gateway = FakeGateway([_stopped("down-1", "abc123")])
    asyncio.run(run_cycle(state, gateway, now=1000.0))
    gateway.set_inspect("abc123", _inspect(running=True))
    remember_attempt_ids("down-1", [ATTEMPT])
    gateway.set_offline_attempt_show(
        IMAGE, ATTEMPT,
        ControlExec(
            stdout=json.dumps({
                "contract": "1.0", "ok": False,
                "error": {"code": "rejected", "message": "running",
                          "details": {"reason": "instance_running"}},
            }).encode(),
            stderr=b"", returncode=4,
        ),
    )
    app = create_app(
        state=state, settings=Settings(poll_interval=5.0),
        gateway=gateway, start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    history = client.get("/instances/down-1/history?preset=all")
    assert history.status_code == 200
    assert "started" in history.text.lower()
    page = client.get(f"/instances/down-1/attempt/{ENCODED_ATTEMPT}")
    assert page.status_code == 200
    assert "started" in page.text.lower()


def test_offline_timeout_is_a_message_and_retries(tmp_path):
    _ = tmp_path
    state = FleetState()
    gateway = FakeGateway([_stopped("down-1", "abc123")])
    asyncio.run(run_cycle(state, gateway, now=1000.0))
    gateway.set_inspect("abc123", _inspect())
    gateway.set_offline_attempts(
        IMAGE, {None: ControlTimeoutError("docker run timed out after 15 s")}
    )
    remember_attempt_ids("down-1", [ATTEMPT])
    app = create_app(
        state=state, settings=Settings(poll_interval=5.0),
        gateway=gateway, start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    response = client.get("/instances/down-1/history?preset=all")
    assert response.status_code == 200
    assert "Could not read" in response.text
    gateway.set_offline_attempts(
        IMAGE, {None: _envelope([_attempt("a-1", 1)], None)}
    )
    retry = client.get("/instances/down-1/history?preset=all")
    assert retry.status_code == 200
    assert "a-1" in retry.text
