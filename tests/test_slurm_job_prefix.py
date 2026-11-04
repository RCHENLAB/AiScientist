"""AISCIENTIST_SLURM_JOB_PREFIX: one deployment's Slurm jobs, invisible to another deployment.

Production and the staging site both submit under each user's own HPC3 account. Empty (production)
keeps every job name and every match byte for byte what it has always been: running jobs are found
by name, so those names are pinned here. With a prefix (the staging site's ``stg-``) every name the
gateway creates starts with it and every lookup matches only names that do, so neither site reuses,
re-models, cancels or idle-releases the other's jobs, even for the same person. Offline: a fake
cluster answers ``squeue`` the way Slurm does (``--name`` is an exact match, ``--me`` is every job of
the user).
"""

from __future__ import annotations

import ast
import re
import types
from pathlib import Path

import pytest

from aiscientist.gateway import gpu, hpc_gc, hpc_originals, shared_llm, storage_delete, worker
from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.scgpt_job import scgpt_job_name
from aiscientist.gateway.settings import HPCSettings, slurm_job_name, slurm_job_prefix
from aiscientist.gateway.shared_llm import SharedLLMSettings
from aiscientist.gateway.slurm_analysis import SlurmAnalysisExecutor
from aiscientist.gateway.slurm_report import SlurmReportRenderer
from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor
from aiscientist.gateway.vlreview_job import vlreview_job_name

ENV = "AISCIENTIST_SLURM_JOB_PREFIX"
GATEWAY = Path(__file__).resolve().parents[1] / "src" / "aiscientist" / "gateway"

# The session-job prefixes production has always matched (the Stop GPU / free-session list).
TODAYS_SESSION_PREFIXES = (
    ("gpu", "bioagent-vllm-"),
    ("gpu", "bioagent-scgpt-"),
    ("worker", "aiscientist-worker-"),
    ("analysis", "bioagent_analysis_"),
    ("analysis", "bioagent_paperqa_"),
    ("gpu", "bioagent_corpusscan_"),
    ("analysis", "bioagent_variant_"),
    ("analysis", "bioagent_phenotype_"),
)

# One HPC3 user, connected to both sites, with a job of their own besides.
JOBS = [
    ("101", "bioagent-vllm-alice", "gpu-1", "10:00"),            # production's model server
    ("102", "aiscientist-worker-alice", "cpu-1", "30:00"),       # production's CPU worker
    ("103", "bioagent_analysis_run_qc_1", "cpu-2", "1:00"),      # production's analysis job
    ("201", "stg-bioagent-vllm-alice", "gpu-2", "5:00"),         # the staging site's
    ("202", "stg-aiscientist-worker-alice", "cpu-3", "20:00"),
    ("203", "stg-bioagent_analysis_run_qc_1", "cpu-4", "2:00"),
    ("301", "my_own_alignment", "cpu-5", "3:00:00"),             # the researcher's own job
]
PRODUCTION, STAGING = {"101", "102", "103"}, {"201", "202", "203"}


@pytest.fixture
def gw():
    return pytest.importorskip("aiscientist.gateway.app")


def _set_prefix(monkeypatch, prefix: str) -> None:
    if prefix:
        monkeypatch.setenv(ENV, prefix)
    else:
        monkeypatch.delenv(ENV, raising=False)


@pytest.fixture
def production(monkeypatch):
    _set_prefix(monkeypatch, "")


@pytest.fixture
def staging(monkeypatch):
    _set_prefix(monkeypatch, "stg-")


def _ok(out: str = "") -> ExecResult:
    return ExecResult("", 0, out, "")


