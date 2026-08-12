#!/usr/bin/env python
"""In-container render-level review: rasterize a rendered report and audit each page for
LAYOUT defects a text model cannot see (text overlap, clipping, table overflow, a caption
printed over its figure).

Two independent detectors, cheapest first, so we never rely on the VL model alone:

  1. Deterministic bbox-overlap pre-check (NO GPU, always runs). PyMuPDF gives every word's
     bounding box on the rendered page; two boxes that significantly intersect ARE stacked
     text. This catches the big, unambiguous class of "文字压文字 / table crushed" for free
     and is fully reproducible.

  2. Qwen2.5-VL page review (the GPU model). Catches what bbox math can't: text sitting on
     top of IMAGE content, axis/legend labels overlapping data, clipped-at-the-margin text,
     a figure that overran the page. Strict-JSON, per-page, against a fixed checklist.

Output contract (read by src/bioagent/gateway/vlreview_job.py):
    <out>/review.json = {
      "clean": bool,                       # no medium/high defect anywhere
      "pages_reviewed": int,
      "defects": [ {page, source, type, severity, note, fix} ... ],
      "fix_directives": [str ...],         # de-duped union of every defect's `fix`
      "model": str, "dpi": int
    }

The `fix` on each defect is drawn from a FIXED vocabulary (FIX_VOCAB below) that the render
loop (bioagent.tools.visual_review) knows how to translate into concrete pandoc/LaTeX format
overrides — that is what lets the render step "return and adjust the format" and re-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# --- Defect -> fix contract -------------------------------------------------------------
# The ONLY fix directives a defect may carry. Keep this list in lock-step with
# bioagent.tools.visual_review.apply_fix_directives (which maps each to a render override).
FIX_VOCAB = {
    "shrink_table_font",     # tables overprint columns -> footnotesize -> scriptsize -> tiny
    "force_table_wrap",      # wide table not wrapping -> lower the wrap threshold
    "landscape_wide_tables", # still too wide after wrap -> rotate wide tables to landscape
    "shrink_body_font",      # body text overlaps / overflows -> 11pt -> 10pt
    "widen_margins",         # text clipped at the page edge -> 1in -> 0.75in margins
    "scale_figures",         # figure/caption collision or oversized figure -> cap fig width
}

# The checklist the VL model answers, one page at a time. Each item maps to a defect `type`
# and a default `fix`. Phrased as concrete, observable render faults — NOT "is the data right"
# (that is the text LLM's job on the structured numbers, a separate step).
CHECK_PROMPT = """You are a strict typesetting proof-reader inspecting ONE rendered page of a
scientific report (an image of the final PDF page). Report ONLY physical LAYOUT defects that
are visible on the page. Do NOT comment on whether the science, numbers, or wording are correct.

Check for these defects and report every one you actually see:
- text_overlap: two pieces of text printed on top of each other / illegibly overlapping
- text_on_figure: caption, label, or body text printed over image/figure content or data points
- text_clipped: text cut off or running past the page margin / off the page edge
- table_overflow: table columns overprinting each other or spilling past the table/page box
- figure_oversized: a figure so large it is cut off or collides with text/the next element
- blank_or_broken: an expected figure/table area is blank, garbled, or obviously broken

