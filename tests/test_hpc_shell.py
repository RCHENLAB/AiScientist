"""The agent's HPC3 filesystem/shell tools: placement, path confinement, and the HITL gate.

Offline — a scripted fake stands in for the SSH executor and the worker. The properties pinned
here are the ones that make a near-complete shell safe to hand an agent on a SHARED lab account:

* metadata reads stay on the login node (cheap, allowed there, no allocation);
* everything that moves bytes or burns CPU runs on the held worker, so RCIC compliance is
  structural rather than a guessed-at list of forbidden commands;
* reads and writes are confined, including through symlinks;
* crossing a line raises a confirmation carrying the exact command — it does not fail silently,
  and it does not proceed silently.
"""

from __future__ import annotations

import re

import pytest

from aiscientist.gateway.executor import ExecResult
from aiscientist.hpc import shell as hs
from aiscientist.hpc.shell import ConfirmRequest, HpcShell, HpcShellError, HpcWorkspace
from aiscientist.gateway.settings import LAB_STORAGE, REFERENCE_ROOT, SHARED_ROOT  # noqa: F401

HOME = "/data/homezvol0/alice"
TEMP = f"{SHARED_ROOT}/Temp/alice"
SHARED_RO = f"{SHARED_ROOT}/containers"


class FakeRemote:
    host = "hpc3"
    username = "alice"

    def __init__(self, rules=(), default=("", "", 0)) -> None:
        self.rules = list(rules)
        self.default = default
        self.cmds: list[str] = []
        self.files: dict[str, bytes] = {}

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.cmds.append(command)
        for pattern, out, err, code in self.rules:
            if re.search(pattern, command):
                return ExecResult(command, code, out, err)
        out, err, code = self.default
        return ExecResult(command, code, out, err)

    def read_bytes(self, path: str, max_bytes: int | None = None) -> bytes:
        return self.files.get(path, b"")[: max_bytes or None]

    def remote_size(self, path: str) -> int:
        return len(self.files.get(path, b""))


def _shell(remote=None, *, confirm=None, worker=True, **kw) -> HpcShell:
    remote = remote or FakeRemote()
    ws = HpcWorkspace(read_roots=(HOME, TEMP, SHARED_RO), write_roots=(HOME, TEMP))
    worker_calls: list[str] = []

    def run_on_worker(alloc, command, timeout_s):
        worker_calls.append(command)
        return ExecResult(command, 0, "worker-ok", "")

    sh = HpcShell(remote=remote, workspace=ws, home=HOME, confirm=confirm,
                  worker_provider=(lambda: object()) if worker else None,
                  run_on_worker=run_on_worker if worker else None, **kw)
    sh.worker_calls = worker_calls          # type: ignore[attr-defined]
    return sh


def _yes(_req):
    return True


def _no(_req):
    return False


# --- path resolution + confinement -------------------------------------------


@pytest.mark.parametrize("given,expected", [
    ("~/data", f"{HOME}/data"),
    ("data/x.vcf", f"{HOME}/data/x.vcf"),
    (f"{HOME}/../../etc/passwd", "/data/etc/passwd"),   # two levels up from the home dir
    ("../../../../etc/passwd", "/etc/passwd"),
    (f"{TEMP}/./run1//out", f"{TEMP}/run1/out"),
])
def test_paths_are_normalised_before_they_are_judged(given, expected):
    """Normalising first is what makes containment mean anything: `$HOME/../../etc/passwd` is
    inside $HOME as text and outside it as a path."""
    assert HpcWorkspace().resolve(given, HOME) == expected


def test_reads_outside_the_allowed_roots_are_refused():
    sh = _shell()
    with pytest.raises(HpcShellError) as exc:
        sh.list_dir("/etc")
    assert "outside the areas this session may read" in str(exc.value)


def test_shared_assets_are_readable_but_not_writable():
    ws = HpcWorkspace(read_roots=(HOME, SHARED_RO), write_roots=(HOME,))
    assert ws.can_read(f"{SHARED_RO}/analysis.sif") is True
    assert ws.can_write(f"{SHARED_RO}/analysis.sif") is False


def test_a_prefix_that_is_not_a_path_boundary_does_not_count_as_inside():
    """`/data/homezvol0/alice-evil` must not pass as inside `/data/homezvol0/alice`."""
    ws = HpcWorkspace(read_roots=(HOME,))
    assert ws.can_read(HOME + "-evil/secret") is False
    assert ws.can_read(HOME + "/ok") is True


