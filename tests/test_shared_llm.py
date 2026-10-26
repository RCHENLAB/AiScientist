"""The lab's shared reasoning-model server and its keeper (gateway/shared_llm.py).

A scripted fake cluster stands in for HPC3: the keeper's listing command returns whatever jobs the
test put in the queue, ``sbatch`` adds one, ``scancel`` removes them. No SSH, no Slurm, no model.
"""

from __future__ import annotations

import re
import types

import pytest

from aiscientist.gateway import shared_llm
from aiscientist.gateway.errors import GatewayError
from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.settings import HPCSettings
from aiscientist.gateway.shared_llm import SharedLLM, SharedLLMSettings

HOME = "/data/homezvol3/keeper"


def _settings(**over) -> SharedLLMSettings:
    base = dict(enabled=True, hpc_user="keeper", key_path="/keys/id",
                image="/dfs3b/ruic20_lab/software/AiScientist/containers/vllm-0.28.0.sif",
                account="ruic20_lab")
    base.update(over)
    return SharedLLMSettings(**base)


class FakeCluster:
    """HPC3 as the keeper sees it: a queue, the port/ready files, and a log per job."""

    host, username = "hpc3.example.edu", "keeper"

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}        # id -> {state, node, left, port, ready, reason}
        self.logs: dict[str, str] = {}
        self.submitted: list[str] = []         # the script of every sbatch
        self.cancelled: list[str] = []
        self.commands: list[str] = []
        self.squeue_ok = True
        self.sbatch_error = ""
        self._next = 9000
        self.target = None
        self.closed = False

    def add(self, state="R", node="gpu-a", left="2-00:00:00", port=31000, ready=True, reason="None") -> str:
        self._next += 1
        jid = str(self._next)
        self.jobs[jid] = dict(state=state, node=node if state == "R" else "", left=left,
                              port=port if state == "R" else 0,
                              ready=ready and state == "R", reason=reason)
        return jid

    def _ok(self, out: str = "", status: int = 0, err: str = "") -> ExecResult:
        return ExecResult(command="", exit_status=status, stdout=out, stderr=err)

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        self.commands.append(command)
        if command.startswith("umask 077"):          # create-or-read the API key
            return self._ok("sk-aisci-test\n")
        if "squeue --me" in command:
            lines = [f"HOME|{HOME}"]
            if self.squeue_ok:
                lines += [f"JOB|{j}|{d['state']}|{d['node']}|{d['left']}|{d['reason']}"
                          for j, d in self.jobs.items()]
            lines.append(f"RC|{0 if self.squeue_ok else 1}")
            lines += [f"PORT|{j}|{d['port']}|{'ready' if d['ready'] else ''}"
                      for j, d in self.jobs.items() if d["port"]]
            return self._ok("\n".join(lines) + "\n")
        if command.startswith("mkdir -p") and "serve.sbatch" in command:
            self.submitted.append(command)
            return self._ok()
        if "sbatch --parsable" in command:
            if self.sbatch_error:
                return self._ok(status=1, err=self.sbatch_error)
            return self._ok(self.add(state="PD", reason="Priority") + "\n")
        if command.startswith("scancel "):
            for jid in command.split()[1:]:
                self.cancelled.append(jid)
                self.jobs.pop(jid, None)
            return self._ok()
        if command.startswith("tail -n"):
            m = re.search(r"serve-(\d+)\.log", command)
            return self._ok(self.logs.get(m.group(1), "") if m else "")
        return self._ok()

    def open_tunnel_to(self, target, local_port: int = 0, label: str = "") -> int:
        self.target = target
        return local_port or 18811

    def close(self) -> None:
        self.closed = True


def _keeper(cluster: FakeCluster, settings: SharedLLMSettings | None = None, *, healthy=True):
    clock = types.SimpleNamespace(now=1_000_000.0)
    probe_calls: list = []

    def probe(port, key, served):
        probe_calls.append((port, key, served))
        ok = healthy() if callable(healthy) else healthy
        return (ok, "" if ok else "no answer from /v1/models")

    keeper = SharedLLM(settings or _settings(), HPCSettings(), lambda: cluster,
                       clock=lambda: clock.now, probe=probe, log=lambda line: None)
    keeper.clock = clock
    keeper.probe_calls = probe_calls
    return keeper


