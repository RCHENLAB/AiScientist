"""A shortened view of a tool result must keep the keys that say how the result may be reported.

Every tool result reaches the models in two shortened views: the Scientist is fed its JSON cut at
``_FEED_RESULT_CHARS`` (``ResearchHarness._feed_result``), and the Critic, and through the same
digest the report writer and the plan reviews, sees ``result_digest``. Both used to cut by
position, and tools put their caveats last. run_de's result in run c135ae589d96 had 38 keys, with
`skipped_groups` and `warnings` at 35-36. At the sizes of the time (30 keys, 4000 characters) the
Critic's digest stopped at key 30, and the Scientist was fed 221 of the 1,428 characters of those
warnings, so the direction-bias, extreme-fold-change and untested-groups warnings never reached it.

The rule (caveats are never the part that is cut) and the sizes (raised the same day) are tested
separately, and the tests read the sizes from the module, so re-tuning a size does not break them.
``RUN_DE`` below is that c135 result, key for key, with its gene lists abbreviated.
"""

from __future__ import annotations

import json
from typing import Any

from bioagent.agents.research_harness import (
    _DIGEST_MAX_ITEMS,
    _DIGEST_MAX_KEYS,
    _DIGEST_MAX_STR,
    _FEED_RESULT_CHARS,
    HarnessContext,
    HarnessResult,
    HarnessTool,
    ResearchHarness,
    default_catalog,
    result_digest,
)

_GROUPS = ("AC", "BC", "Cone", "MG", "Rod")
_SIGNIFICANT = {"AC": (374, 140), "BC": (1463, 284), "Cone": (329, 53), "MG": (5028, 508),
                "Rod": (3279, 107)}
_SKIPPED = (("Endothelial", 7, 27), ("HC", 5, 11), ("Microglia", 60, 24), ("Pericyte", 4, 2),
            ("RGC", 6, 8), ("RPE", 2, 2))

RUN_DE: dict[str, Any] = {
    "status": "ok",
    "step": "de",
    "groupby": "sampleid",
    "method": "wilcoxon",
    "comparison": "sampleid: DDX41 vs WT (reference=WT), stratified by majorclass",
    "reference": "WT",
    "stratify_by": "majorclass",
    "table_key": "majorclass",
    "n_groups": 5,
    "n_genes_per_group": 50,
    "de_rows_by_group": {"AC": 8371, "BC": 7309, "Cone": 6840, "MG": 9720, "Rod": 4649},
    "de_rows_total": 500,
    "top_genes_by_group": {g: [f"{g}gene{i}" for i in range(10)] for g in _GROUPS},
    "tested_universe_size": 11293,
    "min_pct": 0.1,
    "genes_dropped_by_min_pct": {"AC": 14016, "BC": 15078, "Cone": 15547, "MG": 12667, "Rod": 17738},
    "tie_correct": True,
    "universe_table": "tables/de_majorclass_universe.txt",
    "rank_tables": {g: f"rank_majorclass_{g}.rnk" for g in _GROUPS},
    "tables": ["tables/de_majorclass_all.csv"],
    "table_columns": ["group", "gene", "log2fc", "pval", "pval_adj", "score"],
    "figures": [f"figures/volcano_{g}.png" for g in _GROUPS],
    "checkpoint": "adata_de.h5ad",
    "raw_data_to_llm": False,
    "inference": "exploratory_ranking",
    "direction_bias": {"direction": "up", "strata": ["BC", "Cone", "MG", "Rod"]},
    "extreme_fc_genes": [f"{g}:Gene{i}" for g in ("Cone", "MG", "Rod") for i in range(6)],
    "significant_by_group": {g: {"up": u, "down": d, "shown_up": 50, "shown_down": 50,
                                 "truncated": True} for g, (u, d) in _SIGNIFICANT.items()},
    "padj_threshold": 0.05,
    "significance": {"padj_max": 0.05, "abs_log2fc_min": 0.25},
    "n_significant_total": 11565,
    "n_significant_total_note": (
        "a bookkeeping sum over strata that were each BH-corrected independently — quote the "
        "per-stratum counts; this total is not one FDR-controlled family"),
    "n_genes_cap_per_direction": 50,
    "table_truncation_note": (
        "significant_by_group gives the TRUE counts; the written tables and the gene lists passed "
        "downstream keep only the top 50 per direction per group. Report the true count and say the "
        "table is a top-N view of it."),
    "skipped_groups": [{"group": g, "n_condition": c, "n_reference": r,
                        "reason": "fewer than min_cells=30 in one or both arms"}
                       for g, c, r in _SKIPPED],
    "warnings": [
        "'sampleid' is a CONDITION column, so this is a per-cell test within each 'majorclass'. "
        "Stratifying removes the composition confound but NOT the pseudoreplication: report these as "
        "an exploratory ranking with effect sizes, not as differential expression with valid "
        "p-values. Replication available here: 'orig.ident' takes 1 distinct value(s) overall "
        "(DDX41: 1, WT: 1 within each arm). ",
        "DIRECTION BIAS: 4 of 5 strata show a strong same-direction skew (up >> down: BC 1463/284, "
        "Cone 329/53, MG 5028/508, Rod 3279/107). A shift that points the same way in every cell "
        "type is consistent with a technical difference between the arms (sequencing depth, "
        "capture, cell quality) rather than a biological programme; check the per-arm depth in the "
        "dataset profile before interpreting it, and if the arms differ in depth report these as "
        "depth-confounded.",
        "EXTREME FOLD-CHANGES: 23 ranked gene(s) have |log2FC| >= 5 (>=32x), e.g. Cadps2 in Cone "
        "(+5.7), Cartpt in Cone (+6.0), Gfap in MG (+6.0). At this scale the ratio almost always "
        "reflects near-zero detection in one arm (a detection artifact), not a real expression "
        "change — do not quote these fold-changes as findings without checking the per-arm "
        "detection fractions.",
        "6 group(s) were NOT tested for too few cells in one arm (Endothelial, HC, Microglia, "
        "Pericyte, RGC, RPE). The analysis does not cover them — the report must say so rather than "
        "implying full coverage.",
    ],
    "execution_mode": "hpc_slurm",
    "slurm_state": "COMPLETED",
}