def test_a_symlink_out_of_the_workspace_is_caught():
    """String containment cannot see through a symlink; the realpath check can."""
    remote = FakeRemote([(r"readlink -f", "/etc/shadow", "", 0)])
    sh = _shell(remote)
    with pytest.raises(HpcShellError) as exc:
        sh.list_dir(f"{HOME}/looks-fine")
    assert "resolves to /etc/shadow" in str(exc.value)


# --- placement: login node vs worker -----------------------------------------


def test_metadata_reads_stay_on_the_login_node_and_allocate_nothing():
    remote = FakeRemote([(r"readlink", HOME, "", 0), (r"ls -lAh", "total 4\n-rw- x.vcf", "", 0)])
    sh = _shell(remote)
    out = sh.list_dir(f"{HOME}/data")

    assert "x.vcf" in out["listing"]
    assert sh.worker_calls == [], "listing a directory must not cost a CPU allocation"


def test_the_general_shell_never_runs_on_a_login_node():
    """The structural guarantee: there is no command allowlist to get wrong, because run_shell
    simply never executes where RCIC forbids byte-moving work."""
    remote = FakeRemote()
    sh = _shell(remote)
    out = sh.run_shell("bcftools view big.vcf.gz | wc -l")

    assert out["ran_on"] == "worker_node"
    assert sh.worker_calls == ["bcftools view big.vcf.gz | wc -l"]
    assert not [c for c in remote.cmds if "bcftools" in c], "nothing reached the login node"


def test_downloads_run_on_the_worker_not_the_login_node():
    """RCIC bans bulk transfer on login nodes, and this repo has tripped that wire before."""
    sh = _shell()
    out = sh.fetch_url("https://ftp.ensembl.org/pub/x.gtf.gz", dest_dir=f"{TEMP}/refs")

    assert out["ran_on"] == "worker_node"
    assert any("wget" in c for c in sh.worker_calls)


def test_worker_tools_explain_themselves_when_the_worker_is_off():
    sh = _shell(worker=False)
    with pytest.raises(HpcShellError) as exc:
        sh.run_shell("ls")
    assert "AISCIENTIST_WORKER_NODE=1" in str(exc.value)


# --- HITL --------------------------------------------------------------------


def test_a_destructive_command_asks_first_and_is_not_run_when_declined():
    sh = _shell(confirm=_no)
    out = sh.run_shell(f"rm -rf {TEMP}/run1")

    assert out["status"] == "declined"
    assert sh.worker_calls == [], "a declined command must not execute"


def test_an_approved_destructive_command_runs_and_the_prompt_carries_the_exact_command():
    seen: list[ConfirmRequest] = []
    sh = _shell(confirm=lambda r: seen.append(r) or True)
    out = sh.run_shell(f"rm -rf {TEMP}/run1")

    assert out["status"] == "ok"
    assert seen[0].kind == "destructive"
    assert seen[0].detail == f"rm -rf {TEMP}/run1", "the approver sees what they are approving"
    assert seen[0].reversible is False


@pytest.mark.parametrize("command", [
    "echo hi > /etc/motd",
    f"rm -rf {HOME}/data",                                   # the home directory
    "cp results.csv ~/results.csv",                          # ~ is the home directory too
    f"mv {TEMP}/a.bam /dfs3b/ruic20_lab/alice/a.bam",        # a member's personal lab folder
    f"echo x >> {SHARED_RO}/analysis.sif",                   # a shared asset
])
def test_a_write_outside_the_aiscientist_folder_is_refused_without_asking(command):
    """Yijun, 2026-10-25: the agent never deletes or rewrites anything outside its AiScientist
    folder, and this is not a question for the user: no confirmation, no run."""
    sh = _shell(confirm=lambda r: pytest.fail(f"must not ask: {r.summary}"))
    sh.workspace = HpcWorkspace(read_roots=(HOME, TEMP, SHARED_RO), write_roots=(TEMP,))
    out = sh.run_shell(command)
    assert out["status"] == "refused" and "outside the folders this session may write" in out["reason"]
    assert "cannot approve" in out["reason"]
    assert sh.worker_calls == [], "a refused command must not run"
    assert sh.audit()[-1]["refused"]["kind"] == "write_outside"