def _sbatch_count(cluster: FakeCluster) -> int:
    return sum("sbatch --parsable" in c for c in cluster.commands)


# -- the job script ------------------------------------------------------------------------------

def test_the_script_asks_for_the_cards_and_serves_across_all_of_them():
    script = shared_llm.serve_script(_settings(), HPCSettings(), HOME)
    assert "#SBATCH --job-name=aiscientist-llm-shared" in script
    assert "#SBATCH --partition=free-gpu32" in script
    assert "#SBATCH --gres=gpu:RTX6000:4" in script
    assert "#SBATCH --time=3-00:00:00" in script
    assert "#SBATCH --account=ruic20_lab\n" in script
    # Slurm does not expand $HOME in a directive: the log path is spelled out.
    assert f"#SBATCH --output={HOME}/.bioagent/shared-llm/serve-%j.log" in script
    assert "--tensor-parallel-size 4" in script
    assert "--served-model-name \"$SERVED_NAME\"" in script and 'SERVED_NAME="deepseek-v4-flash"' in script
    assert "need 4" in script and "-ne 4" in script          # the dead-card check
    assert "@@" not in script                                # every placeholder was filled


def test_the_api_key_is_read_on_the_node_and_never_written_into_the_script():
    script = shared_llm.serve_script(_settings(), HPCSettings(), HOME)
    assert 'export VLLM_API_KEY="$(cat "$SHARED_DIR/api.key")"' in script
    assert "--api-key" not in script
    assert "sk-" not in script


def test_the_script_passes_the_pr_policy_gate():
    # The gate reads a quoted value of 12 or more characters assigned to a name ending in API_KEY
    # as a committed key. The job script lives in a tracked file, so a quoted reference to the
    # variable there fails every PR that touches it.
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "pr_review_gate.py"
    spec = importlib.util.spec_from_file_location("pr_review_gate", path)
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate              # its dataclasses look their module up by name
    spec.loader.exec_module(gate)
    assert not gate.contains_secret_like(shared_llm.serve_script(_settings(), HPCSettings(), HOME))
    assert not gate.contains_secret_like(Path(shared_llm.__file__).read_text())


def test_the_server_runs_under_the_lab_group_when_its_files_are_on_lab_storage():
    hpc = HPCSettings()
    on_lab = shared_llm.serve_script(_settings(), hpc, HOME)
    assert f"sg {hpc.lab_data_group} -c 'bash -s' <<'AISCIENTIST_SHARED_SERVE'" in on_lab
    elsewhere = shared_llm.serve_script(
        _settings(image="/opt/images/vllm.sif"), HPCSettings(hf_home="/scratch/hf"), HOME)
    assert "\nbash -s <<'AISCIENTIST_SHARED_SERVE'" in elsewhere


def test_one_card_gets_no_tensor_parallel_flag_and_excludes_are_written():
    script = shared_llm.serve_script(_settings(gres="gpu:A100:1", exclude="bad-node"), HPCSettings(), HOME)
    assert "--tensor-parallel-size" not in script
    assert "#SBATCH --exclude=bad-node\n" in script
    assert shared_llm.gpu_count("gpu:RTX6000:4") == 4 and shared_llm.gpu_count("gpu:2") == 2
    assert shared_llm.gpu_count("gpu:A100") == 1


# -- reading the queue ---------------------------------------------------------------------------

def test_the_listing_gives_jobs_their_port_and_readiness():
    home, ok, jobs = shared_llm.parse_listing(
        f"HOME|{HOME}\nJOB|101|R|gpu-a|2-01:00:00|None\nJOB|102|PD||3-00:00:00|Priority\nRC|0\n"
        "PORT|101|31000|ready\nPORT|77|30000|\n")
    assert home == HOME and ok
    a, b = jobs
    assert (a.job_id, a.running, a.node, a.port, a.ready, a.time_left_s) == ("101", True, "gpu-a", 31000, True, 176400)
    assert (b.job_id, b.running, b.port, b.time_left_s, b.reason) == ("102", False, 0, None, "Priority")


