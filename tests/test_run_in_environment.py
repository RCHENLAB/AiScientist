"""A skill can declare the environment its commands run in, and ``run_in_environment`` runs them.

Jin Li's CellQC skill needed an R/Snakemake stack. Until now that meant writing a wrapper tool
(run_cellqc) with an ``image:`` line; a skill could only be read, not run. Now a SKILL.md carries the
same ``image:`` declaration, the platform builds it once for the lab, and the agent runs the skill's
commands inside it as a Slurm job. These tests drive the whole path offline against a fake HPC3."""
from __future__ import annotations

from pathlib import Path

import pytest

from aiscientist.agents import skills as sk
from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor
from aiscientist.gateway.tool_images import build_command, image_sif_name, lock_path

CELLQC = "bioconda:cellqc=0.3.6 r-doubletfinder python=3.12"


def _skill(root: Path, name: str, front: str, body: str = "Run the pipeline.\n",
           files: "dict[str, str] | None" = None) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\n{front}---\n\n{body}")
    for rel, text in (files or {}).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text)
    return folder


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "cellqc-standalone",
           "description: Run CellQC by hand.\nmetadata:\n  category: single-cell\n"
           f'  image: "{CELLQC}"\n  cpus: "16"\n  mem_gb: "80"\n  time_limit: "08:00:00"\n',
           files={"scripts/stage.sh": "echo stage\n"})
    _skill(root, "top-level-env", f'description: Declared at the top.\nimage: "{CELLQC}"\n')
    _skill(root, "floating-tag", 'description: Unpinned.\nimage: "docker://x/y:latest"\n')
    _skill(root, "resources-only", 'description: No image.\nmetadata:\n  cpus: "8"\n')
    _skill(root, "plain", "description: Just advice.\n")
    monkeypatch.setenv("AISCIENTIST_SKILLS_DIR", str(root))
    monkeypatch.setenv("AISCIENTIST_INDUCED_SKILLS_DIR", str(tmp_path / "none"))
    sk.refresh_skills()
    yield root
    monkeypatch.undo()
    sk.refresh_skills()


def test_a_skill_declares_its_environment_in_metadata_or_at_the_top(library):
    s = sk.SKILLS["cellqc-standalone"]
    assert (s.image, s.cpus, s.mem_gb, s.time_limit) == (CELLQC, 16, 80, "08:00:00")
    assert sk.SKILLS["top-level-env"].image == CELLQC
    assert sk.SKILLS["plain"].image == "" and sk.SKILLS["plain"].env_problem == ""


def test_an_unacceptable_declaration_is_dropped_with_the_reason(library):
    assert sk.SKILLS["floating-tag"].image == ""
    assert "latest" in sk.SKILLS["floating-tag"].env_problem
    assert "no image" in sk.SKILLS["resources-only"].env_problem


def test_the_manifest_says_which_skills_run_in_their_own_environment(library):
    lines = sk.skill_manifest().splitlines()
    assert any("cellqc-standalone" in ln and "run_in_environment" in ln for ln in lines)
    assert not any("- plain" in ln and "run_in_environment" in ln for ln in lines)


# --- the tool --------------------------------------------------------------------------------

class _Runner:
    def __init__(self):
        self.calls: list[tuple] = []

    def run_in_image(self, command, image, **kw):
        self.calls.append((command, image, kw))
        return {"status": "ok", "returncode": 0, "stdout": "done"}


def _tool(executor):
    from aiscientist.agents.research_lab import make_run_in_environment_tool
    return make_run_in_environment_tool(executor)


def test_without_an_image_runner_the_tool_is_not_offered(library):
    assert _tool(object()) is None
    from aiscientist.agents.registry import build_scientist_catalog
    assert "run_in_environment" not in {t.name for t in build_scientist_catalog()}
    assert "run_in_environment" in {t.name for t in build_scientist_catalog(code_executor=_Runner())}


def test_the_skill_s_declared_image_and_resources_are_what_runs(library):
    runner = _Runner()
    tool = _tool(runner)
    assert "cellqc-standalone: " + CELLQC in tool.description
    out = tool.executor({"skill": "CellQC-Standalone", "command": "cellqc --version"}, None)
    assert out["status"] == "ok" and out["skill"] == "cellqc-standalone"
    assert out["provenance"]["image"] == CELLQC
    command, image, kw = runner.calls[0]
    assert (command, image) == ("cellqc --version", CELLQC)
    assert (kw["cpus"], kw["mem_gb"], kw["time_limit"]) == (16, 80, "08:00:00")
    assert kw["workdir"] == "env/cellqc-standalone"
    assert kw["files"] == {"scripts/stage.sh": str(library / "cellqc-standalone" / "scripts/stage.sh")}


def test_the_agent_cannot_run_a_skill_without_an_environment_or_an_unknown_one(library):
    tool = _tool(_Runner())
    out = tool.executor({"skill": "plain", "command": "ls"}, None)
    assert out["status"] == "error" and "declares no environment" in out["error"]
    assert "cellqc-standalone" in out["error"]                     # names the ones that can
    out = tool.executor({"skill": "floating-tag", "command": "ls"}, None)
    assert "latest" in out["error"]                                # says why its image was refused
    out = tool.executor({"skill": "nope", "command": "ls"}, None)
    assert out["status"] == "error" and "no skill named" in out["error"]


def test_a_step_naming_the_tool_is_that_tool_s_step():
    from aiscientist.agents.research_lab import _primary_tool
    step = ("**CellQC by the skill** — Follow qc-snrna-with-cellqc-standalone with "
            "`run_in_environment`, then hand the merged checkpoint to `run_clustering`.")
    assert _primary_tool(step) == "run_in_environment"


