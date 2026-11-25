"""Offline bridge contracts; no containers, network, scanpy or real h5ad."""
import json
import re
import shlex
from pathlib import Path

import pytest

from aiscientist.agents import atlas_expression as atlas, skills, skills_registry
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.mcp_bridge import load_servers
from aiscientist.agents.registry import build_scientist_catalog
from aiscientist.gateway.executor import ExecResult

_SERVER = load_servers()[0]
ATLAS_TOOLS = {_SERVER.planner_name(entry["name"]): entry["inputSchema"] for entry in _SERVER.tools}

FIXTURE = Path(__file__).parent / "fixtures" / "atlas_expression"
IMAGE = ("docker://ghcr.io/lijinbio/mcp-atlas-expression-exploration@sha256:"
         "725b31a8cc4f19472144bd6058d9736823718a753a3edf68b1e279f497a6fbaf")
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


def _obs_roles(tmp_path, monkeypatch, capsys, columns, filename="atlas.h5ad", categories=None):
    """Run the in-image column script on a fake atlas; returns (stdout list, stderr)."""
    h5py = pytest.importorskip("h5py")
    path = tmp_path / filename
    with h5py.File(path, "w") as f:
        obs = f.create_group("obs")
        obs.attrs["_index"] = "cell_names"
        obs.create_dataset("cell_names", data=[b"cell1"])
        obs.create_dataset("n_counts", data=[1.0])  # numeric: never a grouping column
        for column in columns:
            if categories and column in categories:
                group = obs.create_group(column)
                group.create_dataset("categories", data=[f"c{i}".encode() for i in range(categories[column])])
                group.create_dataset("codes", data=[0])
            else:
                obs.create_dataset(column, data=[b"value"])
    runner = Runner()
    runner.dataset_path = "/shared/heca/v2026.11/" + filename
    monkeypatch.setenv("AISCIENTIST_ATLAS_FILES", "heca/v2026.11=" + runner.dataset_path)
    monkeypatch.setenv("AISCIENTIST_DATASET", str(path))
    prepared = atlas.AtlasAdapter(planner_name=lambda name: name).prepare(
        {"genes": ["RHO"], "atlas": filename}, runner, tool="submit_query")
    assert prepared.arguments == {"genes": ["RHO"]}
    script = prepared.setup.split("<<'AISCI_ATLAS_OBS'\n", 1)[1].split("\nAISCI_ATLAS_OBS", 1)[0]
    assert prepared.setup.index("AISCI_ATLAS_OBS\n)") < prepared.setup.index("export ATLAS_OBS_COLS")
    exec(compile(script, "atlas-obs-setup", "exec"), {"__name__": "__main__"})
    out = capsys.readouterr()
    return out.out.strip().split(","), out.err


def test_obs_roles_complete_file_sends_the_four_server_positions(tmp_path, monkeypatch, capsys):
    # The server reads class, cell type, region, donor by position; extras are not needed.
    cols, err = _obs_roles(tmp_path, monkeypatch, capsys,
                           ["majorclass", "celltype", "tissue", "donor", "sampleid"], "HRCA_snRNA_allcells.h5ad")
    assert cols == ["majorclass", "celltype", "tissue", "donor"]
    assert "class=majorclass, cell type=celltype, region=tissue, donor=donor" in err


def test_obs_roles_use_the_files_own_synonym_names(tmp_path, monkeypatch, capsys):
    cols, _ = _obs_roles(tmp_path, monkeypatch, capsys,
                         ["Cell_Class", "cell_type", "anatomical_region", "donor_id", "sample_id"])
    assert cols == ["Cell_Class", "cell_type", "anatomical_region", "donor_id"]


