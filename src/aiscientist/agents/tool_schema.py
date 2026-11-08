"""Planner views of MCP schemas, without replacing the schema used for calls.

A plan pins some of a tool's arguments; the rest are supplied when the step runs. So a plan
setting is checked on its own: its value against its property's schema, and its name against the
properties the tool declares when the schema closes the object (``additionalProperties: false``).
Keywords that relate several arguments (``required``, ``if``/``then``, a root ``oneOf``,
``dependentRequired`` and the like) are left to the call: every call is validated against the
whole schema before a job is scheduled. A server whose schemas need more than this at plan time
gets it when one exists.
"""
from __future__ import annotations

import json
import re
from typing import NamedTuple

TOOL_NAME = r"[A-Za-z_][A-Za-z0-9_-]{0,63}"
NAME_LEFT = r"(?<![A-Za-z0-9_-])"
NAME_RIGHT = r"(?![A-Za-z0-9_-])"
REPORT_TOOL = "write_report"  # gateway-owned plan step; never a Scientist/MCP callable


class MCPInputSchema(dict):
    """An MCP tool's input schema, as registered: plan settings are checked with PlanParameters."""


class SettingError(NamedTuple):
    message: str
    absolute_path: tuple = ()


class PlanParameters(dict):
    """``{argument: its schema}`` for an MCP tool's top-level properties, with ``.schema`` the
    whole input schema and ``.errors(settings)`` the plan-time check."""

    def __init__(self, schema: dict):
        self.schema = schema
        super().__init__(schema.get("properties") or {})

    def _specs(self, name: str) -> list:
        """The schemas a value of ``name`` must satisfy; ``None`` when the name is not allowed."""
        specs = [self[name]] if name in self else []
        specs += [spec for pattern, spec in (self.schema.get("patternProperties") or {}).items()
                  if re.search(pattern, name)]
        if specs:
            return specs
        extra = self.schema.get("additionalProperties", True)
        if extra is False:
            return None
        return [extra] if isinstance(extra, dict) else []

    def errors(self, settings: dict) -> list[SettingError]:
        from jsonschema import Draft202012Validator

        try:
            json.dumps(settings, allow_nan=False)
        except (TypeError, ValueError) as exc:
            return [SettingError(f"Settings must be strict JSON: {exc}")]
        # Local references in a property schema (#/$defs/...) resolve against the tool's schema.
        defs = {key: self.schema[key] for key in ("$defs", "definitions") if key in self.schema}
        out: list[SettingError] = []
        for name, value in settings.items():
            specs = self._specs(name)
            if specs is None:
                allowed = ", ".join(sorted(self)) or "none"
                out.append(SettingError(f"{name!r} is not an argument of this tool (arguments: {allowed})",
                                        (name,)))
                continue
            for spec in specs:
                document = {**defs, **spec} if isinstance(spec, dict) else spec
                try:
                    found = list(Draft202012Validator(document).iter_errors(value))
                except Exception:  # an unresolvable reference: the call's full validation decides
                    continue
                out.extend(SettingError(error.message, (name, *error.absolute_path)) for error in found)
        return out


def plan_parameters(schema: dict) -> dict:
    if isinstance(schema, MCPInputSchema):
        return PlanParameters(schema)
    return schema.get("properties") or {}
