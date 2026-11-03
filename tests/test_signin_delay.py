"""Growing sign-in delay and client-address resolution (spec 4.4, 5.3, 5.5, 9).

Web tests use ``TestClient(client=(addr, port))`` for the peer address and
explicit ``X-Forwarded-For`` headers. A clock is injected through
``create_app(clock=...)`` so the delay sequence, expiry and forgetting are
deterministic.
"""

from __future__ import annotations

import asyncio
import ipaddress

from fastapi.testclient import TestClient
from test_passwords import CheapHasher
from test_signin import (
    PASSWORD,
    Clock,
    add_account,
    auth_lines,
    cookie_header,
    make_lan,
    post_login,
    set_cookies,
)

from agent_dashboard.sign_in_delay import (
    LONG_NAME_KEY,
    MAX_KEYS,
    SignInDelay,
    account_key,
    client_key,
    delay_for_failures,
)


def _nets(*values):
    return tuple(ipaddress.ip_network(v, strict=False) for v in values)


def failed_login(env, **kwargs):
    """One failed sign-in from the suite's default peer (period-correct)."""
    return post_login(env.client, password="wrong password here!!", **kwargs)


def peer_client(env, addr, port=5000, base_url="http://localhost"):
    """A client with an explicit peer address (spec 12)."""
    return TestClient(env.app, base_url=base_url, client=(addr, port))


# --- delay sequence (spec 5.5, unit level with an injected clock) ----------------


def test_delay_sequence_doubles_then_caps():
    assert [delay_for_failures(n) for n in range(1, 9)] == [1, 2, 4, 8, 16, 32, 60, 60]


def test_delay_for_zero_failures_is_zero():
    assert delay_for_failures(0) == 0


def test_tracker_sequence_with_injected_clock():
    clock = Clock()
    tracker = SignInDelay(clock=clock)
    expected = [1, 2, 4, 8, 16, 32, 60, 60]
    for want in expected:
        assert tracker.wait("alice", "1.2.3.4") == 0
        got = tracker.record_failure("alice", "1.2.3.4")
        assert got == want
        # Inside the new delay the attempt is refused.
        assert tracker.wait("alice", "1.2.3.4") == want
        clock.now += want + 0.5


def test_success_clears_both_keys():
    clock = Clock()
    tracker = SignInDelay(clock=clock)
    tracker.record_failure("alice", "1.2.3.4")
    assert tracker.wait("alice", "1.2.3.4") > 0
    tracker.record_success("alice", "1.2.3.4")
    assert tracker.wait("alice", "1.2.3.4") == 0
    # The next failure starts over at 1 s.
    assert tracker.record_failure("alice", "1.2.3.4") == 1


def test_success_clears_only_its_own_keys():
    tracker = SignInDelay(clock=Clock())
    tracker.record_failure("alice", "1.2.3.4")
    tracker.record_failure("bob", "5.6.7.8")
    tracker.record_success("alice", "1.2.3.4")
    assert tracker.wait("alice", "1.2.3.4") == 0
    assert tracker.wait("bob", "5.6.7.8") > 0


def test_key_forgotten_15_minutes_after_last_failure():
    clock = Clock()
    tracker = SignInDelay(clock=clock)
    tracker.record_failure("alice", "1.2.3.4")
    tracker.record_failure("alice", "1.2.3.4")
    assert tracker.wait("alice", "1.2.3.4") > 0
    clock.now += 15 * 60
    assert tracker.wait("alice", "1.2.3.4") == 0
    # Forgotten keys start over at 1 s.
    assert tracker.record_failure("alice", "1.2.3.4") == 1


def test_forgetting_follows_last_failure_not_first():
    clock = Clock()
    tracker = SignInDelay(clock=clock)
    tracker.record_failure("alice", "1.2.3.4")
    clock.now += 14 * 60
    assert tracker.record_failure("alice", "1.2.3.4") == 2
    clock.now += 14 * 60
    # 14 minutes after the second failure the key is remembered: the next
    # failure is the third in a row (delay 4), not a fresh start.
    assert tracker.wait("alice", "1.2.3.4") == 0
    assert tracker.record_failure("alice", "1.2.3.4") == 4
    clock.now += 15 * 60
    assert tracker.wait("alice", "1.2.3.4") == 0
    assert tracker.record_failure("alice", "1.2.3.4") == 1