_CAVEATS = ("n_significant_total_note", "table_truncation_note", "skipped_groups", "warnings")

# A failed run_code call shaped like the archived ones (status, returncode, stdout, stderr, error,
# provenance): 20,000 characters each of stdout and stderr, the sandbox's own per-stream cap, and
# the traceback AFTER them. Cut by position, the one line that says what went wrong is what goes.
RUN_CODE_FAILED: dict[str, Any] = {
    "status": "error",
    "returncode": 1,
    "stdout": "cluster\tn_cells\tmean_counts\n" + "".join(f"{i}\t{100 + i}\t{2.5 * i:.1f}\n"
                                                      for i in range(2000))[:19_970],
    "stderr": ("FutureWarning: the default of observed=False is deprecated\n" * 400)[:20_000],
    "error": ("Traceback (most recent call last):\n"
              '  File "snippet_7.py", line 41, in <module>\n'
              "    de = table.loc[:, 'sampleid']\n"
              "KeyError: 'sampleid'"),
    "provenance": {"status": "error", "snippet": "snippet_7.py", "image": "analysis.sif"},
    "execution_mode": "hpc_slurm",
    "slurm_state": "COMPLETED",
}


def test_the_fixtures_reproduce_the_old_losses() -> None:
    """Guard on the fixtures: under the pre-fix cuts they lose what production lost."""
    keys = list(RUN_DE)
    assert len(keys) == 38
    assert all(keys.index(k) >= 30 for k in _CAVEATS)            # past the old 30-key digest cap
    old_feed = json.dumps(RUN_DE)[:4000]                        # the old Scientist feed
    assert json.dumps(RUN_DE["skipped_groups"]) in old_feed
    assert json.dumps(RUN_DE["warnings"]) not in old_feed
    assert "DIRECTION BIAS" not in old_feed
    # Too big for even the new feed, with the traceback past the cut.
    assert len(json.dumps(RUN_CODE_FAILED)) > _FEED_RESULT_CHARS
    assert "KeyError" not in json.dumps(RUN_CODE_FAILED)[:_FEED_RESULT_CHARS]


# --- the Critic's view -------------------------------------------------------------------------


def test_the_c135_run_de_result_now_reaches_the_digest_whole() -> None:
    """38 keys, warnings up to 456 characters: within every digest size, so nothing is cut (the
    36-key result of run 8847d521ba32 is a subset of it)."""
    assert result_digest(RUN_DE) == RUN_DE


def test_past_the_key_cap_data_goes_and_the_caveats_stay() -> None:
    """The rule, independent of the sizes: a dict wider than ``_DIGEST_MAX_KEYS`` loses data from
    its end, keeps its caveats wherever they sit, keeps the tool's order, and names what it left out."""
    wide = {"status": "ok", **{f"g{i}": i for i in range(_DIGEST_MAX_KEYS + 3)},
            "skipped_groups": RUN_DE["skipped_groups"], "warnings": RUN_DE["warnings"]}

    digest = result_digest(wide)

    assert [k for k in digest if k.startswith("g")] == [f"g{i}" for i in range(_DIGEST_MAX_KEYS)]
    assert digest["skipped_groups"] == RUN_DE["skipped_groups"]
    assert digest["warnings"] == RUN_DE["warnings"]
    assert list(digest)[0] == "status" and list(digest)[-3:] == ["skipped_groups", "warnings", "…"]
    n = _DIGEST_MAX_KEYS
    assert digest["…"] == f"+3 more keys: g{n}, g{n + 1}, g{n + 2}"


def test_the_digest_is_still_bounded() -> None:
    big = {"status": "ok", "stdout": "x" * (5 * _DIGEST_MAX_STR), "genes": [f"G{i}" for i in range(500)]}

    digest = result_digest(big)

    assert len(digest["stdout"]) == _DIGEST_MAX_STR + 1           # the head, plus "…"
    assert len(digest["genes"]) == _DIGEST_MAX_ITEMS + 1          # the head, plus the count left out
    assert digest["genes"][-1] == f"… (+{500 - _DIGEST_MAX_ITEMS} more)"


