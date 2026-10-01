"""Render-review REWORK loop: render the report, have a vision model look at the rendered
pages, and if it finds a layout defect a text model cannot see (text overlap, clipping, table
overflow, a caption on the figure), RE-RENDER with escalated formatting — then look again.

This is the piece that lets "the render step return and adjust the format". The vision model
(``deploy/vlreview`` on a short-lived cheap GPU, or any injected reviewer) never edits the
document; it only reports defects, each tagged with a fix directive from a fixed vocabulary.
This module translates those directives into concrete :func:`build_pdf_report` format overrides
and walks an ESCALATION LADDER (footnotesize -> scriptsize -> tiny; 11pt -> 10pt -> 9pt; ...)
so each re-render genuinely changes the layout. When the ladder is exhausted or the page reads
clean, the loop stops; any residual defects are returned for the technical-report Diagnostics
section (the manuscript itself always ships clean — see the silent-degradation design).

Both the renderer and the reviewer are injected callbacks, so the whole loop runs offline in
tests against fakes — no pandoc, no GPU, no Slurm — exactly like the RemoteExecutor pattern.

    render_fn(format_overrides: dict) -> dict   # e.g. functools.partial(build_pdf_report, ...)
                                                # must return {"pdf_path": str|None, ...}
    review_fn(pdf_path: str)        -> dict     # e.g. wraps gateway.vlreview_job.run_vlreview
                                                # must return {"clean": bool,
                                                #              "fix_directives": [str],
                                                #              "defects": [ {...} ]}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# Escalation ladders: each fix directive maps to a format-override key and the ordered rungs
# it steps through on repeat offenses. The FIRST rung is the default (equal to DEFAULT_FORMAT),
# so applying a directive always moves to a MORE aggressive setting than the current one.
_LADDERS: dict[str, tuple[str, list[Any]]] = {
    "shrink_table_font":     ("table_font",        [r"\footnotesize", r"\scriptsize", r"\tiny"]),
    "shrink_body_font":      ("fontsize",          ["11pt", "10pt", "9pt"]),
    "widen_margins":         ("geometry_margin",   ["1in", "0.75in", "0.5in"]),
    "scale_figures":         ("figure_max_width",  [None, r"0.85\linewidth", r"0.7\linewidth"]),
    "force_table_wrap":      ("wide_table_cols",   [4, 3, 2]),
    "landscape_wide_tables": ("landscape_wide_tables", [False, True]),
}


def apply_fix_directives(overrides: dict[str, Any], directives: list[str]) -> dict[str, Any]:
    """Return a NEW overrides dict advanced one rung along each directive's ladder. A directive
    whose ladder is already at its last rung is left as-is (so a no-op result signals the loop
    that this defect can no longer be improved by re-rendering)."""
    out = dict(overrides)
    for directive in directives:
        ladder = _LADDERS.get(directive)
        if not ladder:
            continue
        key, rungs = ladder
        current = out.get(key, rungs[0])
        try:
            idx = rungs.index(current)
        except ValueError:
            idx = 0
        out[key] = rungs[min(idx + 1, len(rungs) - 1)]
    return out


@dataclass
class VisualReviewOutcome:
    """Result of the render-review loop."""

    render: dict                              # the FINAL build_pdf_report() return
    review: dict | None                       # the FINAL reviewer verdict (None if never run)
    overrides: dict[str, Any]                 # the format overrides that produced `render`
    attempts: int                             # how many render passes ran
    history: list[dict] = field(default_factory=list)   # per-attempt {overrides, clean, n_defects}

    @property
    def clean(self) -> bool:
        return bool(self.review and self.review.get("clean"))

    @property
    def residual_defects(self) -> list[dict]:
        """Defects still present in the final pass (empty when clean)."""
        if not self.review or self.review.get("clean"):
            return []
        return list(self.review.get("defects", []))

    @property
    def vl_unavailable(self) -> bool:
        """True when the VISION model never loaded and only the deterministic geometric
        (bbox-overlap) pre-check ran — the reviewer reports ``model == "bbox-only"``. Its verdict
        can be ``clean`` while the actual visual review DID NOT happen, so this must be surfaced
        as a DEGRADATION (not reported as a clean pass)."""
        return bool(self.review and str(self.review.get("model", "")).lower() == "bbox-only")

    @property
    def vl_unavailable_note(self) -> str:
        """The reviewer's reason the vision model was unavailable (e.g. the ImportError), if any."""
        for d in (self.review or {}).get("defects", []):
            note = str(d.get("note", ""))
            if "unavailable" in note.lower():
                return note
        return ""


