"""Where an HPC3 job's own files go: the script, the args/result JSON, the Slurm log, the tool log.

RCHENLAB/AiScientist#2: the scanpy analysis line wrote all of these under a hidden directory in the
user's home (``$HOME/.bioagent/analysis``). So did the report renderer, the CPU worker and the scGPT
/ render-review scripts: each fell back to a home-directory default that one call site did not
override. What is pinned here is the cause, not the one path:

* no job line HAS a default directory — leaving it out is a ``TypeError``, not a file in ``$HOME``;
* the gateway gives every job line a directory from one function, under the swept
  ``<shared_root>/Temp/<user>/scratch/``.

Offline: signatures, the gateway's source, and fakes. No Slurm, no SSH.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import types
from pathlib import Path

import pytest

from aiscientist.gateway import scgpt_job, vlreview_job, worker
from aiscientist.gateway.settings import SHARED_ROOT, HPCSettings
from aiscientist.gateway.slurm_analysis import SlurmAnalysisExecutor
from aiscientist.gateway.slurm_job import SlurmJobSpec
from aiscientist.gateway.slurm_report import SlurmReportRenderer
from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor

GATEWAY_DIR = Path(worker.__file__).parent

# Every job line the gateway starts on HPC3, by the name of its directory under Temp/<user>/scratch/.
JOB_LINES = {"analysis", "runcode", "variant", "phenotype", "paperqa", "report", "worker"}


# --- no default: a caller that names no directory does not get the home directory ---------------


@pytest.mark.parametrize("cls, name", [
    (SlurmAnalysisExecutor, "scratch_dir"),
    (SlurmCodeExecutor, "scratch_dir"),
    (SlurmReportRenderer, "scratch_dir"),
    (SlurmJobSpec, "submit_dir"),
])
def test_a_job_line_has_no_default_directory(cls, name):
    (field,) = [f for f in dataclasses.fields(cls) if f.name == name]
    assert field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING


def test_the_worker_has_no_default_directory():
    param = inspect.signature(worker.ensure_worker).parameters["scratch_dir"]
    assert param.default is inspect.Parameter.empty and param.kind is inspect.Parameter.KEYWORD_ONLY


def test_leaving_the_directory_out_is_an_error_not_a_file_in_the_home_directory():
    with pytest.raises(TypeError, match="scratch_dir"):
        SlurmAnalysisExecutor(remote=None, container_image="/img.sif")
    with pytest.raises(TypeError, match="submit_dir"):
        SlurmJobSpec(script="#!/bin/bash\n", job_name="j")


# --- the gateway: one function names the directory, for every job line --------------------------


def test_the_gateway_puts_job_files_under_the_swept_temp_area():
    pytest.importorskip("fastapi")      # gateway.app needs it; CI installs it only for app tests
    from aiscientist.gateway import app as gw

    conn = types.SimpleNamespace(executor=types.SimpleNamespace(username="tester"), owner="tester",
                                 settings=HPCSettings())
    for kind in JOB_LINES:
        path = gw._scratch_dir(conn, kind)
        assert path == f"{SHARED_ROOT}/Temp/tester/scratch/{kind}"
        assert path.startswith("/") and "$" not in path     # Slurm expands nothing in --output
        assert not path.startswith(gw._storage_base(conn) + "/")    # nor the personal lab folder


def test_every_job_line_in_the_gateway_takes_its_directory_from_that_function():
    tree = ast.parse((GATEWAY_DIR / "app.py").read_text(encoding="utf-8"))
    given = [kw.value for node in ast.walk(tree) if isinstance(node, ast.Call)
             for kw in node.keywords if kw.arg == "scratch_dir"]
    kinds = set()
    for value in given:
        assert (isinstance(value, ast.Call) and getattr(value.func, "id", "") == "_scratch_dir"), \
            f"line {value.lineno}: a job line builds its own scratch path instead of _scratch_dir(conn, kind)"
        conn, kind = value.args
        assert isinstance(conn, ast.Name) and conn.id == "conn" and isinstance(kind, ast.Constant)
        kinds.add(kind.value)
    assert kinds == JOB_LINES


def test_the_report_renderer_the_gateway_builds_writes_under_temp(tmp_path):
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw

    st = HPCSettings()
    st.report_on_hpc = True
    loop = asyncio.new_event_loop()
    conn = gw.Connection(st, mock=False, loop=loop, username="tester")
    loop.close()
    conn.workspace = tmp_path
    conn.executor = types.SimpleNamespace(username="tester")
    fn = gw._build_report_render_fn(conn)
    assert isinstance(fn, SlurmReportRenderer)
    assert fn.scratch_dir == f"{SHARED_ROOT}/Temp/tester/scratch/report"


# --- scGPT and the render review: the script goes beside the job's log --------------------------


class _Submitted(Exception):
    """Stops the fake job at submission: only where the script would be written is under test."""


def _capture(seen: list):
    def run_batch_job(executor, spec, **_):
        seen.append(spec)
        raise _Submitted
    return run_batch_job


OUT = f"{SHARED_ROOT}/Temp/tester/scgpt/run1/out"


def test_the_scgpt_script_is_written_in_the_runs_output_directory(monkeypatch):
    seen: list[SlurmJobSpec] = []
    monkeypatch.setattr(scgpt_job, "run_batch_job", _capture(seen))
    with pytest.raises(_Submitted):
        scgpt_job.run_scgpt_inference(
            types.SimpleNamespace(username="tester"), HPCSettings(),
            input_h5ad=f"{SHARED_ROOT}/Temp/tester/scgpt/run1/query.h5ad", model_dir="/dfs/model",
            out_dir=OUT)
    (spec,) = seen
    assert spec.submit_dir == OUT
    assert f"#SBATCH --output={OUT}/{spec.job_name}-%j.log" in spec.script


def test_the_render_review_script_is_written_in_its_output_directory(monkeypatch):
    seen: list[SlurmJobSpec] = []
    monkeypatch.setattr(vlreview_job, "run_batch_job", _capture(seen))
    out = f"{SHARED_ROOT}/Temp/tester/reports/run1/vlreview"
    with pytest.raises(_Submitted):
        vlreview_job.run_vlreview(types.SimpleNamespace(username="tester"), HPCSettings(),
                                  pdf=f"{out}/report.pdf", out_dir=out)
    (spec,) = seen
    assert spec.submit_dir == out
