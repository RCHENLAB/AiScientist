"""With accounts on, ``run_code`` never executes on the gateway host.

2026-10-09: the local ``CodeSandbox`` is a plain subprocess of the gateway. On a deployment with
accounts the gateway's user reads every account's saved HPC3 keys and API keys
(``AISCIENTIST_STATE_DIR``), the deployment's ``.env`` and every other account's runs, and any
logged-in account's model-written snippet ran there whenever HPC3 did not take it: run_code-on-HPC3
switched off (the default), a mock session, a workspace that could not be set up, or a failed
Slurm job (the old local fallback).

Each test below sends a REAL snippet that writes a canary file through one of those routes. Nothing
is mocked on the local side, so with accounts off the canary appears (the controls at the bottom
prove the tests can see an execution), and with accounts on it must not.
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_slurm_sandbox import FakeHPC  # noqa: E402

import aiscientist.agents.sandbox as sandbox_mod  # noqa: E402
from aiscientist.agents import provenance  # noqa: E402
import aiscientist.gateway.app as gw  # noqa: E402
from aiscientist.agents.research_harness import HarnessContext  # noqa: E402
from aiscientist.agents.research_lab import make_run_code_tool  # noqa: E402
from aiscientist.gateway.executor import ExecResult  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402
from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor, UnavailableCodeExecutor  # noqa: E402

GATEWAY_DIR = Path(gw.__file__).parent

# The directory the gateway gives this job line: <shared_root>/Temp/<user>/scratch/runcode.
SCRATCH = "/dfs/lab/Temp/tester/scratch/runcode"


class SbatchRejects(FakeHPC):
    """HPC3 is reachable but the job cannot be submitted (bad account, quota, a full queue)."""

    def exec(self, command, timeout=60.0):
        if command.strip().startswith("sbatch"):
            return ExecResult(command="", exit_status=1, stdout="",
                              stderr="sbatch: error: Batch job submission failed")
        return super().exec(command, timeout)


class WorkspaceFails(FakeHPC):
    """The SSH session drops while the run's HPC3 workspace is being created."""

    def exec(self, command, timeout=60.0):
        if command.strip().startswith("mkdir -p") and "/analysis/" in command:
            raise OSError("SSH session is not active")
        return super().exec(command, timeout)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The gateway host as a snippet would find it: a canary path it could write, and a record of
    every snippet the local sandbox is asked to run."""
    monkeypatch.setenv(provenance.SEED_ENABLE_ENV, "0")   # no numpy/torch import per snippet
    spawned: list[str] = []
    real_run = sandbox_mod.CodeSandbox.run

    def recording_run(self, code):
        spawned.append(code)
        return real_run(self, code)

    monkeypatch.setattr(sandbox_mod.CodeSandbox, "run", recording_run)
    # The agent's HPC3 shell (import preflight) is another feature with its own tests.
    monkeypatch.setattr(gw, "_build_hpc_shell", lambda conn, run: None)

    class Host:
        canary = tmp_path / "ran_on_the_gateway_host"
        code = f"open({str(tmp_path / 'ran_on_the_gateway_host')!r}, 'w').write('ran')\nprint('done')"
        events: list[tuple[str, str]] = []

        @staticmethod
        def executed() -> bool:
            return bool(spawned) or Host.canary.exists()

        @staticmethod
        def conn(*, mock: bool = False, remote=None, on_hpc: bool = True):
            st = HPCSettings()
            st.run_code_on_hpc = on_hpc
            loop = asyncio.new_event_loop()
            c = gw.Connection(st, mock=mock, loop=loop, username="tester")
            loop.close()
            c.workspace = tmp_path / "runs" / "tester"
            c.executor = remote
            return c

        @staticmethod
        def build(conn):
            art = conn.workspace / "r1" / "artifacts"
            art.mkdir(parents=True, exist_ok=True)
            Host.events = []
            emit = lambda level, stage, message: Host.events.append((level, message))  # noqa: E731
            return asyncio.run(gw._build_code_executor(conn, None, "r1", {}, art, emit))

        @staticmethod
        def run_code(executor) -> dict:
            """One ``run_code`` call, through the tool the Scientist is given."""
            tool = make_run_code_tool(executor)
            return tool.executor({"code": Host.code}, HarnessContext(workspace=tmp_path))

    return Host


def _accounts(monkeypatch, on: bool) -> None:
    monkeypatch.setattr(gw, "_AUTH_ENABLED", on)


# --- the policy --------------------------------------------------------------------------------

def test_host_code_is_allowed_only_without_accounts(monkeypatch):
    _accounts(monkeypatch, True)
    assert gw._host_code_allowed() is False
    _accounts(monkeypatch, False)
    assert gw._host_code_allowed() is True


# --- accounts on: every former route to the gateway host ---------------------------------------

def test_with_run_code_on_hpc_off_the_snippet_is_refused_not_run_locally(host, monkeypatch):
    # The setting's default. A live HPC3 session exists; the snippet used to run on the host anyway.
    _accounts(monkeypatch, True)
    ex = host.build(host.conn(remote=FakeHPC({}), on_hpc=False))
    out = host.run_code(ex)
    assert isinstance(ex, UnavailableCodeExecutor)
    assert out["status"] == "not_enabled" and "AISCIENTIST_RUN_CODE_ON_HPC" in out["note"]
    assert "never on the AiScientist server" in out["note"]
    assert not host.executed()
    assert any(level == "warning" and "run_code is not available" in msg for level, msg in host.events)


@pytest.mark.parametrize("on_hpc", [True, False])
def test_a_mock_session_runs_no_code_on_the_host(host, monkeypatch, on_hpc):
    _accounts(monkeypatch, True)
    out = host.run_code(host.build(host.conn(mock=True, on_hpc=on_hpc)))
    assert out["status"] == "not_enabled" and "mock session" in out["note"]
    assert not host.executed()


def test_a_session_without_a_live_ssh_connection_runs_no_code_on_the_host(host, monkeypatch):
    _accounts(monkeypatch, True)
    out = host.run_code(host.build(host.conn(remote=None)))
    assert out["status"] == "not_enabled" and "no live HPC3 connection" in out["note"]
    assert not host.executed()


def test_a_failed_hpc3_workspace_setup_does_not_move_the_run_to_the_host(host, monkeypatch):
    # Used to read "run_code uses the local sandbox this run".
    _accounts(monkeypatch, True)
    out = host.run_code(host.build(host.conn(remote=WorkspaceFails({}))))
    assert out["status"] == "not_enabled"
    assert "workspace could not be set up" in out["note"] and "SSH session is not active" in out["note"]
    assert not host.executed()
    assert not any("local sandbox" in msg for _level, msg in host.events)


def test_a_failed_slurm_job_is_a_failed_call_not_a_local_run(host, monkeypatch):
    # The old local fallback: any account whose job could not be submitted ran its snippet here.
    _accounts(monkeypatch, True)
    hpc = SbatchRejects({})
    ex = host.build(host.conn(remote=hpc))
    out = host.run_code(ex)
    assert isinstance(ex, SlurmCodeExecutor) and ex.local_fallback is None
    assert out["status"] == "error" and out.get("execution_mode") == "hpc_slurm"
    assert "sbatch rejected the job" in out["error"]
    assert "not run on the gateway host" in out["error"]
    assert "Batch job submission failed" in out["stderr"]
    assert not host.executed()


def test_a_healthy_session_sends_the_snippet_to_hpc3_only(host, monkeypatch):
    _accounts(monkeypatch, True)
    hpc = FakeHPC({".out": "done\n", ".err": "", ".rc": "0\n"})
    out = host.run_code(host.build(host.conn(remote=hpc)))
    assert out["status"] == "ok" and out["execution_mode"] == "hpc_slurm"
    assert len(hpc.submits) == 1 and "ran_on_the_gateway_host" in hpc.staged_snippet
    assert not host.executed()


def test_the_slurm_executor_alone_runs_nothing_locally_without_a_fallback():
    # The class's own contract, whoever builds it: no local_fallback = no local run, on either path.
    no_remote = SlurmCodeExecutor(remote=None, container_image="/img.sif", scratch_dir=SCRATCH)("print(1)")
    assert no_remote["status"] == "not_enabled" and "not run on the gateway host" in no_remote["note"]
    failed = SlurmCodeExecutor(remote=SbatchRejects({}), container_image="/img.sif", scratch_dir=SCRATCH,
                               startup_timeout_s=5, run_timeout_s=5)("print(1)")
    assert failed["status"] == "error" and "not run on the gateway host" in failed["error"]


# --- accounts off: the single-user console is unchanged (and the canary is real) ---------------

def test_without_accounts_the_local_sandbox_still_runs_the_snippet(host, monkeypatch):
    _accounts(monkeypatch, False)
    ex = host.build(host.conn(remote=None, on_hpc=False))
    out = host.run_code(ex)
    assert isinstance(ex, sandbox_mod.CodeSandbox)
    assert out["status"] == "ok" and host.canary.read_text() == "ran"


def test_without_accounts_a_failed_slurm_job_still_falls_back_to_the_local_sandbox(host, monkeypatch):
    _accounts(monkeypatch, False)
    out = host.run_code(host.build(host.conn(remote=SbatchRejects({}))))
    assert out["status"] == "ok" and out["execution_mode"] == "local_fallback"
    assert host.canary.read_text() == "ran"


def test_without_accounts_a_failed_workspace_setup_still_uses_the_local_sandbox(host, monkeypatch):
    _accounts(monkeypatch, False)
    ex = host.build(host.conn(remote=WorkspaceFails({})))
    assert isinstance(ex, sandbox_mod.CodeSandbox)
    assert any("uses the local sandbox" in msg for _level, msg in host.events)


# --- structure: no second way in ---------------------------------------------------------------

def _functions_naming(name: str) -> set[str]:
    """``file::function`` for every place gateway code names ``name``. Comments and docstrings are
    not code, so they do not count; a closure counts as the top-level function it sits in."""
    found: set[str] = set()
    for path in sorted(GATEWAY_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        spans = [(fn.lineno, fn.end_lineno, fn.name) for fn in ast.walk(tree)
                 if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            named = (isinstance(node, ast.Name) and node.id == name
                     or isinstance(node, ast.Attribute) and node.attr == name
                     or isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names))
            if not named:
                continue
            inside = [span for span in spans if span[0] <= node.lineno <= span[1]]
            found.add(f"{path.name}::{min(inside)[2] if inside else '<module>'}")
    return found


def test_the_gateway_builds_a_local_sandbox_in_exactly_one_guarded_place():
    # A new feature that constructs its own CodeSandbox (a chat tool, a "quick check") would run
    # model-written code on the host for every account again. It has to go through the one builder.
    assert _functions_naming("CodeSandbox") == {"app.py::_build_code_executor"}
    src = ast.get_source_segment(
        (GATEWAY_DIR / "app.py").read_text(encoding="utf-8"),
        next(n for n in ast.walk(ast.parse((GATEWAY_DIR / "app.py").read_text(encoding="utf-8")))
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "_build_code_executor"))
    guard, build = src.index("if _host_code_allowed():"), src.index("CodeSandbox(")
    assert guard < build


def test_the_lab_takes_its_code_executor_from_the_one_builder():
    tree = ast.parse((GATEWAY_DIR / "app.py").read_text(encoding="utf-8"))
    run_lab = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_lab")
    assigned = [n for n in ast.walk(run_lab) if isinstance(n, (ast.Assign, ast.AnnAssign))
                and any(isinstance(t, ast.Name) and t.id == "code_executor"
                        for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    assert len(assigned) == 1
    call = assigned[0].value
    assert isinstance(call, ast.Await) and call.value.func.id == "_build_code_executor"
    assert _functions_naming("SlurmCodeExecutor") == {"app.py::_build_code_executor"}
