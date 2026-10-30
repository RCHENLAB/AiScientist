"""The outputs a skill or a pipeline declares, and the check that a step left them.

RCHENLAB/AiScientist issue 10, part 1. A ``SKILL.md`` may list what it should leave behind under
``$AISCIENTIST_ARTIFACTS``, in its front matter, at the top level or under ``metadata:``::

    outputs:
      - path: tables/de_*_vs_*.csv
        kind: table
        description: One row per gene with log2 fold change, p-value and adjusted p-value.
        required: true

``path`` is a path or glob relative to the artifacts directory, ``kind`` one of :data:`KINDS`,
``description`` one sentence for the researcher, ``required`` whether the step is incomplete without
the file (true when left out). A pipeline's entry also names the ``tool`` whose step writes it.
Under ``metadata:`` the list may also be written as a JSON string, the form a registry that keeps
every metadata value a string can carry.

How the platform uses a declaration:

- the Scientist reads it with the skill (``read_skill_reference``, and the brief of a step the plan
  assigned the skill to);
- after the step, :func:`check` looks for each declared file: present, not empty, and for a figure
  not a blank image. A missing required output goes to the Critic, which may not accept the step,
  and to the technical report's Diagnostics;
- the plan shows each step's expected outputs, and the Results panel labels a produced file with
  its declared description (``process/declared_outputs.json``, written by :func:`record_step`).

A skill without ``outputs`` works exactly as before: nothing is checked and nothing is shown.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

KINDS = ("table", "figure", "h5ad", "json", "report_section", "folder")
#: The record the Results panel and the report read, under the run's artifacts directory.
RECORD_PATH = "process/declared_outputs.json"
_DESCRIPTION_MAX = 300
# A file changed less than this long before the step began still counts as the step's own: file
# systems keep mtimes to the second, and a mirrored file gets the time it was copied.
_MTIME_SLACK_S = 2.0
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(?:\s+(.*)|\s*)$")
_RECORD_LOCK = threading.Lock()


@dataclass(frozen=True)
class DeclaredOutput:
    """One file (or folder) a skill or pipeline step should leave under the artifacts directory."""

    path: str
    kind: str
    description: str
    required: bool = True
    tool: str = ""          # a pipeline's entry: the tool whose step writes it

    def to_public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "kind": self.kind,
                               "description": self.description, "required": self.required}
        if self.tool:
            out["tool"] = self.tool
        return out


# --- parsing ------------------------------------------------------------------------------------


def _unquote(value: str) -> str:
    """A YAML scalar without its quotes: ``'it''s'`` is ``it's``, a double-quoted one is read with
    its escapes (``\"``)."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except ValueError:
            return value[1:-1]
    return value


def _quoted_value(first: str, rest: "list[str]") -> "tuple[str, int]":
    """A quoted scalar that may continue on the following lines (YAML folds the line breaks into
    spaces): its text without quotes, and how many of ``rest`` it took."""
    quote = first[:1]

    def closes(text: str, opening: bool) -> bool:
        body = text[1:] if opening else text
        if quote == "'":         # '' is an escaped apostrophe: the string ends on an odd run of '
            run = len(body) - len(body.rstrip("'"))
            return run % 2 == 1
        return body.endswith('"') and not body.endswith('\\"')

    if quote not in ("'", '"') or closes(first, True):
        return _unquote(first), 0
    parts = [first]
    for used, line in enumerate(rest, 1):
        parts.append(line.strip())
        if closes(line.rstrip(), False):
            return _unquote(" ".join(parts)), used
    return _unquote(" ".join(parts)), len(rest)


def _strip_comment(value: str) -> str:
    """``value`` without a trailing YAML comment (`` # ...``), which a quoted value may contain."""
    value = value.strip()
    if value[:1] in ("'", '"'):
        end = value.rfind(value[0])
        return value[:end + 1] if end > 0 else value
    cut = value.find(" #")
    return (value[:cut] if cut != -1 else value).rstrip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def parse_block(lines: "list[str]") -> "list[dict[str, Any]]":
    """A YAML list of flat mappings, as written under ``outputs:`` (the lines after the key, still
    indented). Each ``- key: value`` starts an entry; the ``key: value`` lines indented under it
    belong to it. A value may be quoted, continue on more-indented lines, or be a block scalar
    (``description: >-``). Comments and blank lines are skipped. Anything else is not an entry."""
    items: list[dict[str, Any]] = []
    current: "dict[str, Any] | None" = None
    last_key = ""
    key_indent = -1
    block = ""           # the block-scalar marker of the key being continued, if any
    for raw in lines:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        column = _indent(raw)
        if text.startswith("- ") or text == "-":
            current = {}
            items.append(current)
            text = text[2:].strip() if text != "-" else ""
            key_indent = column = _indent(raw) + 2     # the entry's keys line up after "- "
            last_key, block = "", ""
            if not text:
                continue
        elif current is None:
            continue
        match = _KEY.match(text)
        if match and column <= key_indent:
            key, value = match.group(1), _strip_comment(match.group(2) or "")
            if value in (">", ">-", ">+", "|", "|-", "|+"):
                current[key], block = "", value
            else:
                current[key], block = _unquote(value), ""
            last_key = key
        elif last_key:
            # The continuation of the last value: a plain scalar wrapped onto the next line, or a
            # block scalar's body (``|`` keeps the line break, the others fold it into a space).
            joiner = "\n" if block.startswith("|") else " "
            prev = str(current.get(last_key) or "")
            current[last_key] = (prev + joiner + text) if prev else text
    return items


def block_after(lines: "list[str]", start: int, indent: int) -> "tuple[list[str], int]":
    """The lines after ``lines[start - 1]`` that are indented deeper than ``indent`` (blank lines
    included), and the index of the first line that is not."""
    block: list[str] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if line.strip() and _indent(line) <= indent:
            break
        block.append(line)
        i += 1
    return block, i


def _front_matter(text: str) -> str:
    stripped = (text or "").lstrip()
    if not stripped.startswith("---"):
        return ""
    rest = stripped[3:]
    end = rest.find("\n---")
    return rest[:end] if end != -1 else ""


def raw_declaration(text: str) -> Any:
    """The ``outputs`` value of a SKILL.md's front matter, as written: a list of mappings, a JSON
    string, or None when there is none. The top level wins over ``metadata:``."""
    lines = _front_matter(text).splitlines()
    found_meta: Any = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#") or _indent(line) > 0:
            i += 1
            continue
        match = _KEY.match(line.strip())
        i += 1
        if not match:
            continue
        key, value = match.group(1), (match.group(2) or "").strip()
        if key == "outputs":
            if value:
                return _quoted_value(value, lines[i:])[0]
            block, i = block_after(lines, i, 0)
            return parse_block(block)
        if key == "metadata" and not value:
            block, i = block_after(lines, i, 0)
            found_meta = _metadata_outputs(block)
    return found_meta


def _metadata_outputs(block: "list[str]") -> Any:
    """``outputs`` inside a ``metadata:`` block: a nested list, or a (JSON) string value."""
    j = 0
    while j < len(block):
        line = block[j]
        match = _KEY.match(line.strip()) if line.strip() else None
        j += 1
        if not match or match.group(1) != "outputs":
            continue
        value = (match.group(2) or "").strip()
        if value:
            return _quoted_value(value, block[j:])[0]
        sub, _ = block_after(block, j, _indent(line))
        return parse_block(sub)
    return None


def _as_bool(value: Any) -> "bool | None":
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no", "0"):
        return False
    return None


def clean_path(path: str) -> str:
    """``path`` relative to the artifacts directory: a ``$AISCIENTIST_ARTIFACTS/`` prefix dropped."""
    p = str(path or "").strip()
    for prefix in ("$AISCIENTIST_ARTIFACTS/", "${AISCIENTIST_ARTIFACTS}/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
    return p


def entry_problem(entry: Any, *, needs_tool: bool = False) -> str:
    """Why one declared entry cannot be used ('' when it can)."""
    if not isinstance(entry, dict):
        return "an entry is not a mapping of path, kind, description and required"
    path = clean_path(entry.get("path", ""))
    where = f"output {path or '(no path)'}"
    if not path:
        return "an entry has no path"
    pure = PurePosixPath(path)
    if pure.is_absolute() or path.startswith("~") or ".." in pure.parts or "\\" in path:
        return f"{where}: the path must be relative to $AISCIENTIST_ARTIFACTS, without '..'"
    kind = str(entry.get("kind") or "").strip()
    if kind not in KINDS:
        return f"{where}: kind {kind!r} is not one of {', '.join(KINDS)}"
    description = " ".join(str(entry.get("description") or "").split())
    if not description:
        return f"{where}: no description"
    if len(description) > _DESCRIPTION_MAX:
        return f"{where}: the description is longer than {_DESCRIPTION_MAX} characters"
    if "required" in entry and _as_bool(entry.get("required")) is None:
        return f"{where}: required must be true or false"
    if needs_tool and not str(entry.get("tool") or "").strip():
        return f"{where}: a pipeline's output names the tool whose step writes it"
    unknown = set(entry) - {"path", "kind", "description", "required", "tool"}
    if unknown:
        return f"{where}: unknown field(s) {', '.join(sorted(unknown))}"
    return ""


def parse(value: Any, *, needs_tool: bool = False
          ) -> "tuple[tuple[DeclaredOutput, ...], tuple[str, ...]]":
    """The usable entries of a declaration, and a problem line for each entry left out. ``value`` is
    what :func:`raw_declaration` returned: a list of mappings, a JSON string or None."""
    if value is None or value == "" or value == []:
        return (), ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return (), ("outputs is neither a list nor a JSON list",)
    if not isinstance(value, list):
        return (), ("outputs is not a list",)
    out: list[DeclaredOutput] = []
    problems: list[str] = []
    seen: set[str] = set()
    for entry in value:
        problem = entry_problem(entry, needs_tool=needs_tool)
        if problem:
            problems.append(problem)
            continue
        path = clean_path(entry["path"])
        key = f"{entry.get('tool') or ''}::{path}"
        if key in seen:
            problems.append(f"output {path}: declared twice")
            continue
        seen.add(key)
        required = _as_bool(entry.get("required", True))
        out.append(DeclaredOutput(path=path, kind=str(entry["kind"]).strip(),
                                  description=" ".join(str(entry["description"]).split()),
                                  required=True if required is None else required,
                                  tool=str(entry.get("tool") or "").strip()))
    return tuple(out), tuple(problems)


def from_text(text: str, *, needs_tool: bool = False
              ) -> "tuple[tuple[DeclaredOutput, ...], tuple[str, ...]]":
    """The declared outputs of a SKILL.md, and the problems with the entries left out."""
    return parse(raw_declaration(text), needs_tool=needs_tool)


# --- the check after a step -------------------------------------------------------------------


@dataclass
class OutputCheck:
    """What one declared output looked like after the step."""

    declared: DeclaredOutput
    status: str                     # "ok" | "missing" | "empty" | "blank" | "invalid" | "stale"
    files: list[str] = field(default_factory=list)   # matched paths, relative to the artifacts dir
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_public(self) -> dict[str, Any]:
        out = {**self.declared.to_public(), "status": self.status, "files": self.files[:20]}
        if len(self.files) > 20:
            out["files_more"] = len(self.files) - 20
        if self.detail:
            out["detail"] = self.detail
        return out


def _matches(art: Path, pattern: str) -> "list[Path]":
    try:
        found = sorted(art.glob(pattern))
    except (ValueError, OSError):
        return []
    root = art.resolve()
    keep = []
    for p in found:
        try:
            if p.resolve().is_relative_to(root):
                keep.append(p)
        except OSError:
            continue
    return keep


def is_blank_image(path: Path) -> "bool | None":
    """True when a raster image is one flat colour (an empty canvas), False when it is not, None
    when it cannot be told (not a raster image, or Pillow is not installed)."""
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"):
        return None
    try:
        from PIL import Image  # optional: matplotlib brings it wherever figures are made
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            img.draft("L", (256, 256))
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                flat.alpha_composite(rgba)
                gray = flat.convert("L")
            else:
                gray = img.convert("L")
            gray.thumbnail((512, 512))
            lo, hi = gray.getextrema()
    except Exception:  # noqa: BLE001 - an unreadable image is reported as invalid by the caller
        return None
    return (hi - lo) <= 3


def _table_rows(path: Path) -> "int | None":
    """Data rows of a text table, counted up to 2 (header excluded); None for a binary table."""
    if path.suffix.lower() not in (".csv", ".tsv", ".txt", ".tab") and not path.name.endswith((".csv.gz", ".tsv.gz")):
        return None
    try:
        if path.name.endswith(".gz"):
            import gzip
            fh = gzip.open(path, "rt", encoding="utf-8", errors="replace")
        else:
            fh = open(path, encoding="utf-8", errors="replace")
        with fh:
            lines = 0
            for line in fh:
                if line.strip() and not line.startswith("#"):
                    lines += 1
                    if lines >= 3:
                        break
    except OSError:
        return None
    return max(0, lines - 1)


def _file_problem(path: Path, kind: str) -> "tuple[str, str]":
    """``(status, detail)`` for one matched file: ``("ok", "")`` when it holds what its kind needs."""
    try:
        size = path.stat().st_size
    except OSError:
        return "missing", "unreadable"
    if size == 0:
        return "empty", "the file is empty (0 bytes)"
    if kind == "table":
        if _table_rows(path) == 0:
            # Written, with its header: a legitimate null result ("no gene passed"), not a missing
            # output. Said to the Critic, not held against the step.
            return "ok", "header only: no data rows"
    elif kind == "figure":
        if is_blank_image(path):
            return "blank", "the image is one flat colour"
    elif kind == "json":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return "invalid", "not valid JSON"
        if value in ({}, [], None, ""):
            return "empty", "the JSON holds nothing"
    elif kind == "h5ad":
        try:
            with open(path, "rb") as fh:
                head = fh.read(8)
        except OSError:
            return "missing", "unreadable"
        if head != b"\x89HDF\r\n\x1a\n":
            return "invalid", "not an HDF5 (.h5ad) file"
    elif kind == "report_section":
        try:
            if not path.read_text(encoding="utf-8", errors="replace").strip():
                return "empty", "the section has no text"
        except OSError:
            return "missing", "unreadable"
    return "ok", ""


def _folder_problem(path: Path) -> "tuple[str, str]":
    try:
        for p in path.rglob("*"):
            if p.is_file() and p.stat().st_size > 0:
                return "ok", ""
    except OSError:
        return "missing", "unreadable"
    return "empty", "the folder holds no file with content"


def check(outputs: "Iterable[DeclaredOutput]", art: "Path | str", *,
          since: "float | None" = None) -> "list[OutputCheck]":
    """Look for each declared output under the artifacts directory ``art``.

    A glob is satisfied by any one matching file that is good (present, not empty, a figure that is
    not blank). ``since`` (a ``time.time()``) restricts the match to files written or copied back
    since then — the step's own — so a file an earlier step left behind does not stand in for this
    step's output; when only such older files match, the status is ``stale``."""
    art = Path(art)
    results: list[OutputCheck] = []
    for out in outputs:
        found = _matches(art, out.path)
        if out.kind == "folder":
            found = [p for p in found if p.is_dir()]
        else:
            found = [p for p in found if p.is_file()]
        if not found:
            results.append(OutputCheck(out, "missing", [], "no file matches"))
            continue
        rel = [p.relative_to(art).as_posix() for p in found]
        if since is not None:
            fresh = []
            for p in found:
                try:
                    newest = p.stat().st_mtime
                    if p.is_dir():
                        newest = max([newest, *(q.stat().st_mtime for q in p.rglob("*") if q.is_file())])
                except OSError:
                    continue
                if newest >= since - _MTIME_SLACK_S:
                    fresh.append(p)
            if not fresh:
                results.append(OutputCheck(out, "stale", rel,
                                           "only files from before this step match"))
                continue
            found = fresh
            rel = [p.relative_to(art).as_posix() for p in found]
        status, detail = "missing", ""
        for p in found:
            status, detail = _folder_problem(p) if out.kind == "folder" else _file_problem(p, out.kind)
            if status == "ok" and not detail:
                break
        if status != "ok" and len(found) > 1:
            detail = f"{detail} (all {len(found)} matching files)"
        results.append(OutputCheck(out, status, rel, detail))
    return results


def missing_required(results: "Iterable[OutputCheck]") -> "list[OutputCheck]":
    return [r for r in results if r.declared.required and not r.ok]


def problem_lines(results: "Iterable[OutputCheck]") -> "list[str]":
    """One line per required output the step did not leave, for the Critic and the Diagnostics."""
    lines = []
    for r in missing_required(results):
        why = {"missing": "not written", "stale": "not written by this step (only an earlier "
               "file matches)", "empty": "written but empty", "blank": "a blank image",
               "invalid": "not readable as a " + r.declared.kind}.get(r.status, r.status)
        lines.append(f"{r.declared.path} ({r.declared.kind}): {why}"
                     + (f" — {r.detail}" if r.detail and r.status not in ("missing",) else "")
                     + f". Expected: {r.declared.description}")
    return lines


# --- what the agent and the plan are shown --------------------------------------------------------


def brief_lines(outputs: "Iterable[DeclaredOutput]") -> str:
    """The declaration as lines for a brief: ``- path (kind, required): description``."""
    return "\n".join(f"- {o.path} ({o.kind}, {'required' if o.required else 'optional'}): {o.description}"
                     for o in outputs)


def for_tool(outputs: "Iterable[DeclaredOutput]", tool: "str | None") -> "list[DeclaredOutput]":
    """A pipeline's entries that the step running ``tool`` writes."""
    return [o for o in outputs if tool and o.tool == tool]


# --- the run's record, for the Results panel and the report -------------------------------------


def record_step(art: "Path | str", *, step: str, title: str, index: "int | None", source: str,
                results: "list[OutputCheck]") -> None:
    """Add (or replace) one step's check in ``process/declared_outputs.json``. Never raises: the
    record labels files in the Results panel, and a run must not fail over it."""
    if not results:
        return
    path = Path(art) / RECORD_PATH
    entry = {"step": step, "title": title, "index": index, "source": source,
             "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "outputs": [r.to_public() for r in results]}
    try:
        with _RECORD_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            steps = [s for s in (data.get("steps") or []) if isinstance(s, dict) and s.get("step") != step]
            steps.append(entry)
            steps.sort(key=lambda s: (s.get("index") is None, s.get("index") or 0))
            tmp = path.with_name("." + path.name + ".tmp")
            tmp.write_text(json.dumps({"steps": steps}, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
    except OSError as exc:
        print(f"[outputs] could not record the declared outputs of a step: {exc}")


def file_labels(art: "Path | str") -> "dict[str, dict[str, Any]]":
    """``{file path: {description, kind, step, title, source}}`` for every produced file that a
    declared output matched, from ``process/declared_outputs.json``; ``{}`` without one."""
    try:
        data = json.loads((Path(art) / RECORD_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    labels: dict[str, dict[str, Any]] = {}
    for s in data.get("steps") or []:
        if not isinstance(s, dict):
            continue
        for o in s.get("outputs") or []:
            if not isinstance(o, dict) or o.get("status") not in ("ok", "empty", "blank", "invalid"):
                continue
            for f in o.get("files") or []:
                labels.setdefault(str(f), {
                    "description": o.get("description", ""), "kind": o.get("kind", ""),
                    "step": (s.get("index") + 1) if isinstance(s.get("index"), int) else None,
                    "title": s.get("title", ""), "source": s.get("source", ""),
                    "status": o.get("status")})
    return labels
