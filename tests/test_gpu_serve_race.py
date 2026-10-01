"""GPU serve-job RACE: with settings.gpu_candidates set, submit all candidates and use whichever is
ALLOCATED FIRST, scancelling the losers. Empty candidates → the classic single-job path (unchanged).

A scripted fake RemoteExecutor drives find_running_job -> sbatch (per candidate) -> squeue poll ->
scancel losers -> read per-job port. No real Slurm/SSH.
"""

from __future__ import annotations

import re

from aiscientist.gateway import gpu
from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.settings import HPCSettings


class FakeExec:
    """``states`` maps a SANITISED partition name (non-alnum -> '_', as in the serve.<part>.sbatch
    path) to the (slurm_state, node) that partition's job reaches on the first poll."""

    def __init__(self, states: dict[str, tuple[str, str]]):
        self.username = "tester"
        self.states = states
        self.calls: list[str] = []
        self.scancelled: list[str] = []
        self._next_id = 1000
        self._jid_part: dict[str, str] = {}

    def exec(self, command, timeout=None):
        self.calls.append(command)
        c = command

        def R(status=0, out="", err=""):
            return ExecResult(command=c, exit_status=status, stdout=out, stderr=err)

        if c.startswith("squeue --me"):
            return R(out="")                       # no reusable running job
        if "cat >" in c and ".sbatch" in c:
            return R()                             # write script -> ok
        if c.startswith("sbatch "):
            m = re.search(r"serve\.([^.]+)\.sbatch", c)
            part = m.group(1) if m else "gpu"
            if self.states.get(part) == ("REJECT", ""):
                return R(status=1, err=f"sbatch: error: invalid account for partition {part}")
            self._next_id += 1
            jid = str(self._next_id)
            self._jid_part[jid] = part
            return R(out=f"Submitted batch job {jid}")
        if c.startswith("squeue -j "):
            jid = c.split()[2]
            state, node = self.states.get(self._jid_part.get(jid, ""), ("PD", ""))
            return R(out=f"{state}|{node}|{'Resources' if state == 'PD' else 'None'}")
        if c.startswith("scancel "):
            self.scancelled.extend(c.split()[1:])
            return R()
        if c.startswith("cat ") and ".port" in c:
            return R(out="34567")
        return R()


def _emits():
    log: list[tuple[str, str, str]] = []
    return log, (lambda level, stage, msg: log.append((level, stage, msg)))


def _settings(candidates: str) -> HPCSettings:
    return HPCSettings(gpu_candidates=candidates)


def test_parse_candidates_default_and_list():
    s = HPCSettings()
    one = gpu.parse_candidates(s)
    assert len(one) == 1 and one[0].partition == s.partition and one[0].gres == s.gres
    many = gpu.parse_candidates(_settings(
        "free-gpu32,gpu:RTX6000:1,ruic20_lab;gpu,gpu:A100:1,ruic20_lab_gpu"))
    assert [c.partition for c in many] == ["free-gpu32", "gpu"]
    assert [c.gres for c in many] == ["gpu:RTX6000:1", "gpu:A100:1"]
    assert [c.account for c in many] == ["ruic20_lab", "ruic20_lab_gpu"]


def test_race_first_allocated_wins_and_cancels_losers():
    # RTX6000 (free-gpu32) is RUNNING first; the A100 (gpu) is still queued -> RTX wins, A100 cancelled.
    hpc = FakeExec({"free_gpu32": ("R", "hpc3-gpu-m54-02"), "gpu": ("PD", "")})
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(
        hpc, _settings("free-gpu32,gpu:RTX6000:1,ruic20_lab;gpu,gpu:A100:1,ruic20_lab_gpu"),
        emit, wait_seconds=5)
    assert alloc.node == "hpc3-gpu-m54-02" and alloc.job_id == "1001" and alloc.port == 34567
    assert hpc.scancelled == ["1002"]                        # the slower A100 candidate was cancelled
    assert any("Racing 2 GPU candidates" in m for _, _, m in log)
    assert any("Winner: free-gpu32/gpu:RTX6000:1" in m for _, _, m in log)


def test_race_other_candidate_can_win():
    # Now the A100 wins; the RTX6000 candidate is cancelled.
    hpc = FakeExec({"free_gpu32": ("PD", ""), "gpu": ("R", "hpc3-gpu-l54-03")})
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(
        hpc, _settings("free-gpu32,gpu:RTX6000:1,ruic20_lab;gpu,gpu:A100:1,ruic20_lab_gpu"),
        emit, wait_seconds=5)
    assert alloc.node == "hpc3-gpu-l54-03" and alloc.job_id == "1002"
    assert hpc.scancelled == ["1001"]


