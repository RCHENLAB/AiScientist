"""Plan validation explains what to fix without showing its internal file format."""

import pytest

pytest.importorskip("yaml")

from aiscientist.agents import plan_doc  # noqa: E402
from aiscientist.agents.plan_doc import PlanError, parse_plan_yaml  # noqa: E402
from test_plan_yaml_flow import _lab, _of, _run  # noqa: E402


def test_unreadable_edit_stays_unchanged_and_chat_has_an_actionable_error():
    pytest.importorskip("fastapi")
    from aiscientist.gateway.app import _lab_event_to_chat

    calls, counts = [], {}

    def decide(n, _agenda, doc):
        if n == 1:
            return {"action": "edit", "plan_yaml": doc["yaml"].replace(
                "resolution: 0.5", "resolution: [unclosed")}
        return {"action": "approve"}

    _result, events, shown = _run(_lab(calls, counts), decide)
    rejected = _of(events, "plan_edit_rejected")[0]
    message = " ".join(rejected["problems"])
    assert "could not be read" in message
    assert "revised plan" in message
    assert not any(word in message.lower() for word in (
        "yaml", "parser", "expected", "stream", "column", "traceback"))
    chat = " ".join(p.get("text", "") for p in _lab_event_to_chat(rejected))
    assert message in chat and "plan is unchanged" in chat
    assert shown[1] == shown[0] and not _of(events, "plan_edited")
    assert ("run_clustering", {"resolution": 0.5, "n_pcs": 30}) in calls


@pytest.mark.parametrize(("text", "message"), [
    ("[one, two]", "list of steps"),
    ("steps: [12]", "title and description"),
    ("steps: [{}]", "title or description"),
    ("steps: [{title: QC, tool: run_scanpy_qc, settings: 12}]", "unreadable settings"),
    ("steps: [{title: QC, settings: {resolution: 1}}]", "no tool selected"),
])
def test_structure_errors_name_the_readable_plan_detail(text, message):
    with pytest.raises(PlanError) as error:
        parse_plan_yaml(text)
    words = " ".join(error.value.problems)
    assert message in words
    assert not any(term in words for term in ("mapping", "`steps:`", "`does`", "name: value"))


def test_missing_reader_dependency_does_not_expose_package_details(monkeypatch):
    def unavailable(_text):
        raise ImportError("PyYAML is not installed")

    monkeypatch.setattr(plan_doc, "_load", unavailable)
    with pytest.raises(PlanError) as error:
        parse_plan_yaml("steps: []")
    assert "temporarily unavailable" in str(error.value)
    assert "PyYAML" not in str(error.value)