def test_writes_inside_the_workspace_do_not_interrupt_the_user():
    sh = _shell(confirm=lambda r: pytest.fail(f"should not ask: {r.summary}"))
    assert sh.run_shell(f"samtools sort in.bam > {TEMP}/out.bam")["status"] == "ok"


def test_with_no_approver_wired_the_answer_is_no():
    """A fence that silently opens when nobody can be asked is decorative in exactly the
    headless deployments where it matters most."""
    sh = _shell(confirm=None)
    assert sh.run_shell(f"rm -rf {TEMP}/x")["status"] == "declined"
    assert sh.worker_calls == []


def test_every_confirmation_is_recorded_for_the_report():
    sh = _shell(confirm=_yes)
    sh.run_shell(f"rm -rf {TEMP}/x")
    assert [a["confirm"]["kind"] for a in sh.audit()] == ["destructive"]


# --- downloads ---------------------------------------------------------------


@pytest.mark.parametrize("url,allowed", [
    ("https://ftp.ensembl.org/pub/x.gz", True),
    ("https://hgdownload.soe.ucsc.edu/x", True),
    ("https://sub.ensembl.org/x", True),
    ("https://evil-ensembl.org/x", False),
    ("https://ensembl.org.attacker.net/x", False),
    ("https://random.example.com/x", False),
])
def test_allowlist_is_anchored_on_a_dot(url, allowed):
    """A bare `endswith` would let `evil-ensembl.org` pass as `ensembl.org`."""
    assert hs.host_allowed(url, hs.DEFAULT_FETCH_ALLOWLIST) is allowed


def test_an_unknown_download_host_asks_rather_than_refusing():
    seen: list[ConfirmRequest] = []
    sh = _shell(confirm=lambda r: seen.append(r) or True)
    out = sh.fetch_url("https://random.example.com/data.gz", dest_dir=TEMP)

    assert seen[0].kind == "download" and "random.example.com" in seen[0].summary
    assert out["status"] == "ok", "approving it proceeds — the point is that a human saw the URL"


def test_a_declined_download_does_not_run():
    sh = _shell(confirm=_no)
    assert sh.fetch_url("https://random.example.com/x.gz", dest_dir=TEMP)["status"] == "declined"
    assert sh.worker_calls == []


def test_downloads_are_size_capped_by_wget_itself():
    """Aborting mid-stream matters: the lab's dfs3b is close to full, so discovering the size
    after the file has landed is too late."""
    sh = _shell(max_fetch_mb=500)
    sh.fetch_url("https://ftp.ensembl.org/x.gz", dest_dir=TEMP)
    assert "--quota=500m" in sh.worker_calls[0]


def test_non_http_urls_are_refused():
    sh = _shell()
    for bad in ("file:///etc/passwd", "ftp://x/y", "scp://h/p"):
        with pytest.raises(HpcShellError):
            sh.fetch_url(bad, dest_dir=TEMP)


def test_a_download_target_outside_the_workspace_is_refused():
    sh = _shell()
    with pytest.raises(HpcShellError):
        sh.fetch_url("https://ftp.ensembl.org/x.gz", dest_dir="/etc")


# --- reads -------------------------------------------------------------------


def test_read_text_goes_over_sftp_not_a_login_shell():
    remote = FakeRemote([(r"readlink", f"{HOME}/notes.txt", "", 0)])
    remote.files[f"{HOME}/notes.txt"] = b"##fileformat=VCFv4.2\n#CHROM\tPOS\n"
    sh = _shell(remote)
    out = sh.read_text(f"{HOME}/notes.txt")

    assert "fileformat=VCFv4.2" in out["text"]
    assert out["truncated"] is False
    assert not [c for c in remote.cmds if c.startswith("cat ")]


def test_read_text_is_byte_capped_and_says_so():
    remote = FakeRemote([(r"readlink", f"{HOME}/big.txt", "", 0)])
    remote.files[f"{HOME}/big.txt"] = b"x" * 5000
    out = _shell(remote).read_text(f"{HOME}/big.txt", max_bytes=100)
    assert out["truncated"] is True and out["bytes_returned"] == 100