@pytest.mark.parametrize("name,present,expected", [
    # Shapes of the issue-151 HECA files: a missing role never shifts the others.
    ("HRCA_scRNA", ["majorclass", "tissue", "donor", "sampleid", "subclass"],
     ["majorclass", "subclass", "tissue", "donor"]),
    ("Iris_scRNA", ["majorclass", "tissue", "sampleid", "cluster"],
     ["majorclass", "cluster", "tissue", "sampleid"]),
    ("Lens_snRNA", ["tissue", "sampleid", "cell_annotation", "broad_lineage"],
     ["broad_lineage", "cell_annotation", "tissue", "sampleid"]),
    ("RPEChoroid_snRNA", ["majorclass", "celltype", "sampleid", "anatomical_site"],
     ["majorclass", "celltype", "anatomical_site", "sampleid"]),
])
def test_obs_roles_missing_role_takes_the_files_matching_column(tmp_path, monkeypatch, capsys,
                                                                 name, present, expected):
    cols, _ = _obs_roles(tmp_path, monkeypatch, capsys, present, name + "_allcells.h5ad")
    assert cols == expected
    assert len(set(cols)) == 4


# The text and category columns of the HECA v2026.11.1 files whose names differ from the server's
# defaults, with their category counts, read from the files on HPC3.
HECA_OBS = {
    "HRCA_scRNA": ({"age": 30, "donor": 20, "gender": 3, "group1": 21, "group2": 8, "group3": 14,
                    "majorclass": 10, "race": 5, "sampleid": 51, "tissue": 3},
                   ["majorclass", "group1", "tissue", "donor"]),
    "Iris_scRNA": ({"age": 24, "dataset": 2, "donorid": 27, "loh_cell_type": 18, "majorclass": 11,
                    "race": 5, "sampleid": 34, "sex": 2, "technology": 1, "tissue": 5},
                   ["majorclass", "loh_cell_type", "tissue", "donorid"]),
    "Iris_snRNA": ({"age": 12, "dataset": 2, "donorid": 14, "majorclass": 9, "race": 3, "sampleid": 23,
                    "sanes_cell_type": 10, "sex": 3, "technology": 1, "tissue": 2},
                   ["majorclass", "sanes_cell_type", "tissue", "donorid"]),
    "Lens_scRNA": ({"age": 11, "cell_type": 6, "dataset": 1, "donorid": 11, "race": 4, "sampleid": 11,
                    "sex": 2, "technology": 1, "tissue": 1},
                   ["dataset", "cell_type", "tissue", "donorid"]),
    "Lens_snRNA": ({"age": 24, "cell_type": 6, "dataset": 2, "donorid": 31, "race": 5, "sampleid": 32,
                    "sanes_cell_type": 6, "sex": 3, "technology": 1, "tissue": 1},
                   ["technology", "cell_type", "tissue", "donorid"]),
    "RPEChoroid_scRNA": ({"Age": 31, "Chemistry": 4, "Donor": 65, "Ethnicity": 3, "Gender": 3,
                          "Region": 4, "Tissue": 6, "majorclass": 15, "sampleid": 92, "subclass": 27},
                         ["majorclass", "subclass", "Tissue", "Donor"]),
    "RPEChoroid_snRNA": ({"Donor": 53, "Region": 2, "Tissue": 1, "batch": 6, "celltype": 50,
                          "majorclass": 17, "sample": 82, "sampleid": 82},
                         ["majorclass", "celltype", "Tissue", "Donor"]),
}


@pytest.mark.parametrize("name", sorted(HECA_OBS))
def test_obs_roles_on_the_heca_files_columns(tmp_path, monkeypatch, capsys, name):
    # A file without a cell-type column never gets a sample, donor or demographic column as its
    # cell types (HRCA_scRNA: group1, not its 51 sample ids); Lens has no class column, so its
    # class is a column with one or two values, which only pools the file's cells.
    columns, expected = HECA_OBS[name]
    cols, _ = _obs_roles(tmp_path, monkeypatch, capsys, list(columns), name + "_allcells.h5ad",
                         categories=columns)
    assert cols == expected


def test_obs_roles_never_take_sample_columns_for_cell_roles(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit, match="four distinct"):
        _obs_roles(tmp_path, monkeypatch, capsys, ["majorclass", "tissue", "donor", "sampleid", "age"],
                   categories={"majorclass": 5, "tissue": 2, "donor": 9, "sampleid": 40, "age": 30})


