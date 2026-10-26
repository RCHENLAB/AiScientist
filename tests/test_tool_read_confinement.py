"""A tool that reads files on the gateway reads only what the run may read (``tools.sdk.confine_read``).

``inspect_dataset`` and the REST path of ``annotate_variants`` run on the gateway and take a path from
the model. Before this guard either one read any file the gateway's user could read, including the
API-key store and its master key, and ``inspect_dataset`` returned the head of the file to the model.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from aiscientist.tools import sdk
from aiscientist.tools.annotate_variants.tool import make_variant_annotation_tool
from aiscientist.tools.inspect_dataset.tool import make_inspect_dataset_tool

_VCF = "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n17\t100\t.\tG\tA\t.\tPASS\t.\n"


def _ctx(workspace=None, decisions=None, read_roots=()):
    return SimpleNamespace(workspace=workspace, decisions=decisions or {}, read_roots=read_roots,
                           tunnel_port=None, model="m", llm_is_remote=False)


@pytest.fixture
def layout(tmp_path):
    """A run workspace, the owner's uploads, a bound dataset elsewhere, and a file outside them all."""
    ws = tmp_path / "runs" / "alice" / "run1"
    (ws / "work").mkdir(parents=True)
    (ws / "work" / "made.vcf").write_text(_VCF)
    uploads = tmp_path / "runs" / "alice" / "uploads"
    uploads.mkdir()
    (uploads / "notes.csv").write_text("a,b\n1,2\n")
    shared = tmp_path / "server-data" / "cohort"
    shared.mkdir(parents=True)
    (shared / "bound.vcf").write_text(_VCF)
    (shared / "sibling.vcf").write_text(_VCF)
    secret = tmp_path / "elsewhere" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("TOP-SECRET-VALUE\n")
    return SimpleNamespace(ws=ws, uploads=uploads, shared=shared, secret=secret)


@pytest.fixture(autouse=True)
def _no_registered_private_paths(monkeypatch):
    monkeypatch.setattr(sdk, "_private_paths", None)


# --- confine_read ------------------------------------------------------------------------------


def test_reads_inside_the_workspace_the_bound_datasets_and_the_uploads(layout):
    ctx = _ctx(layout.ws, {"dataset_path": str(layout.shared / "bound.vcf")}, (layout.uploads,))
    for p in (layout.ws / "work" / "made.vcf", "work/made.vcf", layout.shared / "bound.vcf",
              layout.uploads / "notes.csv"):
        path, why = sdk.confine_read(p, ctx)
        assert path is not None and path.is_file(), (p, why)
    assert sdk.confine_read("work/made.vcf", ctx)[0] == (layout.ws / "work" / "made.vcf").resolve()


def test_a_bound_file_admits_itself_not_its_folder(layout):
    ctx = _ctx(layout.ws, {"dataset_path": str(layout.shared / "bound.vcf")})
    path, why = sdk.confine_read(layout.shared / "sibling.vcf", ctx)
    assert path is None and "outside this run" in why


def test_a_bound_folder_and_every_bound_input_are_readable(layout):
    ctx = _ctx(layout.ws, {"dataset_root": str(layout.shared),
                           "datasets": [{"path": str(layout.uploads / "notes.csv")}]})
    assert sdk.confine_read(layout.shared / "sibling.vcf", ctx)[0] is not None
    assert sdk.confine_read(layout.uploads / "notes.csv", ctx)[0] is not None


def test_refuses_a_file_outside_and_a_dotdot_escape(layout):
    ctx = _ctx(layout.ws, read_roots=(layout.uploads,))
    assert sdk.confine_read(layout.secret, ctx)[0] is None
    assert sdk.confine_read("../../../elsewhere/secret.txt", ctx)[0] is None
    assert sdk.confine_read(str(layout.uploads / ".." / ".." / ".." / "elsewhere" / "secret.txt"),
                            ctx)[0] is None
    assert sdk.confine_read("", ctx)[0] is None


def test_a_symlink_inside_the_uploads_cannot_point_out_of_them(layout):
    link = layout.uploads / "looks_like_data.vcf"
    os.symlink(layout.secret, link)
    path, why = sdk.confine_read(link, _ctx(layout.ws, read_roots=(layout.uploads,)))
    assert path is None and "outside this run" in why


def test_no_context_means_nothing_is_readable(layout):
    assert sdk.confine_read(layout.secret, None)[0] is None
    assert sdk.confine_read(layout.secret, SimpleNamespace())[0] is None


def test_a_private_path_is_refused_even_inside_a_root(layout, monkeypatch):
    store = layout.ws / "state"           # a deployment that put its state dir inside a run root
    store.mkdir()
    (store / "llm_key.master").write_text("MASTER")
    monkeypatch.setattr(sdk, "_private_paths", lambda: [store])
    path, why = sdk.confine_read(store / "llm_key.master", _ctx(layout.ws))
    assert path is None and "outside this run" in why
    assert sdk.confine_read(layout.ws / "work" / "made.vcf", _ctx(layout.ws))[0] is not None


# --- the gateway's private paths ---------------------------------------------------------------


