"""Offline bridge contracts; no containers, network, scanpy or real h5ad."""
import json
import shlex
from pathlib import Path

import pytest

from aiscientist.agents import atlas_expression as atlas, skills, skills_registry
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.registry import build_scientist_catalog
from aiscientist.gateway.executor import ExecResult

FIXTURE = Path(__file__).parent / "fixtures" / "atlas_expression"
IMAGE = ("docker://ghcr.io/lijinbio/mcp-atlas-expression-exploration@sha256:"
         "5e9b13da5d180e7983945a7f1f4182e4b12876d3ec8f59ed0a0aa7b43d041d8a")
GENES = "LRAT, CRALBP, rho, ABCR"
RESOLUTION = [
    {"query": "LRAT", "atlas_gene": "LRAT", "method": "exact"},
    {"query": "CRALBP", "atlas_gene": "RLBP1", "method": "hgnc_alias"},
    {"query": "rho", "atlas_gene": "RHO", "method": "case"},
    {"query": "ABCR", "atlas_gene": "ABCA4", "method": "hgnc_previous"},
]


@pytest.fixture
def release(skill_release, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_ATLAS_FILES", "heca/v2026.11=" + Runner.dataset_path)
    return skill_release(FIXTURE, tag="v0.2.0")


class Runner:
    supports_skill_policy = True
    dataset_path = "/shared/heca/v2026.11/HRCA_snRNA_allcells.h5ad"

    def __init__(self):
        self.calls = []
        self.result = {"resolution": RESOLUTION}

    def run_in_image(self, command, image, **kwargs):
        self.calls.append((command, image, kwargs))
        return {"status": "ok", "returncode": 0,
                "stdout": json.dumps({"result": self.result, "atlas": {
                    "path": self.dataset_path, "version": "heca/v2026.11",
                    "size_bytes": 123, "mtime_ns": 456}}),
                "artifacts": ["gene_expression/q/gene_resolution.tsv"]}


def tools(runner):
    return {t.name: t for t in build_scientist_catalog(code_executor=runner)}


def test_release_metadata_checksum_tier_and_exposure(release, monkeypatch):
    skill = skills.get_skill(atlas.SKILL)
    assert (skill.status, skill.registry_release) == ("community", "v0.2.0")
    assert (skill.image, skill.cpus, skill.mem_gb, skill.time_limit) == (IMAGE, 4, 300, "04:00:00")
    assert set(atlas.TOOLS) <= tools(Runner()).keys()
    assert not set(atlas.TOOLS) & tools(None).keys()
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core,verified")
    assert not set(atlas.TOOLS) & tools(Runner()).keys()
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core,verified,community")
    bundle = release / "aiscientist-skills-0.2.0.zip"
    bundle.write_bytes(bundle.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        skills_registry.check_release(release, "v0.2.0")
    assert not set(atlas.TOOLS) & tools(Runner()).keys()


@pytest.mark.parametrize("value", ["", "heca/latest=/shared/heca/x.h5ad", "heca/v1=relative/x.h5ad"])
def test_without_a_configured_atlas_the_tools_are_not_offered(release, monkeypatch, value):
    """A deployment that lists no atlas would offer nine tools that always fail."""
    monkeypatch.setenv("AISCIENTIST_ATLAS_FILES", value)
    catalog = tools(Runner())
    assert not set(atlas.TOOLS) & catalog.keys()
    out = catalog["run_in_environment"].executor({"skill": atlas.SKILL, "command": "true"}, None)
    assert out["status"] == "error" and "not set up on this deployment" in out["error"]


def test_resolution_flow_preserves_all_user_names_and_provenance(release, tmp_path):
    runner = Runner()
    catalog = tools(runner)
    ctx = HarnessContext(workspace=tmp_path)
    result = catalog["resolve_genes"].executor({"genes": GENES}, ctx)
    assert "stdout" not in result
    assert result["resolution"] == RESOLUTION
    runner.result = {"query_id": "HRCA__LRAT_RLBP1_RHO_ABCA4", "state": "COMPLETED", "resolution": RESOLUTION}
    submitted = catalog["submit_query"].executor({"genes": GENES}, ctx)
    assert submitted["resolution"] == RESOLUTION
    assert submitted["artifacts"] == ["gene_expression/q/gene_resolution.tsv"]
    for index, (command, image, kwargs) in enumerate(runner.calls):
        assert image == IMAGE
        assert (kwargs["cpus"], kwargs["mem_gb"], kwargs["time_limit"]) == ((2, 16, "00:30:00") if index == 0 else (4, 300, "04:00:00"))
        cli = next(line for line in command.splitlines() if line.startswith("aiscientist-mcp "))
        assert json.loads(shlex.split(cli)[7]) == {"genes": GENES}
        assert 'ATLAS_H5AD="$AISCIENTIST_DATASET"' in command
        assert 'AISCI_DATA_DIR="$AISCIENTIST_ENV_DIR" AISCI_EXECUTOR=inline' in command
        assert "--timeout 15000" in command
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["atlas"]["version"] == "heca/v2026.11"
    assert manifest["atlas"]["path"] == runner.dataset_path
    assert manifest["skills"][0]["registry_release"] == "v0.2.0"
    runner.result = {"state": "FAILED", "query_id": "q"}
    assert catalog["query_status"].executor({"query_id": "q"}, ctx)["status"] == "error"


def test_all_nine_exact_signatures(release):
    catalog = tools(Runner())
    expected = {
        "resolve_genes": {"genes", "atlas"}, "submit_query": {"genes", "atlas", "rerun"},
        "query_status": {"query_id"}, "get_summary": {"query_id", "level", "gene", "top"},
        "plot_dotplot": {"query_id"}, "plot_violin": {"query_id", "groupby"},
        "plot_featureplot": {"query_id"}, "write_report": {"query_id"}, "list_queries": set(),
    }
    for name, fields in expected.items():
        assert set(catalog[name].parameters["properties"]) == fields
    from aiscientist.agents.research_lab import _primary_tool
    assert _primary_tool("Resolve with `resolve_genes`.") == "resolve_genes"
    assert _primary_tool("Extract with `submit_query`.") == "submit_query"


def test_invalid_binding_version_and_failed_container(release, monkeypatch):
    runner = Runner()
    tool = tools(runner)["resolve_genes"]
    for args in ({}, {"genes": GENES, "extra": 1}, {"genes": GENES, "atlas": "/other.h5ad"}):
        assert tool.executor(args, None)["status"] == "error"
    runner.dataset_path = "/shared/not-an-atlas.txt"
    assert tool.executor({"genes": GENES}, None)["status"] == "error"
    runner.dataset_path = Runner.dataset_path
    monkeypatch.delenv("AISCIENTIST_ATLAS_FILES")
    assert tool.executor({"genes": GENES}, None)["status"] == "error"
    assert not runner.calls
    monkeypatch.setenv("AISCIENTIST_ATLAS_FILES", "heca/v2026.11=" + Runner.dataset_path)
    monkeypatch.setattr(runner, "run_in_image", lambda *a, **kw: {"status": "error", "stderr": "OOM"})
    assert tools(runner)["submit_query"].executor({"genes": GENES}, None)["stderr"] == "OOM"
    monkeypatch.setattr(runner, "run_in_image", lambda *a, **kw: {"status": "ok", "stdout": "truncated"})
    assert tools(runner)["submit_query"].executor({"genes": GENES}, None)["status"] == "error"


def test_deliverables_are_collected_without_checkpoint_or_atlas(release, tmp_path, monkeypatch, capsys):
    data, artifacts = tmp_path / "work", tmp_path / "artifacts"
    query = data / "atlas-expression-exploration" / "q"
    query.mkdir(parents=True)
    names = ["gene_resolution.tsv", "summary_majorclass.tsv", "summary_celltype.tsv",
             "summary_region_class.tsv", "dotplot_majorclass.pdf", "dotplot_majorclass.png",
             "violin.pdf", "violin.png", "featureplot.pdf", "featureplot.png", "report.md"]
    for name in [*names, "query.h5ad", "job.log", "state.json"]:
        (query / name).write_text(name)
    source = tmp_path / "atlas.h5ad"
    source.write_text("mock atlas; never parsed")
    (tmp_path / ".atlas-result.json").write_text(json.dumps({"resolution": RESOLUTION}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AISCI_DATA_DIR", str(data))
    monkeypatch.setenv("AISCIENTIST_ARTIFACTS", str(artifacts))
    monkeypatch.setenv("ATLAS_H5AD", str(source))
    monkeypatch.setenv("AISCI_ATLAS_RESULT", str(tmp_path / ".atlas-result.json"))
    monkeypatch.setenv("AISCI_ATLAS_RELEASE", "heca/v2026.11")
    exec(atlas.COLLECT, {})
    response = json.loads(capsys.readouterr().out)
    assert response["result"]["resolution"] == RESOLUTION
    assert response["atlas"]["size_bytes"] == source.stat().st_size
    assert sorted(p.name for p in artifacts.rglob("*") if p.is_file()) == sorted([*names, "mcp_result.json"])
    assert source.read_text() == "mock atlas; never parsed"


def test_submit_uses_real_slurm_image_path_with_metadata(release):
    from test_run_in_environment import _HPC, _executor
    hpc = _HPC()
    runner = _executor(hpc)
    runner.dataset_path = Runner.dataset_path
    tools(runner)["submit_query"].executor({"genes": GENES}, None)
    script = "\n".join(hpc.commands)
    for value in ("--cpus-per-task=4", "--mem=300G", "--time=04:00:00",
                  "AISCI_EXECUTOR=inline", "call submit_query", "apptainer"):
        assert value in script


@pytest.mark.parametrize("kind", ["summary", "report", "queries"])
def test_large_response_survives_stdout_tail(release, tmp_path, monkeypatch, capsys, kind):
    from aiscientist.agents.sandbox import _tail

    data = tmp_path / "work"
    query = data / "atlas-expression-exploration" / "q"
    query.mkdir(parents=True)
    (query / "state.json").write_text("{}")
    table = query / "summary_majorclass.tsv"
    table.write_text("full table")
    result = {
        "summary": {"table": str(table), "rows": [{"gene": "G", "value": "x" * 316}] * 80},
        "report": {"file": str(query / "report.md"), "markdown": "report" * 10000},
        "queries": {"queries": [{"query_id": "q" * 300}] * 100},
    }[kind]
    source = tmp_path / "atlas.h5ad"
    source.write_text("atlas")
    raw = tmp_path / "result.json"
    raw.write_text(json.dumps(result))
    for key, value in {"AISCI_DATA_DIR": data, "AISCIENTIST_ARTIFACTS": tmp_path / "artifacts",
                       "ATLAS_H5AD": source, "AISCI_ATLAS_RESULT": raw,
                       "AISCI_ATLAS_RELEASE": "heca/v2026.11"}.items():
        monkeypatch.setenv(key, str(value))
    exec(atlas.COLLECT, {})
    stdout = capsys.readouterr().out
    assert len(stdout) < 20000
    payload = json.loads(_tail(stdout, 20000))
    compact = payload["result"]
    if kind == "summary":
        assert len(compact["rows"]) + compact["rows_omitted"] == 80
        assert compact["rows_omitted"] > 0
        assert compact["full_table"] == "gene_expression/q/summary_majorclass.tsv"
    elif kind == "report":
        assert "markdown" not in compact
        assert compact["file"] == "gene_expression/q/report.md"
    else:
        assert compact["response_omitted"]
    runner = Runner()
    monkeypatch.setattr(runner, "run_in_image", lambda *a, **kw: {"status": "ok", "stdout": _tail(stdout, 20000)})
    response = tools(runner)["list_queries"].executor({}, HarnessContext(workspace=tmp_path))
    assert response["status"] == "ok"
    assert "stdout" not in response
    assert json.loads((tmp_path / "manifest.json").read_text())["atlas"]["version"] == "heca/v2026.11"


def test_direct_skill_refused_even_with_model_override_arguments(release):
    runner = Runner()
    tool = tools(runner)["run_in_environment"]
    out = tool.executor({"skill": atlas.SKILL.upper(), "command": "true",
                         "_atlas_bridge": True, "_resources": [1, 1, "00:01:00"]}, None)
    assert out["status"] == "error"
    assert all(name in out["error"] for name in atlas.TOOLS)
    assert not runner.calls
    assert not {"_atlas_bridge", "_resources", "_tool_cache"} & tool.parameters["properties"].keys()


def test_configured_paths_are_normalized_and_supply_the_release(release, monkeypatch):
    runner = Runner()
    runner.dataset_path = "/shared/other/../other/release.h5ad"
    monkeypatch.setenv("AISCIENTIST_ATLAS_FILES", "other/v1=/shared/other/release.h5ad")
    tools(runner)["resolve_genes"].executor({"genes": GENES, "atlas": "/shared/other/release.h5ad"}, None)
    assert "AISCI_ATLAS_RELEASE=other/v1" in runner.calls[0][0]
    runner.dataset_path = "/shared/unconfigured.h5ad"
    assert tools(runner)["resolve_genes"].executor({"genes": GENES}, None)["status"] == "error"


SERVER_FILE = "/data/Users/shared/heca/HRCA_snRNA_allcells.h5ad"
SESSION_COPY = "/dfs/sessions/abc123/server-data/heca/HRCA_snRNA_allcells.h5ad"


def test_a_session_copy_of_a_listed_server_file_is_accepted(release, monkeypatch, tmp_path):
    """A file bound as server data reaches HPC3 as this session's copy, at a new path each
    session: the file it was copied from is the one that has to be listed."""
    monkeypatch.setenv("AISCIENTIST_ATLAS_FILES", "heca/v2026.11=" + SERVER_FILE)
    runner = Runner()
    runner.dataset_path = SESSION_COPY
    runner.dataset_source = SERVER_FILE
    ctx = HarnessContext(workspace=tmp_path)
    result = tools(runner)["resolve_genes"].executor({"genes": GENES, "atlas": SERVER_FILE}, ctx)
    assert result["status"] == "ok"
    command = runner.calls[0][0]
    assert "AISCI_ATLAS_RELEASE=heca/v2026.11" in command
    assert 'ATLAS_H5AD="$AISCIENTIST_DATASET"' in command      # the job reads its HPC3 copy
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["atlas"]["copied_from"] == SERVER_FILE
    assert "copied_from" not in result["atlas"]                # no server path in the reply
    # The copy's own path is accepted too, and nothing else is.
    assert tools(runner)["resolve_genes"].executor({"genes": GENES, "atlas": SESSION_COPY}, ctx)["status"] == "ok"
    assert tools(runner)["resolve_genes"].executor({"genes": GENES, "atlas": "/other.h5ad"}, ctx)["status"] == "error"


def test_an_unlisted_server_file_or_a_source_without_a_copy_is_refused(release, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_ATLAS_FILES", "heca/v2026.11=" + SERVER_FILE)
    runner = Runner()
    runner.dataset_path = SESSION_COPY
    runner.dataset_source = "/data/Users/shared/heca/some_other.h5ad"
    assert tools(runner)["resolve_genes"].executor({"genes": GENES}, None)["status"] == "error"
    runner.dataset_path = None                                 # nothing on HPC3 to read
    runner.dataset_source = SERVER_FILE
    assert tools(runner)["resolve_genes"].executor({"genes": GENES}, None)["status"] == "error"
    assert not runner.calls


CACHE = "/dfs/envs/tool-cache/tester"


@pytest.mark.parametrize("name", sorted(set(atlas.TOOLS) - {"submit_query"}))
def test_light_tools_use_small_jobs_and_the_users_tool_cache(release, name):
    """The HGNC table lives in this HPC3 user's tool cache, whose parent is made 3775 as the
    analysis jobs' is: a folder one member made in a lab-shared place would lock the others out."""
    from test_run_in_environment import _HPC, _executor
    hpc = _HPC()
    runner = _executor(hpc, tool_cache_dir=CACHE)
    runner.dataset_path = Runner.dataset_path
    args = {key: "q" for key in atlas.TOOLS[name][1]["required"]}
    tools(runner)[name].executor(args, None)
    script = "\n".join(hpc.commands)
    for value in ("--cpus-per-task=2", "--mem=16G", "--time=00:30:00",
                  "mkdir -p -m 3775 /dfs/envs/tool-cache", f"export AISCIENTIST_TOOL_CACHE={CACHE}",
                  f"-B {CACHE}:{CACHE} ",
                  'ATLAS_HGNC="$AISCIENTIST_TOOL_CACHE/atlas-expression/hgnc_complete_set.txt"'):
        assert value in script
    assert "/skills/atlas-expression" not in script.replace("tool-cache/tester", "")


def test_without_a_tool_cache_the_job_runs_and_the_table_stays_in_the_run(release):
    from test_run_in_environment import _HPC, _executor

    class NoCache(_HPC):
        def exec(self, command, timeout=60.0):
            if command.startswith("mkdir -p -m 3775"):
                self.commands.append(command)
                return ExecResult(command, 1, "", "Permission denied")
            return super().exec(command, timeout)

    hpc = NoCache()
    runner = _executor(hpc, tool_cache_dir=CACHE)
    runner.dataset_path = Runner.dataset_path
    tools(runner)["resolve_genes"].executor({"genes": GENES}, None)
    script = "\n".join(c for c in hpc.commands if not c.startswith("mkdir -p -m 3775"))
    assert "sbatch" in script
    assert "export AISCIENTIST_TOOL_CACHE" not in script and f"-B {CACHE}" not in script


def test_responses_and_the_saved_result_name_results_files_not_hpc3_paths(release, tmp_path,
                                                                          monkeypatch, capsys):
    data = tmp_path / "work"
    query = data / "atlas-expression-exploration" / "q"
    query.mkdir(parents=True)
    (query / "state.json").write_text("{}")
    (query / "report.md").write_text("report")
    plots = [str(query / f"dotplot_majorclass.{ext}") for ext in ("pdf", "png")]
    result = {"files": plots, "file": str(query / "report.md"), "table": str(query / "summary_celltype.tsv"),
              "rows": [], "data_dir": str(data / "atlas-expression-exploration"),
              "queries": [{"query_id": "q", "genes": ["LRAT"]}]}
    source = tmp_path / "atlas.h5ad"
    source.write_text("atlas")
    raw = tmp_path / "result.json"
    raw.write_text(json.dumps(result))
    artifacts = tmp_path / "artifacts"
    for key, value in {"AISCI_DATA_DIR": data, "AISCIENTIST_ARTIFACTS": artifacts,
                       "ATLAS_H5AD": source, "AISCI_ATLAS_RESULT": raw,
                       "AISCI_ATLAS_RELEASE": "heca/v2026.11"}.items():
        monkeypatch.setenv(key, str(value))
    exec(atlas.COLLECT, {})
    shown = json.loads(capsys.readouterr().out)["result"]
    assert shown["files"] == ["gene_expression/q/dotplot_majorclass.pdf", "gene_expression/q/dotplot_majorclass.png"]
    assert shown["file"] == "gene_expression/q/report.md"
    assert shown["table"] == shown["full_table"] == "gene_expression/q/summary_celltype.tsv"
    assert shown["data_dir"] == "gene_expression"
    assert shown["queries"] == result["queries"]
    saved = (artifacts / "gene_expression" / "mcp_result.json").read_text()
    assert str(tmp_path) not in saved and str(data) not in saved
    assert json.loads(saved)["file"] == "gene_expression/q/report.md"
