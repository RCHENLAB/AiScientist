from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, replace
from typing import Callable

from .errors import CommandFailure, GatewayError
from .executor import RemoteExecutor
from .settings import HPCSettings

EmitFn = Callable[[str, str, str], None]

JOB_NAME = "bioagent-vllm"

# The serve job picks a FREE port on its compute node (HPC3 nodes are shared, so a
# fixed port would collide with another user's serve) and records it PER JOB ID, on the
# user's own shared $HOME. Per-job (not a single fixed file) so two concurrent serve jobs
# — e.g. the RACE below, or two sessions — never clobber each other's port. The gateway
# reads back vllm.<jobid>.port to know where to tunnel. The stable local end is
# AISCIENTIST_LOCAL_TUNNEL_PORT (0 = ephemeral per session).
PORT_FILE = "$HOME/.bioagent/vllm.port"                       # legacy fixed path (pre-per-job jobs)
# Inside the sbatch script the job writes its own port here ($SLURM_JOB_ID is in the job env,
# and survives the `sg <group>` sub-shell). The gateway reads the SAME path by the sbatch job id.
PORT_FILE_JOB = '"$HOME/.bioagent/vllm.${SLURM_JOB_ID}.port"'
# What the serve job should be running: line 1 is the model tag, line 2 the base64 serve body. The
# job writes it at start and watches it; the gateway rewrites it to switch the model IN PLACE, so
# the Slurm allocation — the card — is kept instead of being released and re-queued.
SPEC_FILE = "$HOME/.bioagent/vllm.{job_id}.spec"


def job_name(username: str) -> str:
    """Per-user job name so we can only ever match the current user's own job."""
    safe = "".join(c for c in (username or "user") if c.isalnum() or c in "-_")
    return f"{JOB_NAME}-{safe}"


@dataclass
class GPUAllocation:
    job_id: str
    node: str
    port: int
    owner: str = ""
    reused: bool = False
    # The job's Slurm comment, "model=<repo id>" for jobs submitted since the tag was added; "" for
    # older jobs. ensure_serve_job refuses to reuse a job whose tag is not the configured model.
    model_tag: str = ""
    # True when ensure_serve_job re-pointed this (already running) job at a different model rather
    # than submitting a new one: same node, same port, new weights loading.
    swapped: bool = False


@dataclass
class GPUHealth:
    healthy: bool
    util_percent: int
    mem_used_mb: int
    mem_total_mb: int
    name: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "util_percent": self.util_percent,
            "mem_used_mb": self.mem_used_mb,
            "mem_total_mb": self.mem_total_mb,
            "name": self.name,
            "reason": self.reason,
        }


def _vllm_serve_body(settings: HPCSettings) -> str:
    """Run vLLM inside the Singularity container, serving the OpenAI-compatible /v1
    API on the dynamic port. The model is loaded at launch (no pull). HF cache lives
    on shared DFS (bind-mounted, offline) so it never touches the home quota."""
    hf = settings.hf_home
    flags = [
        "--host 0.0.0.0",
        f'--port "$(cat {PORT_FILE_JOB})"',
        f"--served-model-name {settings.vllm_model}",
        f"--max-model-len {settings.vllm_max_model_len}",
        f"--gpu-memory-utilization {settings.vllm_gpu_mem_util}",
        "--enable-auto-tool-choice",
        f"--tool-call-parser {settings.vllm_tool_parser}",
    ]
    if settings.vllm_quantization:
        flags.append(f"--quantization {settings.vllm_quantization}")
    if settings.vllm_reasoning_parser:
        flags.append(f"--reasoning-parser {settings.vllm_reasoning_parser}")
    if settings.vllm_extra_args:
        flags.append(settings.vllm_extra_args)
    serve = f"vllm serve {settings.vllm_model} " + " ".join(flags)
    return (
        f'export HF_HOME="{hf}"\n'
        # The weights are pre-staged, so force offline to avoid any HF-hub network dependency
        # mid-serve (HPC3 compute nodes DO have egress — verified 2026-07-08 — but we don't rely on it).
        "export HF_HUB_OFFLINE=1\n"
        "source /etc/profile.d/lmod.sh 2>/dev/null || true\n"
        # RCIC HPC3 provides Singularity via a module (NOT apptainer).
        f"module load {settings.container_module} 2>/dev/null || true\n"
        'echo "AiScientist vLLM serve starting on $(hostname) at $(date)"\n'
        f"{settings.container_bin} exec --nv -B {hf}:{hf} --env HF_HOME={hf} --env HF_HUB_OFFLINE=1 "
        f"{settings.vllm_image} {serve}\n"
    )


