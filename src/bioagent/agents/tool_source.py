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
import textwrap
from typing import Any, Callable

from .research_harness import HarnessContext, HarnessTool

# A tool implementation is normally 50-150 lines. The cap exists so that one call cannot eat a
# large share of the window on a module-level fetch; a truncated body says so explicitly rather
# than trailing off, because a silently cut function reads as a complete one.
MAX_CHARS = 20_000


def _unwrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Follow ``functools.wraps`` / closures to the function that actually holds the source.

    Several tools are built by factories (``make_run_code_tool(...)``) and their executor is a
    closure. ``inspect.getsource`` on the closure gives the factory body, which is the honest
    answer — it IS the code that runs — so no special handling is needed beyond unwrapping
    decorators.
    """
    return inspect.unwrap(fn)


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
        fn = _unwrap(tool.executor)

        symbol = str(args.get("symbol", "")).strip()
        module = inspect.getmodule(fn)
        target: Any = fn
        if symbol:
            # Helpers a tool leans on (`_write_table`, `_slug`, a threshold constant) are where
            # behaviour often actually lives, so they must be reachable too.
            if module is None or not hasattr(module, symbol):
                return {"error": f"{name!r} does not resolve a symbol named {symbol!r}",
                        "module": getattr(module, "__name__", "<unknown>"),
                        "hint": "call without `symbol` first to read the tool body and see what it calls"}
            target = getattr(module, symbol)

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
            # The declared contract, next to the code, so the agent can compare what the
            # description PROMISES against what the body DOES. Divergence between those two is
            # exactly the class of defect this tool exists to surface.
            "declared_description": tool.description,
            "declared_parameters": tool.parameters,
        }
        if not symbol:
            out["defaults"] = _declared_defaults(src)
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
