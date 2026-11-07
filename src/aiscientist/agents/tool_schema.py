"""Planner views of MCP schemas, without replacing the schema used for calls.

Plan settings are partial arguments. Discovery can inspect local refs and
branches, but validation keeps their relationships. Unbound required arguments and
undecidable branch choices are deferred; every actual call validates the original
schema with the ordinary Draft 2020-12 validator.
"""
from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

TOOL_NAME = r"[A-Za-z_][A-Za-z0-9_-]{0,63}"
NAME_LEFT = r"(?<![A-Za-z0-9_-])"
NAME_RIGHT = r"(?![A-Za-z0-9_-])"


class MCPInputSchema(dict):
    """A JSON-serializable schema whose plan settings use full-schema validation."""


class PlanParameters(dict):
    """Property-map-compatible discovery view, retaining the full MCP schema."""

    def __init__(self, schema: dict):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        self.schema = schema
        self.validator = Draft202012Validator(schema)
        self.resolver = Registry().resolver_with_root(Resource.from_contents(schema, default_specification=DRAFT202012))
        self.variants: dict[str, list] = defaultdict(list)
        for node in self._nodes(schema):
            for name, spec in node.get("properties", {}).items():
                self.variants[name].append(spec)
        super().__init__({name: self._view(specs) for name, specs in self.variants.items()})

    def _nodes(self, schema: Any, seen=None):
        """Schemas for this object, excluding schemas for nested property values."""
        if not isinstance(schema, dict):
            return
        seen = set() if seen is None else seen
        if id(schema) in seen:
            return
        seen.add(id(schema))
        yield schema
        for key in ("$ref", "$dynamicRef"):
            if key in schema:
                yield from self._nodes(self.resolver.lookup(schema[key]).contents, seen)
        for key in ("allOf", "anyOf", "oneOf"):
            for branch in schema.get(key, []):
                yield from self._nodes(branch, seen)
        for key in ("if", "then", "else"):
            yield from self._nodes(schema.get(key), seen)
        for branch in schema.get("dependentSchemas", {}).values():
            yield from self._nodes(branch, seen)

    def _view(self, specs: list) -> dict:
        # A view is a hint, never an intersection that would discard valid branches.
        expanded = [node for spec in specs for node in self._nodes(spec)]
        if len(specs) == 1 and isinstance(specs[0], dict) and not any(
                key in specs[0] for key in ("$ref", "$dynamicRef", "allOf", "anyOf", "oneOf")):
            return specs[0]
        view = {"anyOf": specs}
        for key in ("type", "default", "description"):
            values = [node[key] for node in expanded if key in node]
            if values and all(v == values[0] for v in values):
                view[key] = values[0]
        return view

    def errors(self, settings: dict) -> list:
        """Validate partial pins; nested supplied values remain complete values.

        A missing runtime argument is allowed. At the root, an unresolved oneOf
        choice may still be made by later arguments, and an if/not predicate that
        reads an unbound property cannot choose a branch yet. The ordinary full
        schema still validates every actual invocation.
        """
        from jsonschema import Draft202012Validator, validators
        from jsonschema.exceptions import ValidationError

        try:
            json.dumps(settings, allow_nan=False)
        except (TypeError, ValueError) as exc:
            return [ValidationError(f"Settings must be strict JSON: {exc}")]

        ordinary = Draft202012Validator.VALIDATORS

        def unbound(schema, seen=None):
            seen = set() if seen is None else seen
            for node in self._nodes(schema, seen):
                names = set(node.get("properties", {})) | set(node.get("required", []))
                names.update(node.get("dependentSchemas", {}))
                for trigger, dependencies in node.get("dependentRequired", {}).items():
                    names.add(trigger)
                    names.update(dependencies)
                if names - settings.keys():
                    return True
                # Future arguments can change predicates over the whole object,
                # even if every explicitly named property is already pinned.
                if {"minProperties", "maxProperties", "propertyNames", "patternProperties",
                    "additionalProperties", "unevaluatedProperties", "const", "enum"} & node.keys():
                    return True
                if "not" in node and unbound(node["not"], seen):
                    return True
            return False

        def required(validator, value, instance, schema):
            if instance is not settings:
                yield from ordinary["required"](validator, value, instance, schema)

        def min_properties(validator, value, instance, schema):
            if instance is not settings:
                yield from ordinary["minProperties"](validator, value, instance, schema)

        def dependent_required(validator, value, instance, schema):
            if instance is not settings:
                yield from ordinary["dependentRequired"](validator, value, instance, schema)

        def one_of(validator, value, instance, schema):
            keyword = "anyOf" if instance is settings and unbound({"anyOf": value}) else "oneOf"
            yield from ordinary[keyword](validator, value, instance, schema)

        def conditional(validator, value, instance, schema):
            if instance is settings and unbound(value):
                yield from ordinary["anyOf"](validator, [schema.get("then", True), schema.get("else", True)],
                                              instance, schema)
            else:
                yield from ordinary["if"](validator, value, instance, schema)

        def negated(validator, value, instance, schema):
            if instance is not settings or not unbound(value):
                yield from ordinary["not"](validator, value, instance, schema)

        partial = validators.extend(Draft202012Validator, {"required": required, "minProperties": min_properties,
                                    "dependentRequired": dependent_required, "oneOf": one_of,
                                    "if": conditional, "not": negated})
        return list(partial(self.schema).iter_errors(settings))


def plan_parameters(schema: dict) -> dict:
    if isinstance(schema, MCPInputSchema):
        return PlanParameters(schema)
    return schema.get("properties") or {}