Respond with STRICT JSON only, no prose, in this exact shape:
{"defects": [{"type": "<one of the above>", "severity": "high|medium|low", "note": "<=15 words"}]}
If the page is clean, respond exactly: {"defects": []}
"""

# Map each VL defect type to the render fix directive that addresses it.
_TYPE_TO_FIX = {
    "text_overlap": "shrink_body_font",
    "text_on_figure": "scale_figures",
    "text_clipped": "widen_margins",
    "table_overflow": "shrink_table_font",
    "figure_oversized": "scale_figures",
    "blank_or_broken": None,   # not a formatting knob — flag for humans, no auto-fix
}


# ---------------------------------------------------------------------------------------
# Detector 1: deterministic bbox overlap (no GPU)
# ---------------------------------------------------------------------------------------
def _bbox_overlap_defects(page, page_no: int) -> list[dict]:
    """Flag pages where word bounding boxes significantly intersect (stacked text). Adjacent
    same-line words naturally touch, so we require a real 2-D overlap: intersection area over
    the smaller box exceeds a threshold AND the boxes overlap on BOTH axes by more than a hair."""
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
    boxes = [(w[0], w[1], w[2], w[3]) for w in words if w[4].strip()]
    flagged = 0
    n = len(boxes)
    for i in range(n):
        ax0, ay0, ax1, ay1 = boxes[i]
        a_area = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
        for j in range(i + 1, n):
            bx0, by0, bx1, by1 = boxes[j]
            ix = min(ax1, bx1) - max(ax0, bx0)
            iy = min(ay1, by1) - max(ay0, by0)
            if ix <= 0.5 or iy <= 0.5:        # no meaningful 2-D intersection
                continue
            b_area = max(1e-6, (bx1 - bx0) * (by1 - by0))
            inter = ix * iy
            # Stacked text: intersection covers >40% of the smaller word's box on both axes.
            if inter / min(a_area, b_area) > 0.40 and iy / min(ay1 - ay0, by1 - by0) > 0.5:
                flagged += 1
                break
    if flagged:
        return [{
            "page": page_no, "source": "bbox", "type": "text_overlap",
            "severity": "high" if flagged > 3 else "medium",
            "note": f"{flagged} word-box overlap(s) detected geometrically",
            "fix": "shrink_body_font",
        }]
    return []


# ---------------------------------------------------------------------------------------
# Detector 2: Qwen2.5-VL page review (GPU)
# ---------------------------------------------------------------------------------------
def _load_vl(model_dir: str):
    """Load Qwen2.5-VL from the bind-mounted weights dir (offline). bf16, device_map=auto."""
    import torch
    from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        model_dir, torch_dtype=torch.bfloat16, device_map="auto", local_files_only=True,
    )
    # High pixel budget: overlap is a FINE-DETAIL fault; downscaling a page to 448px hides it.
    processor = AutoProcessor.from_pretrained(
        model_dir, local_files_only=True, min_pixels=512 * 28 * 28, max_pixels=2048 * 28 * 28,
    )
    return model, processor


def _vl_review_page(model, processor, png_path: Path, page_no: int) -> list[dict]:
    """Run the checklist prompt on one page PNG; parse strict JSON into defect rows."""
    from qwen_vl_utils import process_vision_info

    messages = [{"role": "user", "content": [
        {"type": "image", "image": f"file://{png_path}"},
        {"type": "text", "text": CHECK_PROMPT},
    ]}]
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs = process_vision_info(messages)
    inputs = processor(text=[text], images=image_inputs, videos=video_inputs,
                       padding=True, return_tensors="pt").to(model.device)
    generated = model.generate(**inputs, max_new_tokens=512, do_sample=False)
    trimmed = [out[len(inp):] for inp, out in zip(inputs.input_ids, generated)]
    raw = processor.batch_decode(trimmed, skip_special_tokens=True,
                                 clean_up_tokenization_spaces=False)[0]
    return _parse_defects(raw, page_no)


def _parse_defects(raw: str, page_no: int) -> list[dict]:
    """Extract the JSON object from the model's reply and normalize to defect rows. Robust to
    the model wrapping JSON in prose/```json fences — we slice the outermost braces."""
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e == -1:
        return []
    try:
        payload = json.loads(raw[s:e + 1])
    except json.JSONDecodeError:
        return []
    rows: list[dict] = []
    for d in payload.get("defects", []):
        dtype = str(d.get("type", "")).strip()
        if dtype not in _TYPE_TO_FIX:
            continue
        sev = str(d.get("severity", "medium")).strip().lower()
        sev = sev if sev in ("high", "medium", "low") else "medium"
        rows.append({
            "page": page_no, "source": "vl", "type": dtype, "severity": sev,
            "note": str(d.get("note", ""))[:120], "fix": _TYPE_TO_FIX[dtype],
        })
    return rows


# ---------------------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Render-level VL review of a rendered report.")
    ap.add_argument("--pdf", required=True, help="Rendered report PDF to audit.")
    ap.add_argument("--model", required=True, help="Qwen2.5-VL weights dir (bind-mounted ro).")
    ap.add_argument("--out", required=True, help="Writable output dir for review.json + page PNGs.")
    ap.add_argument("--dpi", type=int, default=200, help="Rasterization DPI (200 catches overlap).")
    ap.add_argument("--max-pages", type=int, default=40, help="Cap pages sent to the VL model.")
    ap.add_argument("--no-vl", action="store_true", help="bbox pre-check only (debug / no GPU).")
    args = ap.parse_args()

    import fitz  # PyMuPDF

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pages_dir = out / "pages"
    pages_dir.mkdir(exist_ok=True)

    doc = fitz.open(args.pdf)
    n_pages = min(doc.page_count, args.max_pages)
    truncated = doc.page_count > args.max_pages

    defects: list[dict] = []
    # Detector 1 always runs; rasterize once and reuse the PNGs for the VL pass.
    zoom = args.dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    png_paths: list[tuple[int, Path]] = []
    for i in range(n_pages):
        page = doc.load_page(i)
        defects.extend(_bbox_overlap_defects(page, i + 1))
        png = pages_dir / f"page_{i + 1:03d}.png"
        page.get_pixmap(matrix=matrix).save(png)
        png_paths.append((i + 1, png))

    model_name = "bbox-only"
    if not args.no_vl:
        try:
            model, processor = _load_vl(args.model)
            model_name = getattr(model.config, "_name_or_path", args.model)
            for page_no, png in png_paths:
                defects.extend(_vl_review_page(model, processor, png, page_no))
        except Exception as exc:  # noqa: BLE001 - VL failure must not lose the bbox findings
            defects.append({
                "page": 0, "source": "vl", "type": "blank_or_broken", "severity": "low",
                "note": f"VL review unavailable: {type(exc).__name__}: {exc}"[:120], "fix": None,
            })

    fix_directives = sorted({d["fix"] for d in defects if d.get("fix") in FIX_VOCAB})
    has_actionable = any(d["severity"] in ("high", "medium") for d in defects)
    review = {
        "clean": not has_actionable,
        "pages_reviewed": n_pages,
        "pages_truncated": truncated,
        "defects": defects,
        "fix_directives": fix_directives,
        "model": model_name,
        "dpi": args.dpi,
    }
    (out / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    print(f"review.json written: {len(defects)} defect(s), directives={fix_directives}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
