"""Live, code-derived overview of the system — the "what do we actually have" view.

Powers the console's **System** page: the pipeline a run goes through (its stages are the ones the
live run monitor moves runs through, see ``run_monitor.STAGES``), the agents and the model roles,
the preset pipelines, tools and skills the models are offered (each with a one-line summary and its
full text), and what this server can run itself. The page draws the running tasks on the same graph
from ``GET /api/system/live``.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
from typing import Any

# The agents of a research run. ``role`` is the ``role=`` of their model calls (``MODEL_ROLES``),
# and every one is a role of some stage in ``run_monitor.STAGES`` (tests/test_system_info.py).
AGENTS = [
    {"name": "Principal Investigator", "role": "plan",
     "description": "Reads the request's scope and the dataset profile, picks the matching pipeline and "
                    "drafts the plan as a task graph; revises it when you ask.",
     "where": "agents/research_lab.py"},
    {"name": "Critic", "role": "critic",
     "description": "Reviews the plan before you see it, then accepts each step's result or sends it back; "
                    "a deterministic guard refuses to accept a failed or empty run.",
     "where": "agents/research_lab.py"},
    {"name": "Scientist", "role": "scientist",
     "description": "Runs each step in the persona of the specialist it needs: calls the curated tools or "
                    "writes and runs code. The scheduler runs steps whose inputs are ready side by side.",
     "where": "agents/research_harness.py"},
    {"name": "Claim auditor", "role": "audit",
     "description": "Checks each claim the accepted steps support, one short question at a time, before "
                    "anything is written.",
     "where": "agents/claim_audit.py"},
    {"name": "Writer", "role": "writer",
     "description": "Writes the report from the accepted steps only; the draft is then self-reviewed and "
                    "every number is traced to the run's tables.",
     "where": "agents/research_lab.py"},
]

# Interchangeable first steps of the scanpy line: each writes the checkpoint the rest of the line
# reads, from a different input (one matrix -> run_scanpy_qc; a folder of 10x Cell Ranger libraries
# -> run_cellqc). Shown as alternatives, not as two steps run one after the other.
_SCRNA_ALTERNATIVES: dict[str, tuple[str, ...]] = {"run_scanpy_qc": ("run_cellqc",)}


def _scrna_chain(names: list[str]) -> list[str]:
    """The scanpy line's stages in catalog order, with each alternative folded into its primary."""
    alts = {a for group in _SCRNA_ALTERNATIVES.values() for a in group}
    return [n for n in names if n not in alts]


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
_ABBREVIATION = re.compile(r"(?:\b(?:e\.g|i\.e|vs|etc|cf|al|approx)|\b[A-Z])\.$", re.I)


def _first_sentence(text: str, limit: int = 160) -> str:
    """The first sentence of ``text`` as one plain line, for a list row; the full text is shown when
    the row is opened. "e.g." and the like do not end a sentence."""
    flat = " ".join(re.sub(r"[*`]", "", str(text or "")).split())
    end = next((m.start() for m in _SENTENCE_END.finditer(flat)
                if not _ABBREVIATION.search(flat[:m.start()])), len(flat))
    line = flat[:end]
    return line if len(line) <= limit else line[:limit - 1].rstrip(" ,;:—–-") + "…"


def pipelines() -> list[dict[str, Any]]:
    """The preset pipelines the PI picks from, as the System page lists them: ``title`` and
    ``summary`` from the pipeline's PROTOCOL.md (its heading and description) when it has one, and
    ``description``, the SKILL.md description the router reads, in full."""
    from ..agents.presets import list_presets

    out = []
    for p in list_presets():
        title = p.get("title") or p["key"].replace("_", " ").capitalize()
        description = " ".join(str(p.get("label") or "").split())
        out.append({"key": p["key"], "title": title,
                    "summary": _first_sentence(p.get("summary") or description),
                    "description": description, "tools": list(p.get("tools") or []),
                    "data_type": p.get("data_type") or ""})
    return out


