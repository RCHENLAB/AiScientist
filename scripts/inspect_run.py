#!/usr/bin/env python3
"""Collect the record of one research run from the gateway host and HPC3, read-only.

Every command runs once over SSH and is stored WITH its verbatim output, so the record can be
quoted, diffed or typeset later (the 2026-10-12 run handbook was built from such a record):

    python scripts/inspect_run.py --list                 # recent runs on the gateway host
    python scripts/inspect_run.py <run_id>               # snapshot -> run_inspect/<run_id>/
    python scripts/inspect_run.py <run_id> --only sacct events db
    python scripts/inspect_run.py <run_id> --watch 30    # while it runs: progress + queue every 30 s,
                                                         # then a full snapshot when it finishes

What a snapshot holds (one ``<id>.json`` per command, all of them again in ``record.txt``):
    gateway  service, tree, events, progress, plan, rounds, state, report, reviews, db
    HPC3     sacct, queue, workspace, jobfiles, jobscripts, tempgc

Targets (the same names as scripts/sync_deploy.sh; a ``.deploy.env`` in the repository root is read):
    ADMIN_SSH    [eyeserver-admin]  gateway host account that may ``sudo -n -u aiscientist`` (the db step)
    HPC_SSH      [hpc3]             HPC3 login node, key auth; only metadata commands run there
    HPC_USER     [from the run]     HPC3 account whose jobs to list; read from the run's paths when possible
    APP_DIR      [/data/aiscientist/app]     VENV [/data/aiscientist/env]
    SHARED_ROOT  [/dfs3b/ruic20_lab/software/AiScientist]

Nothing is changed on either host. The only write is a temporary copy of ``inspect_run_db.py`` in
``/tmp`` on the gateway when the deployed app does not have it yet; it is removed afterwards. No file
is transferred from HPC3: small text files are read with ``cat``/``head``, which RCIC allows on
login nodes. The record contains the run's question and chat messages, so keep it out of git
(``run_inspect/`` is ignored).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_SCRIPT = Path(__file__).resolve().parent / "inspect_run_db.py"

DEFAULTS = {
    "ADMIN_SSH": "eyeserver-admin",
    "HPC_SSH": "hpc3",
    "APP_DIR": "/data/aiscientist/app",
    "VENV": "/data/aiscientist/env",
    "SHARED_ROOT": "/dfs3b/ruic20_lab/software/AiScientist",
}


# ------------------------------------------------------------------ settings

def parse_deploy_env(text_: str) -> dict[str, str]:
    """``KEY=VALUE`` lines of a .deploy.env (comments, blanks and ``export`` allowed)."""
    out: dict[str, str] = {}
    for line in text_.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.split(" #", 1)[0].strip().strip("'\"")
        if key.strip():
            out[key.strip()] = value
    return out


def settings() -> dict[str, str]:
    file_env = {}
    deploy_env = ROOT / ".deploy.env"
    if deploy_env.exists():
        file_env = parse_deploy_env(deploy_env.read_text(encoding="utf-8"))
    return {k: os.environ.get(k) or file_env.get(k) or v for k, v in DEFAULTS.items()} | {
        "HPC_USER": os.environ.get("HPC_USER") or file_env.get("HPC_USER", "")}


# ------------------------------------------------------------------ ssh

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
            "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/inspect-run-%C", "-o", "ControlPersist=120"]


def ssh(host: str, script: str, timeout: int = 180) -> tuple[str, int]:
    """Run ``script`` with bash on ``host``; stdout and stderr together, as a terminal shows them."""
    try:
        p = subprocess.run(["ssh", *SSH_OPTS, host, "bash -s"], input=script, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout} s)", 124
    out = p.stdout
    if p.stderr.strip():
        out += ("\n" if out and not out.endswith("\n") else "") + p.stderr
    return out.rstrip("\n"), p.returncode


# ------------------------------------------------------------------ what to collect

PROBE = r"""
import glob, json, os, time
app, rid = os.environ["APP_DIR"], os.environ["RUN_ID"]
hits = sorted(glob.glob(f"{app}/runs/console/*/{rid}"))
if not hits:
    print(json.dumps({"found": False})); raise SystemExit
d = hits[0]
info = {"found": True, "run_dir": d, "account": d.rstrip("/").split("/")[-2]}
st = os.path.join(d, "artifacts/process/run_state.json")
if os.path.exists(st):
    s = json.load(open(st))
    for k in ("question", "hpc_primary", "hpc_dataset_root", "converged", "accepted_steps"):
        info[k] = s.get(k)
    info["rounds"] = len(s.get("rounds") or [])
first = None
lp = os.path.join(d, "loop.log")
if os.path.exists(lp):
    for line in open(lp):
        try:
            first = json.loads(line)["time"]; break
        except Exception:
            pass
