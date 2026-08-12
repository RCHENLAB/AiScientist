"""Versioning is only real if the NEW skill is what the model is offered.

`skills/` ships both `annotate_clusters_by_markers` (v1) and `..._v2`. The v2 folder declares
`supersedes:` in its frontmatter, which must have three consequences on the LOADED library —
not merely on disk:

  * v2 appears in the progressive-disclosure manifest, and v1 does not (the manifest is the
    only thing the Scientist sees when deciding whether to fetch a skill at all);
  * v1 is NOT deleted and still resolves by name, so a bad new version can be rolled back to;
  * v2 actually carries its reference template.
"""

from __future__ import annotations

from bioagent.agents import skills

V1 = "annotate_clusters_by_markers"
V2 = "annotate_clusters_by_markers_v2"


def test_v2_supersedes_v1_and_only_v2_is_advertised():
    lib = skills.SKILLS
    assert V1 in lib and V2 in lib
    assert lib[V2].supersedes == V1

    manifest_names = {line.split(" — ")[0][2:] for line in skills.skill_manifest().splitlines()}
    assert V2 in manifest_names
    assert V1 not in manifest_names            # hidden, not deleted
    assert V1 in skills.superseded_names(lib)


def test_the_superseded_version_stays_reachable_for_rollback():
    assert skills.get_skill(V1) is not None
    assert "reference.py" in skills.get_skill(V1).files


def test_v2_ships_its_template_and_states_the_rule_that_replaced_v1():
    v2 = skills.get_skill(V2)
    assert "reference.py" in v2.files
    ref = v2.files["reference.py"]
    # The whole point of v2: score -> z-argmax is a FIRST PASS, raw expression decides, and a
    # cluster with no dominant signal is left alone rather than forced into the nearest label.
    assert "score_genes" in ref                      # not v1's top-25 set intersection
    assert "Unassigned" in ref
    assert "first_pass_label" in ref and "corrected_by_raw_check" in ref
    # The v1 procedure this replaces must not have survived into v2.
    assert "top_genes &" not in ref

    # The description survives YAML block-scalar parsing (a '>-' summary is what the manifest
    # shows; if it parsed as the literal '>-' the skill would be unreachable through it).
    assert v2.summary.startswith("Assign a cell-type label")