def _runner(settings: HPCSettings) -> str:
    """How the decoded serve body is run: under the lab group when the image/weights live on the
    DFS lab storage (``newgrp ruic20_hpc``), else plain bash."""
    group = settings.data_group()
    return f"sg {group} -c 'bash -s'" if group else "bash -s"


def _encoded_body(settings: HPCSettings) -> str:
    return base64.b64encode(_vllm_serve_body(settings).encode("utf-8")).decode("ascii")


def _serve_script(settings: HPCSettings, username: str) -> str:
    """The serve job. It does not run vLLM directly: it runs a small SUPERVISOR that serves whatever
    the job's spec file names, and swaps to a new spec when the gateway rewrites it. The allocation
    therefore outlives any one model: switching models keeps the card (no queue, no chance of the
    free RTX6000 going to someone else in between) and only costs the model load.

    Failure semantics are unchanged: if vLLM exits on its own (crash, OOM, bad weights), the job
    exits with vLLM's status, exactly as when vLLM was the job's only process. Only a spec change
    restarts it. The old server is stopped as a whole session (``setsid`` + kill the session and its
    process group), and the port is waited free before the new one binds it, so the gateway's
    existing tunnel keeps working."""
    account = f"#SBATCH --account={settings.account}\n" if settings.account else ""
    exclude = f"#SBATCH --exclude={settings.exclude}\n" if settings.exclude else ""
    constraint = f"#SBATCH --constraint={settings.constraint}\n" if settings.constraint else ""
    tag = _model_tag(settings)
    supervisor = (
        'SPEC="$HOME/.bioagent/vllm.${SLURM_JOB_ID}.spec"\n'
        f"printf '%s\\n%s\\n' '{tag}' '{_encoded_body(settings)}' > \"$SPEC\"\n"
        "set +e\n"
        "while true; do\n"
        '  _sum=$(cksum < "$SPEC")\n'
        '  echo "AiScientist: serving $(sed -n 1p "$SPEC" | cut -d= -f2-) ($(date))"\n'
        f"  setsid bash -c \"printf %s '$(sed -n 2p \"$SPEC\")' | base64 --decode | {_runner(settings)}\" &\n"
        "  _srv=$!\n"
        "  _switch=0\n"
        '  while kill -0 "$_srv" 2>/dev/null; do\n'
        "    sleep 3\n"
        '    if [ "$(cksum < "$SPEC" 2>/dev/null)" != "$_sum" ]; then _switch=1; break; fi\n'
        "  done\n"
        '  if [ "$_switch" = 0 ]; then wait "$_srv"; exit $?; fi\n'
        '  echo "AiScientist: model switch requested; stopping the current server ($(date))"\n'
        '  kill -TERM -- "-$_srv" 2>/dev/null; pkill -TERM -s "$_srv" 2>/dev/null\n'
        '  for _i in $(seq 1 45); do pgrep -s "$_srv" >/dev/null 2>&1 || break; sleep 2; done\n'
        '  pkill -KILL -s "$_srv" 2>/dev/null; kill -KILL -- "-$_srv" 2>/dev/null\n'
        '  wait "$_srv" 2>/dev/null\n'
        '  for _i in $(seq 1 30); do (exec 3<>/dev/tcp/127.0.0.1/$AISCIENTIST_PORT) 2>/dev/null || break; sleep 2; done\n'
        "done\n"
    )
    return (
        "#!/bin/bash\n"
        f"#SBATCH --job-name={job_name(username)}\n"
        f"#SBATCH --partition={settings.partition}\n"
        f"{account}"
        f"#SBATCH --gres={settings.gres}\n"
        f"{exclude}"
        f"{constraint}"
        f"#SBATCH --cpus-per-task={settings.cpus}\n"
        f"#SBATCH --mem={settings.mem_gb}G\n"
        f"#SBATCH --time={settings.time_limit}\n"
        # Which model this job serves, so a later connect can tell a stale job (started before the
        # model was switched) from a reusable one without asking the server. See find_running_job.
        # An in-place switch updates it (`scontrol update ... Comment=`, allowed for your own job).
        f"#SBATCH --comment={tag}\n"
        # NOTE: Slurm does NOT expand $HOME inside #SBATCH directives, so the log
        # path must be relative to the submit dir (the gateway submits from $HOME).
        "#SBATCH --output=bioagent-vllm-%j.log\n\n"
        "set -uo pipefail\n"
        # Pick a free TCP port on THIS compute node (pure bash /dev/tcp probe — no
        # python/ss dependency) and record it for the gateway to tunnel to. A
        # connect that fails => nothing is listening => treat the port as free.
        'mkdir -p "$HOME/.bioagent"\n'
        'AISCIENTIST_PORT=""\n'
        'for _try in {1..50}; do\n'
        '  _cand=$(( (RANDOM % 20000) + 30000 ))\n'
        '  if ! (exec 3<>/dev/tcp/127.0.0.1/$_cand) 2>/dev/null; then AISCIENTIST_PORT=$_cand; break; fi\n'
        'done\n'
        f'[ -n "$AISCIENTIST_PORT" ] || AISCIENTIST_PORT={settings.serve_port}\n'
        f'echo "$AISCIENTIST_PORT" > {PORT_FILE_JOB}\n'
        'echo "AiScientist LLM server will bind 0.0.0.0:$AISCIENTIST_PORT on $(hostname)"\n'
        f"{supervisor}"
    )


