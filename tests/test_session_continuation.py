"""A later message in the same chat continues the analysis instead of starting it again.

A study is rarely one request: QC, look, annotate, look, compare. Until 2026-10-12 a later message
was either a re-run of one step of the finished run (whose checkpoints are deleted when the report
is written, so that fell back to a full re-run) or a brand-new study from the raw input, which
repeated QC and clustering and knew nothing of what the earlier run had found. Now the run that
continues starts from the processed dataset the earlier run handed on — written back as its own
checkpoints, so every tool reads it like the output of this run's QC and clustering — and its
planner is told what the conversation has already done.
"""
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

from aiscientist.agents.research_lab import handed_on_note

_NAME = "Sample1_WT.processed.20261012-1000.run1.h5ad"


@pytest.fixture
def libs():
    """anndata & co. — absent from the offline CI subset, which must still run the other tests."""
    return types.SimpleNamespace(ad=pytest.importorskip("anndata"), np=pytest.importorskip("numpy"),
                                 pd=pytest.importorskip("pandas"), sp=pytest.importorskip("scipy.sparse"),
                                 h5py=pytest.importorskip("h5py"))


def _first_run(root: Path, libs, *, annotated: bool = False) -> Path:
    """A finished run's workspace: QC (all 60 genes, counts layer, .raw) -> clustered (30 scaled
    HVGs, .raw full, leiden + UMAP), three populations with five marker genes each."""
    ad, np, pd, sp = libs.ad, libs.np, libs.pd, libs.sp
    rng = np.random.default_rng(0)
    ws = root / "run1"
    (ws / "work").mkdir(parents=True)
    n, g = 300, 60
    group = np.array([i % 3 for i in range(n)])
    lam = np.full((n, g), 1.0)
    for k in range(3):
        lam[group == k, k * 5:(k + 1) * 5] = 12
    counts = rng.poisson(lam).astype(np.float32)
    qc = ad.AnnData(sp.csr_matrix(np.log1p(counts / counts.sum(1, keepdims=True) * 1e4)))
    qc.var_names = [f"Gene{i}" for i in range(g)]
    qc.obs_names = [f"AAAC{i:04d}-1" for i in range(n)]
    qc.layers["counts"] = sp.csr_matrix(counts)
    qc.uns["counts_layer"] = "UMI counts as read from the input"
    qc.var["highly_variable"] = [i < 30 for i in range(g)]
    qc.raw = qc
    qc.write_h5ad(ws / "work" / "adata_qc.h5ad")
    cl = qc[:, :30].copy()
    cl.X = np.asarray(cl.X.todense())
    cl.obs["leiden"] = pd.Categorical([str(k) for k in group])
    cl.obsm["X_umap"] = rng.normal(size=(n, 2)).astype(np.float32)
    if annotated:
        cl.obs["cell_type"] = cl.obs["leiden"].map({"0": "Rod", "1": "Cone", "2": "MG"}).astype(str)
    cl.write_h5ad(ws / "work" / "adata_clustered.h5ad")
    return ws


def _handed_on(root: Path, libs, **kw) -> Path:
    from aiscientist.tools._lib.result_dataset import export
    ws = _first_run(root, libs, **kw)
    out = export(ws, _NAME, {"run_id": "run1", "question": "run QC and cluster the cells"})
    assert out["status"] == "ok"
    return ws / "result" / _NAME


# --- the dataset says it was handed on, and the planner is told -------------------------------------

def test_the_profile_recognises_a_dataset_an_earlier_run_handed_on(tmp_path, libs):
    from aiscientist.tools.datasets import inspect_h5ad
    src = _handed_on(tmp_path, libs)
    with libs.h5py.File(src, "r") as handle:
        profile = inspect_h5ad(handle)
    rec = profile["handed_on"]
    assert rec["processed"] is True and rec["run_id"] == "run1"
    assert rec["layers"] == ["counts"] and "X_umap" in rec["obsm"]
    assert rec["counts_layer"] == "UMI counts as read from the input"
    # ...and an ordinary h5ad does not.
    with libs.h5py.File(tmp_path / "run1" / "work" / "adata_qc.h5ad", "r") as handle:
        assert "handed_on" not in inspect_h5ad(handle)


