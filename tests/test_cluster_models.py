"""The cluster model list: seed, admin edits, how an entry reaches the serve job, and the routes.

A cluster model is a repo PLUS the image/quantization it needs, so most of these check that the
three travel together — the failure worth guarding is 3.8 weights handed to the 3.6-era vLLM.
"""

from __future__ import annotations

import asyncio

import pytest

from aiscientist.gateway import cluster_models as cm
from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.gpu import GPUAllocation
from aiscientist.gateway.settings import HPCSettings

Q38, Q36 = "RedHatAI/Qwen3.8-27B-INT4", "QuantTrio/Qwen3.6-35B-A3B-AWQ"


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path))
    return tmp_path


def test_seed_is_env_default_plus_known_recipes():
    ms = cm.load(HPCSettings())
    assert [m.repo for m in ms][:2] == [Q38, Q36]
    assert [m.default for m in ms] == [True, False]
    q36 = next(m for m in ms if m.repo == Q36)
    assert q36.quantization == "awq_marlin" and q36.image.endswith("/vllm.sif")


def test_apply_moves_repo_image_and_quantization_together():
    s = HPCSettings()
    cm.apply(s, cm.get(cm.slug(Q36)))
    assert (s.vllm_model, s.vllm_quantization) == (Q36, "awq_marlin")
    assert s.vllm_image.endswith("/vllm.sif")


def test_upsert_default_remove_roundtrip(state_dir):
    cm.upsert({"repo": "org/New-Model", "label": "New", "quantization": ""})
    assert (state_dir / "cluster_models.json").exists()
    new = cm.get("org-new-model")
    assert new and new.image.endswith("vllm-0.28.0.sif")        # a sane image when none given
    cm.set_default("org-new-model")
    assert cm.get(None).id == "org-new-model"
    assert sum(m.default for m in cm.load()) == 1
    # disabling the default hands the default to another enabled entry
    cm.upsert({**cm.get("org-new-model").public(), "enabled": False})
    assert cm.get(None).id != "org-new-model" and cm.get("org-new-model") is None
    assert cm.remove("org-new-model") and cm.get("org-new-model") is None
    with pytest.raises(ValueError):
        cm.upsert({"repo": "no-slash"})


def test_upsert_of_known_repo_fills_the_recipe():
    cm.remove(cm.slug(Q36))
    m = cm.upsert({"repo": Q36})
    assert m.quantization == "awq_marlin" and m.label.startswith("Qwen3.6")


def test_cannot_remove_the_last_enabled_model():
    cm.remove(cm.slug(Q36))
    with pytest.raises(ValueError):
        cm.remove(cm.slug(Q38))


def test_scan_disk_parses_hf_cache_listing():
    class Ex:
        def exec(self, command, timeout=None):
            return ExecResult(command=command, exit_status=0, stdout=(
                f"models--RedHatAI--Qwen3.8-27B-INT4 {16 * 1024 * 1024}\n"
                "models--QuantTrio--Qwen3.6-35B-A3B-AWQ 25165824\nCACHEDIR.TAG\n"), stderr="")
    assert cm.scan_disk(Ex(), HPCSettings()) == {Q38: 16.0, Q36: 24.0}


# --- routes -----------------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from aiscientist.gateway import app as gw
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    return TestClient(gw.app)


def _conn(mock: bool, model: str = Q38):
    from aiscientist.gateway import app as gw
    c = gw.Connection(HPCSettings(vllm_model=model), mock=mock, loop=asyncio.new_event_loop(), username="alice")
    c.status = "ready"
    c.cluster_model_id = cm.slug(model)
    gw.CONNECTIONS[c.id] = c
    return c


def test_list_and_admin_edits(client):
    d = client.get("/api/cluster-models").json()
    assert d["can_manage"] and [m["repo"] for m in d["models"]][:2] == [Q38, Q36]
    assert d["scanned"] is False and d["unregistered"] is None
    assert client.put("/api/cluster-models", json={"repo": "org/x"}).status_code == 200
    assert client.post("/api/cluster-models/org-x/default").json()["model"]["default"] is True
    r = client.delete("/api/cluster-models/org-x")
    assert r.status_code == 200 and "stay on HPC3" in r.json()["note"]


def test_non_admin_cannot_edit(client, monkeypatch):
    from aiscientist.gateway import app as gw
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw, "_optional_user", lambda request: type("U", (), {"role": "user"})())
    assert client.put("/api/cluster-models", json={"repo": "org/x"}).status_code == 403
    assert client.get("/api/cluster-models").json()["can_manage"] is False


def test_switching_to_the_model_already_served_is_instant(client):
    from aiscientist.gateway import app as gw
    c = _conn(mock=False)
    c.alloc = GPUAllocation(job_id="555", node="hpc3-gpu-m54-00", port=30000)
    try:
        r = client.post("/api/llm-endpoint", json={"connection_id": c.id, "cluster_model_id": cm.slug(Q38)})
        assert r.json()["status"] == "ok"
        assert c.summary()["llm_endpoint"]["cluster_model_id"] == cm.slug(Q38)
    finally:
        gw.CONNECTIONS.pop(c.id, None)