def test_obs_roles_unnamed_annotations_fill_by_category_count(tmp_path, monkeypatch, capsys):
    # No name hints at all: class and region take the fewest categories, cell type and donor the most.
    cols, _ = _obs_roles(tmp_path, monkeypatch, capsys, ["a", "b", "c", "d"],
                         categories={"a": 3, "b": 40, "c": 1, "d": 12})
    assert cols == ["c", "b", "a", "d"]


def test_obs_roles_too_few_columns_stop_with_the_files_columns(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit, match=r"Lens_scRNA_allcells.h5ad has 2 .*\((sampleid, tissue|tissue, sampleid)\).*four distinct"):
        _obs_roles(tmp_path, monkeypatch, capsys, ["tissue", "sampleid"], "Lens_scRNA_allcells.h5ad")


@pytest.mark.parametrize("copied", [False, True])
def test_issue151_bare_primary_filename_is_accepted_but_other_files_are_refused(monkeypatch, copied):
    runner = Runner()
    runner.dataset_path = SESSION_COPY if copied else SERVER_FILE
    runner.dataset_source = SERVER_FILE if copied else ""
    monkeypatch.setenv("AISCIENTIST_ATLAS_FILES", "heca/v1=" + SERVER_FILE + ";heca/v1=/shared/other.h5ad")
    adapter = atlas.AtlasAdapter(planner_name=lambda name: name)
    assert adapter.prepare({"atlas": "HRCA_snRNA_allcells.h5ad"}, runner).arguments == {}
    for argument in ("other.h5ad", "/shared/other.h5ad", "/unattached/HRCA_snRNA_allcells.h5ad"):
        with pytest.raises(ValueError, match="attached primary dataset"):
            adapter.prepare({"atlas": argument}, runner)


def test_release_metadata_checksum_tier_and_exposure(release, monkeypatch):
    skill = skills.get_skill(atlas.SKILL)
    assert (skill.status, skill.registry_release) == ("community", "v0.2.0")
    assert (skill.image, skill.cpus, skill.mem_gb, skill.time_limit) == (IMAGE, 4, 32, "04:00:00")
    assert set(ATLAS_TOOLS) <= tools(Runner()).keys()
    assert not set(ATLAS_TOOLS) & tools(None).keys()
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core,verified")
    assert not set(ATLAS_TOOLS) & tools(Runner()).keys()
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_ALLOWED_TIERS", "core,verified,community")
    bundle = release / "aiscientist-skills-0.2.0.zip"
    bundle.write_bytes(bundle.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        skills_registry.check_release(release, "v0.2.0")
    assert not set(ATLAS_TOOLS) & tools(Runner()).keys()


@pytest.mark.parametrize("value", ["", "heca/latest=/shared/heca/x.h5ad", "heca/v1=relative/x.h5ad"])
@pytest.mark.usefixtures("model_written_code")
def test_without_a_configured_atlas_the_tools_are_not_offered(release, monkeypatch, value):
    """A deployment that lists no atlas would offer nine tools that always fail."""
    monkeypatch.setenv("AISCIENTIST_ATLAS_FILES", value)
    catalog = tools(Runner())
    assert not set(ATLAS_TOOLS) & catalog.keys()
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
        assert (kwargs["cpus"], kwargs["mem_gb"], kwargs["time_limit"]) == ((2, 16, "00:30:00") if index == 0 else (4, 32, "04:00:00"))
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
        "plot_featureplot": {"query_id"}, "atlas_write_report": {"query_id"}, "list_queries": set(),
    }
    for name, fields in expected.items():
        assert set(catalog[name].parameters["properties"]) == fields
    from aiscientist.agents.research_lab import _primary_tool
    assert _primary_tool("Resolve with `resolve_genes`.") == "resolve_genes"
    assert _primary_tool("Extract with `submit_query`.") == "submit_query"


