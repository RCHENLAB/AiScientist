"""Read an explicit ordered registered MCP call list without inventing a study.

This is a plan input format, not an execution escape hatch. The live catalog's
schemas validate it, and the normal plan review and guarded tool dispatch remain.
Ordinary literature questions still use the dedicated search/curation routes.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

_ITEM = re.compile(r"(?m)^\s*(\d+)[.)]\s+`?(mcp_[A-Za-z0-9_]+)`?\s*(\{)")
_EXECUTE = re.compile(r"\b(?:execute|run|call)\b[^.\n]{0,100}\b(?:following|listed|these|below)\b[^.\n]{0,80}\btools?\b|"
                      r"(?:执行|运行|调用)(?:以下|下列|下面)[^。\n]{0,60}(?:工具|MCP)", re.I)
_DESCRIBE = re.compile(r"\b(?:explain|describe|example|hypothetical)\b|\bwhat (?:would|if)\b|解释|示例|假设", re.I)
_REFUSE = re.compile(r"\b(?:do not|don't|never|without)\s+(?:execute|run|call)\b|"
                     r"(?:不要|不用|别|不)(?:执行|运行|调用)", re.I)


@dataclass(frozen=True)
class ExplicitCall:
    name: str
    arguments: dict[str, Any]

    def step(self, position: int) -> str:
        # Nested data and source code belong in the document's typed settings,
        # not the legacy prose argument parser. The plan UI displays both.
        return (f"**{position}. {self.name}** — Run `{self.name}` with the exact JSON settings shown for this step. "
                "Report its actual completion or failure and artifact links; preserve any budget/pilot refusal.")



def explicit_workflow(question: str) -> tuple[ExplicitCall, ...]:
    """Recognize a complete positive, numbered call list for a registered service.

    No generic prefix stripping: only names owned by the configured analysis
    adapter qualify. Descriptions, negated commands and partial JSON are not plans.
    """
    matches = list(_ITEM.finditer(question or ""))
    if not matches:
        return ()
    header = question[:matches[0].start()]
    if not _EXECUTE.search(header) or _REFUSE.search(header) or _DESCRIBE.search(header):
        return ()
    from .mcp_bridge import configured_servers
    names = {server.planner_name(tool["name"]) for server in configured_servers()
             if server.adapter == "analysis"
             for tool in server.tools}
    calls = []
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate argument")
            result[key] = value
        return result

    decoder = json.JSONDecoder(object_pairs_hook=unique_object,
                               parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    for i, match in enumerate(matches):
        if int(match[1]) != i + 1 or match[2] not in names:
            return ()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(question)
        body = question[match.start(3):end]
        try:
            arguments, consumed = decoder.raw_decode(body)
        except ValueError:
            return ()
        if not isinstance(arguments, dict) or body[consumed:].strip():
            return ()
        calls.append(ExplicitCall(match[2], arguments))
    return tuple(calls)


def validated_agenda(calls: tuple[ExplicitCall, ...], catalog: list[Any]) -> list[str]:
    from jsonschema import Draft202012Validator
    tools = {tool.name: tool for tool in catalog}
    for call in calls:
        if call.name not in tools or not getattr(tools[call.name], "enabled", True):
            raise ValueError(f"Requested registered tool is unavailable in this session: {call.name}")
        error = next(Draft202012Validator(tools[call.name].parameters).iter_errors(call.arguments), None)
        if error is not None:
            location = ".".join(map(str, error.path)) or "arguments"
            raise ValueError(f"Invalid requested arguments for {call.name}: {location}")
    return [call.step(i) for i, call in enumerate(calls, 1)]