def serve_body_from_script(script: str) -> str:
    """The vLLM serve body a serve script starts with (decoded from its initial spec line)."""
    m = re.search(r"printf '%s\\n%s\\n' '[^']*' '([A-Za-z0-9+/=]+)' > \"\$SPEC\"", script)
    return base64.b64decode(m.group(1)).decode("utf-8") if m else ""


# nvidia-smi names -> the Slurm gres TYPE vocabulary AISCIENTIST_GPU_CANDIDATES already uses, so a
# model's allowed cards and the race's candidates speak the same words.
_CARD_ALIASES = (("RTXPRO6000", "RTX6000"), ("H200", "H200"), ("H100", "H100"), ("A100", "A100"),
                 ("L40S", "L40S"), ("A6000", "A6000"), ("A30", "A30"), ("V100", "V100"))


def gres_type(gpu_name: str) -> str:
    """``NVIDIA RTX PRO 6000 Blackwell Server Edition`` -> ``RTX6000``; ``NVIDIA A100 80GB PCIe``
    -> ``A100``. Unknown names come back normalised (upper case, alphanumerics only)."""
    n = re.sub(r"[^A-Z0-9]", "", (gpu_name or "").upper())
    for key, t in _CARD_ALIASES:
        if key in n:
            return t
    return n


def allowed_cards(settings: HPCSettings) -> list[str]:
    return [c.strip().upper() for c in (getattr(settings, "vllm_gpu_cards", "") or "").split(",") if c.strip()]


def card_fits(executor: RemoteExecutor, settings: HPCSettings, alloc: GPUAllocation) -> tuple[bool, str]:
    """Can the model in ``settings`` run on the card ``alloc`` already holds? No card list on the
    model = any card the race could have given it, so yes without asking the node. Otherwise the
    node is asked (nvidia-smi) and a card we cannot read counts as "does not fit" — releasing a
    card by mistake costs a queue wait; loading weights onto the wrong one costs a failed session."""
    allowed = allowed_cards(settings)
    if not allowed:
        return True, "any card"
    health = check_health(executor, settings, alloc)
    if not health.healthy:
        return False, "the card could not be read"
    return gres_type(health.name) in allowed, health.name


def swap_in_place(executor: RemoteExecutor, settings: HPCSettings, alloc: GPUAllocation) -> bool:
    """Point a running serve job at the model in ``settings`` without giving up its allocation.
    Rewrites the job's spec file (atomically: tmp + mv) and its Slurm comment. False when the job
    predates the supervisor (no spec file) — only a new job can serve a different model then."""
    tag = _model_tag(settings)
    spec = SPEC_FILE.format(job_id=alloc.job_id)
    res = executor.exec(
        f'f="{spec}"; [ -f "$f" ] || {{ echo NO_SUPERVISOR; exit 0; }}; '
        f"printf '%s\\n%s\\n' '{tag}' '{_encoded_body(settings)}' > \"$f.tmp\" && mv \"$f.tmp\" \"$f\" "
        f"&& echo SWAPPED; scontrol update JobId={alloc.job_id} Comment={tag} >/dev/null 2>&1 || true"
    )
    return "SWAPPED" in (getattr(res, "out", "") or "")


