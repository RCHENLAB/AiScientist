"""Registered literature MCP calls for the console's dedicated literature routes.

This adapter contains no retrieval or extraction implementation. It reuses the
reviewed server, skill policy, executor, model binding, and cancellation path. A
console route may consume the complete, synced JSON while the planner-facing
bridge continues to return a bounded preview.

Construction is explicit; existing console entry points do not switch merely
because this module is imported.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Callable

from ..agents import mcp_bridge

TOOLS = frozenset({"literature_search", "search_corpus", "deep_literature",
                   "extract_haploinsufficient_genes", "literature_job_status"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_FAILED_TASK = re.compile(r"\bliterature task ([a-f0-9]{32}-[a-f0-9]{8}) failed\b")
_MAX_RESULT_BYTES = 32 * 1024 * 1024
_TRANSPORT_FIELDS = ("mcp", "provenance", "execution_mode", "slurm_state", "queue_timeout",
                     "slurm_reason", "returncode", "artifacts", "skill")


def registered_server() -> mcp_bridge.Server | None:
    return next((server for server in mcp_bridge.configured_servers() if server.name == "literature"), None)


def rules_identity(server: mcp_bridge.Server, spec: dict | None = None) -> str:
    value = {"image": server.image, "version": server.version,
             "tools_sha256": server.tools_sha256, "spec": spec}
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("literature analysis and curation names must be simple identifiers")
    return value


def _confined(root: Path, relative: str, *, exists: bool = True) -> Path:
    """Check the exact synced artifact tree, including each existing ancestor."""
    root = Path(root)
    rel = PurePosixPath(relative)
    if (not root.is_absolute() or root.is_symlink() or not relative or "\\" in relative
            or rel.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("literature artifact path must remain inside the run's artifacts")
    current = root
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("literature artifact symlinks are forbidden")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError("literature artifact path escapes this run")
    if exists and (not current.is_file() or not stat.S_ISREG(current.stat().st_mode)):
        raise ValueError("literature evidence was not synced as a regular file")
    return current


@dataclass
class LiteratureRoute:
    server: mcp_bridge.Server
    tools: dict[str, Any]
    artifacts: Path
    should_cancel: Callable[[], bool]

    def rules_identity(self, spec: dict | None = None) -> str:
        """Invalidate console caches when the scientific image or task spec changes.

        Corpus fingerprint and selected model identity remain separate cache-key
        inputs. No endpoint, credential, or ephemeral relay port belongs here.
        """
        return rules_identity(self.server, spec)

    def _result(self, out: dict, tool: str, analysis: str) -> dict:
        preview = out.get("result")
        if not isinstance(preview, dict):
            return dict(out)
        result = dict(preview)
        if preview.get("full_result"):
            canonical = f"mcp/literature/{analysis}/{tool}.json"
            task = preview.get("task_id")
            snapshot = (f"mcp/literature/{analysis}/tasks/{_identifier(task)}/{tool}.json" if task else None)
            expected = preview["full_result"]
            if expected not in {canonical, snapshot}:
                raise ValueError("literature evidence belongs to another tool, task or analysis")
            source = _confined(self.artifacts, expected)
            if source.stat().st_size > _MAX_RESULT_BYTES:
                raise ValueError("literature evidence exceeds the console read limit")
            full = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(full, dict):
                raise ValueError("literature evidence must be a JSON object")
            if (full.get("analysis_id", analysis) != analysis
                    or (expected == snapshot and (full.get("analysis_id") != analysis or full.get("task_id") != task))):
                raise ValueError("literature evidence belongs to another task or analysis")
            result = {**full, **{key: preview[key] for key in
                      ("analysis_id", "task_id", "job_id", "state", "full_result") if key in preview}}
        result.update({key: out[key] for key in _TRANSPORT_FIELDS if key in out})
        # A failed transport must never become a successful scan because stale
        # JSON happens to say ok. Native failures keep their richer status/error.
        if out.get("status") != "ok" and result.get("status", "ok") == "ok":
            result.update(status="error", error=out.get("error") or "Literature MCP call failed")
        return result

    def run_tool(self, tool: str, arguments: dict, ctx: Any) -> dict:
        """Call the registered wire tool; recover failed scan evidence once.

        Native inline failures identify their task but return an MCP error. The
        status tool exposes the recorded pilot/usage/resume fields. Recovery is
        read-only, for this analysis, and never submits a second extraction.
        """
        if tool not in TOOLS or tool not in self.tools:
            return {"status": "not_enabled", "error": "The registered literature tool is unavailable"}
        if self.should_cancel():
            return {"status": "cancelled"}
        try:
            args = dict(arguments)
            analysis = _identifier(args.get("analysis_id", "default"))
            out = self.tools[tool].executor(args, ctx)
            if not isinstance(out, dict):
                raise ValueError("literature MCP returned no result object")
            if (tool == "extract_haploinsufficient_genes" and out.get("status") == "error"
                    and "literature_job_status" in self.tools and not self.should_cancel()):
                diagnostic = "\n".join(str(out.get(key) or "") for key in ("error", "stderr", "stdout"))
                task = _FAILED_TASK.search(diagnostic)
                if task:
                    recovered = self.tools["literature_job_status"].executor(
                        {"analysis_id": analysis, "task_id": task[1]}, ctx)
                    if isinstance(recovered, dict) and isinstance(recovered.get("result"), dict):
                        evidence = self._result(recovered, "literature_job_status", analysis)
                        if evidence.get("task_id") != task[1]:
                            raise ValueError("literature status returned another task")
                        evidence["status"] = "error" if evidence.get("status") == "ok" else evidence.get("status", "error")
                        evidence.setdefault("error", out.get("error") or "Literature extraction failed")
                        evidence["failure_recovered"] = True
                        return evidence
            return self._result(out, tool, analysis)
        except (ValueError, TypeError, OSError) as exc:
            return {"status": "error", "error": str(exc), "mcp": self.server.identity()}

    def stage_scan_tables(self, result: dict, subdir: str) -> Path:
        """Keep the existing console curation/download layout, after successful MCP execution."""
        if result.get("status") != "ok" or result.get("state", "COMPLETED") != "COMPLETED":
            raise ValueError("only a completed literature scan can publish curation tables")
        analysis = _identifier(result.get("analysis_id", "default"))
        subdir = _identifier(subdir)
        plan = []
        for name in ("genes.csv", "hits.csv", "requests.csv"):
            source = _confined(self.artifacts, f"mcp/literature/{analysis}/{subdir}/{name}")
            dest = _confined(self.artifacts, f"{subdir}/{name}", exists=False)
            plan.append((source, dest))
        # Validate every source/destination before creating any output.
        for source, dest in plan:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".literature-table-", dir=dest.parent)
            os.close(fd)
            try:
                shutil.copyfile(source, temp)
                os.replace(temp, dest)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        return plan[0][1].parent


def build_literature_route(executor: Any, artifacts: Path, *, should_cancel: Callable[[], bool] | None = None,
                           servers: tuple[mcp_bridge.Server, ...] | None = None) -> LiteratureRoute | None:
    """Resolve the registered server once, independent of staged planner aliases.

    ``executor`` must be built for this console run, with the current session's
    model-binding callback and cancellation signal. No legacy tool is a fallback.
    """
    from ..agents.research_lab import make_run_in_environment_tool

    servers = mcp_bridge.configured_servers() if servers is None else servers
    selected = tuple(server for server in servers if server.name == "literature")
    if not selected:
        return None
    if len(selected) != 1:
        raise ValueError("only one literature MCP server may be registered")
    server = selected[0]
    environment = make_run_in_environment_tool(executor)
    if environment is None:
        return None
    built = {tool.name: tool for tool in mcp_bridge.build_mcp_tools(executor, environment, servers=selected)}
    tools = {entry["name"]: built[server.planner_name(entry["name"])] for entry in server.tools
             if entry["name"] in TOOLS and server.planner_name(entry["name"]) in built}
    return LiteratureRoute(server, tools, Path(artifacts), should_cancel or (lambda: False)) if tools else None