def test_a_dataset_handed_on_keeps_the_tissue_hint_the_delivery_gave(tmp_path, libs):
    """A 10x delivery's profile says which tissue its most abundant genes point to. The processed
    .h5ad a later run starts from said nothing, so that run annotated "with no tissue reference"
    (two of three measured continuation plans). An .h5ad now gets the hint from its own counts."""
    from aiscientist.tools._lib.result_dataset import export
    from aiscientist.tools.datasets import inspect_h5ad
    ad, np, sp = libs.ad, libs.np, libs.sp
    genes = ["Rho", "Sag", "Gnat1", "Pde6b", "Glul", "Rlbp1", "Vsx2"] + [f"Gene{i}" for i in range(93)]
    rng = np.random.default_rng(1)
    lam = np.full((400, 100), 0.3)
    lam[:, :7] = 25                                         # the retinal markers dominate
    counts = rng.poisson(lam).astype(np.float32)
    ws = tmp_path / "run1"
    (ws / "work").mkdir(parents=True)
    qc = ad.AnnData(sp.csr_matrix(np.log1p(counts)))
    qc.var_names, qc.obs_names = genes, [f"C{i}" for i in range(400)]
    qc.layers["counts"] = sp.csr_matrix(counts)
    qc.raw = qc
    qc.write_h5ad(ws / "work" / "adata_qc.h5ad")
    assert export(ws, _NAME, {"run_id": "run1"})["status"] == "ok"
    with libs.h5py.File(ws / "result" / _NAME, "r") as handle:
        profile = inspect_h5ad(handle)
    assert "most likely retina" in profile["tissue_hint"] and profile["top_genes"][0] in genes[:7]
    assert 'reference: "retina"' in profile["tissue_hint"]
    assert "Plan `run_marker_annotation`" not in profile["tissue_hint"]   # how, not one more step

    from aiscientist.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
    from aiscientist.agents.research_lab import LabConfig, ResearchLab
    lab = ResearchLab(HarnessContext(decisions={"dataset_result": profile}, tunnel_port=1, model="m"),
                      LabConfig(), complete_fn=lambda _m: "{}",
                      scientist=ResearchHarness(catalog=default_catalog(),
                                                chat_fn=lambda *_a: {"content": ""}))
    context = lab._dataset_context()
    assert "TISSUE HINT" in context and "PROCESSED OUTPUT OF AN EARLIER RUN" in context


def test_an_h5ad_with_no_curated_tissue_gets_no_hint(tmp_path, libs):
    from aiscientist.tools.datasets import inspect_h5ad
    src = _handed_on(tmp_path, libs)                        # genes named Gene0..Gene59
    with libs.h5py.File(src, "r") as handle:
        profile = inspect_h5ad(handle)
    assert "tissue_hint" not in profile and "top_genes" not in profile


def test_the_planner_is_told_what_such_a_dataset_already_carries():
    profile = {"cells": 300, "genes": 60,
               "obs_categoricals": {"leiden": {"n": 3}, "cell_type": {"n": 3}},
               "handed_on": {"processed": True, "run_id": "run1", "layers": ["counts"],
                             "obsm": ["X_pca", "X_umap"], "counts_layer": "SoupX-corrected counts"}}
    note = handed_on_note(profile)
    assert "PROCESSED OUTPUT OF AN EARLIER RUN (run run1)" in note
    assert "do not filter or normalise again" in note
    assert "clusters in `leiden`" in note and "cell-type labels in `cell_type`" in note
    assert "SoupX-corrected counts" in note and "a UMAP embedding" in note
    assert "Do NOT plan QC" in note and "enrichment needs a DE table" in note
    assert handed_on_note({"cells": 300}) == "" and handed_on_note(None) == ""


# --- it becomes the new run's checkpoints -------------------------------------------------------------

def test_the_handed_on_dataset_is_written_back_as_the_new_runs_checkpoints(tmp_path, libs):
    from aiscientist.tools._lib.result_dataset import adopt
    src = _handed_on(tmp_path, libs, annotated=True)
    ws = tmp_path / "run2"
    out = adopt(ws, src)
    assert out["status"] == "ok"
    got = out["adopted"]
    assert got["checkpoints"] == ["adata_qc.h5ad", "adata_clustered.h5ad"]
    assert (got["source_run"], got["n_cells"], got["n_genes"]) == ("run1", 300, 60)
    assert (got["cluster_key"], got["n_clusters"]) == ("leiden", 3)
    assert (got["label_key"], got["n_labels"]) == ("cell_type", 3)

    qc = libs.ad.read_h5ad(ws / "work" / "adata_qc.h5ad")
    # .raw is back: the export dropped it as a copy of X, and the DE and annotation tools read it.
    assert qc.raw is not None and qc.raw.shape == (300, 60)
    assert "counts" in qc.layers and "leiden" in qc.obs and "X_umap" in qc.obsm
    # Two files, not two names for one: run_clustering rewrites adata_clustered.h5ad in place.
    assert not (ws / "work" / "adata_qc.h5ad").samefile(ws / "work" / "adata_clustered.h5ad")

    assert adopt(ws, src)["status"] == "skipped"                       # never overwrites a checkpoint
    plain = adopt(tmp_path / "run3", tmp_path / "run1" / "work" / "adata_qc.h5ad")
    assert plain["status"] == "skipped" and not (tmp_path / "run3" / "work").exists()