mt = [os.path.getmtime(p) for p in glob.glob(d + "/**", recursive=True) if os.path.isfile(p)]
starts = [t for t in (first, min(mt) if mt else None) if t]
info["start"] = min(starts) if starts else time.time()
info["last_write"] = max(mt) if mt else None
info["finished"] = os.path.exists(os.path.join(d, "artifacts/process/event_log.txt"))
info["now"] = time.time()
fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
end = (info["last_write"] if info["finished"] and info["last_write"] else info["now"]) + 300
info["window"] = [fmt(info["start"] - 300), fmt(end)]
print(json.dumps(info))
"""

LIST = r"""
import glob, json, os, time
app = os.environ["APP_DIR"]
dirs = [d for d in glob.glob(f"{app}/runs/console/*/*") if os.path.isdir(d) and os.path.basename(d)[:1] not in "_."
        and os.path.basename(d) != "uploads"]
dirs.sort(key=os.path.getmtime, reverse=True)
for d in dirs[:int(os.environ.get("N", "15"))]:
    q, state = "", "running" if not os.path.exists(f"{d}/artifacts/process/event_log.txt") else "finished"
    try:
        q = json.load(open(f"{d}/artifacts/process/run_state.json")).get("question", "")
    except Exception:
        pass
    print(f'{time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(d)))}  {d.split("/")[-2]:12s} '
          f'{os.path.basename(d)}  {state:8s}  {" ".join(q.split())[:70]}')
"""

ROUNDS = r"""python3 - <<'EOF'
import glob, json, os
files = sorted(glob.glob("artifacts/process/round_*.json"))
rounds = [json.load(open(f)) for f in files]
if not rounds and os.path.exists("artifacts/process/run_state.json"):
    rounds = json.load(open("artifacts/process/run_state.json")).get("rounds") or []
for r in rounds:
    s, v = r.get("scientist_result") or {}, r.get("verdict") or {}
    tools = [(x.get("tool"), "ok" if x.get("ok") else "ERR") for x in s.get("steps") or []]
    print(f'round {r.get("round_no")}  step {r.get("step_index")}  {r.get("specialist")}')
    print(f'    stop_reason={s.get("stop_reason")}  status={s.get("status")}  tools={tools}')
    print(f'    verdict={str(v.get("verdict")).upper()}  score={v.get("score")}  step="{(r.get("step") or "")[:70]}..."')
    print(f'    critique: {" ".join((v.get("critique") or "").split())[:400]}')
print(f"{len(rounds)} round(s)")
EOF"""

STATE = r"""python3 - <<'EOF'
import json, os
p = "artifacts/process/run_state.json"
if not os.path.exists(p):
    print("(run_state.json is written after the first step finishes)"); raise SystemExit
s = json.load(open(p))
print("question:", s.get("question"))
print("converged:", s.get("converged"), "| accepted_steps:", s.get("accepted_steps"), "| rounds:", len(s.get("rounds") or []))
print("execution:", json.dumps(s.get("execution")))
for i, a in enumerate(s.get("agenda") or [], 1):
    print(f"step {i}: {' '.join(a.split())[:160]}")
for h in s.get("hypotheses") or []:
    print(f'hypothesis {h.get("id")} [{h.get("status")}]: {" ".join(str(h.get("statement")).split())[:200]}')
for c in (s.get("claim_audit") or {}).get("claims") or []:
    print(f'claim [{c.get("status")}] step {c.get("step")}: {" ".join(str(c.get("claim")).split())[:150]}')
