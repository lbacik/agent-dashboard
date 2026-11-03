"""Operator password helper (spec section 6.5).

Passwords are NFKC-normalised, then must be 15 to 128 code points. No
composition rules, no breached-password list (ADR 0013). Hashing uses
argon2-cffi's ``PasswordHasher`` with its defaults (argon2id, the RFC 9106
low-memory profile). Tests inject a cheap hasher through the ``hasher``
keyword instead of paying for argon2.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

MIN_PASSWORD_LENGTH = 15
MAX_PASSWORD_LENGTH = 128


class PasswordHasherLike(Protocol):
    """The part of argon2-cffi's ``PasswordHasher`` this module uses."""

    def hash(self, password: str) -> str: ...
    def verify(self, hash: str, password: str) -> bool: ...
    def check_needs_rehash(self, hash: str) -> bool: ...


def default_hasher() -> PasswordHasher:
    """An argon2-cffi ``PasswordHasher`` with its defaults (argon2id)."""
    return PasswordHasher()


def normalize_password(password: str) -> str:
    """NFKC-normalise a password; length limits count this form's code points."""
    return unicodedata.normalize("NFKC", password)


def validate_password(password: str) -> str:
    """Normalise and length-check a password, returning the normalised form.

    Raises ``ValueError`` with the set-password page's copy (spec 8.3) when
    the normalised form is shorter than 15 or longer than 128 code points.
    """
    normalised = normalize_password(password)
    if len(normalised) < MIN_PASSWORD_LENGTH:
        raise ValueError("Use at least 15 characters.")
    if len(normalised) > MAX_PASSWORD_LENGTH:
        raise ValueError("Use at most 128 characters.")
    return normalised


def hash_password(password: str, *, hasher: Any = None) -> str:
    """Normalise, length-check and hash a password (PHC string)."""
    normalised = validate_password(password)
    return (hasher or default_hasher()).hash(normalised)


def verify_password(
    password_hash: str, password: str, *, hasher: Any = None
) -> bool:
    """True when the NFKC-normalised password matches the hash.

    An unknown name, a removed account or an over-long probe is checked
    against a dummy hash through this same function (spec 5.3), so it never
    raises for a mismatch or a malformed hash: both read as False.
    """
    checker = hasher or default_hasher()
    try:
        return bool(checker.verify(password_hash, normalize_password(password)))
    except (VerifyMismatchError, InvalidHash):
        return False


def needs_rehash(password_hash: str, *, hasher: Any = None) -> bool:
    """True when the hash's parameters differ from the hasher's (spec 5.3)."""
    return bool((hasher or default_hasher()).check_needs_rehash(password_hash))


__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "PasswordHasherLike",
    "default_hasher",
    "hash_password",
    "needs_rehash",
    "normalize_password",
    "validate_password",
    "verify_password",
]
