import sys

import pytest

from agent_dashboard import cli


def test_main_refuses_non_loopback(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--host", "0.0.0.0"])
    assert exc.value.code == 2
    assert "non-loopback" in capsys.readouterr().err


def test_main_runs_single_worker(monkeypatch):
    import agent_dashboard.cli as cli_module

    seen = {}
    monkeypatch.setattr(sys, "argv", ["agent-dashboard"])
    monkeypatch.delenv("AGENT_DASHBOARD_HOST", raising=False)

    def fake_run(app, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    cli_module.main([])
    assert seen["host"] == "127.0.0.1"
    assert seen["workers"] == 1
    assert "reload" not in seen
