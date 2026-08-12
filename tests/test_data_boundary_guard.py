"""Tests for the DataBoundaryGuard after the structural rewrite (option ②):

- Raw tabular data is judged by STRUCTURE (a numeric grid), not by counting commas — so a
  list of artifact paths / comma-heavy prose is NOT mistaken for a CSV dump, while a real
  numeric expression matrix (comma OR tab) still is.
- Raw-data detection is SOURCE-SCOPED: only the untrusted user span is sniffed; system-built
  scaffolding is trusted by construction.
- Secrets are an always-on hard block across the whole prompt.
"""

from __future__ import annotations

from typing import Any

from bioagent.agents.research_harness import HarnessContext, ResearchHarness
from bioagent.integrations.safety import DataBoundaryGuard, DataBoundaryPolicy

_BLOCK = DataBoundaryPolicy(allow_raw_data_to_llm=False)
_ALLOW = DataBoundaryPolicy(allow_raw_data_to_llm=True)


def _risk(prompt: str, policy: DataBoundaryPolicy = _BLOCK, **kw: Any) -> bool:
    return DataBoundaryGuard().inspect_prompt(prompt, None, policy, **kw).prompt_contains_raw_data_risk


# --- structure, not commas ---------------------------------------------------

def test_numeric_grid_is_detected_as_raw_data():
    rows = "\n".join("gene,1.1,2.2,3.3,4.4,5.5" for _ in range(4))
    assert _risk(f"Analyze:\n{rows}") is True


def test_tab_delimited_numeric_matrix_is_detected():
    rows = "\n".join("g\t1.1\t2.2\t3.3\t4.4\t5.5" for _ in range(3))
    assert _risk(f"Data:\n{rows}") is True


def test_artifact_path_list_is_not_raw_data():
    # The exact false positive the old comma-counter produced: a long comma-joined path list.
    paths = "evidence: figures/a.png, figures/b.png, tables/c.csv, tables/d.csv, data/e.json"
    assert _risk(paths) is False
    # And one path per line — also fine.
    per_line = "\n".join(f"  - figures/umap_{i}.png" for i in range(6))
    assert _risk(per_line) is False


def test_comma_heavy_prose_is_not_raw_data():
    prose = ("We ran QC, clustering, DE, and enrichment, then summarized markers, "
             "pathways, and limitations across the samples, groups, and conditions.")
    assert _risk(prose) is False


def test_inconsistent_or_too_few_columns_is_not_raw_data():
    assert _risk("a,b,c\n1,2,3\n4,5,6") is False           # only 3 columns (< MIN_COLS)
    assert _risk("1,2,3,4,5\n1,2,3\n1,2,3,4,5,6") is False  # column counts not consistent


# --- source scoping ----------------------------------------------------------

def test_raw_data_scope_limits_sniffing_to_untrusted_span():
    matrix = "\n".join("g,1.1,2.2,3.3,4.4,5.5" for _ in range(4))
    prompt = f"SYSTEM SCAFFOLDING with an example matrix:\n{matrix}\n\nUser question: hello"
    # Whole-prompt scan (default) sees the matrix and flags it.
    assert _risk(prompt) is True
    # Scoped to the untrusted user span only → the system-built matrix is trusted, not flagged.
    assert _risk(prompt, raw_data_scope="hello") is False
    # But a matrix the USER pasted (inside the scoped span) is still caught.
    assert _risk(prompt, raw_data_scope=matrix) is True


# --- secrets are always blocked ----------------------------------------------

def test_secret_is_flagged_regardless_of_scope():
    report = DataBoundaryGuard().inspect_prompt(
        "OPENAI_API_KEY=sk-abcdef0123456789 here", None, _ALLOW, raw_data_scope="clean user text")
    assert report.prompt_contains_secret_risk is True


# --- end to end through the harness ------------------------------------------

