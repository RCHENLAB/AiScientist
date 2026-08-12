"""GPU serve-job RACE: with settings.gpu_candidates set, submit all candidates and use whichever is
ALLOCATED FIRST, scancelling the losers. Empty candidates → the classic single-job path (unchanged).

A scripted fake RemoteExecutor drives find_running_job -> sbatch (per candidate) -> squeue poll ->
scancel losers -> read per-job port. No real Slurm/SSH.
"""

from __future__ import annotations

import re

from bioagent.gateway import gpu
from bioagent.gateway.executor import ExecResult
from bioagent.gateway.settings import HPCSettings


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
