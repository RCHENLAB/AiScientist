"""Pinned MCP placement reaches real Slurm/container construction, not model overrides."""
from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor
from aiscientist.gateway.executor import ExecResult
from aiscientist.agents.skills_registry import POLICY


class Remote:
    def __init__(self):
        self.commands = []

    def exec(self, command, **kwargs):
        self.commands.append(command)
        return ExecResult(command, 0, "", "")


def capture(monkeypatch):
    from aiscientist.gateway import slurm_sandbox
    scripts = []
    remote = Remote()
    executor = SlurmCodeExecutor(
        remote=remote, container_image="/images/analysis.sif", scratch_dir="/scratch",
        work_dir="/work", artifacts_dir="/artifacts", partition="standard", account="cpu-lab",
        gpu_partition="gpu", gpu_account="gpu-lab", gpu_gres="gpu:A100:1")
    monkeypatch.setattr(slurm_sandbox, "run_batch_job", lambda remote, spec, **kw: scripts.append(spec.script))
    monkeypatch.setattr(executor, "_artifact_snapshot", lambda: {})
    monkeypatch.setattr(executor, "_written_since", lambda _: [])
    monkeypatch.setattr(executor, "_sync_artifacts_back", lambda **kw: None)
    monkeypatch.setattr(executor, "_collect", lambda *a, **kw: {"status": "ok"})
    return executor, scripts


def test_gpu_placement_and_readonly_references(monkeypatch):
    executor, scripts = capture(monkeypatch)
    result = executor.run_in_image("python -m gpu_worker", "", workdir="env/scgpt", gpu=True,
                                   extra_ro_binds=("/refs/scgpt",))
    assert result["status"] == "ok"
    script = scripts[0]
    assert "--partition=gpu" in script and "--account=gpu-lab" in script
    assert "--gres=gpu:A100:1" in script and "--nv" in script
    assert "-B /refs/scgpt:/refs/scgpt:ro" in script


def test_cpu_placement_does_not_request_gpu(monkeypatch):
    executor, scripts = capture(monkeypatch)
    executor.run_in_image("python -m cpu_worker", "", workdir="env/cpu", gpu=False)
    assert "--partition=standard" in scripts[0] and "--account=cpu-lab" in scripts[0]
    assert "--gres" not in scripts[0] and "--nv" not in scripts[0]


def test_gpu_registry_cannot_bypass_skill_policy(monkeypatch):
    executor, scripts = capture(monkeypatch)
    token = POLICY.set({"gpu": False, "secret_values": {}, "network": "none"})
    try:
        result = executor.run_in_image("python -m gpu_worker", "", workdir="env/scgpt", gpu=True)
    finally:
        POLICY.reset(token)
    assert result["status"] == "error" and "does not declare GPU" in result["error"]
    assert not scripts


def test_reference_bind_delimiters_are_rejected_before_staging(monkeypatch):
    executor, scripts = capture(monkeypatch)
    result = executor.run_in_image("true", "", workdir="env/x", extra_ro_binds=("/refs:/secrets",))
    assert result["status"] == "error" and not executor.remote.commands and not scripts
