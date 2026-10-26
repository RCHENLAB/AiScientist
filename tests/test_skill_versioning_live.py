"""Versioning is only real if the NEW skill is what the model is offered.

A ``<name>_v2`` folder (how induction writes an improved version) declares ``supersedes:`` in its
frontmatter, which must have three consequences on the LOADED library — not merely on disk:

  * v2 appears in the progressive-disclosure manifest, and v1 does not (the manifest is the
    only thing the Scientist sees when deciding whether to fetch a skill at all);
  * v1 is NOT deleted and still resolves by name, so a bad new version can be rolled back to;
  * v2 actually carries its reference template.
"""

from __future__ import annotations

import pytest

from aiscientist.agents import skills

V1 = "annotate_clusters_by_markers"
V2 = "annotate_clusters_by_markers_v2"


@pytest.fixture
def lib(tmp_path):
    for name, extra, code in (
            (V1, "", "top = set(markers) & top_genes\n"),
            (V2, f"supersedes: {V1}\n", "sc.tl.score_genes(adata, markers)\nlabel = 'Unassigned'\n")):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: >-\n  Assign a cell-type label ({name}).\n{extra}---\n\n"
            "## When to use\nAfter clustering.\n\n## Run\nAdapt reference.py.\n", encoding="utf-8")
        (folder / "reference.py").write_text(code, encoding="utf-8")
    return skills._load_from(tmp_path)


def test_v2_supersedes_v1_and_only_v2_is_advertised(lib):
    assert V1 in lib and V2 in lib
    assert lib[V2].supersedes == V1

    manifest_names = {line.split(" — ")[0][2:] for line in skills.skill_manifest(lib).splitlines()}
    assert V2 in manifest_names
    assert V1 not in manifest_names            # hidden, not deleted
    assert V1 in skills.superseded_names(lib)


def test_the_superseded_version_stays_reachable_for_rollback(lib):
    assert skills.get_skill(V1, lib) is not None
    assert "reference.py" in skills.get_skill(V1, lib).files


def test_v2_ships_its_template(lib):
    v2 = skills.get_skill(V2, lib)
    assert "score_genes" in v2.files["reference.py"]
    # The description survives YAML block-scalar parsing (a '>-' summary is what the manifest
    # shows; if it parsed as the literal '>-' the skill would be unreachable through it).
    assert v2.summary.startswith("Assign a cell-type label")
