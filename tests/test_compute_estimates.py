"""Offline estimates and accounting tests; all storage is isolated from workspace runs."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aiscientist.gateway import db, estimates
from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.models import ComputeUsage, Run
from aiscientist.gateway.settings import HPCSettings
from aiscientist.gateway.slurm_job import collect_accounting, parse_accounting
from aiscientist.tools.sdk import HarnessTool


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'usage.db').as_posix()}")
    db.reset()
    db.init_db()
    yield
    db.reset()


@pytest.fixture
def rates():
    return HPCSettings(cpu_hour_rate=0.1, gpu_hour_rate=2)


@pytest.fixture
def declared_profile(monkeypatch):
    value = {"unit": "samples", "baseline": 1, "steps": [
        {"name": "filter", "cpus": 2, "gpus": 0, "mem_gb": 8, "minutes": [10, 20]},
        {"name": "detect", "cpus": 4, "gpus": 1, "mem_gb": 16, "minutes": [5, 10]},
    ]}
    monkeypatch.setattr(estimates.catalog, "resource_profile", lambda name: value)
    return value


def test_step_ranges_and_plan_totals(declared_profile, rates):
    tools = [SimpleNamespace(name="qc"), SimpleNamespace(name="finish")]
    plan = estimates.estimate_plan(["Run qc", "Repeat qc"], tools, {"samples": ["a", "b"]}, rates)
    first = plan["steps"][0]
    assert first["seconds"] == [1800, 3600]
    assert first["cpu_hours"] == pytest.approx([4 / 3, 8 / 3])
    assert first["gpu_hours"] == pytest.approx([1 / 6, 1 / 3])
    assert first["cost"] == pytest.approx([7 / 15, 14 / 15])
    for key in estimates.FIELDS:
        assert plan["total"][key] == pytest.approx([x * 2 for x in first[key]])
    assert first["rough"] and plan["total"]["rough"]
    assert plan["rates_configured"]
    assert "HPC3 queue wait time is not included" in plan["note"]


@pytest.mark.parametrize(("unit", "baseline", "inputs"), [
    ("samples", 1, {"n_samples": 2}),
    ("cells", 10000, {"dataset_result": {"n_cells": 20000}}),
    ("gb", 1, {"size_bytes": 2 * 1024**3}),
])
def test_size_scaling(declared_profile, rates, unit, baseline, inputs):
    declared_profile.update(unit=unit, baseline=baseline)
    got = estimates.estimate_skill("qc", inputs, rates, previous=[])
    assert got["seconds"] == [1800, 3600]
    assert got["units"] == 2 * baseline


@pytest.mark.parametrize("nested", [False, True])
def test_sample_count_from_cellranger_layout(declared_profile, rates, nested):
    inputs = {"cellranger_layout": {"n_libraries": 3}}
    if nested:
        inputs = {"dataset_result": inputs}
    got = estimates.estimate_skill("qc", inputs, rates, previous=[])
    assert got["units"] == 3
    assert got["seconds"] == [2700, 5400]


def test_unknown_tool_and_missing_size_are_rough(monkeypatch, rates):
    monkeypatch.setattr(estimates.catalog, "resource_profile", lambda name: None)
    monkeypatch.setattr(estimates.catalog, "manifest", lambda name: None)
    got = estimates.estimate_skill("new_tool", {}, rates)
    assert got["rough"]
    assert 0 < got["seconds"][0] < got["seconds"][1]
    assert got["gpu_hours"] == [0, 0]
    assert got["profile_steps"][0]["mem_gb"] > 0
    plan = estimates.estimate_plan(["Unknown analysis"], [], {}, rates)
    assert plan["steps"][0]["skill"] == "Unspecified"
    assert plan["total"]["rough"]


def test_default_profile_stays_rough_with_history(monkeypatch, rates):
    monkeypatch.setattr(estimates.catalog, "resource_profile", lambda name: None)
    monkeypatch.setattr(estimates.catalog, "manifest", lambda name: None)
    for number in range(3):
        estimates.record_usage(str(number), "new_tool", {"cells": 10000}, 120, rates, success=True)
    got = estimates.estimate_skill("new_tool", {"cells": 10000}, rates)
    assert got["history_count"] == 3
    assert got["rough"]


def test_history_refines_resource_hours_and_excludes_failures(declared_profile, rates):
    for number in range(3):
        estimates.record_usage(str(number), "qc", {"samples": 2}, 120, rates, success=True,
                               jobs=[{"seconds": 120, "cpu_hours": 1, "gpu_hours": 0.5}])
    estimates.record_usage("failed", "qc", {"samples": 2}, 999999, rates, success=False)
    estimates.record_usage("other", "other_skill", {"samples": 2}, 999999, rates, success=True)
    got = estimates.estimate_skill("qc", {"samples": 4}, rates)
    assert got["history_count"] == 3
    assert got["seconds"] == [180, 360]
    assert got["cpu_hours"] == [1.5, 3]
    assert got["gpu_hours"] == [0.75, 1.5]
    assert got["cost"] == pytest.approx([1.65, 3.3])
    assert not got["rough"]


def test_usage_and_estimate_persist_across_database_reopen(declared_profile, rates):
    planned = estimates.estimate_plan(["qc"], [SimpleNamespace(name="qc")], {"samples": 1}, rates)
    estimates.save_estimate("run", planned)
    local = estimates.record_usage("run", "qc", {"samples": 1}, 3600, rates, success=True)
    assert local == {"seconds": 3600, "cpu_hours": 4, "gpu_hours": 1,
                     "cost": 2.4, "rough": True, "source": "wall_clock"}
    remote = estimates.record_usage("run", "qc", {"samples": 1}, 99999, rates, success=False,
                                   jobs=[{"seconds": 60, "cpu_hours": 0.5, "gpu_hours": 0.25},
                                         {"seconds": 120, "cpu_hours": 1, "gpu_hours": 0.5}])
    assert remote["seconds"] == 180
    assert remote["source"] == "sacct" and not remote["rough"]
    db.reset()
    stored = estimates.run_usage("run")
    assert stored["estimate"] == planned
    assert stored["actual"] == {"seconds": 3780, "cpu_hours": 5.5, "gpu_hours": 1.75,
                                "cost": pytest.approx(4.05), "rough": True, "calls": 2}
    assert estimates.run_usage("absent")["estimate"] is None
    assert estimates.run_usage("absent")["actual"]["calls"] == 0


def test_runs_list_reads_every_runs_usage_in_two_queries(declared_profile, rates):
    planned = estimates.estimate_plan(["qc"], [SimpleNamespace(name="qc")], {"samples": 1}, rates)
    estimates.save_estimate("public-run", planned)
    estimates.record_usage("public-run", "qc", {"samples": 1}, 60, rates, success=True)
    estimates.record_usage("other-run", "qc", {"samples": 1}, 120, rates, success=True)
    queries = []
    sqlalchemy = pytest.importorskip("sqlalchemy")
    sqlalchemy.event.listen(db.get_engine(), "before_cursor_execute", lambda *a: queries.append(a[2]))
    got = estimates.runs_usage(["public-run", "other-run", "never-ran"])
    assert len(queries) == 2
    assert got["public-run"]["estimate"] == planned
    assert got["public-run"]["actual"]["seconds"] == 60
    assert got["public-run"]["actual"]["cost"] == pytest.approx(0.04)
    assert got["other-run"]["estimate"] is None and got["other-run"]["actual"]["calls"] == 1
    assert got["never-ran"] == {"estimate": None, "actual": {"seconds": 0, "cpu_hours": 0, "gpu_hours": 0,
                                                             "cost": 0, "rough": False, "calls": 0}}
    assert estimates.run_usage("public-run") == got["public-run"]
    assert "compute_usage" not in Run(run_id="public-run", question="QC", status="done").public()


def test_plan_document_tool_wins_over_tools_named_in_the_text(declared_profile, rates):
    tools = [SimpleNamespace(name="qc"), SimpleNamespace(name="cluster"), SimpleNamespace(name="finish")]
    plan = estimates.estimate_plan(["QC the data before you cluster it", "Write up"], tools, {"samples": 1},
                                   rates, step_tools=["qc", ""])
    assert [s["skill"] for s in plan["steps"]] == ["qc", "Unspecified"]
    # A document that does not match the agenda (or names a tool the session lacks) is not used.
    plan = estimates.estimate_plan(["Run qc, then cluster"], tools, {"samples": 1}, rates, step_tools=["qc", "x"])
    assert plan["steps"][0]["skill"] == "qc, cluster"
    plan = estimates.estimate_plan(["Run qc, then cluster"], tools, {"samples": 1}, rates, step_tools=["absent"])
    assert plan["steps"][0]["skill"] == "qc, cluster"


def test_without_a_database_the_tools_run_unmeasured(rates, monkeypatch):
    def no_db():
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(db, "get_engine", no_db)
    tool = HarnessTool("qc", "quality control", {}, lambda args, ctx: {"status": "ok"})
    assert estimates.instrument_catalog([tool], "no-db", rates) == [tool]


def test_usage_replay_keeps_owning_run_identity(rates):
    pytest.importorskip("fastapi")
    from aiscientist.gateway.app import Connection

    loop = asyncio.new_event_loop()
    conn = Connection(rates, mock=True, loop=loop, username="tester")
    try:
        first = conn.begin_run(conversation_id="conversation-a", run_id="run-a")
        conn.push({"type": "chat_start"}, run=first)
        second = conn.begin_run(conversation_id="conversation-b", run_id="run-b")
        conn.push({"type": "chat_start"}, run=second)
        conn.push({"type": "run_usage", "actual": {"seconds": 60}}, run=first)
        replay = conn.stream_replay_payloads(first.stream)
        usage = next(p for p in replay if p["type"] == "run_usage")
        assert usage["run_id"] == "run-a"
        assert usage["conversation_id"] == "conversation-a"
        assert usage["actual"]["seconds"] == 60
        assert all(p["type"] != "run_usage" for p in conn.stream_replay_payloads(second.stream))
    finally:
        for run in list(conn.live_runs):
            conn.end_run(run)
        loop.close()


@pytest.mark.parametrize("failure", [None, "result", "exception"])
def test_instrumentation_records_success_and_failure(declared_profile, rates, monkeypatch, failure):
    def execute(args, ctx):
        assert estimates.job_usage.get() == []
        if failure == "exception":
            raise RuntimeError("analysis failed")
        return {"status": "failed" if failure else "ok"}

    tool = HarnessTool("qc", "quality control", {}, execute)
    finish = HarnessTool("finish", "finish", {}, execute)
    wrapped, untouched = estimates.instrument_catalog([tool, finish], "instrumented", rates)
    assert untouched is finish
    times = iter([10, 15])
    monkeypatch.setattr(estimates.time, "monotonic", lambda: next(times))
    ctx = SimpleNamespace(decisions={"samples": 3})
    if failure == "exception":
        with pytest.raises(RuntimeError, match="analysis failed"):
            wrapped.executor({}, ctx)
    else:
        assert wrapped.executor({}, ctx)["status"] == ("failed" if failure else "ok")
    assert estimates.job_usage.get() is None
    with db.session_scope() as session:
        row = session.scalar(select(ComputeUsage))
        assert row.seconds == 5
        assert row.units == 3
        assert row.success is (failure is None)


@pytest.mark.parametrize("accounting_available", [True, False])
def test_collect_accounting_flows_into_instrumented_usage(
        declared_profile, rates, monkeypatch, accounting_available):
    commands = []

    class Executor:
        def exec(self, command):
            commands.append(command)
            if not accounting_available:
                raise RuntimeError("accounting unavailable")
            return ExecResult(command, 0,
                              "123.batch|3600|8|gres/gpu=2|COMPLETED\n"
                              "123|1800|8|gres/gpu=2|COMPLETED\n", "")

    def execute(args, ctx):
        collect_accounting(Executor(), "123")
        return {"status": "ok"}

    tool = HarnessTool("qc", "quality control", {}, execute)
    wrapped = estimates.instrument_catalog([tool], "accounted", rates)[0]
    times = iter([10, 70])
    monkeypatch.setattr(estimates.time, "monotonic", lambda: next(times))
    assert wrapped.executor({}, SimpleNamespace(decisions={"samples": 1})) == {"status": "ok"}
    assert len(commands) == 1
    assert "sacct -j 123" in commands[0]
    assert "JobIDRaw,ElapsedRaw,AllocCPUS,AllocTRES,State" in commands[0]
    with db.session_scope() as session:
        row = session.scalar(select(ComputeUsage))
        assert row.success
        assert row.source == ("sacct" if accounting_available else "wall_clock")
        assert row.seconds == (1800 if accounting_available else 60)
        assert row.cpu_hours == pytest.approx(4 if accounting_available else 4 / 60)
        assert row.gpu_hours == pytest.approx(1 if accounting_available else 1 / 60)
        assert row.cost == pytest.approx(2.4 if accounting_available else 2.4 / 60)
    assert estimates.job_usage.get() is None


def test_accounting_fallback_uses_allocated_time_instead_of_queue_time(declared_profile, rates, monkeypatch):
    class Executor:
        def exec(self, command):
            return ExecResult(command, 1, "", "accounting unavailable")

    def execute(args, ctx):
        collect_accounting(Executor(), "123", elapsed=60)
        return {"status": "ok"}

    wrapped = estimates.instrument_catalog([HarnessTool("qc", "QC", {}, execute)], "queued", rates)[0]
    times = iter([0, 3660])
    monkeypatch.setattr(estimates.time, "monotonic", lambda: next(times))
    wrapped.executor({}, SimpleNamespace(decisions={"samples": 1}))
    actual = estimates.run_usage("queued")["actual"]
    assert actual["seconds"] == 60
    assert actual["cpu_hours"] == pytest.approx(4 / 60)
    assert actual["gpu_hours"] == pytest.approx(1 / 60)
    assert actual["cost"] == pytest.approx(0.04)
    assert actual["rough"]


def test_rates_are_configurable(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_CPU_HOUR_RATE", "0.025")
    monkeypatch.setenv("AISCIENTIST_GPU_HOUR_RATE", "1.75")
    settings = HPCSettings.from_env()
    assert settings.cpu_hour_rate == 0.025
    assert settings.gpu_hour_rate == 1.75


@pytest.mark.parametrize("raw", ["", "bad", "-1", "NaN", "inf"])
def test_invalid_rates_fall_back_to_zero(monkeypatch, raw):
    monkeypatch.setenv("AISCIENTIST_CPU_HOUR_RATE", raw)
    monkeypatch.setenv("AISCIENTIST_GPU_HOUR_RATE", raw)
    settings = HPCSettings.from_env()
    assert settings.cpu_hour_rate == settings.gpu_hour_rate == 0


def test_sacct_uses_terminal_parent_and_does_not_double_count_gpu_types():
    text = ("123.batch|3600|8|cpu=8,gres/gpu=2|COMPLETED\n"
            "123.extern|3600|8|cpu=8,gres/gpu=2|COMPLETED\n"
            "124|3600|8|cpu=8,gres/gpu=2|COMPLETED\n"
            "123|1800|8|cpu=8,gres/gpu=2,gres/gpu:a100=2|COMPLETED\n")
    assert parse_accounting(text, "123") == {"seconds": 1800, "cpu_hours": 4, "gpu_hours": 1}
    assert parse_accounting("123|3600|2|gres/gpu:a100=1,gres/gpu:v100=2|FAILED", "123") == {
        "seconds": 3600, "cpu_hours": 2, "gpu_hours": 3}


@pytest.mark.parametrize("text", ["", "garbage", "123|60|8|cpu=8|RUNNING",
                                  "123.batch|60|8|cpu=8|COMPLETED",
                                  "123|-1|8|cpu=8|COMPLETED",
                                  "123|NaN|8|cpu=8|COMPLETED",
                                  "123|inf|8|cpu=8|COMPLETED",
                                  "123|bad|8|cpu=8|COMPLETED"])
def test_sacct_missing_or_incomplete_accounting(text):
    assert parse_accounting(text, "123") is None
