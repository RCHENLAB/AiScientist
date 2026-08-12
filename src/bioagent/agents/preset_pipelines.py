"""Preset-pipeline subsystem — fixed, reproducible, prompt-driven research workflows.

A *preset pipeline* is a folder ``preset_pipelines/<name>/`` (repo root): a ``SKILL.md``
(frontmatter ``name`` + ``description`` + ``tools`` and a markdown body that is the default PI
guidance) plus, for now, a ``scripts/`` folder of ``*.py`` CodeAct templates. Dropping a new folder
adds a research path — no code change here. Override the location with ``$BIOAGENT_PIPELINES_DIR``
(``$BIOAGENT_SKILLS_DIR`` is still honoured as a fallback for older configs).

A preset pipeline *steers* the PI's planning toward a known workflow shape — it does NOT bypass the
PI. The PI still drafts the agenda (adapting the guidance to the actual dataset/question), plan mode
still hands that draft to the user to review/edit, and the Scientist+Critic still execute+verify
each step. So a preset pipeline = "a good starting prompt the user can tweak".

**Vocabulary (see ``docs/skills_and_pipelines_architecture.md``).** A preset pipeline is a *fixed
end-to-end workflow* that composes atomic capabilities. It is distinct from an atomic **skill**
(``agents/skills.py`` — a rewritable code capability surfaced on demand, which now owns the
``read_skill_reference`` progressive-disclosure tool) and from the fixed **registry** tools
(``agents/registry.py``). The reference ``scripts/*.py`` that used to be bundled per-pipeline have
migrated to the shared atomic-skill library ``skills/``.

**What this module owns:**
  - the data model — :class:`PresetPipeline`
  - loading — :data:`PIPELINES`, :func:`get_pipeline`, :func:`list_pipelines`
  - dataset-aware routing — :func:`select_pipeline` (the PI picks a best-fit pipeline itself)
  - composition — :func:`compose_pipeline_prompts` (merge pinned + auto pipelines into ONE block)

``presets.py`` re-exports this surface under the legacy ``ResearchPreset`` / ``PRESETS`` /
``get_preset`` / ``list_presets`` names — the frontend-facing "preset" view of the same registry.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .loop_utils import safe_json_loads
from .research_harness import EventFn


# --- data model --------------------------------------------------------------

@dataclass(frozen=True)
class PresetPipeline:
    """One preset pipeline: the PI-steering prompt plus the capability boundary it composes.
    (The adaptable CodeAct templates a pipeline used to bundle now live in the shared atomic-skill
    library ``skills/`` — see ``agents/skills.py`` — surfaced by progressive disclosure.)"""
    key: str
    label: str
    prompt: str   # default guidance injected into the PI's planning (user-editable)
    # The registered function-tools this pipeline composes (frontmatter ``tools:``) — the
    # capability layer the pipeline *calls*; it never reimplements these. Surfaced on the System page.
    tools: tuple[str, ...] = ()
    # The data MODALITY this pipeline is for (frontmatter ``data_type:`` — e.g. ``scrna`` for a
    # single-cell .h5ad, ``variants`` for a VCF). Empty = modality-agnostic. Used to reconcile a
    # user-PINNED pipeline against the dataset-derived auto pick: when they name different modalities,
    # the dataset wins (a scanpy pipeline is never forced onto a VCF). See ``research_lab.run``.
    data_type: str = ""


# Legacy alias: ``presets.py`` and older call sites refer to a "ResearchPreset".
ResearchPreset = PresetPipeline


# --- loading -----------------------------------------------------------------

def _pipelines_dir() -> Path:
    """The preset-pipeline library: ``$BIOAGENT_PIPELINES_DIR`` or repo-root ``preset_pipelines/``.
    (``$BIOAGENT_SKILLS_DIR`` now points at the atomic-skill library — see ``agents/skills.py``.)"""
    env = os.environ.get("BIOAGENT_PIPELINES_DIR")
    if env:
        return Path(env)
    # preset_pipelines.py -> agents -> bioagent -> src -> <repo root>
    return Path(__file__).resolve().parents[3] / "preset_pipelines"


def _parse_skill(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into (frontmatter dict, body). Frontmatter is the leading
    ``---``-delimited block of simple ``key: value`` lines; body is the rest."""
    meta: dict[str, str] = {}
    body = text
    if text.lstrip().startswith("---"):
        rest = text.lstrip()[3:]
        end = rest.find("\n---")
        if end != -1:
            front = rest[:end]
            body = rest[end + 4:]
            for line in front.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
    return meta, body.strip()