def render_with_visual_review(
    render_fn: Callable[[dict], dict],
    review_fn: Callable[[str], dict],
    *,
    max_iters: int = 3,
    initial_render: dict | None = None,
    emit: Callable[[str, str, str], None] | None = None,
) -> VisualReviewOutcome:
    """Render -> visually review -> re-render with escalated format, up to ``max_iters`` passes.

    ``initial_render`` lets the caller hand in the render it ALREADY produced (the report is
    rendered once in the finalization pipeline before this runs), so attempt 1 reuses it instead
    of rendering the identical PDF a second time; only re-renders (with escalated format) cost
    extra pandoc runs.

    Stops early when the reviewer reports the pages clean, when there is no PDF to review, or
    when the escalation ladder can no longer change the layout. Never raises for a defect — the
    goal is a best-effort clean render plus an honest list of anything left over."""

    def _emit(level: str, msg: str) -> None:
        if emit:
            emit(level, "lab", msg)

    overrides: dict[str, Any] = {}
    last_render: dict = {}
    last_review: dict | None = None
    history: list[dict] = []
    attempts = 0

    for attempt in range(1, max_iters + 1):
        attempts = attempt
        # Attempt 1 reuses the caller's existing render (same empty overrides); later attempts
        # re-render with the escalated format the ladder produced.
        last_render = initial_render if (attempt == 1 and initial_render is not None) else render_fn(overrides)
        pdf_path = last_render.get("pdf_path")
        if not pdf_path:
            # No PDF (pandoc absent, or PDF format not requested) — nothing to look at.
            _emit("info", "Visual review skipped: no rendered PDF to inspect.")
            break

        last_review = review_fn(pdf_path)
        n_defects = len(last_review.get("defects", []))
        history.append({"attempt": attempt, "overrides": dict(overrides),
                        "clean": bool(last_review.get("clean")), "n_defects": n_defects})

        if last_review.get("clean"):
            # A "clean" verdict from the geometric-only fallback does NOT mean the pages were
            # visually reviewed — the vision model never ran. Report that as a DEGRADATION, not clean.
            if str(last_review.get("model", "")).lower() == "bbox-only":
                _emit("warning",
                      "Visual review DEGRADED: the vision model was unavailable — only the geometric "
                      "pre-check ran, so the pages were NOT visually audited (recorded in the "
                      "technical report; manuscript unaffected).")
            elif attempt > 1:
                _emit("success", f"Visual review clean after {attempt} render pass(es).")
            else:
                _emit("info", "Visual review: rendered pages read clean.")
            break

        directives = last_review.get("fix_directives") or []
        next_overrides = apply_fix_directives(overrides, directives)
        if next_overrides == overrides or attempt == max_iters:
            _emit("warning",
                  f"Visual review: {n_defects} layout defect(s) remain after {attempt} pass(es) "
                  f"(recorded in the technical report; manuscript unaffected).")
            break

        _emit("info",
              f"Visual review found {n_defects} layout defect(s); re-rendering with adjusted "
              f"format ({', '.join(directives)}).")
        overrides = next_overrides

    return VisualReviewOutcome(render=last_render, review=last_review,
                               overrides=overrides, attempts=attempts, history=history)


def format_diagnostics(outcome: VisualReviewOutcome) -> str | None:
    """Render-level review Diagnostics block for the TECHNICAL report.

    Returns None only when the pages were genuinely reviewed AND read clean. A DEGRADATION — the
    vision model never loaded, so only the geometric pre-check ran — IS reported (its 'clean'
    verdict is not a real visual pass). Per the silent-degradation design, this goes ONLY into the
    technical report's Diagnostics — the manuscript renders clean regardless."""
    residual = outcome.residual_defects
    if not residual and not outcome.vl_unavailable:
        return None
    lines = ["### Render-level review (vision model)", ""]
    if outcome.vl_unavailable:
        note = outcome.vl_unavailable_note
        lines += [
            "⚠️ **The vision model did NOT run this render** — only the deterministic geometric "
            "(bbox-overlap) pre-check ran. Layout faults that need actual vision (text printed on a "
            "figure, clipped table cells, a caption overlapping an image) were NOT audited, so "
            '"no visual defects" is UNVERIFIED for this render.'
            + (f" Reviewer reason: `{note}`." if note else "")
            + " (Fix: ensure the VL review container has PyTorch + the Qwen2.5-VL weights, and that "
            "`BIOAGENT_VLREVIEW_IMAGE` points at it.)",
            "",
        ]
    if residual:
        lines += [
            f"Automated visual review ran {outcome.attempts} render pass(es) and could not fully "
            "resolve the following layout issues (data content is unaffected):",
            "",
            "| Page | Type | Severity | Detail |",
            "| --- | --- | --- | --- |",
        ]
        for d in residual:
            page = d.get("page", "?")
            lines.append(
                f"| {page} | {d.get('type', '?')} | {d.get('severity', '?')} | {str(d.get('note', ''))[:80]} |"
            )
    return "\n".join(lines)
