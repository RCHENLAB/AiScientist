"""Skill requests: runs that needed code the model wrote, kept for the team (issue 13).

A run whose steps called ``run_code``, ``run_shell`` or ``run_in_environment`` asked for something no
tested tool did. Instead of refusing such a request, the run goes ahead, the reply says which results
came from untested code (``agents/code_provenance.py``), and the run leaves a record here: the
request, the datasets' metadata (never the data), the pipelines that steered the plan and the vetted
skills its code ran under, every code attempt with its outcome and the Critic's verdict, and a
structured summary. Admins read the records
in Admin → Skill requests, grouped by category and closest skill, and decide what to build.

Storage: one JSON file per run, ``<AISCIENTIST_STATE_DIR>/skill_requests/<run id>.json``, mode 0600.
Only the admin routes read it. Credentials, server paths, e-mail addresses and identifier-like numbers
are scrubbed before anything is written. After ``AISCIENTIST_SKILL_REQUEST_RETENTION_DAYS`` (365 by
default) the raw parts — the request text, the earlier turns and the code — are dropped; the summary,
the counts and the status stay. ``AISCIENTIST_SKILL_REQUESTS=0`` turns recording off, and without
``AISCIENTIST_STATE_DIR`` (a laptop) nothing is recorded: there is no private place to keep it.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..agents.loop_utils import safe_json_loads
from ..metadata.facts import scrub_text
from .errors import redact_key_store_paths, scrub_secrets

STATUSES = ("new", "planned", "in_progress", "released", "wont_do")

CATEGORIES = (
    "quality control", "normalization", "integration", "clustering", "cell-type annotation",
    "differential expression", "enrichment", "composition", "trajectory analysis",
    "cell-cell communication", "gene signature scoring", "perturbation analysis",
    "variant annotation", "phenotype and diagnosis", "literature", "visualization", "other",
)

DEFAULT_RETENTION_DAYS = 365

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Beyond the key-shaped tokens (errors.scrub_secrets) and scrub_text's credentials and paths: keys
# and tokens written as assignments, e-mail addresses, and the identifier shapes a clinical note
# carries (SSN, MRN). A heuristic, not a guarantee: the records stay admin-only either way.
_EXTRA_SCRUB = (
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\b(\s*[:=]\s*)(['\"]?)[^\s'\",)]+"),
     r"\1\2\3<redacted>"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,})\b"),
     "<redacted>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "<id>"),
    (re.compile(r"(?i)\b(MRN|medical record(?: number)?)\b[\s#:]*[A-Z0-9-]{4,}"), r"\1 <id>"),
)


def enabled() -> bool:
    if not (os.environ.get("AISCIENTIST_STATE_DIR") or "").strip():
        return False
    return (os.environ.get("AISCIENTIST_SKILL_REQUESTS") or "1").strip().lower() not in (
        "0", "false", "no", "off")


def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("AISCIENTIST_SKILL_REQUEST_RETENTION_DAYS") or
                          DEFAULT_RETENTION_DAYS))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def requests_dir() -> Path:
    return Path(os.environ.get("AISCIENTIST_STATE_DIR", ".")) / "skill_requests"


def scrub(text: Any) -> str:
    # A key-store path first and whole: scrub_text keeps a path's last part, and a key file's name
    # is its credential's id.
    out = scrub_text(scrub_secrets(redact_key_store_paths(str(text or ""))))
    for pattern, repl in _EXTRA_SCRUB:
        out = pattern.sub(repl, out)
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


# --- building a record ---------------------------------------------------------------------------


def _dataset_meta(datasets: "list[dict[str, Any]]") -> "list[dict[str, Any]]":
    """What a record keeps of each bound input: its file name and what kind of data it is."""
    keep = ("kind", "role", "format", "modality", "assay", "organism", "size_bytes", "n_files",
            "content_modality")
    out = []
    for d in datasets or []:
        if not isinstance(d, dict):
            continue
        name = Path(str(d.get("name") or d.get("filename") or d.get("path") or "")).name
        row = {"name": scrub(name)} if name else {}
        row.update({k: d[k] for k in keep if d.get(k) not in (None, "", [], {})})
        if row:
            out.append(row)
    return out[:20]


def _scrub_steps(steps: "list[dict[str, Any]]") -> "list[dict[str, Any]]":
    out = []
    for s in steps or []:
        calls = []
        for c in s.get("code_calls") or []:
            c = dict(c)
            for key in ("code", "summary", "error"):
                if key in c:
                    c[key] = scrub(c[key])
            if "files" in c:
                c["files"] = [scrub(f) for f in c["files"]]
            calls.append(c)
        out.append({**{k: s[k] for k in ("step", "status", "accepted", "verdict", "attempts", "tools")
                       if k in s},
                    "critique": scrub(s.get("critique", "")), "code_calls": calls})
    return out


def build_record(*, run_id: str, account: str, question: str, provenance: dict[str, Any],
                 conversation_id: str = "", earlier_requests: "list[str] | None" = None,
                 datasets: "list[dict[str, Any]] | None" = None,
                 pipelines: "list[str] | None" = None, skills: "list[str] | None" = None,
                 converged: bool = False,
                 claim_audit: "dict[str, Any] | None" = None,
                 induced_skills: "list[str] | None" = None) -> dict[str, Any]:
    """One run's skill request, scrubbed and ready to save. ``provenance`` is
    :func:`agents.code_provenance.run_provenance` of the run's rounds."""
    audit = claim_audit or {}
    claims = audit.get("claims") if isinstance(audit.get("claims"), list) else []
    return {
        "id": run_id if run_id and _ID_RE.match(run_id) else uuid.uuid4().hex[:12],
        "created_at": _iso(_now()),
        "status": "new",
        "status_history": [],
        "account": account or "",
        "conversation_id": conversation_id or "",
        "run_id": run_id or "",
        "request": scrub(question)[:4000],
        "earlier_requests": [scrub(q)[:1000] for q in (earlier_requests or []) if q][:5],
        "datasets": _dataset_meta(datasets or []),
        "pipelines": [str(p) for p in (pipelines or [])],
        "skills": [str(n) for n in (skills or [])],
        "outcome": {
            "converged": bool(converged),
            "accepted_steps": int(provenance.get("accepted_steps") or 0),
            "untested_accepted": len(provenance.get("untested_accepted") or []),
            "tested_accepted": len(provenance.get("tested_accepted") or []),
            "code_attempts": int(provenance.get("code_attempts") or 0),
            "code_failures": sum(1 for s in provenance.get("steps") or []
                                 for c in s.get("code_calls") or [] if not c.get("ok")),
            "claims_checked": len(claims),
        },
        "steps": _scrub_steps(provenance.get("steps") or []),
        "induced_skills": [str(n) for n in (induced_skills or [])],
        "summary": {},
        "summary_by": "",
    }