def test_find_files_is_bounded_in_depth_and_count():
    remote = FakeRemote([(r"readlink", TEMP, "", 0), (r"find ", "a.vcf\t10\nb.vcf\t20", "", 0)])
    out = _shell(remote).find_files(TEMP, "*.vcf", max_depth=99, max_results=10_000)
    cmd = next(c for c in remote.cmds if c.startswith("find "))
    assert "-maxdepth 8" in cmd, "depth is clamped so this stays metadata-class work"
    assert "head -n 500" in cmd
    assert out["count"] == 2


def test_output_is_clipped_so_one_call_cannot_flood_the_context():
    remote = FakeRemote([(r"readlink", TEMP, "", 0), (r"ls -lAh", "y" * 100_000, "", 0)])
    out = _shell(remote).list_dir(TEMP)
    assert len(out["listing"]) < hs.MAX_OUTPUT_CHARS + 200
    assert "truncated" in out["listing"]


# --- catalog -----------------------------------------------------------------


def test_no_session_means_no_tools_rather_than_broken_tools():
    """A tool roster that offers something which cannot work is worse than a smaller roster."""
    assert hs.hpc_shell_catalog(None) == []


def test_catalog_exposes_the_expected_tools():
    names = {t.name for t in hs.hpc_shell_catalog(_shell())}
    assert names == {"list_dir", "stat_path", "find_files", "read_text", "disk_usage",
                     "find_software", "run_shell", "fetch_url", "install_package"}


def test_worker_backed_tools_declare_their_dependency():
    by_name = {t.name for t in hs.hpc_shell_catalog(_shell()) if "hpc_worker" in t.requires}
    assert by_name == {"run_shell", "fetch_url", "install_package"}


def test_tool_errors_come_back_as_results_the_model_can_act_on():
    tool = next(t for t in hs.hpc_shell_catalog(_shell()) if t.name == "list_dir")
    out = tool.executor({"path": "/etc"}, None)
    assert out["status"] == "error" and "outside" in out["error"]


# --- the lab's model assets must be readable ----------------------------------------------
#
# read_roots left out the model/reference asset dirs, and the symptom pointed nowhere near a path
# list: a step told to verify scGPT's reference, taxonomy and preprocessing BEFORE inference could
# not read the model directory, so it withheld inference and reported "compatibility could not be
# verified". Run 3c5fbc8608a7 did exactly that -- and the Critic scored the withholding 0.95,
# because the step was right to refuse on what it could see. A capability can be switched off by a
# missing read path with no error anywhere.

def test_the_shared_model_dirs_are_readable_but_never_writable(monkeypatch):
    import types
    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway.settings import HPCSettings

    st = HPCSettings()
    captured: dict = {}

    class _WS:
        def __init__(self, read_roots=(), write_roots=()):
            captured["read"] = read_roots
            captured["write"] = write_roots

    # _build_hpc_shell imports these inside the function, so patch them at the source module.
    from aiscientist.hpc import shell as hs_mod
    monkeypatch.setattr(hs_mod, "HpcWorkspace", _WS)
    monkeypatch.setattr(gw_app, "_hpc_user", lambda _c: "tester")
    # mock=False: _build_hpc_shell returns None for a mock session before it builds any roots.
    conn = types.SimpleNamespace(id="s1", settings=st, executor=object(), mock=False,
                                 emit=lambda *a, **k: None, workspace=None)
    try:
        gw_app._build_hpc_shell(conn, None)
    except Exception:
        pass  # the builder does more than roots; we only need the roots it passed

    assert captured, "the workspace was never constructed"
    read, write = captured["read"], captured["write"]
    for asset in (st.scgpt_model_dir, st.vlreview_model_dir):
        assert asset in read, f"{asset} is not readable — the capability silently cannot verify itself"
        assert asset not in write, f"{asset} must stay read-only — the lab account is shared"
    # The containers the models run inside were already readable; keep it that way.
    assert f"{st.shared_root.rstrip('/')}/containers" in read


# --- a worker that ended between the liveness check and the command ----------


# srun's refusals for a job that is gone, as printed on HPC3 (2026-10-19).
GONE = ["srun: error: Slurm job 58247673 has expired",
        "srun: error: Unable to confirm allocation for job 57546244: Invalid job id specified"]


