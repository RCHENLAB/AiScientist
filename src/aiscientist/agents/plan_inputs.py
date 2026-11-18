"""Tools that read an input the chat must already hold, checked when a plan is drafted.

The atlas tools read the chat's attached atlas and variant annotation reads an attached VCF. A
plan that names one of them without that input used to be drafted, reviewed and started, and
failed at its first call. The check here is deliberately loose: it reports a gap only when
nothing attached to the run could be the input, so a run that would have worked is never refused.
"""
from __future__ import annotations

import posixpath
import re
from typing import Any, Iterable

_VCF = (".vcf", ".vcf.gz", ".vcf.bgz", ".bcf")
_PATH_KEYS = ("dataset_path", "dataset_source", "hpc_primary", "server_primary", "dataset_root",
              "hpc_dataset_root", "input_name", "datasets", "input_folders", "shared_inputs")

ATLAS_REASON = ("it reads an atlas attached to the chat, and none is attached. Attach one of the "
                "shared datasets (\"+ Data\"), then send the question again.")
VARIANT_REASON = ("it reads a VCF attached to the chat, and none is attached. Upload the VCF "
                  "(\"+ Data\") or bind it from a server folder, then send the request again.")


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _strings(item)


def attached(decisions: "dict[str, Any] | None") -> list[str]:
    """Every path or file name the run was started with."""
    decisions = decisions or {}
    return [s for key in _PATH_KEYS for s in _strings(decisions.get(key)) if s]


def _is_atlas(path: str) -> bool:
    from .atlas_expression import configured_atlases, shared_release

    if not path.endswith(".h5ad"):
        return False
    return posixpath.normpath(path) in configured_atlases() or bool(shared_release(path))


def missing_inputs(tool_names: "Iterable[str] | None", decisions: "dict[str, Any] | None") -> dict[str, str]:
    """``{tool: why it cannot run in this chat}`` for the tools whose input is absent.

    ``tool_names`` limits the answer to those tools; ``None`` covers every configured one. A plan
    is checked against all of them: a tool whose input is missing may not be registered for the
    run at all (variant annotation is not, in a chat with no file), and a preset pipeline's
    guidance still names it."""
    from .atlas_expression import SKILL as ATLAS_SKILL
    from .mcp_bridge import configured_servers

    names = None if tool_names is None else set(tool_names)
    paths = attached(decisions)
    gaps: dict[str, str] = {}
    for server in configured_servers():
        owned = [n for n in (server.planner_name(tool["name"]) for tool in server.tools)
                 if names is None or n in names]
        if not owned:
            continue
        if server.skill == ATLAS_SKILL:
            if not any(_is_atlas(p) for p in paths):
                gaps.update({n: ATLAS_REASON for n in owned})
        elif server.name == "variant-annotation":
            modality = str((decisions or {}).get("content_modality") or "").lower()
            if not (any(p.lower().endswith(_VCF) for p in paths) or "variant" in modality or "vcf" in modality):
                gaps.update({n: VARIANT_REASON for n in owned if not n.endswith("_job_status")})
    return gaps


def steps_missing_input(agenda: Iterable[str], gaps: dict[str, str]) -> list[tuple[int, str]]:
    """``(step number, tool)`` for each step that calls a tool whose input is absent."""
    found = []
    for index, step in enumerate(agenda, 1):
        called = re.findall(r"`([A-Za-z_][A-Za-z0-9_-]*)(?:\s*\([^`]*\))?`", str(step or ""))
        tool = next((name for name in called if name in gaps), None)
        if tool:
            found.append((index, tool))
    return found


def refusal(found: list[tuple[int, str]], gaps: dict[str, str]) -> str:
    tool = found[0][1]
    return (f"Nothing was planned or run: this request needs `{tool}`, and {gaps[tool]}")
