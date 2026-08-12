#!/usr/bin/env python3
"""Backend-only debug runner — drive the REAL pipeline/Biomni and dump one full log.

Purpose (the validation pivot): instead of clicking through the frontend to find
where a run breaks, run the backend headlessly against the live Qwen3.6 tunnel,
capture EVERYTHING into one timestamped log, and read the DIAGNOSIS at the end.

What it captures into the log file:
  - a config snapshot (execute gating, port, model, paths; api keys redacted);
  - a pre-flight: is biomni / scanpy / anndata installed, is the tunnel reachable;
  - every pipeline AgentMessage + step start/done, timestamped;
  - Biomni A1's own stdout (CodeAct trace) — tee'd from the process;
  - on any Biomni failure, the FULL traceback (the buried real cause);
  - a DIAGNOSIS section: per-agent errors, biomni status, missing-module hints,
    artifact list, and a final verdict + exit code.

Manual HPC3 step: you set up the GPU Slurm job + SSH tunnel yourself (your auth /
2FA). The script PAUSES and waits for you, then verifies the tunnel before running.

GPU release (guaranteed): on success, failure, OR Ctrl-C, the script always prints
the release commands. If you pass ``--slurm-job-id`` (and ``--hpc3-ssh``), it also
runs ``ssh <hpc3> scancel <jobid>`` in a finally/signal-guarded teardown — so a
forgotten or crashed run never leaves a GPU allocation burning.

Usage (on the eye server, after `pip install -e <biomni> scanpy anndata`):
    PYTHONPATH=src python scripts/backend_debug_run.py \
        --ollama-port 37219 --model qwen3.6:35b-a3b \
        --dataset /data/BioAgent/uploads/pbmc3k.h5ad \
        --slurm-job-id 1234567 --hpc3-ssh hpc3

Skip the manual pause (endpoint already up):  add --no-pause
Run only the fast Biomni probe:               --mode biomni
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --- repo import path (so `python scripts/...` works without PYTHONPATH=src) ---
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")


class _Tee(io.TextIOBase):
    """Write to the real stream AND the log file, so A1's prints land in both."""

    def __init__(self, real: object, logfile: object) -> None:
        self._real = real
        self._log = logfile

    def write(self, s: str) -> int:  # type: ignore[override]
        self._real.write(s)
        self._real.flush()
        self._log.write(s)
        self._log.flush()
        return len(s)

    def flush(self) -> None:  # type: ignore[override]
        self._real.flush()
        self._log.flush()


# A module-level log sink set up in main(); LOG() prefixes a timestamp.
_LOGFILE: object | None = None


def LOG(msg: str = "") -> None:
    print(f"[{_now()}] {msg}" if msg else "")


# --------------------------------------------------------------------------- #
# GPU teardown — guaranteed to run once on normal exit, error, or Ctrl-C.
# --------------------------------------------------------------------------- #
class _Teardown:
    def __init__(self, job_id: str | None, hpc3_ssh: str | None, auto: bool, tunnel_port: int | None) -> None:
        self.job_id = job_id
        self.hpc3_ssh = hpc3_ssh
        self.auto = auto
        self.tunnel_port = tunnel_port
        self._done = False

    def _print_manual(self) -> None:
        LOG("Release commands (run these if not done automatically):")
        if self.job_id:
            target = f"ssh {self.hpc3_ssh} " if self.hpc3_ssh else ""
            print(f"    {target}scancel {self.job_id}      # free the HPC3 GPU allocation")
        else:
            print("    ssh <hpc3> scancel <jobid>           # free the HPC3 GPU allocation (find jobid: squeue -u $USER)")
        if self.tunnel_port:
            print(f"    pkill -f '{self.tunnel_port}:'       # close the local SSH tunnel (or just close its terminal)")
        else:
            print("    # close the SSH tunnel terminal you opened for the manual step")

    def run(self) -> None:
        if self._done:
            return
        self._done = True
        print("\n" + "=" * 72)
        LOG("GPU TEARDOWN")
        print("=" * 72)
        if self.auto and self.job_id:
            cmd = ["ssh", self.hpc3_ssh, "scancel", self.job_id] if self.hpc3_ssh else ["scancel", self.job_id]
            LOG(f"auto scancel: {' '.join(cmd)}")
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if proc.returncode == 0:
                    LOG(f"  scancel OK (job {self.job_id} cancelled). GPU released.")
                else:
                    LOG(f"  scancel exit={proc.returncode}: {(proc.stderr or proc.stdout).strip()}")
                    LOG("  >> scancel did not confirm — RELEASE THE GPU MANUALLY:")
                    self._print_manual()
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                LOG(f"  scancel failed to run ({type(exc).__name__}: {exc}).")
                LOG("  >> RELEASE THE GPU MANUALLY:")
                self._print_manual()
        else:
            if self.auto and not self.job_id:
                LOG("no --slurm-job-id given, cannot auto-scancel.")
            self._print_manual()
        print("=" * 72)


