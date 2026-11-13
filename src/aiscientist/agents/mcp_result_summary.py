"""Bounded evidence views for native MCP responses; full artifacts remain authoritative."""
from __future__ import annotations


def project_result(server: str, result: dict, limit: int = 5500) -> dict:
    """Keep scientific outcomes and provenance without implying that a preview is complete.

    Self-contained so the collector can execute this exact function inside its image.
    Values are copied, never inferred from requested parameters or a disease/gene name.
    The limit counts UTF-8 JSON bytes, including the truncation annotation.
    """
    import json
    import re

    if limit < 1000:
        raise ValueError("MCP evidence summaries require at least 1000 bytes")
    omitted = []
    shortened = []
    out = {}

    def encoded(value):
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def text(value, length, path):
        if not isinstance(value, str) or len(value.encode("utf-8")) <= length:
            return value
        shortened.append(path)
        return value.encode("utf-8")[:length].decode("utf-8", errors="ignore") + "…"

    def fields(value, names, length=400, path=""):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        citation = value.get("citation")
        if isinstance(citation, str):
            # Preserve only identifiers explicitly present in the returned citation.
            # Long author lists otherwise push its trailing DOI outside the preview.
            if "doi" in names and not value.get("doi"):
                match = re.search(r"(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/[^\s,]+)",
                                  citation, re.IGNORECASE)
                if match:
                    value["doi"] = match.group(1).rstrip(".;")
            if "pmid" in names and not value.get("pmid"):
                match = re.search(r"(?:PMID\s*:\s*|https?://pubmed\.ncbi\.nlm\.nih\.gov/)([0-9]+)",
                                  citation, re.IGNORECASE)
                if match:
                    value["pmid"] = match.group(1)
        return {key: text(value[key], length, path + "." + key) for key in names if key in value}

    def rows(value, names, count, length=400, path=""):
        if not isinstance(value, list):
            return value
        if len(value) > count:
            shortened.append(path)
        return [fields(row, names, length, path + f"[{i}]") for i, row in enumerate(value[:count])]

    def add(key, value):
        # Reserve space for an explicit, bounded description of omissions.
        if encoded({**out, key: value}) <= limit - 700:
            out[key] = value
        else:
            omitted.append(key)

    core = ("status", "state", "tool", "mode", "analysis_id", "task_id", "job_id", "full_result",
            "returncode", "slurm_state", "error", "raw_data_to_llm", "checkpoint", "checkpoint_path",
            "exported_checkpoints", "response_omitted")
    for key in core:
        if key in result:
            add(key, text(result[key], 500 if key == "error" else 350, key))
    counts = ("n_candidates", "n_hpo_terms", "n_excluded_hpo", "n_observed", "n_excluded", "count",
              "n_contexts", "n_papers", "n_sentences", "n_passages", "n_judged", "n_requests",
              "n_hits_eligible", "n_genes", "over_budget", "model_unreachable")
    for key in counts:
        if key in result:
            add(key, result[key])

    candidate = ("rank", "disease_name", "disease_id", "gene", "posttest_prob", "composite_lr",
                 "final_score", "agreement", "evidence_status", "evidence_tier", "decision_note")
    citations = ("pmid", "doi", "source_id", "title", "citation", "year", "url")
    if server == "cellqc":
        # Execution facts come from the server result, never the tool schema's
        # defaults or the arguments of a later status-only collection call.
        for key in ("thresholds", "ambient", "n_libraries", "cells_before", "cells_after",
                    "pct_cells_removed", "genes_after", "n_hvg", "seed", "doublets",
                    "counts_layer", "raw_slot", "excluded_libraries", "checkpoints"):
            if key in result:
                add(key, result[key])
        if "warnings" in result:
            warnings = result["warnings"]
            if isinstance(warnings, list):
                if len(warnings) > 8:
                    shortened.append("warnings")
                warnings = [text(warning, 350, "warnings") for warning in warnings[:8]]
            add("warnings", warnings)
        if "libraries" in result:
            add("libraries", rows(result["libraries"], (
                "sample", "species", "cells_cellranger", "cells_after_filter", "cells_after_doublets",
                "frac_retained", "median_pct_mt", "median_pct_mt_kept", "removed_only_by",
                "removed_by_mito_total", "doublets_removed", "doublet_kappa",
                "ambient_soupx_contamination_mean", "ambient_decontx_contamination_mean"),
                8, 160, "libraries"))
    elif server == "phenotype-diagnosis":
        for key in ("hpo_terms", "excluded_hpo", "observed", "excluded", "unmapped", "assembly",
                    "phenotype_notes", "notes", "warnings"):
            if key in result:
                value = result[key]
                if isinstance(value, list) and len(value) > 8:
                    shortened.append(key)
                    value = value[:8]
                add(key, value)
        if "literature" in result:
            evidence = []
            for i, row in enumerate((result["literature"] or [])[:4]):
                if not isinstance(row, dict):
                    continue
                item = fields(row, ("status", "gene", "disease", "association", "clingen_tier",
                                    "evidence_status", "error"), 250, f"literature[{i}]")
                refs = row.get("evidence")
                if isinstance(refs, list):
                    item["evidence_count"] = len(refs)
                    item["evidence"] = rows(refs, ("source_id", "pmid", "doi", "study_type"), 3,
                                            180, f"literature[{i}].evidence")
                if row.get("notes"):
                    item["notes"] = [text(str(n), 300, f"literature[{i}].notes") for n in row["notes"][:2]]
                evidence.append(item)
            if len(result["literature"] or []) > 4:
                shortened.append("literature")
            add("literature", evidence)
        if "top" in result:
            add("top", fields(result["top"], candidate, 350, "top"))
        for key in ("differential", "candidates"):
            if key in result:
                add(key, rows(result[key], candidate, 3, 200, key))
    elif server == "literature":
        for key in ("api_usage", "pilot", "unread_check", "genes", "genes_flagged_for_review",
                    "notes", "warnings"):
            if key in result:
                value = result[key]
                if isinstance(value, list) and len(value) > 5:
                    shortened.append(key)
                    value = value[:5]
                add(key, value)
        if "answer" in result:
            add("answer", text(result["answer"], 2300, "answer"))
        elif "formatted_answer" in result:
            add("formatted_answer", text(result["formatted_answer"], 2300, "formatted_answer"))
        if "results" in result:
            add("results", rows(result["results"], citations, 5, 300, "results"))
        if "contexts" in result:
            contexts = result["contexts"] or []
            # Citations survive even when the accompanying long passage exceeds the preview.
            add("sources", rows(contexts, citations, 8, 220, "sources"))
            preview = rows(contexts, (*citations, "text", "summary", "quote", "score", "page"),
                           3, 450, "contexts")
            removed = False
            while preview and encoded({**out, "contexts": preview}) > limit - 700:
                preview.pop()
                removed = True
            if removed:
                shortened.append("contexts")
            if preview or not contexts:
                add("contexts", preview)
            else:
                omitted.append("contexts")
    retained = set(out)
    omitted.extend(key for key in result if key not in retained and key not in omitted)
    # Lists are bounded too: arbitrary response keys cannot defeat the response budget.
    out["response_summary"] = {
        "complete": False,
        "omitted_fields": [str(k)[:60] for k in omitted[:12]],
        "shortened_fields": [str(k)[:80] for k in shortened[:8]],
        "note": "Bounded evidence preview; omitted rows or text are not evidence of absence. Read full_result for the complete response.",
    }
    while encoded(out) > limit:
        # In rare long-key cases the metadata itself uses the reserve; trim only metadata.
        meta = out["response_summary"]
        if meta["omitted_fields"]:
            meta["omitted_fields"].pop()
        elif meta["shortened_fields"]:
            meta["shortened_fields"].pop()
        else:
            raise ValueError("MCP evidence identity exceeds the summary budget")
    return out