def test_the_gateway_registers_its_credential_stores(tmp_path, monkeypatch):
    from aiscientist.gateway import app as gw_app  # noqa: F401 - importing it registers the provider

    state = tmp_path / "state"
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(state))
    monkeypatch.delenv("AISCIENTIST_LLM_KEY_MASTER_FILE", raising=False)
    monkeypatch.setattr(sdk, "_private_paths", gw_app._private_host_paths)
    named = {p.resolve() for p in gw_app._private_host_paths()}
    for p in (state, state / "llm_creds", state / "llm_key.master", state / "ssh_creds",
              state / "session_secret", gw_app.ROOT / ".env"):
        assert p.resolve() in named, p
    # Without a state dir the stores sit under the working directory: still each one named.
    monkeypatch.delenv("AISCIENTIST_STATE_DIR")
    monkeypatch.chdir(tmp_path)
    named = {p.resolve() for p in gw_app._private_host_paths()}
    assert {(tmp_path / "llm_creds").resolve(), (tmp_path / "llm_key.master").resolve(),
            (tmp_path / "ssh_creds").resolve()} <= named
    assert tmp_path.resolve() not in named          # the working directory itself stays readable


def test_inspect_dataset_cannot_read_the_api_key_store(tmp_path, monkeypatch):
    """The reported hole, end to end: a saved key, its master key, and the tool asked for both."""
    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway import llm_credentials

    state = tmp_path / "state"
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(state))
    monkeypatch.delenv("AISCIENTIST_LLM_KEY_MASTER_FILE", raising=False)
    monkeypatch.setenv("AISCIENTIST_LLM_KEY_ENCRYPTION", "1")
    monkeypatch.setattr(sdk, "_private_paths", gw_app._private_host_paths)
    fake_key = "sk-example-" + "0123" * 4      # built, not literal: the PR gate scans for key literals
    cred = llm_credentials.create("bob", provider="anthropic", base_url="https://api.anthropic.com",
                                  model="m", api_key=fake_key)
    master = (state / "llm_key.master").read_text().strip()
    key_file = state / "llm_creds" / "bob" / f"{cred['id']}.key"
    ciphertext = key_file.read_text().strip()

    ws = tmp_path / "runs" / "alice" / "run1"
    ws.mkdir(parents=True)
    bound = ws / "data.vcf"
    bound.write_text(_VCF)
    tool = make_inspect_dataset_tool()
    for ctx in (_ctx(ws, {"dataset_path": str(bound)}), _ctx(ws)):
        for asked in (state / "llm_key.master", key_file, state / "llm_creds" / "bob" / "index.json"):
            out = tool.executor({"path": str(asked)}, ctx)
            text = json.dumps(out)
            assert out["status"] == "error", out
            assert master not in text and ciphertext[:40] not in text and fake_key not in text
    # A deployment whose run root contains the state dir is still refused (the private paths).
    out = tool.executor({"path": str(state / "llm_key.master")}, _ctx(tmp_path))
    assert out["status"] == "error" and master not in json.dumps(out)


# --- inspect_dataset ---------------------------------------------------------------------------


def test_inspect_dataset_reads_a_run_file_and_an_upload(layout):
    tool = make_inspect_dataset_tool()
    ctx = _ctx(layout.ws, {"dataset_path": str(layout.shared / "bound.vcf")}, (layout.uploads,))
    assert tool.executor({"path": "work/made.vcf"}, ctx)["peek"]["name"] == "made.vcf"
    assert tool.executor({"path": str(layout.uploads / "notes.csv")}, ctx)["peek"]["name"] == "notes.csv"
    assert tool.executor({}, ctx)["peek"]["name"] == "bound.vcf"


def test_inspect_dataset_refuses_an_outside_file_instead_of_reading_the_bound_one(layout):
    tool = make_inspect_dataset_tool()
    ctx = _ctx(layout.ws, {"dataset_path": str(layout.shared / "bound.vcf")}, (layout.uploads,))
    out = tool.executor({"path": str(layout.secret)}, ctx)
    assert out["status"] == "error" and "outside this run" in out["error"]
    assert "TOP-SECRET-VALUE" not in json.dumps(out)


def test_inspect_dataset_still_falls_back_to_the_bound_dataset_for_an_hpc3_path(layout):
    tool = make_inspect_dataset_tool()
    ctx = _ctx(layout.ws, {"dataset_path": str(layout.shared / "bound.vcf")})
    out = tool.executor({"path": "/dfs3b/ruic20_lab/uploads/alice/x.vcf"}, ctx)
    assert out["status"] == "ok" and out["peek"]["name"] == "bound.vcf"


# --- annotate_variants (REST, in-process) ------------------------------------------------------


def test_annotate_variants_refuses_a_vcf_outside_the_run(layout, monkeypatch):
    import aiscientist.tools.annotate_variants.tool as va

    monkeypatch.setattr(va, "vep_annotate", lambda variants, **k: [])
    tool = make_variant_annotation_tool()
    ctx = _ctx(layout.ws, read_roots=(layout.uploads,))
    out = tool.executor({"vcf_path": str(layout.secret)}, ctx)
    assert out["status"] == "error" and "outside this run" in out["error"]
    assert "TOP-SECRET-VALUE" not in json.dumps(out)


def test_annotate_variants_reads_a_run_vcf_and_inline_text(layout, monkeypatch):
    import aiscientist.tools.annotate_variants.tool as va

    seen = []
    monkeypatch.setattr(va, "vep_annotate", lambda variants, **k: seen.append(len(variants)) or [])
    tool = make_variant_annotation_tool()
    ctx = _ctx(layout.ws)
    tool.executor({"vcf_path": "work/made.vcf"}, ctx)
    tool.executor({"vcf_path": _VCF}, ctx)                       # raw VCF text, not a path
    assert seen == [1, 1]
