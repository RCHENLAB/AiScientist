"""Lab environments (hpc/environments.py): software the agent asks for when neither its image nor an
HPC3 module has it.

Offline. What is pinned here:

* a recipe is refused, with a message the model can act on, unless every package is pinned, every
  git source is a tag or a commit (never a branch), and images come from the allowed registries;
* the same recipe always names the same folder, so it is built once and reused;
* the build job installs only inside the micromamba container, with only the new environment and
  its own scratch writable, checks the result read-only with the network off, and publishes it in
  one ``mv -T``; it never builds a container image (RCIC: a definition-file build needs an admin);
* a new environment waits for the user's approval on the card, an existing one does not;
* ``environment=`` binds the environment read-only with its bin/ first on PATH in run_shell, run_code
  and run_in_environment, and run_code still runs the snippet on the image's own Python.

The build scripts were run for real on HPC3 on 2026-10-30 (jobs 58636253, 58636254 and later; see
docs/environments/README.md).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from aiscientist.gateway.executor import ExecResult
from aiscientist.hpc import environments as E
from aiscientist.hpc.environments import EnvRef, LabEnvironments, Recipe, RecipeError
from aiscientist.hpc.shell import HpcShell, HpcWorkspace, hpc_shell_catalog

STORE = "/dfs/lab/AiScientist/envs/lab"
SESSION = "/dfs/lab/AiScientist/sessions/s1"

CONDA = {"name": "Mosdepth", "conda": ["mosdepth=0.3.10"], "provides": ["mosdepth"],
         "reason": "per-base coverage for the QC step"}


def _recipe(**kw) -> Recipe:
    return Recipe.from_args({**CONDA, **kw})


# --- the recipe -----------------------------------------------------------------------------------


@pytest.mark.parametrize("bad, said", [
    ({"conda": ["mosdepth"]}, "not pinned"),
    ({"conda": ["mosdepth>=0.3"]}, "not pinned"),
    ({"conda": [], "pip": ["pyranges"]}, "not pinned"),
    ({"conda": [], "pip": ["git+https://github.com/a/b@main"]}, "moves"),
    ({"conda": [], "pip": ["git+https://example.org/a/b@v1"]}, "repositories on"),
    ({"conda": ["r-base=4.4"], "r": ["github:a/b@master"]}, "moves"),
    ({"conda": [], "r": ["bioc:limma"]}, "pinned R"),
    ({"conda": ["r-base=4.4"], "r": ["limma"]}, "cran:"),
    ({"conda": [], "image": "docker://evil.example/x:1"}, "can be pulled from"),
    ({"conda": [], "image": "docker://quay.io/biocontainers/x:latest"}, "latest"),
    ({"conda": [], "image": "docker://quay.io/biocontainers/x"}, "not a pinned image"),
    ({"image": "docker://quay.io/biocontainers/x:1"}, "not both"),
    ({"conda": []}, "empty"),
    ({"provides": []}, "provides"),
    ({"provides": ["R DESeq2"]}, "provides"),
    ({"reason": ""}, "reason"),
    ({"name": "!!"}, "name"),
])
def test_a_recipe_that_is_not_pinned_or_not_allowed_is_refused_with_how_to_fix_it(bad, said):
    with pytest.raises(RecipeError, match=said):
        _recipe(**bad)


def test_allowed_sources_are_accepted():
    r = Recipe.from_args({
        "name": "Mixed Stack", "conda": ["r-base=4.4", "python==3.11", "zlib=1.3.1=h4ab18f5_1"],
        "pip": ["pyranges==0.1.4", "git+https://github.com/tqdm/tqdm@v4.66.5",
                "scikit-bio @ git+https://github.com/biocore/scikit-bio@3f2a9c1#subdirectory=."],
        "r": ["cran:glue==1.8.0", "glue==1.8.0", "bioc:BiocGenerics", "github:r-lib/crayon@v1.5.3",
              "github:owner/repo/pkg@0123abc"],
        "script": "make install PREFIX=$PREFIX", "provides": ["R:crayon", "py:tqdm", "cmd:seqtk"],
        "verify": "mosdepth --version\nRscript -e 'library(glue)'", "reason": "x"})
    assert r.name == "mixed-stack" and r.kind == "conda"
    assert r.verify == ("mosdepth --version", "Rscript -e 'library(glue)'")
    assert Recipe.from_args({**CONDA, "conda": [], "image": "docker://quay.io/biocontainers/seqtk:1.4--h577a1d6_3"}).kind == "image"
    assert Recipe.from_args({**CONDA, "conda": [], "image": "docker://ghcr.io/o/r@sha256:" + "a" * 64}).kind == "image"


def test_the_same_recipe_names_the_same_folder_and_a_change_names_another():
    a = _recipe(conda=["mosdepth=0.3.10", "zlib=1.3.1"])
    b = _recipe(conda=["zlib=1.3.1", "mosdepth=0.3.10"], provides=["mosdepth", "cmd:x"], reason="other")
    c = _recipe(conda=["mosdepth=0.3.9", "zlib=1.3.1"])
    assert re.fullmatch(r"mosdepth-[0-9a-f]{12}", a.env_id)
    assert a.env_id == b.env_id                      # order, checks and reason do not change the content
    assert a.env_id != c.env_id


def test_the_build_adds_the_tools_the_recipe_s_parts_need():
    r = _recipe(conda=["r-base=4.4"], pip=["git+https://github.com/tqdm/tqdm@v4.66.5"],
                r=["bioc:limma"], script="make")
    specs = r.conda_specs()
    assert specs[0] == "r-base=4.4"
    for tool in ("python", "pip", "r-remotes", "r-biocmanager", "compilers", "make", "cmake", "git"):
        assert tool in specs
    assert _recipe().conda_specs() == ("mosdepth=0.3.10",)
    assert "python" not in _recipe(conda=["python=3.12"], pip=["a==1"]).conda_specs()[1:]


def test_the_card_lists_everything_that_will_be_installed():
    r = _recipe(conda=["r-base=4.4"], r=["github:r-lib/crayon@v1.5.3"], script="make install")
    text = r.card_detail(max_gb=10)
    for part in ("per-base coverage", "r-base=4.4", "github:r-lib/crayon@v1.5.3", "make install",
                 "build tools added", "shared read-only with the lab", "10 GB"):
        assert part in text


# --- the build job --------------------------------------------------------------------------------


def _bash_ok(script: str) -> None:
    if shutil.which("bash"):
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_the_build_job_installs_only_inside_the_container_and_publishes_once():
    r = _recipe(conda=["mosdepth=0.3.10", "python=3.11"], pip=["git+https://github.com/tqdm/tqdm@v4.66.5"],
                script="make install PREFIX=$PREFIX", provides=["mosdepth", "py:tqdm"])
    s = E.build_script(r, store=STORE, recipe_dir="/r", base_sif="/c/mm.sif", pkgs_cache="/dfs/lab/envs/conda-pkgs")
    _bash_ok(s)
    final = f"{STORE}/{r.env_id}"
    # Every install command runs in the micromamba image, sealed off from lab storage, building at the
    # final path (a conda prefix records its own path).
    assert f"FINAL={final}" in s
    assert '--containall --no-mount bind-paths' in s and '-B "$STAGE:$FINAL"' in s
    assert f'STAGE="{STORE}/.staging/{r.env_id}.$SLURM_JOB_ID"' in s
    assert "--override-channels -c conda-forge -c bioconda --file /aisci-recipe/conda.txt" in s
    assert "/aisci-shared-pkgs:ro" in s                           # the lab's downloads, read-only
    # The source build sees the compilers: the environment is activated, not `micromamba run`.
    assert "micromamba run" not in s
    assert "activate.d" in s and "bash -eo pipefail /aisci-recipe/build.sh" in s
    # Checked as a run sees it, then published in one rename; a finished one is never rebuilt.
    assert '--net --network none -B "$W/tmp:/tmp" -B "$STAGE:$FINAL:ro"' in s
    assert 'mv -T "$STAGE" "$FINAL"' in s
    assert s.index('record.json" ]; then echo "AISCIENTIST_ENV_READY') < s.index("phase solve")
    assert "10737418240" in s                                     # the size limit
    # Never a container build: RCIC requires an admin for definition-file builds.
    assert "apptainer build" not in s and " build --" not in s


def test_the_image_job_pulls_and_checks_without_network():
    r = Recipe.from_args({**CONDA, "conda": [], "image": "docker://quay.io/biocontainers/seqtk:1.4--h577a1d6_3",
                          "provides": ["seqtk"]})
    s = E.image_script(r, store=STORE, recipe_dir="/r", sif="/c/seqtk.sif")
    _bash_ok(s)
    assert "apptainer pull" in s and "apptainer build" not in s
    assert '--net --network none "$SIF"' in s and 'stat -c %s "$SIF"' in s


def test_r_packages_install_at_their_pinned_versions_and_fail_loudly():
    r = _recipe(conda=["r-base=4.4"], r=["cran:glue==1.8.0", "bioc:BiocGenerics", "github:r-lib/crayon@v1.5.3",
                                       "github:o/repo/sub@0123abc"])
    text = E.r_install_script(r)
    assert 'remotes::install_version("glue", version = "1.8.0"' in text
    assert 'BiocManager::install("BiocGenerics"' in text
    # GitHub archives, not the GitHub API (its hourly limit is shared by the whole cluster).
    assert 'install_url("https://github.com/r-lib/crayon/archive/v1.5.3.tar.gz"' in text
    assert 'subdir = "sub"' in text
    assert 'stop("not installed: "' in text


def test_checks_cover_commands_r_and_python():
    lines = "\n".join(E.check_lines(("mosdepth", "cmd:seqtk", "R:DESeq2", "py:pysam"), ("mosdepth --version",)))
    assert "command -v mosdepth" in lines and "command -v seqtk" in lines
    assert "library(DESeq2)" in lines and "import pysam" in lines and "mosdepth --version" in lines
    _bash_ok("check() { :; }\n" + lines.split("\n", 2)[2])


def test_activation_puts_the_first_environment_first_and_never_fails():
    act = E.activation(["/e/a", "/e/b"])
    assert act.startswith('export PATH=/e/a/bin:/e/b/bin:"$PATH"')
    assert "CONDA_PREFIX=/e/a" in act and "|| true" in act
    assert E.activation([]) == ""
    if shutil.which("bash"):
        out = subprocess.run(["bash", "-c", "set -euo pipefail; " + act + 'echo "$PATH"'],
                             capture_output=True, text=True)
        assert out.returncode == 0 and out.stdout.startswith("/e/a/bin:/e/b/bin:")


# --- the store ------------------------------------------------------------------------------------


class Remote:
    """Login-node fake: records commands, serves records from ``files`` for `cat` / the listing."""

    username = "alice"
    host = "hpc3"

    def __init__(self, files=None):
        self.files: dict[str, str] = dict(files or {})
        self.cmds: list[str] = []

    def exec(self, command, timeout=60.0):
        self.cmds.append(command)
        m = re.match(r"cat (\S+) 2>/dev/null$", command)
        if m:
            text = self.files.get(m.group(1).strip("'"))
            return ExecResult(command, 0 if text else 1, text or "", "")
        if "@@AISCIENTIST_RECORD@@" in command:
            out = "".join(f"@@AISCIENTIST_RECORD@@\n{t}\n" for p, t in self.files.items() if p.endswith("record.json"))
            return ExecResult(command, 0, out, "")
        return ExecResult(command, 0, "", "")


def _record(env_id: str, **kw) -> str:
    rec = {"id": env_id, "name": env_id.rsplit("-", 1)[0], "kind": "conda", "provides": ["mosdepth"],
           "recipe": {"conda": ["mosdepth=0.3.10"]}, "built_at": "2026-10-30T19:58:07Z", **kw}
    return json.dumps(rec)


def _rpath(env_id: str) -> str:
    return f"{STORE}/{env_id}/.aiscientist/record.json"


def _lab(remote=None, *, jobs=None, approval="ask", publish=True) -> LabEnvironments:
    remote = remote or Remote()
    jobs = jobs if jobs is not None else []

    def run_job(script, name, cpus, mem, limit):
        jobs.append({"script": script, "name": name, "cpus": cpus, "mem": mem, "limit": limit})
        m = re.search(r"^FINAL=(\S+)$", script, re.M)
        env_id = m.group(1).rsplit("/", 1)[1]
        if publish:
            remote.files[_rpath(env_id)] = _record(env_id)
            return {"state": "COMPLETED", "job_id": "42", "log": f"AISCIENTIST_ENV_PUBLISHED {m.group(1)}\n"}
        return {"state": "FAILED", "job_id": "43", "log": ("AISCIENTIST_ENV_PHASE verify\n"
                                                          "AISCIENTIST_ENV_CHECK missing mosdepth\n"
                                                          "AISCIENTIST_ENV_FAILED verify\n")}

    lab = LabEnvironments(remote=remote, store=STORE, run_job=run_job, base_image=lambda: "/c/mm.sif",
                          image_sif=lambda ref: "/c/img.sif", stage_dir=f"{SESSION}/runs/r1/jobs/environments/recipes",
                          pkgs_cache="/dfs/lab/AiScientist/envs/conda-pkgs", approval=approval,
                          requested_by="alice", account="Alice", run_id="r1", session_id="s1")
    lab.jobs = jobs  # type: ignore[attr-defined]
    return lab


def test_build_stages_the_recipe_runs_one_job_and_returns_the_id():
    lab = _lab()
    r = _recipe()
    out = lab.build(r, approved_by="Alice")
    assert out["status"] == "ready" and out["environment"] == r.env_id and out["reused"] is False
    assert "environment='" + r.env_id + "'" in out["how_to_use"]
    job = lab.jobs[0]
    assert (job["cpus"], job["mem"], job["limit"]) == (8, 32, "04:00:00")
    staged = " ".join(lab.remote.cmds)
    assert f"mkdir -p {SESSION}/runs/r1/jobs/environments/recipes/{r.env_id}" in staged
    assert f"chmod 3775 {STORE} {STORE}/.staging" in staged
    # The record template carries who asked and who approved; sizes are numbers once filled in.
    b64 = re.search(r"printf %s (\S+) \| base64 -d > \S+/record.json", staged).group(1)
    import base64
    text = base64.b64decode(b64.strip("'")).decode()
    assert '"size_bytes": __SIZE__' in text and '"approved_by": "Alice"' in text
    assert '"requested_by": "alice"' in text and '"run_id": "r1"' in text


def test_a_failed_build_publishes_nothing_and_says_which_check_failed():
    lab = _lab(publish=False)
    out = lab.build(_recipe(), approved_by="Alice")
    assert out["status"] == "failed" and out["phase"] == "verify"
    assert out["checks"] == ["AISCIENTIST_ENV_CHECK missing mosdepth"]
    assert "Nothing was published" in out["next"]


def test_records_find_and_resolve():
    remote = Remote({_rpath("mosdepth-0123456789ab"): _record("mosdepth-0123456789ab"),
                     _rpath("deseq2-r44-aaaaaaaaaaaa"): _record("deseq2-r44-aaaaaaaaaaaa", provides=["R:DESeq2"],
                                                               recipe={"conda": ["r-base=4.4", "bioconductor-deseq2=1.46.0"]}),
                     _rpath("seqtk-img-bbbbbbbbbbbb"): _record("seqtk-img-bbbbbbbbbbbb", kind="image", provides=["seqtk"],
                                                               recipe={"image": "docker://quay.io/biocontainers/seqtk:1.4"},
                                                               image="docker://quay.io/biocontainers/seqtk:1.4")})
    lab = _lab(remote)
    assert [e["environment"] for e in lab.find("mosdepth")] == ["mosdepth-0123456789ab"]
    assert [e["environment"] for e in lab.find("DESeq2")] == ["deseq2-r44-aaaaaaaaaaaa"]
    assert lab.find("nothing-like-it") == []
    refs = lab.resolve("mosdepth-0123456789ab, seqtk-img-bbbbbbbbbbbb")
    assert refs[0] == EnvRef(id="mosdepth-0123456789ab", path=f"{STORE}/mosdepth-0123456789ab",
                             provides=("mosdepth",))
    assert refs[1].kind == "image" and refs[1].image.endswith("seqtk:1.4")
    with pytest.raises(RecipeError, match="No lab environment"):
        lab.resolve("mosdepth-ffffffffffff")
    with pytest.raises(RecipeError, match="No lab environment"):
        lab.resolve("../../etc")


# --- the tools ------------------------------------------------------------------------------------


def _shell(lab, *, confirm=None, worker_calls=None) -> HpcShell:
    calls = worker_calls if worker_calls is not None else []

    def run_on_worker(_alloc, command, _timeout):
        calls.append(command)
        return ExecResult(command, 0, "ok", "")

    return HpcShell(remote=lab.remote, workspace=HpcWorkspace(read_roots=(SESSION,), write_roots=(SESSION,)),
                    worker_provider=lambda: object(), run_on_worker=run_on_worker, confirm=confirm,
                    environments=lab)


def test_a_new_environment_waits_for_the_user_and_an_existing_one_does_not():
    asked = []
    lab = _lab()
    sh = _shell(lab, confirm=lambda req: asked.append(req) or True)
    out = sh.request_environment(CONDA)
    assert out["status"] == "ready" and len(asked) == 1 and len(lab.jobs) == 1
    assert asked[0].kind == "install" and "mosdepth=0.3.10" in asked[0].detail
    again = sh.request_environment({**CONDA, "reason": "another run"})
    assert again["status"] == "ready" and again["reused"] is True
    assert len(asked) == 1 and len(lab.jobs) == 1                 # no second card, no second build


def test_declined_unanswered_and_auto():
    lab = _lab()
    out = _shell(lab, confirm=lambda req: False).request_environment(CONDA)
    assert out["status"] == "declined" and "do not hand-write" in out["reason"] and not lab.jobs
    lab = _lab()
    assert _shell(lab, confirm=None).request_environment(CONDA)["status"] == "declined"   # nobody to ask: no
    lab = _lab(approval="auto")
    sh = _shell(lab, confirm=lambda req: pytest.fail("auto approval must not ask"))
    assert sh.request_environment(CONDA)["status"] == "ready"
    assert sh.audit()[-1] == {"auto_approved": {"kind": "environment", "id": _recipe().env_id}}


def test_an_invalid_recipe_is_returned_to_the_model_not_raised():
    out = _shell(_lab(), confirm=lambda r: True).request_environment({**CONDA, "conda": ["mosdepth"]})
    assert out["status"] == "invalid" and "not pinned" in out["error"]


def test_run_shell_binds_the_environment_read_only_with_its_programs_first():
    env_id = "mosdepth-0123456789ab"
    lab = _lab(Remote({_rpath(env_id): _record(env_id)}))
    calls: list[str] = []
    sh = _shell(lab, worker_calls=calls)
    out = sh.run_shell("mosdepth --version", environment=env_id)
    assert out["status"] == "ok" and out["environments"] == [env_id]
    assert calls[0].startswith(f'export PATH={STORE}/{env_id}/bin:"$PATH"') and calls[0].endswith("mosdepth --version")
    from aiscientist.hpc.shell import ShellSandbox
    sh.sandbox = ShellSandbox(image="/c/rocky.sif")
    sh.run_shell("mosdepth --version", environment=env_id)
    assert f"-B {STORE}/{env_id}:{STORE}/{env_id}:ro" in calls[-1]
    with pytest.raises(Exception, match="No lab environment"):
        sh.run_shell("ls", environment="other-ffffffffffff")


def test_find_software_offers_lab_environments_and_request_environment():
    env_id = "mosdepth-0123456789ab"
    remote = Remote({_rpath(env_id): _record(env_id)})
    lab = _lab(remote)
    sh = _shell(lab)
    sh.module_catalog = lambda: None  # type: ignore[method-assign]
    found = sh.find_software("mosdepth")
    assert found["status"] == "ok" and found["lab_environments"][0]["environment"] == env_id
    from aiscientist.hpc.modules import ModuleCatalog
    sh.module_catalog = lambda: ModuleCatalog.parse("")  # type: ignore[method-assign]
    missing = sh.find_software("nanoplot")
    assert missing["status"] == "not_found" and "request_environment" in missing["next"]
    note = sh.missing_software_note("bash: mosdepth: command not found", "run_code")
    assert note[0]["lab_environments"] == [env_id] and "environment='" + env_id in note[0]["hint"]
    note = sh.missing_software_note("Error: there is no package called 'DESeq2'", "run_code")
    assert "request_environment" in note[0]["hint"] and "bioconductor-deseq2" in note[0]["hint"]


def test_the_tool_is_offered_only_with_lab_environments():
    lab = _lab()
    names = {t.name for t in hpc_shell_catalog(_shell(lab))}
    assert "request_environment" in names
    sh = _shell(lab)
    sh.environments = None
    assert "request_environment" not in {t.name for t in hpc_shell_catalog(sh)}
    run_shell = next(t for t in hpc_shell_catalog(_shell(lab)) if t.name == "run_shell")
    assert "environment" in run_shell.parameters["properties"]


# --- run_code and run_in_environment ----------------------------------------------------------------


class _HPC:
    """Fake HPC for SlurmCodeExecutor: jobs complete at once; records every command."""

    host, username = "hpc3", "alice"

    def __init__(self):
        self.commands: list[str] = []

    def exec(self, command, timeout=60.0):
        self.commands.append(command)
        if command.startswith("sbatch"):
            return ExecResult(command, 0, "Submitted batch job 7", "")
        if command.startswith("sacct"):
            return ExecResult(command, 0, "COMPLETED", "")
        return ExecResult(command, 0, "", "")

    def read_bytes(self, path, max_bytes=None):
        return b"0\n" if path.endswith(".rc") else b""

    def remote_size(self, path):
        return 0

    def put_file(self, *a, **k):
        pass


def _executor(hpc):
    from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor
    return SlurmCodeExecutor(remote=hpc, container_image="/c/analysis.sif", scratch_dir="/s/jobs/runcode",
                             dataset_path="/d/ds.h5ad", work_dir="/w/work", artifacts_dir="/w/art",
                             images_dir="/c", startup_timeout_s=5, run_timeout_s=5)


def test_run_code_binds_environments_and_keeps_the_image_python():
    hpc = _HPC()
    ref = EnvRef(id="mosdepth-0123456789ab", path=f"{STORE}/mosdepth-0123456789ab")
    with E.using([ref]):
        out = _executor(hpc)("import subprocess")
    script = "\n".join(hpc.commands)
    assert out["environments"] == [ref.id]
    assert f"-B {ref.path}:{ref.path}:ro" in script
    assert '_aisci_runner="$(command -v python)"' in script and '"$_aisci_runner" ' in script
    assert script.index("_aisci_runner=") < script.index(f"export PATH={ref.path}/bin")
    hpc2 = _HPC()
    _executor(hpc2)("print(1)")
    assert "_aisci_runner" not in "\n".join(hpc2.commands)        # nothing bound: unchanged


def test_run_command_runs_bash_in_run_code_s_image_with_the_environment():
    hpc = _HPC()
    ref = EnvRef(id="mosdepth-0123456789ab", path=f"{STORE}/mosdepth-0123456789ab")
    with E.using([ref]):
        out = _executor(hpc).run_command("mosdepth --version", workdir="env/mosdepth-0123456789ab")
    script = "\n".join(hpc.commands)
    assert out["environment"]["sif"] == "/c/analysis.sif" and "/c/analysis.sif" in script
    assert f"-B {ref.path}:{ref.path}:ro" in script and "/w/work/env/mosdepth-0123456789ab" in script


def test_the_run_code_tool_resolves_environment_and_refuses_an_image_one():
    from aiscientist.agents.research_lab import make_run_code_tool
    seen = {}

    def executor(code):
        seen["active"] = E.ACTIVE.get()
        return {"status": "ok", "stdout": "", "returncode": 0}

    conda = EnvRef(id="mosdepth-0123456789ab", path="/e/m")
    image = EnvRef(id="seqtk-bbbbbbbbbbbb", path="/e/s", kind="image", image="docker://quay.io/biocontainers/seqtk:1.4")
    tool = make_run_code_tool(executor, environments=lambda ids: [conda] if "mosdepth" in ids else [image])
    assert "environment" in tool.parameters["properties"]
    assert tool.executor({"code": "print(1)", "environment": "mosdepth-0123456789ab"}, None)["status"] == "ok"
    assert seen["active"] == (conda,) and E.ACTIVE.get() == ()
    assert "run_in_environment" in tool.executor({"code": "x", "environment": "seqtk-bbbbbbbbbbbb"}, None)["error"]
    off = make_run_code_tool(executor)
    assert "not enabled" in off.executor({"code": "x", "environment": "mosdepth-0123456789ab"}, None)["error"]


def test_run_in_environment_runs_a_lab_environment_without_a_skill():
    from aiscientist.agents.research_lab import make_run_in_environment_tool

    class Exec:
        def __init__(self):
            self.calls = []

        def run_in_image(self, command, image, **kw):
            self.calls.append(("image", command, image, kw))
            return {"status": "ok"}

        def run_command(self, command, **kw):
            self.calls.append(("command", command, E.ACTIVE.get(), kw))
            return {"status": "ok"}

    conda = EnvRef(id="mosdepth-0123456789ab", path="/e/m")
    image = EnvRef(id="seqtk-bbbbbbbbbbbb", path="/e/s", kind="image", image="docker://quay.io/biocontainers/seqtk:1.4")
    ex = Exec()
    tool = make_run_in_environment_tool(ex, environments=lambda ids: [conda] if "mosdepth" in ids else [image])
    assert tool.parameters["required"] == ["command"] and "environment" in tool.parameters["properties"]
    out = tool.executor({"environment": "mosdepth-0123456789ab", "command": "mosdepth -h"}, None)
    assert out["environments"] == ["mosdepth-0123456789ab"]
    assert ex.calls[0] == ("command", "mosdepth -h", (conda,), {"workdir": "env/mosdepth-0123456789ab"})
    tool.executor({"environment": "seqtk-bbbbbbbbbbbb", "command": "seqtk"}, None)
    assert ex.calls[1][:3] == ("image", "seqtk", image.image)
    plain = make_run_in_environment_tool(ex)
    assert plain.parameters["required"] == ["skill", "command"]
