"""The report folder of a run: a short summary, the approved plan and every scientific result, once.

A run's own folders hold what the application needs to continue, debug and account for a run:
plan versions, process records, logs, pointers in JSON. This module builds, beside them and
without changing them, the folder a researcher takes away (RCHENLAB/AiScientist issue 113):

    <run-id>/
      summary.md       five fixed sections; at most NARRATIVE_WORD_LIMIT words outside "Files"
      plan.md          the approved plan, readable
      plan.yaml        the only YAML: the approved plan as it ran
      results/
        report/        the written report, when the run was asked for one
        figures/       plots, with the subfolders their tool gave them (an MCP server's: its name)
        tables/        CSV and TSV results; ``records/`` holds what was carried over from JSON
        datasets/      H5AD and other datasets
        cellqc/        CellQC's own result set, one copy (one folder per call when it ran more than once)
        extra/         any other result, under its original relative path

Everything is decided from the files of ``<run>/artifacts`` and ``<run>/result``; nothing is run
again. The summary's two written parts (key results and limitations) are the ones a model wrote for
the run when the report was asked for (:func:`write_narrative`, one call, kept with the run);
otherwise they are taken from the run's existing summary. Building the folder calls no model.

    folder = plan_report_folder(run_dir)        # what goes where, what is left out and why
    problems = build_report_folder(run_dir, dest)   # writes ``dest``; an empty list means valid
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml

FORMAT_VERSION = 1
SUMMARY_SECTIONS = ("Question", "Key results", "Method and inputs", "Limitations", "Files")
NARRATIVE_WORD_LIMIT = 300

# The roots of a run that can hold results, in the order that decides which of two identical
# files is kept: a tool's own result set first, then the gateway's sorted folders.
RESULT_ROOTS = ("result", "artifacts")

FIGURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".pdf", ".tif", ".tiff", ".eps", ".gif", ".webp"}
TABLE_SUFFIXES = {".csv", ".tsv", ".xlsx", ".parquet"}
DATASET_SUFFIXES = {".h5ad", ".h5", ".h5mu", ".loom", ".rds", ".h5seurat", ".zarr"}

# Records the application keeps for itself. Their facts reach the summary's "Method and inputs";
# the files do not belong in the report.
PROCESS_DIRS = {"process", "logs", "plan_replies"}
LOG_NAMES = {"transcript.md", "event_log.txt", "checkpoint_stalled.md", "loop.log"}
WORKFLOW_NAMES = {"workflow.dot", "workflow.png", "workflow.svg", "workflow.pdf"}
TEMP_RE = re.compile(r"(^\.)|(~$)|(\.tmp$)|(\.part$)|(\.swp$)")
# JSON whose values are measurements or a dataset's description: carried into a table before the
# file is left out. Every other JSON under a result root is a pointer or a process record.
RECORD_JSON = ("artifacts/data/qc_metrics.json", "artifacts/data/dataset_results.json",
               "artifacts/data/result_dataset.json")
POINTER_JSON = {"croissant.json", "current.json", "mcp_result.json", "manifest.json", "hpc3_home.json",
                "stage_out.json", "curation_tables.json", "agent_decision_trace.json", "dataset_manifest.json",
                "cellqc_result.json"}
PLAN_TRAILER = "# --- Saved by AiScientist with this version; not part of the plan. ---"
# The two written parts of the summary, when a model wrote them for this run (write_narrative).
NARRATIVE_NAME = "narrative.md"
KEY_RESULTS_WORDS, LIMITATIONS_WORDS = 170, 60
_CATEGORY_ORDER = {"cellqc": 0, "datasets": 1, "figures": 2, "tables": 3, "report": 4, "extra": 5}
# A JSON record larger than this is not carried into a table. It is not put in the folder as JSON
# either (the folder has none): the summary's "Files" names it, so it is never left out silently.
JSON_LIMIT = 20_000_000
# What an MCP call wrote: ``artifacts/extra/mcp/<server>/<call>/<kind>/<path>`` (``kind`` is
# figures, tables, data or result), its JSON record beside them, and a copy of that record per task.
MCP_PREFIX = ("artifacts", "extra", "mcp")
# Sentences of a run's own summary that are about the website, not the result: where to click,
# where a file is kept and for how long. The short summary never carries them.
_UI_CLAUSE = re.compile(r"[;,]\s*see the (?:optional )?technical details(?: in (?:the )?Downloads(?: panel)?)?"
                        r"(?=[.\s]|$)", re.I)
_UI_SENTENCE = re.compile(r"Save to my data|Downloads panel|\bin Downloads\b|\bRuns\s*→|\bkept on (?:HPC3|the server)\b"
                          r"|\bsaved in your data\b|until the session ends|Optional technical details", re.I)


@dataclass
class Entry:
    """One file of the report folder: copied from ``source`` or written from ``text``."""
    dest: str                          # POSIX path inside the report folder
    source: Path | None = None
    text: str | None = None
    origin: str = ""                   # the run-relative path it came from (or was derived from)
    size: int = 0

    @property
    def category(self) -> str:
        parts = PurePosixPath(self.dest).parts
        return parts[1] if len(parts) > 2 and parts[0] == "results" else ""


@dataclass
class ReportFolder:
    run_id: str
    entries: list[Entry] = field(default_factory=list)
    left_out: list[tuple[str, str]] = field(default_factory=list)       # (run-relative path, why)
    too_large: list[tuple[str, int]] = field(default_factory=list)      # JSON records over JSON_LIMIT: (path, bytes)
    same_as: dict[str, str] = field(default_factory=dict)               # run-relative path -> dest kept
    plan_state: str = "none"           # approved | draft | cancelled | spec | none
    plan_versions: int = 0
    executed: bool = False             # the run produced at least one result
    notes: list[str] = field(default_factory=list)

    def dest_of(self, origin: str) -> str | None:
        """Where a run-relative path went: its own entry, or the identical file that was kept."""
        for e in self.entries:
            if e.origin == origin:
                return e.dest
        return self.same_as.get(origin)


# ----------------------------------------------------------------------------------------------
# What a file is
# ----------------------------------------------------------------------------------------------

def _suffix(name: str) -> str:
    low = name.lower()
    for double in (".csv.gz", ".tsv.gz", ".txt.gz"):
        if low.endswith(double):
            return double
    return PurePosixPath(low).suffix


def _kind(name: str) -> str:
    suf = _suffix(name)
    if suf in FIGURE_SUFFIXES:
        return "figures"
    if suf in TABLE_SUFFIXES or suf in (".csv.gz", ".tsv.gz"):
        return "tables"
    if suf in DATASET_SUFFIXES:
        return "datasets"
    return "extra"


def mcp_parts(rel: str) -> tuple[str, str, tuple[str, ...]] | None:
    """``(server, call, rest)`` for a file an MCP call wrote, or None for any other path. A second
    copy one ``mcp/`` level down (``extra/mcp/mcp/<server>/...``) is read as the same call."""
    parts = PurePosixPath(rel).parts
    if parts[:3] != MCP_PREFIX:
        return None
    segs = parts[3:]
    while segs[:1] == ("mcp",):
        segs = segs[1:]
    if len(segs) < 3:
        return None
    return segs[0], segs[1], tuple(segs[2:])


def _mcp_dest(server: str, call: str, rest: tuple[str, ...], name: str, keep_call: bool) -> str:
    """Where a result of an MCP call goes: under its kind and its server, and under its call only
    when the run made more than one call to that server. CellQC's result set stays whole."""
    where = f"{server}/{call}" if keep_call else server
    head, below = rest[0], "/".join(rest[1:])
    if server == "cellqc" and head == "result" and below:
        at = f"{call}/{below}" if keep_call else below
        return f"results/datasets/cellqc/{at}" if _kind(name) == "datasets" else f"results/cellqc/{at}"
    if head in ("figures", "tables") and below:
        return f"results/{head}/{where}/{below}"
    return f"results/{_kind(name)}/{where}/{'/'.join(rest)}"