def test_cap_drops_least_recently_failed_first(monkeypatch):
    monkeypatch.setattr("agent_dashboard.sign_in_delay.MAX_KEYS", 3)
    clock = Clock()
    tracker = SignInDelay(clock=clock)
    for name in ("a", "b", "c"):
        tracker.record_failure(name, f"client-{name}")
    # "a" failed least recently; the next key drops it.
    tracker.record_failure("d", "client-d")
    assert tracker.wait("a", "fresh-client") == 0
    assert tracker.wait("b", "fresh-client") > 0
    assert tracker.wait("c", "fresh-client") > 0
    assert tracker.wait("d", "fresh-client") > 0


def test_cap_applies_per_map(monkeypatch):
    monkeypatch.setattr("agent_dashboard.sign_in_delay.MAX_KEYS", 2)
    clock = Clock()
    tracker = SignInDelay(clock=clock)
    tracker.record_failure("a", "1.1.1.1")
    tracker.record_failure("b", "2.2.2.2")
    tracker.record_failure("c", "3.3.3.3")
    assert tracker.wait("a", "fresh-client") == 0
    assert tracker.wait("b", "fresh-client") > 0
    # The client map drops its least recently failed key too.
    assert tracker.wait("fresh-account", "1.1.1.1") == 0
    assert tracker.wait("fresh-account", "2.2.2.2") > 0


# --- key rules (spec 5.5) --------------------------------------------------------


def test_account_key_strips_and_lowercases():
    assert account_key("  ALICE  ") == "alice"


def test_account_names_over_64_chars_share_one_key():
    assert account_key("a" * 64) == "a" * 64
    assert account_key("a" * 65) == LONG_NAME_KEY
    assert account_key("b" * 100) == LONG_NAME_KEY
    assert account_key("  " + "C" * 70 + "  ") == LONG_NAME_KEY


def test_client_key_v4_is_itself():
    assert client_key("192.0.2.10") == "192.0.2.10"


def test_client_key_v6_shares_its_64():
    first = client_key("2001:db8:1:2::1")
    second = client_key("2001:db8:1:2::2")
    other = client_key("2001:db8:1:3::1")
    assert first == second
    assert first != other


def test_client_key_non_ip_kept_as_is():
    assert client_key("testclient") == "testclient"
    assert client_key("  testclient  ") == "testclient"


# --- POST /login delay (spec 5.3 step 3) ------------------------------------------


def test_failed_check_carries_growing_delay(tmp_path, monkeypatch, capsys):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=env.clock.now)
    for want in (1, 2, 4, 8, 16, 32, 60, 60):
        response = post_login(env.client, password="wrong password here!!")
        assert response.status_code == 401
        (line,) = auth_lines(capsys)
        assert line["event"] == "sign_in_failed"
        assert line["delay_s"] == want
        if want > 1:
            assert f"Wait {want} seconds before trying again." in response.text
        else:
            assert "Wait " not in response.text
        env.clock.now += want + 0.5


def test_attempt_inside_delay_gets_429_and_skips_password_check(
    tmp_path, monkeypatch, capsys
):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=env.clock.now)
    first = post_login(env.client, password="wrong password here!!")
    assert first.status_code == 401
    capsys.readouterr()
    # The correct password is still refused while the 1 s delay runs.
    response = post_login(env.client, account="alice", password=PASSWORD)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert 'data-tone="warn"' in response.text
    assert (
        "Too many failed sign-ins. Wait 1 seconds, then try again. "
        "That attempt was not checked." in response.text
    )
    (line,) = auth_lines(capsys)
    assert line["event"] == "sign_in_delayed"
    assert line["wait_s"] == 1
    assert line["account"] == "alice"
    assert line["client"] == "testclient"
    # After the delay the same correct password succeeds.
    env.clock.now += 1.5
    assert post_login(env.client, password=PASSWORD).status_code == 303


def test_longer_wait_is_shown(tmp_path, monkeypatch, capsys):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=env.clock.now)
    add_account("bob", now=env.clock.now)
    # Grow bob's address bucket by failing twice with a gap past the 1 s delay.
    assert failed_login(env, account="bob").status_code == 401
    env.clock.now += 1.5
    assert failed_login(env, account="bob").status_code == 401
    capsys.readouterr()
    # alice has no account failures, but the shared address bucket waits 2 s.
    response = post_login(env.client, account="alice", password=PASSWORD)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"
    assert "Wait 2 seconds" in response.text