def test_a_failed_squeue_is_not_an_empty_queue():
    _home, ok, jobs = shared_llm.parse_listing(f"HOME|{HOME}\nRC|1\n")
    assert not ok and jobs == []


# -- keeping the job -----------------------------------------------------------------------------

def test_no_job_means_one_is_submitted_once():
    cluster = FakeCluster()
    keeper = _keeper(cluster)
    keeper.step()
    assert _sbatch_count(cluster) == 1
    assert "aiscientist-llm-shared" in cluster.submitted[0]
    keeper.step()                                  # it is queued now: nothing more to do
    assert _sbatch_count(cluster) == 1
    assert keeper.status()["state"] == "queued" and keeper.binding() is None


def test_a_running_job_that_answers_is_served_from():
    cluster = FakeCluster()
    jid = cluster.add(node="gpu-a", port=31000, left="2-12:00:00")
    keeper = _keeper(cluster)
    keeper.step()
    assert cluster.target() == ("gpu-a", 31000)
    bound = keeper.binding()
    assert bound and (bound.port, bound.model, bound.api_key, bound.effort) == (18811, "deepseek-v4-flash", "sk-aisci-test", "low")
    assert bound.think_kwargs == {"enable_thinking": True}
    st = keeper.status()
    assert (st["state"], st["job_id"], st["node"]) == ("up", jid, "gpu-a")
    assert st["expires_at"] == keeper.clock.now + 60 * 3600
    assert _sbatch_count(cluster) == 0


def test_a_job_still_loading_is_not_handed_out():
    cluster = FakeCluster()
    cluster.add(ready=False)
    keeper = _keeper(cluster)
    keeper.step()
    assert keeper.status()["state"] == "loading" and keeper.binding() is None
    assert cluster.target() is None                # the local port refuses connections


def test_the_next_job_is_submitted_ahead_of_the_limit_and_only_once():
    cluster = FakeCluster()
    cluster.add(left="2-01:00:00")
    keeper = _keeper(cluster)
    keeper.step()
    assert _sbatch_count(cluster) == 0             # 49 h left, lead is 48 h
    next(iter(cluster.jobs.values()))["left"] = "1-23:00:00"
    keeper.step()
    assert _sbatch_count(cluster) == 1
    keeper.step()
    assert _sbatch_count(cluster) == 1             # the successor is queued
    assert keeper.status()["state"] == "up" and keeper.status()["queued_job_id"]


def test_it_moves_to_the_newer_job_once_that_answers_and_cancels_the_old_one_after_a_drain():
    cluster = FakeCluster()
    old = cluster.add(node="gpu-a", port=31000, left="20:00:00")
    keeper = _keeper(cluster, _settings(drain_s=1800))
    keeper.step()
    queued = next(j for j, d in cluster.jobs.items() if d["state"] == "PD")
    cluster.jobs[queued].update(state="R", node="gpu-b", port=42000, left="3-00:00:00", ready=False)
    keeper.step()
    assert cluster.target() == ("gpu-a", 31000)    # the new one is still loading weights
    cluster.jobs[queued]["ready"] = True
    keeper.step()
    assert cluster.target() == ("gpu-b", 42000)
    assert cluster.cancelled == []                 # calls in flight on the old job may finish
    keeper.clock.now += 1801
    keeper.step()
    assert cluster.cancelled == [old]
    assert keeper.status()["job_id"] == queued


def test_when_slurm_does_not_answer_nothing_is_submitted_or_cancelled():
    cluster = FakeCluster()
    cluster.add(node="gpu-a", port=31000, left="2-12:00:00")
    keeper = _keeper(cluster)
    keeper.step()
    cluster.squeue_ok = False
    keeper.step()
    assert _sbatch_count(cluster) == 0 and cluster.cancelled == []
    assert keeper.binding() is not None            # the job we already serve from still answers
    assert "squeue" in keeper.status()["detail"]