@pytest.mark.parametrize("refusal", GONE)
def test_a_command_refused_because_the_worker_ended_runs_once_on_a_new_one(refusal):
    """The worker is released while idle and ends itself when unused (issue 23), so it can end
    between the provider's check and the srun. srun refuses BEFORE the step starts, so asking the
    provider again is safe — and the command still runs exactly once."""
    allocs = iter(["old", "new"])
    seen: list[tuple[str, str]] = []

    def run_on_worker(alloc, command, timeout_s):
        seen.append((alloc, command))
        if alloc == "old":
            return ExecResult(command, 1, "", refusal + "\n")
        return ExecResult(command, 0, "done", "")

    sh = HpcShell(remote=FakeRemote(), workspace=HpcWorkspace(read_roots=(HOME,), write_roots=(HOME,)),
                  home=HOME, worker_provider=lambda: next(allocs), run_on_worker=run_on_worker)
    res = sh._worker("du -sh /x")
    assert res.ok and seen == [("old", "du -sh /x"), ("new", "du -sh /x")]


@pytest.mark.parametrize("err", [
    "srun: error: hpc3-23-18: task 0: Exited with exit code 1",
    "ERROR: cannot verify example.org's certificate: Issued certificate has expired.",
    "wget: Slurm job 7 has expired",
])
def test_a_command_that_failed_on_its_own_is_not_run_again(err):
    """Only srun's refusal to START is retried — never a command that ran and failed, even when
    its own message happens to say "has expired"."""
    calls: list[str] = []

    def run_on_worker(alloc, command, timeout_s):
        calls.append(command)
        return ExecResult(command, 1, "", err)

    sh = HpcShell(remote=FakeRemote(), workspace=HpcWorkspace(read_roots=(HOME,), write_roots=(HOME,)),
                  home=HOME, worker_provider=object, run_on_worker=run_on_worker)
    assert not sh._worker("false").ok and calls == ["false"]


# --- the fixed AiScientist folder and the shell sandbox ------------------------
# docs/environments/README.md §5. The textual check above explains a refusal early; the boundary is
# run_shell's Apptainer sandbox, where only the AiScientist folders are mounted writable.

def test_downloads_default_to_the_aiscientist_folder_and_never_the_home_directory():
    sh = _shell()
    sh.workspace = HpcWorkspace(read_roots=(HOME, TEMP), write_roots=(TEMP,))
    out = sh.fetch_url("https://ftp.ensembl.org/pub/x.gtf.gz")
    assert out["dest_dir"] == f"{TEMP}/downloads"
    with pytest.raises(HpcShellError, match="inside your AiScientist folder"):
        sh.fetch_url("https://ftp.ensembl.org/pub/x.gtf.gz", dest_dir=HOME)


def _sandboxed(network=True, **kw):
    sh = _shell(**kw)
    sh.workspace = HpcWorkspace(read_roots=(HOME, TEMP, SHARED_RO), write_roots=(TEMP,))
    sh.sandbox = hs.ShellSandbox(image=f"{SHARED_RO}/rockylinux_9.7.20251123.sif",
                                 container_module="apptainer/1.4.5", network=network)
    return sh


def test_run_shell_runs_inside_the_sandbox_with_only_the_aiscientist_folder_writable():
    sh = _sandboxed()
    out = sh.run_shell("module load samtools/1.15.1 && samtools --version")
    (script,) = sh.worker_calls
    assert out["sandbox"] == "apptainer" and out["status"] == "ok"
    assert "module load apptainer/1.4.5" in script
    assert "exec apptainer exec --containall --no-mount bind-paths --home" in script
    assert "--net --network none" not in script                    # network per AISCIENTIST_SANDBOX_NETWORK
    assert f"B+=(-B {TEMP}:{TEMP})" in script                       # the only writable mount
    assert f"B+=(-B {HOME}:{HOME}:ro)" in script                    # the home directory: read-only
    for host in ("/opt/apps", "/opt/rcic", "/usr"):
        assert f"B+=(-B {host}:{host}:ro)" in script                # RCIC modules work, read-only
    assert f"--home {TEMP}/shell-home --pwd {TEMP}" in script       # ~ inside is in the folder
    assert 'APPTAINERENV_USER="${USER:-$(id -un)}"' in script      # RCIC module files need USER
    assert "samtools --version" in script
    assert sh.sandbox.image_ref in script                          # pulled on the worker on first use