def read_serve_port(
    executor: RemoteExecutor,
    settings: HPCSettings,
    *,
    job_id: str | None = None,
    retries: int = 10,
    delay: float = 1.5,
) -> int:
    """Read the dynamic port the serve job bound. Prefers the PER-JOB file
    (``vllm.<job_id>.port``) so racing / concurrent jobs never read each other's port; falls back to
    the legacy fixed ``vllm.port`` (a job started before per-job files), then ``settings.serve_port``.
    Polls briefly because the file appears a moment after the job goes RUNNING."""
    paths = ([f"$HOME/.bioagent/vllm.{job_id}.port"] if job_id else []) + [PORT_FILE]
    for attempt in range(retries):
        for path in paths:
            result = executor.exec(f"cat {path} 2>/dev/null")
            text = result.out.strip()
            if text.isdigit():
                return int(text)
        if attempt < retries - 1:
            time.sleep(delay)
    return settings.serve_port


def _model_tag(settings: HPCSettings) -> str:
    return "model=" + settings.serving_model()


def find_running_job(executor: RemoteExecutor, settings: HPCSettings) -> GPUAllocation | None:
    """Return *the current user's own* running serve job to reuse, if any.

    Strictly scoped with ``squeue --me`` and the per-user job name, so it can
    never see, reuse, or touch another lab member's jobs. Reconnecting therefore
    reuses your own GPU server instead of starting a second one. Read-only: whether the job is
    still the right one to reuse (``model_tag``) is decided by :func:`ensure_serve_job`.
    """
    result = executor.exec(
        f"squeue --me --name={job_name(executor.username)} --states=R --noheader "
        "--format='%i|%u|%t|%N|%b|%M|%k'"
    )
    line = result.out.splitlines()[0] if result.out else ""
    if not line:
        return None
    parts = line.split("|")
    if len(parts) < 4 or not parts[3].strip():
        return None
    return GPUAllocation(
        job_id=parts[0].strip(),
        node=parts[3].strip(),
        # Reused job already wrote its port; read it quickly by job id (short fallback for a
        # pre-dynamic-port legacy job).
        port=read_serve_port(executor, settings, job_id=parts[0].strip(), retries=3, delay=1.0),
        owner=parts[1].strip() if len(parts) > 1 else executor.username,
        reused=True,
        model_tag=parts[6].strip() if len(parts) > 6 else "",
    )


@dataclass
class GpuCandidate:
    """One GPU option to submit for the serve job: a partition + gres (+ optional charge account)."""
    partition: str
    gres: str
    account: str | None = None

    def label(self) -> str:
        return f"{self.partition}/{self.gres}"


def parse_candidates(settings: HPCSettings) -> list[GpuCandidate]:
    """The GPU options to try, from ``settings.gpu_candidates`` (';'-separated ``partition,gres[,account]``).
    Empty → a single candidate from ``partition``/``gres``/``account`` (the classic single-job behaviour,
    unchanged). A per-candidate empty field falls back to the corresponding top-level setting."""
    raw = (settings.gpu_candidates or "").strip()
    if not raw:
        return [GpuCandidate(settings.partition, settings.gres, settings.account)]
    out: list[GpuCandidate] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(",")]
        partition = parts[0] if parts and parts[0] else settings.partition
        gres = parts[1] if len(parts) > 1 and parts[1] else settings.gres
        account = parts[2] if len(parts) > 2 and parts[2] else settings.account
        out.append(GpuCandidate(partition, gres, account))
    return out or [GpuCandidate(settings.partition, settings.gres, settings.account)]


