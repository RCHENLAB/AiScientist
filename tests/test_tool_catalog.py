"""The TOOL.md front-matter parser that tool discovery rests on (tools/catalog.py).

It is deliberately small (stdlib only: the HPC3 job images import it), so these tests pin both what
it reads and what it refuses to guess about."""
from __future__ import annotations

import pytest

from aiscientist.tools.catalog import ManifestError, parse_front_matter


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
