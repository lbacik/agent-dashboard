"""Sealed Attempt Archive cache: host-side reuse of sealed archives.

The dashboard keeps Sealed Attempt Archives under the XDG cache home, so
re-reading old evidence is fast and never touches the instance again:
Attempt pages, previews and downloads of a cached Sealed Attempt make no
Docker call. Unsealed archives are never cached, a cached manifest is never
updated, every stored file is verified by sha256 on write and on read (a
failed read verification drops the entry and fetches again), and the total
size is bounded with least-recently-used eviction.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import tarfile
import time

import pytest
from contract_replay import ReplayProcessRunner
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import (
    DEFAULT_SEALED_ARCHIVE_CACHE_MAX_BYTES,
    Settings,
    load_settings,
)
from agent_dashboard.contract_history import (
    clear_known_attempt_ids,
    remember_attempt_ids,
)
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    CONTRACT_PATH,
    ControlExec,
    DockerGateway,
    ManagedInstance,
)
from agent_dashboard.sealed_archive_cache import (
    SealedArchiveCache,
    SealedArchiveKey,
    default_cache_dir,
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


def _key(attempt: str = ATTEMPT, **overrides) -> SealedArchiveKey:
    fields = {
        "instance": INST,
        "backend": "coding-agent",
        "repository": "example-owner/example-repo",
        "attempt_id": attempt,
    }
    fields.update(overrides)
    return SealedArchiveKey(**fields)


def _attempt(*, sealed: bool = True, with_sealed: bool = True) -> dict:
    entry: dict = {
        "attempt_id": ATTEMPT,
        "issue": {"number": 41, "title": "Issue 41"},
        "started_at": "2026-11-06T08:01:10Z",
        "lifecycle_state": "finished",
        "attention_required": False,
    }
    if with_sealed:
        entry["sealed"] = sealed
        entry["sealed_at"] = "2026-11-06T08:45:01Z" if sealed else None
    if sealed:
        entry["finished_at"] = "2026-11-06T08:44:52Z"
        entry["outcome"] = {"label": "Delivered", "native": "delivered",
                            "reason": None}
        entry["delivery"] = {
            "pull_request": {
                "number": 77,
                "url": "https://github.com/example-owner/example-repo/pull/77",
            }
        }
    else:
        entry["finished_at"] = None
        entry["outcome"] = None
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


def _manifest(*entries: dict) -> list[dict]:
    return list(entries)


def _events_lines() -> bytes:
    return (
        json.dumps(
            {"at": "2026-11-06T08:01:10Z", "level": "info",
             "event": "claim_opened", "text": "claimed",
             "attempt_id": ATTEMPT, "issue": 41}
        ).encode()
        + b"\n"
    )


def _events_manifest(data: bytes) -> list[dict]:
    return _manifest(
        _manifest_entry("events", "events.jsonl", data, "application/x-ndjson")
    )


# --- location and permissions ------------------------------------------------


def test_default_dir_uses_xdg_cache_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_cache_dir() == (
        tmp_path / "xdg" / "agent-dashboard" / "sealed-attempts"
    )


def test_default_dir_falls_back_to_home_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_cache_dir() == (
        tmp_path / ".cache" / "agent-dashboard" / "sealed-attempts"
    )


def test_cache_creates_directory_with_mode_0700(tmp_path):
    target = tmp_path / "cache"
    SealedArchiveCache(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_cache_repairs_existing_directory_permissions(tmp_path):
    target = tmp_path / "cache"
    target.mkdir()
    os.chmod(target, 0o755)
    SealedArchiveCache(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_stored_files_have_mode_0600(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    key = _key()
    data = _events_lines()
    assert cache.store_show(key, _attempt(), _events_manifest(data))
    assert cache.store_artifact(key, "events.jsonl", data)
    modes = {
        path.name: stat.S_IMODE(path.stat().st_mode)
        for path in (tmp_path / "cache" / key.entry_name()).iterdir()
    }
    assert modes
    assert set(modes.values()) == {0o600}
    entry_dir = tmp_path / "cache" / key.entry_name()
    assert stat.S_IMODE(entry_dir.stat().st_mode) == 0o700


# --- contents and keys ---------------------------------------------------------


def test_round_trip_show_and_artifact(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    key = _key()
    data = _events_lines()
    manifest = _events_manifest(data)
    assert cache.store_show(key, _attempt(), manifest)
    assert cache.store_artifact(key, "events.jsonl", data)
    attempt, stored_manifest = cache.load_show(key)
    assert attempt["attempt_id"] == ATTEMPT
    assert attempt["sealed"] is True
    assert stored_manifest == manifest
    assert cache.load_artifact(key, "events.jsonl") == data


def test_entries_are_keyed_by_instance_backend_repository_and_attempt(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    data = _events_lines()
    manifest = _events_manifest(data)
    assert cache.store_show(_key(), _attempt(), manifest)
    assert cache.load_show(_key()) is not None
    assert cache.load_show(_key(instance="other-1")) is None
    assert cache.load_show(_key(backend="other-backend")) is None
    assert cache.load_show(_key(repository="other-owner/other-repo")) is None
    assert cache.load_show(_key(attempt="#42/1")) is None
    # The misses above dropped nothing: the original entry still reads.
    assert cache.load_show(_key()) is not None


def test_unknown_artifact_path_is_a_miss(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    key = _key()
    data = _events_lines()
    assert cache.store_show(key, _attempt(), _events_manifest(data))
    assert cache.load_artifact(key, "note.txt") is None
    assert cache.load_show(key) is not None


# --- only sealed archives are written -------------------------------------------


def test_unsealed_archives_are_never_cached(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    key = _key()
    data = _events_lines()
    assert cache.store_show(key, _attempt(sealed=False), _events_manifest(data)) is False
    assert cache.load_show(key) is None
    assert list((tmp_path / "cache").iterdir()) == []


def test_outcome_without_sealed_is_never_treated_as_final(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    key = _key()
    data = _events_lines()
    assert cache.store_show(
        key, _attempt(sealed=False, with_sealed=False), _events_manifest(data)
    ) is False
    assert cache.load_show(key) is None


def test_manifest_without_events_member_is_refused(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    key = _key()
    data = b"note"
    manifest = _manifest(_manifest_entry("other", "note.txt", data, "text/plain"))
    assert cache.store_show(key, _attempt(), manifest) is False
    assert cache.load_show(key) is None


def test_cached_manifest_is_never_updated(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    key = _key()
    first = _events_lines()
    second = first + first
    assert cache.store_show(key, _attempt(), _events_manifest(first))
    # A second show for the same Attempt keeps the first manifest.
    assert cache.store_show(key, _attempt(), _events_manifest(second))
    _, manifest = cache.load_show(key)
    assert manifest == _events_manifest(first)
    assert cache.store_artifact(key, "events.jsonl", second) is False
    assert cache.store_artifact(key, "events.jsonl", first) is True


# --- sha256 verification on write and on read --------------------------------------


def test_write_verifies_size_and_sha256(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    key = _key()
    data = _events_lines()
    assert cache.store_show(key, _attempt(), _events_manifest(data))
    assert cache.store_artifact(key, "events.jsonl", data + b"tampered") is False
    assert cache.load_artifact(key, "events.jsonl") is None
    # A well-formed manifest with a wrong sha256 stores, but no bytes ever
    # verify against it.
    other = dict(_events_manifest(data)[0], sha256="0" * 64)
    other_key = _key(attempt="#9/9")
    assert cache.store_show(other_key, _attempt(), [other]) is True
    assert cache.store_artifact(other_key, "events.jsonl", data) is False
    assert cache.load_artifact(other_key, "events.jsonl") is None


def test_failed_read_verification_drops_the_entry(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    key = _key()
    data = _events_lines()
    assert cache.store_show(key, _attempt(), _events_manifest(data))
    assert cache.store_artifact(key, "events.jsonl", data)
    blob = next(
        path for path in (tmp_path / "cache" / key.entry_name()).iterdir()
        if path.name.startswith("blob-")
    )
    blob.write_bytes(b"corrupted")
    assert cache.load_artifact(key, "events.jsonl") is None
    # The whole entry is dropped, so the header reads as a miss too.
    assert not (tmp_path / "cache" / key.entry_name()).exists()
    assert cache.load_show(key) is None


def test_corrupt_entry_recovers_as_a_miss(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache")
    key = _key()
    data = _events_lines()
    assert cache.store_show(key, _attempt(), _events_manifest(data))
    assert cache.store_artifact(key, "events.jsonl", data)
    (tmp_path / "cache" / key.entry_name() / "meta.json").write_bytes(b"{broken")
    assert cache.load_show(key) is None
    assert not (tmp_path / "cache" / key.entry_name()).exists()
    # The entry can be written again after the drop.
    assert cache.store_show(key, _attempt(), _events_manifest(data))
    assert cache.load_show(key) is not None


# --- bounded size with LRU eviction --------------------------------------------------


def _sized_entry(number: int, size: int = 500) -> tuple[dict, list[dict], bytes]:
    attempt = _attempt()
    attempt["attempt_id"] = f"#{number}/1"
    data = b"%d" % number * size
    return attempt, _events_manifest(data), data


def test_total_size_is_bounded_with_lru_eviction(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache", max_bytes=3000)
    first, first_manifest, first_data = _sized_entry(1)
    second, second_manifest, second_data = _sized_entry(2)
    assert cache.store_show(_key(attempt="#1/1"), first, first_manifest)
    assert cache.store_artifact(_key(attempt="#1/1"), "events.jsonl", first_data)
    time.sleep(0.02)
    assert cache.store_show(_key(attempt="#2/1"), second, second_manifest)
    assert cache.store_artifact(_key(attempt="#2/1"), "events.jsonl", second_data)
    assert cache.total_bytes() <= 3000
    # Touch the first entry so the second is least recently used.
    time.sleep(0.02)
    assert cache.load_show(_key(attempt="#1/1")) is not None
    time.sleep(0.02)
    third, third_manifest, third_data = _sized_entry(3)
    assert cache.store_show(_key(attempt="#3/1"), third, third_manifest)
    assert cache.store_artifact(_key(attempt="#3/1"), "events.jsonl", third_data)
    assert cache.total_bytes() <= 3000
    assert cache.load_show(_key(attempt="#2/1")) is None
    assert cache.load_show(_key(attempt="#1/1")) is not None
    assert cache.load_show(_key(attempt="#3/1")) is not None


def test_oversize_single_entry_still_evicts(tmp_path):
    cache = SealedArchiveCache(tmp_path / "cache", max_bytes=100)
    attempt, manifest, data = _sized_entry(1, size=500)
    # The show alone already exceeds the bound, so it is evicted at once:
    # the later artifact write refuses and the header reads as a miss.
    assert cache.store_show(_key(), attempt, manifest)
    assert cache.store_artifact(_key(), "events.jsonl", data) is False
    assert cache.load_show(_key()) is None
    assert cache.total_bytes() <= 100


# --- configuration ------------------------------------------------------------------


def test_cache_config_defaults(monkeypatch):
    for name in (
        "AGENT_DASHBOARD_SEALED_ARCHIVE_CACHE_MAX_BYTES",
        "AGENT_DASHBOARD_SEALED_ARCHIVE_CACHE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings([])
    assert settings.sealed_archive_cache_max_bytes == (
        DEFAULT_SEALED_ARCHIVE_CACHE_MAX_BYTES
    )
    assert settings.sealed_archive_cache_dir is None


def test_cache_config_env_and_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DASHBOARD_SEALED_ARCHIVE_CACHE_MAX_BYTES", "1048576")
    monkeypatch.setenv("AGENT_DASHBOARD_SEALED_ARCHIVE_CACHE_DIR", str(tmp_path))
    settings = load_settings([])
    assert settings.sealed_archive_cache_max_bytes == 1048576
    assert settings.sealed_archive_cache_dir == str(tmp_path)
    flagged = load_settings(
        ["--sealed-archive-cache-max-bytes", "2048",
         "--sealed-archive-cache-dir", str(tmp_path / "flag")]
    )
    assert flagged.sealed_archive_cache_max_bytes == 2048
    assert flagged.sealed_archive_cache_dir == str(tmp_path / "flag")


@pytest.mark.parametrize("value", ["0", "-100"])
def test_cache_config_rejects_non_positive_bounds(value, monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_SEALED_ARCHIVE_CACHE_MAX_BYTES", value)
    with pytest.raises(ValueError):
        load_settings([])
    with pytest.raises(ValueError):
        load_settings(["--sealed-archive-cache-max-bytes", value])


# --- Attempt pages, previews and downloads over HTTP -----------------------------------


def _status_result() -> dict:
    from agent_operator_contract import documents

    return json.loads(documents.load_fixture("status-idle")["stdout"])["result"]


def _show_payload(entries: list[dict], *, sealed: bool = True) -> dict:
    return {
        "contract": "1.0",
        "ok": True,
        "result": {"attempt": _attempt(sealed=sealed), "manifest": entries},
    }


def _export_tar(entries: list[dict], members: dict[str, bytes]) -> bytes:
    manifest = {
        "contract": "1.0",
        "attempt": _attempt(),
        "files": entries,
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        raw_manifest = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw_manifest)
        tar.addfile(info, io.BytesIO(raw_manifest))
        for member_name, member_data in members.items():
            info = tarfile.TarInfo(member_name)
            info.size = len(member_data)
            tar.addfile(info, io.BytesIO(member_data))
    return buf.getvalue()


def _page_entries(data: bytes) -> list[dict]:
    return _events_manifest(data)


def _make_client(
    show: ControlExec | Exception,
    export: ControlExec | Exception,
    cache: SealedArchiveCache,
    *,
    sealed: bool = True,
) -> tuple[TestClient, FakeGateway]:
    fleet_state = FleetState()
    rows = [
        ManagedInstance(
            INST, CID, "running", path=CONTRACT_PATH,
            instance=INST, exec_user=USER, backend="coding-agent",
        )
    ]
    gateway = FakeGateway(rows)
    gateway.set_contract_snapshot(CID, _status_result())
    asyncio.run(run_cycle(fleet_state, gateway, now=1000.0))
    remember_attempt_ids(INST, [ATTEMPT])
    gateway.set_contract_attempt_show(CID, ATTEMPT, show)
    gateway.set_contract_attempt_export(CID, ATTEMPT, "events.jsonl", export)
    app = create_app(
        state=fleet_state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
        sealed_archive_cache=cache,
    )
    return TestClient(app, base_url="http://localhost"), gateway


def _scripted_page(*, sealed: bool = True) -> tuple[ControlExec, ControlExec]:
    data = _events_lines()
    stream = _export_tar(_page_entries(data), {"events.jsonl": data})
    return (
        ControlExec(
            stdout=json.dumps(_show_payload(_page_entries(data), sealed=sealed)).encode(),
            stderr=b"",
            returncode=0,
        ),
        ControlExec(stdout=stream, stderr=b"", returncode=0),
    )


def _page_url() -> str:
    return f"/instances/{INST}/attempt/{ENCODED_ATTEMPT}"


def test_attempt_page_second_hit_makes_no_docker_call(tmp_path):
    show, export = _scripted_page()
    cache = SealedArchiveCache(tmp_path / "cache")
    client, gateway = _make_client(show, export, cache)
    first = client.get(_page_url())
    assert first.status_code == 200
    assert "claim_opened" in first.text
    assert gateway.contract_show_calls == [(CID, USER, ATTEMPT)]
    assert gateway.contract_export_calls == [(CID, USER, ATTEMPT, "events.jsonl")]
    second = client.get(_page_url())
    assert second.status_code == 200
    assert second.text == first.text
    assert gateway.contract_show_calls == [(CID, USER, ATTEMPT)]
    assert gateway.contract_export_calls == [(CID, USER, ATTEMPT, "events.jsonl")]


def test_attempt_preview_second_hit_makes_no_docker_call(tmp_path):
    data = _events_lines()
    note = b"hello note"
    entries = _page_entries(data) + [
        _manifest_entry("other", "note.txt", note, "text/plain")
    ]
    show = ControlExec(
        stdout=json.dumps(_show_payload(entries)).encode(), stderr=b"", returncode=0
    )
    events_tar = _export_tar([entries[0]], {"events.jsonl": data})
    note_tar = _export_tar([entries[1]], {"note.txt": note})
    cache = SealedArchiveCache(tmp_path / "cache")
    fleet_state = FleetState()
    rows = [
        ManagedInstance(
            INST, CID, "running", path=CONTRACT_PATH,
            instance=INST, exec_user=USER, backend="coding-agent",
        )
    ]
    gateway = FakeGateway(rows)
    gateway.set_contract_snapshot(CID, _status_result())
    asyncio.run(run_cycle(fleet_state, gateway, now=1000.0))
    remember_attempt_ids(INST, [ATTEMPT])
    gateway.set_contract_attempt_show(CID, ATTEMPT, show)
    gateway.set_contract_attempt_export(
        CID, ATTEMPT, "events.jsonl",
        ControlExec(stdout=events_tar, stderr=b"", returncode=0),
    )
    gateway.set_contract_attempt_export(
        CID, ATTEMPT, "note.txt",
        ControlExec(stdout=note_tar, stderr=b"", returncode=0),
    )
    app = create_app(
        state=fleet_state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
        sealed_archive_cache=cache,
    )
    client = TestClient(app, base_url="http://localhost")
    preview_url = f"/instances/{INST}/attempt/{ENCODED_ATTEMPT}/artifact?path=note.txt"
    download_url = (
        f"/instances/{INST}/attempt/{ENCODED_ATTEMPT}/artifact/download?path=note.txt"
    )
    first_preview = client.get(preview_url)
    assert first_preview.status_code == 200
    assert "hello note" in first_preview.text
    first_download = client.get(download_url)
    assert first_download.status_code == 200
    assert first_download.content == note
    show_calls = list(gateway.contract_show_calls)
    export_calls = list(gateway.contract_export_calls)
    assert show_calls != []
    assert export_calls != []
    second_preview = client.get(preview_url)
    assert second_preview.status_code == 200
    assert second_preview.text == first_preview.text
    second_download = client.get(download_url)
    assert second_download.status_code == 200
    assert second_download.content == note
    assert gateway.contract_show_calls == show_calls
    assert gateway.contract_export_calls == export_calls


def test_unsealed_attempt_is_never_cached_over_http(tmp_path):
    show, export = _scripted_page(sealed=False)
    cache = SealedArchiveCache(tmp_path / "cache")
    client, gateway = _make_client(show, export, cache, sealed=False)
    assert "still changing" in client.get(_page_url()).text
    assert "still changing" in client.get(_page_url()).text
    assert len(gateway.contract_show_calls) == 2
    assert len(gateway.contract_export_calls) == 2
    assert list((tmp_path / "cache").iterdir()) == []


def test_corrupt_entry_recovers_by_fetching_again(tmp_path):
    show, export = _scripted_page()
    cache = SealedArchiveCache(tmp_path / "cache")
    client, gateway = _make_client(show, export, cache)
    first = client.get(_page_url())
    assert first.status_code == 200
    assert len(gateway.contract_export_calls) == 1
    entry_dir = next((tmp_path / "cache").iterdir())
    blob = next(path for path in entry_dir.iterdir() if path.name.startswith("blob-"))
    blob.write_bytes(b"corrupted")
    second = client.get(_page_url())
    assert second.status_code == 200
    assert second.text == first.text
    assert len(gateway.contract_export_calls) == 2
    # The refetch re-stored the entry: the next read is served from cache.
    third = client.get(_page_url())
    assert third.status_code == 200
    assert third.text == first.text
    assert len(gateway.contract_export_calls) == 2


def test_cached_manifest_is_never_updated_over_http(tmp_path):
    data = _events_lines()
    first_entries = _page_entries(data)
    show, export = _scripted_page()
    cache = SealedArchiveCache(tmp_path / "cache")
    client, gateway = _make_client(show, export, cache)
    assert client.get(_page_url()).status_code == 200
    note = b"late note"
    second_entries = first_entries + [
        _manifest_entry("other", "note.txt", note, "text/plain")
    ]
    gateway.set_contract_attempt_show(
        CID,
        ATTEMPT,
        ControlExec(
            stdout=json.dumps(_show_payload(second_entries)).encode(),
            stderr=b"",
            returncode=0,
        ),
    )
    body = client.get(_page_url()).text
    assert "note.txt" not in body
    assert gateway.contract_show_calls == [(CID, USER, ATTEMPT)]


# --- replay runner: no Docker call at all -----------------------------------------


def _replay_show(entries: list[dict], *, sealed: bool = True) -> dict:
    return {
        "argv": ["--contract", "1", "attempts", "show", ATTEMPT],
        "exit_code": 0,
        "stdout": json.dumps(_show_payload(entries, sealed=sealed)),
        "stderr": "",
    }


def _replay_export(path: str, tar_bytes: bytes) -> dict:
    return {
        "argv": ["--contract", "1", "attempts", "export", ATTEMPT, "--path", path],
        "exit_code": 0,
        "stdout": tar_bytes,
        "stderr": b"",
    }


def test_replay_cached_reads_make_no_docker_call(tmp_path):
    from agent_operator_contract import documents

    data = _events_lines()
    note = b"hello note"
    entries = _page_entries(data) + [
        _manifest_entry("other", "note.txt", note, "text/plain")
    ]
    events_tar = _export_tar([entries[0]], {"events.jsonl": data})
    note_tar = _export_tar([entries[1]], {"note.txt": note})
    # First round: the page reads show plus the events export; the preview
    # reuses the cached header (no second show) and fetches only its member;
    # the download is then fully cached. Each recording answers once.
    fixtures = (
        [documents.load_fixture("status-idle")]
        + [_replay_show(entries)]
        + [
            _replay_export("events.jsonl", events_tar),
            _replay_export("note.txt", note_tar),
        ]
    )
    runner = ReplayProcessRunner(fixtures)
    gateway = DockerGateway(process_runner=runner)

    async def _list() -> list[ManagedInstance]:
        return [
            ManagedInstance(
                INST, CID, "running", path=CONTRACT_PATH,
                instance=INST, exec_user=USER, backend="coding-agent",
            )
        ]

    gateway.list_instances = _list  # type: ignore[method-assign]
    state = FleetState()
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=time.time()))
    remember_attempt_ids(INST, [ATTEMPT])
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
        sealed_archive_cache=SealedArchiveCache(tmp_path / "cache"),
    )
    client = TestClient(app, base_url="http://localhost")
    preview_url = f"/instances/{INST}/attempt/{ENCODED_ATTEMPT}/artifact?path=note.txt"
    download_url = (
        f"/instances/{INST}/attempt/{ENCODED_ATTEMPT}/artifact/download?path=note.txt"
    )
    first_page = client.get(_page_url())
    assert first_page.status_code == 200
    assert "claim_opened" in first_page.text
    first_preview = client.get(preview_url)
    assert first_preview.status_code == 200
    assert "hello note" in first_preview.text
    first_download = client.get(download_url)
    assert first_download.status_code == 200
    assert first_download.content == note
    assert runner.remaining == 0
    calls = list(runner.calls)
    # Every repeat read is served from the host cache: no Docker call runs.
    second_page = client.get(_page_url())
    assert second_page.status_code == 200
    assert second_page.text == first_page.text
    second_preview = client.get(preview_url)
    assert second_preview.status_code == 200
    assert second_preview.text == first_preview.text
    second_download = client.get(download_url)
    assert second_download.status_code == 200
    assert second_download.content == note
    assert runner.calls == calls
    assert runner.remaining == 0