def test_the_critic_is_shown_skipped_groups_and_warnings(tmp_path) -> None:
    """End to end: the payload ``ResearchLab._critic`` actually sends to the model."""
    from bioagent.agents.research_lab import LabConfig, ResearchLab

    seen: list = []

    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    lab = ResearchLab(
        HarnessContext(decisions={}, workspace=tmp_path), LabConfig(auto_select_skill=False),
        complete_fn=lambda m: (seen.append(m), json.dumps(
            {"verdict": "accept", "score": 0.9, "critique": "ok"}))[1],
        scientist=_Stub())

    lab._critic("q", "Per-cell-type effect ranking", HarnessResult(
        status="ok", stop_reason="finished", final_answer="ranked",
        steps=[{"tool": "run_de", "ok": True, "result": RUN_DE}], errors=[]), lambda _e: None)

    shown = json.loads(seen[0][1]["content"])["tool_results"][0]["result"]
    assert shown["skipped_groups"] == RUN_DE["skipped_groups"]
    assert shown["warnings"] == RUN_DE["warnings"]
    assert "table_truncation_note" in shown


# --- the Scientist's view ----------------------------------------------------------------------


def _tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"content": "", "tool_calls": [{"id": f"call_{name}", "type": "function",
                                           "function": {"name": name, "arguments": args}}]}


def test_the_c135_run_de_result_is_now_fed_whole() -> None:
    messages: list[dict] = []

    ResearchHarness._feed_result(messages, True, "call_1", RUN_DE)

    assert messages[0]["content"] == json.dumps(RUN_DE)


def test_a_result_past_the_feed_cap_is_fed_caveats_first_within_the_cap() -> None:
    """End to end through the loop: what the model reads on the turn after the failed call."""
    run_code = HarnessTool("run_code", "run python", {"type": "object", "properties": {}},
                           lambda _args, _ctx: RUN_CODE_FAILED)
    replies = iter([_tool_call("run_code", {}), _tool_call("finish", {"answer": "done"})])
    seen: list[list[dict]] = []

    def chat(messages: list[dict], _tools: list[dict]) -> dict:
        seen.append([dict(m) for m in messages])
        return next(replies)

    harness = ResearchHarness(
        catalog=[run_code, *[t for t in default_catalog() if t.name == "finish"]], chat_fn=chat)
    harness.run("summarise the clusters", HarnessContext(decisions={}))

    fed = next(m["content"] for m in seen[1] if m.get("role") == "tool")
    assert len(fed) == _FEED_RESULT_CHARS                       # the cap still holds
    assert fed.startswith('{"status": "error", "error": ')
    assert json.dumps(RUN_CODE_FAILED["error"]) in fed           # the whole traceback
    assert '"stdout": "cluster\\tn_cells' in fed                # then the data, in its order


def test_a_result_that_fits_is_fed_and_digested_exactly_as_before() -> None:
    small = {"status": "ok", "n_cells": 120, "tables": ["tables/qc.csv"], "note": "kept all",
             "warnings": ["NO mitochondrial genes matched the 'MT-' name prefix"]}
    messages: list[dict] = []

    ResearchHarness._feed_result(messages, True, "call_1", small)
    ResearchHarness._feed_result(messages, False, None, small)

    assert messages[0] == {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(small)}
    assert messages[1] == {"role": "user", "content": f"Tool result: {json.dumps(small)}"}
    assert json.dumps(result_digest(small)) == json.dumps(small)  # same keys, same order

    at_cap = {f"k{i}": i for i in range(_DIGEST_MAX_KEYS)}      # at the cap: no marker, no reorder
    assert json.dumps(result_digest(at_cap)) == json.dumps(at_cap)


def test_a_long_result_that_is_not_a_dict_is_cut_as_before() -> None:
    payload = ["x" * 100] * (_FEED_RESULT_CHARS // 50)
    messages: list[dict] = []

    ResearchHarness._feed_result(messages, True, "call_1", payload)

    assert messages[0]["content"] == json.dumps(payload)[:_FEED_RESULT_CHARS]


def test_compressed_history_keeps_the_small_digest() -> None:
    """Compression only runs when the window is full, so its stub stays at the small sizes whatever
    the Critic's digest sizes are, and it still keeps the reporting keys."""
    from bioagent.agents.research_harness import _COMPRESS_DIGEST_SIZES, _compress_message

    max_str = _COMPRESS_DIGEST_SIZES[0]
    reply = {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
        {"status": "ok", "stdout": "x" * (4 * _DIGEST_MAX_STR), "warnings": ["QC REMOVED NOTHING"]})}

    stub = json.loads(_compress_message(reply)["content"])

    assert max_str < _DIGEST_MAX_STR
    assert len(stub["stdout"]) == max_str + 1
    assert stub["warnings"] == ["QC REMOVED NOTHING"]