def test_a_job_that_dies_before_it_serves_is_not_resubmitted_in_a_loop():
    cluster = FakeCluster()
    keeper = _keeper(cluster)
    keeper.step()
    first = next(iter(cluster.jobs))
    cluster.logs[first] = "preflight: torch sees 3 CUDA devices (need 4)\nABORT: gpu-bad.local exposes 3 usable CUDA devices, not 4\n"
    cluster.jobs.clear()                           # it started, failed its check and left the queue
    keeper.step()
    assert _sbatch_count(cluster) == 1             # not yet: 10 minutes first
    assert "ended before it served" in keeper.status()["detail"]
    keeper.clock.now += 601
    keeper.step()
    assert _sbatch_count(cluster) == 2
    assert "#SBATCH --exclude=gpu-bad\n" in cluster.submitted[-1]
    second = next(iter(cluster.jobs))
    cluster.jobs.clear()
    cluster.logs[second] = "vllm: CUDA out of memory\n"
    keeper.step()
    keeper.clock.now += 601
    keeper.step()
    assert _sbatch_count(cluster) == 2             # the wait doubles
    keeper.clock.now += 600
    keeper.step()
    assert _sbatch_count(cluster) == 3


def test_a_job_that_serves_clears_the_backoff():
    cluster = FakeCluster()
    keeper = _keeper(cluster)
    keeper.step()
    cluster.jobs.clear()
    keeper.step()
    keeper.clock.now += 601
    keeper.step()                                  # second submit
    jid = next(iter(cluster.jobs))
    cluster.jobs[jid].update(state="R", node="gpu-a", port=31000, left="3-00:00:00", ready=True)
    keeper.step()
    assert keeper.status()["state"] == "up"
    cluster.jobs.clear()                           # preempted
    keeper.step()
    assert _sbatch_count(cluster) == 3             # resubmitted at once: it had served


def test_a_refused_sbatch_is_reported_and_retried_later():
    cluster = FakeCluster()
    cluster.sbatch_error = "sbatch: error: QOSMaxGRESPerUser"
    keeper = _keeper(cluster)
    keeper.step()
    assert "QOSMaxGRESPerUser" in keeper.status()["detail"]
    keeper.step()
    assert _sbatch_count(cluster) == 1


def test_duplicate_queued_jobs_are_cut_back_to_the_oldest():
    cluster = FakeCluster()
    keep = cluster.add(state="PD")
    drop = cluster.add(state="PD")
    keeper = _keeper(cluster)
    keeper.step()
    assert cluster.cancelled == [drop] and keep in cluster.jobs
    assert _sbatch_count(cluster) == 0


def test_a_failed_call_stops_the_handout_until_the_keeper_has_looked_again():
    cluster = FakeCluster()
    cluster.add()
    health = {"ok": True}
    keeper = _keeper(cluster, healthy=lambda: health["ok"])
    keeper.step()
    assert keeper.binding() is not None
    keeper.note_failure("vLLM stopped mid-reply")
    assert keeper.binding() is None and keeper.status()["state"] == "unreachable"
    keeper.step()
    assert keeper.binding() is not None            # it answered the probe: a one-off
    health["ok"] = False
    keeper.step()
    assert keeper.binding() is None


def test_a_login_that_fails_is_retried_with_a_growing_wait():
    attempts: list[float] = []
    clock = types.SimpleNamespace(now=5000.0)

    def factory():
        attempts.append(clock.now)
        raise GatewayError("SSH authentication failed.", stage="ssh_auth")

    keeper = SharedLLM(_settings(), HPCSettings(), factory, clock=lambda: clock.now,
                       probe=lambda *a: (True, ""), log=lambda line: None)
    keeper.step()
    keeper.step()
    assert len(attempts) == 1 and keeper.status()["state"] == "ssh_error"
    clock.now += 121
    keeper.step()
    assert len(attempts) == 2
    clock.now += 121
    keeper.step()
    assert len(attempts) == 2                      # the second wait is 4 minutes