def _record_dest(rel: str, multi_call: frozenset[str] = frozenset()) -> str:
    """The table a JSON record is carried into, under ``results/tables/records/``."""
    mcp = mcp_parts(rel)
    if mcp is not None:
        server, call, rest = mcp
        if rest[0] in ("data", "tables", "figures") and len(rest) > 1:
            rest = rest[1:]
        below = PurePosixPath(server, *((call,) if server in multi_call else ()), *rest)
    else:
        below = PurePosixPath(rel).relative_to(PurePosixPath(rel).parts[0])
        if below.parts[:1] in (("data",), ("extra",)):
            below = PurePosixPath(*below.parts[1:])
    return f"results/tables/records/{below.with_suffix('.tsv').as_posix()}"


def classify(rel: str, size: int = 1, multi_call: frozenset[str] = frozenset()) -> tuple[str, str]:
    """``(what, detail)`` for a run-relative POSIX path: ``("result", dest)``, ``("plan", "")``,
    ``("record", "")`` for JSON whose values are carried into a table, ``("summary", "")`` for the
    run's existing summary, or ``("out", reason)``. ``multi_call`` names the MCP servers this run
    called more than once: their results keep the call's folder."""
    p = PurePosixPath(rel)
    parts, name = p.parts, p.name
    if not parts or parts[0] not in RESULT_ROOTS:
        return "out", "outside the result folders (a run record or a log)"
    if size == 0:
        return "out", "empty file"
    if any(TEMP_RE.search(part) for part in parts):
        return "out", "temporary or hidden file"
    inner = parts[1:]                      # below the root
    if parts[0] == "artifacts":
        if inner[:1] == ("plan",) or inner[:2] == ("extra", "plan"):
            return "plan", ""
        if inner[:1] == ("report",):
            if name in ("plan.yaml", "plan.md"):
                return "plan", ""
            if name in ("summary.md", NARRATIVE_NAME):
                return "summary", ""
    if any(part in PROCESS_DIRS for part in parts[:-1]):
        return "out", "process record or log"
    if name in LOG_NAMES or name.endswith(".log"):
        return "out", "process record or log"
    if name in WORKFLOW_NAMES or name.endswith(".dot"):
        return "out", "workflow schematic"
    if name.endswith(".json"):
        if rel in RECORD_JSON or (parts[0] == "result" and name.endswith(".h5ad.json")):
            return "record", ""
        if name in POINTER_JSON or name.startswith("round_"):
            return "out", "machine record (a pointer, not a result)"
        return "record", ""            # an unknown JSON under a result root: carried, never dropped
    if name.endswith((".yaml", ".yml")):
        return "out", "a second YAML file (the report keeps one: the approved plan)"

    mcp = mcp_parts(rel)
    if mcp is not None:
        return "result", _mcp_dest(*mcp, name, keep_call=mcp[0] in multi_call)
    # A result. CellQC's own result set stays whole and in one place.
    if (parts[0] == "result" and inner[:1] == ("cellqc",)) or (parts[0] == "artifacts" and inner[:1] == ("result",)):
        below = "/".join(inner[1:])
        if _kind(name) == "datasets":
            return "result", f"results/datasets/cellqc/{below}"
        return "result", f"results/cellqc/{below}"
    if parts[0] == "result":
        below = "/".join(inner)
        return "result", f"results/{_kind(name)}/{below}"
    # artifacts/: the gateway's sorted folders keep their name; anything else goes by its type and
    # keeps the subfolders its tool gave it.
    head, below = inner[0], "/".join(inner[1:])
    if head in ("figures", "tables") and below:
        return "result", f"results/{head}/{below}"
    if head == "report":
        return "result", f"results/report/{below}"             # a written report the run was asked for
    if head == "extra" and below:
        return "result", f"results/{_kind(name)}/{below}"
    if head == "data" and below:
        return "result", f"results/extra/data/{below}"
    return "result", f"results/{_kind(name)}/{'/'.join(inner)}"


# ----------------------------------------------------------------------------------------------
# Reading a run
# ----------------------------------------------------------------------------------------------

