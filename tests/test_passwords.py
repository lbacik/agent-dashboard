"""Password helper: NFKC normalisation, length limits, argon2 (spec 6.5)."""

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from agent_dashboard.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    needs_rehash,
    normalize_password,
    validate_password,
    verify_password,
)


class CheapHasher:
    """Injectable stand-in for argon2's PasswordHasher (spec 12)."""

    def hash(self, password: str) -> str:
        return f"cheap${password}"

    def verify(self, hash: str, password: str) -> bool:
        if hash != f"cheap${password}":
            raise VerifyMismatchError("mismatch")
        return True

    def check_needs_rehash(self, hash: str) -> bool:
        return not hash.startswith("cheap$")


@pytest.fixture()
def cheap():
    return CheapHasher()


def test_limits_are_15_and_128():
    assert MIN_PASSWORD_LENGTH == 15
    assert MAX_PASSWORD_LENGTH == 128


def test_nfkc_normalisation_applied():
    # U+FB01 ligature expands to two code points under NFKC.
    assert normalize_password("ﬁsh") == "fish"


def test_length_counted_after_nfkc():
    # 13 + decomposed e-acute (2 code points) is 15 raw, 14 normalised.
    raw = "a" * 13 + "é"
    assert len(raw) == 15
    with pytest.raises(ValueError, match="at least 15"):
        validate_password(raw)


def test_ligature_expansion_counts_extra_code_points(cheap):
    # 14 raw code points normalise to 15, so the password is accepted.
    raw = "a" * 14 + "ﬁ"
    assert len(raw) == 15
    assert len(normalize_password(raw)) == 16
    assert hash_password(raw, hasher=cheap) == f"cheap${'a' * 14}fi"


@pytest.mark.parametrize("length", [15, 16, 100, 128])
def test_accepted_lengths(length, cheap):
    password = "p" * length
    assert validate_password(password) == password
    assert verify_password(hash_password(password, hasher=cheap), password, hasher=cheap)


@pytest.mark.parametrize("length", [0, 1, 14])
def test_too_short(length):
    with pytest.raises(ValueError, match="at least 15"):
        validate_password("p" * length)


@pytest.mark.parametrize("length", [129, 200])
def test_too_long(length):
    with pytest.raises(ValueError, match="at most 128"):
        validate_password("p" * length)


def test_hash_and_verify_round_trip(cheap):
    hashed = hash_password("a correct horse phrase here", hasher=cheap)
    assert verify_password(hashed, "a correct horse phrase here", hasher=cheap) is True
    assert verify_password(hashed, "a correct horse phrase!!!!", hasher=cheap) is False


def test_verify_normalises_before_checking(cheap):
    hashed = hash_password("ﬁsh and fifteen chars", hasher=cheap)
    assert verify_password(hashed, "ﬁsh and fifteen chars", hasher=cheap) is True


def test_verify_malformed_hash_is_false(cheap):
    assert verify_password("not-a-hash", "a correct horse phrase", hasher=cheap) is False


def test_needs_rehash_reports_stub_params(cheap):
    assert needs_rehash("cheap$anything long enough here", hasher=cheap) is False
    assert needs_rehash("argon2$old-params", hasher=cheap) is True


def test_real_argon2_round_trip():
    # One real hash proves the default hasher works; the rest use the stub.
    hasher = PasswordHasher()
    hashed = hash_password("a correct horse phrase here", hasher=hasher)
    assert hashed.startswith("$argon2")
    assert verify_password(hashed, "a correct horse phrase here", hasher=hasher) is True
    assert verify_password(hashed, "a wrong horse phrase!!!!", hasher=hasher) is False
    assert needs_rehash(hashed, hasher=hasher) is False