# --- the executor ------------------------------------------------------------------------------

class _HPC:
    """A fake HPC3: records every command; images exist unless listed in ``missing``."""

    def __init__(self, missing: "set[str] | None" = None):
        self.host, self.username = "hpc3-mock", "tester"
        self.commands: list[str] = []
        self.puts: list[tuple[str, str]] = []
        self.missing = set(missing or ())
        self._next = 900

    def exec(self, command, timeout=60.0):
        self.commands.append(command)
        cmd = command.strip()
        ok = lambda out="": ExecResult(command, 0, out, "")      # noqa: E731
        if cmd.startswith("test -s "):
            path = cmd.split()[2].strip("'")
            if path in self.missing:
                self.missing.discard(path)                       # "built" by the provisioning job
                return ExecResult(command, 1, "", "")
            return ok()
        if cmd.startswith("sbatch"):
            self._next += 1
            return ok(f"Submitted batch job {self._next}")
        if cmd.startswith("sacct"):
            return ok("COMPLETED")
        return ok()

    def read_bytes(self, path, max_bytes=None):
        if path.endswith(".rc"):
            return b"0\n"
        if path.endswith(".out"):
            return b"cellqc, version 0.3.6\n"
        return b""

    def remote_size(self, path):
        return 0

    def put_file(self, local, remote):
        self.puts.append((local, remote))

    def get_file(self, *a, **k):
        pass


def _executor(hpc, **kw):
    return SlurmCodeExecutor(
        remote=hpc, container_image="/dfs/containers/analysis.sif",
        dataset_path="/dfs/up/lib/filtered_feature_bc_matrix.h5", dataset_root="/dfs/up/lib",
        work_dir="/dfs/run/work", artifacts_dir="/dfs/run/art", scratch_dir="/dfs/scratch",
        images_dir="/dfs/containers", pkgs_cache_dir="/dfs/conda-pkgs", cpus=8, mem_gb=64,
        startup_timeout_s=5, run_timeout_s=5, **kw)


def test_the_command_runs_in_the_skill_image_with_the_run_s_data(tmp_path):
    hpc = _HPC()
    stage = tmp_path / "stage.sh"
    stage.write_text("echo stage\n")
    out = _executor(hpc).run_in_image("cellqc --version", CELLQC, workdir="env/cellqc",
                                      cpus=16, mem_gb=80, time_limit="08:00:00",
                                      files={"scripts/stage.sh": str(stage)})
    assert out["status"] == "ok" and out["execution_mode"] == "hpc_slurm_image"
    sif = f"/dfs/containers/{image_sif_name(CELLQC)}"
    assert out["environment"] == {"image": CELLQC, "sif": sif, "workdir": "/dfs/run/work/env/cellqc"}
    script = "\n".join(hpc.commands)
    assert f"{sif} bash -lc" in script                                  # the skill's image
    assert "analysis.sif bash -lc" not in script                       # not the analysis image
    assert "-B /dfs/up/lib:/dfs/up/lib:ro" in script                   # the delivery, read-only
    for needle in ("--cpus-per-task=16", "--mem=80G", "--time=08:00:00",
                   "AISCIENTIST_ENV_DIR=/dfs/run/work/env/cellqc", "AISCIENTIST_JOB_CPUS=16",
                   "HOME=/dfs/run/work/env/cellqc/.home", "cd /dfs/run/work/env/cellqc",
                   "AISCIENTIST_DATASET_ROOT=/dfs/up/lib"):
        assert needle in script, needle
    assert "bash /dfs/scratch/snippet_" in script and ".sh >" in script
    assert hpc.puts == [(str(stage), "/dfs/run/work/env/cellqc/.skill/scripts/stage.sh")]
    assert "sitecustomize" not in script                                # no Python package cache


def test_the_first_use_builds_the_image_from_shared_downloads():
    sif = f"/dfs/containers/{image_sif_name(CELLQC)}"
    hpc = _HPC(missing={sif})
    out = _executor(hpc).run_in_image("cellqc --version", CELLQC, workdir="env/cellqc")
    assert out["status"] == "ok"
    script = "\n".join(hpc.commands)
    assert "micromamba install" in script and "/dfs/conda-pkgs:/aisci-shared-pkgs:ro" in script
    assert script.index("micromamba install") < script.index("cellqc --version")


def test_without_a_containers_dir_it_says_so():
    hpc = _HPC()
    out = SlurmCodeExecutor(remote=hpc, container_image="x.sif", work_dir="/w").run_in_image(
        "ls", CELLQC, workdir="env/x")
    assert out["status"] == "not_enabled"


def test_a_build_reuses_the_lab_s_downloads_and_leaves_a_lock_file():
    cmd = build_command(["cellqc=0.3.6", "python=3.12"], "/dfs/c/x.sif", pkgs_cache="/dfs/conda-pkgs")
    assert "CONDA_PKGS_DIRS=/aisci-pkgs,/aisci-shared-pkgs" in cmd
    assert "-B /dfs/conda-pkgs:/aisci-shared-pkgs:ro" in cmd           # read-only while building
    assert "rsync -a --ignore-existing" in cmd and "/dfs/conda-pkgs/" in cmd
    assert "env export -n base --explicit" in cmd and lock_path("/dfs/c/x.sif") in cmd
    plain = build_command(["cellqc=0.3.6"], "/dfs/c/x.sif")
    assert "aisci-shared-pkgs:ro" not in plain and "rsync" not in plain
    # The download cache does not change what the image IS, so the image keeps its name.
    assert image_sif_name("bioconda:cellqc=0.3.6") == image_sif_name("bioconda:cellqc=0.3.6")
