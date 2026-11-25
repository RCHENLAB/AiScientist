"""A dataset chosen while a plan is reviewed is attached to the chat and read by the run (#120).

RCHENLAB/AiScientist#120 (item 2 of #154): the planner looks datasets up from a catalogue of the
account's own uploads and the lab's shared datasets, names one in a step's ``dataset``, and approving
the plan attaches it to the chat, so the run reads that one. Pinned here against the real database and
shared-dataset store of ``test_shared_datasets``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("bcrypt")
pytest.importorskip("itsdangerous")
pytest.importorskip("fastapi")
pytest.importorskip("yaml")

import test_shared_datasets as _store_tests  # noqa: E402

from aiscientist.agents import plan_datasets  # noqa: E402
from aiscientist.agents.plan_doc import PlanDoc, PlanStep  # noqa: E402

_publish = _store_tests._publish
env = _store_tests.env  # the store fixture, used by name below


@pytest.fixture()
def gw(env, monkeypatch):
    from aiscientist.gateway import app as gw_app
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    return gw_app


def _user(env, name="bob") -> int:
    with env["db"].session_scope() as s:
        return s.query(env["models"].User).filter_by(username=name).one().id


def _own(env, user_id, name, path):
    with env["db"].session_scope() as s:
        row = env["models"].Dataset(user_id=user_id, name=name, path=str(path), size_bytes=10, kind="file")
        s.add(row)
        s.commit()
        return row.id, row.uuid


def _chat(env, user_id) -> int:
    with env["db"].session_scope() as s:
        conv = env["models"].Conversation(user_id=user_id, title="plan")
        s.add(conv)
        s.commit()
        return conv.id


def _plan(*datasets) -> str:
    return PlanDoc("Where is RHO expressed?", tuple(
        PlanStep(f"s{i}", f"**Step {i}** — Read the data with `mcp_run_scanpy_qc`.", tool="mcp_run_scanpy_qc",
                 dataset=ds) for i, ds in enumerate(datasets, 1))).to_yaml()


def test_the_catalogue_holds_own_and_shared_datasets_with_versions_and_files_and_no_path(gw, env, tmp_path):
    _publish(env)
    uid = _user(env)
    _own(env, uid, "ko_vs_wt.h5ad", tmp_path / "secret" / "ko_vs_wt.h5ad")
    rows = gw._plan_dataset_catalogue(SimpleNamespace(app_user_id=uid))
    own = [r for r in rows if r["source"] == "own"]
    heca = next(r for r in rows if r["source"] == "shared")
    assert [r["name"] for r in own] == ["ko_vs_wt.h5ad"]
    assert heca["id"] == "heca" and heca["latest"] == "v2026.11"
    assert [v["label"] for v in heca["versions"]] == ["v2026.11"]
    assert {"Iris_snRNA_allcells.h5ad", "Lens_scRNA_allcells.h5ad"} <= {f["name"] for f in heca["versions"][0]["files"]}
    assert "secret" not in repr(rows) and str(env["store"]) not in repr(rows), "no server path reaches the planner"
    text = plan_datasets.catalogue_text(rows)
    assert "Human Eye Cell Atlas (HECA) [heca]" in text and "Iris_snRNA_allcells.h5ad" in text


def test_a_chat_with_no_dataset_gets_the_plans_dataset_as_its_main_one(gw, env, tmp_path):
    _publish(env)
    uid = _user(env)
    conn = SimpleNamespace(app_user_id=uid)
    ref, _ = plan_datasets.checker(gw._plan_dataset_catalogue(conn))(
        {"name": "Human Eye Cell Atlas (HECA)", "file": "Iris_snRNA_allcells.h5ad"})
    state = {"run_options": {"question": "q", "connection_id": "c"}}
    chosen = gw._plan_chosen_inputs(conn, state, _plan(ref))
    assert chosen["shared_datasets"] == [{"shared": "heca", "version": "v2026.11",
                                          "files": ["Iris_snRNA_allcells.h5ad"], "role": "primary"}]
    assert chosen["primary"] == "Human Eye Cell Atlas (HECA) v2026.11 · Iris_snRNA_allcells.h5ad"
    cid = _chat(env, uid)
    views = gw._attach_plan_inputs(conn, str(cid), chosen)
    assert [(v["shared"], v["version"], v["files"], v["role"]) for v in views["shared_datasets"]] == [
        ("heca", "v2026.11", ["Iris_snRNA_allcells.h5ad"], "primary")]
    # The run reads it: the request binds it, and the bind-set puts it first.
    req = gw.LabRequest(connection_id="c", question="q", shared_datasets=chosen["shared_datasets"])
    req._shared_resolved = gw._resolve_shared_refs(req, SimpleNamespace(
        settings=SimpleNamespace(shared_root=""), app_user_id=uid))
    bound = gw._select_bound_datasets(req)
    assert bound[0]["name"].endswith("Iris_snRNA_allcells.h5ad")


def test_own_data_chosen_in_review_is_attached_and_a_bound_dataset_stays_the_main_one(gw, env, tmp_path):
    uid = _user(env)
    conn = SimpleNamespace(app_user_id=uid)
    first_id, first_uuid = _own(env, uid, "ko_vs_wt.h5ad", tmp_path / "ko_vs_wt.h5ad")
    second_id, second_uuid = _own(env, uid, "reference.h5ad", tmp_path / "reference.h5ad")
    # Nothing attached: the plan's own dataset is the main one.
    chosen = gw._plan_chosen_inputs(conn, {"run_options": {}},
                                    _plan({"name": "ko_vs_wt.h5ad", "source": "own", "id": first_uuid}))
    assert chosen["datasets"] == [{"path": str(tmp_path / "ko_vs_wt.h5ad"), "name": "ko_vs_wt.h5ad", "role": "primary"}]
    assert chosen["own_ids"] == [first_id]
    cid = _chat(env, uid)
    views = gw._attach_plan_inputs(conn, str(cid), chosen)
    assert [(d["name"], d["role"]) for d in views["datasets"]] == [("ko_vs_wt.h5ad", "primary")]
    # A chat that reads ko_vs_wt already: a second dataset the plan names is an extra input.
    state = {"run_options": {"datasets": [{"path": str(tmp_path / "ko_vs_wt.h5ad"), "name": "ko_vs_wt.h5ad"}]}}
    plan = _plan({"name": "ko_vs_wt.h5ad", "source": "own", "id": first_uuid},
                 {"name": "reference.h5ad", "source": "own", "id": second_uuid})
    chosen = gw._plan_chosen_inputs(conn, state, plan)
    assert [d.get("role") for d in chosen["datasets"]] == [None, "extra_input"] and chosen["own_ids"] == [second_id]
    assert chosen["primary"] == ""
    # A plan that names only what the run reads binds nothing new.
    assert gw._plan_chosen_inputs(conn, state, _plan({"name": "ko_vs_wt.h5ad", "source": "own",
                                                      "id": first_uuid})) is None
    assert gw._plan_chosen_inputs(conn, state, _plan(None)) is None


def test_a_dataset_that_is_gone_stops_the_approval_with_the_reason(gw, env):
    uid = _user(env)
    with pytest.raises(gw._PlanBindRefused, match="not among your datasets any more"):
        gw._plan_chosen_inputs(SimpleNamespace(app_user_id=uid), {"run_options": {}},
                               _plan({"name": "deleted.h5ad", "source": "own", "id": "gone"}))


# --- review of PR #207: versions and uploads that share a name -------------------------------------

def _two_versions(env):
    _publish(env)
    (env["source"] / "Lens_scRNA_allcells.h5ad").write_bytes(b"lens v2 " * 1000)
    _publish(env, label="v2026.12", changelog="Lens.")


@pytest.mark.parametrize("attached, planned, held", [
    ("latest", "v2026.11", "v2026.12 attached (its latest version)"),   # latest means v2026.12 now
    ("v2026.11", "v2026.12", "v2026.11 attached"),
])
def test_a_version_chosen_in_review_that_the_chat_does_not_hold_is_refused_with_the_reason(
        gw, env, attached, planned, held):
    _two_versions(env)
    uid = _user(env)
    state = {"run_options": {"shared_datasets": [{"shared": "heca", "version": attached, "files": None}]}}
    ref = {"name": "Human Eye Cell Atlas (HECA)", "source": "shared", "id": "heca", "version": planned,
           "file": "Iris_snRNA_allcells.h5ad"}
    with pytest.raises(gw._PlanBindRefused) as refused:
        gw._plan_chosen_inputs(SimpleNamespace(app_user_id=uid), state, _plan(ref))
    said = str(refused.value)
    assert f"reads Human Eye Cell Atlas (HECA) {planned}" in said and held in said
    assert "A run reads one version of a dataset" in said


def test_the_same_version_named_by_its_label_adds_the_file_and_two_versions_are_refused(gw, env):
    _two_versions(env)
    uid = _user(env)
    conn = SimpleNamespace(app_user_id=uid)
    state = {"run_options": {"shared_datasets": [{"shared": "heca", "version": "latest",
                                                  "files": ["Lens_scRNA_allcells.h5ad"]}]}}
    ref = {"name": "Human Eye Cell Atlas (HECA)", "source": "shared", "id": "heca", "version": "v2026.12",
           "file": "Iris_snRNA_allcells.h5ad"}
    chosen = gw._plan_chosen_inputs(conn, state, _plan(ref))
    assert chosen["shared_datasets"] == [{"shared": "heca", "version": "latest",
                                          "files": ["Lens_scRNA_allcells.h5ad", "Iris_snRNA_allcells.h5ad"]}]
    with pytest.raises(gw._PlanBindRefused, match="reads two versions"):
        gw._plan_chosen_inputs(conn, {"run_options": {}}, _plan(ref, {**ref, "version": "v2026.11"}))


def test_two_uploads_with_one_name_are_bound_by_id_and_a_name_alone_is_refused(gw, env, tmp_path):
    uid = _user(env)
    conn = SimpleNamespace(app_user_id=uid)
    _old, _ = _own(env, uid, "filtered_feature_bc_matrix.h5", tmp_path / "a" / "filtered_feature_bc_matrix.h5")
    new_id, new_uuid = _own(env, uid, "filtered_feature_bc_matrix.h5", tmp_path / "b" / "filtered_feature_bc_matrix.h5")
    rows = [r for r in gw._plan_dataset_catalogue(conn) if r["source"] == "own"]
    assert len(rows) == 2 and all(r["uploaded"] for r in rows)
    chosen = gw._plan_chosen_inputs(conn, {"run_options": {}}, _plan(
        {"name": "filtered_feature_bc_matrix.h5", "source": "own", "id": new_uuid}))
    assert chosen["own_ids"] == [new_id] and chosen["datasets"][0]["path"].endswith("b/filtered_feature_bc_matrix.h5")
    with pytest.raises(gw._PlanBindRefused, match="Two of your datasets are named"):
        gw._plan_chosen_inputs(conn, {"run_options": {}}, _plan({"name": "filtered_feature_bc_matrix.h5",
                                                                  "source": "own"}))
