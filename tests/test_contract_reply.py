"""Unit tests for the contract-path helpers beyond the conformance suite."""

import pytest

from agent_dashboard.contract_reply import (
    AMBIGUOUS,
    CONTRACT_MISMATCH,
    NOT_FOUND,
    REJECTION,
    REPLAY,
    SUCCESS,
    UNSUPPORTED,
    classify_contract_reply,
    new_contract_request_id,
    required_capability,
    validate_contract_request_id,
)


def test_ok_true_is_success():
    assert (
        classify_contract_reply(
            0, '{"contract": "1.0", "ok": true, "result": {"replayed": false}}'
        )
        == SUCCESS
    )


def test_ok_true_replayed_is_replay():
    assert (
        classify_contract_reply(
            0, '{"contract": "1.0", "ok": true, "result": {"replayed": true}}'
        )
        == REPLAY
    )


def test_ok_true_with_nonzero_exit_is_ambiguous():
    assert (
        classify_contract_reply(
            1, '{"contract": "1.0", "ok": true, "result": {}}'
        )
        == AMBIGUOUS
    )


def test_error_codes_map_to_classes():
    cases = [
        ("rejected", 1, REJECTION),
        ("repository_mismatch", 1, REJECTION),
        ("payload_mismatch", 1, REJECTION),
        ("invalid_argument", 1, REJECTION),
        ("usage", 2, REJECTION),
        ("unsupported", 3, UNSUPPORTED),
        ("contract_mismatch", 4, CONTRACT_MISMATCH),
        ("not_found", 5, NOT_FOUND),
        ("agent_unreachable", 6, AMBIGUOUS),
        ("internal", 7, AMBIGUOUS),
    ]
    from agent_operator_contract import documents

    table = documents.error_table()
    for code, exit_code, expected in cases:
        retryable = "true" if table[code]["retryable"] else "false"
        doc = (
            f'{{"contract": "1.0", "ok": false, "error": '
            f'{{"code": "{code}", "message": "m", "retryable": {retryable}}}}}'
        )
        if code == "contract_mismatch":
            doc = (
                '{"contract": "1.0", "ok": false, "error": {"code": '
                '"contract_mismatch", "message": "m", "retryable": false, '
                '"details": {"supported": [1]}}}'
            )
        assert classify_contract_reply(exit_code, doc) == expected, code


def test_internal_stored_false_is_rejection():
    doc = (
        '{"contract": "1.0", "ok": false, "error": {"code": "internal", '
        '"message": "m", "retryable": true, "details": {"stored": false}}}'
    )
    assert classify_contract_reply(7, doc) == REJECTION


def test_exit_code_contradiction_is_ambiguous():
    doc = (
        '{"contract": "1.0", "ok": false, "error": {"code": "rejected", '
        '"message": "m", "retryable": false}}'
    )
    assert classify_contract_reply(0, doc) == AMBIGUOUS
    assert classify_contract_reply(2, doc) == AMBIGUOUS


def test_transport_failures_without_envelope_are_ambiguous():
    for exit_code in (125, 126, 127):
        assert classify_contract_reply(exit_code, "") == AMBIGUOUS
        assert classify_contract_reply(exit_code, "docker: daemon error") == AMBIGUOUS


def test_unknown_contract_major_is_ambiguous():
    doc = '{"contract": "2.0", "ok": true, "result": {}}'
    assert classify_contract_reply(0, doc) == AMBIGUOUS


def test_new_request_id_fits_the_contract_pattern():
    assert validate_contract_request_id(new_contract_request_id()) is not None
    assert new_contract_request_id() != new_contract_request_id()


def test_validate_contract_request_id():
    assert validate_contract_request_id("req-3f1c9a2b7d10") == "req-3f1c9a2b7d10"
    for bad in ("", "req with space", "x" * 200, "-lead", 0, None, b"req-abc"):
        with pytest.raises(ValueError):
            validate_contract_request_id(bad)


def test_required_capability_mapping():
    assert required_capability("status", {}) is None
    assert required_capability("stop", {}) is None
    assert required_capability("stop", {"after": "3"}) == "stop-after"
    assert required_capability("handoff", {}) == "operator-handoff"
    assert required_capability("next-issue", {}) == "next-issue"
    assert required_capability("no-such-op", {}) is None
    assert required_capability(None, {}) is None
