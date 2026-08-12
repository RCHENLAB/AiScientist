from __future__ import annotations

import json
import os
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SlurmJobSpec:
    job_name: str
    command: str
    partition: str = "gpu"
    account: str | None = None
    cpus_per_task: int = 8
    memory_gb: int = 32
    time_limit: str = "02:00:00"
    gres: str = "gpu:1"


@dataclass(frozen=True)
class SlurmQueuePolicy:
    wait_notice_minutes: int = 15
    human_review_minutes: int = 30
    max_queue_wait_minutes: int = 240
    allow_lab_server_heavy_fallback: bool = False


@dataclass(frozen=True)
class SlurmQueueFallbackPlan:
    status: str
    queue_status: str
    user_notice: str
    fallback_options: list[str]
    prohibited_fallbacks: list[str]
    resume_strategy: str
    policy: SlurmQueuePolicy


class SlurmAdapter:
    """Small Slurm boundary that can dry-run now and submit later."""

    def build_job_spec(self, command: str, job_name: str = "bioagent_vision_dryrun") -> SlurmJobSpec:
        return SlurmJobSpec(
            job_name=os.getenv("BIOAGENT_SLURM_JOB_NAME", job_name),
            command=command,
            partition=os.getenv("BIOAGENT_SLURM_PARTITION", "gpu"),
            account=os.getenv("BIOAGENT_SLURM_ACCOUNT") or None,
            cpus_per_task=int(os.getenv("BIOAGENT_SLURM_CPUS_PER_TASK", "8")),
            memory_gb=int(os.getenv("BIOAGENT_SLURM_MEMORY_GB", "64")),
            time_limit=os.getenv("BIOAGENT_SLURM_TIME_LIMIT", "04:00:00"),
            gres=os.getenv("BIOAGENT_SLURM_GRES", "gpu:1"),
        )

    def render_script(self, spec: SlurmJobSpec) -> str:
        account_line = f"#SBATCH --account={spec.account}\n" if spec.account else ""
        return (
            "#!/usr/bin/env bash\n"
            f"#SBATCH --job-name={spec.job_name}\n"
            f"#SBATCH --partition={spec.partition}\n"
            f"{account_line}"
            f"#SBATCH --cpus-per-task={spec.cpus_per_task}\n"
            f"#SBATCH --mem={spec.memory_gb}G\n"
            f"#SBATCH --time={spec.time_limit}\n"
            f"#SBATCH --gres={spec.gres}\n"
            "#SBATCH --output=slurm-%j.out\n\n"
            "set -euo pipefail\n"
            "module purge || true\n"
            "echo \"Starting AiScientist workflow on $(hostname)\"\n"
            f"{spec.command}\n"
        )

    def write_script(self, spec: SlurmJobSpec, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_script(spec), encoding="utf-8")
        return path

    def build_queue_fallback_plan(self, spec: SlurmJobSpec, policy: SlurmQueuePolicy | None = None) -> SlurmQueueFallbackPlan:
        policy = policy or SlurmQueuePolicy()
        return SlurmQueueFallbackPlan(
            status="dry_run_waiting_policy_prepared",
            queue_status="unknown_until_sbatch_or_squeue_enabled",
            user_notice=(
                "High-compute analysis is intended for UCI HPC3/RCIC GPUs through Slurm. "
                "If the GPU queue is long, the system should keep the job checkpointed, notify the researcher, "
                "and offer safe alternatives instead of silently moving the heavy analysis to the lab server."
            ),
            fallback_options=[
                "wait_for_hpc_queue_and_keep_checkpointing",
                "run_lab_server_lightweight_preflight_only",
                "run_small_local_fallback_with_explicit_diagnostic_label",
                "submit_smaller_gpu_diagnostic_job_after_review",
                "reduce_requested_gpu_resources_after_human_review",
                "pause_for_researcher_review_with_resume_command",
            ],
            prohibited_fallbacks=[
                "do_not_run_heavy_gpu_analysis_on_lab_server_by_default",
                "do_not_upload_private_dataset_to_cloud_compute",
                "do_not_submit_or_cancel_slurm_jobs_without_human_review",
            ],
            resume_strategy=(
                "Persist Slurm script, queue policy, checkpoint files, and report artifacts so the researcher can "
                "resume after queue wait, provider failure, or process interruption."
            ),
            policy=policy,
        )

    def write_queue_fallback_plan(self, spec: SlurmJobSpec, output_dir: Path, policy: SlurmQueuePolicy | None = None) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        plan = self.build_queue_fallback_plan(spec, policy=policy)
        json_path = output_dir / "hpc_queue_fallback_plan.json"
        md_path = output_dir / "hpc_queue_fallback_plan.md"
        json_path.write_text(json.dumps(asdict(plan), indent=2), encoding="utf-8")
        md_path.write_text(self.render_queue_fallback_plan(plan), encoding="utf-8")
        return {"json_path": json_path, "summary_path": md_path}

    def build_readiness_report(self, spec: SlurmJobSpec) -> dict[str, object]:
        configured = {
            "account": bool(spec.account),
            "partition": bool(spec.partition),
            "time_limit": bool(spec.time_limit),
            "memory_gb": spec.memory_gb > 0,
            "cpus_per_task": spec.cpus_per_task > 0,
            "gres": bool(spec.gres),
        }
        missing = [key for key, ok in configured.items() if not ok]
        return {
            "status": "ready_for_reviewed_dry_run" if not missing else "missing_cluster_configuration",
            "configured": configured,
            "missing": missing,
            "job_spec": asdict(spec),
            "submit_enabled": False,
            "review_required_before_sbatch": True,
            "notes": [
                "This repository still generates Slurm dry-run scripts only.",
                "Use the rendered script to verify UCI account, partition, modules, and command shape before enabling sbatch.",
                "Do not submit private datasets or production jobs until the command and output paths are reviewed.",
            ],
        }

    def write_readiness_report(self, spec: SlurmJobSpec, output_dir: Path) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        report = self.build_readiness_report(spec)
        json_path = output_dir / "slurm_readiness.json"
        md_path = output_dir / "slurm_readiness.md"
        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        md_path.write_text(self.render_readiness_report(report), encoding="utf-8")
        return {"json_path": json_path, "summary_path": md_path}

    def render_readiness_report(self, report: dict[str, object]) -> str:
        spec = report["job_spec"]
        lines = [
            "# Slurm Readiness Report",
            "",
            f"Status: `{report['status']}`",
            f"Submit enabled: `{str(report['submit_enabled']).lower()}`",
            f"Review required before sbatch: `{str(report['review_required_before_sbatch']).lower()}`",
            "",
            "## Job Spec",
            "",
        ]
        assert isinstance(spec, dict)
        for key in ("job_name", "partition", "account", "cpus_per_task", "memory_gb", "time_limit", "gres", "command"):
            lines.append(f"- {key}: `{spec.get(key)}`")
        lines.extend(["", "## Missing Configuration", ""])
        missing = report["missing"]
        assert isinstance(missing, list)
        if missing:
            lines.extend(f"- `{item}`" for item in missing)
        else:
            lines.append("- none")
        lines.extend(["", "## Notes", ""])
        notes = report["notes"]
        assert isinstance(notes, list)
        lines.extend(f"- {note}" for note in notes)
        lines.append("")
        return "\n".join(lines)

    def render_queue_fallback_plan(self, plan: SlurmQueueFallbackPlan) -> str:
        lines = [
            "# HPC Queue Waiting and Fallback Plan",
            "",
            f"Status: `{plan.status}`",
            f"Queue status: `{plan.queue_status}`",
            f"Wait notice minutes: `{plan.policy.wait_notice_minutes}`",
            f"Human review minutes: `{plan.policy.human_review_minutes}`",
            f"Max queue wait minutes: `{plan.policy.max_queue_wait_minutes}`",
            f"Allow lab-server heavy fallback: `{str(plan.policy.allow_lab_server_heavy_fallback).lower()}`",
            "",
            "## User Notice",
            "",
            plan.user_notice,
            "",
            "## Safe Fallback Options",
            "",
        ]
        lines.extend(f"- `{item}`" for item in plan.fallback_options)
        lines.extend(
            [
                "",
                "## Local Fallback Labeling",
                "",
                "Small-scale lab-server fallback is allowed only for preflight, toy/subset diagnostics, "
                "or derived-summary checks. Any local fallback output must be labeled:",
                "",
                "- `execution_mode=local_fallback`",
                "- `result_scope=diagnostic_or_preflight_only`",
                "- `not_production_hpc_result=true`",
                "",
                "UI copy: Local fallback result generated while UCI GPU job is queued. Use for triage only; "
                "rerun on HPC for production analysis.",
            ]
        )
        lines.extend(["", "## Prohibited Fallbacks", ""])
        lines.extend(f"- `{item}`" for item in plan.prohibited_fallbacks)
        lines.extend(["", "## Resume Strategy", "", plan.resume_strategy, ""])
        return "\n".join(lines)

    def submit(self, script_path: Path) -> str:
        raise NotImplementedError(
            f"Dry-run prototype only. Review {script_path} and submit with sbatch when ready."
        )
