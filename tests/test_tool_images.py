"""A tool can declare the container its HPC3 job runs in (``image:``) and its own Slurm resources in
TOOL.md, and the platform provisions that image on first use — pulled (``docker://``) or built
from conda packages (``bioconda:``) without root. That is how a tool that needs R, Bioconductor or a
pinned Python is added without anyone installing anything."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.slurm_analysis import SlurmAnalysisExecutor
from aiscientist.gateway.tool_images import (
    BUILD_BASE,
    build_command,
    image_sif_name,
    provision_command,
    provision_resources,
    pull_command,
)
from aiscientist.agents import tool_skills
from aiscientist.tools import catalog
from aiscientist.tools.catalog import ManifestError, load_manifest


def _manifest(tmp_path: Path, extra: str, runs_on: str = "hpc:analysis") -> Path:
    folder = tmp_path / "demo_tool"
    folder.mkdir(exist_ok=True)
    (folder / "TOOL.md").write_text(
        f"---\nname: demo_tool\nsummary: demo\ncategory: qc\nruns_on: {runs_on}\norder: 999\n"
        f"{extra}---\n# demo\n", encoding="utf-8")
    return folder


# --- the manifest fields -------------------------------------------------------------------------

def test_image_and_resources_are_read_from_the_manifest(tmp_path):
    m = load_manifest(_manifest(tmp_path, 'image: "docker://quay.io/biocontainers/x:1.2--py_0"\n'
                                          'cpus: 16\nmem_gb: 80\ntime_limit: "08:00:00"\n'))
    assert (m.image, m.cpus, m.mem_gb, m.time_limit) == (
        "docker://quay.io/biocontainers/x:1.2--py_0", 16, 80, "08:00:00")


def test_a_tool_without_them_keeps_the_lines_defaults(tmp_path):
    m = load_manifest(_manifest(tmp_path, ""))
    assert (m.image, m.cpus, m.mem_gb, m.time_limit) == ("", 0, 0, "")


@pytest.mark.parametrize("value, problem", [
    ('"docker://quay.io/biocontainers/x:latest"', "latest"),
    ('"docker://quay.io/biocontainers/x"', "pinned"),
    ('"bioconda:cellqc python=3.12"', "must be pinned"),
    ('"bioconda:"', "conda package specs"),
    ('"ftp://example.org/x.sif"', "docker://"),
])
def test_an_unpinned_or_unknown_image_is_refused(tmp_path, value, problem):
    with pytest.raises(ManifestError, match=problem):
        load_manifest(_manifest(tmp_path, f"image: {value}\n"))


def test_a_bioconda_recipe_is_accepted(tmp_path):
    m = load_manifest(_manifest(tmp_path, 'image: "bioconda:cellqc=0.3.6 r-doubletfinder>=2.0.6 '
                                          'python=3.12"\n'))
    assert m.image.startswith("bioconda:cellqc=0.3.6")


def test_job_fields_belong_to_hpc_tools_only(tmp_path):
    with pytest.raises(ManifestError, match="hpc"):
        load_manifest(_manifest(tmp_path, "cpus: 4\n", runs_on="inprocess"))


@pytest.mark.parametrize("extra", ["cpus: 0\n", "mem_gb: lots\n", 'time_limit: "8h"\n'])
def test_bad_resources_are_refused(tmp_path, extra):
    with pytest.raises(ManifestError):
        load_manifest(_manifest(tmp_path, extra))


# --- naming and the provisioning shell ------------------------------------------------------------

def test_image_file_names():
    assert image_sif_name("docker://quay.io/biocontainers/cellqc:0.3.6--pyhdfd78af_1") == \
        "cellqc_0.3.6--pyhdfd78af_1.sif"
    digest = "docker://quay.io/x/tool@sha256:" + "ab" * 32
    assert image_sif_name(digest) == "tool_sha256-" + "ab" * 8 + ".sif"
    a = image_sif_name("bioconda:cellqc=0.3.6 python=3.12")
    b = image_sif_name("bioconda:cellqc=0.3.6 python=3.13")
    assert a.startswith("cellqc-0.3.6_") and a.endswith(".sif") and a != b   # a new recipe = a new image
    assert a == image_sif_name("bioconda:cellqc=0.3.6  python=3.12")        # whitespace is not a change


def test_a_pull_publishes_atomically_and_keeps_the_layer_cache_off_dfs3b():
    cmd = pull_command("docker://quay.io/biocontainers/x:1", "/dfs/c/x_1.sif")
    assert "pull --name" in cmd and 'mv -n "/dfs/c/x_1.sif.partial.$SLURM_JOB_ID"' in cmd
    assert "APPTAINER_CACHEDIR=\"${TMPDIR:-/tmp}" in cmd and "test -s /dfs/c/x_1.sif" in cmd


def test_a_build_needs_no_root():
    cmd = build_command(["cellqc=0.3.6", "python=3.12"], "/dfs/c/cellqc.sif")
    assert f"build --sandbox \"$W/box\" {BUILD_BASE}" in cmd
    assert "exec --writable --containall --no-mount bind-paths" in cmd       # HPC3's /data bind
    assert "-c conda-forge -c bioconda" in cmd and "cellqc=0.3.6" in cmd
    assert "aiscientist-packages.txt" in cmd                                  # provenance in the image
    assert "90-aiscientist-conda.sh" in cmd                                   # the env on PATH at exec
    assert "--fakeroot" not in cmd and "sudo" not in cmd
    assert 'mv -n "/dfs/c/cellqc.sif.partial.$SLURM_JOB_ID" /dfs/c/cellqc.sif' in cmd


def test_provisioning_dispatches_on_the_reference():
    assert "pull --name" in provision_command("docker://q/x:1", "/s.sif")
    assert "build --sandbox" in provision_command("bioconda:x=1", "/s.sif")
    assert provision_resources("bioconda:x=1")[0] > provision_resources("docker://q/x:1")[0]


# --- the executor ----------------------------------------------------------------------------------

class FakeHPC:
    """Jobs complete at once; ``test -s`` answers from ``present``; an image job makes it present."""

    def __init__(self, present: bool = True):
        self.present = present
        self.cmds: list[str] = []
        self._jid = 900

    def _r(self, out="", status=0):
        return ExecResult(command="", exit_status=status, stdout=out, stderr="")

    def exec(self, command, timeout=60.0):
        cmd = command.strip()
        self.cmds.append(cmd)
        if cmd.startswith("test -s"):
            return self._r(status=0 if self.present else 1)
        if cmd.startswith("echo "):
            return self._r(cmd[5:])
        if cmd.startswith("sbatch"):
            self._jid += 1
            if "_image_" in cmd:
                self.present = True
            return self._r(f"Submitted batch job {self._jid}")
        if cmd.startswith("sacct"):
            return self._r("COMPLETED")
        return self._r()

    def read_bytes(self, path, max_bytes=None):
        if path.endswith(".result.json"):
            return b'AISCIENTIST_RESULT_JSON {"status": "ok"}\n'
        return b""

    def get_file(self, *a, **k): pass


def _executor(hpc, **kw):
    return SlurmAnalysisExecutor(
        remote=hpc, container_image="/dfs/c/analysis.sif", remote_workspace="/dfs/run",
        remote_dataset="/dfs/up/cohort/S1/filtered_feature_bc_matrix.h5", scratch_dir="/dfs/scratch",
        source_dir="/dfs/pysrc", deps_dir="/dfs/pydeps", images_dir="/dfs/c",
        startup_timeout_s=5, run_timeout_s=5, cpus=8, mem_gb=64, time_limit="01:00:00", **kw)


def _sbatch_scripts(hpc) -> str:
    return "\n".join(hpc.cmds)


def test_a_tool_with_its_own_image_runs_in_it_with_its_resources():
    m = tool_skills.job_environment(catalog.manifest("run_cellqc"))   # from the skill it names
    hpc = FakeHPC(present=True)
    out = _executor(hpc, dataset_root="/dfs/up/cohort").run_tool("run_cellqc", {}, ctx=None)
    assert out["status"] == "ok"
    joined = _sbatch_scripts(hpc)
    assert f"/dfs/c/{image_sif_name(m['image'])}" in joined
    assert f"--cpus-per-task={m['cpus']}" in joined and f"--mem={m['mem_gb']}G" in joined
    assert f"--time={m['time_limit']}" in joined
    assert "--dataset-root /dfs/up/cohort" in joined
    assert "-B /dfs/up/cohort:/dfs/up/cohort:ro" in joined
    assert "/dfs/pydeps" not in joined          # analysis.sif's extra deps stay out of its own image
    assert f"AISCIENTIST_JOB_CPUS={m['cpus']}" in joined
    assert not any("_image_" in c for c in hpc.cmds if c.startswith("sbatch"))   # already there


def test_the_image_is_provisioned_once_on_first_use():
    hpc = FakeHPC(present=False)
    ex = _executor(hpc, dataset_root="/dfs/up/cohort")
    ex.run_tool("run_cellqc", {}, ctx=None)
    ex.run_tool("run_cellqc", {}, ctx=None)
    image_jobs = [c for c in hpc.cmds if c.startswith("sbatch") and "_image_" in c]
    assert len(image_jobs) == 1
    assert any("build --sandbox" in c for c in hpc.cmds)   # run_cellqc's image is a bioconda: build


def test_a_failed_provisioning_is_an_error_not_a_run_in_the_wrong_image():
    class NeverThere(FakeHPC):
        def exec(self, command, timeout=60.0):
            out = super().exec(command, timeout)
            self.present = False
            return out

    hpc = NeverThere(present=False)
    out = _executor(hpc).run_tool("run_cellqc", {}, ctx=None)
    assert out["status"] == "error" and "could not provision" in out["error"]
    assert not any("--tool run_cellqc" in c for c in hpc.cmds)


def test_line_tools_keep_the_line_image_resources_and_deps():
    hpc = FakeHPC()
    _executor(hpc).run_tool("run_clustering", {}, ctx=None)
    joined = _sbatch_scripts(hpc)
    assert "/dfs/c/analysis.sif" in joined and "--cpus-per-task=8" in joined
    assert "/dfs/pydeps" in joined and "--dataset-root" not in joined


def test_scrna_cli_hands_the_folder_root_to_the_tool(monkeypatch):
    from aiscientist.tools import scrna_cli
    seen = {}

    def fake_tools(only=None):
        seen["only"] = only
        return {"run_cellqc": lambda args, ctx: {"status": "ok", "root": ctx.decisions.get("dataset_root")}}

    monkeypatch.setattr(scrna_cli, "_analysis_tools", fake_tools)
    out = scrna_cli.run_tool("run_cellqc", "/ws", "/up/S1/f.h5", {}, dataset_root="/up/S1")
    assert out["root"] == "/up/S1" and seen["only"] == "run_cellqc"
    assert json.dumps(out)


def test_every_bound_folder_is_bound_and_handed_to_the_job():
    """Two Cell Ranger folders bound to one run (RCHENLAB/AiScientist issue 22): the job sees both,
    by name, and can read both inside its container."""
    hpc = FakeHPC(present=True)
    folders = ({"name": "Sample3_GSM5676874", "path": "/dfs/tmp/s/aaa/original"},
               {"name": "Sample1_WT", "path": "/dfs/tmp/s/bbb/original"})
    out = _executor(hpc, dataset_root="/dfs/tmp/s/aaa/original",
                    input_folders=folders).run_tool("run_cellqc", {}, ctx=None)
    assert out["status"] == "ok"
    joined = _sbatch_scripts(hpc)
    assert joined.count("-B /dfs/tmp/s/aaa/original:/dfs/tmp/s/aaa/original:ro") == 1   # bound once
    assert "-B /dfs/tmp/s/bbb/original:/dfs/tmp/s/bbb/original:ro" in joined
    assert "--input-folders" in joined and '"name": "Sample1_WT"' in joined


def test_scrna_cli_hands_every_bound_folder_to_the_tool(monkeypatch, capsys):
    from aiscientist.tools import scrna_cli

    monkeypatch.setattr(scrna_cli, "_analysis_tools", lambda only=None: {
        "run_cellqc": lambda args, ctx: {"status": "ok",
                                         "folders": ctx.decisions.get("input_folders"),
                                         "root": ctx.decisions.get("dataset_root")}})
    folders = [{"name": "Sample3_GSM5676874", "path": "/up/a"}, {"name": "Sample1_WT", "path": "/up/b"}]
    assert scrna_cli.main(["--tool", "run_cellqc", "--workspace", "/ws", "--dataset-root", "/up/a",
                           "--input-folders", json.dumps(folders)]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(scrna_cli.RESULT_MARKER))
    out = json.loads(line[len(scrna_cli.RESULT_MARKER):])
    assert out["folders"] == folders and out["root"] == "/up/a"


# -- a tool that runs a registry skill's method (TOOL.md ``skill:``, issue #42) -------------------

_CELLQC_SKILL = "qc-snrna-with-cellqc-standalone"


def _fake_skill(**kw):
    from aiscientist.agents.skills import Skill
    base = dict(name=_CELLQC_SKILL, summary="CellQC by hand.", image="bioconda:cellqc=0.3.6 python=3.12",
                cpus=4, mem_gb=16, time_limit="00:30:00", version="9.9.9", registry_release="v9.9.9",
                files={"assets/cellqc-defaults.json": '{"thresholds": {}}', "scripts/run.sh": "echo"})
    return Skill(**{**base, **kw})


def test_run_cellqc_names_its_skill_and_repeats_none_of_its_environment():
    from aiscientist.agents.skills import get_skill, refresh_skills
    m = catalog.manifest("run_cellqc")
    assert m.skill == _CELLQC_SKILL
    assert (m.image, m.cpus, m.mem_gb, m.time_limit) == ("", 0, 0, "")
    refresh_skills()
    s = get_skill(_CELLQC_SKILL)
    assert tool_skills.job_environment(m) == {"image": s.image, "cpus": s.cpus, "mem_gb": s.mem_gb,
                                          "time_limit": s.time_limit}


def test_a_manifest_cannot_name_a_skill_and_also_declare_its_environment(tmp_path):
    folder = tmp_path / "demo_tool"
    folder.mkdir()
    head = "---\nname: demo_tool\nsummary: A demo.\ncategory: qc\norder: 1\n"
    (folder / "TOOL.md").write_text(head + f"runs_on: hpc:analysis\nskill: {_CELLQC_SKILL}\ncpus: 4\n---\n")
    with pytest.raises(ManifestError, match="come from skill"):
        load_manifest(folder)
    (folder / "TOOL.md").write_text(head + f"runs_on: inprocess\nskill: {_CELLQC_SKILL}\n---\n")
    with pytest.raises(ManifestError, match="only apply to an hpc"):
        load_manifest(folder)


def test_changing_the_skill_changes_the_job_without_editing_the_tool(tmp_path, monkeypatch):
    from aiscientist.agents.research_harness import HarnessContext
    monkeypatch.setattr(tool_skills, "tool_skill", lambda m: _fake_skill())
    hpc = FakeHPC(present=True)
    ctx = HarnessContext(decisions={}, workspace=tmp_path)
    out = _executor(hpc, dataset_root="/dfs/up/cohort").run_tool("run_cellqc", {"min_counts": 700}, ctx=ctx)
    assert out["status"] == "ok"
    joined = _sbatch_scripts(hpc)
    assert f"/dfs/c/{image_sif_name('bioconda:cellqc=0.3.6 python=3.12')}" in joined
    assert "--cpus-per-task=4" in joined and "--mem=16G" in joined and "--time=00:30:00" in joined
    # The skill's identity and its assets/ travel with the job; its scripts do not.
    staged = next(c for c in hpc.cmds if ".args.json" in c and "cat >" in c)
    args = json.loads(staged.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert args["min_counts"] == 700
    assert args[catalog.SKILL_ARG]["version"] == "9.9.9"
    assert args[catalog.SKILL_ARG]["registry_release"] == "v9.9.9"
    assert list(args[catalog.SKILL_ARG]["files"]) == ["assets/cellqc-defaults.json"]
    # Recorded in the run's manifest.json, without becoming the run's active skill.
    doc = json.loads((tmp_path / "manifest.json").read_text())
    assert {"name": _CELLQC_SKILL, "version": "9.9.9", "registry_release": "v9.9.9"} in doc["skills"]
    assert ctx.decisions.get("active_registry_skill") is None


def test_a_tool_whose_skill_cannot_be_used_is_an_error_and_submits_nothing(monkeypatch):
    def missing(m):
        raise tool_skills.ToolSkillError(f"{m.name} runs skill {m.skill!r}, which is not installed")
    monkeypatch.setattr(tool_skills, "tool_skill", missing)
    hpc = FakeHPC(present=True)
    out = _executor(hpc, dataset_root="/dfs/up/cohort").run_tool("run_cellqc", {}, ctx=None)
    assert out["status"] == "error" and "not installed" in out["error"]
    assert not any(c.startswith("sbatch") for c in hpc.cmds)


def test_plan_estimates_take_the_resources_from_the_skill(monkeypatch):
    from aiscientist.gateway import estimates
    monkeypatch.setattr(tool_skills, "tool_skill", lambda m: _fake_skill(cpus=12, mem_gb=48))
    steps = estimates.profile("run_cellqc")["steps"]
    assert steps and all(s["cpus"] == 12 and s["mem_gb"] == 48 for s in steps)
    assert all("minutes" in s for s in steps)             # the timings stay in resource_profile.json
