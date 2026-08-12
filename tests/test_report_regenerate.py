"""Offline tests for the report-regenerate path (A1): rebuild a prior run's report from its
persisted bundle WITHOUT re-running the PI or the analysis.

Covers the pure helpers (`_split_front_matter`, `_edit_report_body`), the endpoint's validation
(404/409), and the `_regenerate_report` worker (re-render as-is + instruction-driven edit) with a
fake `build_pdf_report` and a stub LLM — no real Slurm, pandoc, or vLLM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.gateway.settings import HPCSettings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_conns():
    yield
    gw_app.CONNECTIONS.clear()


def _conn(tmp_path, **attrs):
    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "tester"
    for k, v in attrs.items():
        setattr(conn, k, v)
    gw_app.CONNECTIONS[conn.id] = conn
    return conn, loop


def _seed_run(conn, run_id="run123", body="Original body paragraph.\n"):
    art = conn.workspace / run_id / "artifacts"
    (art / "report").mkdir(parents=True)
    (art / "figures").mkdir(parents=True)
    (art / "figures" / "umap.png").write_bytes(b"x")
    (art / "report" / "report.md").write_text('---\ntitle: "T"\n---\n\n' + body, encoding="utf-8")
    return art


def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


# --- pure helpers -----------------------------------------------------------

def test_split_front_matter():
    front, body = gw_app._split_front_matter('---\ntitle: "X"\n---\n\nHello\n')
    assert front == '---\ntitle: "X"\n---'
    assert body == "Hello\n"
    # no front matter → untouched
    assert gw_app._split_front_matter("# Just a body\n") == ("", "# Just a body\n")


def test_edit_report_body_never_loses_the_report(tmp_path):
    art = tmp_path / "art"
    (art / "figures").mkdir(parents=True)
    body = "A reasonably long original report body. " * 6

    # degenerate edit (model returned almost nothing) → keep the original
    assert gw_app._edit_report_body(body, "shorten", art, lambda m: "tiny") == body
    # LLM raised → keep the original
    def boom(_m):
        raise RuntimeError("llm down")
    assert gw_app._edit_report_body(body, "x", art, boom) == body
    # a real edit is used
    edited = "E" * 600
    assert gw_app._edit_report_body(body, "x", art, lambda m: edited) == edited


def test_edit_report_body_constrains_figures(tmp_path):
    art = tmp_path / "art"
    (art / "figures").mkdir(parents=True)
    (art / "figures" / "umap.png").write_bytes(b"x")
    seen = {}
    gw_app._edit_report_body("body " * 60, "tweak", art,
                             lambda msgs: seen.update(user=msgs[-1]["content"]) or ("Z" * 600))
    assert "figures/umap.png" in seen["user"]   # valid figure list handed to the model


# --- endpoint validation ----------------------------------------------------

def test_regenerate_409_when_no_previous_run(tmp_path):
    conn, _ = _conn(tmp_path)
    r = _client().post("/api/report/regenerate", json={"connection_id": conn.id})
    assert r.status_code == 409


def test_regenerate_404_when_report_md_missing(tmp_path):
    conn, _ = _conn(tmp_path, last_run_id="ghost")
    r = _client().post("/api/report/regenerate", json={"connection_id": conn.id})
    assert r.status_code == 404


def test_regenerate_409_when_a_run_is_in_progress(tmp_path):
    conn, _ = _conn(tmp_path, last_run_id="run123", chat_running=True)
    _seed_run(conn)
    r = _client().post("/api/report/regenerate", json={"connection_id": conn.id})
    assert r.status_code == 409


def test_regenerate_unknown_connection():
    r = _client().post("/api/report/regenerate", json={"connection_id": "nope"})
    assert r.status_code == 404


# --- the worker -------------------------------------------------------------

def _fake_build(captured):
    def build(md, out_dir, **kw):
        captured["md"] = md
        captured["basename"] = kw.get("basename")
        captured["title"] = kw.get("title")
        out = Path(out_dir) / f"{kw.get('basename')}.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF fake")
        return {"status": "ok", "pdf_path": str(out), "docx_path": None, "error": None}
    return build


def test_regenerate_rerenders_as_is_without_calling_the_llm(tmp_path, monkeypatch):
    conn, loop = _conn(tmp_path, last_run_id="run123")
    art = _seed_run(conn)
    captured: dict = {}
    monkeypatch.setattr("bioagent.tools.report.build_pdf_report", _fake_build(captured))
    # If the LLM were called with no instruction, that's a bug — make it explode.
    monkeypatch.setattr(gw_app, "_lab_llm", lambda c: (_ for _ in ()).throw(AssertionError("LLM must not run")))
    msgs: list = []
    monkeypatch.setattr(conn, "push", lambda p: msgs.append(p))

    loop.run_until_complete(gw_app._regenerate_report(conn, "run123", art, "report", None))

    types = [m.get("type") for m in msgs]
    assert "chat_start" in types and "chat_done" in types
    assert any(m.get("type") == "artifacts" for m in msgs)
    assert any(m.get("type") == "run_complete" and m.get("run_id") == "run123" for m in msgs)
    assert captured["title"] is None                       # md keeps its own YAML title block
    assert captured["md"].startswith("---")                # front matter preserved
    assert "Original body paragraph." in captured["md"]    # body unchanged


def test_regenerate_applies_instruction_and_preserves_title(tmp_path, monkeypatch):
    conn, loop = _conn(tmp_path, last_run_id="run123")
    art = _seed_run(conn)
    captured: dict = {}
    monkeypatch.setattr("bioagent.tools.report.build_pdf_report", _fake_build(captured))
    monkeypatch.setattr(gw_app, "_lab_llm",
                        lambda c: (lambda messages: "EDITED BODY " + "x" * 600, None, None, None, None))
    monkeypatch.setattr(conn, "push", lambda p: None)

    loop.run_until_complete(
        gw_app._regenerate_report(conn, "run123", art, "report", "make the abstract punchier"))

    assert "EDITED BODY" in captured["md"]                  # instruction applied
    assert captured["md"].startswith('---\ntitle: "T"\n---')  # original title kept
    assert "Original body paragraph." not in captured["md"]  # body was replaced by the edit
