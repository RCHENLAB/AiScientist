"""Discover the tools from their folders: one ``tools/<name>/TOOL.md`` per model-callable tool.

A tool folder holds its code (``tool.py``) and a ``TOOL.md`` whose front matter is the manifest
the platform needs and whose body is the documentation people read::

    ---
    name: run_de                 # the name the model calls; equals the folder name
    summary: Differential expression between groups of cells (Wilcoxon), per cluster or A vs B.
    category: analysis           # must equal the HarnessTool's category
    runs_on: hpc:analysis        # where the executor runs: see RUNS_ON
    entry: tool:make_tool        # <module in this folder>:<factory>   (this is the default)
    needs: []                    # factory arguments the platform injects: see build()
    chat: false                  # also offered on the fast chat path
    order: 220                   # position in the Scientist's catalog (ascending)
    line: scrna                  # the analysis line it belongs to (optional)
    owner: analysis              # the team line that maintains it
    ---
    # run_de
    ...documentation...

Adding a tool is adding a folder: the registry, the HPC3 routing, the fast-chat selection and the
System page all read these manifests, so no platform file names a tool. The parser is standard
library only, because the HPC3 job images import this module too.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

from .sdk import HarnessTool

TOOLS_DIR = Path(__file__).resolve().parent
PACKAGE = __name__.rsplit(".", 1)[0]          # "aiscientist.tools"

# Where a tool's executor runs. ``inprocess``: in the gateway process. ``hpc:<line>``: as a Slurm
# job on HPC3 through that line's executor (the platform falls back in-process when it has none).
# ``gpu:scgpt``: through a runner the platform injects (``needs: [scgpt_runner=scgpt_runner]``).
RUNS_ON = ("inprocess", "hpc:analysis", "hpc:variant", "hpc:phenotype", "hpc:literature", "gpu:scgpt")
CATEGORIES = ("qc", "analysis", "annotation", "literature", "figure")
_REQUIRED = ("name", "summary", "category", "runs_on", "order")


class ManifestError(ValueError):
    """A TOOL.md that is missing a field, names an unknown value, or does not match its folder."""


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """``(fields, body)`` from a Markdown file that starts with a ``---`` front-matter block.

    Supports exactly what the manifests use: ``key: value`` with strings, integers and booleans,
    inline lists ``[a, b]``, quotes, trailing ``# comments``, and indented continuation lines that
    fold into the previous value. Anything else is an error rather than a guess."""
    if not text.startswith("---"):
        raise ManifestError("no front matter (the file must start with ---)")
    lines = text.splitlines()
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ManifestError("front matter is not closed with ---") from None
    fields: dict[str, Any] = {}
    last: str | None = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1].isspace():
            if last is None or not isinstance(fields[last], str):
                raise ManifestError(f"continuation line without a text field: {raw!r}")
            fields[last] = (fields[last] + " " + _strip_comment(raw).strip()).strip()
            continue
        m = re.match(r"([A-Za-z_][\w-]*)\s*:\s*(.*)$", raw)
        if not m:
            raise ManifestError(f"cannot read front-matter line: {raw!r}")
        key, value = m.group(1), _strip_comment(m.group(2)).strip()
        fields[key] = _scalar_or_list(value)
        last = key
    return fields, "\n".join(lines[end + 1:]).lstrip("\n")


def _strip_comment(value: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out)


def _scalar_or_list(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [_scalar(v.strip()) for v in inner.split(",")] if inner else []
    return _scalar(value)


def _scalar(value: str) -> Any:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    low = value.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


@dataclass(frozen=True)
class ToolManifest:
    """One tool folder's manifest (the front matter of its ``TOOL.md``)."""

    name: str
    summary: str
    category: str
    runs_on: str
    order: int
    folder: Path
    entry: str = "tool:make_tool"
    needs: tuple[str, ...] = ()
    chat: bool = False
    line: str = ""
    owner: str = ""
    doc: str = field(default="", repr=False, compare=False)

    @property
    def module(self) -> str:
        return f"{PACKAGE}.{self.folder.name}.{self.entry.split(':', 1)[0]}"

    def factory(self) -> Callable[..., Any]:
        mod, fn = self.entry.split(":", 1)
        return getattr(importlib.import_module(f"{PACKAGE}.{self.folder.name}.{mod}"), fn)

    def injected(self) -> list[tuple[str, str]]:
        """``needs`` as ``(factory keyword, source)`` pairs, e.g. ``("lirical_fn", "tool:run_lirical")``."""
        out = []
        for need in self.needs:
            kw, _, src = need.partition("=")
            out.append((kw.strip(), (src or kw).strip()))
        return out

    @property
    def composite(self) -> bool:
        """True when it composes other tools' FINAL executors (built after the platform routes them)."""
        return any(src.startswith("tool:") for _kw, src in self.injected())


