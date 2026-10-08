"""Let the agent READ the code of the tools it is calling.

Every analysis tool is a black box to the model: it sees a name, a prose description, and a
result dict. It cannot see that ``run_de`` capped its output at 50 genes per group, that
``run_enrichment`` used the constant 20000 as its statistical background, or that
``run_clustering`` clustered at ``resolution=1.0`` because nobody passed one. Those three
defaults survived seven weeks, produced self-consistent reports, and passed a green test
suite. They were found by a human reading the source.

That is the asymmetry this module removes. A tool description states INTENT; the source states
BEHAVIOUR, and only the second can be checked. With ``read_tool_source`` the agent can pull the
implementation of any tool in its catalog and ask the question a reviewer would ask: *what did
this actually do, and is the parameter it chose defensible for THIS dataset?*

Pairs with ``run_code``. Reading the source tells the agent what a tool did; the sandbox lets
it recompute the same quantity independently and compare. Agreement is evidence; a discrepancy
is a bug in one of them, and either answer is worth more than trusting the result dict.

Deliberately read-only. Nothing here lets the agent WRITE to the running codebase — a tool that
rewrote its own implementation mid-run would make every result in that run unreproducible. The
output of an audit is a finding for a human, or an argument for doing the step in ``run_code``
instead.
"""

from __future__ import annotations

import inspect
import json
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from ..tools import catalog as tool_catalog
from .research_harness import HarnessContext, HarnessTool

# A tool implementation is normally 50-150 lines. The cap exists so that one call cannot eat a
# large share of the window on a module-level fetch; a truncated body says so explicitly rather
# than trailing off, because a silently cut function reads as a complete one.
MAX_CHARS = 20_000


def _unwrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Follow ``functools.wraps`` to the function that actually holds the source."""
    return inspect.unwrap(fn)


def _in_folder(obj: Any, manifest: "tool_catalog.ToolManifest") -> bool:
    """True when ``obj`` is defined under the tool's own folder (``tools/<name>/``)."""
    try:
        file = inspect.getsourcefile(obj)
    except TypeError:
        return False
    return bool(file) and Path(file).resolve().is_relative_to(manifest.folder.resolve())


def _implementation(tool: HarnessTool, manifest: "tool_catalog.ToolManifest | None") -> Callable[..., Any]:
    """The function that holds the tool's behaviour, whatever the platform wrapped around it.

    A tool routed to HPC3 is registered with a dispatcher as its executor (``registry._exec``: "send
    this to the line's Slurm executor"), and that dispatcher is all ``tool.executor`` shows. Run
    78a707cd79e9 asked twice for ``run_enrichment`` and got ``registry._exec`` both times; asking for
    ``symbol="run_enrichment"`` then failed, because the lookup searched registry.py. The step ended
    without ever calling the tool. The registry now marks its dispatcher with ``__wrapped__``; for any
    other wrapper, the tool's own folder is the authority (one folder per tool), so the executor is
    rebuilt from its manifest's factory — the same function the HPC3 job imports and runs.
    """
    fn = _unwrap(tool.executor)
    if manifest is None or _in_folder(fn, manifest):
        return fn
    try:
        own = _unwrap(tool_catalog.build(manifest).executor)
    except Exception:                        # a factory that cannot build here: report what we have
        return fn
    return own if _in_folder(own, manifest) else fn


def _folder_module(manifest: "tool_catalog.ToolManifest | None") -> ModuleType | None:
    """The tool's own ``tool.py`` module (where its factory lives), or None for a platform tool."""
    if manifest is None:
        return None
    try:
        return inspect.getmodule(manifest.factory())
    except Exception:                        # an import error is reported by the main lookup
        return None


def _injected(fn: Callable[..., Any]) -> dict[str, Any]:
    """Callables the executor closed over — the code a thin dispatcher actually DELEGATES to.

    ``inspect.getsource`` on a factory-built executor returns the wrapper, which for most tools is
    the whole story. For a gateway-INJECTED one it is not: ``scgpt_annotate``'s body checks for a
    runner and calls it, and everything a reviewer would want to check — the reference, the
    preprocessing, the bindings — lives in that runner. Run 3c5fbc8608a7's scGPT step was written
    to verify exactly those before running inference, read the tool source, got the dispatcher, and
    reported "lookup of its injected runner failed". It then withheld inference, correctly, on what
    it could see.

    Returned as a map of free-variable name -> location so the agent can fetch it with ``symbol``.
    """
    out: dict[str, Any] = {}
    names = getattr(getattr(fn, "__code__", None), "co_freevars", ()) or ()
    for name, cell in zip(names, fn.__closure__ or ()):
        try:
            val = cell.cell_contents
        except ValueError:                       # an empty cell (recursive closure being built)
            continue
        if not callable(val) or inspect.isclass(val):
            continue
        # A composite (``diagnose_disease``) closes over the ROUTED executors of the tools it
        # wraps; point at those tools' code, not at the HPC dispatcher in front of them.
        val = _unwrap(val)
        try:
            out[name] = {"where": f"{inspect.getsourcefile(val)}:{inspect.getsourcelines(val)[1]}",
                         "qualname": getattr(val, "__qualname__", getattr(val, "__name__", name))}
        except (OSError, TypeError):
            out[name] = {"where": "<unavailable>",
                         "qualname": getattr(val, "__qualname__", name)}
    return out


