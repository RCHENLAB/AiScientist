"""Offline tests for the scgpt_annotate lab tool + its gateway runner.

No real cluster: a scripted fake ``RemoteExecutor`` (Slurm queue + put_file/get_file)
drives the round-trip. Covers graceful not-enabled, the .h5ad guard, catalog
registration, and the full stage -> infer -> fetch -> summarise runner path.
"""

from __future__ import annotations

from pathlib import Path

from bioagent.agents.research_harness import HarnessContext
from bioagent.agents.registry import build_scientist_catalog
from bioagent.gateway.executor import ExecResult
from bioagent.gateway.scgpt_runner import build_scgpt_runner
from bioagent.gateway.settings import HPCSettings
from bioagent.tools.scgpt_annotate import make_scgpt_annotate_tool


# --- the tool itself ---------------------------------------------------------


def _ctx(dataset_path: str | None, workspace: Path | None = None) -> HarnessContext:
    dec = {"dataset_path": dataset_path} if dataset_path is not None else {}
    return HarnessContext(decisions=dec, workspace=workspace)


def test_tool_not_enabled_without_a_runner():
    tool = make_scgpt_annotate_tool(None)
    out = tool.executor({}, _ctx("/data/q.h5ad"))
    assert out["status"] == "not_enabled"


def test_tool_errors_without_a_dataset():
    tool = make_scgpt_annotate_tool(lambda args, ctx: {"status": "ok"})
    assert tool.executor({}, _ctx(None))["status"] == "error"


def test_tool_rejects_non_h5ad_dataset():
    tool = make_scgpt_annotate_tool(lambda args, ctx: {"status": "ok"})
    out = tool.executor({}, _ctx("/data/cells.csv"))
    assert out["status"] == "not_applicable"


def test_tool_calls_runner_for_h5ad():
    seen = {}
    def runner(args, ctx):
        seen["args"] = args
        return {"status": "ok", "tool": "scgpt_annotate"}
    tool = make_scgpt_annotate_tool(runner)
    out = tool.executor({"model_dir": "/m"}, _ctx("/data/q.h5ad"))
    assert out["status"] == "ok" and seen["args"] == {"model_dir": "/m"}


def test_catalog_always_includes_scgpt_annotate():
    names = [t.name for t in build_scientist_catalog()]
    assert "scgpt_annotate" in names


# --- the gateway runner (stage -> infer -> fetch -> summarise) ----------------


class FakeRunnerHost:
    """Fake HPC3 host with a Slurm batch lifecycle + put_file/get_file. ``plans[0]`` is the
    job's per-poll squeue script; ``get_file`` writes a canned predictions.csv."""

    def __init__(self, plans, finals=None):
        self.host = "hpc3-mock"
        self.username = "u1"
        self.plans = plans
        self.finals = finals or {}
        self.submits: list[str] = []
        self.staged: list[tuple[str, str]] = []
        self.fetched: list[tuple[str, str]] = []
        self._poll: dict[str, int] = {}
        self._next = 3000

    def _ok(self, out=""):
        return ExecResult(command="", exit_status=0, stdout=out, stderr="")

    @staticmethod
    def _arg_after(flag, cmd):
        toks = cmd.split()
        return toks[toks.index(flag) + 1] if flag in toks else ""

    def exec(self, command, timeout=60.0):
        cmd = command.strip()
        if "BIOAGENT_EOF" in cmd or ("cat >" in cmd and "<<" in cmd) or cmd.startswith("mkdir"):
            return self._ok()
        if cmd.startswith("sbatch"):
            jid = str(self._next)
            self._next += 1
            self.submits.append(jid)
            return self._ok(f"Submitted batch job {jid}")
        if cmd.startswith("scancel"):
            return self._ok()
        if cmd.startswith("squeue"):
            jid = self._arg_after("-j", cmd)
            idx = self.submits.index(jid)
            plan = self.plans[idx] if idx < len(self.plans) else [""]
            n = self._poll.get(jid, 0)
            self._poll[jid] = n + 1
            val = plan[n] if n < len(plan) else plan[-1]
            if val == "":
                return self._ok("")
            if ":" in val:
                st, node = val.split(":", 1)
                return self._ok(f"{st}|{node}")
            return self._ok(f"{val}|")
        if cmd.startswith("sacct"):
            return self._ok(self.finals.get(self._arg_after("-j", cmd), "COMPLETED"))
        if cmd.startswith("test -f"):
            return self._ok("OK")
        return self._ok()

    def put_file(self, local_path, remote_path):
        self.staged.append((local_path, remote_path))

    def get_file(self, remote_path, local_path):
        self.fetched.append((remote_path, local_path))
        Path(local_path).write_text(
            "barcode,predictions,confidence\n"
            "AAA,Rod,0.98\nBBB,Rod,0.95\nCCC,Cone,0.81\n"
        )

    def open_tunnel(self, *a, **k):
        return 1234

    def close(self):
        pass


def test_runner_stages_infers_fetches_and_summarises(tmp_path):
    dataset = tmp_path / "query.h5ad"
    dataset.write_text("not-real-h5ad-but-exists")
    workspace = tmp_path / "runs" / "abc123"
    workspace.mkdir(parents=True)

    fake = FakeRunnerHost(plans=[["R:gpu-3-1", ""]], finals={"3000": "COMPLETED"})
    runner = build_scgpt_runner(fake, HPCSettings(), cluster_user_dir="/dfs3b/ruic20_lab/u1")
    # Speed up the lifecycle polling for the test via the engine defaults being overridable
    # is not needed here — the fake reports R then "" on the first two polls.
    ctx = HarnessContext(decisions={"dataset_path": str(dataset)}, workspace=workspace)

    out = runner({}, ctx)
    assert out["status"] == "ok"
    assert out["n_cells"] == 3 and out["n_cell_types"] == 2
    assert out["label_counts"] == {"Rod": 2, "Cone": 1}      # sorted by count desc
    # staged the dataset to this run's DFS scratch, fetched predictions into artifacts.
    assert fake.staged and fake.staged[0][0] == str(dataset)
    assert "/scgpt/abc123/query.h5ad" in fake.staged[0][1]
    assert Path(out["predictions_csv"]).is_file()


def test_runner_errors_when_dataset_missing(tmp_path):
    fake = FakeRunnerHost(plans=[["R:gpu-3-1", ""]])
    runner = build_scgpt_runner(fake, HPCSettings(), cluster_user_dir="/dfs3b/ruic20_lab/u1")
    ctx = HarnessContext(decisions={"dataset_path": str(tmp_path / "missing.h5ad")},
                         workspace=tmp_path)
    assert runner({}, ctx)["status"] == "error"