def test_the_tools_read_it_and_qc_refuses_to_run_on_it_again(tmp_path, libs):
    pytest.importorskip("scanpy")
    from aiscientist.tools._lib.result_dataset import adopt, export
    from aiscientist.tools.run_de.tool import make_tool as de_tool
    from aiscientist.tools.run_scanpy_qc.tool import make_tool as qc_tool
    src = _handed_on(tmp_path, libs)
    ws = tmp_path / "run2"
    adopt(ws, src)
    ctx = types.SimpleNamespace(workspace=ws, decisions={"dataset_path": str(src)})

    de = de_tool().executor({"groupby": "leiden"}, ctx)
    assert de["status"] == "ok" and (ws / "work" / "adata_de.h5ad").is_file()

    qc = qc_tool().executor({}, ctx)
    assert qc["status"] == "error" and "already quality-controlled" in qc["error"]
    assert "normalise twice" in qc["error"]

    # What THIS run hands on says which run it built on.
    again = export(ws, "Sample1_WT.processed.20261012-1100.run2.h5ad", {"run_id": "run2"})
    assert again["status"] == "ok"
    out = libs.ad.read_h5ad(ws / "result" / "Sample1_WT.processed.20261012-1100.run2.h5ad")
    assert list(out.uns["aiscientist"]["builds_on"]) == ["run1"] and out.raw is None


# --- the gateway: what the conversation already did ---------------------------------------------------

pytest.importorskip("fastapi")
from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

_AGENDA = ["**QC & normalization** — Filter cells with `run_scanpy_qc` at 200 genes and 10% mito.",
           "**Clustering & UMAP** — Cluster with `run_clustering`.",
           "**Marker genes** — Rank markers with `run_de`."]


def _state(**extra) -> dict:
    def rnd(i, verdict, answer):
        return {"round_no": i + 1, "step_index": i, "step": _AGENDA[i], "specialist": "Scientist",
                "scientist_result": {"final_answer": answer},
                "verdict": {"verdict": verdict, "score": 0.9, "critique": ""}}
    return {"question": "run QC and cluster the cells", "agenda": _AGENDA,
            "rounds": [rnd(0, "accept", "16,750 of 18,681 cells kept."),
                       rnd(1, "revise", "failed"), rnd(1, "accept", "24 clusters at resolution 0.8."),
                       rnd(2, "revise", "no table written")],
            "dataset_path": "/ws/run1/staged/filtered_feature_bc_matrix.h5", **extra}


def test_what_the_earlier_run_did_is_read_from_its_saved_state(tmp_path):
    prior = gw_app._prior_work("run1", tmp_path, _state(bound_primary="/data/shared/Sample1_WT"))
    assert prior["run_id"] == "run1" and prior["dataset"] is None
    assert [(s["title"], s["done"]) for s in prior["steps"]] == [
        ("QC & normalization", True), ("Clustering & UMAP", True), ("Marker genes", False)]
    assert prior["steps"][1]["finding"] == "24 clusters at resolution 0.8."      # the accepted round
    assert prior["bound_primary"] == "/data/shared/Sample1_WT"

    block = gw_app._prior_work_block(prior, from_dataset=True)
    assert block.startswith("EARLIER IN THIS CONVERSATION")
    assert "1. QC & normalization — 16,750 of 18,681 cells kept." in block
    assert "3. Marker genes [NOT completed]" in block
    assert "starts from the processed dataset that run handed on" in block
    assert "Plan only what it asks for" in block
    # Without the dataset the plan must put the prerequisites back.
    assert "starts from the ORIGINAL input" in gw_app._prior_work_block(prior, from_dataset=False)


