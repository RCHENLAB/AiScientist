"""The PI's SELF-SOURCED disclosure is a field of the planning reply, not the agenda's last string.

Prod run 74873590ca7b (2026-10-23, Claude Haiku 5.5) and one of two DSV4 drafts on HPC3 wrote the
same invalid reply: the prompt asked for the disclosure as the agenda's LAST string, while its
format puts `later` after `agenda`, so the model closed the agenda, wrote `later`, and then added
the disclosure after it, outside any array. Scripted models throughout.
"""

from __future__ import annotations

import json
import re

import pytest

from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness
from aiscientist.agents.research_lab import (
    _PI_SYSTEM,
    _PLAN_REFORMAT,
    LabConfig,
    ResearchLab,
    _parse_plan,
    _parse_self_sourced,
)

QC = "**QC & normalization** — Run `run_clustering` on the matrix at resolution 0.5."
SCORE = "**Score per class** — Run `run_clustering` again and summarise the classes."
NOTE = "The 5% mitochondrial cut-off is my choice for single-nucleus data."


def _lab(*replies):
    """A lab whose PI answers each planning call with the next of ``replies``."""
    left = list(replies)
    calls: list[list[dict]] = []

    def complete(messages):
        if "Principal Investigator of a bioinformatics lab" in messages[0]["content"]:
            calls.append(messages)
            return left.pop(0) if left else "no plan"
        return "{}"

    tools = [HarnessTool("run_clustering", "cluster", {"type": "object", "properties": {}},
                         lambda _a, _c: {"status": "ok"}, category="analysis")]
    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"), LabConfig(),
                      complete_fn=complete, scientist=ResearchHarness(catalog=tools))
    return lab, calls


def _plan(lab):
    events: list[dict] = []
    kind, agenda = lab._pi_plan("Score the ISG signature in DDX41 vs WT", events.append)
    return kind, agenda, events


def _of(events, kind):
    return [e for e in events if e.get("type") == kind]


# --- the prompt ---------------------------------------------------------------------------------------

def test_the_prompt_example_is_one_json_object_with_the_disclosure_as_its_own_field():
    """The example is what models copy. It must be valid JSON, and the disclosure must sit in its
    own field, not at a position inside `agenda` that `later` can come after."""
    example = re.search(r'\{"breadth": "whole", "agenda": \[.*?"later": \[\]\}', _PI_SYSTEM, re.DOTALL)
    assert example, "the format example is still in the prompt"
    obj = json.loads(example.group(0))
    assert list(obj) == ["breadth", "agenda", "self_sourced", "later"]
    assert isinstance(obj["self_sourced"], str)
    assert not any(str(s).upper().startswith("SELF-SOURCED") for s in obj["agenda"])
    assert _parse_self_sourced(example.group(0)) == "", "the example's placeholder is not a disclosure"


def test_the_prompt_no_longer_asks_for_a_disclosure_step():
    assert "final step string" not in _PI_SYSTEM
    assert "it is the last item" not in _PI_SYSTEM
    assert "never goes in `agenda`" in _PI_SYSTEM
    assert '"self_sourced"' in _PLAN_REFORMAT, "the format retry names the field too"


# --- reading the field --------------------------------------------------------------------------------

@pytest.mark.parametrize("reply, expected", [
    (json.dumps({"agenda": [QC], "self_sourced": NOTE}), NOTE),
    (json.dumps({"agenda": [QC], "self_sourced": f"SELF-SOURCED: {NOTE}"}), NOTE),
    (json.dumps({"agenda": [QC], "self_sourced": f"**SELF-SOURCED:** {NOTE}"}), NOTE),
    (json.dumps({"agenda": [QC], "self_sourced": ["The cut-off is mine.", "So is the bin count."]}),
     "The cut-off is mine. So is the bin count."),
    (json.dumps({"agenda": [QC], "self_sourced": "  spread\n over   lines "}), "spread over lines"),
    (json.dumps({"agenda": [QC], "self_sourced": "<every choice in this plan that came from your own judgement>"}), ""),
    (json.dumps({"agenda": [QC], "self_sourced": 3}), ""),
    (json.dumps({"agenda": [QC]}), ""),
    ("```json\n" + json.dumps({"agenda": [QC], "self_sourced": NOTE}) + "\n```", NOTE),
    (json.dumps([QC]), ""),
    ("not json", ""),
])
def test_the_field_is_read_from_the_reply(reply, expected):
    assert _parse_self_sourced(reply) == expected


# --- through the planner ------------------------------------------------------------------------------

def test_a_reply_with_the_field_is_read_first_time_and_the_disclosure_is_no_step():
    lab, calls = _lab(json.dumps({"breadth": "specific", "agenda": [QC, SCORE],
                                  "self_sourced": NOTE, "later": ["Run enrichment."]}))
    kind, agenda, events = _plan(lab)
    assert (kind, agenda) == ("agenda", [QC, SCORE])
    assert len(calls) == 1 and not _of(events, "plan_reformat")
    assert lab.self_sourced == NOTE
    assert [e["text"] for e in _of(events, "plan_self_sourced")] == [NOTE]


def test_a_reply_in_the_old_form_still_has_its_disclosure_split_off():
    lab, _calls = _lab(json.dumps({"breadth": "specific", "agenda": [QC, SCORE, f"SELF-SOURCED: {NOTE}"],
                                   "later": []}))
    kind, agenda, events = _plan(lab)
    assert (kind, agenda) == ("agenda", [QC, SCORE])
    assert lab.self_sourced == NOTE and _of(events, "plan_self_sourced")


def test_the_field_wins_over_an_old_form_string_and_neither_runs():
    lab, _calls = _lab(json.dumps({"agenda": [QC, "SELF-SOURCED: an older note."], "self_sourced": NOTE}))
    kind, agenda, _events = _plan(lab)
    assert agenda == [QC]
    assert lab.self_sourced == NOTE


def test_none_is_kept_as_the_pi_wrote_it():
    none = "none — every choice in this plan comes from the guidance and the dataset profile."
    lab, _calls = _lab(json.dumps({"agenda": [QC], "self_sourced": none}))
    _kind, _agenda, _events = _plan(lab)
    assert lab.self_sourced == none


def test_the_reply_that_failed_on_haiku_and_dsv4_is_still_not_a_plan_and_is_asked_for_again():
    """The broken shape itself, cut down from run 74873590ca7b's first reply: the disclosure after
    `later`. It is not repaired; the format retry, now naming the field, gets the plan."""
    broken = ('{"breadth": "specific", "agenda": ["' + QC.replace('"', '\\"') + '"], '
              '"later": ["Run enrichment."], "SELF-SOURCED: ' + NOTE + '"]}')
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    assert _parse_plan(broken, 40, False) is None
    lab, calls = _lab(broken, json.dumps({"agenda": [QC], "self_sourced": NOTE}))
    kind, agenda, events = _plan(lab)
    assert (kind, agenda) == ("agenda", [QC])
    assert len(calls) == 2 and _of(events, "plan_reformat")
    assert '"self_sourced"' in calls[1][-1]["content"]
    assert lab.self_sourced == NOTE