def load_manifest(folder: Path) -> ToolManifest:
    path = folder / "TOOL.md"
    fields, body = parse_front_matter(path.read_text(encoding="utf-8"))
    missing = [k for k in _REQUIRED if k not in fields]
    if missing:
        raise ManifestError(f"{path}: missing {', '.join(missing)}")
    if fields["name"] != folder.name:
        raise ManifestError(f"{path}: name {fields['name']!r} differs from its folder {folder.name!r}")
    if fields["runs_on"] not in RUNS_ON:
        raise ManifestError(f"{path}: runs_on {fields['runs_on']!r} is not one of {RUNS_ON}")
    if fields["category"] not in CATEGORIES:
        raise ManifestError(f"{path}: category {fields['category']!r} is not one of {CATEGORIES}")
    known = set(_REQUIRED) | {"entry", "needs", "chat", "line", "owner"}
    unknown = set(fields) - known
    if unknown:
        raise ManifestError(f"{path}: unknown field(s) {sorted(unknown)}")
    needs = fields.get("needs") or []
    return ToolManifest(
        name=fields["name"], summary=str(fields["summary"]), category=fields["category"],
        runs_on=fields["runs_on"], order=int(fields["order"]), folder=folder,
        entry=str(fields.get("entry") or "tool:make_tool"),
        needs=tuple(str(n) for n in (needs if isinstance(needs, list) else [needs])),
        chat=bool(fields.get("chat", False)), line=str(fields.get("line") or ""),
        owner=str(fields.get("owner") or ""), doc=body)


@lru_cache(maxsize=1)
def manifests() -> tuple[ToolManifest, ...]:
    """Every tool folder's manifest, in catalog order. Folders starting with ``_`` are not tools."""
    found = [load_manifest(p) for p in sorted(TOOLS_DIR.iterdir())
             if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "TOOL.md").is_file()]
    orders: dict[int, str] = {}
    for m in found:
        if m.order in orders:
            raise ManifestError(f"{m.name} and {orders[m.order]} share order {m.order}")
        orders[m.order] = m.name
    return tuple(sorted(found, key=lambda m: m.order))


def manifest(name: str) -> ToolManifest | None:
    return next((m for m in manifests() if m.name == name), None)


def build(m: ToolManifest, deps: dict[str, Any] | None = None) -> HarnessTool:
    """Call the tool's factory with what its manifest ``needs``. ``deps`` maps a source name
    (``scgpt_runner``, ``tool:run_lirical``, ...) to the object to inject; a missing one is passed
    as ``None`` (every factory degrades to an honest not-enabled result without it)."""
    deps = deps or {}
    tool = m.factory()(**{kw: deps.get(src) for kw, src in m.injected()})
    if not isinstance(tool, HarnessTool) or tool.name != m.name:
        raise ManifestError(f"{m.folder}/TOOL.md: factory {m.entry} built {getattr(tool, 'name', tool)!r}")
    return tool


def build_tools(deps: dict[str, Any] | None = None, *,
                where: Callable[[ToolManifest], bool] | None = None,
                route: Callable[[ToolManifest, HarnessTool], HarnessTool] | None = None) -> list[HarnessTool]:
    """Build the selected tools in catalog order.

    Two phases, because a composite tool (``needs: [x=tool:<name>]``) must be bound to the FINAL
    executors of the tools it composes: first every plain tool is built and passed through
    ``route`` (where the platform swaps in its HPC3 executors); then each composite is built with
    those routed executors. Binding earlier would freeze the in-process versions."""
    chosen = [m for m in manifests() if where is None or where(m)]
    built: dict[str, HarnessTool] = {}
    for m in chosen:
        if not m.composite:
            t = build(m, deps)
            built[m.name] = route(m, t) if route else t
    for m in chosen:
        if m.composite:
            inject = dict(deps or {})
            for _kw, src in m.injected():
                if src.startswith("tool:"):
                    dep = built.get(src[5:])
                    inject[src] = dep.executor if dep is not None else None
            t = build(m, inject)
            built[m.name] = route(m, t) if route else t
    return [built[m.name] for m in chosen if m.name in built]


def line_tools(line: str) -> list[HarnessTool]:
    """The tools of one analysis line (e.g. ``scrna``), in order, built in-process."""
    return build_tools(where=lambda m: m.line == line)


def scrna_catalog() -> list[HarnessTool]:
    """The single-cell analysis line, in pipeline order: what ``scrna_pack.scrna_catalog()``
    returned before the tools moved into folders."""
    return line_tools("scrna")


def runs_on(where: str) -> list[ToolManifest]:
    return [m for m in manifests() if m.runs_on == where]


def names(ms: Iterable[ToolManifest] | None = None) -> list[str]:
    return [m.name for m in (manifests() if ms is None else ms)]
