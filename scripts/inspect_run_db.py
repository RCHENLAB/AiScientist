#!/usr/bin/env python3
"""Print what the app database holds for one research run. Read-only; prints no credentials.

Runs ON the gateway host as the service account, which can read the deployment's ``.env``
(``AISCIENTIST_DATABASE_URL``). ``scripts/inspect_run.py`` calls it for you; by hand:

    cd /data/aiscientist/app
    sudo -n -u aiscientist env PYTHONPATH=src /data/aiscientist/env/bin/python \\
        scripts/inspect_run_db.py <run_id> [--llm-jsonl | --messages-json]

Default output: the ``runs`` row, its conversation, that conversation's messages (role, kind,
size, first words), and every LLM call of the run (``llm_calls``) summed by role, followed by the
calls in time order. ``--llm-jsonl`` prints one JSON line per call instead (to draw a timeline);
``--messages-json`` prints the conversation and its messages in full.

The queries are plain SQL that PostgreSQL (production) and SQLite (local development) both accept.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine


def database_url() -> str:
    """The deployment's database, the same way the gateway finds it (``.env`` in the app dir)."""
    try:
        from aiscientist.core.config import load_project_env

        load_project_env()
    except Exception:  # noqa: BLE001 - run without the package: rely on the environment alone
        pass
    url = os.environ.get("AISCIENTIST_DATABASE_URL") or os.environ.get("BIOAGENT_DATABASE_URL")
    if url:
        return url
    from aiscientist.gateway.db import database_url as gateway_default

    return gateway_default()


def _one_line(text_: str | None, width: int) -> str:
    return " ".join((text_ or "").split())[:width]


def run_row(engine: Engine, run_id: str) -> dict[str, Any] | None:
    with engine.connect() as c:
        row = c.execute(text(
            "select id, user_id, run_id, conversation_id, status, plan_mode, artifacts_path, question, "
            "created_at, finished_at, length(coalesce(summary, '')) as summary_chars "
            "from runs where run_id = :r"), {"r": run_id}).mappings().first()
    return dict(row) if row else None


def conversation(engine: Engine, conversation_id: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if conversation_id in (None, ""):
        return None, []
    with engine.connect() as c:
        conv = c.execute(text(
            "select id, user_id, title, preset_key, created_at, updated_at from conversations "
            "where cast(id as text) = :c"), {"c": str(conversation_id)}).mappings().first()
        msgs = c.execute(text(
            "select seq, role, kind, content, meta, created_at from messages "
            "where cast(conversation_id as text) = :c order by seq, id"), {"c": str(conversation_id)}).mappings().all()
    return (dict(conv) if conv else None), [dict(m) for m in msgs]


def llm_calls(engine: Engine, run_id: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        rows = c.execute(text(
            "select created_at, role, model, endpoint, remote, prompt_tokens, completion_tokens, duration_ms "
            "from llm_calls where run_id = :r order by created_at, id"), {"r": run_id}).mappings().all()
    return [dict(r) for r in rows]


def summary(engine: Engine, run_id: str, *, first_calls: int = 60) -> str:
    out: list[str] = []
    tables = sorted(inspect(engine).get_table_names())
    with engine.connect() as c:
        counts = {t: c.execute(text(f'select count(*) from "{t}"')).scalar() for t in tables}
    out.append("tables: " + ", ".join(f"{t}={n}" for t, n in counts.items()))

    run = run_row(engine, run_id)
    if run is None:
        out.append(f"\nno runs row for {run_id}")
        return "\n".join(out) + "\n"
    out.append("\nruns:")
    out += [f"  {k:16s} {v}" for k, v in run.items()]

    conv, msgs = conversation(engine, run.get("conversation_id"))
    if conv:
        out.append("\nconversation: " + json.dumps({k: str(v) for k, v in conv.items()}, ensure_ascii=False))
        out.append("\nmessages (seq role kind chars time [meta keys] text):")
        for m in msgs:
            try:
                keys = ",".join(sorted(json.loads(m["meta"]).keys())) if m.get("meta") else "-"
            except (TypeError, ValueError):
                keys = "?"
            out.append(f'  {m["seq"]:>3} {m["role"]:9s} {m["kind"]:10s} {len(m["content"] or ""):>6} '
                       f'{str(m["created_at"])[11:19]} [{keys}] {_one_line(m["content"], 64)}')

    calls = llm_calls(engine, run_id)
    out.append(f"\nllm_calls: {len(calls)}" + (f", endpoint = {calls[0]['endpoint']}" if calls else ""))
    if calls:
        agg: dict[str, list[int]] = {}
        for x in calls:
            a = agg.setdefault(x["role"], [0, 0, 0, 0])
            a[0] += 1
            a[1] += x["prompt_tokens"] or 0
            a[2] += x["completion_tokens"] or 0
            a[3] += x["duration_ms"] or 0
        out.append(f"  {'role':10s} {'calls':>5} {'prompt_tok':>10} {'compl_tok':>9} {'seconds':>7}")
        for role, a in sorted(agg.items(), key=lambda kv: -kv[1][0]):
            out.append(f"  {role:10s} {a[0]:>5} {a[1]:>10} {a[2]:>9} {a[3] / 1000:>7.0f}")
        tot = [sum(a[i] for a in agg.values()) for i in range(4)]
        out.append(f"  {'total':10s} {tot[0]:>5} {tot[1]:>10} {tot[2]:>9} {tot[3] / 1000:>7.0f}")
        out.append(f"\nfirst {min(first_calls, len(calls))} calls (end time, role, prompt tokens, completion tokens, ms):")
        for x in calls[:first_calls]:
            out.append(f'  {str(x["created_at"])[11:19]}  {x["role"]:10s} in={x["prompt_tokens"] or 0:>6} '
                       f'out={x["completion_tokens"] or 0:>5} {x["duration_ms"] or 0:>6} ms')
    return "\n".join(out) + "\n"


def llm_jsonl(engine: Engine, run_id: str) -> str:
    return "".join(json.dumps({"t": str(x["created_at"]), "role": x["role"], "in": x["prompt_tokens"],
                               "out": x["completion_tokens"], "ms": x["duration_ms"]}) + "\n"
                   for x in llm_calls(engine, run_id))


def messages_json(engine: Engine, run_id: str) -> str:
    run = run_row(engine, run_id) or {}
    conv, msgs = conversation(engine, run.get("conversation_id"))
    return json.dumps({"conversation": conv, "messages": msgs}, default=str, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--llm-jsonl", action="store_true", help="one JSON line per LLM call")
    mode.add_argument("--messages-json", action="store_true", help="the conversation and its messages, in full")
    parser.add_argument("--first-calls", type=int, default=60, help="calls listed in time order (default 60)")
    args = parser.parse_args(argv)
    engine = create_engine(database_url())
    if args.llm_jsonl:
        sys.stdout.write(llm_jsonl(engine, args.run_id))
    elif args.messages_json:
        sys.stdout.write(messages_json(engine, args.run_id))
    else:
        sys.stdout.write(summary(engine, args.run_id, first_calls=args.first_calls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
