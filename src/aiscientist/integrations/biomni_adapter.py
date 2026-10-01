from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .biomni_runtime import (
    BiomniRunResult,
    BiomniRuntime,
    BiomniRuntimeConfig,
    RealBiomniRuntime,
)
from .safety import DataBoundaryGuard, DataBoundaryPolicy, DataBoundaryReport

# Policy mode that actually permits a real (or mocked) Biomni call. Any other
# mode (the default ``offline_plan``) yields a plan only — never an execution.
EXECUTE_MODE = "execute"


def _analysis_task(task: str, dataset_path: Path | None) -> str:
    """The brief handed to Biomni's A1. When a dataset is attached, tell the agent the
    local PATH so its generated code reads the file directly (the data never enters the
    prompt — only the path; analysis happens in Biomni's local code execution)."""
    if dataset_path is None:
        return task
    return (
        f"{task}\n\n"
        f"A dataset is available locally at this path: {dataset_path}\n"
        "Load it directly from that path with Python — scanpy/anndata for .h5ad, pandas for "
        ".csv/.tsv — then analyze it to answer the request. Do NOT ask for the data to be "
        "uploaded; read it from the path. Treat conclusions as hypotheses to validate."
    )


@dataclass(frozen=True)
class BiomniSafetyPolicy:
    """Local-first boundary for planned Biomni execution."""

    mode: str = "offline_plan"
    allow_external_network: bool = False
    allow_remote_llm: bool = False
    allow_private_data: bool = False
    require_local_model: bool = True


@dataclass(frozen=True)
class BiomniCapability:
    name: str
    reason: str
    needs_network: bool = False
    needs_remote_llm: bool = False
    can_use_private_data: bool = False


@dataclass(frozen=True)
class BiomniCapabilityDecision:
    capability: BiomniCapability
    status: str
    reasons: list[str]
    constraints: list[str]


@dataclass(frozen=True)
class BiomniExecutionPlan:
    status: str
    summary: str
    mode: str
    local_model_required: bool
    raw_data_to_biomni_allowed: bool
    capability_decisions: list[BiomniCapabilityDecision]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BiomniExecutionResult:
    """Outcome of ``BiomniAdapter.run`` — plan + guard verdict + (maybe) a run."""

    status: str                    # "executed" | "blocked_by_guard" | "blocked_by_policy"
    plan: BiomniExecutionPlan
    data_boundary: DataBoundaryReport
    run_result: BiomniRunResult | None
    notes: list[str]

    @property
    def executed(self) -> bool:
        return self.status == "executed"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "plan": self.plan.to_dict(),
            "data_boundary": asdict(self.data_boundary),
            "run_result": self.run_result.to_dict() if self.run_result else None,
            "notes": list(self.notes),
        }


