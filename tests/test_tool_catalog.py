"""The TOOL.md front-matter parser that tool discovery rests on (tools/catalog.py).

It is deliberately small (stdlib only: the HPC3 job images import it), so these tests pin both what
it reads and what it refuses to guess about."""
from __future__ import annotations

import pytest

from aiscientist.tools.catalog import ManifestError, load_manifest, parse_front_matter


def test_scalars_lists_comments_and_body():
    fields, body = parse_front_matter(
        "---\n"
        "name: run_de            # trailing comment\n"
        "summary: \"A #hash inside quotes stays\"\n"
        "order: 220\n"
        "chat: false\n"
        "needs: [lirical_fn=tool:run_lirical, literature_fn=tool:deep_literature]\n"
        "empty: []\n"
        "---\n"
        "# run_de\n\nBody text.\n")
    assert fields == {
        "name": "run_de", "summary": "A #hash inside quotes stays", "order": 220, "chat": False,
        "needs": ["lirical_fn=tool:run_lirical", "literature_fn=tool:deep_literature"], "empty": [],
    }
    assert body.startswith("# run_de") and "Body text." in body


def test_indented_lines_fold_into_the_previous_text_field():
    fields, _ = parse_front_matter("---\nsummary: Differential expression\n  between groups of cells.\n---\n")
    assert fields["summary"] == "Differential expression between groups of cells."


@pytest.mark.parametrize("text", [
    "name: x\n",                                   # no front matter
    "---\nname: x\n",                              # not closed
    "---\n- a list item\n---\n",                   # not key: value
    "---\n  folded: without a field\n---\n",       # continuation with nothing to continue
])
def test_refuses_what_it_cannot_read(text):
    with pytest.raises(ManifestError):
        parse_front_matter(text)


def _folder(tmp_path, extra: str = ""):
    folder = tmp_path / "scan_everything"
    folder.mkdir(parents=True)
    (folder / "TOOL.md").write_text(
        "---\nname: scan_everything\nsummary: s\ncategory: literature\nruns_on: hpc:literature\n"
        f"order: 1\n{extra}---\n# scan_everything\n", encoding="utf-8")
    return folder


def test_a_tool_is_offered_to_the_research_agents_unless_it_opts_out(tmp_path):
    assert load_manifest(_folder(tmp_path)).research is True
    assert load_manifest(_folder(tmp_path / "b", "research: false\n")).research is False


def test_an_unknown_field_is_refused(tmp_path):
    with pytest.raises(ManifestError, match="unknown field"):
        load_manifest(_folder(tmp_path, "reserch: false\n"))


def test_a_job_brings_its_own_gpu_or_calls_the_sessions_model_not_both(tmp_path):
    m = load_manifest(_folder(tmp_path, "own_gpu: true\n"))
    assert m.own_gpu is True and m.served_model is False
    with pytest.raises(ManifestError, match="own_gpu and served_model"):
        load_manifest(_folder(tmp_path / "b", "own_gpu: true\nserved_model: true\n"))