def test_rejected_candidate_is_skipped_race_continues():
    # free-gpu32 submission is rejected (no access); the A100 still wins the race.
    hpc = FakeExec({"free_gpu32": ("REJECT", ""), "gpu": ("R", "hpc3-gpu-l54-03")})
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(
        hpc, _settings("free-gpu32,gpu:RTX6000:1,ruic20_lab;gpu,gpu:A100:1,ruic20_lab_gpu"),
        emit, wait_seconds=5)
    assert alloc.job_id == "1001" and alloc.node == "hpc3-gpu-l54-03"   # only the A100 got an id
    assert hpc.scancelled == []                                          # nothing to cancel
    assert any("was rejected" in m for _, _, m in log)


def test_single_candidate_path_unchanged():
    # Empty gpu_candidates -> one job, classic behaviour: no "Racing" line, no scancel.
    hpc = FakeExec({"gpu": ("R", "hpc3-gpu-l54-03")})
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(hpc, HPCSettings(), emit, wait_seconds=5)
    assert alloc.node == "hpc3-gpu-l54-03" and alloc.job_id == "1001"
    assert hpc.scancelled == []
    assert not any("Racing" in m for _, _, m in log)
    assert any("Submitting Slurm GPU job" in m for _, _, m in log)


RACE = "free-gpu32,gpu:RTX6000:1,ruic20_lab;gpu,gpu:A100:1,ruic20_lab_gpu"


class StagedExec(FakeExec):
    """Per-partition state SEQUENCES, advanced one step per squeue poll of that partition's job."""

    def __init__(self, seqs: dict[str, list[tuple[str, str]]]):
        super().__init__({})
        self.seqs = {k: list(v) for k, v in seqs.items()}

    def exec(self, command, timeout=None):
        if command.startswith("squeue -j "):
            part = self._jid_part.get(command.split()[2], "")
            seq = self.seqs.get(part) or [("PD", "")]
            self.states[part] = seq.pop(0) if len(seq) > 1 else seq[0]
        return super().exec(command, timeout)