def _submit_candidate(
    executor: RemoteExecutor, settings: HPCSettings, cand: GpuCandidate, emit: EmitFn, *, hard_fail: bool
) -> str | None:
    """Write + ``sbatch`` ONE candidate serve job (its own partition/gres/account, its own script file
    so racing candidates don't overwrite each other). Returns the Slurm job id, or ``None`` when Slurm
    rejects it (bad partition/account for this user) — in a race a rejected candidate is skipped, not
    fatal. With ``hard_fail`` (the single-candidate path) a rejection raises, preserving old behaviour."""
    cand_settings = replace(settings, partition=cand.partition, gres=cand.gres, account=cand.account)
    home = settings.serve_home
    safe = "".join(c if c.isalnum() else "_" for c in cand.partition) or "gpu"
    script_path = f"{home}/serve.{safe}.sbatch"
    write = executor.exec(
        f"mkdir -p {home} && cat > {script_path} <<'AISCIENTIST_EOF'\n"
        f"{_serve_script(cand_settings, executor.username)}"
        "AISCIENTIST_EOF"
    )
    if not write.ok:
        raise GatewayError(
            "Failed to write the GPU serve job script on the cluster.",
            stage="gpu_alloc",
            detail=CommandFailure("write serve.sbatch", write.exit_status, write.stdout, write.stderr),
        )
    submit = executor.exec(f"sbatch {script_path}")
    if not submit.ok or "Submitted batch job" not in submit.stdout:
        if hard_fail:
            raise GatewayError(
                "Failed to submit the GPU serve job to Slurm.",
                stage="gpu_alloc",
                detail=CommandFailure(
                    command="sbatch serve.sbatch",
                    exit_status=submit.exit_status,
                    stdout=submit.stdout,
                    stderr=submit.stderr,
                    hint="Verify your Slurm account, partition, and GPU gres are valid for your allocation.",
                ),
            )
        emit("warning", "gpu_alloc",
             f"GPU candidate {cand.label()} was rejected ({(submit.stderr or submit.stdout).strip()[:160]}); "
             "skipping it and racing the rest.")
        return None
    return submit.stdout.strip().split()[-1]


def ensure_serve_job(
    executor: RemoteExecutor,
    settings: HPCSettings,
    emit: EmitFn,
    *,
    wait_seconds: int = 300,
) -> GPUAllocation:
    """Ensure a GPU job is serving vLLM; submit + wait if needed.

    This is what guarantees "a GPU was actually allocated" on every connect. With
    ``settings.gpu_candidates`` set, submits every candidate at once and uses whichever is ALLOCATED
    FIRST (ties and, with ``gpu_prefer_seconds``, a grace window go to the earlier-listed candidate),
    scancelling the losers — so a session grabs the earliest-free card instead of queueing on
    one scarce type. Empty candidates → the classic single-job path (unchanged)."""
    existing = find_running_job(executor, settings)
    if existing and existing.model_tag != _model_tag(settings):
        # The running job serves another model. Keep its card when the new model fits that card and
        # the job can switch in place; release it only when it cannot (the card is not one the model
        # allows, or the job predates in-place switching). Reusing it unchanged would fail the
        # connect at ensure_model.
        want = settings.serving_model()
        fits, card = card_fits(executor, settings, existing)
        if fits and swap_in_place(executor, settings, existing):
            existing.model_tag = _model_tag(settings)
            existing.swapped = True
            emit("info", "gpu_alloc",
                 f"Keeping your GPU job {existing.job_id} on {existing.node} ({card}) and switching it to "
                 f"{want} in place — no new queue wait; the new model loads in a few minutes.")
            return existing
        executor.exec(f"scancel {existing.job_id}")
        why = (f"{card} is not a card {want} is configured for" if not fits
               else "that job was started before in-place model switching existed")
        emit("info", "gpu_alloc",
             f"Released your GPU job {existing.job_id} ({why}); starting a fresh one for {want}.")
        existing = None
    if existing:
        emit(
            "success",
            "gpu_alloc",
            f"Reusing your own running Qwen3 GPU job {existing.job_id} on node {existing.node} — "
            "no new GPU allocation for this session.",
        )
        return existing

    candidates = parse_candidates(settings)
    if len(candidates) == 1:
        emit("step", "gpu_alloc",
             f"Submitting Slurm GPU job (partition={candidates[0].partition}, gres={candidates[0].gres}) ...")
    else:
        emit("step", "gpu_alloc",
             f"Racing {len(candidates)} GPU candidates — first to start wins: "
             f"{', '.join(c.label() for c in candidates)} ...")

    # Submit every candidate; map job_id -> candidate. A rejected candidate is skipped (unless it is
    # the only one, which hard-fails like before). If none submit, fail.
    jobs: dict[str, GpuCandidate] = {}
    for cand in candidates:
        jid = _submit_candidate(executor, settings, cand, emit, hard_fail=(len(candidates) == 1))
        if jid:
            jobs[jid] = cand
            emit("info", "gpu_alloc",
                 f"Submitted GPU job {jid} ({cand.label()}); waiting for it to start (GPU queue) ...")
    if not jobs:
        raise GatewayError(
            "No GPU candidate could be submitted — every partition/account was rejected.",
            stage="gpu_alloc",
        )

    # Race: poll all jobs. List order is preference order: among jobs RUNNING in the same poll the
    # highest-priority one wins, and with ``gpu_prefer_seconds`` > 0 a lower-priority job that is
    # already running is held until every higher-priority job still queued has waited that long.
    # Losers are scancelled.
    rank = {jid: i for i, jid in enumerate(jobs)}
    started = time.monotonic()
    prefer = max(0, int(getattr(settings, "gpu_prefer_seconds", 0) or 0))
    deadline = started + wait_seconds
    last_pending_emit = 0.0
    held_note: set[str] = set()
    while time.monotonic() < deadline:
        running: list[tuple[str, str]] = []
        for jid, cand in list(jobs.items()):
            status = executor.exec(f"squeue -j {jid} --noheader --format='%t|%N|%r'")
            row = status.out.splitlines()[0] if status.out else ""
            if not row:
                continue
            fields = row.split("|")
            state = fields[0].strip()
            node = fields[1].strip() if len(fields) > 1 else ""
            reason = fields[2].strip() if len(fields) > 2 else ""
            if state == "R" and node:
                running.append((jid, node))
            elif state in ("F", "CA", "TO", "NF"):
                emit("warning", "gpu_alloc",
                     f"GPU candidate {cand.label()} (job {jid}) failed to start ({state} {reason}); dropping it.")
                del jobs[jid]
        running.sort(key=lambda r: rank[r[0]])
        if running:
            jid, node = running[0]
            cand = jobs[jid]
            better_pending = [j for j in jobs if rank[j] < rank[jid]]
            if better_pending and time.monotonic() - started < prefer:
                if jid not in held_note:
                    held_note.add(jid)
                    emit("info", "gpu_alloc",
                         f"{cand.label()} started on {node}, but holding up to {prefer}s for the preferred "
                         f"{', '.join(jobs[j].label() for j in better_pending)} ...")
            else:
                losers = [j for j in jobs if j != jid]
                if losers:
                    executor.exec(f"scancel {' '.join(losers)}")
                    emit("info", "gpu_alloc",
                         f"Winner: {cand.label()} on {node} — cancelled {len(losers)} other candidate(s).")
                port = read_serve_port(executor, settings, job_id=jid)
                emit("success", "gpu_alloc",
                     f"GPU job {jid} is RUNNING on node {node} (vLLM port {port}).")
                return GPUAllocation(job_id=jid, node=node, port=port)
        if not jobs:
            raise GatewayError("Every GPU candidate entered a failed state before starting.", stage="gpu_alloc")
        now = time.monotonic()
        if now - last_pending_emit >= 30:
            last_pending_emit = now
            emit("info", "gpu_alloc",
                 f"Still queued: {', '.join(f'{c.label()}(job {j})' for j, c in jobs.items())} ...")
        time.sleep(3)

    if jobs:
        executor.exec(f"scancel {' '.join(jobs)}")
    raise GatewayError(
        f"No GPU candidate started within {wait_seconds}s. The GPU queue may be long.",
        stage="gpu_alloc",
        detail={"job_ids": list(jobs)},
    )


