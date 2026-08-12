"""Live, code-derived overview of the system — the "what do we actually have" view.

Powers the console's **System** page so development is legible: which AGENTS are
defined, which TOOLS/SKILLS exist (with their deps + whether this host can actually run
them), what server CAPABILITIES are installed, and a maintained ROADMAP (done / in
progress / planned). Everything except the roadmap is introspected from the live code
+ runtime, so it can never drift from reality.
"""

from __future__ import annotations

import importlib.util
import shutil
from typing import Any

# The agents/roles that make up the research lab (static — they're code structure).
AGENTS = [
    {"name": "Principal Investigator", "role": "plan + synthesize",
     "description": "Breaks the question into an ordered agenda, then writes the final report grounded "
                    "only in accepted results.", "where": "agents/research_lab.py"},
    {"name": "Scientist", "role": "execute (tool-calling + CodeAct)",
     "description": "Runs the curated tools and/or writes-and-runs Python to carry out each agenda step.",
     "where": "agents/research_harness.py"},
    {"name": "Critic", "role": "judge convergence",
     "description": "Scores each step (accept/revise) and drives convergence; a deterministic guard "
                    "refuses to accept a failed run.", "where": "agents/research_lab.py"},
]

def workflows() -> list[dict[str, Any]]:
    """The designed workflow PRESETS — the development-time view of "which flows exist".
    The analysis-pipeline stages are derived from the live scrna catalog order, so the
    preset can't drift. New research lines register a preset here."""
    from ..tools.scrna_pack import scrna_catalog

    scrna_stages = [t.name for t in scrna_catalog()]   # run_scanpy_qc → clustering → de → enrichment
    return [
        {
            "name": "Research Lab (PI → Scientist → Critic)",
            "kind": "orchestration",
            "description": "Team-of-agents loop: the PI plans an agenda, the Scientist executes each step with "
                           "tools/CodeAct, the Critic judges accept/revise, the PI synthesizes the report. "
                           "Optional Plan mode pauses for human review of the agenda.",
            "stages": ["PI: plan agenda", "Scientist: run tools / code", "Critic: accept or revise",
                       "PI: synthesize report"],
        },
        {
            "name": "Single-cell analysis pipeline",
            "kind": "pipeline",
            "description": "The end-to-end scRNA-seq preset the Scientist follows for an .h5ad dataset: "
                           "QC → clustering → per-cluster DE → pathway enrichment, emitting figures + tables, "
                           "then a categorized report bundle.",
            "stages": [*scrna_stages, "figures + report (PDF/DOCX)"],
        },
    ]

# How a tool's declared `requires` maps to a capability check.
_CAP_KIND = {
    "scanpy": "module", "anndata": "module", "gseapy": "module", "matplotlib": "module",
    # The PyPI package is `paper-qa` but it imports as `paperqa` (no hyphen) — map it so the
    # `deep_literature` tool's `requires=("paper-qa",)` resolves instead of always failing.
    "paper-qa": "module:paperqa",
    "graphviz": "bin:dot", "pandoc": "bin:pandoc", "d2": "bin:d2", "mermaid": "bin:mmdc",
}


def _have(dep: str) -> bool:
    kind = _CAP_KIND.get(dep, f"module:{dep}")
    if kind.startswith("bin:"):
        return shutil.which(kind.split(":", 1)[1]) is not None
    name = kind.split(":", 1)[1] if ":" in kind else dep
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _full_catalog() -> list[Any]:
    """The exact catalog the live Scientist is built from — the SAME registry _run_lab uses."""
    from ..agents.registry import build_scientist_catalog

    return build_scientist_catalog()


def capabilities() -> dict[str, bool]:
    """What this host can actually run right now.

    The literature row reflects the CURRENT tiered strategy (Biomni retired): the manuscript
    References module runs on Europe PMC (Tier 2, built-in keyword API, needs only internet),
    with an env-gated remote RAG service (Crow/Edison) as Tier 1; the `deep_literature`
    Scientist tool additionally needs the local `paper-qa` package, which is usually absent.
    """
    import os

    remote_lit = bool((os.environ.get("BIOAGENT_LITERATURE_REMOTE_URL") or "").strip())
    return {
        "scanpy": _have("scanpy"), "anndata": _have("anndata"), "gseapy": _have("gseapy"),
        "matplotlib": _have("matplotlib"),
        "pandoc": _have("pandoc"), "graphviz": _have("graphviz"), "d2": _have("d2"), "mermaid": _have("mermaid"),
        # Literature stack — Europe PMC is the active fallback; Crow is dormant until env-gated; paper-qa is optional.
        "europepmc": True,
        "literature_remote": remote_lit,
        "paper-qa": _have("paper-qa"),
    }