def test_a_third_run_knows_the_whole_conversation(tmp_path):
    first = gw_app._prior_work("run1", tmp_path, _state())
    second_state = _state(question="now annotate the cell types",
                          session=[gw_app._session_entry(first)], continues="run1")
    block = gw_app._prior_work_block(gw_app._prior_work("run2", tmp_path, second_state), True)
    assert 'Run run1 — request: "run QC and cluster the cells". Did: QC & normalization; Clustering & UMAP.' in block
    assert 'Run run2 — request: "now annotate the cell types".' in block


def test_the_report_of_a_continuing_run_says_what_it_did_not_redo(tmp_path):
    prior = gw_app._prior_work("run1", tmp_path, _state())
    note = gw_app._earlier_work_note(prior, from_dataset=True)
    assert note.startswith("## Earlier work in this conversation")
    assert "continues run `run1`" in note and "- Clustering & UMAP" in note
    assert "- Marker genes" not in note                       # it was not completed there
    md = gw_app._insert_before_backmatter("# T\n\nBody.\n\n## References\n\n1. x\n", note)
    assert md.index("## Earlier work") < md.index("## References")
    assert "started\nagain" not in note and "original input" in gw_app._earlier_work_note(prior, False)


def _conn(tmp_path):
    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


@pytest.fixture(autouse=True)
def _clean_conns():
    yield
    gw_app.CONNECTIONS.clear()


def test_the_handed_on_dataset_is_used_only_while_it_is_still_there(tmp_path):
    conn = _conn(tmp_path)
    f = conn.workspace / "run1" / "result" / _NAME
    f.parent.mkdir(parents=True)
    prior = {"dataset": {"path": str(f), "name": _NAME, "location": "gateway"}}
    assert gw_app._handed_on_dataset(conn, prior) is None            # swept, or never written
    f.write_bytes(b"x")
    assert gw_app._handed_on_dataset(conn, prior) == {"path": str(f), "name": _NAME}
    assert gw_app._handed_on_dataset(conn, {"dataset": None}) is None


