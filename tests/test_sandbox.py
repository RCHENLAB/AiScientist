"""Tests for the CodeSandbox — real subprocess isolation, no LLM."""

from __future__ import annotations

from bioagent.agents.sandbox import CodeSandbox


def test_runs_code_and_captures_stdout():
    out = CodeSandbox()("print(6 * 7)")
    assert out["status"] == "ok"
    assert out["stdout"].strip() == "42"
    assert out["returncode"] == 0


def test_stop_kills_a_running_snippet():
    # A Stop mid-snippet must KILL the subprocess within ~a poll interval, not wait for the
    # snippet to finish (30s) or the 180s timeout — the old blocking subprocess.run ignored Stop.
    import time
    t0 = time.monotonic()
    out = CodeSandbox(should_cancel=lambda: True)("import time; time.sleep(30)")
    assert out["status"] == "cancelled"
    assert time.monotonic() - t0 < 5   # killed promptly, not after 30s


def test_reports_error_with_traceback():
    out = CodeSandbox()("raise ValueError('boom')")
    assert out["status"] == "error"
    assert "ValueError" in out["stderr"] and "boom" in out["stderr"]
    assert out["error"]  # a concise error is surfaced


def test_exposes_dataset_and_run_paths_to_codeact(tmp_path):
    # The CodeAct snippet can read the dataset + write into the run's artifacts dir.
    ds = tmp_path / "data.csv"
    ds.write_text("a,b\n1,2\n", encoding="utf-8")
    art = tmp_path / "artifacts"
    art.mkdir()
    sandbox = CodeSandbox(dataset_path=str(ds), work_dir=str(tmp_path / "work"), artifacts_dir=str(art))
    code = (
        "import os\n"
        "print(open(os.environ['BIOAGENT_DATASET']).read().strip())\n"
        "open(os.path.join(os.environ['BIOAGENT_ARTIFACTS'], 'out.txt'), 'w').write('made by codeact')\n"
    )
    out = sandbox(code)
    assert out["status"] == "ok" and "a,b" in out["stdout"]
    assert (art / "out.txt").read_text() == "made by codeact"   # CodeAct wrote a real artifact


def test_nonzero_exit_is_error():
    out = CodeSandbox()("import sys; sys.exit(3)")
    assert out["status"] == "error" and out["returncode"] == 3


def test_timeout_is_reported_not_hung():
    out = CodeSandbox(timeout_s=0.5)("import time; time.sleep(10)")
    assert out["status"] == "timeout"
    assert "time limit" in out["error"]


def test_empty_code_is_rejected():
    assert CodeSandbox()("   ")["status"] == "error"


def test_runs_in_isolated_tempdir_not_the_repo():
    # the snippet's cwd is a throwaway temp dir (cleaned up), never the project root
    out = CodeSandbox()("import os; print(os.getcwd())")
    assert out["status"] == "ok"
    cwd = out["stdout"].strip()
    assert "bioagent-code-" in cwd  # the TemporaryDirectory prefix


# --- Execution-context injection (guides the CodeAct model) ----------------------------

def test_build_run_code_context_lists_paths_and_cwd_warning():
    from bioagent.agents.sandbox import build_run_code_context

    sb = CodeSandbox(dataset_path=None, work_dir="/runs/x/work", artifacts_dir="/runs/x/art")
    ctx = build_run_code_context(sb)
    assert "EXECUTION ENVIRONMENT" in ctx
    assert "throwaway temp dir" in ctx and "FileNotFoundError" in ctx   # CWD/path warning
    assert "/runs/x/work" in ctx and "/runs/x/art" in ctx
    assert "SIGKILL" in ctx                                             # memory caveat


def test_build_run_code_context_empty_without_executor():
    from bioagent.agents.sandbox import build_run_code_context

    assert build_run_code_context(None) == ""
    assert build_run_code_context(CodeSandbox()) == ""  # no dataset/work/artifacts set


def test_describe_dataset_obs_missing_path_is_safe():
    from bioagent.agents.sandbox import describe_dataset_obs

    assert describe_dataset_obs(None) == ""
    assert describe_dataset_obs("/no/such/file.h5ad") == ""


def test_sandbox_network_enabled_defaults_on_and_env_toggles_off(monkeypatch):
    from bioagent.agents.sandbox import sandbox_network_enabled

    monkeypatch.delenv("BIOAGENT_SANDBOX_NETWORK", raising=False)
    assert sandbox_network_enabled() is True                 # default ON (Jin Li: prioritize effect)
    for off in ("0", "false", "off", "no", ""):
        monkeypatch.setenv("BIOAGENT_SANDBOX_NETWORK", off)
        assert sandbox_network_enabled() is False, off
    monkeypatch.setenv("BIOAGENT_SANDBOX_NETWORK", "1")
    assert sandbox_network_enabled() is True


def test_container_command_network_follows_env(monkeypatch):
    # With a container image, the singularity command isolates the network ONLY when disabled.
    monkeypatch.delenv("BIOAGENT_SANDBOX_NETWORK", raising=False)   # default ON
    cmd, _ = CodeSandbox(container_image="/img/analysis.sif")._command("/tmp/s.py", "/tmp/wd")
    assert "--network" not in cmd and "none" not in cmd            # network allowed → no isolation flags

    isolated = CodeSandbox(container_image="/img/analysis.sif", allow_network=False)
    cmd2, _ = isolated._command("/tmp/s.py", "/tmp/wd")
    assert "--net" in cmd2 and "--network" in cmd2 and "none" in cmd2   # explicit off → isolated