class BiomniAdapter:
    """Safe planning boundary for using Biomni as a biomedical backend later."""

    def __init__(self, policy: BiomniSafetyPolicy | None = None) -> None:
        self.policy = policy or BiomniSafetyPolicy()

    def capability_registry(self) -> list[BiomniCapability]:
        return [
            BiomniCapability(
                name="biomni_single_cell_omics",
                reason="Useful for single-cell QC, annotation, pathway follow-up, and omics interpretation.",
                can_use_private_data=True,
            ),
            BiomniCapability(
                name="biomni_public_database_lookup",
                reason="Useful for public gene, variant, protein, pathway, and literature lookups.",
                needs_network=True,
            ),
            BiomniCapability(
                name="biomni_structure_or_docking",
                reason="Useful for protein structure, docking, and pharmacology-style follow-up when relevant.",
                needs_network=True,
                needs_remote_llm=True,
            ),
            BiomniCapability(
                name="biomni_local_code_execution",
                reason="Useful for running selected biomedical scripts against reviewed local or derived inputs.",
                can_use_private_data=True,
            ),
        ]

    def decide_capability(self, capability: BiomniCapability) -> BiomniCapabilityDecision:
        reasons: list[str] = []
        constraints: list[str] = []
        status = "allowed"

        if capability.needs_network and not self.policy.allow_external_network:
            status = "blocked"
            reasons.append("External network access is disabled for the current Biomni policy.")
        if capability.needs_remote_llm and not self.policy.allow_remote_llm:
            status = "blocked"
            reasons.append("Remote LLM access is disabled for the current Biomni policy.")
        if capability.can_use_private_data and not self.policy.allow_private_data:
            status = "planned_local_only"
            reasons.append("Private lab data can only be used after local-only Biomni runtime review.")
        if self.policy.require_local_model:
            constraints.append("Use a local OpenAI-compatible model endpoint for private or sensitive tasks.")
        constraints.append("Do not send raw dataset rows, sample identifiers, API keys, or private metadata to Biomni prompts.")
        constraints.append("Return structured artifacts for AiScientist validation before final reporting.")
        if not reasons:
            reasons.append("Capability fits the current Biomni adapter boundary.")
        return BiomniCapabilityDecision(
            capability=capability,
            status=status,
            reasons=reasons,
            constraints=constraints,
        )

    def build_execution_plan(self, question: str, dataset_path: Path | None = None) -> BiomniExecutionPlan:
        decisions = [self.decide_capability(capability) for capability in self.capability_registry()]
        has_dataset = dataset_path is not None
        summary = (
            "Biomni is planned as a biomedical execution backend behind AiScientist validation. "
            "This offline plan records which Biomni-style capabilities are usable now and which need "
            "local runtime or network review before execution."
        )
        if has_dataset:
            summary += " A dataset was provided, so private-data capabilities remain local-only until reviewed."
        return BiomniExecutionPlan(
            status="offline_plan_prepared",
            summary=summary,
            mode=self.policy.mode,
            local_model_required=self.policy.require_local_model,
            raw_data_to_biomni_allowed=self.policy.allow_private_data and not self.policy.allow_remote_llm,
            capability_decisions=decisions,
        )

    def run(
        self,
        task: str,
        dataset_path: Path | None = None,
        runtime: BiomniRuntime | None = None,
    ) -> BiomniExecutionResult:
        """Run a task through Biomni — but only after the safety layer clears it.

        Order is deliberate: (1) build the capability/policy plan, (2) run the
        shared ``DataBoundaryGuard`` over the *task text* and refuse on secret or
        raw-table content, (3) gate on the policy mode, and only then (4) call the
        runtime (real ``A1.go`` or a mock). The runtime is constructed lazily so
        the real Biomni import never happens on the blocked paths.
        """

        plan = self.build_execution_plan(task, dataset_path=dataset_path)
        # Weave the dataset PATH (not its rows) into the task so Biomni's generated
        # code reads the local file directly. The data never enters the prompt —
        # only the path; the analysis happens in Biomni's local code execution.
        analysis_task = _analysis_task(task, dataset_path)
        guard = DataBoundaryGuard()
        # The guard only consults ``allow_raw_data_to_llm``; map Biomni's
        # ``allow_private_data`` onto it so the shared guard stays the single
        # boundary in front of every external/LLM call.
        boundary_policy = DataBoundaryPolicy(allow_raw_data_to_llm=self.policy.allow_private_data)
        report = guard.inspect_prompt(analysis_task, dataset_path, boundary_policy)
        notes: list[str] = []

        try:
            guard.assert_safe_for_prompt(report)
        except ValueError as exc:
            notes.append(f"blocked by data-boundary guard: {exc}")
            return BiomniExecutionResult("blocked_by_guard", plan, report, None, notes)

        if self.policy.mode != EXECUTE_MODE:
            notes.append(
                f"policy mode '{self.policy.mode}' is plan-only; "
                f"set mode='{EXECUTE_MODE}' to call Biomni A1."
            )
            return BiomniExecutionResult("blocked_by_policy", plan, report, None, notes)

        runtime = runtime or RealBiomniRuntime(BiomniRuntimeConfig.from_env())
        run_result = runtime.run(analysis_task)
        notes.append(f"executed via {run_result.runtime} Biomni runtime")
        return BiomniExecutionResult("executed", plan, report, run_result, notes)

    def write_execution_plan(self, plan: BiomniExecutionPlan, output_dir: Path) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "biomni_execution_plan.json"
        md_path = output_dir / "biomni_execution_plan.md"
        json_path.write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
        md_path.write_text(self.render_execution_plan(plan), encoding="utf-8")
        return {"json_path": json_path, "summary_path": md_path}

    def render_execution_plan(self, plan: BiomniExecutionPlan) -> str:
        lines = [
            "# Biomni Execution Backend Plan",
            "",
            f"Status: `{plan.status}`",
            f"Mode: `{plan.mode}`",
            f"Local model required: `{str(plan.local_model_required).lower()}`",
            f"Raw data to Biomni allowed: `{str(plan.raw_data_to_biomni_allowed).lower()}`",
            "",
            "## Summary",
            "",
            plan.summary,
            "",
            "## Capability Decisions",
            "",
        ]
        for decision in plan.capability_decisions:
            lines.extend(
                [
                    f"### {decision.capability.name}",
                    "",
                    f"Status: `{decision.status}`",
                    "",
                    decision.capability.reason,
                    "",
                    "Reasons:",
                ]
            )
            lines.extend(f"- {reason}" for reason in decision.reasons)
            lines.append("")
            lines.append("Constraints:")
            lines.extend(f"- {constraint}" for constraint in decision.constraints)
            lines.append("")
        return "\n".join(lines)