def test_a_continuing_run_starts_from_the_earlier_runs_dataset(tmp_path, libs, monkeypatch):
    """End to end through _run_lab with the lab replaced: the earlier run's processed dataset is
    bound and adopted, the planner gets the conversation so far, the report says what was not
    redone, and the saved state lets the NEXT message continue in turn."""
    from aiscientist.agents import research_lab as rl
    from aiscientist.agents.research_lab import CriticVerdict, LabResult, LabRound
    from aiscientist.tools._lib.result_dataset import export

    conn = _conn(tmp_path)
    ws1 = _first_run(conn.workspace, libs)                          # <owner>/run1/work/...
    assert export(ws1, _NAME, {"run_id": "run1", "location": "gateway"})["status"] == "ok"
    import shutil
    shutil.rmtree(ws1 / "work")                                     # released when the report was written
    art1 = ws1 / "artifacts"
    (art1 / "report").mkdir(parents=True, exist_ok=True)
    (art1 / "report" / "report.md").write_text("# Report\n", encoding="utf-8")
    state1 = _state(bound_primary="/data/shared/Sample1_WT")
    (art1 / "process").mkdir(parents=True, exist_ok=True)
    (art1 / "process" / "run_state.json").write_text(json.dumps(state1), encoding="utf-8")

    seen: dict = {}

    class _FakeLab:
        def __init__(self, ctx, config, **_k):
            seen.update(config=config, profile=dict(ctx.decisions.get("dataset_result") or {}),
                        checkpoints=sorted(p.name for p in (ctx.workspace / "work").glob("*.h5ad")))
            self.guidance, self.protocols, self.self_sourced = None, [], ""
            self.execution_profile = {}
            self.later = ["Run pathway enrichment on the markers."]
            self.request_breadth = "specific"
            self.proposed_steps = [{"step": "**Doublet audit** — Re-check the doublet calls.",
                                    "why": "two callers disagree", "decision": "declined"}]

        def run(self, question, on_event=None, plan_review=None, **_kw):
            step = "**Annotation** — Label the clusters with `run_marker_annotation`."
            rnd = LabRound(1, 0, step, "Scientist", {"final_answer": "Three cell types.", "evidence": []},
                           CriticVerdict("accept", 0.9, ""))
            return LabResult(question, [step], [rnd], True, 1, "Three cell types were assigned.")

    monkeypatch.setattr(rl, "ResearchLab", _FakeLab)
    monkeypatch.delenv("AISCIENTIST_PLAN_YAML", raising=False)
    monkeypatch.setattr(gw_app, "_build_report", lambda *a, **k: "# Annotation\n\nThree cell types.\n")
    monkeypatch.setattr(gw_app, "_review_and_finalize_report", lambda md, *a, **k: md)
    said: list[str] = []
    monkeypatch.setattr(conn, "_publish", lambda payload: said.append(str(payload.get("text") or "")))

    req = gw_app.LabRequest(connection_id=conn.id, question="now annotate the cell types",
                            conversation_id="conv-1", dataset_path="/data/shared/Sample1_WT")
    conn.begin_run("conv-1")
    asyncio.run(gw_app._run_lab(conn, req, prior=gw_app._prior_work("run1", art1, state1)))

    # It started from run1's dataset, already in place as checkpoints when the lab was built.
    assert seen["profile"]["handed_on"]["run_id"] == "run1"
    assert seen["checkpoints"] == ["adata_clustered.h5ad", "adata_qc.h5ad"]
    assert any("Continuing from run run1" in line for line in said)
    assert seen["config"].plan_yaml is True, "a run carries the plan as a YAML document unless turned off"
    # The planner was told what the conversation had done.
    block = seen["config"].prior_work
    assert "EARLIER IN THIS CONVERSATION" in block and "2. Clustering & UMAP" in block
    assert "starts from the processed dataset that run handed on" in block

    run2 = conn.last_run_by_conversation["conv-1"]
    art2 = conn.workspace / run2 / "artifacts"
    state2 = json.loads((art2 / "process" / "run_state.json").read_text())
    assert state2["continues"] == "run1"
    assert state2["bound_primary"] == "/data/shared/Sample1_WT"       # what the browser keeps sending
    assert state2["session"] == [{"run_id": "run1", "question": "run QC and cluster the cells",
                                  "steps": ["QC & normalization", "Clustering & UMAP"]}]
    assert state2["adopted"]["cluster_key"] == "leiden"
    assert state2["later"] == ["Run pathway enrichment on the markers."]
    assert "## Earlier work in this conversation" in (art2 / "report" / "report.md").read_text()
    assert any("Next, in this chat" in line and "pathway enrichment" in line for line in said)
    # A step proposed mid-run and not approved is listed, and recorded, never silently dropped.
    assert any("Proposed during the run and NOT run" in line and "Doublet audit" in line for line in said)
    assert state2["proposed_steps"][0]["decision"] == "declined"

    # The next message is still recognised as a follow-up of this chat, and continues from run2.
    target = gw_app._followup_target(conn, req)
    assert target is not None and target[0] == run2
    pointer = gw_app._result_dataset_pointer(art2)
    assert pointer and Path(pointer["path"]).is_file()
    assert pointer["name"].startswith("Sample1_WT.processed.") and pointer["name"].count(".processed.") == 1
    handed = libs.ad.read_h5ad(pointer["path"])
    assert list(handed.uns["aiscientist"]["builds_on"]) == ["run1"]


def test_a_continuing_run_is_given_the_plan_document_the_earlier_run_was_approved_as(tmp_path):
    """With the plan as a document, the earlier run left report/plan.yaml: the tools and the settings
    that ran. "The same clustering at 0.8" is planned from that, not from a step title."""
    (tmp_path / "report").mkdir()
    (tmp_path / "report" / "plan.yaml").write_text(
        "# AiScientist plan. Run it as it is, or edit it.\nplan: 1\nsteps:\n  - id: s2\n"
        "    title: Clustering & UMAP\n    tool: run_clustering\n    settings:\n      resolution: 0.5\n",
        encoding="utf-8")
    prior = gw_app._prior_work("run1", tmp_path, _state())
    assert prior["plan_yaml"].startswith("plan: 1") and "#" not in prior["plan_yaml"]
    block = gw_app._prior_work_block(prior, from_dataset=True)
    assert "approved as" in block and "resolution: 0.5" in block and "```yaml" in block
    # A run from before the plan document (or with the flag off) has none, and says nothing about it.
    assert gw_app._prior_work("run0", tmp_path / "nowhere", _state())["plan_yaml"] == ""
    assert "approved as" not in gw_app._prior_work_block(
        gw_app._prior_work("run0", tmp_path / "nowhere", _state()), from_dataset=True)


@pytest.mark.parametrize("value, on", [(None, True), ("1", True), ("", True), ("0", False), ("off", False), ("false", False)])
def test_the_plan_document_is_on_unless_the_operator_turns_it_off(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv("AISCIENTIST_PLAN_YAML", raising=False)
    else:
        monkeypatch.setenv("AISCIENTIST_PLAN_YAML", value)
    assert gw_app._plan_yaml_on() is on
