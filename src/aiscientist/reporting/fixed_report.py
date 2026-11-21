"""The run's report from the reports its MCP servers wrote themselves (RCHENLAB/AiScientist#102).

Jin Li: a skill that needs a report should get it from its server's own report function, in a fixed
format, not from a model summarising the results. A server names that tool in ``mcp_servers.json``
(``report``); the plan's report step then calls it (``research_lab.step_kinds.fixed_report_step``),
and this module finds what those calls wrote and makes the run's report from it, by code only: each
server's Markdown as it wrote it, one after another, with its figure links named from the run's folder
so the renderer finds them.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterable

REPORT_NAME = "report.md"

# A Markdown image or link: ``![label](target)`` / ``[label](target)``; the target has no spaces.
_LINK_RE = re.compile(r"(!?\[[^\]\n]*\]\()([^)\s]+)(\))")


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _call_reports(call: dict, art: Path) -> "list[Path]":
    """The report files one report call wrote, each once: the ``.../report.md`` its result names (a
    path in the run's folder, as the adapters rewrite them), else the one in a folder named by its id
    argument (``query_id``, ``curation_id``), for a response the transport bound cut short. A result
    may name the same file more than once: Atlas's names it as its ``file`` and in its ``artifacts``."""
    root = art.resolve()
    named = [s for s in _strings(call.get("result")) if s.endswith("/" + REPORT_NAME)
             and not s.startswith("/") and ".." not in Path(s).parts]
    found = [p for p in ((root / s).resolve() for s in named) if p.is_file() and _inside(p, root)]
    if found:
        return list(dict.fromkeys(found))
    ids = [v for v in (call.get("args") or {}).values()
           if isinstance(v, str) and v and "/" not in v and v not in (".", "..")]
    return sorted({p.resolve() for v in ids for p in root.glob(f"*/{v}/{REPORT_NAME}")
                   if p.is_file() and _inside(p.resolve(), root)})


def report_files(result: Any, art: Path, *, is_report_step: Callable[[str], bool],
                 tools: "set[str]", succeeded: Callable[[dict], bool]) -> "list[Path]":
    """The report files the accepted report steps of ``result`` wrote with one of ``tools``, in run
    order, each once. A report tool called inside an analysis step is that step's output, not the
    run's report, so only steps ``is_report_step`` recognises count."""
    found: "list[Path]" = []
    for rnd in getattr(result, "rounds", None) or []:
        verdict = getattr(getattr(rnd, "verdict", None), "verdict", "")
        if verdict != "accept" or not is_report_step(getattr(rnd, "step", "") or ""):
            continue
        calls = (getattr(rnd, "scientist_result", None) or {}).get("steps") or []
        for call in calls:
            if not isinstance(call, dict) or call.get("tool") not in tools or not succeeded(call):
                continue
            found += [p for p in _call_reports(call, art) if p not in found]
    return found


def _relink(text: str, folder: str, art: Path) -> str:
    """Name each relative link of a report written in ``folder`` from the run's folder."""
    root = art.resolve()

    def fix(m: "re.Match[str]") -> str:
        target = m.group(2)
        if target.startswith(("#", "/")) or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
            return m.group(0)
        moved = f"{folder}/{target}"
        path = (root / moved).resolve()
        return m.group(1) + moved + m.group(3) if _inside(path, root) and path.exists() else m.group(0)

    return _LINK_RE.sub(fix, text)


def assemble(art: Path, files: "list[Path]") -> str:
    """The run's report: each file's Markdown, unchanged but for its links, in order."""
    root = art.resolve()
    parts = []
    for path in files:
        folder = path.resolve().parent.relative_to(root).as_posix()
        parts.append(_relink(path.read_text(encoding="utf-8").strip(), folder, art))
    return "\n\n".join(p for p in parts if p) + "\n"


SOURCES_RECORD = "process/fixed_report.json"


def recorded_sources(art: Path) -> "list[Path]":
    """The report files a run's fixed-format report was made from, as it recorded them
    (:data:`SOURCES_RECORD`), each once and each one that is still a file in the run's folder: where
    it was named, or in ``extra/``, where the bundle cleanup moves a server's folder (a run that
    recorded the place before the move). Empty when the run's report was not one (a model wrote it,
    or there was none)."""
    import json

    root = art.resolve()
    try:
        names = json.loads((art / SOURCES_RECORD).read_text(encoding="utf-8")).get("sources") or []
    except (OSError, ValueError, AttributeError):
        return []
    found: "list[Path]" = []
    for s in names:
        if not isinstance(s, str) or not s or s.startswith("/") or ".." in Path(s).parts:
            continue
        for cand in ((root / s).resolve(), (root / "extra" / s).resolve()):
            if cand.is_file() and _inside(cand, root):
                found.append(cand)
                break
    return list(dict.fromkeys(found))