EOF"""


def hpc_user_from(info: dict, fallback: str = "") -> str:
    for key in ("hpc_primary", "hpc_dataset_root"):
        m = re.search(r"/(?:uploads|Temp)/([^/]+)/", str(info.get(key) or ""))
        if m:
            return m.group(1)
    return fallback


def build_captures(run_id: str, info: dict, user: str, cfg: dict[str, str], db_script: str) -> list[tuple[str, str, str, str]]:
    """(id, target, cwd, command) for every capture. ``target`` is "gateway" or "hpc"."""
    app, venv, shared, run_dir = cfg["APP_DIR"], cfg["VENV"], cfg["SHARED_ROOT"], info["run_dir"]
    start, end = info["window"]
    temp = f"{shared}/Temp/{user}"
    q = shlex.quote
    caps = [
        ("service", "gateway", "~", "systemctl show aiscientist.service -p ActiveState -p SubState -p ActiveEnterTimestamp\n"
                                     f"cat {q(app)}/.deployed_sha"),
        ("tree", "gateway", run_dir, 'find . -type f -printf "%10s  %TY-%Tm-%Td %TH:%TM  %p\\n" | sort -k4'),
        ("events", "gateway", run_dir, 'cat artifacts/process/event_log.txt 2>/dev/null || echo "(event_log.txt is written when the run ends; see progress)"'),
        ("progress", "gateway", run_dir, 'tail -n 40 loop.log 2>/dev/null || echo "(no loop.log yet: the first step has not started)"'),
        ("plan", "gateway", run_dir, 'cat artifacts/report/plan.md 2>/dev/null || echo "(plan.md is written when the run ends; see state)"'),
        ("rounds", "gateway", run_dir, ROUNDS),
        ("state", "gateway", run_dir, STATE),
        ("report", "gateway", run_dir, "ls -la artifacts/report 2>/dev/null\nhead -n 12 artifacts/report/verification.md 2>/dev/null"),
        ("reviews", "gateway", run_dir, "cat artifacts/process/report_review.md artifacts/process/visual_review_pass*.json "
                                         "artifacts/process/capabilities.log 2>/dev/null; true"),
        ("db", "gateway", app, f"sudo -n -u aiscientist env PYTHONPATH=src {q(venv)}/bin/python {q(db_script)} {q(run_id)}"),
        ("sacct", "hpc", "~", f"sacct -u {q(user)} -S {start.replace(' ', 'T')} -E {end.replace(' ', 'T')} -X "
                               "--format=JobID%9,JobName%33,Partition%10,AllocCPUS%5,ReqMem%6,State%10,Start%19,Elapsed%8,NodeList%15"),
        ("queue", "hpc", "~", f'squeue -u {q(user)} -o "%.9i %.30j %.9P %.8T %.10M %.5C %.6m %R"'),
        ("workspace", "hpc", temp, "for k in analysis variant phenotype scgpt vlreview; do\n"
                                   f'  [ -d "$k/{run_id}" ] && find "$k/{run_id}" -maxdepth 3 -printf "%y %12s  %TY-%Tm-%Td %TH:%TM  %p\\n" | sort -k5\n'
                                   "done; true"),
        ("jobfiles", "hpc", "~", f"find ~/.bioagent/analysis ~/.bioagent/report ~/.bioagent/jobs {q(temp)}/scratch -maxdepth 2 -type f "
                                 f'-newermt {q(start)} ! -newermt {q(end)} -printf "%TY-%Tm-%Td %TH:%TM %10s  %p\\n" 2>/dev/null | sort'),
        ("jobscripts", "hpc", "~", f"find ~/.bioagent/analysis ~/.bioagent/report ~/.bioagent/jobs {q(temp)}/scratch -maxdepth 2 -type f "
                                   f"-name '*.sbatch' -newermt {q(start)} ! -newermt {q(end)} 2>/dev/null | sort | while read -r f; do\n"
                                   '  echo "===== $f"; cat "$f"\n'
                                   '  for x in "${f%.sbatch}.args.json" "${f%.sbatch}.rc" "${f%.sbatch}.err"; do\n'
                                   '    [ -s "$x" ] && { echo "----- $x"; head -c 2000 "$x"; echo; }\n'
                                   "  done\n"
                                   "done | head -c 300000"),
        ("tempgc", "hpc", "~", f"tail -n 3 {q(shared)}/bin/{q(user)}/temp_gc.log 2>/dev/null"),
    ]
    return caps


# ------------------------------------------------------------------ modes

def probe(cfg: dict[str, str], run_id: str) -> dict:
    script = f"export APP_DIR={shlex.quote(cfg['APP_DIR'])} RUN_ID={shlex.quote(run_id)}\npython3 - <<'EOF'\n{PROBE}\nEOF\n"
    out, rc = ssh(cfg["ADMIN_SSH"], script, timeout=60)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise SystemExit(f"could not read the run on {cfg['ADMIN_SSH']} (rc={rc}):\n{out}")


def snapshot(cfg: dict[str, str], run_id: str, info: dict, user: str, out_dir: Path, only: set[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    gw, hpc = cfg["ADMIN_SSH"], cfg["HPC_SSH"]
    deployed, _ = ssh(gw, f"test -f {shlex.quote(cfg['APP_DIR'])}/scripts/inspect_run_db.py && echo yes")
    db_script, temp_copy = "scripts/inspect_run_db.py", None
    caps = build_captures(run_id, info, user, cfg, db_script)
    wanted = [c for c in caps if not only or c[0] in only]
    if any(c[0] == "db" for c in wanted) and deployed.strip() != "yes":
        temp_copy = f"/tmp/inspect_run_db.{os.getpid()}.py"
        subprocess.run(["scp", "-q", *SSH_OPTS, str(DB_SCRIPT), f"{gw}:{temp_copy}"], check=True)
        wanted = [(i, t, cwd, cmd.replace("scripts/inspect_run_db.py", temp_copy)) for i, t, cwd, cmd in wanted]
    record = []
    try:
        for cid, target, cwd, cmd in wanted:
            host = gw if target == "gateway" else hpc
            out, rc = ssh(host, f"cd {cwd} || exit 97\n{cmd}\n")
            rec = {"id": cid, "host": host, "cwd": cwd, "cmd": cmd, "output": out, "rc": rc,
                   "taken_at": time.strftime("%Y-%m-%d %H:%M:%S %Z")}
            (out_dir / f"{cid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            record.append(f"### {cid}  [{host}:{cwd}]  rc={rc}  {rec['taken_at']}\n$ {cmd}\n{out}\n")
            print(f"{cid:11s} rc={rc:<3} {out.count(chr(10)) + 1 if out else 0:>5} lines  ({host})")
    finally:
        if temp_copy:
            ssh(gw, f"rm -f {temp_copy}")
    (out_dir / "record.txt").write_text("\n".join(record), encoding="utf-8")
    (out_dir / "index.json").write_text(json.dumps({"run_id": run_id, "hpc_user": user, **info}, indent=1, default=str),
                                        encoding="utf-8")
    print(f"\nwrote {out_dir}/ ({len(record)} captures; record.txt has them all)")


WATCH_GW = r"""cd {run_dir} || exit 97
tail -n 1 loop.log 2>/dev/null
python3 - <<'EOF'
import glob, json, os
rs = sorted(glob.glob("artifacts/process/round_*.json"))
if os.path.exists("artifacts/process/run_state.json"):
    s = json.load(open("artifacts/process/run_state.json")); r = s.get("rounds") or []
    last = r[-1] if r else {}
    v = last.get("verdict") or {}
    print(f'rounds={len(r)} accepted={s.get("accepted_steps")} last: step {last.get("step_index")} '
          f'{str(v.get("verdict")).upper()} {v.get("score")}')
