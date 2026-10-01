"""A protocol's parameter table and the code's defaults must not be allowed to disagree.

Three places used to carry the same numbers independently: the tool body (``args.get("x", 200)``),
the JSON schema handed to the model, and whatever a protocol's prose happened to say. Nothing
compared them. So a researcher could read a protocol claiming one threshold, the model could read a
schema that named none, and the code could apply a third — and the run would look perfectly
consistent from every single vantage point.

``scrna_pack.PARAMS`` is now the one table all three read. These tests are what stop the copies
coming back: the schema is generated from it, the bodies resolve through it, and a protocol that
documents a value it no longer matches fails here rather than in somebody's Methods section.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("scanpy")

from bioagent.agents.preset_pipelines import _pipelines_dir, _parse_skill  # noqa: E402
from bioagent.tools._lib.scrna import PARAMS
from bioagent.tools.catalog import scrna_catalog  # noqa: E402

# `| `param` | `default` | meaning |` inside a `**`tool`**` block of a SKILL.md Parameters section.
_TOOL_HEADING_RE = re.compile(r"^\*\*`([a-z_]+)`\*\*\s*$")
_ROW_RE = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|\s*$")


def _documented_params(body: str) -> dict[str, dict[str, str]]:
    """{tool: {param: default-as-json-text}} parsed out of a SKILL.md ``## Parameters`` section."""
    section = body.split("## Parameters", 1)
    if len(section) < 2:
        return {}
    out: dict[str, dict[str, str]] = {}
    tool = None
    for line in section[1].splitlines():
        if line.startswith("## "):
            break
        heading = _TOOL_HEADING_RE.match(line.strip())
        if heading:
            tool = heading.group(1)
            out.setdefault(tool, {})
            continue
        row = _ROW_RE.match(line.strip())
        if row and tool and row.group(1) != "parameter":
            out[tool][row.group(1)] = row.group(2)
    return out


def _skills_with_parameters() -> list[tuple[str, str]]:
    found = []
    for skill_md in sorted(_pipelines_dir().glob("*/SKILL.md")):
        meta, body = _parse_skill(skill_md.read_text(encoding="utf-8"))
        if "## Parameters" in body:
            found.append((meta.get("name") or skill_md.parent.name, body))
    return found


def test_at_least_one_protocol_documents_its_parameters():
    """Guards the guard: a parsing change that silently found nothing would make every check
    below vacuously pass."""
    assert _skills_with_parameters(), "no preset SKILL.md has a '## Parameters' section"


def test_every_documented_default_matches_the_code():
    for name, body in _skills_with_parameters():
        documented = _documented_params(body)
        assert documented, f"{name}: '## Parameters' section present but no rows parsed"
        for tool, params in documented.items():
            assert tool in PARAMS, f"{name} documents unknown tool '{tool}'"
            for param, shown in params.items():
                assert param in PARAMS[tool], f"{name} documents unknown {tool}.{param}"
                expected = json.dumps(PARAMS[tool][param][0])
                assert shown == expected, (
                    f"{name} says {tool}.{param} defaults to {shown}, the code uses {expected}")


def test_a_documented_tool_documents_all_of_its_parameters():
    """Half a table is worse than none: a reader assumes the omitted knobs do not exist."""
    for name, body in _skills_with_parameters():
        for tool, params in _documented_params(body).items():
            missing = sorted(set(PARAMS[tool]) - set(params))
            assert not missing, f"{name} omits {tool} parameter(s): {missing}"


def test_the_schema_the_model_reads_carries_the_same_defaults_and_a_meaning():
    """The model never sees PARAMS or the SKILL.md table — it sees the function schema. If the
    default is only in the other two, the model is choosing values blind, which is how a run ends
    up filtering at 10% mitochondrial reads against a declared 20."""
    catalog = {t.name: t for t in scrna_catalog()}
    for tool, params in PARAMS.items():
        assert tool in catalog, f"{tool} declares parameters but is not in the catalog"
        props = catalog[tool].parameters["properties"]
        for param, (default, meaning) in params.items():
            assert param in props, f"{tool}.{param} is declared but absent from the schema"
            assert props[param]["default"] == default
            assert props[param]["description"] == meaning
            # A meaning that just respells the parameter name tells a reviewer nothing about
            # whether the value is right for their data, which is the only question they have.
            words = [w for w in re.findall(r"[a-z]+", meaning.lower())
                     if w not in param.split("_")]
            assert len(words) >= 5, (
                f"{tool}.{param} needs a plain-English meaning, not a restated name: {meaning!r}")


def test_the_tool_body_resolves_through_the_declared_table():
    """`_p` raises on an undeclared parameter on purpose: adding a knob to a tool body without
    adding it here would otherwise reintroduce exactly the invisible default this replaced."""
    from bioagent.tools._lib.scrna import _p

    assert _p("run_de", "n_genes", {}) == 50
    assert _p("run_de", "n_genes", {"n_genes": 200}) == 200
    assert _p("run_scanpy_qc", "max_pct_mt", {}) == 10.0
    with pytest.raises(KeyError):
        _p("run_de", "not_a_declared_parameter", {})


def test_the_protocol_text_and_the_code_agree_on_the_replication_floor():
    """The one number the whole protocol turns on. `min_samples_per_condition` is what makes
    `run_pseudobulk_de` refuse instead of degrading, and the protocol's ">=2 samples per arm" rule
    is only true while it stays 2."""
    assert PARAMS["run_pseudobulk_de"]["min_samples_per_condition"][0] == 2
    body = (Path(_pipelines_dir()) / "differential_expression" / "SKILL.md").read_text()
    assert ">=2 samples per arm" in body or "≥2 samples per arm" in body


def test_enrichment_tests_the_significant_set_not_an_arbitrary_top_n():
    """ORA is run on THE significant genes, defined by a p-value cutoff and an effect-size floor.

    `top_n_genes=100` had no basis, and it hid a worse truncation: the combined DE table it read
    was itself capped at `run_de.n_genes` (50 per direction), so ORA never saw more than 100 genes
    per group however many were significant. On the real DDX41 object the selection is AC 128,
    BC 555, Cone 91, MG 3,996, Rod 4,030 — and MG/Rod being that large is itself the signal that
    the upstream test was pseudoreplicated, which a cap at 100 made invisible.
    """
    assert PARAMS["run_enrichment"]["top_n_genes"][0] == 0, "0 = no cap; the field does not top-N"
    assert PARAMS["run_enrichment"]["padj"][0] == 0.05
    assert PARAMS["run_enrichment"]["lfc"][0] == 0.25, "Seurat's single-cell effect-size floor"
    # the same effect-size convention as the DE step it consumes — two different floors between
    # the test and its interpretation would be indefensible in a methods section
    assert PARAMS["run_enrichment"]["lfc"][0] == PARAMS["run_de"]["lfc"][0]