def test_atlas_flow_hints_use_all_configured_planner_names(release):
    from dataclasses import replace
    from aiscientist.agents.mcp_bridge import build_mcp_tools
    from aiscientist.agents.research_lab import make_run_in_environment_tool
    server = replace(_SERVER, prefix="atlas_", aliases={"resolve_genes": "resolve", "write_report": "query_report"})
    runner = Runner()
    catalog = {t.name: t for t in build_mcp_tools(runner, make_run_in_environment_tool(runner), servers=(server,))}
    description = catalog["atlas_resolve"].description
    assert "atlas_resolve -> atlas_submit_query -> atlas_query_status -> atlas_get_summary" in description
    assert "atlas_plot_dotplot, atlas_plot_violin, atlas_plot_featureplot -> atlas_query_report" in description
    assert catalog["atlas_resolve"].executor({"genes": "LRAT"}, None)["status"] == "ok"
    assert " call resolve_genes " in runner.calls[0][0]


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
    monkeypatch.setenv("AISCI_MCP_RESULT", str(tmp_path / ".atlas-result.json"))
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
    for value in ("--cpus-per-task=4", "--mem=32G", "--time=04:00:00",
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
                       "ATLAS_H5AD": source, "AISCI_MCP_RESULT": raw,
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


@pytest.mark.usefixtures("model_written_code")
def test_direct_skill_refused_even_with_model_override_arguments(release):
    runner = Runner()
    tool = tools(runner)["run_in_environment"]
    out = tool.executor({"skill": atlas.SKILL.upper(), "command": "true",
                         "_mcp_server": {"image": "fake"}, "_resources": [1, 1, "00:01:00"]}, None)
    assert out["status"] == "error"
    assert all(name in out["error"] for name in ATLAS_TOOLS)
    assert not runner.calls
    assert not {"_mcp_server", "_resources", "_tool_cache"} & tool.parameters["properties"].keys()


@pytest.mark.parametrize("other_server", [False, True])
def test_omitting_atlas_registration_does_not_reopen_its_shell(release, tmp_path, monkeypatch, other_server):
    from aiscientist.agents.research_lab import make_run_in_environment_tool
    from test_mcp_bridge import write_config

    if other_server:
        path = write_config(tmp_path)
    else:
        path = tmp_path / "mcp_servers.json"
        path.write_text(json.dumps({"schema_version": 1, "servers": []}))
    monkeypatch.setenv("AISCIENTIST_MCP_CONFIG", str(path))
    runner = Runner()
    direct = make_run_in_environment_tool(runner)
    result = direct.executor({"skill": atlas.SKILL, "command": "true"}, None)
    assert result["status"] == "error" and "Atlas MCP" in result["error"]
    assert not runner.calls


@pytest.mark.parametrize("changes", [
    {"adapter": "json"}, {"adapter": None}, {"skill": "unrelated-skill"},
    {"name": "other", "release_tag": "other-v0.0.4"},
])
def test_atlas_binding_cannot_select_a_generic_adapter(tmp_path, changes):
    from aiscientist.agents.mcp_bridge import DEFAULT_CONFIG
    cfg = json.loads(DEFAULT_CONFIG.read_text())
    cfg["servers"][0].update(changes)
    if cfg["servers"][0]["adapter"] is None:
        cfg["servers"][0].pop("adapter")
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="Atlas"):
        load_servers(path)


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
    cli = next(line for line in command.splitlines() if line.startswith("aiscientist-mcp "))
    assert json.loads(shlex.split(cli)[7]) == {"genes": GENES}  # source host paths never reach the server
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


@pytest.mark.parametrize("name", sorted(set(ATLAS_TOOLS) - {"submit_query"}))
def test_light_tools_use_small_jobs_and_the_users_tool_cache(release, name):
    """The HGNC table lives in this HPC3 user's tool cache, whose parent is made 3775 as the
    analysis jobs' is: a folder one member made in a lab-shared place would lock the others out."""
    from test_run_in_environment import _HPC, _executor
    hpc = _HPC()
    runner = _executor(hpc, tool_cache_dir=CACHE)
    runner.dataset_path = Runner.dataset_path
    args = {key: "q" for key in ATLAS_TOOLS[name].get("required", [])}
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
                       "ATLAS_H5AD": source, "AISCI_MCP_RESULT": raw,
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