class FakeCluster:
    """``squeue --me`` over ``JOBS``; ``scancel`` removes the jobs it names."""

    host, username = "hpc3.rcic.uci.edu", "alice"

    def __init__(self, jobs=JOBS):
        self.jobs = list(jobs)
        self.cmds: list[str] = []

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.cmds.append(command)
        if command.startswith("squeue --me"):
            name = re.search(r"--name=(\S+)", command)
            rows = [j for j in self.jobs if name is None or j[1] == name.group(1)]
            fmt = re.search(r"--format='?([^' ]+)", command).group(1)
            return _ok("\n".join(self._row(fmt, j) for j in rows))
        if command.startswith("scancel "):
            gone = set(command.split()[1:])
            self.jobs = [j for j in self.jobs if j[0] not in gone]
            return _ok()
        if command.startswith("sbatch "):
            return _ok("Submitted batch job 999")
        if command.startswith("date +%s"):
            return _ok("1700000000\n")                # no heartbeat files: holders look stale
        port = re.match(r"cat \$HOME/\.bioagent/vllm\.(\d+)\.port", command)
        if port:
            return _ok(f"3{port.group(1)}")
        return _ok()

    @staticmethod
    def _row(fmt: str, job: tuple[str, str, str, str]) -> str:
        jid, name, node, elapsed = job
        fields = {"i": jid, "j": name, "N": node, "M": elapsed, "u": "alice", "t": "R", "b": "gpu",
                  "k": "model=" + HPCSettings().serving_model()}
        return re.sub(r"%(\w)", lambda m: fields[m.group(1)], fmt)

    def scancelled(self) -> set[str]:
        return {jid for c in self.cmds if c.startswith("scancel ") for jid in c.split()[1:]}


def _conn(cluster: FakeCluster) -> types.SimpleNamespace:
    return types.SimpleNamespace(executor=cluster, alloc=None, worker_alloc=None, owner="alice",
                                 settings=HPCSettings(cpu_account="lab_cpu"), emit=lambda *a, **k: None)


# --- production: every name and every match unchanged -----------------------------------------------

def test_empty_prefix_keeps_todays_job_names(production, gw):
    assert slurm_job_prefix() == ""
    assert gpu.job_name("alice") == "bioagent-vllm-alice"
    assert worker.job_name("alice") == "aiscientist-worker-alice"
    assert scgpt_job_name("alice") == "bioagent-scgpt-alice"
    assert vlreview_job_name("alice") == "bioagent-vlreview-alice"
    assert shared_llm.job_name() == "aiscientist-llm-shared"
    assert shared_llm.remote_dir() == "$HOME/.bioagent/shared-llm"
    spec = storage_delete.job_spec("true", "alice", "/l.log", "/s", partition="standard")
    assert spec.job_name == "aiscientist-delete-alice" and "#SBATCH --job-name=aiscientist-delete-alice\n" in spec.script
    spec = hpc_originals.job_spec("true", "alice", "/l.log", "/s", partition="standard")
    assert spec.job_name == "aiscientist-originals-alice"
    assert "#SBATCH --job-name=aiscientist-originals-alice\n" in spec.script
    assert hpc_gc.submit_command("true", "/l", "alice", partition="standard", account="").startswith(
        "sbatch --job-name=aiscientist-tempgc-alice --partition=standard ")
    assert gw._session_job_prefixes() == TODAYS_SESSION_PREFIXES


def test_empty_prefix_keeps_todays_scripts_and_queries(production):
    assert "#SBATCH --job-name=bioagent-vllm-alice\n" in gpu._serve_script(HPCSettings(), "alice")
    script = shared_llm.serve_script(SharedLLMSettings(enabled=True, image="/img.sif"), HPCSettings(),
                                     "/home/alice")
    assert "#SBATCH --job-name=aiscientist-llm-shared\n" in script
    assert "#SBATCH --output=/home/alice/.bioagent/shared-llm/serve-%j.log\n" in script
    assert 'export SHARED_DIR="$HOME/.bioagent/shared-llm"\n' in script
    assert shared_llm.list_command().startswith(
        'D="$HOME/.bioagent/shared-llm"; echo "HOME|$HOME"; '
        "squeue --me --name=aiscientist-llm-shared --states=R,PD,CF --noheader ")

    cluster = FakeCluster()
    gpu.find_running_job(cluster, HPCSettings())
    worker.find_running_worker(cluster, "alice")
    assert cluster.cmds[0] == ("squeue --me --name=bioagent-vllm-alice --states=R --noheader "
                               "--format='%i|%u|%t|%N|%b|%M|%k'")
    assert cluster.cmds[-1] == "squeue --me --name=aiscientist-worker-alice --states=R --noheader --format='%i|%N'"