print("FINISHED" if os.path.exists("artifacts/process/event_log.txt") else "running")
EOF"""


def watch(cfg: dict[str, str], run_id: str, info: dict, user: str, out_dir: Path, every: int, then_snapshot: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "watch.log"
    print(f"watching {run_id} every {every} s (Ctrl-C to stop); also appended to {log}")
    while True:
        stamp = time.strftime("%H:%M:%S")
        g, grc = ssh(cfg["ADMIN_SSH"], WATCH_GW.format(run_dir=shlex.quote(info["run_dir"])), timeout=60)
        h, hrc = ssh(cfg["HPC_SSH"], f'squeue -u {shlex.quote(user)} -h -o "%.9i %.30j %.8T %.10M %R"', timeout=60)
        block = f"--- {stamp}\n{g if grc == 0 else '(gateway unreachable: ' + g[-200:] + ')'}\n" \
                f"{h if hrc == 0 else '(HPC3 unreachable: ' + h[-200:] + ')'}\n"
        print(block, end="", flush=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(block)
        if grc == 0 and g.rstrip().endswith("FINISHED"):
            break
        time.sleep(every)
    if then_snapshot:
        snapshot(cfg, run_id, probe(cfg, run_id), user, out_dir, set())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("run_id", nargs="?", help="12-hex run id (see --list)")
    parser.add_argument("--list", action="store_true", help="list recent runs on the gateway host")
    parser.add_argument("--only", nargs="+", default=[], metavar="ID", help="collect only these captures")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="poll progress and the queue until the run ends")
    parser.add_argument("--no-snapshot", action="store_true", help="with --watch: do not take a snapshot at the end")
    parser.add_argument("--hpc-user", default="", help="HPC3 account (default: from the run's paths, then HPC_USER)")
    parser.add_argument("--out", type=Path, help="output directory (default: run_inspect/<run_id>)")
    args = parser.parse_args(argv)
    cfg = settings()

    if args.list:
        out, rc = ssh(cfg["ADMIN_SSH"], f"export APP_DIR={shlex.quote(cfg['APP_DIR'])}\npython3 - <<'EOF'\n{LIST}\nEOF\n")
        print(out)
        return rc
    if not args.run_id:
        parser.error("a run id is required (or --list)")
    info = probe(cfg, args.run_id)
    if not info.get("found"):
        print(f"run {args.run_id} not found under {cfg['APP_DIR']}/runs/console on {cfg['ADMIN_SSH']}", file=sys.stderr)
        return 2
    user = args.hpc_user or hpc_user_from(info, cfg["HPC_USER"])
    if not user:
        user = ssh(cfg["HPC_SSH"], "id -un")[0].strip()
    out_dir = args.out or Path("run_inspect") / args.run_id
    print(f"run {args.run_id}: account {info['account']}, HPC3 user {user}, "
          f"window {info['window'][0]} .. {info['window'][1]}, {'finished' if info['finished'] else 'running'}")
    if args.watch:
        watch(cfg, args.run_id, info, user, out_dir, args.watch, not args.no_snapshot)
    else:
        snapshot(cfg, args.run_id, info, user, out_dir, set(args.only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
