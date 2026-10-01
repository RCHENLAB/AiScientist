"""How a session's LLM choice binds to an endpoint — ``_lab_llm`` against saved credentials.

The env-var path (``AISCIENTIST_LLM_BASE_URL``) is covered in ``test_gateway_lab.py``; this file
covers the product path, where the endpoint comes from the user's OWN stored API credential.

The property worth naming: the key is resolved ONCE, at run start, and captured by the closures.
That is what lets a user rotate a key while a long analysis is running without breaking it.

Imports the FastAPI app (needs the gateway extra installed). Skipped cleanly if
fastapi is absent.
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway import vllm_client  # noqa: E402
from aiscientist.gateway.errors import GatewayError  # noqa: E402

_ENV = ("AISCIENTIST_LLM_BASE_URL", "AISCIENTIST_LLM_API_KEY", "AISCIENTIST_LLM_MODEL",
        "AISCIENTIST_LAB_LLM_BASE_URL", "AISCIENTIST_LAB_LLM_MODEL", "AISCIENTIST_LAB_LLM_API_KEY")


class _Conn:
    """Enough of a Connection for _lab_llm: an owner, a tunnel, and an endpoint choice."""

    selected_model = "QuantTrio/Qwen3.6-35B-A3B-AWQ"
    tunnel_port = 37219
    mock = False

    def __init__(self, owner="alice", credential_id=None) -> None:
        self.owner = owner
        self.llm_choice = {"credential_id": credential_id} if credential_id else {}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AISCIENTIST_LLM_KEY_ENCRYPTION", raising=False)
    for v in _ENV:
        monkeypatch.delenv(v, raising=False)

    from aiscientist.gateway import llm_credentials
    importlib.reload(llm_credentials)
    return llm_credentials


@pytest.fixture()
def calls(monkeypatch):
    """Capture every outbound LLM call as (role, port, model, base_url, api_key, max_tokens)."""
    seen: list[dict] = []
    # The lab roles go through ``complete_ex`` (text AND the provider's usage) so the gateway can
    # record what each call cost; it returns a (text, usage) pair rather than bare text.
    monkeypatch.setattr(vllm_client, "complete_ex",
                        lambda port, model, messages, **kw: seen.append(
                            {"role": "lab", "port": port, "model": model,
                             "base": kw.get("base_url"), "key": kw.get("api_key"),
                             "max_tokens": kw.get("max_tokens")}) or ("x", {}))
    monkeypatch.setattr(vllm_client, "chat_tools",
                        lambda port, model, messages, tools, **kw: seen.append(
                            {"role": "sci", "port": port, "model": model,
                             "base": kw.get("base_url"), "key": kw.get("api_key")}) or {"content": ""})
    return seen


def _cred(store, owner="alice", **kw):
    body = dict(provider="deepseek", base_url="https://api.deepseek.com/v1",
                model="deepseek-chat", api_key="sk-user-own-example", label="My DeepSeek")
    body.update(kw)
    return store.create(owner, **body)


def _drive(conn, seen):
    llm = gw_app._lab_llm(conn)
    llm.complete_fn([{"role": "user", "content": "plan"}])
    llm.scientist_chat([{"role": "user", "content": "run"}], [])
    return llm, {s["role"]: s for s in seen}


# --- the product path --------------------------------------------------------


def test_a_saved_credential_routes_both_roles_to_the_users_endpoint(store, calls):
    cred = _cred(store)
    llm, by_role = _drive(_Conn(credential_id=cred["id"]), calls)

    for role in ("lab", "sci"):
        assert by_role[role]["base"] == "https://api.deepseek.com/v1"
        assert by_role[role]["key"] == "sk-user-own-example"
        assert by_role[role]["model"] == "deepseek-chat"
    assert llm.label == "My DeepSeek"
    assert llm.scientist_remote is True and llm.lab_role_remote is True


def test_lab_model_sends_only_the_reasoning_roles_to_the_stronger_model(store, calls):
    """The paid split: PI/Critic on the strong model, the high-volume Scientist on the cheap one."""
    cred = _cred(store, lab_model="deepseek-reasoner")
    llm, by_role = _drive(_Conn(credential_id=cred["id"]), calls)

    assert by_role["lab"]["model"] == "deepseek-reasoner"
    assert by_role["sci"]["model"] == "deepseek-chat"
    assert by_role["lab"]["base"] == by_role["sci"]["base"], "one endpoint, two models"
    assert llm.lab_label == "deepseek-reasoner @ My DeepSeek"


def test_no_choice_still_uses_the_cluster_tunnel(store, calls):
    """Back-compat: an untouched session behaves exactly as before this feature."""
    llm, by_role = _drive(_Conn(), calls)

    assert by_role["lab"]["base"] is None and by_role["lab"]["port"] == 37219
    assert by_role["sci"]["base"] is None and by_role["sci"]["port"] == 37219
    assert llm.label == "vLLM"
    assert llm.scientist_remote is False and llm.lab_role_remote is False


def test_a_credential_beats_the_env_fallback(store, calls, monkeypatch):
    """The env path was only ever a dev convenience; a user's own choice outranks it."""
    monkeypatch.setenv("AISCIENTIST_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("AISCIENTIST_LLM_API_KEY", "sk-server-wide")
    cred = _cred(store)

    _, by_role = _drive(_Conn(credential_id=cred["id"]), calls)
    assert by_role["sci"]["base"] == "https://api.deepseek.com/v1"
    assert by_role["sci"]["key"] == "sk-user-own-example"


# --- the reason the id is stable --------------------------------------------


def test_rotating_a_key_mid_run_does_not_disturb_the_running_lab(store, calls):
    """The key is read once at bind time, so an analysis in flight keeps working."""
    cred = _cred(store)
    conn = _Conn(credential_id=cred["id"])

    llm = gw_app._lab_llm(conn)                       # run starts, key snapshotted
    store.rotate_key("alice", cred["id"], "sk-rotated-example")

    llm.complete_fn([{"role": "user", "content": "still going"}])
    assert calls[-1]["key"] == "sk-user-own-example", "the in-flight run keeps its own key"

    later = gw_app._lab_llm(conn)                     # the NEXT run picks up the new one
    later.complete_fn([{"role": "user", "content": "next run"}])
    assert calls[-1]["key"] == "sk-rotated-example"


def test_an_edited_model_applies_on_the_next_run_without_reconnecting(store, calls):
    cred = _cred(store)
    conn = _Conn(credential_id=cred["id"])
    store.update("alice", cred["id"], model="deepseek-reasoner")

    _drive(conn, calls)
    assert calls[-1]["model"] == "deepseek-reasoner"


# --- failure and confinement -------------------------------------------------


def test_a_missing_key_file_fails_with_a_local_cause(store):
    """Better than letting the provider answer 401: the fix is to re-enter the key, and a bare
    'unauthorized' would not say so."""
    cred = _cred(store)
    from pathlib import Path
    Path(store.get_credential("alice", cred["id"])["key_path"]).unlink()

    with pytest.raises(GatewayError) as exc:
        gw_app._lab_llm(_Conn(credential_id=cred["id"]))
    assert "Re-enter it" in str(exc.value)


def test_another_owners_credential_id_does_not_resolve(store, calls):
    """A leaked id is not usable from someone else's session — it falls back to the cluster,
    which is the safe direction (data stays local) rather than the dangerous one."""
    cred = _cred(store, owner="alice")

    _, by_role = _drive(_Conn(owner="bob", credential_id=cred["id"]), calls)
    assert by_role["sci"]["base"] is None and by_role["sci"]["port"] == 37219


def test_a_deleted_credential_falls_back_to_the_cluster(store, calls):
    cred = _cred(store)
    conn = _Conn(credential_id=cred["id"])
    store.delete_credential("alice", cred["id"])

    _, by_role = _drive(conn, calls)
    assert by_role["sci"]["base"] is None, "falls back local, never to some other endpoint"


def test_a_self_hosted_endpoint_is_not_reported_as_egress(store, calls):
    """Another lab's vLLM on the same host is a credential, but nothing leaves the machine."""
    cred = _cred(store, base_url="http://127.0.0.1:8001/v1", provider="custom")
    llm, _ = _drive(_Conn(credential_id=cred["id"]), calls)
    assert llm.scientist_remote is False and llm.lab_role_remote is False


# --- what the UI is told -----------------------------------------------------


def test_status_summary_names_the_endpoint_and_never_the_key(store):
    cred = _cred(store, lab_model="deepseek-reasoner")
    conn = _Conn(credential_id=cred["id"])

    summary = gw_app.Connection._llm_endpoint_summary(conn)
    assert summary["kind"] == "credential"
    assert summary == {**summary, "model": "deepseek-chat", "lab_model": "deepseek-reasoner",
                       "remote": True, "label": "My DeepSeek"}
    assert "sk-user-own-example" not in repr(summary)
    assert summary["key_hint"] == "sk-use…mple"   # first 6 + last 4 of the fixture key


def test_status_summary_reports_a_deleted_credential_rather_than_pretending(store):
    cred = _cred(store)
    conn = _Conn(credential_id=cred["id"])
    store.delete_credential("alice", cred["id"])

    assert gw_app.Connection._llm_endpoint_summary(conn)["kind"] == "missing"


def test_status_summary_for_the_cluster_default(store):
    summary = gw_app.Connection._llm_endpoint_summary(_Conn())
    assert summary == {"kind": "cluster", "cluster_model_id": "", "label": "vLLM (UCI GPU node)",
                       "model": None, "remote": False}


# --- egress: consent is the control, the scanner is the backstop -------------


def test_a_secret_in_the_reasoning_prompt_is_blocked_before_it_leaves(store, calls):
    """An API key cannot be un-sent, so this is a hard block regardless of endpoint.

    The fixture is AWS's own published example key: the egress scanner recognises it
    (``AKIA`` + 16) while it is self-evidently not a live credential, so the repo secret
    gate does not have to special-case this file. Swapping it for a plausible-looking
    key would re-trip that gate; swapping it for an undetectable string would make this
    test pass for the wrong reason.
    """
    cred = _cred(store)
    llm = gw_app._lab_llm(_Conn(credential_id=cred["id"]))

    with pytest.raises(GatewayError) as exc:
        llm.complete_fn([{"role": "user", "content": "here is my key AKIAIOSFODNN7EXAMPLE"}])
    assert "withheld from the remote model" in str(exc.value)
    assert calls == [], "nothing reached the wire"


def test_a_raw_matrix_in_the_reasoning_prompt_is_blocked(store, calls):
    """The reasoning payload should carry derived fields only; a matrix there is a bug."""
    cred = _cred(store)
    llm = gw_app._lab_llm(_Conn(credential_id=cred["id"]))
    matrix = "\n".join(",".join(str(i * j) for j in range(8)) for i in range(6))

    with pytest.raises(GatewayError):
        llm.complete_fn([{"role": "user", "content": matrix}])
    assert calls == []


def test_ordinary_reasoning_prompts_pass_through(store, calls):
    cred = _cred(store)
    llm = gw_app._lab_llm(_Conn(credential_id=cred["id"]))
    llm.complete_fn([{"role": "user", "content":
                      "Plan: run QC, then cluster. Accepted finding: RHO is enriched in rods."}])
    assert len(calls) == 1


def test_the_local_cluster_path_is_not_guarded_this_way(store, calls):
    """Nothing leaves the box, so the reasoning payload needs no egress check — and blocking
    there would kill legitimate steps for no privacy gain."""
    llm = gw_app._lab_llm(_Conn())
    matrix = "\n".join(",".join(str(i * j) for j in range(8)) for i in range(6))
    llm.complete_fn([{"role": "user", "content": matrix}])
    assert len(calls) == 1


# --- provenance recorded in the technical report -----------------------------


def test_provenance_note_states_the_split_when_roles_differ(store, calls):
    cred = _cred(store, lab_model="deepseek-reasoner")
    note = gw_app._llm_provenance_note(gw_app._lab_llm(_Conn(credential_id=cred["id"])))

    assert "OFF-SITE" in note
    assert "deepseek-reasoner @ My DeepSeek" in note
    assert "Raw expression tables and variant records were not sent" in note


def test_provenance_note_says_so_plainly_when_nothing_left(store, calls):
    note = gw_app._llm_provenance_note(gw_app._lab_llm(_Conn()))
    assert "No prompt left UCI" in note
    assert "OFF-SITE" not in note
