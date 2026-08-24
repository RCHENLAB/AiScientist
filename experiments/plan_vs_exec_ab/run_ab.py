#!/usr/bin/env python3
"""Is it the PLAN or the EXECUTION?  Same scaffolding, different models, three stages measured apart.

The production runs on the DDX41 retina dataset produced reports with real defects (depth artefact
narrated as biology, "significant" after being told there is no valid p-value, fabricated captions,
run_code churn to max_steps on almost every step). Before buying a bigger model — or blaming the
model at all — separate the three places the model acts, hold everything else constant, and swap
ONLY the model:

  A  PLAN     the real ``ResearchLab._pi_plan`` (same _PI_SYSTEM, same dataset profile incl.
              design_by_arm, same protocol guidance, same tool roster) → agenda.  Scored by a
              deterministic rubric + two blind LLM judges.
  B  EXECUTE  the real ``ResearchLab._scientist`` on ONE FIXED plan (the 7-step plan the production
              PI wrote for run 8847d521ba32), step by step, with the REAL analysis tools running
              locally on the REAL dataset (seeded with the outputs of the earlier steps, exactly as
              production had them), then the real ``_critic``.  Scored deterministically: did it call
              the tool the step is about, with the arguments the step specifies; how many run_code
              calls; did it re-implement a catalog tool; did it finish with a grounded answer;
              turns / wall-clock / tokens.  Plus a blind judge on the finding's fidelity.
  C  WRITE    the real ``ResearchLab._synthesize`` on the SAME accepted findings (run 8847), with the
              CURRENT writer prompt and with the PRE-FIX writer prompt (git 9d72d43), so a model's
              own judgement is visible with and without the rules that were added to compensate.
              Scored deterministically (sci-notation %, "significant" after no-valid-p, donor
              wording, depth/library-size caveat present) + blind judge.

Every model runs through the SAME code paths production runs — the lab methods are called
directly, not re-implemented — so a difference between arms is the model, and a defect shared by
all arms is the scaffolding.

Model arms are OpenRouter ids; the production model (Qwen3.6-35B-A3B, thinking OFF — exactly how
the gateway calls it) is arm 0.  Reads OPENROUTER_API_KEY from the environment or from ``.env``.

Usage
  python experiments/plan_vs_exec_ab/run_ab.py --stage prep            # dataset profile + Stage-B seeds
  python experiments/plan_vs_exec_ab/run_ab.py --stage A --reps 3
  python experiments/plan_vs_exec_ab/run_ab.py --stage B --reps 2 --workers 4
  python experiments/plan_vs_exec_ab/run_ab.py --stage C --reps 2
  python experiments/plan_vs_exec_ab/run_ab.py --stage judge
  python experiments/plan_vs_exec_ab/run_ab.py --stage report
Outputs go under ``--out`` (default: experiments/plan_vs_exec_ab/results/).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

OR_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1") + "/chat/completions"
QUESTION = "What changes between DDX41 mutant and WT retina?"
# The HPC3 paths the recorded run wrote to, rewritten to each trial's local workspace so evidence
# pointers resolve. Matched by PATTERN rather than by a literal path: the account segment is
# whoever recorded the run, which is not knowable from this file and is nobody else's to hard-code.
# A literal also fails silently — the replace simply matches nothing and every pointer stays
# remote, with no error to notice.
_HPC_ROOT = "/dfs3b/ruic20_lab/software/AiScientist"
_REMOTE_RUN_RE = re.compile(re.escape(_HPC_ROOT) + r"/Temp/[^/\"]+/analysis/8847d521ba32")
_REMOTE_DATA_RE = re.compile(re.escape(_HPC_ROOT) + r"/uploads/[^/\"]+/Ddx41_DEG\.h5ad")


def _localize(txt: str, workspace: "Path | str", dataset: "Path | str") -> str:
    """Rewrite the recorded run's HPC3 paths onto this trial's local workspace / dataset."""
    txt = _REMOTE_RUN_RE.sub(str(workspace).replace("\\", "\\\\"), txt)
    return _REMOTE_DATA_RE.sub(str(dataset).replace("\\", "\\\\"), txt)
PRE_FIX_SHA = "9d72d43"      # last deployed sha before the writer/reviewer rules of 06e4937

# --------------------------------------------------------------------------------------------
# Model arms.  reasoning: None = provider default; "none" = explicitly off (how the gateway calls
# Qwen: enable_thinking=False on every role); "medium"/"high" = on.
# --------------------------------------------------------------------------------------------
ARMS: dict[str, dict[str, Any]] = {
    # production: same weights as prod (prod serves an AWQ 4-bit quant; these endpoints serve fp8 — noted).
    # Providers are PINNED: Venice returned empty content + no tool_calls for Qwen (a provider fault,
    # not a model fault), which would have been scored as "the model did nothing".
    "qwen36-35b":        {"model": "qwen/qwen3.6-35b-a3b", "reasoning": "none",
                          "provider": {"order": ["AkashML", "AtlasCloud", "Phala", "Io Net"], "ignore": ["Venice", "DeepInfra"], "require_parameters": True}},
    # same weights, thinking ON — the cheapest possible upgrade (no new GPU, no new model)
    "qwen36-35b-think":  {"model": "qwen/qwen3.6-35b-a3b", "reasoning": "medium", "max_tokens_scale": 3.0,
                          "provider": {"order": ["AkashML", "AtlasCloud", "Parasail", "Phala"], "ignore": ["Venice", "DeepInfra"], "require_parameters": True}},
    # bigger open-weight candidates that would still fit our own 4×96 GB nodes
    "qwen35-122b":       {"model": "qwen/qwen3.5-122b-a10b", "reasoning": "none",
                          "provider": {"order": ["Alibaba", "AtlasCloud", "Novita", "DeepInfra"], "require_parameters": True}},
    "deepseek-v4-pro":   {"model": "deepseek/deepseek-v4-pro", "reasoning": "none",
                          "provider": {"order": ["Alibaba", "BaseTen", "DeepInfra"], "require_parameters": True}},
    # frontier ceiling
    "sonnet-5":          {"model": "anthropic/claude-sonnet-5", "reasoning": "none",
                          "provider": {"order": ["Anthropic"], "allow_fallbacks": True}},
    "gpt-5.4":           {"model": "openai/gpt-5.4", "reasoning": None, "max_tokens_scale": 3.0,
                          "provider": {"order": ["OpenAI"], "allow_fallbacks": True}},
    # --- round 2 (2026-08-19, Yijun): open-weight candidates for our own 4×96 GB node ---
    # provider default reasoning (MiniMax M2.x think interleaved by design; DeepSeek V4 effort default)
    "minimax-m2.7":      {"model": "minimax/minimax-m2.7", "reasoning": None, "max_tokens_scale": 3.0,
                          "provider": {"order": ["Minimax", "Novita", "Fireworks", "GMICloud"], "require_parameters": True}},
    "minimax-m3":        {"model": "minimax/minimax-m3", "reasoning": None, "max_tokens_scale": 3.0,
                          "provider": {"order": ["Minimax", "Novita", "Together", "Parasail"], "ignore": ["Venice"], "require_parameters": True}},
    "deepseek-v4-flash": {"model": "deepseek/deepseek-v4-flash-0731", "reasoning": None, "max_tokens_scale": 3.0,
                          "provider": {"order": ["DeepSeek", "Fireworks", "Novita", "Parasail", "Together"], "require_parameters": True}},
    # flash at provider-default reasoning spent the ENTIRE 24k output budget thinking on the
    # synthesize call and returned empty content (4/4) — re-run with effort=low + 6x budget
    "deepseek-v4-flash-low": {"model": "deepseek/deepseek-v4-flash-0731", "reasoning": "low", "max_tokens_scale": 6.0,
                          "provider": {"order": ["DeepSeek", "Fireworks", "Novita", "Parasail", "Together"], "require_parameters": True}},
    # --- round 3 (2026-08-22, Jin Li's suggestion): a CODING-AGENT model, not a generalist ---
    # 118B total / 8B active, 1M context, $0.09/$0.18. At FP8 that is ~118 GB — it fits our own node
    # with room to spare, which none of the round-2 candidates do comfortably. Worth measuring on
    # BOTH stages: a coding-agent model should be strong at stage B (tool calls + run_code) and is
    # the open question at stage A (writing a sound analysis plan is not a coding task).
    "laguna-s-2.1":      {"model": "poolside/laguna-s-2.1", "reasoning": None, "max_tokens_scale": 3.0,
                          "provider": {"require_parameters": True}},
    # --- round 4 (2026-08-23): the SAME model we already measure through OpenRouter, but served
    # from OUR OWN cards (SGLang 0.5.18 + MJPansa 0731-NVFP4 + moe-runner marlin, TP=4 on
    # free-gpu32, reached over an SSH tunnel). Paired against "deepseek-v4-flash" this isolates one
    # variable: what our local NVFP4 quantisation costs versus the provider's FP8 serving.
    "dsv4-local":        {"model": "deepseek-v4-flash", "reasoning": None, "max_tokens_scale": 3.0,
                          "base_url": os.environ.get("DSV4_LOCAL_URL", "http://127.0.0.1:30000/v1")},
}
JUDGES = {
    "judge-opus5":   {"model": "anthropic/claude-opus-5", "reasoning": "low", "max_tokens_scale": 4.0},
    "judge-gemini31": {"model": "google/gemini-3.1-pro-preview", "reasoning": "low", "max_tokens_scale": 4.0},
}


# --------------------------------------------------------------------------------------------
# .env + OpenRouter client (stdlib only)
# --------------------------------------------------------------------------------------------
def _load_dotenv() -> None:
    for p in (ROOT / ".env", ROOT.parents[2] / ".env" if len(ROOT.parents) > 2 else None):
        if p and p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _api_key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("BIOAGENT_LLM_API_KEY")
    if not k:
        sys.exit("OPENROUTER_API_KEY not set")
    return k


class Usage:
    """Thread-safe token/cost/latency accounting per arm."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_arm: dict[str, dict[str, float]] = {}

    def add(self, arm: str, usage: dict[str, Any], seconds: float) -> None:
        with self._lock:
            d = self.by_arm.setdefault(arm, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                             "cost_usd": 0.0, "seconds": 0.0})
            d["calls"] += 1
            d["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            d["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            d["cost_usd"] += float(usage.get("cost") or 0.0)
            d["seconds"] += seconds


USAGE = Usage()


def _call_log(line: str) -> None:
    path = os.environ.get("PVE_CALL_LOG")
    if path:
        try:
            with open(path, "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {line}\n")
        except OSError:
            pass


def _with_deadline(fn, deadline_s: float):
    """Run ``fn`` in a daemon thread and give up after ``deadline_s`` — the ONLY reliable guard
    against a connection that neither delivers nor errors (observed: synchronized ~960 s hangs
    across unrelated providers, i.e. the network path, that no socket timeout caught)."""
    import queue
    q: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)

    def run():
        try:
            q.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001
            q.put(("err", exc))

    th = threading.Thread(target=run, daemon=True)
    th.start()
    try:
        kind, val = q.get(timeout=deadline_s)
    except queue.Empty:
        raise TimeoutError(f"call abandoned after {deadline_s:.0f}s (hung connection)")
    if kind == "err":
        raise val
    return val


