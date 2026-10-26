"""The pinned aiscientist-skills release, as installed, against what the platform expects of it.

These tests read the release ``configs/skills_registry.json`` pins, where the loader reads it, so
they run where it is installed (the server, a developer's checkout) and are skipped elsewhere, CI
included. Install it with ``gh release download <tag> -R lijinbio/aiscientist-skills -D
skills/releases/<version>``. Every other test builds its own release (``tests/conftest.py``).
"""
from __future__ import annotations

import json

import pytest

from aiscientist.agents import skills as sk
from aiscientist.agents import skills_registry as registry
from aiscientist.tools import catalog

CFG = registry.config()
RELEASE = registry.release_dir(CFG)
pytestmark = pytest.mark.skipif(
    not RELEASE.exists(),
    reason=f"pinned skills release {CFG['release_tag']} is not installed: {registry.install_command(CFG)}")


@pytest.fixture(scope="module")
def lib():
    return registry.load(sk._load_from)


def test_every_catalog_skill_of_an_allowed_tier_loads(lib):
    catalog_rows = json.loads((RELEASE / "catalog.json").read_text(encoding="utf-8"))["skills"]
    wanted = {r["name"] for r in catalog_rows if r["status"] in CFG["allowed_tiers"]}
    assert set(lib) == wanted
    assert {s.registry_release for s in lib.values()} == {CFG["release_tag"]}


def test_the_skills_name_only_tools_that_exist(lib):
    from aiscientist.agents.preset_pipelines import PIPELINES
    from test_tool_contract import PLATFORM_TOOLS, toolish_names

    known = (set(catalog.names()) | PLATFORM_TOOLS | set(PIPELINES) | set(lib)
             | {a for s in lib.values() for a in s.aliases})
    bad = []
    for skill in lib.values():
        texts = {"SKILL.md": skill.doc, **{k: v for k, v in skill.files.items() if k.endswith(".md")}}
        bad += [f"{skill.name}/{rel}: `{name}`" for rel, text in texts.items()
                for name in toolish_names(text) if name not in known]
    assert not bad, "skills name tools that do not exist:\n" + "\n".join(sorted(set(bad)))


def test_the_skills_this_repository_names_resolve(lib):
    from test_tool_contract import NAMED_SKILLS

    missing = [f"{n} ({where})" for n, where in sorted(NAMED_SKILLS.items()) if sk.get_skill(n, lib) is None]
    assert not missing, f"named skills the pinned release lacks: {missing}"


def test_run_cellqc_runs_the_release_skill_with_its_defaults(lib):
    from aiscientist.tools._lib.scrna import PARAMS

    m = catalog.manifest("run_cellqc")
    skill = sk.get_skill(m.skill, lib)
    assert skill is not None and skill.image, f"{m.skill} has no usable environment"
    settings = json.loads(skill.files["assets/cellqc-defaults.json"])
    # The defaults the tool's schema shows the model must be the skill's.
    assert {k: PARAMS["run_cellqc"][k][0] for k in settings["thresholds"]} == settings["thresholds"]


def test_the_atlas_skill_reads_the_tables_the_real_run_de_writes(lib, tmp_path, monkeypatch):
    """The labels the atlas skill trusts (a marker table is refused, a stratified contrast's
    exploratory p-values are not inferential) are the ones run_de actually writes. The skill's own
    tests in the registry write those files by hand; this runs the platform's tool."""
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    pytest.importorskip("matplotlib")
    pytest.importorskip("scanpy")
    import anndata as ad
    import runpy
    from types import SimpleNamespace

    from aiscientist.tools.run_de import tool as run_de_tool
    from aiscientist.tools.run_scanpy_qc import tool as run_scanpy_qc_tool

    skill = sk.get_skill("atlas_disease_genes", lib)
    assert skill is not None, "the pinned release has no skill answering to atlas_disease_genes"
    script = tmp_path / "reference.py"
    script.write_text(skill.files[sk.default_file(skill.files)], encoding="utf-8")

    def run(**overrides) -> dict:
        module = runpy.run_path(str(script), run_name="skill_module")
        return module["main"](dict(module["CONFIG"], atlas_path="atlas/precalculated_atlas.csv",
                                   **overrides))

    rng = np.random.default_rng(0)
    n = 300
    x = rng.poisson(4.0, (n, 40)).astype(np.float32) + 1.0
    cond = np.array(["DDX41" if i % 2 else "WT" for i in range(n)])
    ctype = np.array([["Rod", "Cone", "MG"][i % 3] for i in range(n)])
    x[(cond == "DDX41") & (ctype == "Rod"), 0] *= 6          # Gene0 up in DDX41 rods
    adata = ad.AnnData(x)
    adata.var_names = [f"Gene{i}" for i in range(40)]
    adata.obs_names = [f"c{i}" for i in range(n)]
    adata.obs["sampleid"] = pd.Categorical(cond)
    adata.obs["majorclass"] = pd.Categorical(ctype)
    adata.obs["orig.ident"] = pd.Categorical(["s1"] * n)
    adata.write(tmp_path / "retina.h5ad")
    ctx = SimpleNamespace(workspace=tmp_path, decisions={"dataset_path": str(tmp_path / "retina.h5ad")})
    assert run_scanpy_qc_tool.run_scanpy_qc(
        {"min_genes": 1, "min_cells": 1, "max_pct_mt": 100.0}, ctx)["status"] == "ok"
    work, art = tmp_path / "work", tmp_path / "artifacts"
    (work / "atlas").mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"cell_type": ct, "gene": f"Gene{i}",
                   "mean_expression": 40.0 if (ct, i) == ("Rod", 0) else 4.0}
                  for ct in ("Rod", "Cone", "MG") for i in range(40)]).to_csv(
                      work / "atlas" / "precalculated_atlas.csv", index=False)
    monkeypatch.setenv("AISCIENTIST_WORK", str(work))
    monkeypatch.setenv("AISCIENTIST_ARTIFACTS", str(art))

    assert run_de_tool.run_de({"groupby": "majorclass"}, ctx)["status"] == "ok"
    with pytest.raises(SystemExit, match="MARKER table"):
        run(de_key="majorclass")

    out = run_de_tool.run_de({"groupby": "sampleid", "reference": "WT", "stratify_by": "majorclass"}, ctx)
    assert out["status"] == "ok" and out["inference"] == "exploratory_ranking"
    summary = run(de_key="majorclass", query_path=None)
    assert summary["p_values_inferential"] is False
    assert set(summary["de_source"]["rows_from"].values()) == {"per_group_full"}
    assert set(summary["de_source"]["rows_from"]) == {"Rod", "Cone", "MG"}
    candidates = pd.read_csv(art / "tables" / "atlas" / "candidates.csv")
    top = candidates[candidates.selected].iloc[0]
    assert (top["gene"], top["group"]) == ("GENE0", "Rod")