# --- RCHENLAB #151 item 4 (C3): an Atlas run reads its own result files without run_code -------------

C3_QUERY = "HRCA_snRNA_allcells__LRAT_RLBP1_RHO_ABCA4"
C3_STEP = ("**Cell-type expression summary** — Rank the cell types for LRAT, RLBP1, RHO and ABCA4 with "
           "`get_summary` at level celltype.")


def _summary_table(n_types=160):
    """summary_celltype.tsv as the server writes it: one row per gene and cell type."""
    head = ("gene\tcelltype\tn_cells\tn_expressing\tpct_expressing\tmean_lognorm\tpseudobulk_cp10k\t"
            "n_donors\tdonor_median_cp10k\tdonor_q25_cp10k\tdonor_q75_cp10k\tfrac_donors_detected\t"
            "rank_pseudobulk")
    rows = [f"{gene}\tType_{i:03d}\t{1000 + i}\t{i}\t{i / 10:.2f}\t0.01\t{(n_types - i) / 100:.3f}\t"
            f"{10 + i % 90}\t0.1\t0.05\t0.2\t0.5\t{i + 1}"
            for gene in ("LRAT", "RLBP1", "RHO", "ABCA4") for i in range(n_types)]
    return "\n".join([head, *rows]) + "\n", rows


def _collected_summary(tmp_path, monkeypatch, capsys, artifacts, table, rows):
    """Run the image's real collector on a get_summary answer: what the tool hands the model, and the
    files it leaves in the run's HPC3 artifacts."""
    query = tmp_path / "server-data" / "atlas-expression-exploration" / C3_QUERY
    query.mkdir(parents=True)
    (query / "state.json").write_text("{}")
    (query / "summary_celltype.tsv").write_text(table)
    for name in ("dotplot_majorclass.png", "dotplot_majorclass.pdf"):
        (query / name).write_bytes(b"\x89PNG plot")
    fields = table.splitlines()[0].split("\t")
    answer = {"query_id": C3_QUERY, "level": "celltype", "table": str(query / "summary_celltype.tsv"),
              "rows": [dict(zip(fields, row.split("\t"))) for row in rows]}
    raw, source = tmp_path / "result.json", tmp_path / "atlas.h5ad"
    raw.write_text(json.dumps(answer))
    source.write_text("atlas; never parsed")
    for key, value in {"AISCI_DATA_DIR": query.parent.parent, "AISCIENTIST_ARTIFACTS": artifacts,
                       "ATLAS_H5AD": source, "AISCI_MCP_RESULT": raw,
                       "AISCI_ATLAS_RELEASE": "heca/v2026.11"}.items():
        monkeypatch.setenv(key, str(value))
    exec(atlas.COLLECT, {})
    return capsys.readouterr().out


def _hpc3_login():
    """The login node over this machine's files: GNU ``stat -c`` as HPC3 answers it (macOS has none)."""
    from test_hpc_shell import LocalReadRemote

    class Login(LocalReadRemote):
        def exec(self, command, timeout=60):
            if command.startswith("stat -c "):
                path = Path(shlex.split(command)[3])
                if not path.exists():
                    return ExecResult(command, 0, f"stat: cannot statx '{path}'", "")
                kind = "regular file" if path.is_file() else "directory"
                return ExecResult(command, 0, f"{path}|{kind}|{path.stat().st_size}|2026-11-23 14:30:00|"
                                              "alice|-rw-r--r--", "")
            return super().exec(command, timeout)
    return Login()


C3_QUERY_STEP = ("**Full-atlas expression query** — Resolve LRAT, CRALBP, rho and ABCR with "
                 "`resolve_genes`, then stream the atlas with `submit_query` and `query_status`.")