# --- the structured summary ----------------------------------------------------------------------


_SUMMARY_SYSTEM = (
    "You summarize one request made to AiScientist, a single-cell and variant analysis platform, "
    "for the team that decides which tested skills to build next. The run went ahead with code the "
    "AI wrote itself; you are told what that code did and how it went. Reply with ONE JSON object "
    "and nothing else, with these keys:\n"
    '  "one_line": the request in one plain sentence, as the user would confirm it;\n'
    '  "goal": what the user wants to find out;\n'
    '  "inputs": list of input data types and formats;\n'
    '  "outputs": list of expected outputs (figures, tables, a report ...);\n'
    '  "methods": list of methods or tools the user named (empty if none);\n'
    f'  "category": exactly one of: {", ".join(CATEGORIES)};\n'
    '  "closest_skill": the closest existing pipeline, skill or tool from the list given, or "";\n'
    '  "missing": what the closest one does not do that this request needed.\n'
    "Never copy identifiers, names, paths or numbers that identify a person or a sample."
)


def _summary_payload(rec: dict[str, Any], choices: "list[str]") -> str:
    steps = []
    for s in rec.get("steps") or []:
        calls = s.get("code_calls") or []
        if not calls:
            continue
        steps.append({
            "step": s.get("step", "")[:300], "accepted": s.get("accepted"),
            "code_attempts": len(calls), "failed": sum(1 for c in calls if not c.get("ok")),
            "last_result": (calls[-1].get("summary") or calls[-1].get("error") or "")[:300],
        })
    return json.dumps({
        "request": rec.get("request", ""), "earlier_requests": rec.get("earlier_requests", []),
        "datasets": rec.get("datasets", []), "pipelines_used": rec.get("pipelines", []),
        "skills_used": rec.get("skills", []),
        "steps_that_needed_code": steps[:8], "existing_pipelines_skills_and_tools": choices[:120],
    }, ensure_ascii=False)