def test_without_network_the_sandbox_is_offline_and_a_workdir_becomes_its_cwd():
    sh = _sandboxed(network=False)
    sh.run_shell("ls", workdir=f"{TEMP}/run1")
    (script,) = sh.worker_calls
    assert "--net --network none" in script and f"--pwd {TEMP}/run1" in script


def test_the_sandbox_script_passes_the_command_through_unchanged(tmp_path):
    """Run the generated script with a stand-in `apptainer` that prints its arguments: the binds,
    the flags and the user's command (quotes and all) must arrive exactly."""
    import json
    import os
    import shutil
    import subprocess
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    ro, rw = tmp_path / "home", tmp_path / "aiscientist" / "Temp" / "alice"
    ro.mkdir()
    image = tmp_path / "rocky.sif"
    image.write_bytes(b"sif")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "apptainer").write_text('#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
    (fake / "apptainer").chmod(0o755)
    command = """awk '{print $1}' "a b.txt" > out.txt; echo 'done'"""
    script = hs.ShellSandbox(image=str(image), host_ro=()).wrap(
        command, read_only=(str(ro),), writable=(str(rw),), home=f"{rw}/shell-home", cwd=str(rw))
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "MODULEPATH": "/m"})
    assert done.returncode == 0, done.stderr
    argv = json.loads(done.stdout)
    assert argv[:4] == ["exec", "--containall", "--no-mount", "bind-paths"]
    assert ["-B", f"{ro}:{ro}:ro"] == argv[argv.index(f"{ro}:{ro}:ro") - 1:argv.index(f"{ro}:{ro}:ro") + 1]
    assert f"{rw}:{rw}" in argv and rw.is_dir() and (rw / "shell-home").is_dir()
    assert argv[argv.index("--home") + 1] == f"{rw}/shell-home" and argv[argv.index("--pwd") + 1] == str(rw)
    assert argv[-4:-1] == [str(image), "/bin/bash", "-c"]
    assert argv[-1].endswith("\n" + command), "the command reaches the sandbox exactly as written"


def test_the_gateway_gives_the_agent_only_the_aiscientist_folders_and_a_sandboxed_shell(monkeypatch):
    """`_build_hpc_shell`: the home directory is readable but not writable; every writable root is
    under the AiScientist root; run_shell gets the sandbox unless switched off."""
    import types
    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway.settings import HPCSettings
    from aiscientist.hpc import shell as hs_mod

    captured: dict = {}

    class _Shell:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(hs_mod, "HpcShell", _Shell)
    monkeypatch.setattr(gw_app, "_hpc_user", lambda _c: "tester")
    monkeypatch.setattr(gw_app, "_hpc_home", lambda _c: "/data/homezvol3/tester")
    st = HPCSettings()
    conn = types.SimpleNamespace(id="s1", settings=st, executor=object(), mock=False,
                                 emit=lambda *a, **k: None, workspace=None)
    gw_app._build_hpc_shell(conn, None)
    ws = captured["workspace"]
    assert "/data/homezvol3/tester" in ws.read_roots
    assert "/data/homezvol3/tester" not in ws.write_roots
    shared = st.shared_root.rstrip("/")
    # This session's folder first (the shell's HOME and downloads), then the retired layout's
    # Temp/<user> (a run continued from before session folders) and uploads/<user>.
    assert ws.write_roots == (f"{shared}/sessions/s1", f"{shared}/Temp/tester", f"{shared}/uploads/tester")
    assert not any(r.startswith(f"{shared}/sessions/") and r != f"{shared}/sessions/s1" for r in ws.write_roots)
    sb = captured["sandbox"]
    assert sb is not None and sb.image == f"{shared}/containers/rockylinux_9.7.20251123.sif"
    assert sb.container_module == st.container_module

    captured.clear()
    monkeypatch.setenv("AISCIENTIST_SHELL_SANDBOX", "0")
    gw_app._build_hpc_shell(conn, None)
    assert captured["sandbox"] is None



# --- software: HPC3's module catalog (hpc/modules.py) ------------------------------------------

_MODULE_CATALOG = "\n".join([
    "M\tBIOTOOLS\tbedtools2/2.29.2\tBedtools2 utilities are a swiss-army knife of tools",
    "M\tBIOTOOLS\tbedtools2/2.30.0\tBedtools2 utilities are a swiss-army knife of tools",
    "C\tbedtools2/2.30.0\tbedtools",
    "M\tLANGUAGES\tR/4.5.2\tR is a freely available language and environment for statistical computing.",
    "C\tR/4.5.2\tRscript",
])


