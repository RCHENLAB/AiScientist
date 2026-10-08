"""Headless end-to-end drive of a DEPLOYED gateway — the browser's own HTTP + WebSocket surface.

Why this exists: two runs of it against prod found nine defects that 1,500+ green unit tests
could not — a DE step routed to the literature fast path, six analysis tools running in-process
where their HPC3 checkpoints did not exist, an empty "What was run" section, and a literature tool
that had never once succeeded in production. Unit tests build the shapes they expect; this drives
the real service, with a real HPC3 session, a real GPU job and real data, and then reads the run's
own process files. Run it after any change to planning, routing, offload or reporting.

Usage (from the eyeserver's point of view the key path is SERVER-side — the gateway loads it):
    .venv/bin/python scripts/e2e_prod_drive.py \
        --base "$AISCIENTIST_BASE" --user "$AISCIENTIST_USER" \
        --key /data/BioAgent/app/ssh_creds/<owner>/<id>.key \
        --dataset /dfs3b/ruic20_lab/software/AiScientist/uploads/<user>/Ddx41_DEG.h5ad \
        [--question "..."] [--plan-only] [--exercise-plan-mode]

With --record DIR every request the drive sends (the first prompt included), every event the gateway
streams back, the plan card and the PASS/FAIL lines are written there as they happen, so a run can
be audited from the first prompt to the report.

Phases: connect -> ready | ask (plan mode) -> plan card | [question reply -> plan unchanged;
change reply -> one-step patch; Stop while pending -> immediate cancel] | approve -> run to
completion | verify: composition offloaded (hpc_slurm), DE step ran run_de, report sections.
Prints PASS/FAIL/WARN lines and exits non-zero on any FAIL. Needs `pip install websocket-client`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    print("pip install websocket-client", file=sys.stderr)
    raise


class Drive:
    def __init__(self, base: str, record: "str | None" = None) -> None:
        self.base = base.rstrip("/")
        self.ws_base = self.base.replace("http", "ws", 1)
        self.events: list[tuple[float, dict]] = []
        self.lock = threading.Lock()
        self.fails = 0
        self.record = None
        if record:
            from pathlib import Path
            self.record = Path(record)
            self.record.mkdir(parents=True, exist_ok=True)

    def _log(self, name: str, obj: dict) -> None:
        """Append one JSON line to ``<record>/<name>.jsonl`` (no-op without --record)."""
        if self.record is None:
            return
        with (self.record / f"{name}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), **obj}, ensure_ascii=False, default=str) + "\n")

    # --- transport ------------------------------------------------------------------------
    def post(self, path: str, body: dict) -> tuple[int, dict]:
        self._log("requests", {"path": path, "body": {k: v for k, v in body.items() if k != "key_path"}})
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=60) as r:
            return json.loads(r.read())

    def listen(self, cid: str) -> None:
        def on_msg(_w, m):
            try:
                ev = json.loads(m)
            except Exception:  # noqa: BLE001
                return
            with self.lock:
                self.events.append((time.time(), ev))
                self._log("events", {"event": ev})
        threading.Thread(target=lambda: websocket.WebSocketApp(
            f"{self.ws_base}/ws/{cid}", on_message=on_msg).run_forever(ping_interval=20),
            daemon=True).start()

    def wait(self, pred, timeout: float, since: int = 0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.lock:
                for i in range(since, len(self.events)):
                    if pred(self.events[i][1]):
                        return i, self.events[i]
            time.sleep(1)
        return None, None

    # --- reporting ------------------------------------------------------------------------
    def check(self, ok: bool | None, msg: str) -> None:
        tag = "PASS " if ok else ("WARN " if ok is None else "FAIL ")
        if ok is False:
            self.fails += 1
        print(f"{tag} {msg}", flush=True)
        self._log("checks", {"result": tag.strip(), "message": msg})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--key", required=True, help="SERVER-side path to the SSH key")
    ap.add_argument("--dataset", default=None, help="dfs3b path (uploads/, never Temp/)")
    ap.add_argument("--server-path", default=None,
                    help="instead of --dataset: a path on the gateway host inside "
                         "AISCIENTIST_SERVER_DATA_ROOTS, bound the way the console binds it")
    ap.add_argument("--question", default="What changes between DDX41 mutant and WT retina?")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--exercise-plan-mode", action="store_true",
                    help="after the plan: ask a question, request a one-step change, Stop")
    ap.add_argument("--conv", default="e2e")
    ap.add_argument("--continue-run", default=None,
                    help="instead of a new study: redo step --from-step of this run_id and everything after "
                         "it (POST /api/lab/continue), reusing the earlier steps' checkpoints")
    ap.add_argument("--from-step", type=int, default=0, help="0-based agenda step to redo (--continue-run)")
    ap.add_argument("--modify-note", default=None, help="steering for the redone step (--continue-run)")
    ap.add_argument("--resume-interrupted", action="store_true",
                    help="with --continue-run: pick up a run that crashed or was cut off where it stopped "
                         "(keeps every accepted step, runs the rest; --from-step is ignored)")
    ap.add_argument("--record", default=None,
                    help="directory to write requests/events/checks (JSONL) and the plan into")
    # Qwen3.8 at xhigh effort took 38 min to produce a plan card on 2026-09-30 (team research, then
    # one ~35k-token PI call); the old fixed 25 min gave up while the plan was still being written.
    ap.add_argument("--plan-timeout", type=float, default=3600, help="seconds to wait for the plan card")
    ap.add_argument("--run-timeout", type=float, default=6 * 3600, help="seconds to wait for the run")
    a = ap.parse_args()
    if not (a.dataset or a.server_path or a.continue_run):
        ap.error("give --dataset or --server-path")
    d = Drive(a.base, record=a.record)

    st, r = d.post("/api/connect", {"ucinetid": a.user, "auth_method": "ssh_key",
                                    "key_path": a.key, "campus_network_confirmed": True})
    print("connect ->", st, r.get("connection_id"), r.get("error"))
    if st != 200:
        return 1
    cid = r["connection_id"]
    d.listen(cid)
    t0 = time.time()
    while True:
        s = d.get(f"/api/connections/{cid}")
        if s.get("status") == "ready":
            break
        if s.get("status") in ("error", "disconnected") or time.time() - t0 > 2400:
            d.check(False, f"connect ended in {s.get('status')}")
            return 1
        time.sleep(15)
    d.check(True, f"ready in {time.time()-t0:.0f}s on {(s.get('gpu') or {}).get('node')}")

    if a.continue_run:
        return _continue(d, cid, a)
    if a.server_path:
        st, info = d.post("/api/server-data/check", {"connection_id": cid, "path": a.server_path})
        d.check(st == 200, f"server path check: {info.get('message') or info.get('error')}")
        if st != 200:
            return 1
        st, bound = d.post("/api/server-data/bind", {"connection_id": cid, "path": a.server_path})
        d.check(st == 200 and bound.get("kind", "").startswith("server-"),
                f"server path bound as {bound.get('kind')}: {bound.get('path')}")
        if st != 200:
            return 1
        a.dataset = bound["path"]

    def lab(q, conv):
        return d.post("/api/lab", {"connection_id": cid, "conversation_id": conv, "question": q,
                                   "dataset_path": a.dataset, "plan_mode": True,
                                   "autonomous": False, "planner": "dag", "route": "research",
                                   "history": [], "mode": "auto", "presets": [], "skills": [],
                                   "preset_prompt": None})

    def plan(conv, action, fb=""):
        return d.post("/api/lab/plan", {"connection_id": cid, "conversation_id": conv,
                                        "action": action, "feedback": fb})

    tq = time.time()
    st, r = lab(a.question, a.conv)
    print("lab ->", st, r)
    i, ev = d.wait(lambda e: e.get("type") in ("plan_prompt", "chat_error"), a.plan_timeout)
    if not ev or ev[1].get("type") == "chat_error":
        d.check(False, f"no plan card: {ev[1].get('message') if ev else 'timeout'}")
        return 1
    agenda = list(ev[1].get("agenda") or [])
    d.check(True, f"plan card in {time.time()-tq:.0f}s, {len(agenda)} steps")
    if d.record is not None:
        (d.record / "plan.md").write_text(
            f"# Plan card\n\nQuestion: {a.question}\n\nDataset: {a.dataset}\n\n"
            + "\n".join(f"{n}. {s_}" for n, s_ in enumerate(agenda, 1)) + "\n", encoding="utf-8")
    for n, s_ in enumerate(agenda, 1):
        print(f"   {n}. {s_[:150]}")
    titled = sum(1 for s_ in agenda if re.match(r"^\*\*.+?\*\*\s*[—–:-]", s_))
    d.check(titled == len(agenda), f"titled steps {titled}/{len(agenda)}")
    d.check(any("stratif" in s_.lower() and "majorclass" in s_ for s_ in agenda) or None,
            "DDX41-vs-WT stratified by majorclass mentioned")
    pre = sum(1 for t, e in d.events if t < ev[0] and e.get("type") == "lab_progress")
    d.check(pre >= 3, f"{pre} progress events before the plan card (silence check)")

    if a.exercise_plan_mode:
        n0 = len(d.events)
        plan(a.conv, "revise", "第3步为什么要去掉低质量细胞?")
        j, pp = d.wait(lambda e: e.get("type") == "plan_prompt", 300, since=n0)
        d.check(bool(pp) and pp[1].get("agenda") == agenda, "question reply: plan unchanged")
        n0 = len(d.events)
        plan(a.conv, "revise", "第 1 步的线粒体阈值改成 5%")
        j, pp = d.wait(lambda e: e.get("type") == "plan_prompt", 300, since=n0)
        if pp:
            new = list(pp[1].get("agenda") or [])
            diff = [k for k, (x, y) in enumerate(zip(agenda, new)) if x != y]
            d.check(len(diff) == 1 and len(new) == len(agenda),
                    f"change reply: {len(diff)} step(s) differ ({len(agenda)}->{len(new)})")
            agenda = new
        n0 = len(d.events)
        ts = time.time()
        d.post("/api/chat/stop", {"connection_id": cid, "conversation_id": a.conv})
        j, done = d.wait(lambda e: e.get("type") in ("plan_done", "chat_stopped", "run_cancelled"),
                         60, since=n0)
        d.check(bool(done), f"Stop while plan pending settled in {time.time()-ts:.1f}s")
        return 1 if d.fails else 0

    if a.plan_only:
        plan(a.conv, "cancel")
        return 1 if d.fails else 0

    n0 = len(d.events)
    st, r = plan(a.conv, "approve")
    print("approve ->", st, r)
    t1 = time.time()
    # A manual-mode run can pause mid-way on a decision point (e.g. "use the existing labels or
    # re-cluster?"). Nobody answers in a headless drive, so each one used to cost its full 600 s
    # timeout (2026-09-30, run f107bcf7b660). Answer with the first option and say so.
    since = n0
    while True:
        left = a.run_timeout - (time.time() - t1)
        j, done = d.wait(lambda e: e.get("type") in ("run_complete", "chat_done", "chat_error",
                                                     "decision_prompt"), max(left, 1), since=since)
        if not done or done[1].get("type") != "decision_prompt":
            break
        options = list(done[1].get("options") or [])
        pick = options[0] if options else ""
        print(f"decision: {str(done[1].get('goal', ''))[:100]!r} -> {pick!r}", flush=True)
        plan(a.conv, "approve", pick)
        since = j + 1
    d.check(bool(done) and done[1].get("type") != "chat_error",
            f"run finished: {done[1].get('type') if done else 'TIMEOUT'} in {(time.time()-t1)/60:.1f} min")
    if a.server_path:
        lines = [e.get("text", "") for _, e in d.events if e.get("type") == "lab_progress"]
        d.check(any(t.startswith("🔗 Server data:") for t in lines), "the run admitted the server path")
        d.check(any(t.startswith("✓ Copied") or "is up to date" in t for t in lines),
                "the server data reached HPC3: " + next(
                    (t for t in lines if t.startswith("✓ ")), "no copy line"))
    return 1 if d.fails else 0


def _continue(d: "Drive", cid: str, a: "argparse.Namespace") -> int:
    """Redo one step of an existing run and everything downstream (or, with --resume-interrupted,
    finish an interrupted run), then wait for the report."""
    t1 = time.time()
    n0 = len(d.events)
    st, r = d.post("/api/lab/continue", {"connection_id": cid, "conversation_id": a.conv,
                                          "run_id": a.continue_run, "from_step_index": a.from_step,
                                          "modify_note": a.modify_note,
                                          "resume_interrupted": a.resume_interrupted})
    print("continue ->", st, r, flush=True)
    if st != 200:
        d.check(False, f"continue refused: {r.get('error')}")
        return 1
    j, done = d.wait(lambda e: e.get("type") in ("run_complete", "chat_done", "chat_error"),
                     a.run_timeout, since=n0)
    d.check(bool(done) and done[1].get("type") != "chat_error",
            f"continued run finished: {done[1].get('type') if done else 'TIMEOUT'} in "
            f"{(time.time()-t1)/60:.1f} min")
    return 1 if d.fails else 0


if __name__ == "__main__":
    sys.exit(main())