def test_harness_does_not_block_system_brief_with_path_lists():
    # A brief whose SYSTEM part lists many artifact paths, with a clean user question, must
    # not be blocked (no tunnel_port → strict endpoint) — the paths are trusted scaffolding.
    calls = {"n": 0}

    def finish_now(_messages: list[dict], _tools: list[dict]) -> dict:
        calls["n"] += 1
        return {"content": "", "tool_calls": [{"id": "f", "type": "function",
                "function": {"name": "finish", "arguments": {"answer": "ok"}}}]}

    harness = ResearchHarness(chat_fn=finish_now)
    paths = "\n".join(f"  - figures/plot_{i}.png, tables/t_{i}.csv" for i in range(5))
    brief = f"System scaffolding\nevidence:\n{paths}\n\nQuestion: characterize the data"
    result = harness.run(brief, HarnessContext(), untrusted_text="characterize the data")
    assert result.status != "blocked_by_guard"
    assert calls["n"] >= 1


def test_harness_still_blocks_user_pasted_matrix():
    def must_not_call(_m: list[dict], _t: list[dict]) -> dict:
        raise AssertionError("guard must block before the model")

    matrix = "\n".join("g,1.1,2.2,3.3,4.4,5.5" for _ in range(4))
    harness = ResearchHarness(chat_fn=must_not_call)
    # The user pasted the matrix into the question → it is in the untrusted span → blocked.
    result = harness.run(f"Analyze:\n{matrix}", HarnessContext(), untrusted_text=f"Analyze:\n{matrix}")
    assert result.status == "blocked_by_guard"


# --- which ENDPOINT the prompt goes to decides whether raw data may ride along ----------------
# The permissive branch exists only because a locally tunneled Qwen means the prompt never leaves
# the box. Locality therefore needs POSITIVE evidence; a tunnel alone is not it, because a session
# can hold an open vLLM tunnel while the lab's completions are routed to a paid remote API.

_MATRIX = "\n".join("g,1.1,2.2,3.3,4.4,5.5" for _ in range(4))


def _run_with(ctx: HarnessContext):
    seen = {"called": False}

    def chat(_m: list[dict], _t: list[dict]) -> dict:
        seen["called"] = True
        return {"content": "", "tool_calls": [{"id": "f", "type": "function",
                "function": {"name": "finish", "arguments": {"answer": "ok"}}}]}

    result = ResearchHarness(chat_fn=chat).run(
        f"Analyze:\n{_MATRIX}", ctx, untrusted_text=f"Analyze:\n{_MATRIX}")
    return result, seen["called"]


def test_raw_data_passes_to_the_local_tunneled_model():
    result, called = _run_with(HarnessContext(tunnel_port=11434))
    assert result.status != "blocked_by_guard" and called


def test_raw_data_is_blocked_when_a_remote_endpoint_is_bound_over_the_tunnel():
    """The regression this guard rewrite exists for: GPU still allocated (tunnel_port set) while
    the completions go to an external API. Inferring 'local' from the tunnel would have sent the
    matrix off-site."""
    result, called = _run_with(HarnessContext(tunnel_port=11434, llm_is_remote=True))
    assert result.status == "blocked_by_guard"
    assert not called, "the guard must block BEFORE the model is called"


def test_no_tunnel_and_no_flag_is_still_strict():
    result, called = _run_with(HarnessContext())
    assert result.status == "blocked_by_guard" and not called


def test_endpoint_is_off_host_classifies_conservatively():
    from bioagent.integrations.safety import endpoint_is_off_host as _endpoint_is_off_host
    # the session's own tunnel / an explicit loopback URL stays on the box
    assert _endpoint_is_off_host(None) is False
    assert _endpoint_is_off_host("http://127.0.0.1:11434/v1") is False
    assert _endpoint_is_off_host("http://localhost:8000/v1") is False
    # anything else leaves it — including a garbled URL, which must never read as local
    assert _endpoint_is_off_host("https://openrouter.ai/api/v1") is True
    assert _endpoint_is_off_host("https://api.moonshot.ai/v1") is True
    assert _endpoint_is_off_host("not a url") is True
