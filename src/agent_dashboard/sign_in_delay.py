"""Growing sign-in delay (spec 5.5): per-account and per-address throttling.

In memory, reset by a restart. Two independent maps:

- by account name as typed (stripped and lowercased; names over 64
  characters share one key, ``<long>``), whether or not the account exists;
- by client address (spec 4.4), IPv6 addresses by their ``/64``.

After the n-th consecutive failure for a key the delay is
``min(2 ** (n - 1), 60)`` seconds: 1, 2, 4, 8, 16, 32, 60, 60, ...
An attempt is refused unchecked while either of its keys is inside its
delay; the longer remaining wait is the one shown. A key is forgotten
15 minutes after its last failure. Each map holds at most 10 000 keys and
drops the least recently failed first. A success clears both of its keys.
Accounts are never locked. The server never sleeps on a request.
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections import OrderedDict
from dataclasses import dataclass

#: A key is forgotten this many seconds after its last failure (spec 5.5).
FORGET_AFTER_S = 15 * 60

#: Each map holds at most this many keys (spec 5.5).
MAX_KEYS = 10_000

#: The delay caps here (spec 5.5).
MAX_DELAY_S = 60

#: Shared key for account names over 64 characters (spec 5.5).
LONG_NAME_KEY = "<long>"


def delay_for_failures(count: int) -> int:
    """The delay after the n-th consecutive failure: 1, 2, 4, ..., 60, 60."""
    if count <= 0:
        return 0
    return min(2 ** (count - 1), MAX_DELAY_S)


def account_key(name: str) -> str:
    """The account map key for a typed account name (spec 5.5).

    Stripped and lowercased; names over 64 characters share ``<long>``.
    """
    cleaned = name.strip().lower()
    if len(cleaned) > 64:
        return LONG_NAME_KEY
    return cleaned


def client_key(client: str) -> str:
    """The address map key for a resolved client address (spec 5.5).

    IPv6 addresses share their ``/64``; everything else is its own key.
    Non-IP values (such as the test client's default peer) are kept as-is.
    """
    text = client.strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text
    if isinstance(address, ipaddress.IPv6Address):
        network = ipaddress.ip_network(f"{text}/64", strict=False)
        return str(network.network_address)
    return str(address)


@dataclass
class _Entry:
    failures: int = 0
    last_failed: float = 0.0


class SignInDelay:
    """The two in-memory failure maps with an injectable clock (spec 5.5)."""

    def __init__(self, clock=None) -> None:
        self._clock = clock if clock is not None else time.time
        self._accounts: OrderedDict[str, _Entry] = OrderedDict()
        self._clients: OrderedDict[str, _Entry] = OrderedDict()

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    @staticmethod
    def _is_expired(entry: _Entry, now: float) -> bool:
        return now - entry.last_failed >= FORGET_AFTER_S

    @classmethod
    def _prune(cls, entries: OrderedDict[str, _Entry], now: float) -> None:
        expired = [
            key for key, entry in entries.items() if cls._is_expired(entry, now)
        ]
        for key in expired:
            del entries[key]

    @classmethod
    def _remaining(cls, entry: _Entry | None, now: float) -> float:
        if entry is None or cls._is_expired(entry, now):
            return 0.0
        return entry.last_failed + delay_for_failures(entry.failures) - now

    def wait(
        self,
        account: str,
        client: str,
        *,
        now: float | None = None,
    ) -> float:
        """Remaining delay in seconds for either key, or 0 when allowed."""
        moment = self._now(now)
        self._prune(self._accounts, moment)
        self._prune(self._clients, moment)
        remaining = max(
            self._remaining(self._accounts.get(account), moment),
            self._remaining(self._clients.get(client), moment),
        )
        return remaining if remaining > 0 else 0.0

    def wait_s(
        self,
        account: str,
        client: str,
        *,
        now: float | None = None,
    ) -> int:
        """Remaining delay rounded up to whole seconds (0 when allowed)."""
        remaining = self.wait(account, client, now=now)
        return int(math.ceil(remaining)) if remaining > 0 else 0

    def record_failure(
        self,
        account: str,
        client: str,
        *,
        now: float | None = None,
    ) -> int:
        """Count one failure against both keys; return the new longer delay."""
        moment = self._now(now)
        self._prune(self._accounts, moment)
        self._prune(self._clients, moment)
        account_delay = self._bump(self._accounts, account, moment)
        client_delay = self._bump(self._clients, client, moment)
        return max(account_delay, client_delay)

    @classmethod
    def _bump(cls, entries: OrderedDict[str, _Entry], key: str, now: float) -> int:
        entry = entries.get(key)
        if entry is None or cls._is_expired(entry, now):
            entry = _Entry()
            entries[key] = entry
        entry.failures += 1
        entry.last_failed = now
        entries.move_to_end(key)
        while len(entries) > MAX_KEYS:
            entries.popitem(last=False)
        return delay_for_failures(entry.failures)

    def record_success(
        self,
        account: str,
        client: str,
        *,
        now: float | None = None,
    ) -> None:
        """Clear both keys after a successful sign-in (spec 5.5)."""
        moment = self._now(now)
        self._prune(self._accounts, moment)
        self._prune(self._clients, moment)
        self._accounts.pop(account, None)
        self._clients.pop(client, None)


__all__ = [
    "FORGET_AFTER_S",
    "LONG_NAME_KEY",
    "MAX_DELAY_S",
    "MAX_KEYS",
    "SignInDelay",
    "account_key",
    "client_key",
    "delay_for_failures",
]