def _files(run_dir: Path) -> list[tuple[str, Path, int]]:
    """The regular files of the run's result folders and of its top level, as ``(run-relative POSIX
    path, path, size)``. Symbolic links are not followed and not listed: a report never reaches
    outside its run. Other top-level folders (logs, a download being written) are not read."""
    out: list[tuple[str, Path, int]] = []
    for path in sorted(run_dir.iterdir()):
        if path.is_file() and not path.is_symlink():
            out.append((path.name, path, path.stat().st_size))
    for top in RESULT_ROOTS:
        start = run_dir / top
        if not start.is_dir() or start.is_symlink():
            continue
        for root, dirs, names in os.walk(start, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(root) / d).is_symlink())
            for name in sorted(names):
                path = Path(root) / name
                if path.is_symlink() or not path.is_file():
                    continue
                out.append((path.relative_to(run_dir).as_posix(), path, path.stat().st_size))
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _read_json(path: Path, limit: int | None = None) -> Any:
    try:
        if path.stat().st_size > (JSON_LIMIT if limit is None else limit):
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def flatten_json(value: Any) -> list[tuple[str, str]]:
    """Every scalar of a JSON value as ``(path, value)``, in document order: ``a.b[0].c``. Strings
    are written as they are, other scalars as JSON, and an empty list or object as ``[]`` / ``{}``,
    so the table holds everything the record did."""
    rows: list[tuple[str, str]] = []

    def walk(v: Any, at: str) -> None:
        if isinstance(v, dict) and v:
            for k, x in v.items():
                walk(x, f"{at}.{k}" if at else str(k))
        elif isinstance(v, list) and v:
            for i, x in enumerate(v):
                walk(x, f"{at}[{i}]")
        else:
            rows.append((at, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)))

    walk(value, "")
    return rows


def _tsv(rows: Iterable[Iterable[str]], header: tuple[str, ...]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


# ----------------------------------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------------------------------

def _strip_trailer(text: str) -> str:
    """A saved plan version without the review record AiScientist appends to it."""
    cut = text.find(PLAN_TRAILER)
    return (text[:cut] if cut >= 0 else text).rstrip() + "\n"


def _version_state(text: str) -> str:
    cut = text.find(PLAN_TRAILER)
    if cut < 0:
        return ""
    try:
        review = (yaml.safe_load(text[cut:]) or {}).get("_review") or {}
    except yaml.YAMLError:
        return ""
    return str(review.get("state") or "")


def find_plan(run_dir: Path) -> tuple[str, str, int, str]:
    """``(plan yaml text, state, number of versions, run-relative origin)``. The approved plan is
    ``report/plan.yaml`` when the run wrote it, else the version ``current.json`` names when that
    one was approved. A run that never had a plan approved gives its latest draft with the state
    its last version was left in (``draft`` or ``cancelled``), so that a report can say so. A
    curation has no plan document; the task spec the researcher approved stands in for it."""
    art = run_dir / "artifacts"
    versions: list[Path] = []
    plan_dir = None
    for cand in (art / "plan", art / "extra" / "plan"):
        if cand.is_dir() and not cand.is_symlink():
            plan_dir = cand
            versions = sorted(p for p in cand.glob("v*.yaml") if p.is_file() and not p.is_symlink())
            break
    approved = art / "report" / "plan.yaml"
    if approved.is_file() and not approved.is_symlink():
        return (_strip_trailer(approved.read_text(encoding="utf-8")), "approved", len(versions),
                "artifacts/report/plan.yaml")
    if plan_dir is not None and versions:
        current = _read_json(plan_dir / "current.json") or {}
        named = plan_dir / str(current.get("filename") or "")
        if current.get("state") == "approved" and named in versions:
            return (_strip_trailer(named.read_text(encoding="utf-8")), "approved", len(versions),
                    named.relative_to(run_dir).as_posix())
        for path in reversed(versions):
            text = path.read_text(encoding="utf-8")
            if _version_state(text) == "approved":
                return _strip_trailer(text), "approved", len(versions), path.relative_to(run_dir).as_posix()
        last = versions[-1].read_text(encoding="utf-8")
        state = _version_state(last) or str(current.get("state") or "") or "draft"
        return (_strip_trailer(last), "cancelled" if state == "cancelled" else "draft", len(versions),
                versions[-1].relative_to(run_dir).as_posix())
    specs = sorted(p for p in art.glob("*/task_spec.json") if p.is_file() and not p.is_symlink())
    if len(specs) == 1:
        spec = _read_json(specs[0])
        if isinstance(spec, dict):
            text = ("# The task specification this curation ran with, as approved on its plan card.\n"
                    + yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100))
            return text, "spec", 1, specs[0].relative_to(run_dir).as_posix()
    return "", "none", 0, ""