# --------------------------------------------------------------------------- #
# Pre-flight checks
# --------------------------------------------------------------------------- #
def _have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def preflight(base_url: str) -> dict[str, bool]:
    LOG("PRE-FLIGHT")
    checks = {
        "biomni installed": _have("biomni"),
        "scanpy installed": _have("scanpy"),
        "anndata installed": _have("anndata"),
        "pandas installed": _have("pandas"),
    }
    for name, ok in checks.items():
        LOG(f"  [{'OK ' if ok else 'MISS'}] {name}")
    reachable = _check_tunnel(base_url)
    checks["tunnel reachable"] = reachable
    LOG(f"  [{'OK ' if reachable else 'DOWN'}] Qwen3.6 endpoint {base_url}")
    return checks


def _check_tunnel(base_url: str, timeout: float = 8.0) -> bool:
    """GET <base_url>/models (OpenAI-compatible) to confirm the tunnel is live."""
    url = base_url.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer ollama"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local tunnel
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        # Some servers answer /models with auth errors but are clearly up.
        return exc.code in (401, 403, 404)
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #
def run_biomni_probe(question: str, dataset: Path | None, ollama_port: int | None, model: str | None) -> dict:
    """One real BiomniAdapter.run — capture answer, A1 log, and full traceback."""
    from dataclasses import replace

    from bioagent.integrations.biomni_adapter import EXECUTE_MODE, BiomniAdapter, BiomniSafetyPolicy
    from bioagent.integrations.biomni_runtime import BiomniRuntimeConfig, RealBiomniRuntime

    cfg = BiomniRuntimeConfig.from_env()
    if ollama_port:
        cfg = replace(cfg, base_url=f"http://127.0.0.1:{ollama_port}/v1")
    if model:
        cfg = replace(cfg, model=model)
    LOG("BIOMNI PROBE (real A1.go)")
    LOG(f"  base_url={cfg.base_url}  model={cfg.model}  source={cfg.source}  data_path={cfg.data_path}")
    LOG(f"  dataset={dataset}")
    adapter = BiomniAdapter(policy=BiomniSafetyPolicy(mode=EXECUTE_MODE))
    started = time.perf_counter()
    out: dict[str, object] = {"phase": "biomni_probe"}
    try:
        result = adapter.run(question, dataset_path=dataset, runtime=RealBiomniRuntime(cfg))
        elapsed = time.perf_counter() - started
        run = result.run_result
        out["status"] = result.status
        out["notes"] = result.notes
        if run is not None:
            out["run_status"] = run.status
            out["answer"] = run.answer
            LOG(f"  status={result.status} run_status={run.status} ({elapsed:.1f}s)")
            LOG(f"  ANSWER: {run.answer[:800]}")
            if run.log:
                LOG("  --- A1 log (tail) ---")
                print(run.log[-4000:])
            if run.error:
                LOG(f"  run.error: {run.error}")
        else:
            LOG(f"  status={result.status} (no run_result) — {'; '.join(result.notes)}")
        out["ok"] = bool(result.executed and run is not None and run.status == "ok")
    except Exception as exc:  # noqa: BLE001 - the probe reports any cause, with traceback
        elapsed = time.perf_counter() - started
        tb = traceback.format_exc()
        out["status"] = "exception"
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = tb
        out["ok"] = False
        LOG(f"  EXCEPTION after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
        LOG("  --- traceback ---")
        print(tb)
    return out


def run_pipeline(workspace: Path, question: str, dataset: Path | None, ollama_port: int | None, model: str | None) -> dict:
    """Full 13-agent pipeline; log every message + step, then summarize artifacts."""
    from bioagent.core.models import AgentMessage
    from bioagent.workflows.vision import VisionResearchAgent

    LOG("PIPELINE (VisionResearchAgent, full 13-agent run)")
    LOG(f"  workspace={workspace}  dataset={dataset}  port={ollama_port}  model={model}")

    def on_step(agent: str, phase: str) -> None:
        if phase == "start":
            LOG(f"  -> {agent} ...")

    def on_message(msg: AgentMessage) -> None:
        meta = " ".join(f"{k}={v}" for k, v in (msg.metadata or {}).items())
        flag = " ⚠" if ("error" in msg.kind or str(msg.metadata.get("status", "")).endswith("error")) else ""
        LOG(f"     [{msg.sender}->{msg.recipient}] {msg.kind}{flag}: {msg.content}  {meta}".rstrip())

    agent = VisionResearchAgent(workspace, use_llm=False, ollama_port=ollama_port, ollama_model=model)
    started = time.perf_counter()
    state = agent.run(question, dataset_path=dataset, on_step=on_step, on_message=on_message)
    elapsed = time.perf_counter() - started
    LOG(f"  pipeline finished in {elapsed:.1f}s")

    out: dict[str, object] = {"phase": "pipeline", "run_id": state.run_id}
    biomni = state.decisions.get("biomni_execution")
    out["biomni_execution"] = biomni
    errors = [
        {"sender": m.sender, "kind": m.kind, "content": m.content}
        for m in state.messages
        if "error" in m.kind or str(m.metadata.get("status", "")).endswith("error")
    ]
    out["errors"] = errors
    out["artifacts"] = sorted(p.name for p in state.artifacts_dir.glob("*")) if state.artifacts_dir.exists() else []
    out["artifacts_dir"] = str(state.artifacts_dir)
    out["ok"] = not errors
    return out


# --------------------------------------------------------------------------- #
# Diagnosis
# --------------------------------------------------------------------------- #
def diagnose(results: list[dict], captured: str) -> bool:
    print("\n" + "#" * 72)
    LOG("DIAGNOSIS")
    print("#" * 72)
    overall_ok = True

    # Missing-module hints scraped from everything we captured.
    missing = sorted({
        line.split("'")[1]
        for line in captured.splitlines()
        if "No module named" in line and "'" in line
    })
    if missing:
        overall_ok = False
        LOG("Missing Python modules detected (install on the server):")
        for m in missing:
            print(f"    pip install {m}")

    for res in results:
        phase = res.get("phase")
        ok = res.get("ok")
        overall_ok = overall_ok and bool(ok)
        LOG(f"[{ 'PASS' if ok else 'FAIL' }] {phase}")
        if phase == "biomni_probe" and not ok:
            if res.get("traceback"):
                LOG("  cause: see traceback above")
            elif res.get("status"):
                LOG(f"  status={res.get('status')} error={res.get('error') or res.get('notes')}")
        if phase == "pipeline":
            be = res.get("biomni_execution") or {}
            LOG(f"  biomni_execution.status = {be.get('status', '(absent — execute off or agent skipped)')}")
            if be.get("traceback"):
                LOG("  biomni traceback (head):")
                print("    " + "\n    ".join(str(be["traceback"]).strip().splitlines()[-12:]))
            for e in res.get("errors", []):
                LOG(f"  ⚠ {e['sender']}: {e['kind']}: {e['content']}")
            LOG(f"  artifacts ({len(res.get('artifacts', []))}) in {res.get('artifacts_dir')}:")
            print("    " + ", ".join(res.get("artifacts", [])))

    print("#" * 72)
    LOG(f"OVERALL: {'PASS' if overall_ok else 'FAIL — see above'}")
    print("#" * 72)
    return overall_ok


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backend-only debug runner with guaranteed GPU teardown.")
    p.add_argument("--mode", choices=["biomni", "pipeline", "both"], default="both")
    p.add_argument("--question", default="Run QC and identify marker genes for this PBMC single-cell dataset.")
    p.add_argument("--dataset", type=Path, default=None, help="Local .h5ad/.csv to analyze (path only; never enters the prompt).")
    p.add_argument("--ollama-port", type=int, default=None, help="Live SSH-tunnel port to the HPC3 Qwen3.6.")
    p.add_argument("--model", default=None, help="Ollama model tag, e.g. qwen3.6:35b-a3b.")
    p.add_argument("--workspace", type=Path, default=None, help="Pipeline workspace (default: ./runs/debug-<ts>).")
    p.add_argument("--log-dir", type=Path, default=Path("runs"), help="Where to write the debug log.")
    p.add_argument("--no-pause", action="store_true", help="Skip the manual HPC3-setup pause (endpoint already up).")
    p.add_argument("--slurm-job-id", default=None, help="HPC3 Slurm job id to scancel on exit (enables auto-teardown).")
    p.add_argument("--hpc3-ssh", default=None, help="SSH alias/host to run scancel on (e.g. hpc3).")
    p.add_argument("--keep-gpu", action="store_true", help="Do NOT auto-scancel; only print release commands.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from bioagent.core.config import load_project_env
    load_project_env(Path.cwd())  # pick up ./.env like the gateway does

    ts = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / f"backend-debug-{ts}.log"
    logfile = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - lifetime is the whole run

    teardown = _Teardown(
        job_id=args.slurm_job_id,
        hpc3_ssh=args.hpc3_ssh,
        auto=not args.keep_gpu,
        tunnel_port=args.ollama_port,
    )

    # Signal-guard teardown so Ctrl-C / SIGTERM still releases the GPU.
    def _handler(signum, _frame):  # noqa: ANN001
        LOG(f"received signal {signum} — releasing GPU before exit")
        teardown.run()
        raise SystemExit(130)

    signal.signal(signal.SIGINT, _handler)
    with contextlib.suppress(ValueError):  # SIGTERM may be unavailable in some envs
        signal.signal(signal.SIGTERM, _handler)

    # Tee stdout+stderr into the log for the rest of the run (captures A1's prints).
    cap = io.StringIO()  # also keep an in-memory copy for missing-module scraping
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(_Tee(real_out, logfile), cap)  # type: ignore[assignment]
    sys.stderr = _Tee(_Tee(real_err, logfile), cap)  # type: ignore[assignment]

    rc = 1
    try:
        print("=" * 72)
        LOG(f"BACKEND DEBUG RUN — log: {log_path}")
        print("=" * 72)

        # --- config snapshot ---
        from bioagent.integrations.biomni_runtime import BiomniRuntimeConfig
        from bioagent.integrations.execution import BiomniExecution
        ex = BiomniExecution.from_env(ollama_port=args.ollama_port, model=args.model)
        cfg = BiomniRuntimeConfig.from_env()
        base_url = f"http://127.0.0.1:{args.ollama_port}/v1" if args.ollama_port else cfg.base_url
        LOG("CONFIG")
        LOG(f"  mode={args.mode}  dataset={args.dataset}")
        LOG(f"  BIOMNI_EXECUTE enabled={ex.enabled}  runtime_kind={ex.runtime_kind}")
        LOG(f"  endpoint={base_url}  model={args.model or cfg.model}  source={cfg.source}")
        LOG(f"  data_path={cfg.data_path}  load_data_lake={cfg.load_data_lake}")
        LOG(f"  api_key={'<set>' if cfg.api_key else '<empty>'} (redacted)")
        if not ex.enabled:
            LOG("  NOTE: BIOAGENT_BIOMNI_EXECUTE is off -> Biomni stays plan-only (no real A1 call).")

        # --- manual HPC3 setup pause ---
        if not args.no_pause:
            print("\n" + "-" * 72)
            LOG("MANUAL STEP: set up your HPC3 GPU Slurm job + SSH tunnel now.")
            LOG("  1) ssh into HPC3 and launch your ollama-serve GPU job (your usual flow).")
            LOG("  2) open the SSH tunnel so the model is reachable at the port below.")
            LOG("  3) note the Slurm job id (squeue -u $USER) — pass it as --slurm-job-id for auto-release.")
            print("-" * 72)
            try:
                entered = input(f"[{_now()}] Press Enter when the tunnel is live (or type a port to use): ").strip()
            except EOFError:
                entered = ""
            if entered.isdigit():
                args.ollama_port = int(entered)
                base_url = f"http://127.0.0.1:{args.ollama_port}/v1"
                teardown.tunnel_port = args.ollama_port
                LOG(f"  using port {args.ollama_port}")

        # --- pre-flight ---
        checks = preflight(base_url)
        if not checks.get("tunnel reachable"):
            LOG("ABORT: the Qwen3.6 endpoint is not reachable — fix the tunnel, then re-run.")
            LOG("       (GPU teardown will still run below.)")
            rc = 2
            return rc

        # --- run ---
        results: list[dict] = []
        if args.mode in ("biomni", "both"):
            results.append(run_biomni_probe(args.question, args.dataset, args.ollama_port, args.model))
        if args.mode in ("pipeline", "both"):
            ws = args.workspace or (Path("runs") / f"debug-{ts}")
            ws.mkdir(parents=True, exist_ok=True)
            results.append(run_pipeline(ws, args.question, args.dataset, args.ollama_port, args.model))

        ok = diagnose(results, cap.getvalue())
        rc = 0 if ok else 1
        return rc
    except Exception:  # noqa: BLE001 - top-level: log the traceback, still tear down
        LOG("UNHANDLED EXCEPTION in debug runner:")
        traceback.print_exc()
        rc = 3
        return rc
    finally:
        teardown.run()  # guaranteed once: success, handled error, or top-level crash
        LOG(f"full log saved to: {log_path}")
        sys.stdout, sys.stderr = real_out, real_err
        logfile.close()


if __name__ == "__main__":
    raise SystemExit(main())
