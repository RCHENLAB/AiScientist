"""Execute analysis bridge commands with a fixture CLI, including file collection."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

from aiscientist.agents import mcp_bridge as bridge
from aiscientist.agents.mcp_analysis import AnalysisAdapter, PREFIXES, setting_paths
from aiscientist.agents.research_harness import HarnessContext
from test_mcp_bridge import build, write_config, write_skill
from test_mcp_bridge_command import LocalCommandRunner


class AnalysisRunner(LocalCommandRunner):
    def __init__(self, root):
        super().__init__(root)
        self.checkpoints = root / "run checkpoints"
        self.dataset_root = root / "attached folder"
        self.checkpoints.mkdir()
        self.dataset_root.mkdir()
        self.dataset_path = self.dataset_root / "sample.h5ad"
        self.dataset_path.write_bytes(b"bound fixture")
        self.inputs = [{"name": "regions", "path": str(root / "attached intervals.bed")}]
        Path(self.inputs[0]["path"]).write_text("1\t0\t100\n")
        self.work_dir = str(self.checkpoints)
        self.extra_env = {}
        cli = self.bin / "aiscientist-mcp"
        text = cli.read_text().replace('"argv": sys.argv[1:],', '"env": dict(os.environ), "argv": sys.argv[1:],')
        cli.write_text(text)

    def run_in_image(self, command, image, **kwargs):
        env = {"AISCIENTIST_WORK": str(self.checkpoints), "AISCIENTIST_DATASET": str(self.dataset_path),
               "AISCIENTIST_DATASET_ROOT": str(self.dataset_root), "AISCIENTIST_INPUTS": json.dumps(self.inputs),
               **self.extra_env}
        prefix = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in env.items())
        return super().run_in_image(prefix + command, image, **kwargs)


def config(root, server="cellqc", tool="run_cellqc", *, settings=(), schema=None, **overrides):
    schema = schema or {"type": "object", "properties": {
        "analysis_id": {"type": "string"}, "task_id": {"type": "string"}, "dataset": {"type": "string"},
        "dataset_paths": {"type": "array", "items": {"type": "string"}},
        "input": {"type": "string"}, "model_dir": {"type": "string"}}, "additionalProperties": False}
    return write_config(root, entries=[{"name": tool, "description": "Run analysis.", "inputSchema": schema}],
                        overrides={"name": server, "release_tag": server + "-v1.2.3", "prefix": "mcp_",
                                   "adapter": "analysis", "entrypoint": server + "-mcp", "resources": {},
                                   "settings_env": list(settings), **overrides})


@pytest.fixture
def analysis_bridge(tmp_path, monkeypatch, skill_release):
    source = tmp_path / "skills"
    write_skill(source)
    skill_release(source)
    runner = AnalysisRunner(tmp_path / "image runner")
    ctx = HarnessContext(workspace=tmp_path / "run")

    def make(server="cellqc", tool="run_cellqc", **kwargs):
        cfg = config(tmp_path, server, tool, **kwargs)
        monkeypatch.setenv("AISCIENTIST_MCP_CONFIG", str(cfg))
        return build(runner)[0]
    return runner, ctx, make


def fixture_result(runner, server="cellqc", analysis="default"):
    output = runner.work / server / analysis / "jobs" / "task-one" / "output"
    artifacts, work = output / "artifacts", output / "work"
    (artifacts / "tables").mkdir(parents=True)
    work.mkdir()
    table = artifacts / "tables" / "qc.csv"
    table.write_text("library,cells\nsample,90\n")
    checkpoint = work / "adata_qc.h5ad"
    checkpoint.write_bytes(b"real fixture checkpoint bytes")
    value = {"status": "ok", "state": "COMPLETED", "analysis_id": analysis, "task_id": "task-one",
             "artifact_root": str(artifacts), "checkpoint_path": str(checkpoint), "checkpoint": checkpoint.name,
             "table": str(table), "relative_table": "tables/qc.csv", "cells_after": 90}
    runner.fixture.write_text(json.dumps(value))
    return value, output


def test_collector_copies_tables_checkpoints_and_preserves_json(analysis_bridge):
    runner, ctx, make = analysis_bridge
    tool = make()
    value, _ = fixture_result(runner)
    result = tool.executor({"dataset": str(runner.dataset_path)}, ctx)
    assert result["status"] == "ok", result
    assert result["checkpoint"] == "adata_qc.h5ad"
    assert (runner.checkpoints / "adata_qc.h5ad").read_bytes() == b"real fixture checkpoint bytes"
    target = "mcp/cellqc/default/tables/qc.csv"
    assert result["table"] == result["relative_table"] == target
    assert (runner.artifacts / target).read_text().endswith("sample,90\n")
    evidence = json.loads((runner.artifacts / result["full_result"]).read_text())
    assert evidence["cells_after"] == value["cells_after"]
    assert str(runner.root) not in json.dumps(result)
    assert str(runner.root) not in json.dumps(evidence)
    recorded = json.loads(runner.capture.read_text())
    assert json.loads(recorded["argv"][-1]) == {}
    assert recorded["env"]["CELLQC_DATASET"] == str(runner.dataset_root)
    assert recorded["env"]["CELLQC_READ_ROOTS"].split(":") == [str(runner.dataset_root), str(runner.dataset_path), runner.inputs[0]["path"]]


def test_cellqc_multispecies_handoff_is_readable_by_generated_scrna_call(analysis_bridge):
    runner, ctx, make = analysis_bridge
    value, output = fixture_result(runner)
    for species in ("mouse", "human"):
        (output / "work" / f"adata_qc.{species}.h5ad").write_bytes(species.encode())
    value["checkpoints"] = {species: {"checkpoint": f"adata_qc.{species}.h5ad", "cells": 45}
                            for species in ("mouse", "human")}
    runner.fixture.write_text(json.dumps(value))
    first = make().executor({}, ctx)
    assert first["status"] == "ok", first
    assert first["exported_checkpoints"] == ["adata_qc.h5ad", "adata_qc.mouse.h5ad", "adata_qc.human.h5ad"]
    # The next actual shell invocation reads the handed-off checkpoint through
    # SCRNA_CHECKPOINT_ROOT, as the independent scRNA worker does.
    cli = runner.bin / "aiscientist-mcp"
    cli.write_text(cli.read_text().replace('sys.stdout.write(', '''root = Path(os.environ.get("SCRNA_CHECKPOINT_ROOT", "/unused"))
if os.environ["AISCI_MCP_SERVER"] == "scrna-analysis":
    assert (root / "adata_qc.mouse.h5ad").read_bytes() == b"mouse"
sys.stdout.write('''))
    fixture_result(runner, "scrna-analysis")
    second = make("scrna-analysis", "run_clustering").executor({"input": "adata_qc.mouse.h5ad"}, ctx)
    assert second["status"] == "ok", second
    assert json.loads(runner.capture.read_text())["env"]["SCRNA_CHECKPOINT_ROOT"] == str(runner.checkpoints)


def full_cellqc_result(runner):
    value, output = fixture_result(runner)
    full = output / "result" / "cellqc"
    (full / "mouse").mkdir(parents=True)
    files = {"metrics.csv": b"sampleid,cells\nmouse,90\n",
             "mouse/library.h5ad": b"complete library matrix", "report.html": b"<html>QC</html>"}
    for name, content in files.items():
        (full / name).write_bytes(content)
    artifacts = output / "artifacts"
    (artifacts / "data").mkdir()
    (artifacts / "result").mkdir()
    (artifacts / "result" / "report.html").write_bytes(files["report.html"])
    (artifacts / "tables" / "cellqc_metrics.csv").write_bytes(files["metrics.csv"])
    (artifacts / "data" / "cellqc_result.json").write_text(json.dumps({
        "path": "/untrusted/pointer/path", "files": [{"name": "fake.h5ad", "size": 999}],
        "libraries": ["mouse"], "created_at": "2026-11-12T00:00:00Z"}))
    value["result_directory"] = str(full)
    runner.fixture.write_text(json.dumps(value))
    return value, output, files


def test_complete_cellqc_results_and_real_gateway_pointer_are_published(analysis_bridge):
    runner, ctx, make = analysis_bridge
    value, _, files = full_cellqc_result(runner)
    result = make().executor({}, ctx)
    assert result["status"] == "ok", result
    root = runner.artifacts.parent / "result" / "cellqc"
    assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()} == files
    pointer = json.loads((runner.artifacts / "data/cellqc_result.json").read_text())
    assert pointer["path"] == str(root)
    assert pointer["files"] == [{"name": name, "size": len(files[name])} for name in sorted(files)]
    assert pointer["size_bytes"] == sum(map(len, files.values()))
    assert pointer["matrices"] == ["mouse/library.h5ad"]
    assert (runner.artifacts / "tables/cellqc_metrics.csv").read_bytes() == files["metrics.csv"]
    assert (runner.artifacts / "result/report.html").read_bytes() == files["report.html"]
    public = json.loads((runner.artifacts / "mcp/cellqc/default/data/cellqc_result.json").read_text())
    assert public["path"] == "result/cellqc" and str(runner.root) not in json.dumps(public)
    assert result["result_directory"] == "result/cellqc"
    assert str(runner.root) not in json.dumps(result)
    assert value["result_directory"] not in (runner.artifacts / result["full_result"]).read_text()


@pytest.mark.parametrize("mutation", ["source_link", "nested_link", "target_link", "outside"])
def test_complete_result_confinement_is_checked_before_any_copy(analysis_bridge, mutation):
    runner, ctx, make = analysis_bridge
    value, output, _ = full_cellqc_result(runner)
    outside = runner.root / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"unrelated")
    full = output / "result/cellqc"
    if mutation == "source_link":
        alias = output / "linked_results"
        alias.symlink_to(full, target_is_directory=True)
        value["result_directory"] = str(alias)
    elif mutation == "nested_link":
        (full / "mouse" / "secret").symlink_to(outside / "private")
    elif mutation == "target_link":
        (runner.artifacts.parent / "result").symlink_to(outside, target_is_directory=True)
    else:
        value["result_directory"] = str(outside)
    runner.fixture.write_text(json.dumps(value))
    result = make().executor({}, ctx)
    assert result["status"] == "error", result
    assert not (runner.artifacts / "tables/cellqc_metrics.csv").exists()
    assert not (runner.checkpoints / "adata_qc.h5ad").exists()
    assert (outside / "private").read_bytes() == b"unrelated"


def test_report_aliases_track_success_without_erasing_other_tools_or_user_changes(analysis_bridge):
    runner, ctx, make = analysis_bridge
    value, output = fixture_result(runner, "scrna-analysis")
    tables = output / "artifacts/tables"
    for name in ("old.csv", "user.csv"):
        (tables / name).write_text("old\n")
    assert make("scrna-analysis", "run_clustering").executor({}, ctx)["status"] == "ok"
    (runner.artifacts / "tables/user.csv").write_text("user edited\n")
    (runner.artifacts / "tables/unrelated.csv").write_text("other tool\n")
    (tables / "old.csv").unlink()
    (tables / "user.csv").unlink()
    value["task_id"] = "second"
    runner.fixture.write_text(json.dumps(value))
    assert make("scrna-analysis", "run_clustering").executor({}, ctx)["status"] == "ok"
    assert not (runner.artifacts / "tables/old.csv").exists()
    assert (runner.artifacts / "tables/user.csv").read_text() == "user edited\n"
    assert (runner.artifacts / "tables/unrelated.csv").read_text() == "other tool\n"
    current = (runner.artifacts / "tables/qc.csv").read_text()
    (tables / "qc.csv").write_text("failed run\n")
    value.update(state="FAILED", status="error")
    runner.fixture.write_text(json.dumps(value))
    assert make("scrna-analysis", "run_clustering").executor({}, ctx)["status"] == "error"
    assert (runner.artifacts / "tables/qc.csv").read_text() == current


def test_polling_an_old_completed_task_does_not_replace_current_report_aliases(analysis_bridge):
    runner, ctx, make = analysis_bridge
    value, output = fixture_result(runner)
    assert make().executor({}, ctx)["status"] == "ok"
    current = (runner.artifacts / "tables/qc.csv").read_text()
    (output / "artifacts/tables/qc.csv").write_text("historical task\n")
    (output / "work/adata_qc.h5ad").write_bytes(b"historical checkpoint")
    value["task_id"] = "older-task"
    runner.fixture.write_text(json.dumps(value))
    assert make("cellqc", "cellqc_job_status").executor({"task_id": "older-task"}, ctx)["status"] == "ok"
    assert (runner.artifacts / "tables/qc.csv").read_text() == current
    assert (runner.checkpoints / "adata_qc.h5ad").read_bytes() == b"real fixture checkpoint bytes"


def test_cellqc_rerun_replaces_complete_tree_and_discards_stale_files(analysis_bridge):
    runner, ctx, make = analysis_bridge
    value, output, _ = full_cellqc_result(runner)
    assert make().executor({}, ctx)["status"] == "ok"
    (output / "result/cellqc/report.html").unlink()
    (output / "artifacts/result/report.html").unlink()
    value["task_id"] = "rerun"
    runner.fixture.write_text(json.dumps(value))
    assert make().executor({}, ctx)["status"] == "ok"
    assert not (runner.artifacts.parent / "result/cellqc/report.html").exists()
    assert not (runner.artifacts / "result/report.html").exists()
    pointer = json.loads((runner.artifacts / "data/cellqc_result.json").read_text())
    assert "report.html" not in {f["name"] for f in pointer["files"]}
    assert pointer["mcp_provenance"]["task_id"] == "rerun"


def test_complete_result_publication_and_rollback_stay_on_result_mount(analysis_bridge):
    """Execute the generated collector with work/result mounted as different devices."""
    runner, ctx, make = analysis_bridge
    value, output, files = full_cellqc_result(runner)
    hook = runner.root / "mount simulation"
    hook.mkdir()
    recorded = hook / "renames.jsonl"
    result_root = runner.artifacts.parent / "result"
    (hook / "sitecustomize.py").write_text(f'''
import errno, json, os
from pathlib import Path
original = os.replace
result_root = Path({str(result_root)!r})
def replace(source, target, *args, **kwargs):
    source, target = Path(source), Path(target)
    if source.is_dir() and (source.is_relative_to(result_root) or target.is_relative_to(result_root)):
        with open({str(recorded)!r}, "a") as stream:
            stream.write(json.dumps([str(source), str(target)]) + "\\n")
        if source.parent != result_root or target.parent != result_root:
            raise OSError(errno.EXDEV, "separate Apptainer bind mounts")
    return original(source, target, *args, **kwargs)
os.replace = replace
''')
    runner.extra_env["PYTHONPATH"] = str(hook)
    tool = make()
    assert tool.executor({}, ctx)["status"] == "ok"
    (output / "result/cellqc/report.html").unlink()
    (output / "artifacts/result/report.html").unlink()
    value["task_id"] = "rerun"
    runner.fixture.write_text(json.dumps(value))
    assert tool.executor({}, ctx)["status"] == "ok"
    assert (result_root / "cellqc/mouse/library.h5ad").read_bytes() == files["mouse/library.h5ad"]
    assert not (result_root / "cellqc/report.html").exists()
    renames = [json.loads(line) for line in recorded.read_text().splitlines()]
    assert len(renames) == 3  # first publish, prior-tree backup, replacement publish
    assert all(Path(source).parent == Path(target).parent == result_root for source, target in renames)
    assert not list(result_root.glob(".mcp-cellqc-*"))


def test_alias_ownership_prevents_cross_server_cleanup(analysis_bridge):
    runner, ctx, make = analysis_bridge
    cellqc, output = fixture_result(runner)
    assert make().executor({}, ctx)["status"] == "ok"
    _, scrna = fixture_result(runner, "scrna-analysis")
    (scrna / "artifacts/tables/qc.csv").write_text("latest scRNA table\n")
    assert make("scrna-analysis", "run_clustering").executor({}, ctx)["status"] == "ok"
    (output / "artifacts/tables/qc.csv").unlink()
    cellqc["task_id"] = "new-qc"
    runner.fixture.write_text(json.dumps(cellqc))
    assert make().executor({}, ctx)["status"] == "ok"
    assert (runner.artifacts / "tables/qc.csv").read_text() == "latest scRNA table\n"
    # Read-only metadata without an artifact tree also leaves published results intact.
    runner.fixture.write_text(json.dumps({"status": "ok", "analysis_id": "default"}))
    assert make("scrna-analysis", "describe").executor({}, ctx)["status"] == "ok"
    assert (runner.artifacts / "tables/qc.csv").read_text() == "latest scRNA table\n"


def test_cellqc_species_rerun_retires_only_unchanged_owned_checkpoints(analysis_bridge):
    runner, ctx, make = analysis_bridge
    value, output = fixture_result(runner)
    for species in ("human", "mouse"):
        (output / "work" / f"adata_qc.{species}.h5ad").write_bytes(species.encode())
    value["checkpoints"] = {s: {"checkpoint": f"adata_qc.{s}.h5ad"} for s in ("human", "mouse")}
    runner.fixture.write_text(json.dumps(value))
    assert make().executor({}, ctx)["status"] == "ok"
    # Keep a user-owned variant while dropping the previous run's absent species.
    (runner.checkpoints / "adata_user.h5ad").write_bytes(b"user variant")
    value.update(task_id="mouse-only", checkpoints={"mouse": {"checkpoint": "adata_qc.mouse.h5ad"}})
    runner.fixture.write_text(json.dumps(value))
    assert make().executor({}, ctx)["status"] == "ok"
    assert not (runner.checkpoints / "adata_qc.human.h5ad").exists()
    assert (runner.checkpoints / "adata_qc.mouse.h5ad").read_bytes() == b"mouse"
    assert (runner.checkpoints / "adata_user.h5ad").read_bytes() == b"user variant"


@pytest.mark.parametrize("mutation", ["outside", "other_analysis", "root_symlink", "nested_symlink", "checkpoint_symlink", "dest_symlink", "checkpoint_escape"])
def test_collector_rejects_unsafe_file_sources_and_destinations(analysis_bridge, mutation):
    runner, ctx, make = analysis_bridge
    value, output = fixture_result(runner)
    outside = runner.root / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("must never copy")
    if mutation == "outside":
        value["artifact_root"] = str(outside)
    elif mutation == "other_analysis":
        value["analysis_id"] = "someone-else"
    elif mutation == "root_symlink":
        path = output / "linked"
        path.symlink_to(output / "artifacts", target_is_directory=True)
        value["artifact_root"] = str(path)
    elif mutation == "nested_symlink":
        (output / "artifacts" / "secret").symlink_to(outside / "private.txt")
    elif mutation == "checkpoint_symlink":
        path = output / "work" / "adata_secret.h5ad"
        path.symlink_to(outside / "private.txt")
        value["checkpoint_path"] = str(path)
    elif mutation == "dest_symlink":
        (runner.artifacts / "mcp").symlink_to(outside, target_is_directory=True)
    elif mutation == "checkpoint_escape":
        value["checkpoints"] = {"mouse": {"checkpoint": "../../../../../../outside/private.txt"}}
    runner.fixture.write_text(json.dumps(value))
    result = make().executor({}, ctx)
    assert result["status"] == "error", result
    assert not (runner.checkpoints / "adata_qc.h5ad").exists()
    assert (outside / "private.txt").read_text() == "must never copy"


@pytest.mark.parametrize("args", [{"dataset": "/etc/passwd"}, {"dataset_paths": ["/etc"]},
                                  {"input": "../../secret.h5ad"}, {"model_dir": "/approved/model"}])
def test_host_paths_and_model_configuration_are_refused_before_execution(analysis_bridge, args):
    runner, ctx, make = analysis_bridge
    result = make().executor(args, ctx)
    assert result["status"] == "error"
    assert runner.calls == []


@pytest.mark.parametrize("state,status", [("FAILED", "ok"), ("COMPLETED", "error"), ("TIMEOUT", "ok"), ("COMPLETED", "dependency_missing")])
def test_server_failure_cannot_become_execution_success(analysis_bridge, state, status):
    runner, ctx, make = analysis_bridge
    value, _ = fixture_result(runner)
    value.update(state=state, status=status, error="failed " + "details " * 3000)
    runner.fixture.write_text(json.dumps(value))
    result = make().executor({}, ctx)
    assert result["status"] == "error", result
    assert result["response_omitted"] is True
    assert result["error"] and len(result["error"]) <= 1500
    assert len(json.dumps(result["result"])) <= 14000
    assert not (runner.checkpoints / "adata_qc.h5ad").exists()
    assert "details" in (runner.artifacts / result["full_result"]).read_text()


def test_bounded_result_preserves_complete_json(analysis_bridge):
    runner, ctx, make = analysis_bridge
    value, output = fixture_result(runner)
    full = output / "complete.json"
    full.write_text(json.dumps({"rows": list(range(8000)), "host": "/private/server/reference.vcf"}))
    value.update(full_result=str(full), note="x" * 15000)
    runner.fixture.write_text(json.dumps(value))
    result = make().executor({}, ctx)
    assert result["status"] == "ok", result
    assert len(json.dumps(result["result"])) <= 14000
    evidence = json.loads((runner.artifacts / result["full_result"]).read_text())
    assert len(evidence["rows"]) == 8000 and evidence["host"] == "[server path]"


def test_bounded_result_preserves_authoritative_slurm_metadata(analysis_bridge, monkeypatch):
    runner, ctx, make = analysis_bridge
    value, _ = fixture_result(runner)
    value["note"] = "large evidence " * 3000
    runner.fixture.write_text(json.dumps(value))
    original = runner.run_in_image

    def run(*args, **kwargs):
        return original(*args, **kwargs) | {"slurm_state": "COMPLETED", "slurm_job_id": "59539880",
                                          "node": "compute-node", "execution_mode": "hpc_slurm_image"}

    monkeypatch.setattr(runner, "run_in_image", run)
    result = make().executor({}, ctx)
    assert result["response_omitted"] is True
    assert result["slurm_state"] == "COMPLETED" and result["slurm_job_id"] == "59539880"
    assert result["node"] == "compute-node" and result["returncode"] == 0
    assert len(json.dumps(result)) <= 14000


@pytest.mark.parametrize("server", sorted(PREFIXES))
def test_registry_analysis_allowlist_and_noncolliding_prefix(tmp_path, server):
    path = config(tmp_path, server)
    assert bridge.load_servers(path)[0].planner_name("run_cellqc") == "mcp_run_cellqc"
    config(tmp_path, server, prefix="")
    with pytest.raises(ValueError, match="prefix"):
        bridge.load_servers(path)
    config(tmp_path, server, adapter="json")
    with pytest.raises(ValueError, match="analysis adapter"):
        bridge.load_servers(path)


@pytest.mark.parametrize("change", [{"name": "unknown", "release_tag": "unknown-v1.2.3"},
                                    {"settings_env": ["VARIANT_READ_ROOTS"]},
                                    {"settings_env": ["PYTHONPATH"]},
                                    {"settings_env": ["LITERATURE_MODEL_API_KEY"]},
                                    {"settings_env": {"CELLQC_DATASET": "/etc"}}])
def test_registry_refuses_unknown_adapter_names_and_unapproved_settings(tmp_path, change):
    path = config(tmp_path, **change)
    with pytest.raises(ValueError):
        bridge.load_servers(path)


@pytest.mark.parametrize("route", ["hpc:scrna", "gpu:scgpt", "inprocess"])
def test_registry_accepts_trusted_execution_routes(tmp_path, route):
    assert bridge.load_servers(config(tmp_path, runs_on=route))[0].runs_on == route


def test_settings_are_quoted_and_only_deployment_values_are_forwarded(analysis_bridge, monkeypatch):
    runner, ctx, make = analysis_bridge
    value = str(runner.root / "references with spaces" / "cache")
    monkeypatch.setenv("VARIANT_VEP_CACHE_DIR_GRCH38", value)
    fixture_result(runner, "variant-annotation")
    result = make("variant-annotation", "variant_job_status", settings=["VARIANT_VEP_CACHE_DIR_GRCH38"]).executor({"task_id": "task-one"}, ctx)
    assert result["status"] == "ok", result
    assert json.loads(runner.capture.read_text())["env"]["VARIANT_VEP_CACHE_DIR_GRCH38"] == value
    assert runner.calls[-1][1]["extra_ro_binds"] == (str(Path(value).parent),)


def test_settings_reject_credential_endpoints_and_root_mount(tmp_path, monkeypatch):
    server, = bridge.load_servers(config(tmp_path, "literature", settings=["LITERATURE_MODEL_ENDPOINT"]))
    monkeypatch.setenv("LITERATURE_MODEL_ENDPOINT", "https://user:placeholder@example.org/v1")
    with pytest.raises(ValueError, match="without credentials"):
        setting_paths(server)
    monkeypatch.setenv("LITERATURE_CORPUS_ROOT", "/data")
    with pytest.raises(ValueError, match="filesystem root"):
        setting_paths(replace(server, settings_env=["LITERATURE_CORPUS_ROOT"]))
    assert setting_paths(replace(server, name="atlas-expression-exploration", settings_env=())) == ()


def test_bound_inputs_and_authoritative_clinical_variant_context(tmp_path):
    server, = bridge.load_servers(config(tmp_path, "phenotype-diagnosis"))
    adapter = AnalysisAdapter(server)
    executor = SimpleNamespace(dataset_path="/bound/patient.vcf", inputs=[{"name": "bed", "path": "/bound/regions.bed"}])
    ctx = SimpleNamespace(decisions={"case_note": "Attached clinical findings", "mcp_variant_assembly": "GRCh37"})
    assert adapter.authoritative({"text": "model substitution"}, ctx, "map_phenotype_to_hpo", executor)["text"] == ctx.decisions["case_note"]
    args = adapter.authoritative({}, ctx, "run_lirical", executor)
    assert args["vcf_path"] == executor.dataset_path
    assert adapter.prepare(args, executor).arguments["vcf_path"] == executor.dataset_path
    variant = AnalysisAdapter(replace(server, name="variant-annotation"))
    args = variant.authoritative({"assembly": "GRCh38", "max_variants": 500}, ctx, "annotate_variants", executor)
    assert args == {"assembly": "GRCh37", "max_variants": 0}
    assert variant.prepare({"regions_bed": "bed"}, executor).arguments["regions_bed"] == "/bound/regions.bed"


def test_complete_public_response_is_bounded_before_transport(analysis_bridge):
    runner, ctx, make = analysis_bridge
    value, _ = fixture_result(runner)
    value["rows"] = ["0123456789"] * 700
    runner.fixture.write_text(json.dumps(value))
    result = make().executor({}, ctx)
    assert result["status"] == "ok" and result["response_compacted"] is True
    assert len(json.dumps(result)) <= 14000
    assert len(result["result"]["rows"]) == 700
    assert len(json.loads((runner.artifacts / result["full_result"]).read_text())["rows"]) == 700


def test_declared_secret_is_redacted_from_json_evidence(analysis_bridge, monkeypatch):
    from aiscientist.agents import skills
    runner, ctx, make = analysis_bridge
    get_skill = skills.get_skill
    monkeypatch.setattr(skills, "get_skill", lambda name: replace(
        get_skill(name), requirements={**get_skill(name).requirements, "secrets": ["TEST_MODEL_KEY"]}))
    secret = "fixture-secret-never-persist-this"
    monkeypatch.setenv("AISCIENTIST_SKILL_SECRET_TEST_MODEL_KEY", secret)
    runner.extra_env["TEST_MODEL_KEY"] = secret
    value, _ = fixture_result(runner)
    value["note"] = "server echoed " + secret
    runner.fixture.write_text(json.dumps(value))
    result = make().executor({}, ctx)
    assert result["status"] == "ok", result
    evidence = (runner.artifacts / result["full_result"]).read_text()
    assert secret not in evidence and "[REDACTED]" in evidence
    assert secret not in json.dumps(result)


@pytest.mark.parametrize("variable", ["AISCIENTIST_WORK", "AISCIENTIST_ARTIFACTS", "AISCIENTIST_ENV_DIR"])
def test_collector_rejects_symlinked_configured_root(analysis_bridge, variable):
    runner, ctx, make = analysis_bridge
    fixture_result(runner)
    targets = {"AISCIENTIST_WORK": runner.checkpoints, "AISCIENTIST_ARTIFACTS": runner.artifacts,
               "AISCIENTIST_ENV_DIR": runner.work}
    link = runner.root / "redirected root"
    link.symlink_to(targets[variable], target_is_directory=True)
    runner.extra_env[variable] = str(link)
    result = make().executor({}, ctx)
    assert result["status"] == "error"
    assert not (runner.checkpoints / "adata_qc.h5ad").exists()


@pytest.mark.parametrize("value", ["adata_qc.h5ad", "work/adata_qc.mouse.h5ad",
                                  "artifacts/tables/rank_group*.rnk", "artifacts/custom/input.h5ad"])
def test_scrna_accepts_confined_relative_input_paths(tmp_path, value):
    server, = bridge.load_servers(config(tmp_path, "scrna-analysis"))
    assert AnalysisAdapter(server).prepare({"input": value}, SimpleNamespace()).arguments["input"] == value


@pytest.mark.parametrize("value", ["/etc/adata_qc.h5ad", "work/../secret.h5ad", "artifacts/tables/../../secret.rnk",
                                  "artifacts//table.rnk", "external/table.rnk", "work/./adata.h5ad"])
def test_scrna_refuses_unconfined_relative_inputs(tmp_path, value):
    server, = bridge.load_servers(config(tmp_path, "scrna-analysis"))
    with pytest.raises(ValueError, match="relative path inside"):
        AnalysisAdapter(server).prepare({"input": value}, SimpleNamespace())


def test_scgpt_refactor_is_a_trusted_readonly_deployment_path(monkeypatch):
    from types import SimpleNamespace
    from aiscientist.agents.mcp_analysis import setting_paths, setting_values

    server = SimpleNamespace(name="scgpt-annotation", settings_env=("SCGPT_REFACTOR",))
    monkeypatch.setenv("SCGPT_REFACTOR", "/refs/scgpt-refactor")
    assert setting_values(server) == {"SCGPT_REFACTOR": "/refs/scgpt-refactor"}
    assert setting_paths(server) == ("/refs",)
    monkeypatch.setenv("SCGPT_REFACTOR", "../../untrusted")
    with pytest.raises(ValueError, match="absolute deployment path"):
        setting_values(server)


def test_invalid_used_variant_default_returns_a_structured_error(analysis_bridge, monkeypatch):
    runner, ctx, make = analysis_bridge
    monkeypatch.setenv("VARIANT_DEFAULT_GENE_PANEL", "missing-panel")
    tool = make("variant-annotation", "annotate_variants", settings=["VARIANT_DEFAULT_GENE_PANEL"])
    result = tool.executor({}, ctx)
    assert result["status"] == "error" and "deployment settings" in result["error"]
    assert result["mcp"]["name"] == "variant-annotation"
    assert not runner.capture.exists()


@pytest.mark.parametrize("padding", [7000, 25000])
def test_cellqc_status_keeps_executed_science_through_both_bounds(analysis_bridge, padding):
    runner, ctx, make = analysis_bridge
    value, _ = fixture_result(runner)
    value.update(thresholds={"min_counts": 500, "min_features": 300, "max_pct_mt": 5.0},
                 ambient={"applied": "soupx", "compared": "decontx"},
                 n_libraries=2, cells_before=120, cells_after=90,
                 checkpoints={"mouse": {"cells": 60, "genes": 200},
                              "human": {"cells": 30, "genes": 300}},
                 warnings=["Human library retained only 32%; inspect the mitochondrial threshold."],
                 libraries=[{"sample": "mouse_A", "species": "mouse", "cells_after_doublets": 60},
                            {"sample": "human_B", "species": "human", "cells_after_doublets": 30}],
                 diagnostics="d" * padding)
    runner.fixture.write_text(json.dumps(value))
    result = make(tool="cellqc_job_status").executor({"task_id": "task-one"}, ctx)
    assert result["status"] == "ok", result
    assert len(json.dumps(result)) <= 14000
    native = result["result"]
    assert native["thresholds"] == value["thresholds"]
    assert native["ambient"] == value["ambient"]
    assert native["cells_after"] == 90 and native["n_libraries"] == 2
    assert native["checkpoints"] == value["checkpoints"]
    assert native["warnings"] == value["warnings"]
    assert "seed" not in native  # the status result does not echo the original seed
    if padding == 7000:
        assert native["diagnostics"] == value["diagnostics"]
        assert "response_omitted" not in native
    else:
        assert native["response_omitted"] is True
        assert native["response_summary"]["complete"] is False
    complete = json.loads((runner.artifacts / result["full_result"]).read_text())
    assert complete["diagnostics"] == value["diagnostics"]
    assert complete["thresholds"] == native["thresholds"]


def test_cellqc_schema_support_does_not_invent_executed_defaults(analysis_bridge):
    runner, ctx, make = analysis_bridge
    schema = {"type": "object", "properties": {
        "ambient_compare": {"type": "string", "default": "decontx", "enum": ["", "soupx", "decontx", "none"]},
        "seed": {"type": "integer", "default": 42}}, "additionalProperties": False}
    value, _ = fixture_result(runner)
    value.update(ambient={"applied": "soupx", "compared": "decontx"}, diagnostics="x" * 20000)
    runner.fixture.write_text(json.dumps(value))
    result = make(schema=schema).executor({}, ctx)
    declaration = result["parameter_schema"]
    assert declaration["declared_parameters"] == ["ambient_compare", "seed"]
    assert declaration["sha256"] == result["mcp"]["tools_sha256"]
    assert declaration["tool"] == "mcp_run_cellqc"
    assert "seed" not in result and "seed" not in result["result"]


def test_collector_preserves_scientific_slashes_and_redacts_real_paths(analysis_bridge):
    runner, ctx, make = analysis_bridge
    value, _ = fixture_result(runner)
    labels = ["Leber congenital amaurosis-3 (LCA3)/Retinitis pigmentosa",
              "Deletion syndrome (Velocardiofacial / DiGeorge syndrome)",
              "phenotype /genotype pairing", "affected / unaffected"]
    value.update(diseases=labels, source_url="https://example.org/retina/phenotypes",
                 error_detail='File "/home/private/source.py", input=/dfs3b/private/input.vcf; read (/tmp/result.json)',
                 private_reference="/private/reference.vcf")
    runner.fixture.write_text(json.dumps(value))
    result = make().executor({}, ctx)
    assert result["diseases"] == labels
    assert result["source_url"] == value["source_url"]
    assert result["private_reference"] == "[server path]"
    assert "/home/private" not in result["error_detail"] and "/dfs3b" not in result["error_detail"]
    assert "/tmp" not in result["error_detail"]
    full = json.loads((runner.artifacts / result["full_result"]).read_text())
    assert full["diseases"] == labels and full["private_reference"] == "[server path]"
    adapter = AnalysisAdapter(bridge.configured_servers()[0])
    failed = adapter.failure({"status": "error", "stderr": value["error_detail"], "labels": labels})
    assert failed["labels"] == labels and failed["stderr"] == result["error_detail"]


@pytest.mark.parametrize("server", ["cellqc", "literature", "phenotype-diagnosis"])
def test_semantic_preview_and_navigation_remain_within_wire_limit(analysis_bridge, server):
    runner, ctx, make = analysis_bridge
    value, _ = fixture_result(runner, server)
    value.update(thresholds={"max_pct_mt": 5}, ambient={"applied": "soupx", "compared": "decontx"},
                 warnings=["表型" * 2000] * 8, answer="Retinal evidence. " + "表型" * 10000,
                 contexts=[{"pmid": "12345", "citation": "Verified source", "text": "表型" * 10000}],
                 literature=[{"gene": "GENE1", "association": False, "evidence_status": "unsupported"}],
                 n_candidates=8621, top={"gene": "", "posttest_prob": 0.4287, "agreement": "lirical_only"})
    runner.fixture.write_text(json.dumps(value))
    result = make(server).executor({}, ctx)
    assert result["status"] == "ok", result
    assert len(json.dumps(result)) <= 14000
    assert result["result"]["full_result"] == result["full_result"]
    if server == "cellqc":
        assert result["result"]["thresholds"] == value["thresholds"]
    if server == "phenotype-diagnosis":
        assert result["result"]["literature"][0]["association"] is False
    if server == "literature":
        assert result["result"]["answer"].startswith("Retinal evidence.")



def _phenotype_catalog(runner, monkeypatch):
    schema = {"type": "object", "properties": {"task_id": {"type": "string"}}, "additionalProperties": False}
    cfg = write_config(runner.root.parent, entries=[{"name": name, "description": "Read fixture evidence.",
        "inputSchema": schema} for name in ("run_lirical", "diagnose_disease", "phenotype_job_status")],
        overrides={"name": "phenotype-diagnosis", "release_tag": "phenotype-diagnosis-v1.2.3",
                   "prefix": "mcp_", "adapter": "analysis", "entrypoint": "phenotype-diagnosis-mcp", "resources": {}})
    monkeypatch.setenv("AISCIENTIST_MCP_CONFIG", str(cfg))
    return {tool.name: tool for tool in build(runner)}


def test_two_status_snapshots_keep_distinct_full_evidence_and_untracked_history(analysis_bridge, monkeypatch):
    runner, ctx, _ = analysis_bridge
    catalog = _phenotype_catalog(runner, monkeypatch)
    value, output = fixture_result(runner, "phenotype-diagnosis")
    dest = runner.artifacts / "mcp/phenotype-diagnosis/default"
    dest.mkdir(parents=True)
    old = dest / "run_lirical.json"
    old.write_text('{"historical": "untracked original evidence"}')
    original = old.read_bytes()
    responses = []
    for task, operation, n in (("task-one", "run_lirical", 8621), ("task-two", "diagnose_disease", 8)):
        request = output.parent.parent / task / "request.json"
        request.parent.mkdir(exist_ok=True)
        request.write_text(json.dumps({"operation": operation}))
        value.update(task_id=task, tool="untrusted_free_payload_name", n_candidates=n)
        runner.fixture.write_text(json.dumps(value))
        responses.append(catalog["mcp_phenotype_job_status"].executor({"task_id": task}, ctx))
    assert all(r["status"] == "ok" for r in responses), responses
    assert responses[0]["full_result"] != responses[1]["full_result"]
    for response, count in zip(responses, (8621, 8)):
        full = json.loads((runner.artifacts / response["full_result"]).read_text())
        assert full["n_candidates"] == count and full["task_id"] == response["task_id"]
    assert old.read_bytes() == original
    ownership = json.loads((runner.artifacts / "mcp/.compatibility.json").read_text())["evidence"]
    assert ownership[responses[0]["full_result"]]["operation"] == "run_lirical"
    assert ownership[responses[1]["full_result"]]["operation"] == "diagnose_disease"
    assert not (dest / "untrusted_free_payload_name.json").exists()


def test_status_task_mismatch_fails_before_artifact_copy(analysis_bridge):
    runner, ctx, make = analysis_bridge
    fixture_result(runner)
    result = make(tool="cellqc_job_status").executor({"task_id": "different-task"}, ctx)
    assert result["status"] == "error" and "another task identifier" in json.dumps(result)
    assert not (runner.artifacts / "mcp/cellqc").exists()
    assert not (runner.checkpoints / "adata_qc.h5ad").exists()


@pytest.mark.parametrize("live_state", ["COMPLETED", "FAILED"])
def test_projection_reads_all_eight_original_passages_and_live_failure_wins(analysis_bridge, monkeypatch, live_state):
    runner, ctx, _ = analysis_bridge
    catalog = _phenotype_catalog(runner, monkeypatch)
    value, output = fixture_result(runner, "phenotype-diagnosis")
    source = output.parent / "result.json"
    scientific = {"status": "ok", "tool": "diagnose_disease", "n_candidates": 8621,
        "literature": [{"association": False, "evidence_status": "unsupported",
                        "evidence": [{"source_id": str(i), "quote": "full passage " * 1000} for i in range(8)]}],
        "differential": [{"disease_name": "Condition", "rank": i} for i in range(8621)]}
    source.write_text(json.dumps(scientific))
    (output.parent / "request.json").write_text(json.dumps({"operation": "diagnose_disease"}))
    value.update(full_result=str(source), state=live_state, status="ok" if live_state == "COMPLETED" else "error",
                 literature=[{"association": False, "evidence_status": "unsupported", "evidence": [{}] * 5}],
                 response_omitted=True)
    if live_state == "FAILED":
        value["error"] = "actual live allocation failed"
    runner.fixture.write_text(json.dumps(value))
    result = catalog["mcp_phenotype_job_status"].executor({"task_id": "task-one"}, ctx)
    assert result["status"] == ("ok" if live_state == "COMPLETED" else "error"), result
    assert result["result"]["literature"][0]["evidence_count"] == 8
    assert result["result"]["literature"][0]["association"] is False
    full = json.loads((runner.artifacts / result["full_result"]).read_text())
    assert len(full["literature"][0]["evidence"]) == 8 and len(full["differential"]) == 8621
    assert full["state"] == live_state and full["status"] == result["status"]
    if live_state == "FAILED":
        assert full["error"] == "actual live allocation failed" and result["error"]


def test_status_refreshes_only_matching_managed_operation_alias(analysis_bridge, monkeypatch):
    runner, ctx, _ = analysis_bridge
    catalog = _phenotype_catalog(runner, monkeypatch)
    value, output = fixture_result(runner, "phenotype-diagnosis")
    first = catalog["mcp_run_lirical"].executor({}, ctx)
    assert first["status"] == "ok"
    canonical = runner.artifacts / "mcp/phenotype-diagnosis/default/run_lirical.json"
    first_snapshot = runner.artifacts / first["full_result"]
    first_bytes = first_snapshot.read_bytes()
    (output.parent / "request.json").write_text(json.dumps({"operation": "run_lirical"}))
    value["n_candidates"] = 8621
    runner.fixture.write_text(json.dumps(value))
    result = catalog["mcp_phenotype_job_status"].executor({"task_id": "task-one"}, ctx)
    assert result["status"] == "ok"
    assert json.loads(canonical.read_text())["n_candidates"] == 8621
    managed = canonical.read_bytes()
    other = output.parent.parent / "other-task"
    other.mkdir()
    (other / "request.json").write_text(json.dumps({"operation": "run_lirical"}))
    value.update(task_id="other-task", n_candidates=1)
    runner.fixture.write_text(json.dumps(value))
    result = catalog["mcp_phenotype_job_status"].executor({"task_id": "other-task"}, ctx)
    assert result["status"] == "ok" and result["result"]["n_candidates"] == 1
    assert canonical.read_bytes() == managed
    assert first_snapshot.read_bytes() == first_bytes



def test_primary_calls_have_immutable_task_paths_when_operation_alias_advances(analysis_bridge):
    runner, ctx, make = analysis_bridge
    value, _ = fixture_result(runner)
    tool = make()
    first = tool.executor({}, ctx)
    first_file = runner.artifacts / first["full_result"]
    original = first_file.read_bytes()
    value.update(task_id="second-task", cells_after=70)
    runner.fixture.write_text(json.dumps(value))
    second = tool.executor({}, ctx)
    assert first["full_result"] != second["full_result"]
    assert first_file.read_bytes() == original
    canonical = runner.artifacts / "mcp/cellqc/default/run_cellqc.json"
    assert json.loads(canonical.read_text())["task_id"] == "second-task"
    assert json.loads((runner.artifacts / second["full_result"]).read_text())["cells_after"] == 70


def test_scrna_reference_settings_preserve_explicit_operator_paths(monkeypatch):
    from types import SimpleNamespace
    from aiscientist.agents.mcp_analysis import setting_paths, setting_values
    server = SimpleNamespace(name="scrna-analysis", settings_env=("SCRNA_GENESETS_DIR",))
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", "/cluster/reference/legacy")
    monkeypatch.delenv("SCRNA_GENESETS_DIR", raising=False)
    assert setting_values(server) == {"SCRNA_GENESETS_DIR": "/cluster/reference/legacy"}
    assert setting_paths(server) == ("/cluster/reference",)
    monkeypatch.setenv("SCRNA_GENESETS_DIR", "/cluster/native/genesets")
    assert setting_values(server) == {"SCRNA_GENESETS_DIR": "/cluster/native/genesets"}
    monkeypatch.setenv("SCRNA_GENESETS_DIR", "")
    assert setting_values(server) == {"SCRNA_GENESETS_DIR": ""} and setting_paths(server) == ()
    monkeypatch.delenv("SCRNA_GENESETS_DIR")
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", "relative/legacy")
    with pytest.raises(ValueError, match="absolute deployment path"):
        setting_values(server)