def fallback_summary(rec: dict[str, Any]) -> dict[str, Any]:
    """The summary when no model is available or its reply is unusable: what code can say."""
    req = " ".join(str(rec.get("request") or "").split())
    files = [f for s in rec.get("steps") or [] for c in s.get("code_calls") or []
             for f in c.get("files") or []]
    return {
        "one_line": req[:200], "goal": req[:400],
        "inputs": sorted({str(d.get("kind") or d.get("format") or d.get("name") or "")
                          for d in rec.get("datasets") or []} - {""}),
        "outputs": sorted({Path(f).suffix.lstrip(".") or f for f in files})[:10],
        "methods": [], "category": "other",
        "closest_skill": ([*(rec.get("pipelines") or []), *(rec.get("skills") or [])] or [""])[0],
        "missing": "",
    }


def _clean_summary(obj: dict[str, Any], choices: "list[str]") -> dict[str, Any]:
    def text(k: str, n: int) -> str:
        return scrub(obj.get(k) or "")[:n]

    def items(k: str) -> "list[str]":
        v = obj.get(k)
        vals = v if isinstance(v, list) else ([v] if v else [])
        return [scrub(x)[:120] for x in vals if str(x).strip()][:10]

    category = str(obj.get("category") or "").strip().lower()
    closest = str(obj.get("closest_skill") or "").strip()
    return {
        "one_line": text("one_line", 300), "goal": text("goal", 600),
        "inputs": items("inputs"), "outputs": items("outputs"), "methods": items("methods"),
        "category": category if category in CATEGORIES else "other",
        "closest_skill": closest if closest in choices else "",
        "missing": text("missing", 600),
    }


def summarize(rec: dict[str, Any], complete_fn: "Callable[..., str] | None",
              choices: "list[str]") -> "tuple[dict[str, Any], str]":
    """``(summary, by)``: the model's structured summary on the cheap ``classify`` tier (so its
    tokens land in the usage record like every other call of the run), or the code-built one."""
    if complete_fn is not None:
        try:
            reply = complete_fn([{"role": "system", "content": _SUMMARY_SYSTEM},
                                 {"role": "user", "content": _summary_payload(rec, choices)}],
                                role="classify")
            obj = safe_json_loads(_json_block(reply))
            if obj:
                return _clean_summary(obj, choices), "model"
        except Exception as exc:  # noqa: BLE001 - the record matters more than its summary
            print(f"[skill_requests] summary failed: {type(exc).__name__}: {exc}")
    return fallback_summary(rec), "code"


def _json_block(text: str) -> str:
    """The outermost ``{...}`` of a reply that wrapped its JSON in prose."""
    t = (text or "").strip()
    if t.startswith("{") or t.startswith("```"):
        return t
    a, b = t.find("{"), t.rfind("}")
    return t[a:b + 1] if 0 <= a < b else t


# --- the store -----------------------------------------------------------------------------------


def _path(rid: str) -> Path:
    if not _ID_RE.match(rid or ""):
        raise KeyError(rid)
    return requests_dir() / f"{rid}.json"