# --- a prefix: every name the gateway creates carries it ------------------------------------------

def test_prefix_starts_every_created_name(staging, gw):
    names = [
        gpu.job_name("alice"), worker.job_name("alice"), scgpt_job_name("alice"),
        vlreview_job_name("alice"), shared_llm.job_name(),
        storage_delete.job_spec("true", "alice", "/l.log", "/s", partition="standard").job_name,
        hpc_originals.job_spec("true", "alice", "/l.log", "/s", partition="standard").job_name,
    ]
    assert names == ["stg-bioagent-vllm-alice", "stg-aiscientist-worker-alice", "stg-bioagent-scgpt-alice",
                     "stg-bioagent-vlreview-alice", "stg-aiscientist-llm-shared",
                     "stg-aiscientist-delete-alice", "stg-aiscientist-originals-alice"]
    assert "#SBATCH --job-name=stg-bioagent-vllm-alice\n" in gpu._serve_script(HPCSettings(), "alice")
    for name in ("tempgc", "sessionclean"):
        assert f"--job-name=stg-aiscientist-{name}-alice " in hpc_gc.submit_command(
            "true", "/l", "alice", partition="standard", account="", name=name)
    # The srun steps on a compute node.
    cluster = FakeCluster()
    gw._remote_sha256(_conn(cluster), "/dfs/a.h5")
    assert " -J stg-aiscientist-verify-alice sha256sum " in cluster.cmds[-1]
    # No device numbers from the check: the move is taken as one across filesystems, a Slurm step.
    gw._move_on_hpc(_conn(cluster), "/dfs/a.h5ad", "/other/a.h5ad")
    assert cluster.cmds[-1].startswith("srun ")
    assert " --job-name=stg-aiscientist_save_result " in cluster.cmds[-1]


def test_prefix_gives_the_shared_model_server_its_own_job_and_folder(staging):
    script = shared_llm.serve_script(SharedLLMSettings(enabled=True, image="/img.sif"), HPCSettings(),
                                     "/home/alice")
    assert "#SBATCH --job-name=stg-aiscientist-llm-shared\n" in script
    assert "#SBATCH --output=/home/alice/.bioagent/stg-shared-llm/serve-%j.log\n" in script
    assert 'export SHARED_DIR="$HOME/.bioagent/stg-shared-llm"\n' in script
    assert "/shared-llm" not in script
    listing = shared_llm.list_command()
    assert 'D="$HOME/.bioagent/stg-shared-llm";' in listing
    assert "--name=stg-aiscientist-llm-shared " in listing


class BatchHPC:
    """Submits complete at once, so the executors' job names can be read off ``sbatch``."""

    host, username = "hpc3-mock", "alice"

    def __init__(self):
        self.cmds: list[str] = []
        self._next = 700

    def exec(self, command, timeout=60.0):
        cmd = command.strip()
        self.cmds.append(cmd)
        if cmd.startswith("echo "):
            return _ok(cmd[len("echo "):].replace("$HOME", "/home/alice"))
        if cmd.startswith("sbatch"):
            self._next += 1
            return _ok(f"Submitted batch job {self._next}")
        if cmd.startswith("sacct"):
            return _ok("COMPLETED")
        return _ok()

    def read_bytes(self, remote_path, max_bytes=None):
        if remote_path.endswith(".result.json"):
            return b'AISCIENTIST_RESULT_JSON {"status": "ok"}\n'
        return b"0\n" if remote_path.endswith(".rc") else b""

    def remote_size(self, remote_path):
        return len(self.read_bytes(remote_path))

    def put_file(self, *a, **k):
        pass

    def get_file(self, remote_path, local_path):
        Path(local_path).write_bytes(b"%PDF-1.4\n")

    def submitted(self) -> list[str]:
        """The job name of every submission: ``sbatch <submit dir>/<job name>.sbatch``."""
        names = [Path(c.split()[-1]).name.removesuffix(".sbatch") for c in self.cmds if c.startswith("sbatch")]
        for name in names:
            assert any(f"#SBATCH --job-name={name}\n" in c for c in self.cmds), name
        return names