def _no_sleep(monkeypatch, step=1.0):
    clock = {"t": 0.0}
    monkeypatch.setattr(gpu.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(gpu.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    return clock


def test_both_running_same_poll_first_listed_wins():
    hpc = FakeExec({"free_gpu32": ("R", "hpc3-gpu-m54-02"), "gpu": ("R", "hpc3-gpu-l54-03")})
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(hpc, _settings(RACE), emit, wait_seconds=5)
    assert alloc.node == "hpc3-gpu-m54-02" and hpc.scancelled == ["1002"]


def test_prefer_window_holds_a100_until_rtx_starts(monkeypatch):
    # A100 is up at once; the preferred free RTX6000 starts on the 3rd poll, inside the window -> RTX wins.
    _no_sleep(monkeypatch)
    hpc = StagedExec({"free_gpu32": [("PD", ""), ("PD", ""), ("R", "hpc3-gpu-m54-02")],
                      "gpu": [("R", "hpc3-gpu-l54-03")]})
    s = _settings(RACE)
    s.gpu_prefer_seconds = 60
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(hpc, s, emit, wait_seconds=300)
    assert alloc.node == "hpc3-gpu-m54-02" and alloc.job_id == "1001"
    assert hpc.scancelled == ["1002"]
    assert any("holding up to 60s" in m for _, _, m in log)


def test_prefer_window_expires_then_a100_wins(monkeypatch):
    _no_sleep(monkeypatch)
    hpc = StagedExec({"free_gpu32": [("PD", "")], "gpu": [("R", "hpc3-gpu-l54-03")]})
    s = _settings(RACE)
    s.gpu_prefer_seconds = 30
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(hpc, s, emit, wait_seconds=300)
    assert alloc.node == "hpc3-gpu-l54-03" and hpc.scancelled == ["1001"]


def test_prefer_seconds_env(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_GPU_PREFER_SECONDS", "120")
    assert HPCSettings.from_env().gpu_prefer_seconds == 120


class RunningJobExec(FakeExec):
    """The user already has a RUNNING serve job carrying ``comment``."""

    def __init__(self, comment: str):
        super().__init__({"gpu": ("R", "hpc3-gpu-l54-04")})
        self.comment = comment

    def exec(self, command, timeout=None):
        if command.startswith("squeue --me") and not self.scancelled:
            self.calls.append(command)
            return ExecResult(command=command, exit_status=0,
                              stdout=f"555|tester|R|hpc3-gpu-l54-03|gpu:A100:1|0:42|{self.comment}", stderr="")
        return super().exec(command, timeout)


def test_running_job_for_same_model_is_reused():
    s = HPCSettings()
    hpc = RunningJobExec(f"model={s.vllm_model}")
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(hpc, s, emit, wait_seconds=5)
    assert alloc.job_id == "555" and alloc.reused and hpc.scancelled == []


def test_running_job_for_other_or_untagged_model_is_replaced():
    for comment in ("model=QuantTrio/Qwen3.6-35B-A3B-AWQ", "(null)"):
        s = HPCSettings(vllm_model="RedHatAI/Qwen3.8-27B-INT4")
        hpc = RunningJobExec(comment)
        log, emit = _emits()
        alloc = gpu.ensure_serve_job(hpc, s, emit, wait_seconds=5)
        assert hpc.scancelled[0] == "555" and alloc.job_id == "1001" and not alloc.reused
        assert any("started before in-place model switching" in m for _, _, m in log)


def test_serve_script_carries_model_tag():
    s = HPCSettings(vllm_model="RedHatAI/Qwen3.8-27B-INT4")
    assert "#SBATCH --comment=model=RedHatAI/Qwen3.8-27B-INT4\n" in gpu._serve_script(s, "tester")


# --- in-place model switch: keep the card when it fits ------------------------------------------

class SupervisorJobExec(RunningJobExec):
    """A running serve job started by the SUPERVISOR script (it has a spec file), on ``card``."""

    def __init__(self, comment: str, card: str = "NVIDIA RTX PRO 6000 Blackwell Server Edition"):
        super().__init__(comment)
        self.card = card
        self.spec_writes: list[str] = []

    def exec(self, command, timeout=None):
        if "[ -f \"$f\" ]" in command and ".spec" in command:
            self.calls.append(command)
            self.spec_writes.append(command)
            return ExecResult(command=command, exit_status=0, stdout="SWAPPED", stderr="")
        if command.startswith("srun --jobid=") and "nvidia-smi" in command:
            self.calls.append(command)
            return ExecResult(command=command, exit_status=0, stdout=f"3, 900, 97887, {self.card}", stderr="")
        return super().exec(command, timeout)


def test_switch_keeps_the_card_when_the_job_can_swap_in_place():
    s = HPCSettings(vllm_model="QuantTrio/Qwen3.6-35B-A3B-AWQ", vllm_quantization="awq_marlin")
    hpc = SupervisorJobExec("model=RedHatAI/Qwen3.8-27B-INT4")
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(hpc, s, emit, wait_seconds=5)
    assert alloc.job_id == "555" and alloc.swapped and hpc.scancelled == []
    assert alloc.model_tag == "model=QuantTrio/Qwen3.6-35B-A3B-AWQ"
    assert not any(c.startswith("sbatch") for c in hpc.calls)            # no new job, no queue
    w = hpc.spec_writes[0]
    assert "vllm.555.spec" in w and "mv" in w and "scontrol update JobId=555 Comment=model=QuantTrio/" in w
    assert any("switching it to QuantTrio/Qwen3.6-35B-A3B-AWQ in place" in m for _, _, m in log)


def test_switch_releases_the_card_when_the_model_does_not_allow_it():
    s = HPCSettings(vllm_model="org/big-model", vllm_gpu_cards="A100")
    hpc = SupervisorJobExec("model=RedHatAI/Qwen3.8-27B-INT4")      # sits on an RTX PRO 6000
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(hpc, s, emit, wait_seconds=5)
    assert hpc.scancelled[0] == "555" and alloc.job_id != "555" and hpc.spec_writes == []
    assert any("is not a card org/big-model is configured for" in m for _, _, m in log)


def test_switch_keeps_the_card_when_it_is_an_allowed_type():
    s = HPCSettings(vllm_model="org/big-model", vllm_gpu_cards="RTX6000, A100")
    hpc = SupervisorJobExec("model=RedHatAI/Qwen3.8-27B-INT4")
    log, emit = _emits()
    alloc = gpu.ensure_serve_job(hpc, s, emit, wait_seconds=5)
    assert alloc.swapped and alloc.job_id == "555" and hpc.scancelled == []


def test_gres_type_maps_nvidia_names_to_slurm_types():
    assert gpu.gres_type("NVIDIA RTX PRO 6000 Blackwell Server Edition") == "RTX6000"
    assert gpu.gres_type("NVIDIA A100 80GB PCIe") == "A100"
    assert gpu.gres_type("NVIDIA L40S") == "L40S"


def test_serve_script_is_a_supervisor_that_watches_its_spec():
    script = gpu._serve_script(HPCSettings(), "tester")
    assert 'SPEC="$HOME/.bioagent/vllm.${SLURM_JOB_ID}.spec"' in script
    assert "printf '%s\\n%s\\n' 'model=RedHatAI/Qwen3.8-27B-INT4' '" in script
    assert "setsid bash -c" in script and 'kill -TERM -- "-$_srv"' in script
    # vLLM dying on its own still ends the job (unchanged failure semantics); only a spec change restarts
    assert 'if [ "$_switch" = 0 ]; then wait "$_srv"; exit $?; fi' in script
    assert "vllm serve RedHatAI/Qwen3.8-27B-INT4" in gpu.serve_body_from_script(script)