_TEMPLATE_PREFIX = re.compile(r"^reference template\s*[—–-]\s*", re.I)
_WHEN_TO_USE = re.compile(r"^##+\s*when to use\b.*$", re.I | re.M)
_HEADING = re.compile(r"^#{1,6}\s", re.M)


def _skill_use(doc: str, limit: int = 1200) -> str:
    """The "When to use" section of a SKILL.md body (Markdown, up to the next heading), else the
    body's first paragraph; cut at a paragraph break past ``limit`` characters."""
    doc = doc or ""
    m = _WHEN_TO_USE.search(doc)
    if m:
        rest = doc[m.end():]
        nxt = _HEADING.search(rest)
        text = rest[:nxt.start()] if nxt else rest
    else:
        paras = [p for p in re.split(r"\n\s*\n", doc.strip()) if p.strip() and not _HEADING.match(p.lstrip())]
        text = paras[0] if paras else ""
    text = text.strip()
    if len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        text = (text[:cut] if cut > 0 else text[:limit]).rstrip() + " …"
    return text


def skills() -> list[dict[str, Any]]:
    """The skills the models are offered (curated and approved induced ones, the newest version of
    each), as the System page lists them: a one-line ``summary``, the full ``description`` and the
    "When to use" text, the files it bundles and the environment it runs in."""
    from ..agents.skills import SKILLS, refresh_skills, superseded_names

    refresh_skills()
    lib = dict(SKILLS)
    hidden = superseded_names(lib)
    out = []
    for s in sorted(lib.values(), key=lambda s: (s.category, s.name)):
        if s.name in hidden:
            continue
        description = _TEMPLATE_PREFIX.sub("", " ".join(str(s.summary or "").split()))
        if description.split(" ", 1)[0].islower():        # "prioritize …" after the prefix, not "scGPT"
            description = description[:1].upper() + description[1:]
        out.append({"name": s.name, "summary": _first_sentence(description),
                    "description": description, "use": _skill_use(s.doc),
                    "category": s.category, "files": sorted(s.files), "image": s.image,
                    "induced": s.induced, "supersedes": s.supersedes})
    return out


