"""Consumer conformance: the classifier against the pinned contract suite.

Loads the consumer suite (the fixture classification index plus every
``{argv, exit_code, stdout, stderr}`` fixture) from the installed
agent-operator-contract package data — fixtures are never copied into this
repository — and checks the contract-path entry point
(:func:`agent_dashboard.contract_reply.classify_contract_reply`) against it.

The suite records eight classifications; the entry point folds the two
"unknown outcome" ones (``ambiguous_outcome`` and ``malformed_stdout``) into
``ambiguous``, since both mean the same thing to a consumer: retrying with
the same request ID is safe. Every other class is returned verbatim.
"""

from agent_dashboard import contract_reply
from agent_dashboard.contract_reply import (
    AMBIGUOUS,
    CONTRACT_CLASSES,
    classify_contract_reply,
)

AMBIGUOUS_IN_INDEX = frozenset({"ambiguous_outcome", "malformed_stdout"})


def _index():
    from agent_operator_contract import documents

    return documents.load_fixture_index()


def _expected(index_class: str) -> str:
    if index_class in AMBIGUOUS_IN_INDEX:
        return AMBIGUOUS
    return index_class


def test_suite_index_lists_every_packaged_fixture():
    from agent_operator_contract import documents

    index = _index()
    assert set(index["fixtures"]) == set(documents.fixture_names())


def test_suite_index_uses_only_known_classifications():
    from agent_operator_contract import documents

    known = set(documents.CLASSIFICATIONS)
    for name, entry in _index()["fixtures"].items():
        assert entry["classification"] in known, name


def test_fixture_shapes_are_valid_consumer_recordings():
    from agent_operator_contract import documents

    for name in documents.fixture_names():
        fixture = documents.load_fixture(name)
        assert documents.validate_fixture(fixture) == [], name


def test_library_reference_agrees_with_the_index():
    """The library's own reader must see what the index records.

    This pins the test to the suite's meaning: if the reference and the
    index ever disagree, the test fails here rather than silently testing
    the wrong expectation.
    """
    from agent_operator_contract import documents

    for name in documents.fixture_names():
        fixture = documents.load_fixture(name)
        assert documents.classify_fixture(fixture) == (
            _index()["fixtures"][name]["classification"]
        ), name


def test_classifier_matches_the_consumer_suite():
    from agent_operator_contract import documents

    failures = []
    for name in documents.fixture_names():
        fixture = documents.load_fixture(name)
        expected = _expected(_index()["fixtures"][name]["classification"])
        assert expected in CONTRACT_CLASSES, name
        seen = classify_contract_reply(fixture["exit_code"], fixture["stdout"])
        if seen != expected:
            failures.append(f"{name}: index expects {expected}, got {seen}")
    assert not failures, "\n".join(failures)


def test_classifier_never_raises():
    garbage = [
        (None, None),
        (True, ""),
        ("0", ""),
        (0, None),
        (0, b"\xff\xfe not json"),
        (0, ""),
        (0, "null"),
        (0, "[]"),
        (0, "{}"),
        (0, '{"ok": true}'),
        (1, '{"contract": "9.9", "ok": false}'),
        (125, ""),
        (126, "not json"),
        (127, b"\x00"),
        (-1, '{"ok": false}'),
        (256, '{"ok": false}'),
        (float("nan"), "{}"),
    ]
    for exit_code, stdout in garbage:
        assert classify_contract_reply(exit_code, stdout) in CONTRACT_CLASSES


def test_classifier_is_pure_and_deterministic():
    from agent_operator_contract import documents

    fixture = documents.load_fixture("stop-accepted")
    first = classify_contract_reply(fixture["exit_code"], fixture["stdout"])
    assert first == contract_reply.SUCCESS
    assert classify_contract_reply(fixture["exit_code"], fixture["stdout"]) == first
    assert classify_contract_reply(
        fixture["exit_code"], fixture["stdout"].encode("utf-8")
    ) == first