def _stream_once(req: urllib.request.Request, *, stall_s: float, hard_s: float) -> dict[str, Any]:
    """Read one SSE stream and assemble the final assistant message.  Aborts when no delta arrives
    for ``stall_s`` seconds (OpenRouter keeps a hung upstream alive with comment lines, so a plain
    socket timeout never fires) or after ``hard_s`` total."""
    content: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    finish = None
    provider = None
    t_start = last_delta = time.time()
    with urllib.request.urlopen(req, timeout=30) as resp:
        while True:
            now = time.time()
            if now - last_delta > stall_s:
                raise TimeoutError(f"no delta for {stall_s:.0f}s (stream stall)")
            if now - t_start > hard_s:
                raise TimeoutError(f"stream exceeded {hard_s:.0f}s")
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            if ev.get("error") and not ev.get("choices"):
                raise RuntimeError(f"openrouter error: {ev['error']}")
            provider = ev.get("provider") or provider
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    last_delta = time.time()
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index") or 0)
                    slot = calls.setdefault(idx, {"id": tc.get("id"), "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"] if isinstance(fn["arguments"], str) else json.dumps(fn["arguments"])
                    last_delta = time.time()
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                if delta.get("reasoning") or delta.get("reasoning_content"):
                    last_delta = time.time()     # the model IS working, just thinking
    tool_calls = []
    for idx in sorted(calls):
        c = calls[idx]
        tool_calls.append({"id": c["id"] or f"call_{idx}", "type": "function",
                           "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}})
    return {"content": "".join(content), "tool_calls": tool_calls, "finish_reason": finish,
            "usage": usage, "provider": provider}


def or_chat(arm: str, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
            max_tokens: int = 4096, temperature: float = 0.3, timeout: int = 120,
            retries: int = 4) -> dict[str, Any]:
    """One OpenRouter chat call (STREAMED, so a hung upstream is detected as 'no delta for N s'
    instead of a 40-minute trickle).  Returns the assistant message as ``{content, tool_calls}``.
    A stalled provider is retried on the NEXT provider in the arm's order."""
    spec = ARMS.get(arm) or JUDGES[arm]
    scale = float(spec.get("max_tokens_scale") or 1.0)
    payload: dict[str, Any] = {
        "model": spec["model"], "messages": messages, "max_tokens": int(max_tokens * scale),
        "temperature": temperature, "usage": {"include": True}, "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    # A self-hosted OpenAI-compatible server (SGLang) rejects OpenRouter's extensions, so the
    # routing/usage/reasoning fields are only sent when the call actually goes to OpenRouter.
    local_url = spec.get("base_url")
    if local_url:
        payload.pop("usage", None)
    provider = json.loads(json.dumps(spec.get("provider") or {}))
    if provider and not local_url:
        payload["provider"] = provider
    r = spec.get("reasoning")
    if local_url:
        r = None
    if r == "none":
        payload["reasoning"] = {"effort": "none", "exclude": True}
    elif r:
        # reasoning deltas are INCLUDED in the stream so a thinking model is not mistaken for a
        # stalled upstream (they are dropped here, never stored)
        payload["reasoning"] = {"effort": r, "exclude": False}
    last: Exception | None = None
    n_msgs = len(messages)
    stall_s = float(timeout)
    for attempt in range(retries + 1):
        body = json.dumps(payload).encode("utf-8")
        url = (local_url.rstrip("/") + "/chat/completions") if local_url else OR_URL
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {'local' if local_url else _api_key()}", "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "HTTP-Referer": "https://aiscientist.local/experiments", "X-Title": "plan_vs_exec_ab"})
        t0 = time.time()
        try:
            out = _with_deadline(lambda: _stream_once(req, stall_s=stall_s, hard_s=(480.0 if r and r != "none" else 300.0)),
                                 deadline_s=(520.0 if r and r != "none" else 330.0))
            usage = out.pop("usage", {}) or {}
            prov = out.pop("provider", None)
            USAGE.add(arm, usage, time.time() - t0)
            if not out["content"] and not out["tool_calls"] and out.get("finish_reason") not in ("stop", "length", "tool_calls", None):
                raise RuntimeError(f"empty reply (finish={out.get('finish_reason')})")
            _call_log(f"{arm} ok {time.time() - t0:.1f}s prov={prov} msgs={n_msgs} "
                      f"ptok={usage.get('prompt_tokens')} ctok={usage.get('completion_tokens')} "
                      f"calls={[c['function']['name'] for c in out['tool_calls']]} content={len(out['content'])}ch")
            return out
        except urllib.error.HTTPError as exc:
            txt = exc.read().decode("utf-8", "replace")[:500]
            last = RuntimeError(f"HTTP {exc.code}: {txt}")
            _call_log(f"{arm} HTTP{exc.code} attempt={attempt} {time.time() - t0:.1f}s {txt[:120]!r}")
            if exc.code == 400 and "reasoning" in txt.lower() and "reasoning" in payload:
                payload.pop("reasoning", None)          # provider rejects the flag: retry without it
                continue
            if exc.code in (400, 401, 402, 403, 404) and "context" not in txt.lower():
                break
        except Exception as exc:  # noqa: BLE001
            last = exc
            _call_log(f"{arm} EXC attempt={attempt} {time.time() - t0:.1f}s {type(exc).__name__}: {str(exc)[:120]}")
        # rotate providers so a stalled/limited one is not retried first
        order = (payload.get("provider") or {}).get("order") or []
        if len(order) > 1:
            payload["provider"]["order"] = order[1:] + order[:1]
        time.sleep(min(30, 2 * (2 ** attempt)))
    raise RuntimeError(f"{arm}: {last}")


# --------------------------------------------------------------------------------------------
# Lab wiring: real ResearchLab, real tools, local execution
# --------------------------------------------------------------------------------------------
class LocalShell:
    """The HPC3 filesystem/shell tool-set (list_dir/read_text/run_shell/...) bound to the LOCAL
    trial workspace, so the roster the model sees is production's roster and the calls do real
    (local) work.  Same method contract as ``bioagent.tools.hpc_shell.HpcShell``."""

    def __init__(self, workspace: Path) -> None:
        self.ws = workspace

    def _p(self, path: str) -> Path:
        path = (path or ".").replace("~", str(self.ws))
        p = Path(path)
        return p if p.is_absolute() else self.ws / p

    def list_dir(self, path: str, max_entries: int = 200) -> dict[str, Any]:
        p = self._p(path)
        if not p.exists():
            return {"status": "error", "error": f"No such directory: {path}"}
        entries = []
        for child in sorted(p.iterdir())[:max_entries]:
            st = child.stat()
            entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file",
                            "size_bytes": st.st_size, "mtime": int(st.st_mtime)})
        return {"status": "ok", "path": str(p), "entries": entries, "n": len(entries)}

    def stat_path(self, path: str) -> dict[str, Any]:
        p = self._p(path)
        if not p.exists():
            return {"status": "ok", "path": str(p), "exists": False}
        st = p.stat()
        return {"status": "ok", "path": str(p), "exists": True, "type": "dir" if p.is_dir() else "file",
                "size_bytes": st.st_size}

    def find_files(self, root: str, pattern: str = "*", max_depth: int = 4, max_results: int = 100) -> dict[str, Any]:
        r = self._p(root)
        out = []
        if r.exists():
            for p in r.rglob(pattern):
                if len(p.relative_to(r).parts) <= max_depth:
                    out.append(str(p))
                if len(out) >= max_results:
                    break
        return {"status": "ok", "root": str(r), "matches": out, "n": len(out)}

    def read_text(self, path: str, max_bytes: int = 200_000, tail: bool = False) -> dict[str, Any]:
        p = self._p(path)
        if not p.exists() or not p.is_file():
            return {"status": "error", "error": f"No such file: {path}"}
        data = p.read_bytes()
        chunk = data[-max_bytes:] if tail else data[:max_bytes]
        return {"status": "ok", "path": str(p), "size_bytes": len(data), "truncated": len(data) > max_bytes,
                "text": chunk.decode("utf-8", "replace")}

    def disk_usage(self, path: str) -> dict[str, Any]:
        return {"status": "ok", "path": str(self._p(path)), "note": "local trial workspace"}

    def run_shell(self, command: str, timeout_s: int = 0, workdir: str = "") -> dict[str, Any]:
        cwd = self._p(workdir) if workdir else self.ws
        env = dict(os.environ)
        env.update({"BIOAGENT_WORK": str(self.ws / "work"), "BIOAGENT_ARTIFACTS": str(self.ws / "artifacts")})
        try:
            cp = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True, text=True,
                                timeout=timeout_s or 120, env=env)
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": "timeout"}
        return {"status": "ok" if cp.returncode == 0 else "error", "returncode": cp.returncode,
                "stdout": cp.stdout[-8000:], "stderr": cp.stderr[-4000:]}

    def fetch_url(self, url: str, dest_dir: str = "", max_mb: int = 0) -> dict[str, Any]:
        return {"status": "error", "error": "fetch_url disabled in the offline A/B harness"}

    def install_package(self, package: str, import_name: str = "") -> dict[str, Any]:
        return {"status": "error", "error": "install_package disabled in the offline A/B harness"}


