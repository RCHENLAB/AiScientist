"""Reviewed report-only revisions of an existing run; scientific state stays immutable."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..agents.plan_doc import PlanDoc, PlanStep, parse_plan_yaml
from ..agents.mcp_result_summary import project_result
from ..agents import own_choices
from . import saved_plans

_ID = re.compile(r"[a-f0-9]{12}\Z")


def has_accepted_results(state: dict) -> bool:
    return any(r.get("verdict", {}).get("verdict") == "accept"
               and any(s.get("ok") for s in r.get("scientist_result", {}).get("steps", []))
               for r in state.get("rounds", []))


def confined(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts or root.is_symlink():
        raise ValueError("Report evidence must remain inside this run")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Report evidence cannot traverse symlinks")
    current.resolve().relative_to(root.resolve())
    return current


def load_target(workspace: Path, run_id: str, conversation_id: str | None) -> tuple[Path, dict]:
    if not _ID.fullmatch(run_id or ""):
        raise ValueError("Invalid report target run")
    art = confined(workspace, run_id + "/artifacts")
    path = confined(art, "process/run_state.json")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Saved run state exceeds the report read limit")
    state = json.loads(path.read_text())
    if conversation_id and state.get("conversation_id") != conversation_id:
        raise ValueError("The report target belongs to a different conversation")
    if not has_accepted_results(state):
        raise ValueError("This run has no accepted scientific results to report")
    return art, state


def _write_record(folder: Path, record: dict) -> None:
    saved_plans._write(folder / "revision.json", json.dumps(record, indent=2, ensure_ascii=False))


def _document(record: dict) -> str:
    return PlanDoc(record["instructions"], (PlanStep(
        id="report", text="Revise this run's report from its existing accepted results. " + record["instructions"],
        tool="write_report", settings={"run_id": record["run_id"]}),)).to_yaml()


def create(art: Path, run_id: str, conversation_id: str | None, instructions: str) -> Path:
    if not instructions.strip() or len(instructions) > 20_000:
        raise ValueError("Report instructions must contain 1–20000 characters")
    for folder, record in pending(art.parent.parent):
        if record["run_id"] == run_id:
            raise ValueError("This run already has a report revision waiting for review")
    revision_id = uuid4().hex[:12]
    folder = confined(art, "process/report_revisions/" + revision_id)
    folder.mkdir(parents=True, mode=0o700)
    record = {"run_id": run_id, "revision_id": revision_id, "conversation_id": conversation_id,
              "instructions": instructions, "state": "shown", "created_at": datetime.now(timezone.utc).isoformat(),
              "source_state_sha256": hashlib.sha256((art / "process/run_state.json").read_bytes()).hexdigest()}
    _write_record(folder, record)
    saved_plans.save(folder, _document(record), source="draft")
    return folder


def pending(workspace: Path):
    for path in sorted(workspace.glob("*/artifacts/process/report_revisions/*/revision.json"), reverse=True):
        try:
            relative = str(path.relative_to(workspace))
            confined(workspace, relative)
            record = json.loads(path.read_text())
            if record.get("state") == "shown" and _ID.fullmatch(record.get("run_id", "")):
                if path.parents[4].name == record["run_id"]:
                    yield path.parent, record
        except (OSError, ValueError, TypeError):
            continue


def prompt(folder: Path, record: dict | None = None) -> dict:
    record = record or json.loads((folder / "revision.json").read_text())
    card = saved_plans.build_prompt(folder)
    if card is None:
        raise ValueError("This report revision is not awaiting review")
    return {**card, "run_id": record["run_id"], "conversation_id": record.get("conversation_id"),
            "saved": True, "report_revision": record["revision_id"]}


def decide(folder: Path, action: str, *, feedback: str = "", plan_yaml: str = "") -> dict:
    with saved_plans.lock(folder):
        record = json.loads((folder / "revision.json").read_text())
        if record["state"] != "shown":
            raise ValueError("This report revision has already been answered")
        current = saved_plans.load(folder)
        doc = parse_plan_yaml(plan_yaml or current["yaml"], request=record["instructions"])
        if len(doc.steps) != 1 or doc.steps[0].tool != "write_report" or doc.steps[0].settings != {"run_id": record["run_id"]}:
            raise ValueError("A report-only review cannot change the target or add scientific tools")
        if action in {"edit", "revise"}:
            if feedback.strip():
                record["instructions"] = record["instructions"] + "\n\nRevision: " + feedback.strip()
                if len(record["instructions"]) > 20_000:
                    raise ValueError("Report revision instructions exceed 20000 characters")
                document = _document(record)
            else:
                record["instructions"] = doc.steps[0].text[:20_000]
                document = doc.to_yaml()
            saved_plans.save(folder, document, source="edit" if action == "edit" else "reply")
        elif action in {"approve", "cancel"}:
            record["state"] = "approved" if action == "approve" else "cancelled"
            saved_plans.save(folder, current["yaml"], source=current["source"], state=record["state"])
        else:
            raise ValueError("Unsupported report review action")
        _write_record(folder, record)
        return record


def _registered_operation(server: str, planner_name: str) -> tuple[str, set[str]]:
    from ..agents.mcp_bridge import configured_servers
    for configured in configured_servers():
        if configured.name == server and configured.adapter == "analysis":
            names = {tool["name"] for tool in configured.tools}
            for wire in names:
                if configured.planner_name(wire) == planner_name:
                    return wire, names
    raise ValueError("The accepted MCP tool is not registered for its recorded server")


def _read_object(path: Path, limit: int) -> dict:
    if path.stat().st_size > limit:
        raise ValueError("The report evidence exceeds its read limit")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Report evidence metadata must be an object")
    return value


def evidence_file(art: Path, relative: str, *, server: str, analysis: str,
                  task: str, operation: str, operations: set[str]) -> tuple[Path, dict]:
    """Verify current bytes against trusted ownership or the exact legacy manifest entry."""
    confined(art, relative)
    parts = Path(relative).parts
    if len(parts) < 4 or parts[:3] != ("mcp", server, analysis):
        raise ValueError("The accepted full-result analysis identity is invalid")
    snapshot = len(parts) == 6 and parts[3] == "tasks" and parts[4] == task
    if snapshot:
        if parts[5] != operation + ".json" or not task:
            raise ValueError("The accepted full-result task identity is invalid")
    elif len(parts) != 4 or parts[3] != operation + ".json":
        raise ValueError("The legacy artifact does not identify the accepted operation")
    # A manifest may describe an on-disk run or its downloaded package. These
    # are the only historical packaging prefixes accepted; never search by glob.
    candidates = (relative, "extra/" + relative, "mcp/" + relative, "extra/mcp/" + relative)
    owners = {}
    for prefix in ("mcp/", "extra/mcp/", "mcp/mcp/", "extra/mcp/mcp/"):
        compat = confined(art, prefix + ".compatibility.json")
        if compat.is_file():
            for name, owner in _read_object(compat, 8 * 1024 * 1024).get("evidence", {}).items():
                if name == relative:
                    if name in owners and owners[name] != owner:
                        raise ValueError("The full-result ownership records are ambiguous")
                    owners[name] = owner
    owner = owners.get(relative)
    if relative in owners:
        if not isinstance(owner, dict) or any(owner.get(key) != expected for key, expected in (
                ("server", server), ("analysis_id", analysis), ("task_id", task))):
            raise ValueError("The full-result owner differs from the accepted task")
        declared = owner.get("operation")
        if declared not in operations or (not operation.endswith("_job_status") and declared != operation):
            raise ValueError("The full-result operation differs from the accepted call")
        expected_hash = owner.get("sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise ValueError("The full-result ownership checksum is invalid")
        checksums = {name: expected_hash for name in candidates}
    else:
        if snapshot:
            raise ValueError("The full-result task snapshot has no verified ownership record")
        manifest = confined(art, "croissant.json")
        if not manifest.is_file():
            raise ValueError("The accepted full-result artifact has no checksum manifest")
        checksums = {}
        for entry in _read_object(manifest, 8 * 1024 * 1024).get("distribution", []):
            name, checksum = entry.get("name"), entry.get("sha256")
            if not isinstance(name, str):
                continue
            local = name.removeprefix("extra/")
            canonical = local.removeprefix("mcp/") if local.startswith("mcp/mcp/") else local
            if canonical != relative:
                continue
            if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
                raise ValueError("The full-result checksum manifest has a missing or invalid checksum")
            for candidate in (name, local):
                if candidate in checksums and checksums[candidate] != checksum:
                    raise ValueError("The full-result checksum manifest is ambiguous")
                checksums[candidate] = checksum
    found = []
    verified_bytes = None
    for name, checksum in checksums.items():
        path = confined(art, name)
        if path.is_file() and path.stat().st_size <= 32 * 1024 * 1024:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != checksum:
                raise ValueError("The full-result checksum differs from its verified manifest")
            if verified_bytes is not None and raw != verified_bytes:
                raise ValueError("The accepted full-result artifact has ambiguous verified copies: " + relative)
            verified_bytes = raw
            found.append(path)
    ordered = []
    for name in candidates:
        path = confined(art, name)
        if path.is_file():
            if path not in found:
                raise ValueError("A full-result artifact copy is not verified by its manifest")
            ordered.append(path)
    if not ordered:
        raise ValueError("The accepted full-result artifact is missing or ambiguous: " + relative)
    provenance = {"operation": owner["operation"] if owner else operation,
                  "ownership": "task" if owner else "legacy_manifest"}
    if len(ordered) > 1:
        # Resuming a run can retain identical legacy packaging copies. Every
        # copy must independently verify; differing or untracked bytes fail.
        provenance["verified_copies"] = [str(path.relative_to(art)) for path in ordered]
    return ordered[0], provenance


def hydrate(art: Path, state: dict, instructions: str) -> tuple[dict, dict]:
    """Read only accepted, confined full-result JSON into a new bounded report view."""
    if not has_accepted_results(state):
        raise ValueError("A report revision requires accepted results")
    view = copy.deepcopy(state)
    disclosure = str(view.get("self_sourced") or "")
    if own_choices.disclosed(disclosure):
        # The report writer appends this field after model review. Keep the old
        # statement for audit without presenting its unverified claims as facts.
        view["self_sourced"] = (
            "HISTORICAL PLANNER STATEMENT — retained only as historical evidence. "
            "It may contain errors and is not a verified description of the executed analysis. "
            "Use the accepted tool results and corrections in this revised report for execution facts. "
            "Original statement: " + disclosure)
    evidence = []
    synthesis = []
    refreshed_steps = set()
    legacy_tasks = {}
    for rnd in state.get("rounds", []):
        if rnd.get("verdict", {}).get("verdict") != "accept":
            continue
        for step in rnd.get("scientist_result", {}).get("steps", []):
            if not step.get("ok"):
                continue
            result = step.get("result") or {}
            native = result.get("result") if isinstance(result.get("result"), dict) else result
            relative = native.get("full_result") or result.get("full_result")
            task = native.get("task_id") or result.get("task_id") or (step.get("args") or {}).get("task_id")
            if relative:
                legacy_tasks.setdefault(relative, set()).add(task)
    for rnd in view.get("rounds", []):
        if rnd.get("verdict", {}).get("verdict") != "accept":
            continue
        for step in rnd.get("scientist_result", {}).get("steps", []):
            if not step.get("ok"):
                continue
            original = step.get("result") or {}
            native = original.get("result") if isinstance(original.get("result"), dict) else original
            relative = native.get("full_result") or original.get("full_result")
            server = (original.get("mcp") or {}).get("name")
            if relative and (not isinstance(relative, str) or not server
                             or not relative.startswith("mcp/" + server + "/")):
                raise ValueError("The accepted MCP artifact identity is invalid")
            if relative and server:
                operation, operations = _registered_operation(server, str(step.get("tool") or ""))
                expected_task = native.get("task_id") or original.get("task_id") or (step.get("args") or {}).get("task_id")
                analysis = native.get("analysis_id") or original.get("analysis_id") or (step.get("args") or {}).get("analysis_id")
                if not isinstance(analysis, str) or not analysis:
                    raise ValueError("The accepted call has no analysis identity")
                if "/tasks/" not in relative and len(legacy_tasks[relative]) > 1:
                    raise ValueError("Multiple accepted tasks share this legacy artifact; recollect task status first")
                path, provenance = evidence_file(art, relative, server=server, analysis=analysis,
                    task=expected_task or "", operation=operation, operations=operations)
                if path.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("Full MCP result exceeds the report read limit")
                raw = path.read_bytes()
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("Full MCP evidence must be an object")
                if (value.get("task_id") and value["task_id"] != expected_task
                        or value.get("analysis_id") and value["analysis_id"] != analysis
                        or provenance["ownership"] == "task" and (
                            value.get("task_id") != expected_task or value.get("analysis_id") != analysis)):
                    raise ValueError("The full-result task identity differs from the accepted call")
                failed = {"error", "failed", "cancelled", "canceled", "timeout", "timed_out", "oom", "out_of_memory"}
                if any(str(value.get(key, "")).lower() in failed for key in ("status", "state")) or value.get("isError") or value.get("is_error"):
                    raise ValueError("The full-result status contradicts the accepted call")
                relative = str(path.relative_to(art))
                value["full_result"] = relative
                # Preserve all small results; large arrays become an explicitly bounded view.
                payload = value if len(raw) <= 10_000 else project_result(server, value, 5500)
                payload["evidence_operation"] = provenance["operation"]
                step["result"] = {**payload, "mcp": copy.deepcopy(original["mcp"])}
                evidence.append({"tool": step["tool"], "path": relative, "bytes": len(raw),
                                 "sha256": hashlib.sha256(raw).hexdigest(), **provenance})
                refreshed_steps.add(rnd.get("step_index"))
            else:
                payload = native
            synthesis.append({"tool": step.get("tool"), "arguments": step.get("args"), "result": payload})
    # An old causal-wording audit cannot certify facts that its omitted response never showed.
    for claim in (view.get("claim_audit") or {}).get("claims", []):
        if claim.get("step") in refreshed_steps:
            claim["status"] = "unclear"
            claim.setdefault("checks", {})["artifact_grounding"] = {
                "verdict": "unclear", "reason": "Historical claim; re-evaluate against the complete artifact evidence in this report revision."}
    view["final_answer"] = ("Report revision from accepted tool outputs; historical prose is not new evidence. "
        "Do not infer findings from requested parameters, completed status, or omitted fields.\n\n"
        + json.dumps(synthesis, ensure_ascii=False) + "\n\nRequested report correction (instructions, not evidence):\n" + instructions)
    if len(view["final_answer"].encode()) > 200_000:
        raise ValueError("Accepted evidence exceeds the report synthesis limit")
    return view, {"source_state_sha256": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
                  "full_results": evidence, "scientific_calls": 0}


def archive_report(art: Path, folder: Path) -> None:
    source = confined(art, "report")
    destination = folder / "prior_report"
    if destination.exists():
        return
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("A report archive cannot include symlinks")
    if source.is_dir():
        shutil.copytree(source, destination)