def test_an_identity_needs_a_user_with_an_explicit_key(monkeypatch):
    with pytest.raises(GatewayError) as err:
        shared_llm.resolve_identity(_settings(hpc_user="", key_path="/keys/id"), HPCSettings())
    assert "HPC_USER" in err.value.message
    with pytest.raises(GatewayError):
        shared_llm.resolve_identity(_settings(hpc_user="", key_path="", owner=""), HPCSettings())
    assert shared_llm.resolve_identity(_settings(), HPCSettings()) == ("keeper", "/keys/id")


def test_settings_come_from_the_environment(monkeypatch):
    for k, v in {"AISCIENTIST_SHARED_LLM": "1", "AISCIENTIST_SHARED_LLM_OWNER": "labadmin",
                 "AISCIENTIST_SHARED_LLM_RENEW_LEAD_HOURS": "12",
                 "AISCIENTIST_SHARED_LLM_THINK_KWARGS": '{"thinking": true}',
                 "AISCIENTIST_SHARED_LLM_LOCAL_PORT": "8811"}.items():
        monkeypatch.setenv(k, v)
    hpc = HPCSettings()
    s = SharedLLMSettings.from_env(hpc)
    assert s.enabled and s.owner == "labadmin" and s.renew_lead_s == 12 * 3600
    assert dict(s.think_kwargs) == {"thinking": True}
    assert s.local_port == 8811 and s.image == hpc.vllm_image
    monkeypatch.delenv("AISCIENTIST_SHARED_LLM")
    assert not SharedLLMSettings.from_env(hpc).enabled


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("AISCIENTIST_SHARED_LLM", raising=False)
    monkeypatch.setattr(shared_llm, "_MANAGER", None)
    assert shared_llm.start(HPCSettings()) is None
    assert shared_llm.status() == {"enabled": False, "state": "off"}
    assert shared_llm.endpoint_binding() is None and shared_llm.endpoint_public() is None


# -- which sessions use it ---------------------------------------------------------

fastapi = pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw  # noqa: E402


class _Conn:
    selected_model = "RedHatAI/Qwen3.8-27B-INT4"
    tunnel_port = 37219
    mock = False

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def emit(self, *event, **kw) -> None:
        self.events.append(event)


@pytest.fixture()
def lab(monkeypatch):
    """A session with the shared server up, and every completion recorded."""
    for v in ("AISCIENTIST_LLM_BASE_URL", "AISCIENTIST_LAB_LLM_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    cluster = FakeCluster()
    cluster.add()
    keeper = _keeper(cluster)
    keeper.step()
    monkeypatch.setattr(shared_llm, "_MANAGER", keeper)
    calls: list[dict] = []
    replies: list = []

    def complete_ex(port, model, messages, **kw):
        calls.append(dict(port=port, model=model, **kw))
        reply = replies.pop(0) if replies else "fine"
        if isinstance(reply, Exception):
            raise reply
        return reply, {"completion_tokens": 3}

    monkeypatch.setattr(gw.vllm_client, "complete_ex", complete_ex)
    monkeypatch.setattr(gw, "_record_lab_usage", lambda conn, **f: None)
    return types.SimpleNamespace(keeper=keeper, calls=calls, replies=replies, conn=_Conn())


def test_a_session_on_its_own_gpu_never_uses_the_shared_server(lab):
    """Each session is on one model: the shared server answers only the sessions that chose it as
    their endpoint (tests/test_shared_endpoint.py). Yijun, 2026-10-15: with Qwen, Qwen throughout."""
    llm = gw._lab_llm(lab.conn)
    for role in ("plan", "critic", "writer", "classify"):
        llm.complete_fn([{"role": "user", "content": role}], role=role)
    assert {(c["port"], c["model"]) for c in lab.calls} == {(37219, "RedHatAI/Qwen3.8-27B-INT4")}
    assert lab.keeper.binding() is not None, "although the shared server is up"
    assert gw._llm_provenance_note(llm).startswith("All model calls ran on the cluster's own vLLM")


def test_a_session_release_does_not_cancel_the_shared_job():
    assert gw._classify_session_job(shared_llm.JOB_NAME) is None