def workflow_graph() -> dict[str, Any]:
    """A node+edge spec of the CURRENTLY IMPLEMENTED workflow, introspected from the live
    code (the role loop + specialists + the real tool catalog + presets). Read-only — it
    powers the System page's live graph, so it can never drift from the code.

    Node ``type`` in {agent, output, preset, persona, tool}; edge ``type`` in
    {flow, steer, persona, tool, pipeline}."""
    from ..agents.presets import list_presets
    from ..agents.research_lab import DEFAULT_SPECIALISTS

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    ids: set[str] = set()

    def node(nid: str, label: str, ntype: str, **meta: Any) -> None:
        nodes.append({"id": nid, "label": label, "type": ntype, "meta": meta})
        ids.add(nid)

    def edge(src: str, dst: str, label: str = "", etype: str = "flow") -> None:
        # Skip dangling edges (e.g. the scanpy chain when scanpy isn't installed) so the
        # rendered graph never references a node that doesn't exist on this host.
        if src in ids and dst in ids:
            edges.append({"source": src, "target": dst, "label": label, "type": etype})

    # Core role loop — mirrors ResearchLab.run (PI plan -> Scientist -> Critic -> synthesize).
    node("pi_plan", "PI: plan agenda", "agent", where="agents/research_lab.py",
         description="Breaks the question into an ordered agenda achievable with the available tools.")
    node("scientist", "Scientist: run tools", "agent", where="agents/research_harness.py",
         description="Executes each step with the tools / CodeAct, adopting a specialist persona.")
    node("critic", "Critic: accept / revise", "agent", where="agents/research_lab.py",
         description="Judges each step; a deterministic guard refuses to accept a failed or empty run.")
    node("pi_synth", "PI: synthesize report", "agent", where="agents/research_lab.py",
         description="Writes the final answer grounded only in accepted step results.")
    node("report", "Report bundle (PDF / DOCX)", "output", where="tools/report.py",
         description="Deterministic post-run bundle: schematic + manuscript + self-review, rendered via pandoc.")
    edge("pi_plan", "scientist")
    edge("scientist", "critic")
    edge("critic", "scientist", "revise / next step")
    edge("critic", "pi_synth", "all accepted")
    edge("pi_synth", "report")

    # Presets steer the PI's planning (they don't bypass it).
    for p in list_presets():
        pid = f"preset:{p['key']}"
        node(pid, p["label"], "preset", description=p["prompt"])
        edge(pid, "pi_plan", "steers", "steer")

    # Specialist personas the single Scientist adopts per step (prompt-only, not separate agents).
    for s in DEFAULT_SPECIALISTS:
        sid = f"persona:{s.name}"
        node(sid, s.name, "persona", description=s.persona)
        edge("scientist", sid, "adopts", "persona")

    # Tools (the skills) the Scientist can call — the same catalog _run_lab builds.
    for t in _full_catalog():
        if t.name == "finish":            # loop-control, not a research capability
            continue
        tid = f"tool:{t.name}"
        node(tid, t.name, "tool",
             category=getattr(t, "category", "general"), description=t.description,
             requires=list(t.requires), reads_private_data=t.reads_private_data,
             available=all(_have(dep) for dep in t.requires))
        edge("scientist", tid, "calls", "tool")

    # The scanpy analysis line is an ORDERED pipeline (real "Run AFTER" dependencies).
    from ..tools.scrna_pack import scrna_catalog

    chain = [f"tool:{t.name}" for t in scrna_catalog()]
    for a, b in zip(chain, chain[1:]):
        edge(a, b, "then", "pipeline")

    return {"nodes": nodes, "edges": edges}


def system_overview() -> dict[str, Any]:
    """Agents + tools (with availability) + capabilities + roadmap."""
    tools = []
    for t in _full_catalog():
        if t.name == "finish":            # loop-control, not a research capability
            continue
        available = all(_have(dep) for dep in t.requires)
        tools.append({
            "name": t.name,
            "category": getattr(t, "category", "general"),
            "description": t.description,
            "requires": list(t.requires),
            "reads_private_data": t.reads_private_data,
            "available": available,
        })
    tools.sort(key=lambda x: (x["category"], x["name"]))
    from ..agents.research_lab import DEFAULT_SPECIALISTS

    specialists = [{"name": s.name, "persona": s.persona} for s in DEFAULT_SPECIALISTS]
    return {
        "agents": AGENTS,
        "specialists": specialists,
        "workflows": workflows(),
        "graph": workflow_graph(),
        "tools": tools,
        "capabilities": capabilities(),
    }