# How a tool's declared `requires` maps to a capability check of this host.
_CAP_KIND = {
    "scanpy": "module", "anndata": "module", "gseapy": "module", "matplotlib": "module",
    "graphviz": "bin:dot", "pandoc": "bin:pandoc",
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
    """What this host can run itself: the software the in-process tools and the gateway's own work
    use (the scRNA-seq stack when a run computes here, Graphviz for schematics and the report's
    workflow figure, pandoc for documents), and Europe PMC, which ``literature_search`` reaches
    over the internet. A tool that runs on HPC3 brings its software in its job's container, so it is
    not checked here: the literature corpus (``search_corpus``, ``deep_literature``) is one of them.
    Chat diagrams are Mermaid drawn by the browser, which needs nothing on this host."""
    return {
        "scanpy": _have("scanpy"), "anndata": _have("anndata"), "gseapy": _have("gseapy"),
        "matplotlib": _have("matplotlib"),
        "pandoc": _have("pandoc"), "graphviz": _have("graphviz"),
        "europepmc": True,
    }


# Where a tool runs (its manifest's ``runs_on``), as the graph shows it. ``sandbox`` is run_code's
# Slurm sandbox, which has no manifest. A new ``RUNS_ON`` value without an entry here still gets a
# box, labelled with the raw value (tests/test_system_info.py checks every value has one).
PLACES: dict[str, tuple[str, str]] = {
    "inprocess": ("Gateway (in process)",
                  "Runs inside the gateway on the Eye Server: quick reads, lookups and figures."),
    "hpc:analysis": ("HPC3 · analysis (Slurm)",
                     "One Slurm job per call in analysis.sif: the scRNA-seq line (scanpy, CellQC)."),
    "hpc:variant": ("HPC3 · variants (VEP)", "Offline VEP with its cache, in vep.sif."),
    "hpc:phenotype": ("HPC3 · phenotype (LIRICAL)", "LIRICAL phenotype-to-disease ranking, in lirical.sif."),
    "hpc:literature": ("HPC3 · literature corpus",
                       "The lab's PubMedBERT-indexed paper corpus, in paperqa.sif: search_corpus returns "
                       "the matching passages with no model on HPC3; deep_literature answers with "
                       "PaperQA2, calling the session's served model."),
    "gpu:scgpt": ("HPC3 GPU · scGPT", "A short GPU batch job per annotation."),
    "sandbox": ("HPC3 · code sandbox (Slurm)",
                "run_code: Python the Scientist writes, run as a Slurm job next to the data."),
}
_NO_MANIFEST_PLACE = {"run_code": "sandbox", "run_in_environment": "sandbox"}

# The model roles, as the Model node lists them (the ``role=`` of each model call).
MODEL_ROLES: dict[str, str] = {
    "classify": "routes a request (literature or analysis) and a follow-up, reads a request's scope, writes "
                "literature queries",
    "plan": "drafts and revises the plan",
    "critic": "reviews the plan, then accepts or sends back each step",
    "scientist": "chooses each step's next tool call",
    "audit": "checks the claims one at a time",
    "writer": "writes the report from the accepted steps",
    "literature": "answers a general question from the PubMed abstracts found",
    "chat": "answers on the Chat route",
}


def _tool_place(t: Any) -> str:
    from ..tools import catalog as tool_catalog

    m = tool_catalog.manifest(t.name)
    if m is not None:
        return m.runs_on
    return _NO_MANIFEST_PLACE.get(t.name, "inprocess")


def _tool_available(t: Any, place: str) -> bool | None:
    """Whether this host can run the tool. Only an in-process tool runs here: a tool that runs on
    HPC3 has its software in its job's container, so the gateway's own modules say nothing about it
    (None: not checked here)."""
    if place != "inprocess":
        return None
    return all(_have(dep) for dep in t.requires)


def workflow_graph() -> dict[str, Any]:
    """The pipeline a run actually goes through, built from the code that runs it, so it cannot drift:

    - ``stage`` nodes are ``run_monitor.STAGES``, the same list the live monitor moves runs
      through (``meta.kinds`` = which run kinds pass through, ``meta.roles`` = the model roles at
      work there); ``flow`` edges follow each kind's stage order;
    - ``agent`` nodes are the step loop (Scientist, Critic) and the Model every role calls;
    - ``place`` nodes are where tools run (``PLACES``), ``tool`` nodes the live Scientist catalog,
      each under the place its manifest names; ``pipeline`` edges are the scRNA line's real order.

    Node ``type`` in {stage, agent, place, tool}; edge ``type`` in
    {flow, skip, step, tool, pipeline}."""
    from ..agents.presets import list_presets
    from ..agents.research_lab import DEFAULT_SPECIALISTS
    from . import run_monitor

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    ids: set[str] = set()

    def node(nid: str, label: str, ntype: str, **meta: Any) -> None:
        nodes.append({"id": nid, "label": label, "type": ntype, "meta": meta})
        ids.add(nid)

    def edge(src: str, dst: str, label: str = "", etype: str = "flow") -> None:
        # Skip dangling edges so the rendered graph never references a node that doesn't exist.
        if src in ids and dst in ids:
            edges.append({"source": src, "target": dst, "label": label, "type": etype})

    presets = [{"key": p["key"], "label": p["label"], "tools": list(p.get("tools") or [])}
               for p in list_presets()]
    for i, st in enumerate(run_monitor.STAGES):
        extra: dict[str, Any] = {}
        if st.id == "plan":
            extra["pipelines"] = presets
        node(st.id, st.label, "stage", order=i, kinds=list(st.kinds), roles=list(st.roles),
             description=st.detail, where=st.where, **extra)
    for kind in run_monitor.KINDS:
        chain = [st.id for st in run_monitor.STAGES if kind in st.kinds]
        for a, b in zip(chain, chain[1:]):
            edge(a, b, "", "flow")
    # The shortcut a research run really takes: a run that only did quality control is answered in
    # the chat with no audit or write-up. Every plan is reviewed (issue 21), so none skips "approve".
    edge("execute", "report", "QC only", "skip")

    node("scientist", "Scientist", "agent", where="agents/research_harness.py",
         description="Carries out one step at a time in the persona of the specialist it needs: calls "
                     "the tools below, or writes and runs code. Independent steps may run side by side.",
         specialists=[{"name": s.name, "persona": s.persona} for s in DEFAULT_SPECIALISTS])
    node("critic", "Critic", "agent", where="agents/research_lab.py",
         description="Accepts a step's result or sends it back with what to fix; a deterministic guard "
                     "refuses to accept a failed or empty run.")
    node("model", "Model", "agent", where="gateway/app.py",
         description="The session's language model (the cluster's own model on HPC3, the shared server, "
                     "or the account's API key). Every role calls it.",
         roles=[{"role": r, "does": d} for r, d in MODEL_ROLES.items()])
    edge("execute", "scientist", "hands a step", "step")
    edge("scientist", "critic", "result", "step")
    edge("critic", "scientist", "revise", "step")

    tools = [t for t in _full_catalog() if t.name != "finish"]     # finish is loop control
    used_places: list[str] = []
    for t in tools:
        place = _tool_place(t)
        if place not in used_places:
            used_places.append(place)
    order = list(PLACES)
    used_places.sort(key=lambda p: order.index(p) if p in order else len(order))
    for place in used_places:
        label, desc = PLACES.get(place, (place, ""))
        node(f"place:{place}", label, "place", runs_on=place, description=desc)
        edge("scientist", f"place:{place}", "calls", "tool")
    for t in tools:
        place = _tool_place(t)
        node(f"tool:{t.name}", t.name, "tool", place=f"place:{place}", runs_on=place,
             category=getattr(t, "category", "general"), description=t.description,
             requires=list(t.requires), reads_private_data=t.reads_private_data,
             available=_tool_available(t, place))

    # The scanpy analysis line is an ORDERED pipeline (real "Run AFTER" dependencies).
    from ..tools.api import scrna_catalog

    names = [t.name for t in scrna_catalog()]
    chain = [f"tool:{n}" for n in _scrna_chain(names)]
    for a, b in zip(chain, chain[1:]):
        edge(a, b, "then", "pipeline")
    for primary, alts in _SCRNA_ALTERNATIVES.items():
        nxt = chain[chain.index(f"tool:{primary}") + 1] if f"tool:{primary}" in chain[:-1] else None
        for alt in alts:
            if nxt and alt in names:
                edge(f"tool:{alt}", nxt, "then", "pipeline")    # an alternative feeds the same step

    return {"nodes": nodes, "edges": edges}


def system_overview() -> dict[str, Any]:
    """Agents + pipelines + tools (with where they run) + skills + capabilities + the graph."""
    from ..tools import catalog as tool_catalog

    tools = []
    for t in _full_catalog():
        if t.name == "finish":            # loop-control, not a research capability
            continue
        place = _tool_place(t)
        m = tool_catalog.manifest(t.name)
        tools.append({
            "name": t.name,
            "category": getattr(t, "category", "general"),
            # A tool folder's TOOL.md has a one-line summary; a registry tool has only what the
            # model is told, whose first sentence stands in for it.
            "summary": m.summary if m is not None else _first_sentence(t.description),
            "description": t.description,
            "code": f"src/aiscientist/tools/{m.folder.name}/" if m is not None else "",
            "requires": list(t.requires),
            "reads_private_data": t.reads_private_data,
            "runs_on": place,
            "where": PLACES.get(place, (place, ""))[0],
            "available": _tool_available(t, place),
        })
    tools.sort(key=lambda x: (x["category"], x["name"]))
    from ..agents.research_lab import DEFAULT_SPECIALISTS

    specialists = [{"name": s.name, "persona": s.persona} for s in DEFAULT_SPECIALISTS]
    return {
        "agents": AGENTS,
        "specialists": specialists,
        "pipelines": pipelines(),
        "graph": workflow_graph(),
        "tools": tools,
        "skills": skills(),
        "capabilities": capabilities(),
    }