def _plan_doc(text: str) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(text) if text else None
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def plan_markdown(text: str, state: str) -> str:
    """A readable plan from the plan document: the request and each step with its tool, skill,
    settings and what it does."""
    doc = _plan_doc(text)
    lines = ["# Plan", ""]
    if state == "spec":
        lines += ["The task specification this curation ran with.", ""]
        for key, value in doc.items():
            shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            lines.append(f"- **{key}**: {shown}")
        return "\n".join(lines).rstrip() + "\n"
    if doc.get("request"):
        lines += [f"**Request:** {doc['request']}", ""]
    for n, step in enumerate(doc.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        lines.append(f"{n}. **{step.get('title') or step.get('id') or 'Step'}**")
        for key in ("tool", "skill", "runs_on"):
            if step.get(key):
                lines.append(f"   - {key}: `{step[key]}`")
        if isinstance(step.get("settings"), dict) and step["settings"]:
            settings = ", ".join(f"`{k}` = {json.dumps(v, ensure_ascii=False)}" for k, v in step["settings"].items())
            lines.append(f"   - settings: {settings}")
        if step.get("does"):
            lines.append(f"   - {' '.join(str(step['does']).split())}")
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------------------------------
# What goes where
# ----------------------------------------------------------------------------------------------

def plan_report_folder(run_dir: Path) -> ReportFolder:
    """Decide the report folder of ``run_dir`` without writing anything: each result once, at its
    place; what is left out and why; the plan and its state. ``summary.md`` is added by
    :func:`build_report_folder`, which needs the finished list for its file index."""
    run_dir = Path(run_dir)
    folder = ReportFolder(run_id=run_dir.name)
    results: list[Entry] = []
    summary_origin = ""
    files = _files(run_dir)
    calls: dict[str, set[str]] = {}
    for rel, _path, _size in files:
        mcp = mcp_parts(rel)
        if mcp is not None and not TEMP_RE.search(mcp[1]):
            calls.setdefault(mcp[0], set()).add(mcp[1])
    multi_call = frozenset(server for server, names in calls.items() if len(names) > 1)
    for rel, path, size in files:
        what, detail = classify(rel, size, multi_call)
        if what == "result":
            results.append(Entry(dest=detail, source=path, origin=rel, size=size))
        elif what == "record":
            value = _read_json(path)
            if value is None:
                folder.left_out.append((rel, f"JSON record over {JSON_LIMIT // 1_000_000} MB, too large to carry "
                                             "into a table" if size > JSON_LIMIT else "unreadable JSON record"))
                if size > JSON_LIMIT:
                    folder.too_large.append((rel, size))
                continue
            text = _tsv(flatten_json(value), ("field", "value"))
            dest = _record_dest(rel, multi_call)
            results.append(Entry(dest=dest, text=text, origin=rel, size=len(text.encode("utf-8"))))
            folder.left_out.append((rel, f"JSON; its values are in {dest}"))
        elif what == "summary":
            summary_origin = summary_origin or rel
        elif what == "plan":
            pass                                        # the plan is chosen below, once
        else:
            folder.left_out.append((rel, detail))

    folder.entries = _once(results, folder)
    folder.executed = any(e.source is not None for e in folder.entries)

    text, state, n_versions, origin = find_plan(run_dir)
    folder.plan_state, folder.plan_versions = state, n_versions
    if text:
        folder.entries.append(Entry(dest="plan.yaml", text=text, origin=origin, size=len(text.encode("utf-8"))))
        readable = run_dir / "artifacts" / "report" / "plan.md"
        if state == "approved" and readable.is_file() and not readable.is_symlink():
            md, md_origin = readable.read_text(encoding="utf-8"), "artifacts/report/plan.md"
        else:
            md, md_origin = plan_markdown(text, state), origin
        folder.entries.append(Entry(dest="plan.md", text=md, origin=md_origin, size=len(md.encode("utf-8"))))
    if origin.endswith(".json"):                        # the task spec is the plan: not also a table
        _drop(folder, origin, "JSON; it is the report's plan.yaml")
    described = {PurePosixPath(e.origin).name for e in folder.entries if e.text is not None}
    if "result_dataset.json" in described:
        for e in list(folder.entries):
            if e.text is not None and e.origin.startswith("result/") and e.origin.endswith(".h5ad.json"):
                _drop(folder, e.origin, "JSON; the same description is in results/tables/records/result_dataset.tsv")
    if summary_origin:
        folder.notes.append(f"summary:{summary_origin}")
    for rel, _path, _size in files:
        if classify(rel, 1)[0] == "plan" and rel != origin and rel != "artifacts/report/plan.md":
            folder.left_out.append((rel, "an earlier plan version or the plan pointer"))
    return folder


def _drop(folder: ReportFolder, origin: str, why: str) -> None:
    """Take the entry made from ``origin`` out of the folder and say why."""
    folder.entries = [e for e in folder.entries if e.origin != origin or e.dest in ("plan.yaml", "plan.md")]
    folder.left_out = [(rel, w) for rel, w in folder.left_out if rel != origin] + [(origin, why)]


def _once(results: list[Entry], folder: ReportFolder) -> list[Entry]:
    """Each content once. Of identical files the one in the earlier category is kept (a tool's own
    result set stays whole) and the others are recorded in ``folder.same_as``. Two different files
    that would land on one path both stay, the later one renamed after where it came from."""
    def content_key(e: Entry) -> tuple[int, str]:
        digest = hashlib.sha256(e.text.encode("utf-8")).hexdigest() if e.text is not None else ""
        return e.size, digest

    by_size: dict[int, list[Entry]] = {}
    for e in results:
        by_size.setdefault(e.size, []).append(e)
    digest: dict[int, str] = {}
    for group in by_size.values():
        if len(group) > 1:                              # only files of one size can be identical
            for e in group:
                digest[id(e)] = content_key(e)[1] or _sha256(e.source)     # type: ignore[arg-type]

    def root_rank(e: Entry) -> int:
        root = PurePosixPath(e.origin).parts[0] if e.origin else ""
        return RESULT_ROOTS.index(root) if root in RESULT_ROOTS else len(RESULT_ROOTS)

    def copy_rank(e: Entry) -> int:
        # ``artifacts/result/`` is the gateway's copy of a CellQC result set: of identical files, the
        # set CellQC wrote is kept, so that each set stays whole in its own folder.
        return 1 if e.origin.startswith("artifacts/result/") else 0

    order = sorted(results, key=lambda e: (_CATEGORY_ORDER.get(e.category, 9), copy_rank(e), e.dest,
                                           root_rank(e), e.origin))
    kept: list[Entry] = []
    seen_content: dict[tuple[int, str], Entry] = {}
    taken: dict[str, Entry] = {}
    for e in order:
        key = (e.size, digest.get(id(e), ""))
        if key[1] and key in seen_content:
            folder.same_as[e.origin] = seen_content[key].dest
            folder.left_out.append((e.origin, f"identical to {seen_content[key].dest}"))
            continue
        if e.dest in taken:                             # same place, different content: keep both
            p = PurePosixPath(e.dest)
            tag = re.sub(r"[^A-Za-z0-9]+", "-", str(PurePosixPath(e.origin).parent)).strip("-")
            suf = _suffix(p.name)
            e.dest = (p.parent / f"{p.name[: len(p.name) - len(suf)]}__{tag}{suf}").as_posix()
            folder.notes.append(f"renamed:{e.origin}->{e.dest}")
        taken[e.dest] = e
        if key[1]:
            seen_content[key] = e
        kept.append(e)
    return sorted(kept, key=lambda e: e.dest)


# ----------------------------------------------------------------------------------------------
# The summary
# ----------------------------------------------------------------------------------------------

def words(text: str) -> int:
    """Words of running text: markdown marks, table rules and link targets do not count."""
    text = re.sub(r"\]\([^)]*\)", "]", text)
    text = re.sub(r"^\s*\|?[\s:|-]+\|?\s*$", " ", text, flags=re.M)
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9.,%'’/-]*", text))


def shorten(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` words at the end of a sentence, line or list item."""
    text = text.strip()
    if limit <= 0:
        return ""
    if words(text) <= limit:
        return text
    pieces = re.split(r"(?<=[.!?])\s+|\n+", text)
    out: list[str] = []
    for piece in pieces:
        if not piece.strip():
            continue
        if words("\n".join(out + [piece])) > limit:
            break
        out.append(piece.strip())
    if not out:                                         # one long sentence: cut it at the word
        found = list(re.finditer(r"[A-Za-z0-9][A-Za-z0-9.,%'’/-]*", text))
        return text[: found[limit - 1].end()].rstrip(",;:") + " …"
    joined = "\n".join(out) if any(p.startswith(("-", "*", "|")) for p in out) else " ".join(out)
    return joined


def _section(md: str, *titles: str) -> str:
    """The body of the first markdown section whose heading is one of ``titles``."""
    wanted = {t.lower() for t in titles}
    lines, body, level = md.splitlines(), [], 0
    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", line)
        if m:
            if level and len(m.group(1)) <= level:
                break
            if not level and m.group(2).strip().lower() in wanted:
                level = len(m.group(1))
                continue
        if level:
            body.append(line)
    return "\n".join(body).strip()


def existing_summary_parts(run_dir: Path) -> tuple[str, str]:
    """``(key results, limitations)`` as the run has them: the narrative a model wrote for the report
    (``report/narrative.md``), else from the summary the run wrote: its abstract (or the whole of a
    short summary) and its limitations section, or its warning lines."""
    report = Path(run_dir) / "artifacts" / "report"
    narrative = report / NARRATIVE_NAME
    if narrative.is_file() and not narrative.is_symlink():
        md = narrative.read_text(encoding="utf-8", errors="replace")
        key, limits = _section(md, "Key results"), _section(md, "Limitations")
        if key:
            return strip_ui_text(key), strip_ui_text(limits)
    path = report / "summary.md"
    if not path.is_file() or path.is_symlink():
        return "", ""
    md = path.read_text(encoding="utf-8", errors="replace")
    key = _section(md, "Abstract", "Key results", "Summary", "Results")
    if not key:
        key = "\n".join(line for line in md.splitlines() if not re.match(r"^#{1,6}\s", line)).strip()
    limits = _section(md, "Limitations", "Caveats")
    if not limits:
        warned = [line for line in md.splitlines() if "⚠" in line]
        limits = "\n".join(warned).strip()
        key = "\n".join(line for line in key.splitlines() if line not in warned).strip()   # said once
    return strip_ui_text(key), strip_ui_text(limits)


def strip_ui_text(md: str) -> str:
    """``md`` without what a run's summary says about the website: where to click (the Downloads
    panel, Runs → "Save to my data") and where a file is kept and for how long. A sentence that
    says only that goes; a warning that points to the Downloads panel keeps its warning."""
    out: list[str] = []
    for line in md.splitlines():
        line = _UI_CLAUSE.sub("", line)
        lead = re.match(r"^\s*(?:[-*]\s+|\d+\.\s+)?(?:⚠️?\s*)?", line).group(0)          # type: ignore[union-attr]
        body = line[len(lead):]
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z*`(\[\"“])", body)
        kept = [s for s in sentences if not _UI_SENTENCE.search(s)]
        if body.strip() and not "".join(kept).strip():
            continue                                    # the line said nothing but where to click
        out.append(lead + " ".join(kept) if kept != sentences else line)
    return "\n".join(out).strip()


def curation_result(run_dir: Path) -> str:
    """One sentence for a curation run from the record of its tables: how many genes, by claim."""
    rec = _read_json(Path(run_dir) / "curation_tables.json") or {}
    if not rec.get("n_genes"):
        return ""
    counts = rec.get("counts") if isinstance(rec.get("counts"), dict) else {}
    by = ", ".join(f"{n} {name}" for name, n in counts.items())
    what = str(rec.get("curation") or "the curated property")
    return f"The curation of {what} lists {rec['n_genes']} genes" + (f" (by claim: {by})." if by else ".")


NARRATIVE_SYSTEM = f"""You write two parts of a short report of a finished analysis run: "Key results" and "Limitations". You are given the question and what the run recorded as its answer. Use only what is given; never add a number, a gene, a method or a conclusion that is not there. Write each number as it is given: do not round it, convert it or compute a new one.

Key results (at most {KEY_RESULTS_WORDS} words): what the run found, as the answer to the question. Lead with the main finding. Keep the numbers that carry it, with their units and what they were measured on. A short list is fine; no table. Say plainly when a step failed or a result is uncertain.

Limitations (at most {LIMITATIONS_WORDS} words): what limits the conclusion: a failed or skipped step, a warning the run raised, descriptive results without a test, one dataset or one donor, an unverified input. If the record names none, write "None recorded."

Leave out: an abstract or a conclusion that repeats the results; how the plan was chosen; which files exist or where to click to download them; how long a file is kept; process details such as rounds, retries and logs.

Return ONLY JSON: {{"key_results": "...", "limitations": "..."}}. Both values are markdown text."""


def narrative_source(run_dir: Path, limit: int = 24_000) -> str:
    """What the run recorded as its answer: its written report when it has one, else its summary."""
    report = Path(run_dir) / "artifacts" / "report"
    for name in ("report.md", "summary.md"):
        path = report / name
        if path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
    return curation_result(run_dir)


def parse_narrative(text: str) -> tuple[str, str] | None:
    """``(key results, limitations)`` from the model's reply, or None when it is not the JSON asked
    for or has no key results. Both parts are cut to their length."""
    found = re.search(r"\{.*\}", text or "", flags=re.S)
    try:
        data = json.loads(found.group(0)) if found else None
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("key_results"), str) or not data["key_results"].strip():
        return None
    limits = data.get("limitations") if isinstance(data.get("limitations"), str) else ""
    return shorten(data["key_results"], KEY_RESULTS_WORDS), shorten(limits or "None recorded.", LIMITATIONS_WORDS)


# A number as written in running text: 31,303 / 90.6 / 0.05 / 1e-5. Digits inside a name (log2FC,
# Ddx41, H5AD, v0.4.15, CP10k) are not numbers here.
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?(?![A-Za-z0-9_]|,\d)")


# The marker of a numbered list item ("1. ", "2) ") counts items; it is not a number of the run.
_LIST_MARKER = re.compile(r"^[ \t]*\d+[.)][ \t]+", re.M)
# Counts a record may spell out ("two samples") and a summary may write in digits.
_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
                 "twelve")


def _number_values(text: str) -> set[float]:
    values: set[float] = set()
    for m in _NUMBER.finditer(text or ""):
        try:
            values.add(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return values


def unsupported_numbers(text: str, source: str) -> list[str]:
    """The numbers of ``text`` that ``source`` does not have, as written in ``text``. ``31,303`` and
    ``31303`` are the same number, and so are ``10`` and ``10.0``; a count ``source`` spells out
    (``two samples``) is one it has. The markers of a numbered list in ``text`` are not read."""
    have = _number_values(source)
    spelled = set(re.findall(r"[a-z]+", (source or "").lower()))
    have |= {float(n) for n, word in enumerate(_NUMBER_WORDS) if word in spelled}
    out: list[str] = []
    for m in _NUMBER.finditer(_LIST_MARKER.sub("", text or "")):
        try:
            value = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        if value not in have and m.group(0) not in out:
            out.append(m.group(0))
    return out


def write_narrative(complete_fn: Any, run_dir: Path, question: str = "",
                    problems: list[str] | None = None) -> tuple[str, str] | None:
    """Have the model write the summary's two parts for this run and keep them with it
    (``artifacts/report/narrative.md``), where :func:`build_report_folder` reads them. One model
    call, on what the run already recorded; nothing is run again. Returns the two parts, or None
    when the run recorded no answer or the reply could not be used (the folder then falls back to
    the run's own summary). A reply with a number that neither the question nor the record has is
    not used: the summary is the first page a researcher reads, and it does not pass the report's
    claim audit. Why a reply was not used is added to ``problems``."""
    run_dir = Path(run_dir)
    source = narrative_source(run_dir)
    if not source.strip():
        return None
    plan_text = find_plan(run_dir)[0]
    doc = _plan_doc(plan_text)
    question = question or str(doc.get("request") or doc.get("question") or doc.get("task") or "")
    messages = [{"role": "system", "content": NARRATIVE_SYSTEM},
                {"role": "user", "content": f"QUESTION\n{question or 'not recorded'}\n\nWHAT THE RUN RECORDED\n{source}"}]
    parts = parse_narrative(complete_fn(messages))
    if parts is None:
        if problems is not None:
            problems.append("the model's reply was not the JSON asked for")
        return None
    added = unsupported_numbers(f"{parts[0]}\n{parts[1]}", f"{question}\n{source}")
    if added:
        if problems is not None:
            problems.append("the model's summary has numbers the run did not record: " + ", ".join(added[:5]))
        return None
    out = run_dir / "artifacts" / "report" / NARRATIVE_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"## Key results\n\n{parts[0]}\n\n## Limitations\n\n{parts[1]}\n", encoding="utf-8")
    return parts


def method_and_inputs(run_dir: Path, plan_text: str = "", folder: ReportFolder | None = None) -> list[str]:
    """What ran, on what, as the run recorded it: the shared datasets with their versions, the
    skills and MCP servers with theirs, the tools of the plan's steps, the application version.
    Given the ``folder``, a result dataset that is not in it says so, and where it is."""
    run_dir = Path(run_dir)
    lines: list[str] = []
    manifest = _read_json(run_dir / "manifest.json") or {}
    inputs = _read_json(run_dir / "artifacts" / "process" / "inputs.json") or {}
    for item in (inputs.get("shared_inputs") or [])[:6]:
        if isinstance(item, dict) and item.get("dataset"):
            lines.append(f"Dataset: `{item['dataset']}` version `{item.get('version') or 'unknown'}`.")
    atlas = manifest.get("atlas") if isinstance(manifest.get("atlas"), dict) else {}
    if atlas.get("path"):
        lines.append(f"Atlas file: `{PurePosixPath(str(atlas['path'])).name}` ({atlas.get('version') or 'no version'}).")
    result = _read_json(run_dir / "artifacts" / "data" / "result_dataset.json") or {}
    if result.get("name"):
        shape = ""
        if result.get("n_cells") and result.get("n_genes"):
            shape = f" ({int(result['n_cells']):,} cells, {int(result['n_genes']):,} genes)"
        absent = ""
        if folder is not None and not any(PurePosixPath(e.dest).name == result["name"] for e in folder.entries):
            where = ("saved in the owner's data" if result.get("saved_to") else
                     "on HPC3" if result.get("location") == "hpc3" else "")
            absent = f"; {where}, not in this folder" if where else "; not in this folder"
        lines.append(f"Result dataset: `{result['name']}`{shape}{absent}.")
    skills = [f"`{s['name']}` {s.get('version') or ''}".strip() for s in manifest.get("skills") or []
              if isinstance(s, dict) and s.get("name")]
    if skills:
        lines.append("Skills: " + ", ".join(skills) + ".")
    servers = [f"`{s['name']}` {s.get('version') or ''}".strip() for s in manifest.get("mcp_servers") or []
               if isinstance(s, dict) and s.get("name")]
    if servers:
        lines.append("MCP servers: " + ", ".join(servers) + ".")
    tools: list[str] = []
    for step in _plan_doc(plan_text).get("steps") or []:
        if isinstance(step, dict) and step.get("tool") and step["tool"] not in tools:
            tools.append(str(step["tool"]))
    if tools:
        lines.append("Plan steps used: " + ", ".join(f"`{t}`" for t in tools) + ".")
    app = manifest.get("app") if isinstance(manifest.get("app"), dict) else {}
    if app.get("tag"):
        lines.append(f"AiScientist {app['tag']}.")
    return lines


def files_index(folder: ReportFolder) -> str:
    """The compact, linked index of the report folder: the plan, the written report when there is
    one, then each results folder with its files (a folder of more than eight files is listed by its
    subfolders, with counts). A result record that was too large to carry over is named last."""
    lines: list[str] = []
    for name in ("plan.md", "plan.yaml"):
        if any(e.dest == name for e in folder.entries):
            lines.append(f"- [{name}]({name})")
    groups: dict[str, list[Entry]] = {}
    for e in folder.entries:
        if e.category:
            groups.setdefault(e.category, []).append(e)
    for cat in sorted(groups, key=lambda c: (c != "report", _CATEGORY_ORDER.get(c, 9))):
        items = groups[cat]
        if len(items) <= 8:
            links = ", ".join(f"[{PurePosixPath(e.dest).relative_to(f'results/{cat}').as_posix()}]({_link(e.dest)})"
                              for e in items)
            lines.append(f"- `results/{cat}/`: {links}")
            continue
        subs: dict[str, int] = {}
        for e in items:
            rel = PurePosixPath(e.dest).relative_to(f"results/{cat}")
            subs[rel.parts[0] if len(rel.parts) > 1 else "."] = subs.get(rel.parts[0] if len(rel.parts) > 1 else ".", 0) + 1
        shown = ", ".join(f"[{s}/]({_link(f'results/{cat}/{s}')}) ({n} files)" if s != "."
                          else f"{n} files" for s, n in sorted(subs.items()))
        lines.append(f"- [results/{cat}/]({_link(f'results/{cat}')}): {shown}")
    for rel, size in folder.too_large:
        lines.append(f"- Not in this folder: `{rel}` ({size / 1_000_000:.0f} MB), a JSON record too large to carry "
                     "into a table. It stays with the run.")
    return "\n".join(lines)


def _link(dest: str) -> str:
    return "/".join(re.sub(r"[ ()]", lambda m: f"%{ord(m.group()):02X}", part) for part in dest.split("/"))


def compose_summary(folder: ReportFolder, *, question: str, key_results: str, limitations: str,
                    method: list[str]) -> str:
    """The summary in its fixed form. The words outside "Files" are kept to NARRATIVE_WORD_LIMIT by
    shortening the two written parts; the facts of "Method and inputs" are never cut."""
    question = " ".join((question or "").split()) or "Not recorded with this run."
    if not folder.executed:
        if folder.plan_versions:
            n = folder.plan_versions
            state = {"cancelled": "the plan was cancelled", "draft": "the plan was not approved"}.get(
                folder.plan_state, "the plan was not run")
            key_results = (f"This run was not executed: {state}. {n} plan version{'s were' if n != 1 else ' was'} "
                           "drafted, and no analysis ran, so there are no results.")
        else:
            key_results = "This run produced no results."
        limitations = limitations or "Nothing was computed, so nothing can be concluded from this run."
    method_text = "\n".join(f"- {line}" for line in method) or "- No method record was kept with this run."
    question = shorten(question, 60)
    fixed = words(question) + words(method_text) + sum(words(t) for t in SUMMARY_SECTIONS[:-1])
    room = max(40, NARRATIVE_WORD_LIMIT - fixed)
    limitations = shorten(limitations or "None recorded.", max(15, room // 4))
    key_results = shorten(key_results or "No summary of results was recorded with this run.",
                          max(20, room - words(limitations)))
    body = {"Question": question, "Key results": key_results, "Method and inputs": method_text,
            "Limitations": limitations, "Files": files_index(folder) or "No files."}
    out = [f"# Report of run {folder.run_id}", ""]
    for title in SUMMARY_SECTIONS:
        out += [f"## {title}", "", body[title].strip(), ""]
    return "\n".join(out).rstrip() + "\n"


def check_summary(md: str) -> list[str]:
    """What is wrong with a summary: its sections, their order, and the length of its narrative."""
    problems: list[str] = []
    heads = re.findall(r"^##\s+(.*?)\s*$", md, flags=re.M)
    if tuple(heads) != SUMMARY_SECTIONS:
        problems.append(f"summary.md sections are {heads}, expected {list(SUMMARY_SECTIONS)}")
    cut = re.search(r"^##\s+Files\s*$", md, flags=re.M)
    narrative = md[: cut.start()] if cut else md
    narrative = re.sub(r"^#\s+.*$", "", narrative, count=1, flags=re.M)
    n = words(narrative)
    if n > NARRATIVE_WORD_LIMIT:
        problems.append(f"summary.md has {n} words outside Files; the limit is {NARRATIVE_WORD_LIMIT}")
    return problems


# ----------------------------------------------------------------------------------------------
# Writing and checking the folder
# ----------------------------------------------------------------------------------------------

_MD_LINK = re.compile(r"(\]\()([^)\s]+)(\))")
_MD_CODE = re.compile(r"`([^`\n]+)`")


def rewrite_links(md: str, folder: ReportFolder) -> str:
    """Point a markdown text's links and quoted paths at the report folder: a path the run used
    (``tables/x.csv``, ``artifacts/tables/x.csv``, ``result/report.html``) becomes where that file
    is now. A path that names nothing in the report is left as it is."""
    def new_path(old: str) -> str | None:
        old = old.strip().lstrip("./")
        for cand in (old, f"artifacts/{old}", f"result/{old}", f"artifacts/extra/{old}"):
            dest = folder.dest_of(cand)
            if dest:
                return dest
        return None

    md = _MD_LINK.sub(lambda m: m.group(1) + (_link(new_path(m.group(2)) or "") or m.group(2)) + m.group(3)
                      if not re.match(r"^[a-z][a-z0-9+.-]*:|^#", m.group(2)) else m.group(0), md)
    return _MD_CODE.sub(lambda m: f"`{new_path(m.group(1)) or m.group(1)}`" if "/" in m.group(1) else m.group(0), md)


def report_markdown_links(md: str, folder: ReportFolder, at: str) -> str:
    """A written report's Markdown, its links (named from the run's folder, as the renderer reads
    them) pointing at those files in the report folder, from ``at``, where the report is kept. Only
    the links change: its text is the PDF's, quoted paths included."""
    base = PurePosixPath(at).parent.as_posix()

    def new_link(m: "re.Match[str]") -> str:
        old = m.group(2)
        if re.match(r"^[a-z][a-z0-9+.-]*:|^#|^/", old):
            return m.group(0)
        rel = old.strip().lstrip("./")
        for cand in (f"artifacts/{rel}", f"artifacts/extra/{rel}", f"result/{rel}", f"artifacts/report/{rel}"):
            dest = folder.dest_of(cand)
            if dest:
                return m.group(1) + _link(posixpath.relpath(dest, base)) + m.group(3)
        return m.group(0)

    return _MD_LINK.sub(new_link, md)


def build_report_folder(run_dir: Path, dest: Path, *, question: str = "", key_results: str = "",
                        limitations: str = "") -> list[str]:
    """Write the report folder of ``run_dir`` at ``dest`` (which must not exist or be empty) and
    return what is wrong with it; an empty list means it is valid. Files are hard-linked when the
    filesystem allows it, so a run's datasets are not copied, and copied otherwise. The run's own
    folders are not changed. ``key_results`` and ``limitations`` are the caller's (a model's); left
    empty, they come from the summary the run wrote."""
    run_dir, dest = Path(run_dir), Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} is not empty")
    folder = plan_report_folder(run_dir)
    plan_text = next((e.text or "" for e in folder.entries if e.dest == "plan.yaml"), "")
    if not key_results or not limitations:
        old_key, old_limits = existing_summary_parts(run_dir)
        key_results = key_results or old_key or curation_result(run_dir)
        limitations = limitations or old_limits
    doc = _plan_doc(plan_text)
    question = question or str(doc.get("request") or doc.get("question") or doc.get("task") or "")
    summary = compose_summary(folder, question=question, key_results=rewrite_links(key_results, folder),
                              limitations=rewrite_links(limitations, folder),
                              method=method_and_inputs(run_dir, plan_text, folder))
    folder.entries.append(Entry(dest="summary.md", text=summary, size=len(summary.encode("utf-8"))))

    dest.mkdir(parents=True, exist_ok=True)
    for e in folder.entries:
        target = dest / e.dest
        target.parent.mkdir(parents=True, exist_ok=True)
        if e.text is None and e.origin.startswith("artifacts/report/") and e.dest.endswith(".md"):
            try:
                text = e.source.read_text(encoding="utf-8")         # type: ignore[union-attr]
                moved = report_markdown_links(text, folder, e.dest)
            except (OSError, UnicodeDecodeError):
                moved = text = None
            if moved is not None and moved != text:
                target.write_text(moved, encoding="utf-8")
                continue
        if e.text is not None:
            text = rewrite_links(e.text, folder) if e.dest == "plan.md" else e.text
            target.write_text(text, encoding="utf-8")
        else:
            try:
                os.link(e.source, target)                       # type: ignore[arg-type]
            except OSError:
                shutil.copy2(e.source, target)                  # type: ignore[arg-type]
    return check_report_folder(dest, expect_plan=folder.plan_state != "none")


# Already compressed, or large and binary: stored in the zip as they are.
_STORED_SUFFIXES = DATASET_SUFFIXES | {".gz", ".png", ".jpg", ".jpeg", ".pdf", ".zip", ".xlsx", ".parquet"}


def zip_report_folder(folder_dir: Path, out: Any, top: str) -> int:
    """Write the report folder at ``folder_dir`` into the open binary file ``out`` as a zip whose
    paths start with ``top/``. Returns the number of files written."""
    import zipfile

    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in Path(folder_dir).rglob("*") if p.is_file()):
            how = zipfile.ZIP_STORED if _suffix(path.name) in _STORED_SUFFIXES else zipfile.ZIP_DEFLATED
            zf.write(path, f"{top}/{path.relative_to(folder_dir).as_posix()}", compress_type=how)
            n += 1
    return n


def check_report_folder(dest: Path, *, expect_plan: bool = True) -> list[str]:
    """What is wrong with a report folder: a JSON or workflow file, a process folder, a second YAML
    file, a missing part, an empty folder, a link that leads nowhere, a summary out of form."""
    dest = Path(dest)
    problems: list[str] = []
    files = [p for p in dest.rglob("*") if p.is_file()]
    rels = {p.relative_to(dest).as_posix() for p in files}
    yamls = sorted(r for r in rels if r.endswith((".yaml", ".yml")))
    if expect_plan and yamls != ["plan.yaml"]:
        problems.append(f"expected exactly one YAML file, plan.yaml; found {yamls}")
    if not expect_plan and yamls:
        problems.append(f"unexpected YAML files: {yamls}")
    for r in sorted(rels):
        name = PurePosixPath(r).name
        if name.endswith(".json"):
            problems.append(f"JSON file in the report: {r}")
        if name in WORKFLOW_NAMES or name.endswith(".dot"):
            problems.append(f"workflow file in the report: {r}")
        if "process" in PurePosixPath(r).parts[:-1]:
            problems.append(f"process folder in the report: {r}")
    needed = ["summary.md"] + (["plan.md", "plan.yaml"] if expect_plan else [])
    problems += [f"missing {name}" for name in needed if name not in rels]
    for top in sorted(p.name for p in dest.iterdir()):
        if top not in ("summary.md", "plan.md", "plan.yaml", "results"):
            problems.append(f"unexpected entry at the top of the report: {top}")
    for d in sorted(p for p in dest.rglob("*") if p.is_dir()):
        if not any(d.iterdir()):
            problems.append(f"empty folder: {d.relative_to(dest).as_posix()}")
    for name in ("summary.md", "plan.md"):
        if name not in rels:
            continue
        md = (dest / name).read_text(encoding="utf-8")
        for m in _MD_LINK.finditer(md):
            target = m.group(2)
            if re.match(r"^[a-z][a-z0-9+.-]*:|^#", target):
                continue
            path = re.sub(r"%([0-9A-Fa-f]{2})", lambda x: chr(int(x.group(1), 16)), target.split("#")[0])
            if not (dest / path).exists():
                problems.append(f"{name} links to {target}, which is not in the report")
        if name == "summary.md":
            problems += check_summary(md)
    return problems
