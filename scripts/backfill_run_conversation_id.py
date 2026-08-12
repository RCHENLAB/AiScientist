#!/usr/bin/env python3
"""Backfill ``runs.conversation_id`` for runs that finished BEFORE the column existed.

Historical ``Run`` rows land with ``conversation_id = NULL`` (the column is added by an idempotent
migration in ``db.init_db``; old rows can't be filled at ALTER time). The link is not lost, though —
it lives in the chat history: when a run finishes, the console persists a ``kind="artifacts"``
message into the run's OWN conversation, whose ``meta`` carries ``bundleUrl =
/api/bundle/<owner>/<run_id>`` (and item URLs ``/api/file/<owner>/<run_id>/...``). So we can recover
``run_id -> conversation.id`` by scanning ``messages.meta`` and set it on the matching NULL ``Run``.

This is AUDIT/history value today (the run→conversation routing uses the in-memory
``last_run_by_conversation`` and does not read this column). It becomes functional if/when
``_followup_target`` is taught to fall back to the DB after a gateway restart.

Safe by construction: DRY-RUN by default (prints what it WOULD do); pass ``--commit`` to write. Only
ever fills rows where ``conversation_id IS NULL`` (never overwrites), and skips a run_id that is
referenced from MORE THAN ONE conversation (an ambiguous historical leak) rather than guess.

Run it on the server as the ``bioagent`` service account (same env as the gateway, so it points at the
prod DB):  ``python -m scripts.backfill_run_conversation_id --commit``
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict

# /api/bundle/<owner>/<run_id>  |  /api/artifacts/<owner>/<run_id>/...  |  /api/file/<owner>/<run_id>/...
_RUN_URL = re.compile(r"/api/(?:bundle|artifacts|file)/[^/]+/([^/?#\"]+)")


def _run_ids_in_meta(meta_text: str | None) -> set[str]:
    """Every run_id referenced by a message's ``meta`` JSON (bundleUrl + item URLs)."""
    if not meta_text:
        return set()
    try:
        meta = json.loads(meta_text)
    except (ValueError, TypeError):
        # Fall back to a raw regex over the text — still recovers the run_id from any URL.
        return set(_RUN_URL.findall(meta_text))
    urls: list[str] = []
    if isinstance(meta, dict):
        if meta.get("bundleUrl"):
            urls.append(str(meta["bundleUrl"]))
        for it in meta.get("items") or []:
            if isinstance(it, dict) and it.get("url"):
                urls.append(str(it["url"]))
    out: set[str] = set()
    for u in urls:
        out.update(_RUN_URL.findall(u))
    return out


def _build_run_to_conversation() -> tuple[dict[str, str], dict[str, set[str]]]:
    """Map ``run_id -> str(conversation_id)`` from the chat history. Returns (unambiguous_map,
    ambiguous) where ``ambiguous`` lists run_ids referenced by more than one conversation."""
    from bioagent.gateway.db import session_scope
    from bioagent.gateway.models import Message
    from sqlalchemy import select

    seen: dict[str, set[str]] = defaultdict(set)
    with session_scope() as s:
        for msg in s.scalars(select(Message)):
            if not msg.meta:
                continue
            cid = str(msg.conversation_id)
            for rid in _run_ids_in_meta(msg.meta):
                seen[rid].add(cid)
    unambiguous = {rid: next(iter(cids)) for rid, cids in seen.items() if len(cids) == 1}
    ambiguous = {rid: cids for rid, cids in seen.items() if len(cids) > 1}
    return unambiguous, ambiguous


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill runs.conversation_id from chat history.")
    ap.add_argument("--commit", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args(argv)

    from bioagent.gateway.db import init_db, session_scope
    from bioagent.gateway.models import Run
    from sqlalchemy import select

    init_db()  # ensure the column exists (idempotent) before we read/write it

    run_to_conv, ambiguous = _build_run_to_conversation()
    print(f"Recovered {len(run_to_conv)} run->conversation links from chat history "
          f"({len(ambiguous)} ambiguous run_id(s) skipped).")
    for rid, cids in sorted(ambiguous.items()):
        print(f"  ambiguous (skipped): run {rid} referenced by conversations {sorted(cids)}")

    filled = 0
    missing = 0
    with session_scope() as s:
        null_runs = list(s.scalars(select(Run).where(Run.conversation_id.is_(None))))
        print(f"{len(null_runs)} run(s) have NULL conversation_id.")
        for run in null_runs:
            cid = run_to_conv.get(run.run_id)
            if cid is None:
                missing += 1
                continue
            print(f"  {'SET' if args.commit else 'would set'} run {run.run_id} -> conversation {cid}")
            if args.commit:
                run.conversation_id = cid
            filled += 1
        if args.commit:
            s.commit()

    print(f"\n{'Committed' if args.commit else 'Dry-run'}: {filled} filled, {missing} left NULL "
          f"(no artifacts message references them — nothing to recover).")
    if not args.commit and filled:
        print("Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
