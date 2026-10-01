"""What this deployment actually HAS, and where it is.

``system_info`` answers "which agents and tools exist" for the console's System page. This answers
the question that kept costing real runs: **where do the files live, and may this session read
them?** Nothing carried that answer. The paths sit in ``settings.py`` as Python defaults and in
READMEs no agent ever opens, so a capability could be switched off by a path nobody had listed and
every error channel stayed clean:

* run 3c5fbc8608a7's scGPT step was told to verify the model's reference and taxonomy before
  inference. The model directory was not in the session's read roots, so it could not look, so it
  withheld inference — and the Critic scored the withholding 0.95, correctly.
* the same run's pathway step hand-computed a score the tools do not offer, went looking for a
  gene-set collection to download, hit the confirmation guarding non-allowlisted hosts and was
  declined — with three verified ``.gmt`` libraries on disk in the directory the enrichment tools
  read from.

Both are the same shape: the thing was there, and nothing said so. Everything below is derived
from the live code and the live settings, so it cannot drift from what the deployment is; the one
place it can be wrong is a remote path this host cannot stat, which is reported as unknown rather
than guessed.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

#: Where the repo root is, so tool source locations render as repo-relative paths a person can
#: paste into an editor rather than absolute paths that differ on every machine.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _rel(path: str | Path) -> str:
    p = Path(path)
    try:
        return str(p.resolve().relative_to(_REPO_ROOT))
    except (ValueError, OSError):
        return str(p)


def _source_of(obj: Any) -> str:
    """``file:line`` for whatever actually implements a tool, or "" when it cannot be located.

    Runners are frequently closures, ``partial``s or bound methods built by the gateway, so unwrap
    the common wrappers before asking. A tool whose source cannot be found is reported as such:
    guessing a location is worse than admitting one is unknown, because a wrong path sends a reader
    somewhere real and irrelevant.
    """
    target = obj
    for _ in range(5):
        nxt = getattr(target, "func", None) or getattr(target, "__wrapped__", None)
        if nxt is None:
            break
        target = nxt
    try:
        file = inspect.getsourcefile(target)
        if not file:
            return ""
        _src, line = inspect.getsourcelines(target)
        return f"{_rel(file)}:{line}"
    except (TypeError, OSError):
        return ""


def _first_sentence(text: str, limit: int = 220) -> str:
    one = " ".join((text or "").split())
    cut = one.find(". ")
    out = one[: cut + 1] if 0 < cut < limit else one[:limit]
    return out.rstrip()


def tool_index(catalog: list[Any] | None = None) -> list[dict[str, Any]]:
    """Every tool the Scientist can call: what it is for, and WHERE IT IS DEFINED.

    The source location is the half nobody could get at. "Which tools exist" is answered by the
    schemas the model already receives and by the System page; "where is the function that does
    this" was answered only by grepping, which is fine for the person who wrote it and useless to
    everyone else.
    """
    from . import system_info

    cat = catalog if catalog is not None else system_info._full_catalog()
    out: list[dict[str, Any]] = []
    for t in cat:
        if t.name == "finish":               # loop control, not a research capability
            continue
        requires = list(getattr(t, "requires", ()) or ())
        out.append({
            "name": t.name,
            "category": getattr(t, "category", "general"),
            "purpose": _first_sentence(getattr(t, "description", "")),
            "source": _source_of(getattr(t, "executor", None)),
            "requires": requires,
            "available": all(system_info._have(d) for d in requires),
            "enabled": bool(getattr(t, "enabled", True)),
        })
    out.sort(key=lambda x: (x["category"], x["name"]))
    return out


def asset_inventory(settings: Any = None) -> list[dict[str, Any]]:
    """The container images, model weights and reference data this deployment uses.

    ``where`` is "gateway" for paths on the host running this process (checkable) and "hpc3" for
    paths on the cluster (not checkable from here — reported, never guessed).
    """
    if settings is None:
        from .settings import HPCSettings
        settings = HPCSettings.from_env()

    def _local(kind: str, label: str, path: str | Path, note: str = "") -> dict[str, Any]:
        p = Path(path)
        exists = p.exists()
        size = None
        if exists:
            try:
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
            except OSError:
                size = None
        return {"kind": kind, "label": label, "path": str(p), "where": "gateway",
                "exists": exists, "size_mb": round(size / (1 << 20), 1) if size else None,
                "note": note}

    def _remote(kind: str, label: str, path: str, note: str = "") -> dict[str, Any]:
        return {"kind": kind, "label": label, "path": path, "where": "hpc3",
                "exists": None, "size_mb": None, "note": note}

    from ..tools.api import genesets_dir as _genesets_dir

    gmt_dir = _genesets_dir()
    gmts = sorted(p.name for p in gmt_dir.glob("*.gmt")) if gmt_dir.is_dir() else []
    items = [
        _local("gene_sets", "Gene-set libraries (.gmt) for run_enrichment / run_gsea_prerank",
               gmt_dir,
               "Read OFFLINE by the enrichment tools — no network, no Enrichr API. Present: "
               + (", ".join(gmts) if gmts else "NONE — run scripts/fetch_genesets.py")
               + ". BIOAGENT_GENESETS_DIR overrides this location. A pathway summary computed by "
                 "hand should read these same files rather than fetch a collection."),
        _remote("container", "Analysis image (run_code, scanpy line)", settings.analysis_image,
                "Fixed at build time and read-only. A package it lacks is resolved into the shared "
                "cache below, not installed into the image."),
        _remote("container", "scGPT image", settings.scgpt_image),
        _remote("model", "scGPT weights + vocabulary + label map", settings.scgpt_model_dir,
                "best_model.pt, vocab.json, id2type.json, dev_train_args.yml. scgpt_annotate binds "
                "this read-only into its GPU job. HUMAN RETINA reference: a human-symbol vocabulary "
                "(TFRC, not Tfrc) and 123 human retinal cell types (HAC*, HRGC*, DB1-6, RB, Rod, "
                "S_Cone, ML_Cone, MG, Astrocyte, Microglia, RPE; NO endothelial or pericyte class). "
                "A mouse query is case-folded onto it automatically and the transfer is recorded in "
                "species_harmonization.json. Measured on the DDX41 mouse retina: neurons agree with the "
                "supplied labels (rods 99.8%), but 73% of Muller glia come back 'Astrocyte' and every "
                "endothelial cell 'Microglia', at confidence >= 0.94 — confidence is not accuracy."),
        _remote("model", "Vision model for the post-render report review", settings.vlreview_model_dir),
        _remote("cache", "Shared Python package cache (preflight installs land here)",
                f"{settings.shared_root.rstrip('/')}/pkgs",
                "Immutable and content-keyed; appended to sys.path inside the container by a "
                "generated sitecustomize."),
        _remote("reference", "Lab reference data (VEP caches, LIRICAL data, SpliceAI models)",
                f"{settings.lab_storage.rstrip('/')}/software/reference"),
        _remote("gene_sets", "Gene-set libraries (.gmt), the copy HPC3 jobs can read",
                f"{settings.shared_root.rstrip('/')}/pysrc/<user>/bioagent/tools/genesets",
                "The same files as the gateway copy, synced with the live source for analysis jobs. "
                "They list human (upper-case) symbols: run_enrichment / run_gsea_prerank matched "
                "mouse genes in past runs, but code that reads a .gmt directly must upper-case "
                "mouse symbols first."),
        _remote("workspace", "Per-user process files (swept on a TTL)",
                f"{settings.shared_root.rstrip('/')}/Temp/<user>"),
        _remote("workspace", "Per-user uploads (never swept)",
                f"{settings.shared_root.rstrip('/')}/uploads/<user>"),
    ]
    return items


def environment_manifest(settings: Any = None, *, read_roots: tuple[str, ...] = (),
                         write_roots: tuple[str, ...] = ()) -> dict[str, Any]:
    """Everything above in one structure, plus the session's actual filesystem permissions.

    ``read_roots``/``write_roots`` are passed in because they are built per SESSION from the HPC3
    account (see ``_build_hpc_shell``); an empty tuple means "not a live cluster session", which is
    itself worth saying rather than implying the agent may read nothing.
    """
    from . import system_info

    if settings is None:
        from .settings import HPCSettings
        settings = HPCSettings.from_env()
    assets = asset_inventory(settings)
    for a in assets:
        a["agent_readable"] = _readable(a["path"], read_roots) if read_roots else None
    return {
        "tools": tool_index(),
        "assets": assets,
        "roots": {"readable": list(read_roots), "writable": list(write_roots)},
        "capabilities": system_info.capabilities(),
    }


def _readable(path: str, roots: tuple[str, ...]) -> bool:
    from ..hpc.shell import _under_any
    return any(_under_any(path.rstrip("/"), (r,)) for r in roots)


def render_markdown(manifest: dict[str, Any]) -> str:
    """The same manifest as a document a person reads.

    One renderer for both audiences on purpose: a hand-maintained copy of this would be wrong
    within a month, and the version the agent sees would then disagree with the version the team
    reads — which is worse than having neither.
    """
    lines = ["# What this deployment has, and where it is", "",
             "Generated from the live code and settings by `scripts/write_environment_doc.py`.",
             "Do not edit by hand — regenerate it.", ""]

    lines += ["## Assets", "",
              "| what | where | path | agent may read |", "|---|---|---|---|"]
    for a in manifest["assets"]:
        readable = {True: "yes", False: "**NO**", None: "—"}[a.get("agent_readable")]
        size = f" ({a['size_mb']} MB)" if a.get("size_mb") else ""
        lines.append(f"| {a['label']} | {a['where']} | `{a['path']}`{size} | {readable} |")
    lines.append("")
    for a in manifest["assets"]:
        if a.get("note"):
            lines += [f"**{a['label']}** — {a['note']}", ""]

    roots = manifest["roots"]
    lines += ["## This session's filesystem permissions", ""]
    if roots["readable"]:
        lines += ["Readable:", ""] + [f"- `{r}`" for r in roots["readable"]] + [""]
        lines += ["Writable:", ""] + [f"- `{r}`" for r in roots["writable"]] + [""]
    else:
        lines += ["_No live cluster session — roots are built per session from the HPC3 account._", ""]

    lines += ["## Tools", "",
              "`source` is where the function actually lives, so a tool can be found without grepping.",
              "", "| tool | what it does | source | available |", "|---|---|---|---|"]
    for t in manifest["tools"]:
        ok = "yes" if t["available"] and t["enabled"] else "**no**"
        src = f"`{t['source']}`" if t["source"] else "—"
        lines.append(f"| `{t['name']}` | {t['purpose']} | {src} | {ok} |")
    lines.append("")

    caps = manifest["capabilities"]
    lines += ["## Host capabilities", "",
              " · ".join(f"{k}: {'yes' if v else 'no'}" for k, v in sorted(caps.items())), ""]
    return "\n".join(lines)


def render_for_agent(manifest: dict[str, Any], section: str = "all") -> str:
    """The compact rendering the ``describe_environment`` tool returns.

    Deliberately not the markdown document: this is read inside a bounded context window that the
    Scientist loop already trims, so it carries the facts a step acts on — paths, readability,
    availability — and drops the prose a person wants.
    """
    out: list[str] = []
    if section in ("all", "assets"):
        out.append("ASSETS (path | readable by this session)")
        for a in manifest["assets"]:
            r = {True: "readable", False: "NOT READABLE", None: "unknown"}[a.get("agent_readable")]
            out.append(f"  [{a['kind']}] {a['label']}: {a['path']} | {r}")
            if a.get("note"):
                out.append(f"      {a['note']}")
    if section in ("all", "roots"):
        roots = manifest["roots"]
        out.append("READABLE ROOTS: " + (", ".join(roots["readable"]) or "(none — not a cluster session)"))
        out.append("WRITABLE ROOTS: " + (", ".join(roots["writable"]) or "(none)"))
    if section in ("all", "tools"):
        out.append("TOOLS (name | source | available)")
        for t in manifest["tools"]:
            flag = "" if (t["available"] and t["enabled"]) else "  [UNAVAILABLE in this deployment]"
            out.append(f"  {t['name']} | {t['source'] or '?'}{flag}")
    return "\n".join(out)