def test_prefix_starts_the_analysis_run_code_and_report_job_names(staging, tmp_path):
    hpc = BatchHPC()
    SlurmAnalysisExecutor(remote=hpc, container_image="/a.sif", scratch_dir="/dfs/scratch/analysis",
                          remote_workspace="/dfs/run", remote_dataset="/dfs/ds.h5ad",
                          startup_timeout_s=5, run_timeout_s=5).run_tool("run_de", {}, ctx=None)
    SlurmCodeExecutor(remote=hpc, container_image="/a.sif", dataset_path="/dfs/ds.h5ad",
                      work_dir="/dfs/run/work", artifacts_dir="/dfs/run/art", scratch_dir="/dfs/scratch/code",
                      startup_timeout_s=5, run_timeout_s=5)("print(1)")
    bundle = tmp_path / "artifacts"
    (bundle / "report").mkdir(parents=True)
    (bundle / "report" / "report.md").write_text("# hi")
    out = bundle / "report" / "report.pdf"
    SlurmReportRenderer(remote=hpc, container_image="/r.sif", scratch_dir="/dfs/scratch/report",
                        remote_base="/dfs/reports", startup_timeout_s=5, run_timeout_s=5)(
        ["pandoc", str(bundle / "report" / "report.md"), "-o", str(out)], bundle, out, 60.0)
    names = hpc.submitted()
    assert len(names) == 3 and all(n.startswith("stg-bioagent_") for n in names), names
    assert names[0] == "stg-bioagent_analysis_run_de_1"


# --- lookups: a site only ever finds its own jobs ----------------------------------------------------

@pytest.mark.parametrize("prefix,serve,holder", [("", "101", "102"), ("stg-", "201", "202")])
def test_the_serve_job_reuse_query_and_the_worker_lookup_find_only_this_sites_jobs(
        monkeypatch, prefix, serve, holder):
    _set_prefix(monkeypatch, prefix)
    cluster = FakeCluster()
    alloc = gpu.find_running_job(cluster, HPCSettings())
    assert alloc is not None and alloc.job_id == serve
    assert f"--name={prefix}bioagent-vllm-alice " in cluster.cmds[0]
    found = worker.find_running_worker(cluster, "alice")
    assert found is not None and found.job_id == holder
    # Neither site sees a job of the other, even when it is the only one there.
    other = FakeCluster([j for j in JOBS if j[0] not in (serve, holder)])
    assert gpu.find_running_job(other, HPCSettings()) is None
    assert worker.find_running_worker(other, "alice") is None


@pytest.mark.parametrize("prefix,script", [("", "$HOME/.bioagent/serve.gpu.sbatch"),
                                           ("stg-", "$HOME/.bioagent/stg-serve.gpu.sbatch")])
def test_each_site_writes_and_submits_its_own_serve_script(monkeypatch, prefix, script):
    """Both sites write the serve script into the same user's ``~/.bioagent`` and submit it in a
    second command: a shared file could be submitted holding the other site's job."""
    _set_prefix(monkeypatch, prefix)
    cluster = FakeCluster()
    assert gpu._submit_candidate(cluster, HPCSettings(), gpu.GpuCandidate("gpu", "gpu:A100:1"),
                                 lambda *a: None, hard_fail=True) == "999"
    write, submit = cluster.cmds
    assert f"cat > {script} <<" in write and f"#SBATCH --job-name={prefix}bioagent-vllm-alice\n" in write
    assert submit == f"sbatch {script}"


@pytest.mark.parametrize("prefix,holder", [("", "102"), ("stg-", "202")])
def test_the_leftover_worker_sweep_cancels_only_this_sites_holder(monkeypatch, prefix, holder):
    _set_prefix(monkeypatch, prefix)
    cluster = FakeCluster()
    assert worker.sweep_stale_workers(cluster, log_dir="/dfs/jobs/worker", stale_after_s=60) == [holder]
    assert cluster.scancelled() == {holder}