def _source_of(obj: Any) -> tuple[str, str, int]:
    """(source, file, first line). Raises OSError/TypeError for builtins and C code."""
    src = textwrap.dedent(inspect.getsource(obj))
    try:
        file = inspect.getsourcefile(obj) or "<unknown>"
        line = inspect.getsourcelines(obj)[1]
    except (OSError, TypeError):
        file, line = "<unknown>", 0
    return src, file, line


def _truncate(src: str) -> tuple[str, bool]:
    if len(src) <= MAX_CHARS:
        return src, False
    marker = (f"\n\n# ... TRUNCATED at {MAX_CHARS} chars — fetch a single `symbol` instead of "
              "the whole module.\n")
    return src[:MAX_CHARS] + marker, True


def make_tool_source_tool(get_catalog: "Callable[[], list[HarnessTool]] | None" = None) -> HarnessTool:
    """The read-the-implementation tool.

    ``get_catalog`` returns the tools this run can call; it is a callable rather than a list so
    the tool reflects the catalog as actually assembled for the run (the gateway injects its
    own), not a catalog captured at import time.
    """

    def _exec(args: dict[str, Any], ctx: HarnessContext) -> dict[str, Any]:
        catalog: list[HarnessTool] = []
        if get_catalog is not None:
            catalog = list(get_catalog() or [])
        if not catalog:
            catalog = list(getattr(ctx, "catalog", None) or [])
        if not catalog:
            return {"error": "no tool catalog is available to introspect in this run"}

        name = str(args.get("tool", "")).strip()
        by_name = {t.name: t for t in catalog}
        if name not in by_name:
            return {"error": f"unknown tool {name!r}", "available": sorted(by_name)}
        tool = by_name[name]
        manifest = tool_catalog.manifest(name)
        fn = _implementation(tool, manifest)

        symbol = str(args.get("symbol", "")).strip()
        module = inspect.getmodule(fn)
        injected = _injected(fn)
        target: Any = fn
        if symbol:
            # Helpers a tool leans on (`_write_table`, `_slug`, a threshold constant) are where
            # behaviour often actually lives, so they must be reachable too — and so must the
            # callables a dispatcher closed over, which is where an INJECTED implementation lives.
            closed = {n: c.cell_contents for n, c in
                      zip(getattr(getattr(fn, "__code__", None), "co_freevars", ()) or (),
                          fn.__closure__ or ())}
            # The executor's module first, then the tool folder's own tool.py, in case the
            # executor itself is defined elsewhere (a shared ``_lib`` function).
            homes = [m for m in (module, _folder_module(manifest)) if m is not None]
            home = next((m for m in homes if hasattr(m, symbol)), None)
            if symbol in injected and symbol in closed:
                target = _unwrap(closed[symbol])
            elif home is None:
                return {"error": f"{name!r} does not resolve a symbol named {symbol!r}",
                        "module": getattr(module, "__name__", "<unknown>"),
                        "injected": injected,
                        "hint": "call without `symbol` first to read the tool body; anything listed "
                                "under `dispatches_to` can be fetched as `symbol`"}
            else:
                target, module = getattr(home, symbol), home

        try:
            src, file, line = _source_of(target)
        except (OSError, TypeError) as exc:
            return {"error": f"source unavailable for {name}{'.' + symbol if symbol else ''}: "
                             f"{type(exc).__name__}: {exc}"}
        src, truncated = _truncate(src)

        out: dict[str, Any] = {
            "tool": name,
            "symbol": symbol or getattr(fn, "__name__", name),
            "module": getattr(module, "__name__", "<unknown>"),
            "file": file,
            "first_line": line,
            "source": src,
            "truncated": truncated,
            # Names this body delegates to. Empty for a self-contained tool; for an injected one
            # this is where the behaviour actually is, and each is fetchable as `symbol`.
            "dispatches_to": injected,
            # The declared contract, next to the code, so the agent can compare what the
            # description PROMISES against what the body DOES. Divergence between those two is
            # exactly the class of defect this tool exists to surface.
            "declared_description": tool.description,
            "declared_parameters": tool.parameters,
        }
        if manifest is not None:
            out["runs_on"] = manifest.runs_on
            if manifest.runs_on.startswith("hpc:"):
                # Run 78a707cd79e9, given only the dispatcher, went looking for the real file with
                # run_shell — on HPC3, at the gateway's path — and failed.
                out["note"] = ("This tool runs as a Slurm job on HPC3, and the job imports this same "
                               "source. `file` is its path on the gateway host, not on HPC3: fetch "
                               "helpers with `symbol` instead of searching for the file with run_shell.")
        if not symbol:
            out["defaults"] = _merge_defaults(tool.parameters, src)
            out["review_prompt"] = (
                "Check the code against THIS dataset, not in the abstract. Most defaults here are "
                "conventional and correct — the common and expected answer is 'no problem', and "
                "flagging a sound step costs as much as missing a bad one, because an audit that "
                "objects to everything gets ignored. Only report a problem when you can name (a) "
                "the specific parameter, (b) the concrete wrong OUTPUT it produces on this "
                "dataset, and (c) which downstream step consumes that output and is damaged by "
                "it. 'A different value might be better' is not a problem; 'this silently caps / "
                "truncates / assumes something false about this data, and step X needs what was "
                "dropped' is. Go through EVERY entry in `defaults` and report EVERY one that "
                "qualifies, not just the first you notice — a step can have more than one "
                "defect, and the one you happen to see first is not necessarily the worst. Also "
                "check whether the body does what `declared_description` promises. If a value "
                "looks wrong, recompute it independently with run_code and compare — do not just "
                "assert it."
            )
        return out

    return HarnessTool(
        "read_tool_source",
        "Read the ACTUAL SOURCE CODE of one of your own analysis tools. A tool's description "
        "states its intent; only the source states its behaviour — the caps, thresholds and "
        "defaults that decide the numbers you are about to report. Call "
        "`read_tool_source(tool=\"run_de\")` for the implementation plus every literal default "
        "it applies, or add `symbol=\"_write_table\"` to read a helper it calls. Use it when a "
        "result surprises you, when a number will go into the report and you did not choose the "
        "parameter that produced it, or when a later step needs something this step may have "
        "silently truncated. Reading is free and cannot change the run; pair it with run_code to "
        "recompute a suspicious quantity independently and compare.",
        {"type": "object", "properties": {
            "tool": {"type": "string", "description": "name of the tool to read"},
            "symbol": {"type": "string",
                       "description": "optional helper/constant in the same module to read instead"},
        }, "required": ["tool"]},
        _exec,
        reads_private_data=False, category="control",
    )


