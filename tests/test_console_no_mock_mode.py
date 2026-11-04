"""The console's connect form offers no mock session: a connection always goes to HPC3.

The server still accepts ``mock: true`` on ``POST /api/connect`` (admins only with accounts on),
which the tests and a local gateway use through the API.
"""

from pathlib import Path


CONSOLE = Path(__file__).resolve().parents[1] / "frontend/console"


def test_the_connect_form_has_no_mock_mode():
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")
    assert "mockCheck" not in html and "Mock mode" not in html


def test_connect_sends_no_mock_flag_and_always_asks_for_the_ucinetid():
    app = (CONSOLE / "app.js").read_text(encoding="utf-8")
    start = app.index("async function connect(")
    connect = app[start:app.index("\n}\n", start)]
    assert "mock" not in connect
    assert 'if (!body.ucinetid) { toast("Enter your UCInetID."); return; }' in connect
