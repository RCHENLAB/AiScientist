"""The persisted timeline is readable after restart without crossing an account boundary."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from aiscientist import metadata as md  # noqa: E402
from aiscientist.gateway import app as gw  # noqa: E402
from aiscientist.gateway.run_monitor import RunMonitor  # noqa: E402


@pytest.fixture
def timeline(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    monkeypatch.setattr(gw, "CONSOLE_RUNS_DIR", runs)
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    who = {"user": SimpleNamespace(id=1, username="alice", role="user")}
    monkeypatch.setattr(gw, "_optional_user", lambda request: who["user"])
    path = gw._timing_path(runs / "alice", "run0001")
    monitor = RunMonitor(run_id="run0001", conversation_id="c1", owner_id=1,
                         question="Private question must not appear in the operational timeline")
    monitor.bind_timing(path)
    monitor.begin_span("model_call", role="plan")
    # No live registry, Connection, finished result or database row is needed to read it.
    return SimpleNamespace(path=path, runs=runs, who=who, client=TestClient(gw.app))


@pytest.mark.parametrize("user,status", [
    (None, 401),
    (SimpleNamespace(id=2, username="other", role="user"), 403),
    (SimpleNamespace(id=1, username="alice", role="user"), 200),
    (SimpleNamespace(id=3, username="admin", role="admin"), 200),
])
def test_timing_endpoint_requires_owner_or_admin(timeline, user, status):
    timeline.who["user"] = user
    response = timeline.client.get("/api/timing/alice/run0001")
    assert response.status_code == status
    if status != 200:
        assert "model_call" not in response.text


def test_endpoint_recovers_partial_without_claiming_completion_or_mutating_it(timeline):
    with timeline.path.open("a") as stream:
        stream.write('{"event":"span_end"')  # a process died during its last append
    before = timeline.path.read_bytes()
    response = timeline.client.get("/api/timing/alice/run0001")
    assert response.status_code == 200
    data = response.json()
    assert data["run_id"] == "run0001" and data["conversation_id"] == "c1"
    assert data["partial"] and data["ignored_lines"] == 1 and data["durations_overlap"]
    span = data["spans"][0]
    assert span["started_at"].endswith("Z")
    assert span["ended_at"] is None and span["duration_seconds"] is None
    assert span["outcome"] == "unknown" and span["partial"]
    assert "Private question" not in response.text
    assert timeline.path.read_bytes() == before


def test_endpoint_refuses_symlink_to_another_account(timeline):
    other = timeline.runs / "other" / "secret"
    other.mkdir(parents=True)
    (timeline.runs / "alice" / "linked").symlink_to(other, target_is_directory=True)
    path = gw._timing_path(timeline.runs / "other", "secret")
    monitor = RunMonitor(run_id="secret")
    monitor.bind_timing(path)
    monitor.begin_span("sensitive_other_stage")
    response = timeline.client.get("/api/timing/alice/linked")
    assert response.status_code == 404
    assert "sensitive_other_stage" not in response.text


def test_endpoint_missing_and_invalid_run_ids_do_not_create_journals(timeline):
    before = sorted(str(p) for p in timeline.runs.rglob("*"))
    assert timeline.client.get("/api/timing/alice/missing").status_code == 404
    assert timeline.client.get("/api/timing/alice/.hidden").status_code == 400
    assert sorted(str(p) for p in timeline.runs.rglob("*")) == before


def test_mutable_timing_is_excluded_only_at_its_operational_path(tmp_path):
    art = tmp_path / "artifacts"
    (art / "process").mkdir(parents=True)
    (art / "report.md").write_text("# Result\n")
    (art / "timing.jsonl").write_text('{"research_data":1}\n')
    (art / "process" / "timing.jsonl").write_text('{"event":"span_start"}\n')
    before = md.result_files(art)
    assert {file["path"] for file in before} == {"report.md", "timing.jsonl"}
    with (art / "process" / "timing.jsonl").open("a") as stream:
        stream.write('{"event":"save_requested"}\n')
    assert md.result_files(art) == before
    # The operational journal is excluded even when a caller overrides other skip rules.
    assert all(file["path"] != "process/timing.jsonl" for file in md.result_files(art, skip=()))


def test_console_timing_renderer_distinguishes_unknown_and_escapes_metadata():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (Path(__file__).parents[1] / "frontend" / "console" / "app.js").read_text()
    code = source[source.index("function runTimingHtml("):source.index("// A run's breakdown by pipeline")]
    data = {"partial": True, "spans": [
        {"stage": "user_review", "category": "user_wait", "duration_seconds": 12.25,
         "started_at": "2026-10-30T00:00:00Z", "ended_at": "2026-10-30T00:00:12.250Z",
         "outcome": "success", "metadata": {"action": "approve"}},
        {"stage": "hpc_execution", "category": "processing", "duration_seconds": None,
         "started_at": None, "ended_at": None, "outcome": "unknown",
         "metadata": {"source": "<script>bad</script>", "job_id": "7&9"}},
    ]}
    harness = r"""
const escapeHtml = (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
""" + code + "\nconsole.log(runTimingHtml(" + json.dumps(data) + "));"
    result = subprocess.run([node, "-"], input=harness, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    html = result.stdout
    assert "partial timeline" in html and "user_wait" in html and "12.250" in html
    assert "durations do not add up to wall time" in html
    assert "<script>" not in html and "&lt;script&gt;" in html and "7&amp;9" in html
    assert "<td>unknown</td><td>unknown</td><td>unknown</td>" in html