# --- default extraction -------------------------------------------------------


def _merge_defaults(parameters: dict[str, Any], src: str) -> list[dict[str, Any]]:
    """Every default this tool applies, DECLARED ones first.

    Two sources, because tools are in two states. A tool that declares its parameters in the schema
    (``default`` + ``description`` per property) is the authority: the value is exact rather than a
    scraped source token, and it carries the plain-English meaning the reviewer actually needs —
    "20 percent mitochondrial reads" is reviewable, ``args.get("max_pct_mt", 20.0)`` is a grep hit.
    A tool that has not adopted that yet still gets the source scrape below, which is where this
    started and what it can fall back to. Declared entries win on name collision."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    lines = src.splitlines()
    for name, spec in ((parameters or {}).get("properties") or {}).items():
        if not isinstance(spec, dict) or "default" not in spec:
            continue
        seen.add(name)
        # Point at where the body reads it, so the reviewer can see the use, not just the value.
        line_no = next((i for i, ln in enumerate(lines, start=1) if f'"{name}"' in ln), 1)
        out.append({"param": name, "default": json.dumps(spec["default"]),
                    "value": spec["default"], "line": line_no,
                    "meaning": str(spec.get("description") or ""), "declared": True})
    for entry in _declared_defaults(src):
        if entry["param"] not in seen:
            out.append({**entry, "declared": False})
    return out


_DEFAULT_CALLS = ("args.get(", "kwargs.get(")


def _declared_defaults(src: str) -> list[dict[str, Any]]:
    """Every ``args.get("x", <literal>)`` in a tool body, as {param, default, line}.

    A crude parse on purpose: it is a POINTER for the agent ("these values were chosen by
    nobody"), not an authority. It reads the same text a human reviewer would scan first, and
    surfacing them in a structured field is what turns "read the code" from an instruction the
    model can skip into a list it has to look at.
    """
    found: list[dict[str, Any]] = []
    for i, raw in enumerate(src.splitlines(), start=1):
        line = raw.strip()
        for call in _DEFAULT_CALLS:
            start = line.find(call)
            while start != -1:
                inner = line[start + len(call):]
                depth, end = 1, -1
                for j, ch in enumerate(inner):
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            end = j
                            break
                if end > 0:
                    parts = _split_top_level(inner[:end])
                    if len(parts) >= 2:
                        found.append({
                            "param": parts[0].strip().strip("\"'"),
                            "default": parts[1].strip(),
                            "line": i,
                        })
                start = line.find(call, start + 1)
    return found


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside brackets or quotes."""
    out, buf, depth, quote = [], [], 0, ""
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out