def make_lab(arm: str, workspace: Path, dataset_path: Path, dataset_result: dict[str, Any],
             guidance: str | None, *, scientist_max_tokens: int = 2048,
             max_steps: int | None = None):
    from bioagent.agents.registry import build_scientist_catalog
    from bioagent.agents.research_harness import HarnessConfig, HarnessContext, ResearchHarness
    from bioagent.agents.research_lab import LabConfig, ResearchLab
    from bioagent.agents.sandbox import CodeSandbox
    from bioagent.tools.hpc_shell import hpc_shell_catalog

    (workspace / "work").mkdir(parents=True, exist_ok=True)
    (workspace / "artifacts" / "tables").mkdir(parents=True, exist_ok=True)
    (workspace / "artifacts" / "figures").mkdir(parents=True, exist_ok=True)
    ctx = HarnessContext(decisions={"dataset_path": str(dataset_path), "dataset_result": dataset_result},
                         workspace=workspace, model=ARMS[arm]["model"], llm_is_remote=False)
    sandbox = CodeSandbox(dataset_path=str(dataset_path), work_dir=str(workspace / "work"),
                          artifacts_dir=str(workspace / "artifacts"))
    catalog = build_scientist_catalog(code_executor=sandbox)
    # production roster = registry + HPC3 shell tools (+ skill tools + read_tool_source, which the
    # lab constructor attaches itself).  Bind the shell tools to the local trial workspace.
    catalog.extend(hpc_shell_catalog(LocalShell(workspace)))  # type: ignore[arg-type]

    def chat_fn(messages, tools):
        return or_chat(arm, messages, tools=tools, max_tokens=scientist_max_tokens)

    def complete_fn(messages):
        return or_chat(arm, messages, max_tokens=8192).get("content") or ""

    # The tool-call budget. Default None = HarnessConfig's 8, which is what production uses, so
    # the measured numbers describe the system as deployed. --max-steps raises it to answer a
    # different question: is a low score the model, or the budget? (Yijun, 2026-08-22.)
    _hc = HarnessConfig() if max_steps is None else HarnessConfig(max_steps=max_steps)
    scientist = ResearchHarness(catalog=catalog, chat_fn=chat_fn, config=_hc)
    lab = ResearchLab(ctx, LabConfig(planner="dag", auto_select_skill=False, multi_agent=True),
                      complete_fn=complete_fn, scientist=scientist)
    lab._guidance = guidance
    return lab


def _events_sink(store: list[dict[str, Any]]):
    def emit(ev: dict[str, Any]) -> None:
        try:
            json.dumps(ev)
            store.append(ev)
        except TypeError:
            store.append({"type": ev.get("type"), "unserialisable": True})
    return emit


# --------------------------------------------------------------------------------------------
# Inputs: run 8847 (fixed plan + recorded rounds), dataset profile
# --------------------------------------------------------------------------------------------
@dataclass
class Inputs:
    run_state: dict[str, Any]
    dataset_path: Path
    dataset_result: dict[str, Any]
    guidance: str | None
    agenda: list[str]


def load_inputs(args) -> Inputs:
    rs = json.loads(Path(args.run_state).read_text())
    ds = Path(args.dataset)
    prof_path = Path(args.out) / "dataset_result.json"
    if prof_path.exists():
        dr = json.loads(prof_path.read_text())
    else:
        from bioagent.tools.datasets import run_dataset_smoke_analysis
        dr = run_dataset_smoke_analysis(ds, Path(args.out) / "profile")["result"]
        dr["dataset_path"] = str(ds)
        prof_path.parent.mkdir(parents=True, exist_ok=True)
        prof_path.write_text(json.dumps(dr, indent=1, default=str))
    return Inputs(rs, ds, dr, rs.get("guidance") or None, list(rs["agenda"]))


def localized_rounds(inp: Inputs, workspace: Path, upto_step_index: int | None = None):
    """Recorded ACCEPTED rounds of run 8847 as LabRound objects with every remote path rewritten to
    this trial's local workspace / dataset (so evidence pointers resolve locally)."""
    from bioagent.agents.research_lab import CriticVerdict, LabRound
    txt = json.dumps(inp.run_state["rounds"])
    txt = _localize(txt, workspace, inp.dataset_path)
    rounds_raw = json.loads(txt)
    agenda = inp.agenda
    out = []
    for r in rounds_raw:
        if r["verdict"]["verdict"] != "accept":
            continue
        try:
            step_no = agenda.index(r["step"]) + 1
        except ValueError:
            continue
        if upto_step_index is not None and step_no >= upto_step_index:
            continue
        out.append(LabRound(round_no=int(r.get("round_no") or step_no), step_index=step_no, step=r["step"],
                            specialist=r["specialist"], scientist_result=r["scientist_result"],
                            verdict=CriticVerdict.from_dict(r["verdict"])))
    out.sort(key=lambda x: x.step_index)
    return out


def recorded_calls_for_step(inp: Inputs, step_no: int) -> list[dict[str, Any]]:
    step = inp.agenda[step_no - 1]
    calls = []
    for r in inp.run_state["rounds"]:
        if r["step"] != step or r["verdict"]["verdict"] != "accept":
            continue
        for st in r["scientist_result"].get("steps", []):
            calls.append(st)
    return calls


