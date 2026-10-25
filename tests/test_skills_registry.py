"""Pinned registry releases and per-run policy, entirely offline."""
import hashlib
import json
import sqlite3
import zipfile
from dataclasses import replace

import pytest

from aiscientist.agents import skills as sk
from aiscientist.agents import skills_registry as registry
from aiscientist.agents.research_harness import HarnessContext


@pytest.fixture
def release(tmp_path, monkeypatch):
    root = tmp_path / "releases" / "0.1.0"
    root.mkdir(parents=True)
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT", str(root.parent))
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_STATS", str(tmp_path / "counts.sqlite3"))
    rows = []
    for tier in ("core", "verified", "community"):
        name = "skill-" + tier
        with zipfile.ZipFile(root / f"{name}-1.2.3.zip", "w") as zf:
            zf.writestr(name + "/SKILL.md", f"---\nname: {name}\ndescription: old description\n---\nGuidance")
            zf.writestr(name + "/scripts/run.sh", "echo hello")
        rows.append({"name": name, "description": "catalog description", "category": "test",
                     "path": "skills/test/" + name, "version": "1.2.3", "status": tier,
                     "tags": ["biology"], "license": "MIT", "tested_with": "tool 1.0", "author": "",
                     "requirements": {"compute": "16 CPUs, 80 GB, 20 min", "gpu": False,
                                      "scheduler": "optional", "network": "none", "secrets": [],
                                      "allowed_tools": ""},
                     "has_code": True, "has_tests": False})
    (root / "catalog.json").write_text(json.dumps({"schema_version": 1,
        "registry": "aiscientist-skills", "registry_version": "0.1.0", "skills": rows}))
    checksums(root)
    yield root
    monkeypatch.undo()
    sk.refresh_skills()


def checksums(root):
    (root / "SHA256SUMS").write_text("".join(
        hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n"
        for p in sorted(root.iterdir()) if p.is_file() and p.name != "SHA256SUMS"))