def save(rec: dict[str, Any]) -> Path:
    d = requests_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = _path(rec["id"])
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def apply_retention(rec: dict[str, Any], *, now: "datetime | None" = None,
                    days: "int | None" = None) -> bool:
    """Drop a record's raw parts once it is older than the retention period. True if it changed."""
    if rec.get("raw_expired"):
        return False
    try:
        created = datetime.fromisoformat(str(rec.get("created_at")))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if (now or _now()) - created < timedelta(days=days or retention_days()):
        return False
    rec["request"] = ""
    rec["earlier_requests"] = []
    for s in rec.get("steps") or []:
        s["critique"] = ""
        for c in s.get("code_calls") or []:
            c.pop("code", None)
            c.pop("error", None)
            c.pop("summary", None)
    rec["raw_expired"] = _iso(now or _now())
    return True


def load(rid: str) -> "dict[str, Any] | None":
    try:
        rec = json.loads(_path(rid).read_text(encoding="utf-8"))
    except (KeyError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    if apply_retention(rec):
        save(rec)
    return rec


def _row(rec: dict[str, Any]) -> dict[str, Any]:
    """The listing's view of a record: everything but the code."""
    summ = rec.get("summary") or {}
    return {
        "id": rec.get("id"), "created_at": rec.get("created_at"), "status": rec.get("status"),
        "account": rec.get("account"), "run_id": rec.get("run_id"),
        "one_line": summ.get("one_line") or rec.get("request", "")[:200],
        "category": summ.get("category") or "other", "closest_skill": summ.get("closest_skill") or "",
        "missing": summ.get("missing") or "", "pipelines": rec.get("pipelines") or [],
        "skills": rec.get("skills") or [],
        "outcome": rec.get("outcome") or {}, "induced_skills": rec.get("induced_skills") or [],
        "untested_steps": [s.get("step") for s in rec.get("steps") or []
                           if s.get("status") == "untested" and s.get("accepted")][:6],
        "raw_expired": bool(rec.get("raw_expired")),
    }


def listing(*, status: str = "", category: str = "", q: str = "") -> "list[dict[str, Any]]":
    """Every record's row, newest first, filtered by status, category and a text search."""
    d = requests_dir()
    if not d.is_dir():
        return []
    rows = []
    needle = (q or "").strip().lower()
    for p in d.glob("*.json"):
        rec = load(p.stem)
        if rec is None:
            continue
        row = _row(rec)
        if status and row["status"] != status:
            continue
        if category and row["category"] != category:
            continue
        if needle and needle not in json.dumps(row, ensure_ascii=False).lower():
            continue
        rows.append(row)
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows


def groups(rows: "list[dict[str, Any]]") -> "list[dict[str, Any]]":
    """Similar requests together: same category and same closest skill. Most-requested first,
    by the number of different accounts, then of requests."""
    by: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r.get("category") or "other", r.get("closest_skill") or "")
        g = by.setdefault(key, {"category": key[0], "closest_skill": key[1], "requests": 0,
                                "accounts": set(), "latest": "", "ids": [], "open": 0})
        g["requests"] += 1
        g["accounts"].add(r.get("account") or "")
        g["latest"] = max(g["latest"], str(r.get("created_at") or ""))
        g["ids"].append(r.get("id"))
        g["open"] += r.get("status") in ("new", "planned", "in_progress")
    out = [{**g, "accounts": len(g["accounts"] - {""})} for g in by.values()]
    out.sort(key=lambda g: (g["accounts"], g["requests"], g["latest"]), reverse=True)
    return out


def set_status(rid: str, status: str, *, by: str, note: str = "") -> dict[str, Any]:
    status = (status or "").strip().lower()
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    rec = load(rid)
    if rec is None:
        raise KeyError(rid)
    rec.setdefault("status_history", []).append(
        {"from": rec.get("status"), "to": status, "by": by, "at": _iso(_now()),
         "note": scrub(note)[:1000]})
    rec["status"] = status
    save(rec)
    return rec
