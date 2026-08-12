"""The render-review loop must be HONEST when the vision model is unavailable.

When torch/Qwen2.5-VL can't load, deploy/vlreview/run_review.py falls back to a geometric-only
bbox pre-check and returns ``model="bbox-only"`` — with a ``clean: true`` verdict (the low-severity
"VL unavailable" note doesn't flip clean). That is NOT a real visual review, so it must be reported
as a DEGRADATION (technical-report Diagnostics + an honest feed line), never as "pages read clean".
"""

from __future__ import annotations

from bioagent.tools.visual_review import (
    VisualReviewOutcome,
    format_diagnostics,
    render_with_visual_review,
)

_BBOX_ONLY = {
    "clean": True,
    "model": "bbox-only",
    "defects": [{"source": "vl", "type": "blank_or_broken", "severity": "low",
                 "note": "VL review unavailable: ImportError: ...requires the PyTorch library..."}],
    "fix_directives": [],
}


def test_outcome_flags_vl_unavailable():
    out = VisualReviewOutcome(render={}, review=_BBOX_ONLY, overrides={}, attempts=1)
    assert out.vl_unavailable
    assert "ImportError" in out.vl_unavailable_note


def test_format_diagnostics_reports_degradation_even_when_clean():
    out = VisualReviewOutcome(render={}, review=_BBOX_ONLY, overrides={}, attempts=1)
    diag = format_diagnostics(out)
    assert diag is not None                              # NOT swallowed despite clean:true
    assert "vision model did NOT run" in diag
    assert "ImportError" in diag                          # the reviewer's reason is surfaced


def test_format_diagnostics_none_when_genuinely_clean():
    out = VisualReviewOutcome(render={}, review={"clean": True, "model": "Qwen2.5-VL-7B", "defects": []},
                              overrides={}, attempts=1)
    assert not out.vl_unavailable
    assert format_diagnostics(out) is None


def test_loop_emits_degraded_warning_not_read_clean():
    events: list[tuple[str, str]] = []
    outcome = render_with_visual_review(
        render_fn=lambda ov: {"pdf_path": "/tmp/x.pdf"},
        review_fn=lambda pdf: dict(_BBOX_ONLY),
        initial_render={"pdf_path": "/tmp/x.pdf"},
        emit=lambda level, who, msg: events.append((level, msg)),
    )
    assert outcome.vl_unavailable
    assert any(lvl == "warning" and "DEGRADED" in msg for lvl, msg in events)
    assert not any("read clean" in msg for _, msg in events)   # never falsely reported clean
