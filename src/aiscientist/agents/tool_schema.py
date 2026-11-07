"""Planner views of MCP schemas, without replacing the schema used for calls.

Plan settings are partial arguments. Discovery can inspect local refs and
branches, but validation keeps their relationships. Unbound required arguments and
undecidable branch choices are deferred; every actual call validates the original
schema with the ordinary Draft 2020-12 validator.
"""
from __future__ import annotations

from collections import defaultdict
import json
import re
from typing import Any

TOOL_NAME = r"[A-Za-z_][A-Za-z0-9_-]{0,63}"
NAME_LEFT = r"(?<![A-Za-z0-9_-])"
NAME_RIGHT = r"(?![A-Za-z0-9_-])"
REPORT_TOOL = "write_report"  # gateway-owned plan step; never a Scientist/MCP callable


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

        A missing runtime argument is allowed. Fixed values can disprove a
        condition even when other arguments are unknown. Otherwise, unresolved
        conditions are tried one assignment at a time against the entire schema;
        validation and evaluated-property annotations share that same assignment.
        Nested supplied values and every actual invocation remain complete.
        """
        from jsonschema import Draft202012Validator, validators
        from jsonschema.exceptions import ValidationError

        try:
            json.dumps(settings, allow_nan=False)
        except (TypeError, ValueError) as exc:
            return [ValidationError(f"Settings must be strict JSON: {exc}")]

        if self.validator.is_valid(settings):
            return []  # a complete valid call is also a valid set of partial pins

        class Undecided(Exception):
            def __init__(self, predicate):
                self.key = id(predicate)

        # Register before validation: unevaluatedProperties may precede if in
        # the document, and its annotation collector independently tests if.
        predicates = set()

        def register(schema, seen=None):
            seen = set() if seen is None else seen
            for node in self._nodes(schema, seen):
                if "if" in node:
                    predicates.add(id(node["if"]))
                if "not" in node:
                    register(node["not"], seen)

        register(self.schema)
        choices: dict[int, bool] = {}

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
                if node.get("minProperties", 0) > len(settings):
                    return True
                if "maxProperties" in node and len(settings) <= node["maxProperties"]:
                    return True
                if {"propertyNames", "patternProperties",
                    "additionalProperties", "unevaluatedProperties", "const", "enum"} & node.keys():
                    return True
                if "not" in node and unbound(node["not"], seen):
                    return True
            return False

        def reachable_nodes(validator, seen=None):
            """Same-instance annotation sources under the current branch choices."""
            schema = validator.schema
            if not isinstance(schema, dict):
                return
            seen = set() if seen is None else seen
            key = (id(schema), type(validator))
            if key in seen:
                return
            seen.add(key)
            # Respect the selected dialect, including legacy $ref siblings.
            applicable = dict(type(validator)._APPLICABLE_VALIDATORS(schema))
            if "$ref" in schema and schema.keys() - applicable.keys():
                # jsonschema's annotation collector can still inspect legacy
                # $ref siblings that keyword validation ignores. Do not use a
                # narrower static bound to reject a completion it would accept.
                yield None
                return
            node = {key: value for key, value in applicable.items() if key in validator.VALIDATORS}
            yield node
            if "$ref" in node:
                resolved = validator._resolver.lookup(node["$ref"])
                yield from reachable_nodes(validator.evolve(schema=resolved.contents, _resolver=resolved.resolver), seen)
            for keyword in ("allOf", "anyOf", "oneOf"):
                for branch in node.get(keyword, []):
                    child = validator.evolve(schema=branch)
                    if probe(child) is not False:
                        yield from reachable_nodes(child, seen)
            if "if" in node:
                condition = validator.evolve(schema=node["if"])
                if decide(condition):
                    yield from reachable_nodes(condition, seen)
                    branch = applicable.get("then", True)
                else:
                    branch = applicable.get("else", True)
                yield from reachable_nodes(validator.evolve(schema=branch), seen)
            # A missing trigger may be supplied at runtime. Its schema can
            # contribute annotations unless fixed values already rule it out.
            for branch in node.get("dependentSchemas", {}).values():
                child = validator.evolve(schema=branch)
                if probe(child) is not False:
                    yield from reachable_nodes(child, seen)

        def stable_conflict(error, validator):
            """Can appending root arguments never repair this validation error?

            Supplied property values are complete. Root-object failures need a
            proof of stability: additional fields can satisfy dependencies or
            change evaluated-property annotations. Unknown failures stay unknown.
            """
            if error.instance is not settings:
                return True
            keyword = error.validator
            if keyword in {"anyOf", "oneOf"} and error.context:
                # A union is impossible only if *every* branch has a stable
                # failure. Other failures in that same branch may be repairable.
                branches = defaultdict(list)
                for child in error.context:
                    if child.schema_path:
                        branches[child.schema_path[0]].append(child)
                return all(any(stable_conflict(child, validator) for child in branches[index])
                           for index in range(len(error.validator_value)))
            if keyword in {"const", "enum"}:
                candidates = [error.validator_value] if keyword == "const" else error.validator_value
                # Compare complete candidates with the fixed fields overlaid.
                # JSON Schema equality distinguishes booleans from numbers.
                return not any(isinstance(candidate, dict) and Draft202012Validator({"const": candidate}).is_valid(
                    {**candidate, **settings}) for candidate in candidates)
            if keyword in {"not", "oneOf"}:
                return not unbound({keyword: error.validator_value})
            if keyword == "unevaluatedProperties" and error.validator_value is False:
                # An upper bound over reachable branches and future dependencies,
                # using the same if decisions as validation and annotations.
                possible = set()
                for node in reachable_nodes(validator.evolve(schema=error.schema)):
                    if node is None or "$dynamicRef" in node or any(key in node and node[key] is not False
                        for key in ("additionalProperties", "unevaluatedProperties")):
                        return False  # no static proof for this broader evaluation scope
                    possible.update(node.get("properties", {}))
                    possible.update(key for key in settings for pattern in node.get("patternProperties", {})
                                    if re.search(pattern, key))
                return bool(settings.keys() - possible)
            return error.schema is False or keyword in {"type", "maxProperties", "additionalProperties"}

        probing = set()

        def probe(validator):
            """True/False only when proved; None leaves future arguments open."""
            key = (id(validator.schema), type(validator))
            if key in probing:
                return None  # a recursive proof cannot establish a fixed conflict
            probing.add(key)
            try:
                errors = list(validator.iter_errors(settings))
                if any(stable_conflict(error, validator) for error in errors):
                    return False
                if not errors and not unbound(validator.schema):
                    return True
                return None
            finally:
                probing.remove(key)

        def decide(validator):
            state = probe(validator)
            if state is not None:
                return state
            key = id(validator.schema)
            if key not in choices:
                raise Undecided(validator.schema)
            return choices[key]

        partial_classes = {}

        def partial_class(base):
            """Extend each selected dialect locally, without global registration.

            jsonschema.evolve may select a different class at a $schema boundary.
            Keep its dialect and resolver, adding the same root-only plan rules.
            """
            if base in partial_classes:
                return partial_classes[base]
            ordinary = base.VALIDATORS

            def defer_root(keyword):
                def validate(validator, value, instance, schema):
                    if instance is not settings:
                        yield from ordinary[keyword](validator, value, instance, schema)
                return validate

            def one_of(validator, value, instance, schema):
                keyword = "anyOf" if instance is settings and unbound({"anyOf": value}) else "oneOf"
                yield from ordinary[keyword](validator, value, instance, schema)

            def negated(validator, value, instance, schema):
                if instance is not settings or not unbound(value):
                    yield from ordinary["not"](validator, value, instance, schema)

            overrides = {key: defer_root(key) for key in ("required", "minProperties", "dependentRequired")
                         if key in ordinary}
            overrides.update({key: fn for key, fn in {"oneOf": one_of, "not": negated}.items() if key in ordinary})
            partial = validators.extend(base, overrides)
            partial_classes[base] = partial
            original_evolve, original_is_valid = partial.evolve, partial.is_valid

            def evolve(validator, **changes):
                evolved = original_evolve(validator, **changes)
                if type(evolved) in partial_classes.values():
                    return evolved
                # Use the library's chosen dialect and resolution state. Merely
                # deleting $schema would silently change referenced semantics.
                return partial_class(type(evolved))(
                    evolved.schema, resolver=evolved._ref_resolver, format_checker=evolved.format_checker,
                    registry=evolved._registry, _resolver=evolved._resolver)

            def is_valid(validator, instance, _schema=None):
                if _schema is not None:
                    return validator.evolve(schema=_schema).is_valid(instance)
                if instance is settings and id(validator.schema) in predicates:
                    return decide(validator)
                return original_is_valid(validator, instance)

            partial.evolve, partial.is_valid = evolve, is_valid
            return partial

        partial = partial_class(Draft202012Validator)
        pending, failures = [{}], None
        while pending:
            choices = pending.pop()
            try:
                errors = list(partial(self.schema).iter_errors(settings))
            except Undecided as exc:
                # Restart at the root so annotations from incompatible branches
                # can never be combined, regardless of schema keyword order.
                pending.extend([{**choices, exc.key: True}, {**choices, exc.key: False}])
                continue
            if not errors:
                return []
            if failures is None:
                failures = errors
        return failures


def plan_parameters(schema: dict) -> dict:
    if isinstance(schema, MCPInputSchema):
        return PlanParameters(schema)
    return schema.get("properties") or {}