def test_switching_cluster_model_reprovisions_with_the_new_recipe(client, monkeypatch):
    from aiscientist.gateway import app as gw
    started = []

    async def fake(conn, reason):
        started.append((conn.settings.vllm_model, conn.settings.vllm_quantization, reason))

    monkeypatch.setattr(gw, "_reprovision_gpu", fake)
    c = _conn(mock=False)
    c.alloc = GPUAllocation(job_id="555", node="hpc3-gpu-m54-00", port=30000)
    try:
        r = client.post("/api/llm-endpoint", json={"connection_id": c.id, "cluster_model_id": cm.slug(Q36)})
        assert r.json()["status"] == "provisioning"
        assert started and started[0][:2] == (Q36, "awq_marlin")
        assert c.cluster_model_id == cm.slug(Q36)
    finally:
        gw.CONNECTIONS.pop(c.id, None)


def test_switch_is_refused_mid_run(client, monkeypatch):
    from aiscientist.gateway import app as gw
    c = _conn(mock=False)
    c.alloc = GPUAllocation(job_id="555", node="hpc3-gpu-m54-00", port=30000)
    monkeypatch.setattr(type(c), "chat_running", property(lambda self: True), raising=False)
    try:
        r = client.post("/api/llm-endpoint", json={"connection_id": c.id, "cluster_model_id": cm.slug(Q36)})
        assert r.status_code == 409 and c.settings.vllm_model == Q38
    finally:
        gw.CONNECTIONS.pop(c.id, None)


def test_connect_with_unknown_cluster_model_is_rejected(client):
    r = client.post("/api/connect", json={"ucinetid": "alice", "mock": True, "cluster_model_id": "nope"})
    assert r.status_code == 400


def test_apply_filters_race_candidates_by_allowed_cards_and_restores_them():
    race = "free-gpu32,gpu:RTX6000:1,ruic20_lab;gpu,gpu:A100:1,ruic20_lab_gpu"
    s = HPCSettings(gpu_candidates=race)
    cm.apply(s, cm.ClusterModel(id="x", repo="org/x", cards="RTX6000"))
    assert s.gpu_candidates == "free-gpu32,gpu:RTX6000:1,ruic20_lab" and s.vllm_gpu_cards == "RTX6000"
    cm.apply(s, cm.get(cm.slug(Q36)))                                   # no card restriction
    assert s.gpu_candidates == race and s.vllm_gpu_cards == ""
    cm.apply(s, cm.ClusterModel(id="y", repo="org/y", cards="H100"))    # nothing matches: keep all
    assert s.gpu_candidates == race


def test_in_place_switch_keeps_the_tunnel_and_waits_for_the_new_model(monkeypatch):
    """Right after the spec is rewritten the same port still answers with the OLD model; a plain
    reachability check would pass against it. The provisioning must wait for the new one."""
    from aiscientist.gateway import app as gw, gpu, vllm_client
    c = _conn(mock=False, model=Q36)
    c.executor = type("Ex", (), {"exec": lambda self, cmd, timeout=None: ExecResult(cmd, 0, "RUNNING", "")})()
    c.alloc = GPUAllocation(job_id="555", node="hpc3-gpu-m54-00", port=30000)
    c.tunnel_port = 51000
    opened = []
    c.executor.open_tunnel = lambda *a, **k: opened.append(a) or 52000
    monkeypatch.setattr(gpu, "ensure_serve_job", lambda ex, s, emit, **k: GPUAllocation(
        job_id="555", node="hpc3-gpu-m54-00", port=30000, reused=True, swapped=True))
    served = iter([[Q38], [Q38], [Q36], [Q36], [Q36]])
    monkeypatch.setattr(vllm_client, "get_tags", lambda port, timeout=5: next(served))
    monkeypatch.setattr(vllm_client, "ensure_installed", lambda ex, s, emit: {})
    monkeypatch.setattr(vllm_client, "ensure_model", lambda port, s, emit: None)
    monkeypatch.setattr(vllm_client, "warmup", lambda port, model, emit: None)
    monkeypatch.setattr(gw, "_refresh_health", lambda conn, emit: {})
    monkeypatch.setattr("time.sleep", lambda s: None)
    try:
        gw._provision_gpu_blocking(c)
        assert opened == [] and c.tunnel_port == 51000          # same job/node/port: tunnel kept
        assert c.status == "ready" and c.selected_model == Q36
        msgs = [e.get("message", "") for e in c.log]
        assert any("still serves" in m and Q38 in m for m in msgs)
    finally:
        gw.CONNECTIONS.pop(c.id, None)