def check_health(
    executor: RemoteExecutor,
    settings: HPCSettings,
    alloc: GPUAllocation,
) -> GPUHealth:
    """Run nvidia-smi on the allocated node and judge whether the GPU is healthy."""
    result = executor.exec(
        f"srun --jobid={alloc.job_id} --overlap nvidia-smi "
        "--query-gpu=utilization.gpu,memory.used,memory.total,name "
        "--format=csv,noheader,nounits",
        timeout=30,
    )
    if not result.ok or not result.out:
        return GPUHealth(
            healthy=False,
            util_percent=0,
            mem_used_mb=0,
            mem_total_mb=0,
            name="unknown",
            reason=(
                f"nvidia-smi failed on node {alloc.node} (exit {result.exit_status}). "
                f"GPU link may be down. stderr: {result.stderr.strip()[:300]}"
            ),
        )
    first = result.out.splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    try:
        util = int(float(parts[0]))
        mem_used = int(float(parts[1]))
        mem_total = int(float(parts[2]))
        name = parts[3] if len(parts) > 3 else "GPU"
    except (ValueError, IndexError):
        return GPUHealth(False, 0, 0, 0, "unknown", reason=f"Could not parse nvidia-smi output: {first!r}")
    return GPUHealth(
        healthy=True,
        util_percent=util,
        mem_used_mb=mem_used,
        mem_total_mb=mem_total,
        name=name,
    )