@pytest.mark.parametrize("prefix", ["", "stg-"])
def test_the_stop_gpu_prefix_list_matches_only_this_sites_jobs(monkeypatch, gw, prefix):
    _set_prefix(monkeypatch, prefix)
    assert gw._session_job_prefixes() == tuple((k, prefix + p) for k, p in TODAYS_SESSION_PREFIXES)
    mine, theirs = ("stg-", "") if prefix else ("", "stg-")
    for kind, stem in TODAYS_SESSION_PREFIXES:
        assert gw._classify_session_job(f"{mine}{stem}alice") == kind
        assert gw._classify_session_job(f"{theirs}{stem}alice") is None
    assert gw._classify_session_job("my_own_alignment") is None


@pytest.mark.parametrize("prefix,ours", [("", PRODUCTION), ("stg-", STAGING)])
def test_free_my_session_cancels_only_this_sites_jobs(monkeypatch, gw, prefix, ours):
    _set_prefix(monkeypatch, prefix)
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    cluster = FakeCluster()
    released, error = gw._release_session_jobs(_conn(cluster))
    assert error is None
    assert {jid for jid, _name, _kind in released} == ours
    assert cluster.scancelled() == ours


@pytest.mark.parametrize("prefix", ["", "stg-"])
def test_the_busy_check_counts_only_this_sites_jobs(monkeypatch, gw, prefix):
    _set_prefix(monkeypatch, prefix)
    analysis = "203" if prefix else "103"
    assert gw._hpc_jobs_busy(_conn(FakeCluster())) is True
    # The other site's analysis job and both sites' model servers and workers do not count.
    assert gw._hpc_jobs_busy(_conn(FakeCluster([j for j in JOBS if j[0] != analysis]))) is False


# --- a bad prefix is refused, never dropped -----------------------------------------------------------

@pytest.mark.parametrize("value", ["stg prod", "stg;rm", "-stg", "a,b", "$HOME", "x" * 33])
def test_a_prefix_the_shell_or_slurm_could_misread_is_refused(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    with pytest.raises(ValueError, match=ENV):
        slurm_job_name("bioagent-vllm-alice")


def test_surrounding_spaces_are_ignored(monkeypatch):
    monkeypatch.setenv(ENV, "  stg-  ")
    assert gpu.job_name("alice") == "stg-bioagent-vllm-alice"
    monkeypatch.setenv(ENV, "   ")
    assert gpu.job_name("alice") == "bioagent-vllm-alice"


def test_the_gateway_does_not_start_with_a_bad_prefix(monkeypatch, gw, capsys):
    monkeypatch.setattr(gw, "load_project_env", lambda root: {})
    monkeypatch.setenv(ENV, "stg prod")
    assert gw.main([]) == 2
    assert ENV in capsys.readouterr().err


# --- a later job cannot forget the prefix ------------------------------------------------------------

_LITERAL_NAME = re.compile(r"(--job-name=|(?:^|\s)-J |--name=)[A-Za-z0-9]")


def _strings(tree: ast.AST):
    """Every string constant in ``tree`` except docstrings and other bare string statements."""
    bare = {id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in bare:
            yield node


def test_no_gateway_code_writes_a_job_name_by_hand():
    """A Slurm job name or ``squeue --name`` written as a literal would skip the prefix: every one
    goes through ``slurm_job_name`` (or a helper built on it), which this cannot see past."""
    offenders = []
    for path in sorted(GATEWAY.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _strings(tree):
            if _LITERAL_NAME.search(node.value):
                offenders.append(f"{path.name}:{node.lineno}: {node.value.strip()[:80]}")
        for node in ast.walk(tree):
            if (isinstance(node, ast.keyword) and node.arg == "job_name"
                    and isinstance(node.value, (ast.Constant, ast.JoinedStr))):
                offenders.append(f"{path.name}:{node.value.lineno}: job_name= given a literal")
    assert not offenders, "\n".join(offenders)