REFUSED_READ = ("run_code", {"code": "import pandas as pd\nprint(pd.read_csv("
                                     f"'gene_expression/{C3_QUERY}/summary_celltype.tsv', sep='\\t'))"})


def _c3_tools(release, tmp_path, monkeypatch, capsys):
    """C3's tools with model-written code off: the Atlas tools on a runner that answers as the image
    does, and the lookups on the run's HPC3 folder, where the collector left the result files."""
    from aiscientist.hpc import shell as hs

    hpc3 = tmp_path / "hpc3-run"
    artifacts, work = hpc3 / "analysis" / "artifacts", hpc3 / "analysis" / "work"
    work.mkdir(parents=True)
    table, rows = _summary_table()
    summary_stdout = _collected_summary(tmp_path, monkeypatch, capsys, artifacts, table, rows)
    answers = {
        "resolve_genes": {"resolution": RESOLUTION},
        "submit_query": {"query_id": C3_QUERY, "state": "COMPLETED", "resolution": RESOLUTION},
        "query_status": {"query_id": C3_QUERY, "state": "COMPLETED", "returncode": 0},
    }
    runner = Runner()

    def run_in_image(command, image, **kwargs):
        tool = re.search(r"AISCI_MCP_TOOL=(\w+)", command).group(1)
        runner.calls.append(tool)
        if tool == "get_summary":
            return {"status": "ok", "returncode": 0, "stdout": summary_stdout}
        return {"status": "ok", "returncode": 0, "stdout": json.dumps({"result": answers[tool], "atlas": {
            "path": Runner.dataset_path, "version": "heca/v2026.11", "size_bytes": 1, "mtime_ns": 1}})}
    runner.run_in_image = run_in_image
    shell = hs.HpcShell(remote=_hpc3_login(), home=str(tmp_path / "home"), artifacts_dir=str(artifacts),
                        work_dir=str(work), workspace=hs.HpcWorkspace(read_roots=(str(hpc3),)))
    return build_scientist_catalog(code_executor=runner, hpc_shell=shell), runner, table


def _scripted(plan):
    """A model that makes the calls ``plan(results)`` returns, one per turn, given the tool results it
    has seen so far in this step."""
    def chat(messages, tools):
        seen = [m.get("content") or "" for m in messages if m.get("role") == "tool"]
        name, args = plan(seen)
        return {"content": "", "tool_calls": [{"id": f"c{len(seen)}", "type": "function",
                                               "function": {"name": name, "arguments": json.dumps(args)}}]}
    return chat


