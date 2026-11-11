"""Sealed Attempt Archive cache: host-side reuse of sealed archives (contract major 1).

The dashboard keeps Sealed Attempt Archives on the host under the XDG cache
home (``~/.cache`` fallback). Re-reading old evidence is then fast and never
touches the instance again: Attempt pages, previews and downloads of a cached
Sealed Attempt make no Docker call. Unsealed archives are never cached, a
cached manifest is never updated, and an outcome without ``sealed`` is never
treated as final.

Entries are keyed by ``(instance name, backend, repository, Attempt ID)`` and
hold the AttemptSummary, the archive manifest and each artifact fetched so
far. Every stored artifact is verified (size and ``sha256``) against its
manifest entry before it is written and again on every read; a failed read
verification drops the whole entry so the next request fetches again. The
total size is bounded (configurable) with least-recently-used eviction.

Layout (directory mode 0700, file mode 0600)::

    <dir>/<sha256 of the key>/
        meta.json            # key, AttemptSummary, manifest, stored artifacts
        blob-<sha256 path>.bin  # one verified artifact per fetched path

Tolerance: a corrupt or foreign entry is dropped and reads as a miss, never
an error; unknown fields in ``meta.json`` are ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Subdirectory of the XDG cache home holding the sealed archives.
CACHE_DIR_NAME = "agent-dashboard/sealed-attempts"

#: Version stamp inside ``meta.json``; a foreign version reads as a miss.
CACHE_VERSION = 1

#: ``meta.json`` file name inside one entry directory.
META_FILENAME = "meta.json"

#: Default bound for the total cache size: 256 MiB.
DEFAULT_MAX_BYTES = 256 * 1024 * 1024


def default_cache_dir() -> Path:
    """Cache directory: ``$XDG_CACHE_HOME`` (``~/.cache`` fallback) plus the name."""
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        root = Path(base)
    else:
        root = Path.home() / ".cache"
    return root / CACHE_DIR_NAME


@dataclass(frozen=True)
class SealedArchiveKey:
    """Cache key: instance name, backend, repository and Attempt ID.

    Empty strings stand in for an unknown backend or repository; the key is
    stable while the fleet row is stable, so repeated reads hit one entry.
    """

    instance: str
    backend: str
    repository: str
    attempt_id: str

    def entry_name(self) -> str:
        """Directory name for this key: the sha256 of its canonical form."""
        canonical = json.dumps(
            [self.instance, self.backend, self.repository, self.attempt_id],
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _manifest_ok(manifest: Any) -> bool:
    """True when the manifest is a usable archive listing.

    Every entry must carry its ``sha256`` (the contract's manifest entry
    shape) and the ``events.jsonl`` member (kind ``events``) must be listed,
    mirroring the Attempt page parser. Never raises.
    """
    from agent_operator_contract import documents

    if not isinstance(manifest, list) or not manifest:
        return False
    for entry in manifest:
        if documents.validate_manifest_entry(entry, "manifest") != []:
            return False
        if not isinstance(entry, dict):
            return False
        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or not sha256:
            return False
    return any(
        isinstance(entry, dict)
        and entry.get("kind") == "events"
        and entry.get("path") == "events.jsonl"
        for entry in manifest
    )


def _attempt_sealed(attempt: Any) -> bool:
    """True only when the AttemptSummary is sealed (``sealed: true``).

    An outcome without ``sealed`` (missing, null, or any non-``True`` value)
    is never treated as final. Never raises.
    """
    return isinstance(attempt, dict) and attempt.get("sealed") is True


class SealedArchiveCache:
    """File-system cache of sealed Attempt Archives, bounded with LRU eviction."""

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise ValueError(
                f"sealed archive cache bound must be an integer number of bytes: "
                f"{max_bytes!r}"
            )
        if max_bytes < 1:
            raise ValueError(
                f"sealed archive cache bound must be >= 1 byte: {max_bytes}"
            )
        self._max_bytes = max_bytes
        self._directory = (
            Path(directory) if directory is not None else default_cache_dir()
        )
        self._ensure_dir(self._directory)

    @property
    def directory(self) -> Path:
        """Root directory of the cache."""
        return self._directory

    @property
    def max_bytes(self) -> int:
        """Total size bound in bytes."""
        return self._max_bytes

    # -- directories and files -------------------------------------------------

    @staticmethod
    def _ensure_dir(path: Path) -> None:
        """Create a cache directory with mode 0700 (and fix an existing one)."""
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    def _entry_dir(self, key: SealedArchiveKey) -> Path:
        return self._directory / key.entry_name()

    def _meta_path(self, key: SealedArchiveKey) -> Path:
        return self._entry_dir(key) / META_FILENAME

    def _blob_path(self, key: SealedArchiveKey, path: str) -> Path:
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
        return self._entry_dir(key) / f"blob-{digest}.bin"

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        """Write bytes atomically with mode 0600."""
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)

    def _read_meta(self, key: SealedArchiveKey) -> dict[str, Any] | None:
        """Parse one entry's ``meta.json`` (None when missing or corrupt)."""
        try:
            raw = self._meta_path(key).read_bytes()
        except OSError:
            return None
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return meta if isinstance(meta, dict) else None

    def _drop_entry(self, key: SealedArchiveKey) -> None:
        """Delete one entry directory, ignoring errors (a failed read drops it)."""
        shutil.rmtree(self._entry_dir(key), ignore_errors=True)

    def _drop_when_unreadable(self, key: SealedArchiveKey) -> bool:
        """Drop the entry when ``meta.json`` exists but does not parse.

        Returns True when the entry was dropped. A missing ``meta.json``
        reads as a clean miss and drops nothing.
        """
        if self._meta_path(key).exists() and self._read_meta(key) is None:
            self._drop_entry(key)
            return True
        return False

    def _touch(self, key: SealedArchiveKey) -> None:
        """Mark one entry as most recently used (LRU clock)."""
        try:
            os.utime(self._entry_dir(key))
        except OSError:
            pass

    # -- AttemptSummary plus manifest -------------------------------------------

    def store_show(
        self, key: SealedArchiveKey, attempt: dict[str, Any], manifest: list[Any]
    ) -> bool:
        """Store one ``attempts show`` result; True when it is now cached.

        Only archives with ``sealed: true`` are written; anything else is
        refused. A cached manifest is never updated: an entry that already
        exists keeps its first manifest. The stored copies are plain JSON
        data, so later reads re-parse through the same validators.
        """
        if not _attempt_sealed(attempt) or not _manifest_ok(manifest):
            return False
        entry_dir = self._entry_dir(key)
        if self._meta_path(key).exists():
            if self._drop_when_unreadable(key):
                entry_dir = self._entry_dir(key)
            else:
                return True
        self._ensure_dir(entry_dir)
        meta = {
            "version": CACHE_VERSION,
            "key": {
                "instance": key.instance,
                "backend": key.backend,
                "repository": key.repository,
                "attempt_id": key.attempt_id,
            },
            "attempt": attempt,
            "manifest": manifest,
            "artifacts": {},
        }
        try:
            payload = json.dumps(meta).encode("utf-8")
        except (ValueError, TypeError):
            self._drop_entry(key)
            return False
        self._write_file(self._meta_path(key), payload)
        self._evict()
        return True

    def load_show(self, key: SealedArchiveKey) -> tuple[dict[str, Any], list[Any]] | None:
        """Return one cached ``(AttemptSummary, manifest)``, verified.

        A missing, corrupt, foreign or unsealed entry (including an outcome
        without ``sealed``) drops the entry and reads as a miss. A hit marks
        the entry as most recently used.
        """
        meta = self._read_meta(key)
        if meta is None:
            self._drop_when_unreadable(key)
            return None
        if meta.get("version") != CACHE_VERSION:
            self._drop_entry(key)
            return None
        stored_key = meta.get("key")
        if (
            not isinstance(stored_key, dict)
            or stored_key.get("instance") != key.instance
            or stored_key.get("backend") != key.backend
            or stored_key.get("repository") != key.repository
            or stored_key.get("attempt_id") != key.attempt_id
        ):
            self._drop_entry(key)
            return None
        attempt = meta.get("attempt")
        manifest = meta.get("manifest")
        if not _attempt_sealed(attempt) or not _manifest_ok(manifest):
            self._drop_entry(key)
            return None
        assert isinstance(attempt, dict) and isinstance(manifest, list)
        self._touch(key)
        return attempt, manifest

    # -- artifacts ----------------------------------------------------------------

    def store_artifact(self, key: SealedArchiveKey, path: str, data: bytes) -> bool:
        """Store one verified artifact; True when it is now cached.

        The bytes are verified (size and ``sha256``) against the cached
        manifest entry before anything is written; a mismatch, an unknown
        path, a missing entry or an unsealed summary refuses the write.
        """
        if not isinstance(path, str) or not path or not isinstance(data, bytes):
            return False
        meta = self._read_meta(key)
        if meta is None:
            self._drop_when_unreadable(key)
            return False
        if meta.get("version") != CACHE_VERSION:
            self._drop_entry(key)
            return False
        if not _attempt_sealed(meta.get("attempt")) or not _manifest_ok(
            meta.get("manifest")
        ):
            self._drop_entry(key)
            return False
        manifest = meta["manifest"]
        assert isinstance(manifest, list)
        entry = next(
            (
                item
                for item in manifest
                if isinstance(item, dict) and item.get("path") == path
            ),
            None,
        )
        if entry is None:
            return False
        if len(data) != entry.get("size"):
            return False
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            return False
        self._ensure_dir(self._entry_dir(key))
        self._write_file(self._blob_path(key, path), data)
        artifacts = meta.get("artifacts")
        if not isinstance(artifacts, dict):
            artifacts = {}
        artifacts[path] = {"size": entry["size"], "sha256": entry["sha256"]}
        meta["artifacts"] = artifacts
        try:
            payload = json.dumps(meta).encode("utf-8")
        except (ValueError, TypeError):
            return False
        self._write_file(self._meta_path(key), payload)
        self._touch(key)
        self._evict()
        return True

    def load_artifact(self, key: SealedArchiveKey, path: str) -> bytes | None:
        """Return one cached artifact, verified against its manifest entry.

        A failed read verification drops the whole entry and reads as a
        miss, so the next request fetches again. A hit marks the entry as
        most recently used.
        """
        meta = self._read_meta(key)
        if meta is None:
            self._drop_when_unreadable(key)
            return None
        if meta.get("version") != CACHE_VERSION:
            self._drop_entry(key)
            return None
        if not _attempt_sealed(meta.get("attempt")) or not _manifest_ok(
            meta.get("manifest")
        ):
            self._drop_entry(key)
            return None
        manifest = meta["manifest"]
        assert isinstance(manifest, list)
        entry = next(
            (
                item
                for item in manifest
                if isinstance(item, dict) and item.get("path") == path
            ),
            None,
        )
        if entry is None:
            return None
        try:
            data = self._blob_path(key, path).read_bytes()
        except OSError:
            return None
        if len(data) != entry.get("size") or (
            hashlib.sha256(data).hexdigest() != entry.get("sha256")
        ):
            self._drop_entry(key)
            return None
        self._touch(key)
        return data

    # -- bound and eviction ----------------------------------------------------------

    def total_bytes(self) -> int:
        """Current total size of the cache in bytes."""
        total = 0
        try:
            entries = list(self._directory.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    continue
            elif entry.is_dir():
                for root, _, files in os.walk(entry):
                    for name in files:
                        try:
                            total += os.path.getsize(os.path.join(root, name))
                        except OSError:
                            continue
        return total

    def _evict(self) -> None:
        """Delete least-recently-used entries until the total fits the bound."""
        if self.total_bytes() <= self._max_bytes:
            return
        try:
            entries = [item for item in self._directory.iterdir() if item.is_dir()]
        except OSError:
            return

        def _mtime_ns(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        for entry in sorted(entries, key=_mtime_ns):
            shutil.rmtree(entry, ignore_errors=True)
            if self.total_bytes() <= self._max_bytes:
                return

    def clear(self) -> None:
        """Delete every entry (tests only)."""
        try:
            entries = list(self._directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