@pytest.fixture
def module_shell(monkeypatch):
    from aiscientist.hpc import modules
    monkeypatch.setattr(modules, "_CACHE", {})
    remote = FakeRemote(rules=[(r"modulefiles", _MODULE_CATALOG, "", 0)])
    return _shell(remote)


def _tool(sh, name):
    return next(t for t in hs.hpc_shell_catalog(sh) if t.name == name)


def test_find_software_answers_on_the_login_node_and_reads_the_catalog_once(module_shell):
    sh = module_shell
    out = _tool(sh, "find_software").executor({"name": "bedtools"}, None)
    assert out["status"] == "ok" and out["modules"][0]["load"] == "bedtools2/2.30.0"
    assert out["modules"][0]["matched_by"] == "program `bedtools`"
    assert "run_shell" in out["how_to_use"]
    sh.find_software("Rscript")
    assert sum("modulefiles" in c for c in sh.remote.cmds) == 1      # cached for the next search
    assert sh.worker_calls == []                                     # no allocation for a lookup
    assert "find_software" not in {t.name for t in hs.hpc_shell_catalog(sh) if "hpc_worker" in t.requires}


def test_find_software_says_what_to_do_when_nothing_provides_it(module_shell):
    out = module_shell.find_software("mosdepth")
    assert out["status"] == "not_found" and "do not hand-write a replacement" in out["next"]


def test_find_software_reports_an_unreadable_catalog(monkeypatch):
    from aiscientist.hpc import modules
    monkeypatch.setattr(modules, "_CACHE", {})
    sh = _shell(FakeRemote(rules=[(r"modulefiles", "", "Permission denied", 1)]))
    assert sh.find_software("bedtools")["status"] == "error"
    with pytest.raises(HpcShellError):
        sh.find_software("  ")


def test_a_failed_run_shell_names_the_module_it_lacked(module_shell):
    sh = module_shell
    sh.run_on_worker = lambda alloc, cmd, t: ExecResult(cmd, 127, "", "bash: line 1: bedtools: command not found")
    out = sh.run_shell(f"bedtools intersect -a {TEMP}/a.bed -b {TEMP}/b.bed")
    assert out["status"] == "failed"
    note = out["missing_software"][0]
    assert note["name"] == "bedtools" and note["modules"] == ["bedtools2/2.30.0"]
    assert "module load bedtools2/2.30.0 && bedtools" in note["hint"]


def test_a_successful_or_unrelated_failure_does_not_read_the_catalog(module_shell):
    sh = module_shell
    assert "missing_software" not in sh.run_shell(f"ls {TEMP}")
    sh.run_on_worker = lambda alloc, cmd, t: ExecResult(cmd, 2, "", "ls: cannot access 'x': No such file")
    assert "missing_software" not in sh.run_shell(f"ls {TEMP}/x")
    assert not any("modulefiles" in c for c in sh.remote.cmds)


def test_a_failed_run_code_snippet_names_the_module_it_lacked(module_shell):
    from aiscientist.agents.research_lab import make_run_code_tool
    from aiscientist.agents.research_harness import HarnessContext

    def sandbox(code):
        return {"status": "error", "stderr": "bash: line 1: Rscript: command not found", "returncode": 127}

    note_fn = lambda text: module_shell.missing_software_note(text, "run_code")  # noqa: E731
    tool = make_run_code_tool(sandbox, software_note=note_fn)
    out = tool.executor({"code": "import subprocess; subprocess.run('Rscript x.R', shell=True)"},
                        HarnessContext())
    note = out["missing_software"][0]
    assert note["modules"] == ["R/4.5.2"] and "run_shell" in note["hint"]
    assert "find_software" in tool.description

    ok = make_run_code_tool(lambda code: {"status": "ok", "stdout": "fine"}, software_note=note_fn)
    assert "missing_software" not in ok.executor({"code": "print(1)"}, HarnessContext())

    def broken(_text):
        raise RuntimeError("catalog bug")

    survives = make_run_code_tool(sandbox, software_note=broken)
    assert survives.executor({"code": "print(1)"}, HarnessContext())["status"] == "error"
