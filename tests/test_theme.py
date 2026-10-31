"""Crisp Technical Daylight theme pass: tokens, compact top bar, typography.

Web tests assert HTML fragments through TestClient.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "src" / "agent_dashboard" / "static" / "style.css"

SPEC_BASE_TOKENS = {
    "--bg": "#e5e7eb",
    "--card": "#ffffff",
    "--surface-2": "#f3f4f6",
    "--hover": "#f0f9ff",
    "--ink": "#111827",
    "--muted": "#4b5563",
    "--line": "#cbd5e1",
    "--accent": "#0284c7",
    "--link": "#0369a1",
    "--radius": "2px",
}

# Full CSS named-colour set minus the functional keywords that are not
# palette colours (transparent/currentColor are allowed outside :root).
NAMED_COLOURS = {
    "aliceblue", "antiquewhite", "aqua", "aquamarine", "azure", "beige",
    "bisque", "black", "blanchedalmond", "blue", "blueviolet", "brown",
    "burlywood", "cadetblue", "chartreuse", "chocolate", "coral",
    "cornflowerblue", "cornsilk", "crimson", "cyan", "darkblue", "darkcyan",
    "darkgoldenrod", "darkgray", "darkgrey", "darkgreen", "darkkhaki",
    "darkmagenta", "darkolivegreen", "darkorange", "darkorchid", "darkred",
    "darksalmon", "darkseagreen", "darkslateblue", "darkslategray",
    "darkslategrey", "darkturquoise", "darkviolet", "deeppink",
    "deepskyblue", "dimgray", "dimgrey", "dodgerblue", "firebrick",
    "floralwhite", "forestgreen", "fuchsia", "gainsboro", "ghostwhite",
    "gold", "goldenrod", "gray", "grey", "green", "greenyellow", "honeydew",
    "hotpink", "indianred", "indigo", "ivory", "khaki", "lavender",
    "lavenderblush", "lawngreen", "lemonchiffon", "lightblue", "lightcoral",
    "lightcyan", "lightgoldenrodyellow", "lightgray", "lightgrey",
    "lightgreen", "lightpink", "lightsalmon", "lightseagreen",
    "lightskyblue", "lightslategray", "lightslategrey", "lightsteelblue",
    "lightyellow", "lime", "limegreen", "linen", "magenta", "maroon",
    "mediumaquamarine", "mediumblue", "mediumorchid", "mediumpurple",
    "mediumseagreen", "mediumslateblue", "mediumspringgreen",
    "mediumturquoise", "mediumvioletred", "midnightblue", "mintcream",
    "mistyrose", "moccasin", "navajowhite", "navy", "oldlace", "olive",
    "olivedrab", "orange", "orangered", "orchid", "palegoldenrod",
    "palegreen", "paleturquoise", "palevioletred", "papayawhip",
    "peachpuff", "peru", "pink", "plum", "powderblue", "purple",
    "rebeccapurple", "red", "rosybrown", "royalblue", "saddlebrown",
    "salmon", "sandybrown", "seagreen", "seashell", "sienna", "silver",
    "skyblue", "slateblue", "slategray", "slategrey", "snow", "springgreen",
    "steelblue", "tan", "teal", "thistle", "tomato", "turquoise", "violet",
    "wheat", "white", "whitesmoke", "yellow", "yellowgreen",
}


def _uncommented() -> str:
    return re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)


def _root_block() -> str:
    no_comments = _uncommented()
    match = re.search(r":root\s*\{(.*?)\}", no_comments, flags=re.S)
    assert match is not None, ":root block missing"
    return match.group(1)


def _outside_root() -> str:
    no_comments = _uncommented()
    match = re.search(r":root\s*\{(.*?)\}", no_comments, flags=re.S)
    assert match is not None
    return no_comments[: match.start()] + no_comments[match.end() :]


def test_root_declares_light_color_scheme():
    assert "color-scheme: light" in _root_block()


def test_root_carries_spec_base_tokens():
    root = _root_block().lower()
    for token, value in SPEC_BASE_TOKENS.items():
        pattern = re.compile(re.escape(token) + r"\s*:\s*" + re.escape(value) + r"\s*;")
        assert pattern.search(root), f"{token}: {value} missing from :root"


def test_no_literal_colours_outside_root():
    rest = _outside_root()
    rest = re.sub(r'"[^"]*"', "", rest)
    rest = re.sub(r"'[^']*'", "", rest)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", rest), "hex colour outside :root"
    assert not re.findall(r"(?:rgb|hsl|lab|lch|color)\(", rest), "colour function outside :root"
    words = set(re.findall(r"[a-zA-Z][a-zA-Z-]*", rest))
    hits = sorted(w for w in words if w.lower() in NAMED_COLOURS)
    assert not hits, f"named colours outside :root: {hits}"


def test_typography_uses_system_stacks_without_webfonts():
    text = CSS.read_text()
    assert "--font-sans" in text
    assert "--font-mono" in text
    assert "font-family: var(--font-sans)" in text
    assert ".figure" in text and "var(--font-mono)" in text
    assert "@import" not in text and "@font-face" not in text


def test_fleet_page_renders_compact_topbar():
    from test_web import make_client

    client, _ = make_client([("worker-a", "running")])
    body = client.get("/").text
    assert '<meta name="color-scheme" content="light">' in body
    assert "<h1>Agent fleet dashboard</h1>" in body
    assert '<header class="topbar">' in body
    assert "Managed instances on this Docker host" in body
    assert 'aria-label="Breadcrumb"' not in body
    css = client.get("/static/style.css").text
    topbar = css.split(".topbar {", 1)[1].split("}", 1)[0]
    assert "padding: 0.5rem 1rem" in topbar
    assert "border-top: 3px solid var(--accent)" in topbar
    assert "font-family: var(--font-mono)" in topbar


def test_topbar_chips_show_only_real_values():
    from test_web import make_client

    client, _ = make_client([("worker-a", "running")])
    header = client.get("/").text.split("<header", 1)[1].split("</header>", 1)[0]
    assert '<span class="topbar-led" aria-hidden="true"></span>' in header
    assert '<span class="topbar-chip">Stream <strong>' in header
    assert '<span class="topbar-chip">Control <strong>on</strong></span>' in header
    # No prototype host/schema chips.
    assert "docker.sock" not in header
    assert "schema" not in header.lower()


def test_topbar_chip_reports_control_off():
    from fake_gateway import FakeGateway
    from fastapi.testclient import TestClient

    from agent_dashboard.app import create_app
    from agent_dashboard.config import Settings

    app = create_app(
        settings=Settings(poll_interval=5.0, control=False),
        gateway=FakeGateway(),
        start_poller=False,
    )
    body = TestClient(app, base_url="http://localhost").get("/").text
    assert '<span class="topbar-chip">Control <strong>off</strong></span>' in body
    assert '<span class="topbar-chip">Stream <strong>5s</strong></span>' in body


def test_history_page_renders_compact_topbar_with_breadcrumb_and_actions():
    from test_history import _history_payload_old_and_recent, make_history_client

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _history_payload_old_and_recent()}
    )
    body = client.get("/instances/work/history").text
    assert "<h1>Agent fleet dashboard</h1>" in body
    assert '<header class="topbar pinned">' in body
    assert '<nav aria-label="Breadcrumb">' in body
    assert '<a href="/">Fleet</a>' in body
    assert '<span aria-current="page">work</span>' in body
    header = body.split("<header", 1)[1].split("</header>", 1)[0]
    assert "topbar-actions" in header
    assert "data is read on demand" in header
    assert ">Refresh</a>" in header


def test_attempt_log_page_renders_compact_topbar_with_breadcrumb_and_actions():
    from test_attempt_log import (
        STARTED_COLON,
        STARTED_DASH,
        TOOL_CALL_NEW,
        _payload,
        make_log_client,
    )

    client, _, _ = make_log_client(
        [("work", "running")], {"id-work": _payload([TOOL_CALL_NEW])}
    )
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}").text
    assert "<h1>Agent fleet dashboard</h1>" in body
    assert '<header class="topbar pinned">' in body
    assert '<nav aria-label="Breadcrumb">' in body
    assert '<a href="/">Fleet</a>' in body
    assert '<a href="/instances/work/history">work</a>' in body
    assert f'<span aria-current="page">#98 · {STARTED_COLON}</span>' in body
    header = body.split("<header", 1)[1].split("</header>", 1)[0]
    assert "data is read on demand" in header
    assert ">Refresh</a>" in header


def test_progress_page_renders_compact_topbar_with_breadcrumb_and_actions():
    from test_attempt_log import STARTED_DASH
    from test_progress import (
        _progress_payload,
        _reading_event,
        make_progress_client,
    )

    client, _, _ = make_progress_client(
        [("work", "running")],
        {"id-work": _progress_payload([_reading_event(1, 8, "2026-09-27T16:33:51Z")])},
    )
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/progress").text
    assert "<h1>Agent fleet dashboard</h1>" in body
    assert '<header class="topbar pinned">' in body
    assert '<nav aria-label="Breadcrumb">' in body
    assert '<a href="/">Fleet</a>' in body
    assert '<span aria-current="page">Progress</span>' in body
    header = body.split("<header", 1)[1].split("</header>", 1)[0]
    assert "data is read on demand" in header
    assert ">Refresh</a>" in header