def _load_pipelines() -> dict[str, PresetPipeline]:
    """Load every ``preset_pipelines/<name>/SKILL.md`` into the registry (sorted by name).
    Skips folders without a readable SKILL.md or without frontmatter ``name``/body."""
    root = _pipelines_dir()
    out: dict[str, PresetPipeline] = {}
    if not root.is_dir():
        return out
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            meta, body = _parse_skill(skill_md.read_text(encoding="utf-8"))
        except OSError:
            continue
        key = meta.get("name") or skill_md.parent.name
        if not key or not body:
            continue
        tools = tuple(t.strip() for t in meta.get("tools", "").split(",") if t.strip())
        out[key] = PresetPipeline(
            key=key,
            label=meta.get("description", key),
            prompt=body,
            tools=tools,
            data_type=meta.get("data_type", "").strip().lower(),
        )
    return out


PIPELINES: dict[str, PresetPipeline] = _load_pipelines()


def get_pipeline(key: str | None) -> PresetPipeline | None:
    """The preset pipeline for ``key``, or None (unknown key / nothing selected)."""
    return PIPELINES.get(key) if key else None


def list_pipelines() -> list[dict]:
    """All pipelines as plain dicts (for the frontend selector + editable prompt). ``tools`` is
    additive — the selector ignores keys it doesn't use."""
    return [
        {
            "key": p.key,
            "label": p.label,
            "prompt": p.prompt,
            "tools": list(p.tools),
            "data_type": p.data_type,
        }
        for p in PIPELINES.values()
    ]


# --- PI-autonomous, dataset-aware routing ------------------------------------

PIPELINE_SELECT_SYSTEM = (
    "You are the lab's research-protocol router. Given a research question, a profile of the loaded "
    "dataset, and a short list of known protocols (each a key + one-line description), decide whether "
    "ONE protocol clearly fits. Use the DATASET, not just the wording of the question (the question "
    "may be vague like 'complete the research') — e.g. a VCF of variants → the variant-annotation "
    "protocol; an .h5ad already carrying cell-type/majorclass labels → the annotation / "
    "cross-validation path. The researcher does not know which protocol they need — that is your "
    'call. Reply with ONLY the matching protocol\'s key (exactly as written), or the word "none" if '
    "no protocol clearly fits (the lab will then plan from scratch). Do not explain."
)


def parse_pipeline_choice(raw: str, keys: list[str]) -> str | None:
    """Extract the chosen pipeline key from the router's reply (tolerating a bare word, quotes,
    a ``{"key": ...}`` object, or a surrounding sentence). Returns a key from ``keys``, or
    None for "none"/no match. Longest key first so a short key can't shadow a longer one."""
    text = (raw or "").strip()
    obj = safe_json_loads(text)
    if isinstance(obj, dict):
        text = str(obj.get("key") or obj.get("protocol") or obj.get("name") or "")
    low = text.lower()
    for k in sorted(keys, key=len, reverse=True):
        if k.lower() in low:
            return k
    return None


# Feature ①'s ``likely_modality`` vocabulary → a pipeline's ``data_type`` frontmatter. Direct for the
# two real buckets (``variants`` / ``scrna``); a few synonyms are mapped so a model that answered
# "variant" or "single_cell" still routes. A modality with no entry AND no matching pipeline (tabular,
# text, image, bulk_rna, unknown) simply has no bucket → routing falls back to the suffix/LLM path.
_MODALITY_TO_DATA_TYPE = {
    "variants": "variants", "variant": "variants", "vcf": "variants",
    "scrna": "scrna", "sc_rna": "scrna", "single_cell": "scrna", "scrnaseq": "scrna",
}


