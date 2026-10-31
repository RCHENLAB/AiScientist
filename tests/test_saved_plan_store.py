"""Saved review versions are the source for the card and the approved report."""

import json

import pytest

from aiscientist.agents.plan_doc import parse_plan_yaml
from aiscientist.gateway import saved_plans


PLAN = """plan: 1
request: Check cells
steps:
  - id: s1
    title: QC
    does: Filter cells
    tool: run_scanpy_qc
    settings:
      max_pct_mt: 20
"""


def test_each_version_exists_before_its_card_and_records_its_source(tmp_path):
    assert saved_plans.load(tmp_path) is None
    for version, source in enumerate(("draft", "reply", "edit", "question"), 1):
        record = saved_plans.save(tmp_path, PLAN, source=source,
                                  prompt={"estimate": {"minutes": 5}})
        path = tmp_path / "plan" / f"v{version:02d}.yaml"
        # The file is the plan document followed by the version's record.
        assert path.read_text(encoding="utf-8").startswith(record["yaml"])
        assert record["yaml"] == PLAN and "_review" not in record["yaml"]
        assert record["source"] == source and record["state"] == "shown"
        pointer = json.loads((tmp_path / "plan" / "current.json").read_text())
        assert pointer == {"version": version, "filename": path.name, "state": "shown"}
        card = saved_plans.build_prompt(tmp_path)
        assert card["plan"]["yaml"] == record["yaml"]
        assert card["estimate"] == {"minutes": 5}
    assert len(list((tmp_path / "plan").glob("v*.yaml"))) == 4


@pytest.mark.parametrize("source", ["reply", "edit"])
def test_reply_or_edit_begins_with_current_disk_contents(tmp_path, source):
    saved_plans.save(tmp_path, PLAN, source="draft")
    path = tmp_path / "plan" / "v01.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("max_pct_mt: 20", "max_pct_mt: 25"),
                    encoding="utf-8")
    current = saved_plans.load(tmp_path)
    assert parse_plan_yaml(current["yaml"]).steps[0].settings["max_pct_mt"] == 25
    edited = current["yaml"].replace("Filter cells", "Filter and inspect cells")
    saved_plans.save(tmp_path, edited, source=source)
    card = saved_plans.build_prompt(tmp_path)
    assert card["plan"]["steps"][0]["settings"]["max_pct_mt"] == 25
    assert "Filter and inspect" in card["agenda"][0]


def test_approval_writes_the_exact_approved_version_and_hides_waiting_card(tmp_path):
    shown = saved_plans.save(tmp_path, PLAN, source="draft")
    assert not (tmp_path / "report" / "plan.yaml").exists()
    approved = saved_plans.save(tmp_path, saved_plans.load(tmp_path)["yaml"],
                                source="reply", state="approved")
    assert (tmp_path / "report" / "plan.yaml").read_text(encoding="utf-8") == approved["yaml"]
    assert (tmp_path / "plan" / "v01.yaml").read_text(encoding="utf-8").startswith(shown["yaml"])
    assert saved_plans.build_prompt(tmp_path) is None


def test_cancel_retains_versions_and_does_not_write_approved_report(tmp_path):
    saved_plans.save(tmp_path, PLAN, source="draft")
    saved_plans.save(tmp_path, saved_plans.load(tmp_path)["yaml"], source="reply", state="cancelled")
    assert saved_plans.load(tmp_path)["state"] == "cancelled"
    assert saved_plans.build_prompt(tmp_path) is None
    assert len(list((tmp_path / "plan").glob("v*.yaml"))) == 2
    assert not (tmp_path / "report" / "plan.yaml").exists()


def test_clarification_survives_without_a_live_plan_document(tmp_path):
    saved_plans.save(tmp_path, "", source="question", kind="clarify", payload=["Which cells?"])
    assert saved_plans.build_prompt(tmp_path) == {
        "type": "plan_clarify", "questions": ["Which cells?"]}


def test_pointer_cannot_escape_the_plan_folder(tmp_path):
    folder = tmp_path / "plan"
    folder.mkdir()
    (folder / "current.json").write_text('{"filename": "../outside.yaml"}')
    with pytest.raises(ValueError, match="pointer"):
        saved_plans.load(tmp_path)


def test_the_card_and_the_report_get_the_document_as_written_without_the_record(tmp_path):
    from aiscientist.agents.plan_doc import PlanDoc, PlanStep

    doc = PlanDoc("Check cells", (
        PlanStep(id="s1", text="**QC** — Filter cells with `run_scanpy_qc`.", tool="run_scanpy_qc",
                 settings={"max_pct_mt": 20, "batch": "no", "alpha": 1e-05}),))
    text = doc.to_yaml()
    saved_plans.save(tmp_path, text, source="draft", payload=doc.agenda(), prompt={"estimate": None})
    card = saved_plans.build_prompt(tmp_path)
    # The editor shows the document the lab wrote, its header comments included, and none of the
    # record: what produced the version and its state stay in the version's file.
    assert card["plan"]["yaml"] == text and "_review" not in card["plan"]["yaml"]
    assert card["plan"]["yaml"].startswith("# AiScientist plan.")
    version_file = (tmp_path / "plan" / "v01.yaml").read_text(encoding="utf-8")
    assert "_review:" in version_file and "source: draft" in version_file
    # The file is still one plan document: read whole, it is the same plan.
    assert parse_plan_yaml(version_file) == parse_plan_yaml(text)
    approved = saved_plans.save(tmp_path, saved_plans.load(tmp_path)["yaml"], source="draft",
                                state="approved")
    report = (tmp_path / "report" / "plan.yaml").read_text(encoding="utf-8")
    assert report == text == approved["yaml"]


def test_a_version_edited_by_hand_without_its_mark_still_loads(tmp_path):
    saved_plans.save(tmp_path, PLAN, source="draft")
    path = tmp_path / "plan" / "v01.yaml"
    # Someone rewrote the file through a YAML tool: the comment line is gone, the record is a key.
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    current = saved_plans.load(tmp_path)
    assert current["source"] == "draft" and "_review" not in current["yaml"]
    assert parse_plan_yaml(current["yaml"]).steps[0].settings == {"max_pct_mt": 20}
    nxt = saved_plans.save(tmp_path, path.read_text(encoding="utf-8"), source="edit")
    assert "_review" not in nxt["yaml"]


def test_a_version_that_does_not_parse_is_a_value_error(tmp_path):
    saved_plans.save(tmp_path, PLAN, source="draft")
    (tmp_path / "plan" / "v01.yaml").write_text("steps: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not parse"):
        saved_plans.load(tmp_path)
    assert saved_plans.current_state(tmp_path) == "shown"


def test_waiting_plan_keeps_dataset_uses_and_unusable_reasons(tmp_path):
    inputs = {"datasets": [{"name": "study", "use": "main"}, {"name": "sample2", "use": "sample"}],
              "unusable_inputs": [{"name": "missing", "reason": "its files were not found"}]}
    saved_plans.save(tmp_path, PLAN, source="draft", prompt=inputs)
    card = saved_plans.build_prompt(tmp_path)
    assert card["plan"]["datasets"] == inputs["datasets"]
    assert card["plan"]["unusable_inputs"] == inputs["unusable_inputs"]