def test_address_key_delays_other_accounts(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=env.clock.now)
    add_account("bob", now=env.clock.now)
    assert failed_login(env, account="alice").status_code == 401
    # bob never failed, but the shared client address is inside its delay.
    response = failed_login(env, account="bob")
    assert response.status_code == 429


def test_account_key_delays_other_addresses(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=env.clock.now)
    peer_a = peer_client(env, "192.0.2.10")
    peer_b = peer_client(env, "192.0.2.20")
    response = peer_a.post(
        "/login",
        data={"account": "alice", "password": "wrong password here!!"},
        headers={"Origin": "http://localhost"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    delayed = peer_b.post(
        "/login",
        data={"account": "alice", "password": PASSWORD},
        headers={"Origin": "http://localhost"},
        follow_redirects=False,
    )
    assert delayed.status_code == 429


def test_success_resets_delay(tmp_path, monkeypatch, capsys):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=env.clock.now)
    assert failed_login(env).status_code == 401
    env.clock.now += 1.5
    assert failed_login(env).status_code == 401
    env.clock.now += 2.5
    assert post_login(env.client, password=PASSWORD).status_code == 303
    capsys.readouterr()
    response = post_login(env.client, password="wrong password here!!")
    assert response.status_code == 401
    (line,) = auth_lines(capsys)
    assert line["delay_s"] == 1


def test_long_account_names_share_one_key(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=env.clock.now)
    first = post_login(env.client, account="x" * 65,
                       password="wrong password here!!")
    assert first.status_code == 401
    # A different name over 64 characters shares the <long> bucket.
    second = post_login(env.client, account="y" * 70,
                        password="wrong password here!!")
    assert second.status_code == 429


def test_ipv6_clients_in_same_64_share_one_key(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=env.clock.now)
    add_account("bob", now=env.clock.now)
    first = peer_client(env, "2001:db8:1:2::1")
    second = peer_client(env, "2001:db8:1:2::9")
    other = peer_client(env, "2001:db8:1:3::1")
    bad = {"Origin": "http://localhost"}
    assert first.post(
        "/login",
        data={"account": "alice", "password": "wrong password here!!"},
        headers=bad,
        follow_redirects=False,
    ).status_code == 401
    # bob never failed, but the shared /64 bucket is inside its delay.
    delayed = second.post(
        "/login",
        data={"account": "bob", "password": PASSWORD},
        headers=bad,
        follow_redirects=False,
    )
    assert delayed.status_code == 429
    allowed = other.post(
        "/login",
        data={"account": "bob", "password": PASSWORD},
        headers=bad,
        follow_redirects=False,
    )
    assert allowed.status_code == 303


def test_no_request_handler_sleeps(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=env.clock.now)
    assert failed_login(env).status_code == 401

    async def no_sleep(*args, **kwargs):
        raise AssertionError("the delay must answer at once, never sleep")

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr("time.sleep", no_sleep)
    response = post_login(env.client, password=PASSWORD)
    assert response.status_code == 429


# --- client address via TestClient (spec 4.4) -------------------------------------


def failed_with_peer(env, peer, xff, trusted, capsys, account="alice"):
    client = peer_client(env, peer)
    headers = {"Origin": "http://localhost"}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    response = client.post(
        "/login",
        data={"account": account, "password": "wrong password here!!"},
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 401
    (line,) = auth_lines(capsys)
    return line["client"]


def make_proxied(tmp_path, monkeypatch, *trusted):
    return make_lan(tmp_path, monkeypatch, trusted_proxies=_nets(*trusted))


def test_peer_untrusted_ignores_xff(tmp_path, monkeypatch, capsys):
    env = make_proxied(tmp_path, monkeypatch, "10.0.0.0/8")
    add_account("alice", now=env.clock.now)
    assert failed_with_peer(
        env, "192.0.2.10", "10.0.0.5", env.settings.trusted_proxies, capsys
    ) == "192.0.2.10"


def test_missing_xff_uses_peer(tmp_path, monkeypatch, capsys):
    env = make_proxied(tmp_path, monkeypatch, "10.0.0.0/8")
    add_account("alice", now=env.clock.now)
    assert failed_with_peer(
        env, "10.0.0.5", None, env.settings.trusted_proxies, capsys
    ) == "10.0.0.5"


def test_takes_first_untrusted_from_right(tmp_path, monkeypatch, capsys):
    env = make_proxied(tmp_path, monkeypatch, "10.0.0.0/8")
    add_account("alice", now=env.clock.now)
    assert failed_with_peer(
        env, "10.0.0.5", "203.0.113.7, 10.0.0.9, 10.0.0.8",
        env.settings.trusted_proxies, capsys,
    ) == "203.0.113.7"


def test_skips_trusted_tail(tmp_path, monkeypatch, capsys):
    env = make_proxied(tmp_path, monkeypatch, "10.0.0.0/8")
    add_account("alice", now=env.clock.now)
    assert failed_with_peer(
        env, "10.0.0.1", "203.0.113.7, 10.0.0.2",
        env.settings.trusted_proxies, capsys,
    ) == "203.0.113.7"


def test_all_trusted_takes_leftmost(tmp_path, monkeypatch, capsys):
    env = make_proxied(tmp_path, monkeypatch, "10.0.0.0/8")
    add_account("alice", now=env.clock.now)
    assert failed_with_peer(
        env, "10.0.0.1", "10.0.0.3, 10.0.0.2",
        env.settings.trusted_proxies, capsys,
    ) == "10.0.0.3"


def test_non_ip_entry_ends_walk_with_peer(tmp_path, monkeypatch, capsys):
    env = make_proxied(tmp_path, monkeypatch, "10.0.0.0/8")
    add_account("alice", now=env.clock.now)
    assert failed_with_peer(
        env, "10.0.0.1", "203.0.113.7, nonsense",
        env.settings.trusted_proxies, capsys,
    ) == "10.0.0.1"


def test_blank_entry_ends_walk_with_peer(tmp_path, monkeypatch, capsys):
    env = make_proxied(tmp_path, monkeypatch, "10.0.0.0/8")
    add_account("alice", now=env.clock.now)
    assert failed_with_peer(
        env, "10.0.0.1", "203.0.113.7, ",
        env.settings.trusted_proxies, capsys,
    ) == "10.0.0.1"


def test_ipv6_resolution_through_proxy(tmp_path, monkeypatch, capsys):
    env = make_proxied(tmp_path, monkeypatch, "fd00::/8")
    add_account("alice", now=env.clock.now)
    assert failed_with_peer(
        env, "fd00::1", "2001:db8::7, fd00::2",
        env.settings.trusted_proxies, capsys,
    ) == "2001:db8::7"


def test_delayed_line_uses_resolved_client(tmp_path, monkeypatch, capsys):
    env = make_proxied(tmp_path, monkeypatch, "10.0.0.0/8")
    add_account("alice", now=env.clock.now)
    client = peer_client(env, "10.0.0.5")
    headers = {"Origin": "http://localhost",
               "X-Forwarded-For": "203.0.113.9, 10.0.0.6"}
    assert client.post(
        "/login",
        data={"account": "alice", "password": "wrong password here!!"},
        headers=headers,
        follow_redirects=False,
    ).status_code == 401
    capsys.readouterr()
    delayed = client.post(
        "/login",
        data={"account": "alice", "password": PASSWORD},
        headers=headers,
        follow_redirects=False,
    )
    assert delayed.status_code == 429
    (line,) = auth_lines(capsys)
    assert line["event"] == "sign_in_delayed"
    assert line["client"] == "203.0.113.9"
    assert set(line) == {"at", "event", "account", "client", "wait_s"}


def test_sign_in_uses_peer_when_untrusted_proxy(tmp_path, monkeypatch, capsys):
    env = make_proxied(tmp_path, monkeypatch, "10.0.0.0/8")
    add_account("alice", now=env.clock.now)
    client = peer_client(env, "192.0.2.10")
    response = client.post(
        "/login",
        data={"account": "alice", "password": PASSWORD},
        headers={"Origin": "http://localhost",
                 "X-Forwarded-For": "203.0.113.99"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    (line,) = auth_lines(capsys)
    assert line["event"] == "sign_in"
    assert line["client"] == "192.0.2.10"


def unused_imports_guard():
    assert MAX_KEYS == 10_000
    assert CheapHasher is not None
    assert cookie_header is not None
    assert set_cookies is not None