def select_pipeline(
    complete: Callable[[list[dict[str, Any]]], str],
    question: str,
    dataset_hint: str = "",
    library: "tuple[PresetPipeline, ...] | None" = None,
    emit: EventFn | None = None,
    *,
    content_modality: str = "",
    content_confidence: str = "",
) -> PresetPipeline | None:
    """The PI picks the best-matching preset pipeline from the library itself, by its one-line
    descriptions AND the loaded dataset's profile (so a VCF / annotated .h5ad routes right even
    from a vague question). The researcher does not choose. None = plan from scratch.

    ``complete`` is the lab's chat callable (messages -> reply text). Emits ``skill_selected``.

    CONTENT OVERRIDES SUFFIX (feature ② Phase B): when ``content_modality`` is a real modality from
    feature ①'s run-start triage (``peek_dataset``/``describe_dataset``) and its confidence is not
    ``low``, routing is RESTRICTED to the pipelines of that modality — the file's actual bytes, not its
    extension, decide the bucket. So a ``.txt``-named file that is really a VCF routes to a variant
    protocol even though its suffix says text. A single match in that bucket is chosen deterministically
    (no LLM call); several → the LLM picks WITHIN the bucket. When triage is unavailable, low-confidence,
    or names a modality with no matching pipeline, this falls back to the full-library LLM router whose
    dataset hint is the suffix-derived profile — i.e. the suffix is the fallback, not the primary."""
    if library is None:
        library = tuple(PIPELINES.values())
    if not library:
        return None

    # --- content-derived modality routing (the override) ---
    modality = (content_modality or "").strip().lower()
    data_type = _MODALITY_TO_DATA_TYPE.get(modality, modality)
    low_conf = (content_confidence or "").strip().lower() == "low"
    route_library = library
    content_routed = False
    if data_type and data_type not in ("", "unknown") and not low_conf:
        matches = tuple(p for p in library if p.data_type == data_type)
        if matches:
            content_routed = True
            if len(matches) == 1:
                chosen = matches[0]
                if emit is not None:
                    emit({"type": "skill_selected", "key": chosen.key, "label": chosen.label,
                          "reason": f"content:{data_type}"})
                return chosen
            route_library = matches   # >1 in the bucket → let the LLM pick within it
            dataset_hint = (f"The uploaded file's CONTENT is {data_type} data (detected by skimming the "
                            f"bytes, not the filename).\n" + dataset_hint).strip()

    listing = "\n".join(f"- {p.key}: {p.label}" for p in route_library)
    raw = complete([
        {"role": "system", "content": PIPELINE_SELECT_SYSTEM},
        {"role": "user", "content": (
            f"Research question:\n{question}\n\n"
            + (f"{dataset_hint}\n\n" if dataset_hint else "")
            + f"Known research protocols:\n{listing}\n\n"
            'Reply with ONLY the key of the single best-matching protocol, or "none".'
        )},
    ])
    key = parse_pipeline_choice(raw, [p.key for p in route_library])
    chosen = next((p for p in route_library if p.key == key), None)
    if emit is not None:
        emit({"type": "skill_selected",
              "key": chosen.key if chosen else None,
              "label": chosen.label if chosen else None,
              **({"reason": f"content:{data_type}"} if content_routed and chosen else {})})
    return chosen


# --- composition -------------------------------------------------------------

def drop_conflicting_pinned(
    pinned: "list[PresetPipeline]", chosen: "PresetPipeline | None",
) -> "tuple[list[PresetPipeline], list[PresetPipeline]]":
    """Reconcile the user-PINNED pipelines against the dataset-derived auto pick ``chosen``: drop any
    pinned pipeline whose ``data_type`` (modality) differs from the auto pick's, since the auto pick
    reads the actual dataset and is therefore ground truth on modality (a scanpy pipeline must never be
    forced onto a VCF). Modality-agnostic pipelines (no ``data_type``) and a chosen with no
    ``data_type`` never trigger a drop. Returns ``(kept, dropped)``."""
    if chosen is None or not chosen.data_type:
        return list(pinned), []
    dropped = [p for p in pinned if p.data_type and p.data_type != chosen.data_type]
    kept = [p for p in pinned if p not in dropped]
    return kept, dropped


def compose_pipeline_prompts(pipelines: "list[PresetPipeline]") -> str:
    """Compose the loaded pipelines (pinned + auto-selected) into ONE PI-steering block. One → its
    prompt verbatim; several → labeled sections + a reconcile header (they share a QC→clustering
    backbone, so don't blindly run every step of each). Empty → ''. Skips None."""
    pipelines = [p for p in pipelines if p is not None]
    if not pipelines:
        return ""
    if len(pipelines) == 1:
        return pipelines[0].prompt
    header = (
        f"{len(pipelines)} research protocols are active for this study. Apply each where it is relevant "
        "and RECONCILE them into ONE coherent plan — do not blindly run every step of each (they share "
        "a QC→clustering backbone; do that once). Where they conflict, prefer the more specific protocol."
    )
    return header + "\n\n" + "\n\n".join(f"## Protocol: {p.label}\n{p.prompt}" for p in pipelines)