def test_checksum_ok_catalog_metadata_and_tiers(release, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core,verified")
    lib = registry.load(sk._load_from)
    assert set(lib) == {"skill-core", "skill-verified"}
    skill = lib["skill-core"]
    assert skill.summary == "catalog description"
    assert skill.version == "1.2.3" and skill.registry_release == "v0.1.0"
    assert skill.metadata["compute"] == "16 CPUs, 80 GB, 20 min"
    assert skill.files["scripts/run.sh"] == "echo hello"
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core,verified,community")
    assert len(registry.load(sk._load_from)) == 3


@pytest.mark.parametrize("missing", [False, True])
def test_a_checksum_mismatch_or_missing_file_gives_no_curated_skills(release, missing, caplog, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_INDUCED_SKILLS_DIR", raising=False)
    path = release / "skill-core-1.2.3.zip"
    if missing:
        path.unlink()
    else:
        path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        registry.load(sk._load_from)
    # The library is empty, and loading does not raise: it runs at import, so raising would keep
    # the gateway from starting.
    assert sk._load_skills() == {}
    assert "Pinned skills release refused" in caplog.text and "checksum" in caplog.text


@pytest.mark.parametrize("tag", ["main", "develop", "v0.1", "v0.1.0/branch", "../v0.1.0"])
def test_tag_pinning(release, monkeypatch, tag):
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASE_TAG", tag)
    with pytest.raises(ValueError, match="pinned"):
        registry.config()


def test_an_absent_release_is_an_empty_library_and_says_how_to_install_it(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT", str(tmp_path / "absent"))
    monkeypatch.delenv("AISCIENTIST_INDUCED_SKILLS_DIR", raising=False)
    # Neither a folder named by AISCIENTIST_SKILLS_DIR nor a fallback switch brings local skills back.
    monkeypatch.setenv("AISCIENTIST_SKILLS_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_TRANSITIONAL_FALLBACK", "true")
    local = tmp_path / "local" / "example"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("---\nname: example\n---\nLocal guidance")
    assert registry.load(sk._load_from) == {}
    assert sk._load_skills() == {}
    assert "Pinned skills release v0.1.0 is not installed" in caplog.text
    assert (f"gh release download v0.1.0 -R lijinbio/aiscientist-skills -D {tmp_path / 'absent' / '0.1.0'}"
            in caplog.text)


def test_unpacked_files_must_all_be_verified(release):
    catalog = json.loads((release / "catalog.json").read_text())
    row = catalog["skills"][0]
    folder = release / row["path"]
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: skill-core\n---\nGuidance")
    checksums(release)  # Nested files deliberately absent.
    with pytest.raises(ValueError, match="unverified"):
        registry.load(sk._load_from)
    with (release / "SHA256SUMS").open("a") as f:
        path = folder / "SKILL.md"
        f.write("\n" + hashlib.sha256(path.read_bytes()).hexdigest() + "  " + row["path"] + "/SKILL.md")
    assert registry.load(sk._load_from)["skill-core"].doc == "Guidance"


def test_zip_traversal_refused(release):
    with zipfile.ZipFile(release / "skill-core-1.2.3.zip", "a") as zf:
        zf.writestr("../outside", "bad")
    checksums(release)
    with pytest.raises(ValueError, match="unsafe"):
        registry.load(sk._load_from)


def test_manifest_and_version_counters_and_secrets(release, tmp_path, monkeypatch):
    skill = registry.load(sk._load_from)["skill-core"]
    skill = replace(skill, requirements={**skill.requirements, "secrets": ["TOKEN"]})
    ctx = HarnessContext(workspace=tmp_path / "run")
    class Executor:
        supports_skill_policy = True
    def run():
        assert registry.POLICY.get()["secret_values"] == {"TOKEN": "private-value"}
        return {"status": "ok", "stdout": "private-value"}
    monkeypatch.setenv("AISCIENTIST_SKILL_SECRET_TOKEN", "private-value")
    out = registry.execute(skill, ctx, Executor(), run, "run_code")
    assert out["stdout"] == "[REDACTED]"
    monkeypatch.delenv("AISCIENTIST_SKILL_SECRET_TOKEN")
    assert registry.execute(skill, ctx, Executor(), run, "run_code")["status"] == "error"
    assert registry.POLICY.get() is None
    manifest = json.loads((ctx.workspace / "manifest.json").read_text())
    assert manifest == {"registry_release": "v0.1.0", "skills": [{"name": skill.name,
        "version": "1.2.3", "registry_release": "v0.1.0"}]}
    assert "private-value" not in json.dumps(manifest)
    with sqlite3.connect(tmp_path / "counts.sqlite3") as db:
        assert db.execute("SELECT success,error FROM skill_versions").fetchone() == (1, 1)


def test_run_code_policy_and_local_isolation_gate(release, tmp_path):
    from aiscientist.agents.research_lab import make_run_code_tool
    from aiscientist.agents.sandbox import CodeSandbox
    skill = registry.load(sk._load_from)["skill-core"]
    ctx = HarnessContext(workspace=tmp_path / "run")
    registry.record_use(ctx, skill)
    sandbox = CodeSandbox()
    result = make_run_code_tool(sandbox).executor({"code": "print(1)"}, ctx)
    assert result["status"] == "error" and "isolation" in result["error"]
    sandbox.container_image = "test.sif"
    token = registry.POLICY.set({**skill.requirements, "secret_values": {}})
    try:
        cmd, _ = sandbox._command("/tmp/code.py", "/tmp")
        assert "--cleanenv" in cmd and "--network" in cmd and "none" in cmd
    finally:
        registry.POLICY.reset(token)


def test_read_reference_records_version(release, tmp_path):
    lib = registry.load(sk._load_from)
    ctx = HarnessContext(workspace=tmp_path / "run")
    result = sk.make_skill_reference_tool(lambda: lib).executor({"name": "skill-core"}, ctx)
    assert result["version"] == "1.2.3" and result["requirements"]["gpu"] is False
    assert registry.active_skill(ctx).name == "skill-core"
    assert json.loads((ctx.workspace / "manifest.json").read_text())["skills"][0]["version"] == "1.2.3"


@pytest.mark.parametrize("gpu,network,scheduler", [(False, "none", "none"), (True, "runtime", "required"),
                                                   (False, "install", "optional")])
def test_slurm_container_permissions_and_private_credentials(release, tmp_path, monkeypatch, gpu, network,
                                                             scheduler):
    from aiscientist.gateway import slurm_sandbox as slurm
    from aiscientist.gateway.executor import ExecResult
    skill = registry.load(sk._load_from)["skill-core"]
    skill = replace(skill, requirements={**skill.requirements, "gpu": gpu, "network": network,
                    "scheduler": scheduler, "secrets": ["TOKEN"]})
    monkeypatch.setenv("AISCIENTIST_SKILL_SECRET_TOKEN", "private-value")
    class Remote:
        commands = []
        uploads = []
        def exec(self, command, **kwargs):
            self.commands.append(command)
            return ExecResult(command, 0, "", "")
        def put_file(self, local, remote):
            from pathlib import Path
            self.uploads.append((remote, Path(local).read_text()))
    remote = Remote()
    executor = slurm.SlurmCodeExecutor(remote=remote, container_image="test.sif", scratch_dir="/scratch")
    scripts = []
    def batch(remote, spec, **kwargs):
        scripts.append(spec.script)
        return object()
    monkeypatch.setattr(slurm, "run_batch_job", batch)
    monkeypatch.setattr(executor, "_artifact_snapshot", lambda: {})
    monkeypatch.setattr(executor, "_written_since", lambda _: [])
    monkeypatch.setattr(executor, "_sync_artifacts_back", lambda **kw: None)
    monkeypatch.setattr(executor, "_collect", lambda *a, **kw: {"status": "ok", "stdout": "private-value"})
    ctx = HarnessContext(workspace=tmp_path / "run")
    result = registry.execute(skill, ctx, executor, lambda: executor("print(1)"), "run_code")
    assert result["status"] == "ok" and result["stdout"] == "[REDACTED]"
    # Every skill runs as a contained job, scheduler: none included (the job is the platform's
    # container; the code in it cannot reach Slurm).
    script = scripts[0]
    assert "--containall" in script and "--cleanenv" in script
    assert ("--nv" in script) == gpu and ("--gres=gpu:1" in script) == gpu
    # `install` runs with network like `runtime`: the skill installs its own software.
    assert ("--network none" in script) == (network == "none")
    assert "private-value" not in script
    assert remote.uploads[0][1] == "export TOKEN=private-value"
    assert remote.uploads[0][0] in remote.commands[-1] and "rm -f" in remote.commands[-1]
    assert any("umask 077" in c for c in remote.commands)


def test_restricted_tools_and_executor_capability(release, tmp_path):
    skill = registry.load(sk._load_from)["skill-core"]
    ctx = HarnessContext(workspace=tmp_path / "run")
    def forbidden():
        pytest.fail("execution must be denied")
    assert "cannot enforce" in registry.execute(skill, ctx, object(), forbidden, "run_code")["error"]
    class Executor:
        supports_skill_policy = True
    skill = replace(skill, requirements={**skill.requirements, "allowed_tools": "run_in_environment"})
    assert "does not declare tool" in registry.execute(skill, ctx, Executor(), forbidden, "run_code")["error"]
    skill = replace(skill, requirements={**skill.requirements, "allowed_tools": "Read run_code"})
    assert registry.execute(skill, ctx, Executor(), lambda: {"status": "ok"}, "run_code")["status"] == "ok"


def test_registry_policy_does_not_use_uncontained_dependency_preflight(monkeypatch):
    from aiscientist.gateway.code_preflight import PreflightingExecutor
    wrapper = PreflightingExecutor(inner=lambda code: {"status": "ok"})
    def forbidden(code):
        pytest.fail("uncontained dependency preflight must not run")
    monkeypatch.setattr(wrapper, "_resolve", forbidden)
    token = registry.POLICY.set({"network": "none"})
    try:
        assert wrapper("import missing")["status"] == "ok"
    finally:
        registry.POLICY.reset(token)


def test_release_as_the_registry_publishes_it(tmp_path, monkeypatch):
    """The assets of a GitHub release of lijinbio/aiscientist-skills, built the way its
    aiscientist_skills/package.py builds them: <name>-<version>.zip with <name>/ inside, fixed
    timestamps, SHA256SUMS over the zips and catalog.json."""
    root = tmp_path / "releases" / "0.1.0"
    root.mkdir(parents=True)
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT", str(root.parent))
    name = "qc-snrna-with-cellqc-standalone"
    with zipfile.ZipFile(root / f"{name}-1.0.0.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, text in (("SKILL.md", f"---\nname: {name}\ndescription: d\nmetadata:\n"
                                       "  category: single-cell\n  status: core\n---\n# CellQC\n"),
                          ("references/scripts.md", "cellqc -c config.yaml\n")):
            info = zipfile.ZipInfo(f"{name}/{rel}", (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            zf.writestr(info, text)
    (root / "catalog.json").write_text(json.dumps({
        "schema_version": 1, "registry": "aiscientist-skills", "registry_version": "0.1.0",
        "generator": "aiscientist-skills 0.1.0",
        "categories": [{"name": "single-cell", "description": "Single-cell"}],
        "skills": [{"name": name, "category": "single-cell",
                    "path": "skills/single-cell/" + name, "version": "1.0.0", "status": "core",
                    "description": "Run the CellQC pipeline over a cohort of 10x libraries.",
                    "tags": ["qc", "snakemake"], "license": "MIT", "tested_with": "cellqc 0.3.6",
                    "author": "Jin Li",
                    "requirements": {"compute": "16 CPUs, 80 GB requested, 20 min for 4 libraries",
                                     "gpu": False, "scheduler": "optional", "network": "install",
                                     "secrets": [], "allowed_tools": ""},
                    "has_code": False, "has_tests": False}]}, indent=2) + "\n")
    checksums(root)
    try:
        skill = registry.load(sk._load_from)[name]
        assert skill.version == "1.0.0" and skill.status == "core" and skill.registry_release == "v0.1.0"
        assert skill.files["references/scripts.md"] == "cellqc -c config.yaml\n"
        assert skill.metadata["compute"].startswith("16 CPUs")
        assert set(sk._load_skills()) >= {name}
    finally:
        monkeypatch.undo()
        sk.refresh_skills()


CELLQC = "qc-snrna-with-cellqc-standalone"


def bundle_release(tmp_path, monkeypatch, members: dict[str, str], rows: list[str]):
    """A release as aiscientist_skills/package.py now builds it: aiscientist-skills-<version>.zip
    with each skill folder at its catalog path, catalog.json, and SHA256SUMS over the two."""
    root = tmp_path / "releases" / "0.1.0"
    root.mkdir(parents=True)
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT", str(root.parent))
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core,verified,community")
    with zipfile.ZipFile(root / "aiscientist-skills-0.1.0.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, text in members.items():
            info = zipfile.ZipInfo(rel, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            zf.writestr(info, text)
    (root / "catalog.json").write_text(json.dumps({
        "schema_version": 1, "registry": "aiscientist-skills", "registry_version": "0.1.0",
        "skills": [{"name": path.rsplit("/", 1)[1], "category": path.split("/")[1], "path": path,
                    "version": "1.2.0", "status": status, "description": "catalog description",
                    "tags": [], "requirements": {"compute": "16 CPUs, 80 GB", "network": "install"}}
                   for path, status in (r.split("@") for r in rows)]}))
    checksums(root)
    return root


def skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: d\n---\n# {name}\n"


def test_release_bundle_as_the_registry_publishes_it(tmp_path, monkeypatch):
    cellqc = f"skills/single-cell/{CELLQC}"
    other = "skills/variants/normalize-vcf-with-bcftools"
    root = bundle_release(tmp_path, monkeypatch, {
        f"{cellqc}/SKILL.md": skill_md(CELLQC),
        f"{cellqc}/assets/cellqc-defaults.json": '{"thresholds": {}}',
        f"{other}/SKILL.md": skill_md("normalize-vcf-with-bcftools"),
    }, [f"{cellqc}@core", f"{other}@community"])
    assert sorted(p.name for p in root.iterdir()) == [
        "SHA256SUMS", "aiscientist-skills-0.1.0.zip", "catalog.json"]
    try:
        lib = registry.load(sk._load_from)
        assert set(lib) == {CELLQC, "normalize-vcf-with-bcftools"}
        skill = lib[CELLQC]
        assert skill.version == "1.2.0" and skill.registry_release == "v0.1.0"
        assert skill.files["assets/cellqc-defaults.json"] == '{"thresholds": {}}'
        assert lib["normalize-vcf-with-bcftools"].status == "community"
    finally:
        monkeypatch.undo()
        sk.refresh_skills()


def test_release_bundle_traversal_refused(tmp_path, monkeypatch):
    cellqc = f"skills/single-cell/{CELLQC}"
    bundle_release(tmp_path, monkeypatch, {f"{cellqc}/SKILL.md": skill_md(CELLQC),
                                           "../outside": "bad"}, [f"{cellqc}@core"])
    try:
        with pytest.raises(ValueError, match="unsafe"):
            registry.load(sk._load_from)
    finally:
        monkeypatch.undo()


def test_release_bundle_missing_a_catalog_skill_is_refused(tmp_path, monkeypatch):
    cellqc = f"skills/single-cell/{CELLQC}"
    bundle_release(tmp_path, monkeypatch, {f"{cellqc}/SKILL.md": skill_md(CELLQC)},
                   [f"{cellqc}@core", "skills/variants/qc-vcf-callset@community"])
    try:
        with pytest.raises(ValueError, match="missing verified bundle for skill qc-vcf-callset"):
            registry.load(sk._load_from)
    finally:
        monkeypatch.undo()


def test_bad_config_leaves_no_skills_and_no_crash(release, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASE_TAG", "main")
    monkeypatch.delenv("AISCIENTIST_INDUCED_SKILLS_DIR", raising=False)
    assert sk._load_skills() == {}
    assert "pinned" in caplog.text
    sk.refresh_skills()                     # the re-scan signature must not raise either
    ctx = HarnessContext(workspace=tmp_path / "run")
    registry.init_manifest(ctx)            # a run still gets its manifest
    assert json.loads((ctx.workspace / "manifest.json").read_text()) == {
        "registry_release": None, "skills": []}


def test_counts_live_in_the_state_dir_and_skip_a_stop(release, tmp_path, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_SKILLS_REGISTRY_STATS")
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    skill = registry.load(sk._load_from)["skill-core"]
    class Executor:
        supports_skill_policy = True
    for user in ("alice", "bob"):          # two accounts, one file
        ctx = HarnessContext(workspace=tmp_path / "runs" / user / "run1")
        registry.execute(skill, ctx, Executor(), lambda: {"status": "ok"}, "run_code")
    registry.execute(skill, ctx, Executor(), lambda: {"status": "cancelled"}, "run_code")
    path = tmp_path / "state" / "skill_versions.sqlite3"
    assert registry.stats_path(ctx) == path
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT success,error FROM skill_versions").fetchall() == [(2, 0)]
    assert not list((tmp_path / "runs").rglob("*.sqlite3"))


@pytest.mark.parametrize("declared,platform,expected", [
    ("none", True, False), ("install", True, True), ("runtime", True, True),
    ("install", False, False), ("runtime", False, False)])
def test_network_follows_the_declaration_and_the_platform_switch(declared, platform, expected):
    assert registry.network_allowed(None, platform) is platform
    assert registry.network_allowed({"network": declared}, platform) is expected


def test_local_sandbox_gives_install_skills_network(release):
    from aiscientist.agents.sandbox import CodeSandbox
    skill = registry.load(sk._load_from)["skill-core"]
    sandbox = CodeSandbox(container_image="test.sif", allow_network=True)
    for declared, offline in (("install", False), ("none", True)):
        token = registry.POLICY.set({**skill.requirements, "network": declared, "secret_values": {}})
        try:
            cmd, _ = sandbox._command("/tmp/code.py", "/tmp")
        finally:
            registry.POLICY.reset(token)
        assert ("none" in cmd) == offline and "--cleanenv" in cmd


def _registry_layout_skill(root):
    folder = root / "single-cell" / "compare-two-groups"
    (folder / "scripts").mkdir(parents=True)
    (folder / "tests").mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: compare-two-groups\ndescription: Compare two groups of cells.\nlicense: MIT\n"
        "metadata:\n  category: single-cell\n  version: \"1.0.0\"\n  status: verified\n"
        "  tested-with: \"scanpy 1.10\"\n  aliases: \"pairwise_de, pairwise_de_v2\"\n---\n# Use\nRun it.\n")
    (folder / "scripts" / "reference.py").write_text("print('de')\n")
    (folder / "tests" / "test_reference.py").write_text("def test_x():\n    pass\n")
    return sk._load_from(root)


def test_migrated_skill_answers_to_its_old_names(tmp_path):
    lib = _registry_layout_skill(tmp_path)
    skill = lib["compare-two-groups"]
    assert skill.aliases == ("compare_two_groups", "pairwise_de", "pairwise_de_v2")
    for old in ("compare_two_groups", "pairwise_de", "pairwise_de_v2", "pairwise_de.py"):
        assert sk.get_skill(old, lib) is skill
    assert sk.skills_named_in("Step 3: adapt `pairwise_de` to condition", lib) == ["compare-two-groups"]
    assert sk.skills_named_in("Step 3: adapt `pairwise_de_v3`", lib) == []
    # An exact name always wins over an alias.
    local = sk.Skill(name="pairwise_de", doc="local")
    assert sk.get_skill("pairwise_de", {**lib, "pairwise_de": local}) is local


def test_registry_layout_reads_scripts_reference_first(tmp_path):
    lib = _registry_layout_skill(tmp_path)
    assert sk.default_file(lib["compare-two-groups"].files) == "scripts/reference.py"
    assert sk.default_file(["tests/test_reference.py", "references/notes.md"]) == "references/notes.md"
    assert sk.default_file({}) is None
    ctx = HarnessContext(workspace=tmp_path / "run")
    out = sk.make_skill_reference_tool(lambda: lib).executor({"name": "pairwise_de"}, ctx)
    assert out["name"] == "compare-two-groups" and "file='scripts/reference.py'" in out["next"]
    assert "run_shell" in out["next"]        # a local skill may still use run_shell
    registry_lib = {"compare-two-groups": replace(lib["compare-two-groups"], registry_release="v0.1.0")}
    out = sk.make_skill_reference_tool(lambda: registry_lib).executor({"name": "pairwise_de"}, ctx)
    assert "run_shell" not in out["next"]    # refused for registry skills