def clone_dir(src: Path, dst: Path) -> None:
    """APFS clone (instant, copy-on-write) with a portable fallback."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["cp", "-c", "-R", str(src), str(dst)], check=True)
    else:
        shutil.copytree(src, dst)


# --------------------------------------------------------------------------------------------
# Stage prep: seed workspaces = the state production had before each step (replay prod's calls)
# --------------------------------------------------------------------------------------------
def stage_prep(args, inp: Inputs) -> None:
    seeds = Path(args.out) / "seeds"
    seeds.mkdir(parents=True, exist_ok=True)
    seed = seeds / "before_step_1"
    if not seed.exists():
        seed.mkdir()
        (seed / "work").mkdir()
        (seed / "artifacts" / "tables").mkdir(parents=True)
        (seed / "artifacts" / "figures").mkdir(parents=True)
    log = []
    for k in range(1, len(inp.agenda) + 1):
        nxt = seeds / f"before_step_{k + 1}"
        if nxt.exists() and not args.force:
            print(f"[prep] seed before_step_{k + 1} exists — skip")
            continue
        ws = seeds / f"_replay_step_{k}"
        clone_dir(seeds / f"before_step_{k}", ws)
        # replay through the SAME local catalog the trials use (an arm is needed only for the
        # roster; no LLM call is made here)
        lab = make_lab("qwen36-35b", ws, inp.dataset_path, inp.dataset_result, inp.guidance)
        tools = {t.name: t for t in lab.scientist.catalog}
        calls = recorded_calls_for_step(inp, k)
        print(f"[prep] step {k}: replaying {len(calls)} recorded call(s)")
        for st in calls:
            name, a = st["tool"], dict(st.get("args") or {})
            if name in ("finish",) or not st.get("ok", True):
                continue
            if name in ("run_shell",):
                continue        # prod's shell probes of dfs3b paths mean nothing locally
            txt = _localize(json.dumps(a), ws, inp.dataset_path)
            a = json.loads(txt)
            t0 = time.time()
            try:
                res = tools[name].executor(a, lab.ctx)
                status = (res or {}).get("status", "ok") if isinstance(res, dict) else "ok"
                err = (res or {}).get("error") if isinstance(res, dict) else None
            except Exception as exc:  # noqa: BLE001
                status, err = "exception", str(exc)[:300]
            dt = time.time() - t0
            log.append({"step": k, "tool": name, "status": status, "error": err, "seconds": round(dt, 1)})
            print(f"       {name:18s} {status:9s} {dt:6.1f}s {('— ' + str(err)[:100]) if err else ''}")
        clone_dir(ws, nxt)
        shutil.rmtree(ws)
    (Path(args.out) / "prep_log.json").write_text(json.dumps(log, indent=1))


# --------------------------------------------------------------------------------------------
# Stage A: planning
# --------------------------------------------------------------------------------------------
_TOOL_RE = re.compile(r"`(\w+)`")


def _step_names_method(agenda: "list[str]", tools: "tuple[str, ...]",
                       patterns: "tuple[str, ...]") -> bool:
    """Does the step that uses one of ``tools`` also pin down HOW it runs?

    A plan can name the right tool and still leave the executor a decision — which test, which
    gene-set library, which contrast. That gap is invisible to a presence check and is exactly
    what produces "the description does not match what the tool does" in the finished report.
    Steps that never mention the tool at all score False here, which is correct: a step nobody can
    execute is not specific.
    """
    steps = [s for s in agenda if any(t in s for t in tools)]
    if not steps:
        return False
    text = "\n".join(steps).lower()
    return all(re.search(p, text, re.I) for p in patterns)


def _plan_rubric(agenda: list[str], catalog_names: set[str], design_by_arm: Any) -> dict[str, Any]:
    text = "\n".join(agenda).lower()
    named = [n for s in agenda for n in _TOOL_RE.findall(s)]
    run_named = [n for n in named if n.startswith("run_") or n in ("deep_literature", "literature_search",
                                                                   "scgpt_annotate", "inspect_dataset")]
    hallucinated = sorted({n for n in run_named if n not in catalog_names})
    idx = {}
    for i, s in enumerate(agenda):
        for n in _TOOL_RE.findall(s):
            idx.setdefault(n, i)
    de_i = min([i for n, i in idx.items() if n in ("run_de", "run_pseudobulk_de")] or [99])
    enr_i = min([i for n, i in idx.items() if n in ("run_enrichment", "run_gsea_prerank")] or [-1])
    checks = {
        "no_hallucinated_tools": not hallucinated,
        "has_qc": "run_scanpy_qc" in named or "qc" in text,
        "has_de": de_i < 99,
        "de_stratified_by_cell_type": bool(re.search(r"stratif|within each|per[- ]cell[- ]type|per major|each major|majorclass|celltype", text)),
        "has_composition": "run_composition" in named or "composition" in text or "abundance" in text,
        "has_enrichment": enr_i >= 0,
        "enrichment_after_de": (enr_i < 0) or (de_i < 99 and enr_i > de_i),
        "has_literature": any(n in named for n in ("deep_literature", "literature_search")) or "literature" in text,
        "replication_aware": bool(re.search(r"no replicate|single (library|donor|sample)|one library|pseudoreplic|descriptive|exploratory", text)),
        # STRICT: the profile told the planner the DDX41 library is 1.6x deeper per cell. Generic
        # "normalise for sequencing depth" does not count — only a step that checks/corrects/flags
        # the imbalance between the two arms.
        "depth_aware": bool(re.search(r"depth (imbalance|difference|disparit|confound|bias)|imbalance in (sequencing )?depth|"
                                      r"1\.6|deeper|more deeply|shallower|depth[- ]match|down-?sampl|"
                                      r"differ(s|ence|ent)? in (sequencing depth|library size|ncount|umi)|"
                                      r"ncount_rna (differ|imbalance|higher|lower)|median (ncount|umi)|"
                                      r"per-arm (depth|ncount|qc)|depth (per|by) (arm|condition|sample)|"
                                      r"(higher|lower|greater) (sequencing depth|ncount|umi|library size)", text)),
        "mentions_depth_generic": bool(re.search(r"sequencing depth|library size|ncount", text)),
        "reuses_existing_labels": bool(re.search(r"existing (annotation|label|majorclass)|pre-?annotated|majorclass", text)),
        "no_report_busywork": not re.search(r"\b(write|compile|assemble|render|package|zip)\b[^.]{0,40}\b(report|manuscript|bundle|archive)\b", text),
        "titled_steps": all(s.lstrip().startswith("**") for s in agenda),
        "step_count_ok": 4 <= len(agenda) <= 12,
        # --- executability, added 2026-08-22 (Yijun) ------------------------------------------
        # Presence checks alone score a vague plan full marks. The DeepSeek-V4-Flash plan that
        # actually ran in production (run 97dfc89dc5aa) said "perform a descriptive comparison
        # (e.g., log2 fold change and a rank-based metric)" and "run pathway enrichment (e.g.,
        # GSEA or over-representation)" and scored 1.00 under the old rubric — neither says
        # HOW. The executor then has to choose the method itself, which is where the churn and the
        # description-does-not-match-the-tool defects come from. These three ask whether a step
        # could be handed to someone and executed without further decisions.
        "de_step_names_method": _step_names_method(agenda, ("run_de", "run_pseudobulk_de"),
                                                   (r"wilcoxon|rank[- ]sum|t-test|deseq|negative binomial",
                                                    r"reference\s*=|groupby\s*=|stratify_by\s*=")),
        "enrichment_step_names_library": _step_names_method(
            agenda, ("run_enrichment", "run_gsea_prerank"),
            (r"go[_ ]biological|gene ontology|reactome|hallmark|msigdb|kegg|\.gmt", )),
        "no_hedged_method": not any(
            re.search(r"\b(e\.g\.|such as|either)\b[^.]{0,80}\b(gsea|over-?representation|"
                      r"wilcoxon|t-test|deseq|pseudobulk|rank-based|fold[- ]change)\b", s, re.I)
            or re.search(r"\b(gsea|over-?representation)\b\s+or\s+\b(gsea|over-?representation)\b", s, re.I)
            for s in agenda),
    }
    score = sum(1 for v in checks.values() if v) / len(checks)
    return {"checks": checks, "score": round(score, 3), "hallucinated_tools": hallucinated,
            "n_steps": len(agenda), "named_tools": sorted(set(run_named))}


def stage_A(args, inp: Inputs) -> None:
    out = Path(args.out) / "A_plans.jsonl"
    done = _done_keys(out)
    # A re-run under CHANGED scaffolding (an edited SKILL.md, a new prompt) is a DIFFERENT
    # experiment on the same arm: without its own keys the already-drafted plans dedup it away,
    # and worse, mixing pre- and post-change plans under one arm silently averages the two.
    _tag = str(getattr(args, "tag", "") or "")
    tasks = [(arm, rep) for arm in args.arms for rep in range(args.reps)
             if f"{arm}{_tag}#{rep}" not in done]
    print(f"[A] {len(tasks)} plan(s) to draft")

    def one(arm: str, rep: int) -> dict[str, Any]:
        ws = Path(args.out) / "ws_A" / f"{arm}_{rep}"
        ws.mkdir(parents=True, exist_ok=True)
        lab = make_lab(arm, ws, inp.dataset_path, inp.dataset_result, inp.guidance)
        events: list[dict[str, Any]] = []
        t0 = time.time()
        parse_failures = 0
        kind, agenda, err = "error", [], None
        for _attempt in range(3):
            try:
                kind, agenda = lab._pi_plan(QUESTION, _events_sink(events))
                err = None
            except Exception as exc:  # noqa: BLE001
                kind, agenda, err = "error", [], f"{type(exc).__name__}: {exc}"
                break
            # _pi_plan falls back to [question] when the reply could not be parsed as a plan —
            # count it (it IS a failure of the model+parser pair) and try again
            if agenda and agenda[0].strip() == QUESTION:
                parse_failures += 1
                lab._self_sourced = ""
                continue
            break
        cat = {t.name for t in lab.scientist.catalog}
        rec = {"key": f"{arm}{_tag}#{rep}", "arm": arm + _tag, "rep": rep, "kind": kind, "agenda": agenda,
               "parse_failures": parse_failures,
               "seconds": round(time.time() - t0, 1), "error": err,
               "rubric": _plan_rubric(agenda, cat, inp.dataset_result.get("design_by_arm")) if agenda else None,
               "events": [e.get("type") for e in events]}
        print(f"[A] {arm}#{rep}: {kind} {len(agenda)} steps in {rec['seconds']}s"
              + (f"  rubric={rec['rubric']['score']}" if rec["rubric"] else f"  ERR {err}"))
        return rec

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(one, a, r) for a, r in tasks]
        for f in cf.as_completed(futs):
            _append(out, f.result())


# --------------------------------------------------------------------------------------------
# Stage B: execution of the fixed plan, one step at a time, real local tools
# --------------------------------------------------------------------------------------------
STEP_EXPECT: dict[int, dict[str, Any]] = {
    1: {"tool": "run_scanpy_qc", "args": {"min_genes": 200, "max_pct_mt": 10}},
    2: {"tool": "run_de", "args": {"groupby": "sampleid", "reference": "WT", "stratify_by": "majorclass"}},
    3: {"tool": "run_composition", "args": {"group_key": "majorclass"}, "any_of": {"condition_key": "sampleid", "sample_key": "sampleid"}},
    4: {"tool": "run_clustering", "args": {"n_pcs": 30, "n_neighbors": 15}},
    5: {"tool": "run_enrichment", "args": {"groupby": "majorclass"}},
    6: {"tool": "run_gsea_prerank", "args": {"groupby": "majorclass"}},
    7: {"tool": "run_code", "args": {}},
}
# run_code bodies that re-implement a catalog tool (the exact churn the roster is meant to prevent)
_REIMPL = {
    "run_scanpy_qc": re.compile(r"sc\.pp\.(filter_cells|filter_genes|highly_variable_genes|normalize_total)"),
    "run_de": re.compile(r"rank_genes_groups|mannwhitneyu|ranksums"),
    "run_clustering": re.compile(r"sc\.tl\.(leiden|louvain|umap)"),
    "run_enrichment": re.compile(r"gseapy\.enrich|gp\.enrich|enrichr\("),
    "run_gsea_prerank": re.compile(r"gseapy\.prerank|gp\.prerank"),
    "run_composition": re.compile(r"crosstab\([^)]*sampleid|value_counts\(\)[^\n]*normalize"),
}


def _step_metrics(step_no: int, result: dict[str, Any]) -> dict[str, Any]:
    exp = STEP_EXPECT[step_no]
    steps = result.get("steps") or []
    tools = [s.get("tool") for s in steps]
    named_calls = [s for s in steps if s.get("tool") == exp["tool"]]
    ok_named = [s for s in named_calls if s.get("ok")]
    arg_hits = arg_total = 0
    if named_calls:
        a = named_calls[0].get("args") or {}
        for k, v in exp["args"].items():
            arg_total += 1
            arg_hits += int(str(a.get(k)) == str(v))
        if exp.get("any_of"):
            arg_total += 1
            arg_hits += int(any(str(a.get(k)) == str(v) for k, v in exp["any_of"].items()))
    run_code = [s for s in steps if s.get("tool") == "run_code"]
    reimpl = 0
    rx = _REIMPL.get(exp["tool"])
    if rx is not None:
        for s in run_code:
            if rx.search(str((s.get("args") or {}).get("code") or "")):
                reimpl += 1
    recon = [s for s in steps if s.get("tool") in ("list_dir", "read_text", "stat_path", "find_files",
                                                    "inspect_dataset", "read_tool_source", "run_shell",
                                                    "search_skills", "read_skill_reference")]
    errors = result.get("errors") or []
    unknown = [e for e in errors if "unknown tool" in str(e.get("error", "")).lower()]
    fa = (result.get("final_answer") or "").strip()
    return {
        "tools_called": tools,
        "n_turns": len(steps),
        "named_tool_called": bool(named_calls),
        "named_tool_ok": bool(ok_named),
        "arg_match": (arg_hits / arg_total) if arg_total else None,
        "args_used": (named_calls[0].get("args") if named_calls else None),
        "n_run_code": len(run_code),
        "n_run_code_ok": sum(1 for s in run_code if s.get("ok")),
        "reimplemented_named_tool_in_run_code": reimpl,
        "n_recon": len(recon),
        "n_unknown_tool": len(unknown),
        "stop_reason": result.get("stop_reason"),
        "finished_with_answer": bool(fa) and result.get("stop_reason") in ("finished", "model_final_text"),
        "answer_chars": len(fa),
        "hit_max_steps": result.get("stop_reason") == "max_steps",
    }


def _run_B_trial(spec: dict[str, Any]) -> dict[str, Any]:
    """Runs in a subprocess (scanpy + tools + one model). Returns the trial record."""
    _load_dotenv()
    args_ns = argparse.Namespace(**spec["args"])
    inp = load_inputs(args_ns)
    arm, step_no, rep = spec["arm"], spec["step"], spec["rep"]
    from bioagent.agents.research_lab import Specialist
    _ms = getattr(args_ns, "max_steps", None)
    _tag = f"_ms{_ms}" if _ms else ""
    ws = Path(args_ns.out) / "ws_B" / f"{arm}{_tag}_s{step_no}_r{rep}"
    clone_dir(Path(args_ns.out) / "seeds" / f"before_step_{step_no}", ws)
    # Reasoning arms think INSIDE the output budget: the 2048-token production reservation (built
    # for a no-think tool loop) starves them of the closing answer and understates completion.
    # Give any thinking arm an effectively unlimited turn budget (scale multiplies this further).
    _spec = ARMS[arm]
    _tok = 2048 if _spec.get("reasoning") == "none" else 16384
    lab = make_lab(arm, ws, inp.dataset_path, inp.dataset_result, inp.guidance,
                   scientist_max_tokens=_tok, max_steps=getattr(args_ns, "max_steps", None))
    prior = localized_rounds(inp, ws, upto_step_index=step_no)
    step = inp.agenda[step_no - 1]
    spec_name = "Single-Cell Bioinformatician & Biostatistician"
    specialist = Specialist(spec_name, f"You are the team's {spec_name}. You execute analysis steps "
                            "rigorously with the tools and report exactly what they returned.")
    events: list[dict[str, Any]] = []
    before = dict(USAGE.by_arm.get(arm) or {})
    t0 = time.time()
    try:
        res = lab._scientist(QUESTION, step, specialist, "", prior, _events_sink(events))
        result = res.to_dict() if hasattr(res, "to_dict") else {
            "status": res.status, "stop_reason": res.stop_reason, "final_answer": res.final_answer,
            "steps": res.steps, "errors": res.errors}
        err = None
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "stop_reason": "exception", "final_answer": "", "steps": [], "errors": []}
        err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
        res = None
    t_sci = time.time() - t0
    verdict = None
    if res is not None:
        try:
            v = lab._critic(QUESTION, step, res, _events_sink(events))
            verdict = {"verdict": v.verdict, "score": v.score, "critique": v.critique}
        except Exception as exc:  # noqa: BLE001
            verdict = {"verdict": "error", "score": 0.0, "critique": str(exc)[:300]}
    # keep tool results out of the record (large); keep tool, args, ok, summary
    slim_steps = []
    for s in result.get("steps") or []:
        slim_steps.append({"tool": s.get("tool"), "args": s.get("args"), "ok": s.get("ok"),
                           "summary": str(s.get("summary"))[:200]})
    slim = dict(result)
    slim["steps"] = slim_steps
    metrics = _step_metrics(step_no, result)
    tool_secs = [e for e in events if e.get("type") == "tool_result"]
    rec = {"key": f"{arm}{_tag}#s{step_no}#{rep}", "arm": arm + _tag, "step": step_no, "rep": rep,
           "seconds_scientist": round(t_sci, 1), "error": err, "result": slim, "metrics": metrics,
           "critic": verdict, "n_tool_events": len(tool_secs),
           "usage": {k: round(v - before.get(k, 0), 4) for k, v in (USAGE.by_arm.get(arm) or {}).items()}}
    # keep the workspace for inspection; drop only the (cloned, re-creatable) heavy checkpoints
    try:
        (ws / "trial_result.json").write_text(json.dumps({"result": result, "critic": verdict, "events": events},
                                                         default=str)[:5_000_000])
        for f in (ws / "work").glob("*.h5ad"):
            f.unlink()
    except OSError:
        pass
    return rec


def stage_B(args, inp: Inputs) -> None:
    out = Path(args.out) / "B_exec.jsonl"
    done = _done_keys(out)
    steps = [int(s) for s in args.steps.split(",")] if args.steps else list(STEP_EXPECT)
    # A raised --max-steps is a DIFFERENT experiment on the same arm, so it gets its own keys —
    # otherwise the already-done 8-call trials mask it and the run reports "0 trial(s)".
    _tag = f"_ms{args.max_steps}" if getattr(args, "max_steps", None) else ""
    tasks = [{"arm": arm, "step": k, "rep": rep, "args": vars(args)}
             for arm in args.arms for k in steps for rep in range(args.reps)
             if f"{arm}{_tag}#s{k}#{rep}" not in done]
    print(f"[B] {len(tasks)} trial(s)")
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    with cf.ProcessPoolExecutor(max_workers=args.workers, mp_context=ctx) as ex:
        futs = {ex.submit(_run_B_trial, t): t for t in tasks}
        for f in cf.as_completed(futs):
            t = futs[f]
            try:
                rec = f.result()
            except Exception as exc:  # noqa: BLE001
                rec = {"key": f"{t['arm']}{_tag}#s{t['step']}#{t['rep']}", "arm": t["arm"] + _tag, "step": t["step"],
                       "rep": t["rep"], "error": f"worker: {exc}", "metrics": None}
            _append(out, rec)
            m = rec.get("metrics") or {}
            print(f"[B] {rec['key']}: tools={m.get('tools_called')} named_ok={m.get('named_tool_ok')} "
                  f"args={m.get('arg_match')} run_code={m.get('n_run_code')} stop={m.get('stop_reason')} "
                  f"critic={(rec.get('critic') or {}).get('verdict')} {rec.get('seconds_scientist')}s"
                  + (f"  ERR {str(rec.get('error'))[:120]}" if rec.get("error") else ""))


# --------------------------------------------------------------------------------------------
# Stage C: writing — the REAL production path: lab._synthesize → gateway._build_report →
# gateway._review_report, on run 8847's accepted findings + its real figures/tables.
# "prefix" = the gateway writer/reviewer prompts as deployed at 9d72d43 (before the NUMBER FORMAT /
# REPLICATION WORDING / DESCRIPTIVE DE / CAPTION TRUTH rules) and the pre-fix dataset profile (no
# design_by_arm); "current" = the prompts + profile now in prod.
# --------------------------------------------------------------------------------------------
def _load_prefix_gateway():
    """Import the 9d72d43 gateway module side-by-side (it lives inside the package so its relative
    imports resolve).  Only its prompt-builder functions are used."""
    import importlib
    dst = ROOT / "src" / "bioagent" / "gateway" / "_app_prefix_9d72d43.py"
    if not dst.exists():
        src = subprocess.run(["git", "show", f"{PRE_FIX_SHA}:src/bioagent/gateway/app.py"], cwd=str(ROOT),
                             capture_output=True, text=True, check=True).stdout
        dst.write_text(src)
    return importlib.import_module("bioagent.gateway._app_prefix_9d72d43")


def _lab_result_from_run_state(inp: Inputs, workspace: Path):
    from bioagent.agents.research_lab import LabResult
    rounds = localized_rounds(inp, workspace)
    rs = inp.run_state
    return LabResult(question=rs["question"], agenda=list(rs["agenda"]), rounds=rounds,
                     converged=bool(rs.get("converged")), accepted_steps=int(rs.get("accepted_steps") or len(rounds)),
                     final_answer=rs.get("final_answer") or "")


_SCI_PCT = re.compile(r"\d\.\d+e[+-]?\d+\s*%")
_SIG = re.compile(r"\bsignificant(?:ly)?\b", re.I)
_NOT_SIG = re.compile(r"\b(not|no|non-?|without|non)\s+(statistically\s+)?significan", re.I)
_DONORS = re.compile(r"\b(two|three|2|3|multiple|several)\s+(donors|animals|mice|replicates|biological replicates)\b", re.I)
_DEPTH = re.compile(r"sequencing depth|library size|depth (difference|imbalance|artefact|artifact)|total counts per cell|ncount|umi(s)? per cell|more deeply sequenced|deeper", re.I)
_TECH_CAVEAT = re.compile(r"(technical|batch|depth|normali[sz]ation)[^.]{0,80}(artefact|artifact|confound|caveat|cannot be (excluded|ruled out)|may reflect|could reflect)", re.I)


def _report_rubric(report: str) -> dict[str, Any]:
    body = re.split(r"(?m)^# Output Files Index", report)[0]
    # the deterministic '## The dataset' section is not the model's writing — score the rest
    model_text = re.sub(r"(?s)## The dataset.*?(?=\n## )", "", body)
    model_text = re.sub(r"(?s)## What was run.*?(?=\n## )", "", model_text)
    sig = list(_SIG.finditer(model_text))
    neg = len(_NOT_SIG.findall(model_text))
    return {
        "chars": len(body),
        "sci_notation_percent": len(_SCI_PCT.findall(model_text)),
        "significant_claims": max(0, len(sig) - neg),
        "multi_donor_claim": len(_DONORS.findall(model_text)),
        "depth_mentioned": len(_DEPTH.findall(model_text)),
        "technical_caveat": bool(_TECH_CAVEAT.search(model_text)),
        "same_direction_pattern_named": bool(re.search(r"(across|in) (all|every|each) (tested )?(cell type|major class|class)[^.]{0,80}(up|increase|elevat|ribosom|translation)", model_text, re.I)),
        "n_figures_embedded": len(re.findall(r"!\[[^\]]*\]\(figures/", model_text)),
        "unrendered_markup": len(re.findall(r"\*\*[^*\n]{1,80}\*\*[^\n]{0,10}\*\*", model_text)) + model_text.count("```"),
    }


def stage_C(args, inp: Inputs) -> None:
    import bioagent.gateway.app as gw
    out = Path(args.out) / "C_reports.jsonl"
    done = _done_keys(out)
    old = _load_prefix_gateway()
    art_src = Path(args.run_state).resolve().parents[1]           # <run>/artifacts
    variants = {"prefix": old, "current": gw}
    tasks = [(arm, var, rep) for arm in args.arms for var in variants for rep in range(args.reps)
             if f"{arm}#{var}#{rep}" not in done]
    print(f"[C] {len(tasks)} report(s); art = {art_src}")

    def one(arm: str, var: str, rep: int) -> dict[str, Any]:
        ws = Path(args.out) / "ws_C" / f"{arm}_{var}_{rep}"
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        art = ws / "artifacts"
        shutil.copytree(art_src, art, ignore=shutil.ignore_patterns("process", "report", "*.h5ad"))
        if var == "current":
            # prod today profiles the dataset with design_by_arm → the deterministic dataset section
            # carries per-arm cell counts + the depth check
            (art / "data" / "dataset_results.json").write_text(json.dumps(inp.dataset_result, default=str))
        lab = make_lab(arm, ws, inp.dataset_path, inp.dataset_result, inp.guidance)
        result = _lab_result_from_run_state(inp, ws)
        mod = variants[var]
        events: list[dict[str, Any]] = []
        t0 = time.time()
        rec: dict[str, Any] = {"key": f"{arm}#{var}#{rep}", "arm": arm, "variant": var, "rep": rep}
        try:
            synthesis = lab._synthesize(QUESTION, result.rounds, _events_sink(events))
            rec["synthesis"] = synthesis
            complete_fn = lab._complete_fn
            draft = mod._build_report(synthesis, art, complete_fn, QUESTION, result)
            rec["draft"] = draft
            reviewed = mod._review_report(draft, art, complete_fn, QUESTION)
            report = gw._strip_render_residue(reviewed)
            err = None
        except Exception as exc:  # noqa: BLE001
            report, err = "", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1200:]}"
        rec.update({"report": report, "seconds": round(time.time() - t0, 1), "error": err,
                    "rubric": _report_rubric(report) if report else None})
        (ws / f"report_{arm}_{var}_{rep}.md").write_text(report or "")
        print(f"[C] {arm}/{var}#{rep}: {len(report)} chars in {rec['seconds']}s "
              f"{rec['rubric'] if rec['rubric'] else err}")
        return rec

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(one, *t) for t in tasks]
        for f in cf.as_completed(futs):
            _append(out, f.result())


# --------------------------------------------------------------------------------------------
# Judges (blind: the judge never sees which model wrote the artefact)
# --------------------------------------------------------------------------------------------
_JUDGE_PLAN = (
    "You are a senior single-cell bioinformatics reviewer. Score an analysis PLAN for the study below. "
    "Return ONLY JSON: {\"scientific_soundness\": 0-10, \"specificity\": 0-10, \"dataset_fidelity\": 0-10, "
    "\"efficiency\": 0-10, \"depth_confound_addressed\": true|false, \"replication_limit_addressed\": true|false, "
    "\"worst_problem\": \"one sentence\"}. scientific_soundness = would a careful analyst run this; "
    "specificity = are steps concrete (tools, parameters, what is compared to what); dataset_fidelity = does it "
    "use the facts of THIS dataset (two arms in one library each, 11 annotated major classes, per-arm depth "
    "difference) rather than a generic template; efficiency = no redundant or busywork steps; "
    "depth_confound_addressed = does any step plan to check or correct for the sequencing-depth difference "
    "between the DDX41 and WT libraries; replication_limit_addressed = does the plan treat the comparison as "
    "descriptive / acknowledge no replicates."
)
_JUDGE_EXEC = (
    "You are auditing ONE step of an automated analysis. You get the step the agent was told to execute, "
    "the tool calls it made (tool, arguments, ok flag, summary) and its final answer. Return ONLY JSON: "
    "{\"did_the_step\": 0-10, \"answer_grounded\": 0-10, \"wasted_effort\": 0-10, \"worst_problem\": \"one sentence\"}. "
    "did_the_step = did it perform what the step asked with the tool the step named and the parameters specified; "
    "answer_grounded = is the final answer a faithful account of what the tools returned (0 = empty or invented); "
    "wasted_effort = 10 means every call was needed, 0 means mostly reconnaissance / repeated reads / re-implementing "
    "a tool it already had."
)
_JUDGE_REPORT = (
    "You are a journal reviewer for a single-cell study report. FACTS you must know: the dataset is one 10x library "
    "per arm (DDX41 mutant vs WT), no biological replicates, so per-cell Wilcoxon p-values are not valid inference; "
    "the DDX41 library is ~1.6x more deeply sequenced per cell than WT (median nCount 3078 vs 1916) — the report's "
    "authors were NOT told this, but a careful analyst would suspect a depth/normalisation artefact when the same "
    "direction of change (ribosomal / translation genes up) appears in every cell type. Return ONLY JSON: "
    "{\"raises_technical_artefact_possibility\": true|false, \"consistent_about_replication\": true|false, "
    "\"overclaims_significance\": true|false, \"invents_details\": true|false, \"overall_quality\": 0-10, "
    "\"worst_problem\": \"one sentence\"}. raises_technical_artefact_possibility = does the report itself say the "
    "pan-cell-type same-direction shift could be technical (depth, library size, normalisation) rather than biology; "
    "consistent_about_replication = does it say the same thing about donors/libraries/replicates everywhere; "
    "overclaims_significance = does it call things statistically significant or report padj-based counts as findings; "
    "invents_details = figure captions or numbers not supported by the listed results."
)


def _judge(judge: str, system: str, user: str) -> dict[str, Any]:
    msg = or_chat(judge, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                  max_tokens=800, temperature=0.0)
    txt = msg.get("content") or ""
    m = re.search(r"\{(?:.|\n)*\}", txt)
    try:
        return json.loads(m.group(0)) if m else {"parse_error": txt[:200]}
    except json.JSONDecodeError:
        return {"parse_error": txt[:200]}


def stage_judge(args, inp: Inputs) -> None:
    outdir = Path(args.out)
    judges = list(JUDGES)
    jobs: list[tuple[str, str, str, str]] = []   # (stage, key, judge, prompt)
    for rec in _read(outdir / "A_plans.jsonl"):
        if not rec.get("agenda"):
            continue
        agenda = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(rec["agenda"]))
        user = (f"Study: {QUESTION}\nDataset facts: 15,307 cells, 33,696 genes; obs columns include sampleid "
                f"(DDX41 6,260 cells / WT 9,047 cells), majorclass (11 classes), celltype (87), orig.ident (1 level), "
                f"percent.mt, nCount_RNA, nFeature_RNA, nuclear_fraction; median nCount_RNA per cell DDX41 3,078 vs WT "
                f"1,916 (1.6x).\n\nPLAN:\n{agenda}")
        for j in judges:
            jobs.append(("A", rec["key"], j, user))
    for rec in _read(outdir / "B_exec.jsonl"):
        if not rec.get("result"):
            continue
        r = rec["result"]
        calls = "\n".join(f"- {s['tool']}({json.dumps(s.get('args'))[:600]}) ok={s.get('ok')} → {s.get('summary')}"
                          for s in r.get("steps") or [])
        user = (f"STEP: {inp.agenda[rec['step'] - 1]}\n\nTOOL CALLS ({len(r.get('steps') or [])}):\n{calls or '(none)'}\n\n"
                f"STOP REASON: {r.get('stop_reason')}\nFINAL ANSWER:\n{(r.get('final_answer') or '(empty)')[:6000]}")
        for j in judges:
            jobs.append(("B", rec["key"], j, user))
    for rec in _read(outdir / "C_reports.jsonl"):
        if not rec.get("report"):
            continue
        for j in judges:
            jobs.append(("C", rec["key"], j, f"REPORT:\n{rec['report'][:30000]}"))
    out = outdir / "judgements.jsonl"
    done = {(d["stage"], d["key"], d["judge"]) for d in _read(out)
            if not ("error" in (d.get("verdict") or {}) or "parse_error" in (d.get("verdict") or {}))}
    jobs = [j for j in jobs if (j[0], j[1], j[2]) not in done]
    print(f"[judge] {len(jobs)} judgement(s)")
    systems = {"A": _JUDGE_PLAN, "B": _JUDGE_EXEC, "C": _JUDGE_REPORT}

    def one(job):
        stage, key, judge, user = job
        try:
            verdict = _judge(judge, systems[stage], user)
        except Exception as exc:  # noqa: BLE001
            verdict = {"error": str(exc)[:200]}
        return {"stage": stage, "key": key, "judge": judge, "verdict": verdict}

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for rec in ex.map(one, jobs):
            _append(out, rec)
            print(f"[judge] {rec['stage']} {rec['key']} {rec['judge']}: {json.dumps(rec['verdict'])[:160]}")


# --------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------
def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.mean(xs), 2) if xs else "—"


def _pct(xs):
    xs = [x for x in xs if x is not None]
    return round(100 * sum(1 for x in xs if x) / len(xs)) if xs else "—"


def stage_report(args, inp: Inputs) -> None:
    outdir = Path(args.out)
    A = _read(outdir / "A_plans.jsonl")
    B = _read(outdir / "B_exec.jsonl")
    C = _read(outdir / "C_reports.jsonl")
    J = _read(outdir / "judgements.jsonl")
    jmap: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for j in J:
        jmap.setdefault((j["stage"], j["key"]), []).append(j)
    arms = [a for a in ARMS if any(r["arm"] == a for r in A + B + C)]
    for r in C:
        if r.get("report"):
            r["rubric"] = _report_rubric(r["report"])
    # recompute the plan rubric from the stored agendas (the rubric may have been tightened after
    # the plans were drafted); hallucinated-tool detection needs the roster
    if A:
        _lab = make_lab("qwen36-35b", outdir / "ws_report", inp.dataset_path, inp.dataset_result, inp.guidance)
        _cat = {t.name for t in _lab.scientist.catalog}
        for r in A:
            if r.get("agenda"):
                r["rubric"] = _plan_rubric(r["agenda"], _cat, None)
    lines = ["# Plan vs execution vs writing — same scaffolding, different models\n",
             f"Question: {QUESTION}\nFixed plan for stage B: production run 8847d521ba32 (7 steps).\n"]

    # ---- A
    lines.append("\n## A · Planning (real `_pi_plan`, current prompt + dataset profile incl. design_by_arm)\n")
    lines.append("| arm | n | steps | rubric | halluc. tools | depth-aware | replication-aware | judge: sound | specific | dataset-fid | depth✓ (judge) | seconds |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for a in arms:
        rs = [r for r in A if r["arm"] == a and r.get("rubric")]
        if not rs:
            continue
        js = [v["verdict"] for r in rs for v in jmap.get(("A", r["key"]), []) if "error" not in v["verdict"] and "parse_error" not in v["verdict"]]
        lines.append(f"| {a} | {len(rs)} | {_mean([r['rubric']['n_steps'] for r in rs])} | {_mean([r['rubric']['score'] for r in rs])} | "
                     f"{sum(len(r['rubric']['hallucinated_tools']) for r in rs)} | {_pct([r['rubric']['checks']['depth_aware'] for r in rs])}% | "
                     f"{_pct([r['rubric']['checks']['replication_aware'] for r in rs])}% | "
                     f"{_mean([j.get('scientific_soundness') for j in js])} | {_mean([j.get('specificity') for j in js])} | "
                     f"{_mean([j.get('dataset_fidelity') for j in js])} | {_pct([j.get('depth_confound_addressed') for j in js])}% | "
                     f"{_mean([r['seconds'] for r in rs])} |")
    errs = [r for r in A if r.get("error")]
    if errs:
        lines.append("\nPlanning errors: " + "; ".join(f"{r['key']}: {r['error'][:80]}" for r in errs))

    # ---- B
    lines.append("\n## B · Execution of the SAME 7-step plan (real `_scientist` + real local tools + real `_critic`)\n")
    lines.append("| arm | trials | named tool called | named tool ok | arg match | run_code/step | re-implemented tool | recon calls/step | hit max_steps | finished w/ answer | critic accept | judge: did step | grounded | not wasted | prompt tok/step |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for a in arms:
        rs = [r for r in B if r["arm"] == a and r.get("metrics") and not r.get("error")]
        if not rs:
            continue
        ms = [r["metrics"] for r in rs]
        js = [v["verdict"] for r in rs for v in jmap.get(("B", r["key"]), []) if "error" not in v["verdict"] and "parse_error" not in v["verdict"]]
        lines.append(f"| {a} | {len(rs)} | {_pct([m['named_tool_called'] for m in ms])}% | {_pct([m['named_tool_ok'] for m in ms])}% | "
                     f"{_mean([m['arg_match'] for m in ms])} | {_mean([m['n_run_code'] for m in ms])} | "
                     f"{sum(m['reimplemented_named_tool_in_run_code'] for m in ms)} | {_mean([m['n_recon'] for m in ms])} | "
                     f"{_pct([m['hit_max_steps'] for m in ms])}% | {_pct([m['finished_with_answer'] for m in ms])}% | "
                     f"{_pct([(r.get('critic') or {}).get('verdict') == 'accept' for r in rs])}% | "
                     f"{_mean([j.get('did_the_step') for j in js])} | {_mean([j.get('answer_grounded') for j in js])} | "
                     f"{_mean([j.get('wasted_effort') for j in js])} | {_mean([(r.get('usage') or {}).get('prompt_tokens') for r in rs if (r.get('usage') or {}).get('prompt_tokens')])} |")
    lines.append("\nPer step (named tool ok % / mean run_code):\n")
    lines.append("| arm | " + " | ".join(f"s{k} {STEP_EXPECT[k]['tool']}" for k in STEP_EXPECT) + " |")
    lines.append("|---|" + "---|" * len(STEP_EXPECT))
    for a in arms:
        cells = []
        for k in STEP_EXPECT:
            ms = [r["metrics"] for r in B if r["arm"] == a and r["step"] == k and r.get("metrics") and not r.get("error")]
            cells.append(f"{_pct([m['named_tool_ok'] for m in ms])}% / {_mean([m['n_run_code'] for m in ms])}" if ms else "—")
        lines.append(f"| {a} | " + " | ".join(cells) + " |")
    errs = [r for r in B if r.get("error")]
    if errs:
        lines.append(f"\nExecution errors ({len(errs)}): " + "; ".join(f"{r['key']}: {str(r['error'])[:80]}" for r in errs[:12]))

    # ---- C
    lines.append("\n## C · Writing the SAME accepted findings (real `_synthesize`)\n")
    lines.append("| arm | prompt | n | chars | sci-notation % | 'significant' claims | multi-donor claim | depth mentioned | technical caveat | judge: raises artefact | consistent replication | overclaims | invents | quality |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for a in arms:
        for var in ("prefix", "current"):
            rs = [r for r in C if r["arm"] == a and r["variant"] == var and r.get("rubric") and not r.get("error")]
            if not rs:
                continue
            rb = [r["rubric"] for r in rs]
            js = [v["verdict"] for r in rs for v in jmap.get(("C", r["key"]), []) if "error" not in v["verdict"] and "parse_error" not in v["verdict"]]
            lines.append(f"| {a} | {var} | {len(rs)} | {_mean([x['chars'] for x in rb])} | {sum(x['sci_notation_percent'] for x in rb)} | "
                         f"{_mean([x['significant_claims'] for x in rb])} | {sum(x['multi_donor_claim'] for x in rb)} | "
                         f"{_mean([x['depth_mentioned'] for x in rb])} | {_pct([x['technical_caveat'] for x in rb])}% | "
                         f"{_pct([j.get('raises_technical_artefact_possibility') for j in js])}% | "
                         f"{_pct([j.get('consistent_about_replication') for j in js])}% | "
                         f"{_pct([j.get('overclaims_significance') for j in js])}% | {_pct([j.get('invents_details') for j in js])}% | "
                         f"{_mean([j.get('overall_quality') for j in js])} |")

    # ---- cost
    lines.append("\n## Tokens / cost / latency (this experiment, per arm)\n")
    usage_path = outdir / "usage.json"
    if usage_path.exists():
        u = json.loads(usage_path.read_text())
        lines.append("| arm | calls | prompt tok | completion tok | USD | seconds |")
        lines.append("|---|---|---|---|---|---|")
        # the B workers report their own deltas (per trial) — fold them into the per-arm totals
        totals: dict[str, dict[str, float]] = {}
        for a, d in u.items():
            if a == "_B_ledger":
                for key, dd in d.items():
                    arm = key.split("#")[0]
                    t = totals.setdefault(arm, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "seconds": 0.0})
                    for k in t:
                        t[k] += float(dd.get(k) or 0)
            elif isinstance(d, dict) and "calls" in d:
                t = totals.setdefault(a, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "seconds": 0.0})
                for k in t:
                    t[k] += float(d.get(k) or 0)
        for a, d in totals.items():
            lines.append(f"| {a} | {int(d['calls'])} | {int(d['prompt_tokens'])} | {int(d['completion_tokens'])} | {d['cost_usd']:.2f} | {int(d['seconds'])} |")
    text = "\n".join(lines) + "\n"
    (outdir / "SUMMARY.md").write_text(text)
    print(text)


# --------------------------------------------------------------------------------------------
def _append(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    # persist usage alongside (merge across processes: last writer wins per arm, so B workers
    # return their usage inside the record instead)
    up = path.parent / "usage.json"
    try:
        cur = json.loads(up.read_text()) if up.exists() else {}
    except json.JSONDecodeError:
        cur = {}
    for arm, d in USAGE.by_arm.items():
        cur[arm] = d
    if rec.get("usage"):
        # a subprocess trial reports its own totals; accumulate them under a per-key ledger
        led = cur.setdefault("_B_ledger", {})
        led[rec["key"]] = rec["usage"]
    up.write_text(json.dumps(cur, indent=1))


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _done_keys(path: Path) -> set[str]:
    return {r["key"] for r in _read(path) if not r.get("error")}


def main() -> None:
    _load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["prep", "A", "B", "C", "judge", "report"])
    ap.add_argument("--arms", nargs="*", default=list(ARMS))
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--steps", default="", help="B only: comma list of step numbers (default all 7)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tag", default="", help="suffix appended to the arm name in stage-A records, so "
                    "a re-run under CHANGED scaffolding (edited SKILL.md, new prompt) gets its own "
                    "keys instead of being deduped away against the pre-change plans")
    ap.add_argument("--max-steps", dest="max_steps", type=int, default=None,
                    help="B only: tool-call budget per step (default = production's 8)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--run-state", dest="run_state", required=True, help="run 8847's artifacts/process/run_state.json")
    ap.add_argument("--dataset", required=True, help="local Ddx41_DEG.h5ad")
    args = ap.parse_args()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    inp = load_inputs(args)
    {"prep": stage_prep, "A": stage_A, "B": stage_B, "C": stage_C, "judge": stage_judge,
     "report": stage_report}[args.stage](args, inp)


if __name__ == "__main__":
    main()