def test_c3_reads_its_summary_tables_with_the_lookups_when_run_code_is_off(release, tmp_path, monkeypatch,
                                                                          capsys):
    """C3 on production (v0.4.25, run bff4c779876c) read summary_celltype.tsv with run_code, because
    get_summary returns only the rows that fit. Replayed with model-written code off: run_code is not
    offered and the call is refused; the model reads the table get_summary names with read_text, which
    resolves it in the run's HPC3 artifacts where the collector put it, a page at a time, every page
    seen whole, and the step ends with its answer."""
    from aiscientist.agents import code_policy
    from aiscientist.agents.research_harness import ResearchHarness
    from aiscientist.hpc.shell import MAX_PAGE_CHARS

    assert not code_policy.model_written_code()
    catalog, runner, table = _c3_tools(release, tmp_path, monkeypatch, capsys)
    names = {t.name for t in catalog}
    assert not names & code_policy.WITHHELD_TOOLS
    assert {"get_summary", "read_text", "stat_path", "list_dir"} <= names
    assert len(json.dumps(table)) > 2 * MAX_PAGE_CHARS, "the table is longer than two pages"
    ctx = HarnessContext(workspace=tmp_path)

    query_calls = [("resolve_genes", {"genes": "LRAT, CRALBP, rho, ABCR"}),
                   ("submit_query", {"genes": "LRAT,RLBP1,RHO,ABCA4"}),
                   ("query_status", {"query_id": C3_QUERY}),
                   ("finish", {"answer": f"Query {C3_QUERY} completed."})]
    query = ResearchHarness(catalog=catalog, chat_fn=_scripted(lambda seen: query_calls[len(seen)])).run(
        C3_QUERY_STEP, ctx)
    assert query.final_answer == f"Query {C3_QUERY} completed." and not query.errors

    final = "RLBP1 ranks Type_000 first (1.600 CP10k); the full table has 640 rows."

    def summary_plan(seen):
        if not seen:
            return "get_summary", {"query_id": C3_QUERY, "level": "celltype", "top": 500}
        if len(seen) == 1:
            return REFUSED_READ          # what C3 did next
        last = json.loads(seen[-1])
        if len(seen) == 2:               # refused: read the table get_summary named, as it named it
            return "read_text", {"path": json.loads(seen[0])["full_table"]}
        if "next_offset" in last:
            return "read_text", {"path": last["path"], "offset": last["next_offset"]}
        if "exists" not in last:
            return "stat_path", {"path": f"gene_expression/{C3_QUERY}/dotplot_majorclass.png"}
        return "finish", {"answer": final}
    fed: list[str] = []

    def chat(messages, tools):
        fed[:] = [m.get("content") or "" for m in messages if m.get("role") == "tool"]
        return _scripted(summary_plan)(messages, tools)
    summary = ResearchHarness(catalog=catalog, chat_fn=chat).run(C3_STEP, ctx)

    assert runner.calls == ["resolve_genes", "submit_query", "query_status", "get_summary"], "no code ran"
    shown = json.loads(fed[0])
    assert shown["rows_omitted"] > 0 and shown["full_table"] == f"gene_expression/{C3_QUERY}/summary_celltype.tsv"
    assert "get_summary with gene" in shown["rows_note"] and "read_text" in shown["rows_note"]
    refusal = json.loads(fed[1])["error"]
    assert "'run_code' is turned off on this deployment" in refusal and "read_text" in refusal
    assert [e["tool"] for e in summary.errors] == ["run_code"]
    pages = [json.loads(text) for text in fed[2:-1]]   # every page reached the model whole
    assert len(pages) >= 3 and all("text" in page for page in pages)
    assert "".join(page["text"] for page in pages) == table, "every row, the omitted ones included"
    assert json.loads(fed[-1])["exists"] is True
    assert summary.final_answer == final


def test_a_model_that_keeps_calling_run_code_ends_the_step_on_the_result_it_has(release, tmp_path,
                                                                                 monkeypatch, capsys):
    """If the model does not switch to the lookups and asks for run_code again, the refusals count as
    wasted turns: the step ends on the result get_summary already gave (``done_early``), after two of
    them, instead of spending the step's whole budget on refusals."""
    from aiscientist.agents.research_harness import ResearchHarness

    catalog, runner, _ = _c3_tools(release, tmp_path, monkeypatch, capsys)
    turns: list[int] = []

    def plan(seen):
        turns.append(len(seen))
        return ("get_summary", {"query_id": C3_QUERY, "level": "celltype", "top": 500}) if not seen else REFUSED_READ
    summary = ResearchHarness(catalog=catalog, chat_fn=_scripted(plan)).run(
        C3_STEP, HarnessContext(workspace=tmp_path))
    assert runner.calls == ["get_summary"]
    assert summary.stop_reason == "done_early" and len(turns) == 3
    assert [s["tool"] for s in summary.steps if s.get("ok")] == ["get_summary"]
    assert C3_QUERY in summary.final_answer, "the step reports what get_summary returned"


def test_the_same_refusal_three_times_ends_a_step_that_has_no_result_yet(tmp_path):
    from aiscientist.agents.research_harness import ResearchHarness, default_catalog

    turns: list[int] = []

    def plan(seen):
        turns.append(len(seen))
        return REFUSED_READ
    result = ResearchHarness(catalog=list(default_catalog()), chat_fn=_scripted(plan)).run(
        C3_STEP, HarnessContext(workspace=tmp_path))
    assert result.stop_reason == "repeated_tool_errors" and len(turns) == 3
