"""Tests for the /api/lab LLM binding — vLLM tunnel by default, OpenRouter override.

Imports the FastAPI app (needs the gateway extra installed). Skipped cleanly if
fastapi is absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.gateway import vllm_client  # noqa: E402


class _FakeConn:
    selected_model = "QuantTrio/Qwen3.6-35B-A3B-AWQ"
    tunnel_port = 37219


def test_lab_llm_defaults_to_session_vllm_tunnel(monkeypatch):
    for v in ("BIOAGENT_LLM_BASE_URL", "BIOAGENT_LLM_API_KEY", "BIOAGENT_LLM_MODEL"):
        monkeypatch.delenv(v, raising=False)
    cap: dict = {}
    monkeypatch.setattr(vllm_client, "complete",
                        lambda port, model, messages, **kw: cap.update(port=port, model=model, base=kw.get("base_url")) or "x")

    _r = gw_app._lab_llm(_FakeConn())
    complete_fn, model, label = _r.complete_fn, _r.model, _r.label
    complete_fn([{"role": "user", "content": "q"}])

    assert label == "vLLM"
    assert model == "QuantTrio/Qwen3.6-35B-A3B-AWQ"
    assert cap["port"] == 37219 and cap["base"] is None   # the session tunnel, no override


def test_lab_llm_uses_openrouter_when_env_set(monkeypatch):
    monkeypatch.setenv("BIOAGENT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("BIOAGENT_LLM_API_KEY", "sk-or-test")
    monkeypatch.setenv("BIOAGENT_LLM_MODEL", "qwen/qwen3.6-35b-a3b")
    cap: dict = {}
    monkeypatch.setattr(vllm_client, "chat_tools",
                        lambda port, model, messages, tools, **kw:
                        cap.update(port=port, model=model, base=kw.get("base_url"), key=kw.get("api_key")) or {"content": "", "tool_calls": []})

    _r = gw_app._lab_llm(_FakeConn())
    scientist_chat, model, label = _r.scientist_chat, _r.model, _r.label
    scientist_chat([{"role": "user", "content": "q"}], [])

    assert label == "OpenRouter"
    assert model == "qwen/qwen3.6-35b-a3b"          # the OpenRouter model id, not the vLLM served name
    assert cap["port"] == 0 and cap["base"] == "https://openrouter.ai/api/v1" and cap["key"] == "sk-or-test"


# --- fast-chat context management binding -----------------------------------
# agents/quick_chat.py must not import the gateway (it is unit-tested on a bare checkout),
# so the exact token counter and the summarizer are INJECTED here. These pin that wiring:
# a silent mis-bind would degrade chat to char estimates + drop-oldest with nothing failing.


def test_quickchat_context_fns_bind_to_the_session_tunnel(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(vllm_client, "count_tokens",
                        lambda port, model, messages, tools, **kw:
                        cap.update(port=port, model=model, tools=tools, base=kw.get("base_url")) or 4242)

    count_tokens, _summarize = gw_app._quickchat_context_fns(_FakeConn(), "M", None, None)
    got = count_tokens([{"role": "user", "content": "q"}], [{"type": "function"}])

    assert got == 4242
    assert cap["port"] == 37219 and cap["base"] is None
    assert cap["tools"] == [{"type": "function"}]     # schemas counted too, not just messages


def test_quickchat_summarizer_runs_with_thinking_off_and_a_small_budget(monkeypatch):
    """think=False is not an optimisation. With thinking ON a Qwen3 reasoning trace can eat the
    whole max_tokens budget and return EMPTY content — exactly how map_phenotype_to_hpo was
    silently getting zero terms. An empty summary here would throw away the chat's memory."""
    cap: dict = {}
    monkeypatch.setattr(vllm_client, "complete",
                        lambda port, model, messages, **kw:
                        cap.update(port=port, think=kw.get("think"), max_tokens=kw.get("max_tokens"),
                                   timeout=kw.get("timeout")) or "a briefing")

    _count, summarize = gw_app._quickchat_context_fns(_FakeConn(), "M", None, None)
    assert summarize([{"role": "user", "content": "fold this"}], 512) == "a briefing"

    assert cap["think"] is False
    assert cap["max_tokens"] == 512                   # the caller's budget, not the client default
    assert cap["port"] == 37219
    # Short timeout: this runs BEFORE the user's first token, so a hung summarizer must fail
    # fast and let compaction degrade rather than stall the fast path.
    assert cap["timeout"] <= 120.0


def test_quickchat_context_fns_honour_the_openrouter_override(monkeypatch):
    """With a remote base_url there is no /tokenize; vllm_client.count_tokens answers None and
    the compactor falls back to its char estimate. The binding must pass base_url through so
    that happens instead of a wrong count against the wrong tokenizer."""
    cap: dict = {}
    monkeypatch.setattr(vllm_client, "count_tokens",
                        lambda port, model, messages, tools, **kw:
                        cap.update(port=port, base=kw.get("base_url"), key=kw.get("api_key")) or None)

    count_tokens, _s = gw_app._quickchat_context_fns(
        _FakeConn(), "m", "https://openrouter.ai/api/v1", "sk-or-test")
    assert count_tokens([], []) is None
    assert cap["port"] == 0 and cap["base"] == "https://openrouter.ai/api/v1" and cap["key"] == "sk-or-test"


def test_context_events_render_through_the_shared_activity_renderer():
    """The chat path deliberately reuses the research path's event vocabulary so there is only
    ONE renderer to keep in sync — including the honesty fix: an ESTIMATE must not be reported
    as a measurement by the served model's tokenizer."""
    exact, = gw_app._lab_event_to_chat(
        {"type": "context_measured", "exact_tokens": 18000, "allowed": 24000, "exact": True})
    assert "server tokenizer" in exact["token"] and "18000 / 24000" in exact["token"]

    estimated, = gw_app._lab_event_to_chat(
        {"type": "context_measured", "exact_tokens": 18000, "allowed": 24000, "exact": False})
    assert "estimate" in estimated["token"] and "server tokenizer" not in estimated["token"]

    # The research path omits `exact` entirely and must keep its original wording.
    legacy, = gw_app._lab_event_to_chat(
        {"type": "context_measured", "exact_tokens": 5, "allowed": 9})
    assert "server tokenizer" in legacy["token"]

    trimmed, = gw_app._lab_event_to_chat(
        {"type": "context_trimmed", "compressed_turns": 3, "dropped_turns": 1})
    assert "compressed 3" in trimmed["token"] and "dropped 1" in trimmed["token"]


# --- dataset upload ---------------------------------------------------------

def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


def test_upload_saves_file_under_connection_workspace(tmp_path):
    import asyncio
    from pathlib import Path

    from bioagent.gateway.settings import HPCSettings

    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "tester"
    gw_app.CONNECTIONS[conn.id] = conn
    try:
        r = _client().post(
            "/api/upload",
            data={"connection_id": conn.id},
            files={"file": ("../../etc/pbmc3k.h5ad", b"binary-data", "application/octet-stream")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # filename sanitized — path-traversal stripped to the basename, extension kept
        assert body["name"] == "pbmc3k.h5ad"
        saved = Path(body["path"])
        assert saved.read_bytes() == b"binary-data"
        assert saved.parent.name == "uploads"        # under the per-user workspace
        assert saved.parent.parent == conn.workspace
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
        loop.close()


def test_upload_uniquifies_on_name_collision(tmp_path):
    """A second upload of the SAME filename must NOT overwrite the first — it lands as
    ``query (1).h5ad`` with distinct content."""
    import asyncio
    from pathlib import Path

    from bioagent.gateway.settings import HPCSettings

    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "tester"
    gw_app.CONNECTIONS[conn.id] = conn
    try:
        c = _client()
        r1 = c.post("/api/upload", data={"connection_id": conn.id},
                    files={"file": ("query.h5ad", b"first", "application/octet-stream")})
        r2 = c.post("/api/upload", data={"connection_id": conn.id},
                    files={"file": ("query.h5ad", b"second", "application/octet-stream")})
        assert r1.json()["name"] == "query.h5ad"
        assert r2.json()["name"] == "query (1).h5ad"          # uniquified, not overwritten
        assert Path(r1.json()["path"]).read_bytes() == b"first"
        assert Path(r2.json()["path"]).read_bytes() == b"second"
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
        loop.close()


_VCF_UPLOAD = (
    b"##fileformat=VCFv4.2\n"
    b"##source=DeepVariant\n"
    b"##contig=<ID=1,length=249250621,assembly=GRCh37>\n"
    b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE1\n"
    b"1\t100\t.\tA\tT\t50\tPASS\t.\tGT\t0/1\n"
)


def _mock_conn(tmp_path):
    import asyncio

    from bioagent.gateway.settings import HPCSettings

    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "tester"
    gw_app.CONNECTIONS[conn.id] = conn
    return conn, loop


def test_upload_response_carries_a_content_peek(tmp_path):
    """Feature ①: an upload is SKIMMED deterministically at ingest — the response peek must identify
    a VCF by CONTENT (assembly/sample/caller), not by the filename."""
    conn, loop = _mock_conn(tmp_path)
    try:
        r = _client().post("/api/upload", data={"connection_id": conn.id},
                           files={"file": ("callset.vcf", _VCF_UPLOAD, "application/octet-stream")})
        assert r.status_code == 200, r.text
        peek = r.json()["peek"]
        assert peek["detected_format"] == "vcf"
        assert peek["vcf"]["assembly"] == "GRCh37"
        assert peek["vcf"]["sample_ids"] == ["SAMPLE1"]
        assert peek["vcf"]["caller"] == "DeepVariant"
        assert peek["gist"]                                   # a one-line deterministic summary
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
        loop.close()


def test_describe_endpoint_deterministic_without_a_served_model(tmp_path):
    """The LLM description must run ONLY when a model is up. With no GPU (tunnel_port unset) the
    endpoint returns the deterministic peek + a note, and NEVER provisions anything."""
    conn, loop = _mock_conn(tmp_path)
    assert conn.tunnel_port is None                           # a fresh session has no served model
    try:
        c = _client()
        up = c.post("/api/upload", data={"connection_id": conn.id},
                    files={"file": ("callset.vcf", _VCF_UPLOAD, "application/octet-stream")})
        path = up.json()["path"]
        r = c.post("/api/dataset/describe", json={"connection_id": conn.id, "path": path})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["model_available"] is False
        assert body["description"]["source"] == "deterministic"
        assert body["description"]["likely_modality"] == "variants"
        assert body["description"]["assembly"] == "GRCh37"
        assert "note" in body["description"]
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
        loop.close()


def test_describe_endpoint_requires_a_path(tmp_path):
    conn, loop = _mock_conn(tmp_path)
    try:
        r = _client().post("/api/dataset/describe", json={"connection_id": conn.id})
        assert r.status_code == 400
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
        loop.close()


def test_reserve_folder_avoids_collision(tmp_path):
    """Reserving the same folder name twice yields distinct top-level dirs (data, data (1))."""
    import asyncio

    from bioagent.gateway.settings import HPCSettings

    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.workspace = tmp_path / "tester"
    gw_app.CONNECTIONS[conn.id] = conn
    try:
        c = _client()
        a = c.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "data"})
        b = c.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "data"})
        assert a.json()["folder"] == "data"
        assert b.json()["folder"] == "data (1)"
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
        loop.close()


def test_lab_plan_review_sets_decision_event(tmp_path):
    import asyncio

    from bioagent.gateway.settings import HPCSettings

    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.pending_plan = {"kind": "agenda", "payload": ["a", "b"]}
    gw_app.CONNECTIONS[conn.id] = conn
    try:
        # revise: the user's natural-language feedback is captured for the PI to re-plan
        r = _client().post("/api/lab/plan",
                           json={"connection_id": conn.id, "action": "revise", "feedback": "drop step b"})
        assert r.status_code == 200 and r.json()["action"] == "revise"
        assert conn.plan_event.is_set()
        assert conn.plan_value == {"action": "revise", "feedback": "drop step b"}
        assert conn.pending_plan is None

        # approve path
        conn.plan_event.clear()
        r2 = _client().post("/api/lab/plan", json={"connection_id": conn.id, "action": "approve"})
        assert r2.json()["action"] == "approve" and conn.plan_value["action"] == "approve"

        # back-compat: {approved: false} maps to cancel
        conn.plan_event.clear()
        r3 = _client().post("/api/lab/plan", json={"connection_id": conn.id, "approved": False})
        assert r3.json()["action"] == "cancel" and conn.plan_value["action"] == "cancel"
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
        loop.close()


def test_lab_plan_review_unknown_connection_is_404():
    r = _client().post("/api/lab/plan", json={"connection_id": "nope", "action": "approve"})
    assert r.status_code == 404


def test_upload_unknown_connection_is_404():
    r = _client().post(
        "/api/upload",
        data={"connection_id": "does-not-exist"},
        files={"file": ("x.h5ad", b"x", "application/octet-stream")},
    )
    assert r.status_code == 404


# --- artifacts files browser (list + nested preview serve) ------------------

def test_files_list_includes_nested_and_serves_them(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    art = tmp_path / "tester" / "run0001" / "artifacts"
    (art / "tables" / "DEG").mkdir(parents=True)
    (art / "tables" / "DEG" / "DEG_Cone.csv").write_text("gene,lfc\nCD3D,1.2\n", encoding="utf-8")
    (art / "umap.png").write_bytes(b"\x89PNG-fake")

    client = _client()
    listing = client.get("/api/files/tester/run0001")
    assert listing.status_code == 200
    files = {f["name"]: f for f in listing.json()["files"]}
    assert "DEG_Cone.csv" in files and "umap.png" in files
    assert files["umap.png"]["kind"] == "image"
    assert files["DEG_Cone.csv"]["path"] == "tables/DEG/DEG_Cone.csv"   # nested path preserved
    assert listing.json()["bundle_url"] == "/api/bundle/tester/run0001"

    # the nested file serves inline for preview
    served = client.get(files["DEG_Cone.csv"]["url"])
    assert served.status_code == 200 and served.content == b"gene,lfc\nCD3D,1.2\n"


def test_files_unknown_run_is_empty_and_bad_path_404(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    assert _client().get("/api/files/tester/nope").json()["files"] == []
    assert _client().get("/api/file/tester/nope/whatever.csv").status_code == 404


def test_assemble_report_md_embeds_figures_and_tables(tmp_path):
    art = tmp_path / "artifacts"
    (art / "figures").mkdir(parents=True)
    (art / "tables").mkdir(parents=True)
    (art / "figures" / "umap_clusters.png").write_bytes(b"\x89PNG")
    (art / "tables" / "de_leiden_all.csv").write_text("group,gene\n0,CD3D\n", encoding="utf-8")

    md = gw_app._assemble_report_md("## Findings\n\nThe data show X.", art)
    assert "The data show X." in md
    # figures embedded with relative paths (pandoc cwd=art) so the PDF includes them
    assert "![Umap clusters](figures/umap_clusters.png){width=85%}" in md
    assert "# Figures" in md
    # tables indexed
    assert "`tables/de_leiden_all.csv`" in md


def test_assemble_report_md_handles_no_artifacts(tmp_path):
    md = gw_app._assemble_report_md("just prose", tmp_path / "artifacts")
    assert "just prose" in md and "# Figures" not in md


def test_csv_preview_md_renders_markdown_table(tmp_path):
    csv = tmp_path / "t.csv"
    csv.write_text("gene,log2fc,padj\nCD3D,2.1,0.001\nMS4A1,1.4,0.02\n", encoding="utf-8")
    md = gw_app._csv_preview_md(csv)
    assert "| gene | log2fc | padj |" in md and "| --- | --- | --- |" in md
    assert "| CD3D | 2.1 | 0.001 |" in md
    # header-only / unreadable CSVs preview to empty (not a crash)
    (tmp_path / "h.csv").write_text("only_header\n", encoding="utf-8")
    assert gw_app._csv_preview_md(tmp_path / "h.csv") == ""


def test_report_writer_prompt_is_manuscript_structured():
    sysp = gw_app._report_writer_system()
    assert "MANUSCRIPT" in sysp
    for section in ("Abstract", "Introduction", "Results", "Discussion", "Methods"):
        assert section in sysp, f"manuscript section missing from writer prompt: {section}"
    # citations live in a References section reserved for the literature module (PaperQA);
    # real DOI/PMID only, never fabricated
    assert "fabricate" in sysp and "PaperQA" in sysp
    assert "DOI" in sysp or "PMID" in sysp
    # the manuscript now also mandates a Conclusion and a (reserved) References section
    assert "Conclusion" in sysp and "References" in sysp
    # anti-fabrication rules: no framework/deferral padding, no question-rewriting, honour RUN STATUS
    assert "framework" in sysp and "deferred" in sysp
    assert "RUN STATUS" in sysp and "STAY ON THE ACTUAL QUESTION" in sysp
    # the variant-kind prompt carries the same honesty rules
    assert "framework" in gw_app._report_writer_system("variant")
    # reviewer enforces the manuscript shape + keeps real, drops fabricated citations
    review = gw_app._report_review_system()
    assert "Title, Abstract" in review
    assert "DOI" in review


def test_manuscript_run_status_block_empty_when_converged():
    from types import SimpleNamespace
    conv = SimpleNamespace(converged=True, accepted_steps=4, agenda=["a", "b", "c", "d"], rounds=[])
    assert gw_app._manuscript_run_status_block(conv) == ""   # clean run → manuscript unchanged
    assert gw_app._manuscript_run_status_block(None) == ""


def test_manuscript_run_status_block_is_honest_when_not_converged():
    from types import SimpleNamespace
    rounds = [{
        "step_index": 1,
        "step": "Annotate the uploaded VCF using the curated variant annotation tool, specifying GRCh38",
        "scientist_result": {"status": "incomplete", "stop_reason": "max_steps", "steps": []},
        "verdict": {"verdict": "revise", "score": 0.0},
    }]
    result = SimpleNamespace(converged=False, accepted_steps=1,
                             agenda=["s1", "s2", "s3", "s4"], rounds=rounds)
    block = gw_app._manuscript_run_status_block(result)
    assert "did NOT fully complete" in block and "1/4" in block
    assert "Annotate the uploaded VCF" in block                 # the failed step topic, no tool spew
    assert "pysam" not in block and "Slurm" not in block        # detail stays in the technical report
    assert "framework" in block and "fabricate" in block        # forbids the fabrication we saw


def test_variant_facts_block_falls_back_to_filter_summary(tmp_path):
    import json as _json
    from types import SimpleNamespace
    art = tmp_path / "artifacts"
    (art / "tables").mkdir(parents=True)
    (art / "tables" / "variant_filter_summary.json").write_text(
        _json.dumps({"total_variants": 4934923, "n_pass": 4721988, "n_nonpass": 212935}), encoding="utf-8")
    # no successful annotate_variants result → fall back to the PASS-filter summary
    facts = gw_app._variant_facts_block(art, SimpleNamespace(rounds=[]))
    assert "AUTHORITATIVE COUNTS" in facts
    assert "4934923" in facts and "4721988" in facts and "212935" in facts
    assert "did not complete" in facts.lower()                  # honest: annotation produced nothing


def test_build_report_injects_run_status_for_incomplete_run(tmp_path):
    from types import SimpleNamespace
    art = tmp_path / "artifacts"
    (art / "figures").mkdir(parents=True)
    (art / "tables").mkdir(parents=True)
    result = SimpleNamespace(
        converged=False, accepted_steps=1, agenda=["a", "b", "c", "d"],
        rounds=[{"step_index": 1, "step": "Annotate the VCF",
                 "scientist_result": {"status": "incomplete", "stop_reason": "max_steps", "steps": []},
                 "verdict": {"verdict": "revise", "score": 0.0}}])
    seen = {}

    def fake_complete(messages):
        seen["user"] = messages[-1]["content"]
        return "# Report\n\nBody."

    gw_app._build_report("", art, fake_complete, "Complete the research", result)
    assert "RUN STATUS" in seen["user"] and "did NOT fully complete" in seen["user"]


def test_build_report_uses_model_and_appends_index(tmp_path):
    art = tmp_path / "artifacts"
    (art / "figures").mkdir(parents=True)
    (art / "tables").mkdir(parents=True)
    (art / "figures" / "umap.png").write_bytes(b"\x89PNG")
    (art / "tables" / "de_leiden_0.csv").write_text("gene,padj\nCD3D,0.001\n", encoding="utf-8")

    seen = {}

    def fake_complete(messages):
        seen["user"] = messages[-1]["content"]
        return "# My Report\n\n![umap](figures/umap.png)\n\nFindings here."

    md = gw_app._build_report("grounded synthesis", art, fake_complete, "What changed?")
    # the model got the figure inventory + a real CSV preview to embed
    assert "figures/umap.png" in seen["user"] and "| gene | padj |" in seen["user"]
    assert "What changed?" in seen["user"]
    # body preserved + an authoritative index appended listing every output file
    assert "# My Report" in md
    assert "# Output Files Index" in md
    assert "`figures/umap.png`" in md and "`tables/de_leiden_0.csv`" in md


def test_build_report_falls_back_when_model_fails(tmp_path):
    art = tmp_path / "artifacts"
    (art / "figures").mkdir(parents=True)
    (art / "figures" / "umap.png").write_bytes(b"\x89PNG")

    def boom(messages):
        raise RuntimeError("vLLM down")

    md = gw_app._build_report("prose synthesis", art, boom, "Q?")
    # deterministic gallery fallback — never lose the run over a report hiccup
    assert "prose synthesis" in md and "# Figures" in md


def test_review_report_cleans_draft_and_guards_degenerate(tmp_path):
    art = tmp_path / "artifacts"
    (art / "figures").mkdir(parents=True)
    (art / "figures" / "umap.png").write_bytes(b"\x89PNG")
    draft = "# Report\n\nTODO: fill this in.\n\n![umap](figures/umap.png)\n\nReal findings.\n" * 3

    # the model returns a cleaned version → we ship it, and it was given the valid fig paths
    seen = {}

    def cleaner(messages):
        seen["user"] = messages[-1]["content"]
        return ("# Report\n\n![umap](figures/umap.png)\n\n"
                + "Real findings with no placeholders, grounded in the data. " * 6)

    out = gw_app._review_report(draft, art, cleaner, "Q?")
    assert "TODO" not in out and "figures/umap.png" in seen["user"]

    # a degenerate review (model returns near-nothing) → keep the original draft
    assert gw_app._review_report(draft, art, lambda m: "x", "Q?") == draft
    # a failing review → keep the original draft (never lose the report)
    def boom(m):
        raise RuntimeError("down")
    assert gw_app._review_report(draft, art, boom, "Q?") == draft


def test_review_and_finalize_report_reapplies_literature_references(tmp_path):
    art = tmp_path / "artifacts"
    (art / "figures").mkdir(parents=True)
    draft = (
        "# Report\n\n## Results\n\nGrounded findings.\n\n## References\n\n"
        "1. Good Ref. doi:10.1234/good https://doi.org/10.1234/good\n"
    )
    lit = {"citations": [{
        "citation": "Kida J et al. (2025) Germline DDX41 mutations in myeloid neoplasms. doi:10.1097/moh.0000000000000854",
        "url": "https://doi.org/10.1097/moh.0000000000000854",
    }]}

    def corrupting_reviewer(_messages):
        return (
            "# Report\n\n## Results\n\nGrounded findings.\n\n## References\n\n"
            "- Publication Only. https://europepmc.org/article/PMC/PMC12163242\n"
        )

    out = gw_app._review_and_finalize_report(draft, art, corrupting_reviewer, "Q?", lit)

    assert "Germline DDX41 mutations in myeloid neoplasms" in out
    assert "10.1097/moh.0000000000000854" in out
    assert "Publication Only" not in out


def test_remove_body_bibliography_metadata_keeps_final_references():
    md = (
        "# Report\n\n"
        "## Discussion\n\n"
        "### Key Findings from Relevant Literature\n\n"
        "- **Title:** MicroRNA-218-5p-Ddx41 axis restrains neuroinflammation.\n"
        "  - **Authors:** Wang D, Gao H, Qin Q.\n"
        "  - **DOI/PMID:** 10.1186/s12967-024-04881-w\n\n"
        "Wang et al. provide context for DDX41-linked inflammatory signaling.\n\n"
        "## References\n\n"
        "1. Wang D et al. (2024) MicroRNA-218-5p-Ddx41 axis restrains "
        "neuroinflammation. doi:10.1186/s12967-024-04881-w\n"
    )

    out = gw_app._remove_body_bibliography_metadata(md)

    body = out.split("## References", 1)[0]
    assert "Title:" not in body
    assert "Authors:" not in body
    assert "DOI/PMID:" not in body
    assert "Wang et al. provide context" in out
    assert "## References" in out
    assert "10.1186/s12967-024-04881-w" in out


def test_remove_literature_figure_callouts_keeps_results_figure_refs():
    md = (
        "# Report\n\n"
        "## Results\n\n"
        "The UMAP separates major retinal classes (see Figure 1).\n\n"
        "## Literature Review\n\n"
        "Our literature search identified two studies relevant to DDX41 in retina biology "
        "(see Figures 1-2 for cited figure references). These studies support interpretation.\n\n"
        "### Antiviral defense mechanisms\n\n"
        "Sauter MM et al. (2023) described retinal antiviral defense, see Figure 2 for context.\n\n"
        "## References\n\n"
        "1. Sauter MM et al. (2023) Retinal antiviral defense. doi:10.1016/j.exer.2023.109647\n"
    )

    out = gw_app._remove_literature_figure_callouts(md)

    assert "The UMAP separates major retinal classes (see Figure 1)." in out
    literature = out.split("## Literature Review", 1)[1].split("## References", 1)[0]
    assert "see Figures" not in literature
    assert "see Figure" not in literature
    assert "These studies support interpretation." in literature
    assert "## References" in out
    assert "10.1016/j.exer.2023.109647" in out


def test_references_from_accepted_literature_search_prefers_summarized_citations():
    from types import SimpleNamespace

    accepted_lit_round = SimpleNamespace(
        verdict=SimpleNamespace(verdict="accept"),
        scientist_result={
            "final_answer": (
                "Use Mars et al. doi:10.64898/2026.01.28.26344834 and "
                "Sauter et al. PMID:37689341 for the final interpretation."
            ),
            "steps": [{
                "tool": "literature_search",
                "ok": True,
                "args": {"query": "DDX41 retina innate immunity"},
                "result": {
                    "status": "ok",
                    "query": "DDX41 retina innate immunity",
                    "results": [
                        {
                            "title": "Biallelic germline variants in DDX41 cause retinal dystrophy",
                            "authors": "Mars Z",
                            "year": "2026",
                            "journal": "bioRxiv",
                            "doi": "10.64898/2026.01.28.26344834",
                            "pmid": "",
                            "url": "https://doi.org/10.64898/2026.01.28.26344834",
                            "citation": "Mars Z et al. (2026) Biallelic germline variants in DDX41 cause retinal dystrophy. doi:10.64898/2026.01.28.26344834",
                        },
                        {
                            "title": "The RLR intrinsic antiviral system is expressed in neural retina",
                            "authors": "Sauter MM",
                            "year": "2023",
                            "journal": "Experimental Eye Research",
                            "doi": "10.1016/j.exer.2023.109647",
                            "pmid": "37689341",
                            "url": "https://doi.org/10.1016/j.exer.2023.109647",
                            "citation": "Sauter MM et al. (2023) The RLR intrinsic antiviral system is expressed in neural retina. doi:10.1016/j.exer.2023.109647",
                        },
                        {
                            "title": "Generic immunity paper not chosen by the scientist",
                            "authors": "Off T",
                            "year": "2025",
                            "journal": "Immunity",
                            "doi": "10.9999/off.topic",
                            "pmid": "",
                            "url": "https://doi.org/10.9999/off.topic",
                            "citation": "Off T et al. (2025) Generic immunity paper. doi:10.9999/off.topic",
                        },
                    ],
                },
            }],
        },
    )
    rejected_round = SimpleNamespace(
        verdict=SimpleNamespace(verdict="revise"),
        scientist_result={
            "final_answer": "doi:10.0000/rejected",
            "steps": [{
                "tool": "literature_search",
                "ok": True,
                "result": {"results": [{
                    "doi": "10.0000/rejected",
                    "citation": "Rejected ref. doi:10.0000/rejected",
                }]},
            }],
        },
    )

    lit = gw_app._references_from_accepted_literature_search(
        SimpleNamespace(rounds=[rejected_round, accepted_lit_round]))

    assert lit and lit["tier"] == "lab_literature_search"
    rendered = "\n".join(c["citation"] for c in lit["citations"])
    assert "10.64898/2026.01.28.26344834" in rendered
    assert "10.1016/j.exer.2023.109647" in rendered
    assert "10.9999/off.topic" not in rendered
    assert "10.0000/rejected" not in rendered

    no_explicit_citation_round = SimpleNamespace(
        verdict=SimpleNamespace(verdict="accept"),
        scientist_result={
            "final_answer": "Mars et al. looked relevant but no DOI or PMID was recorded.",
            "steps": accepted_lit_round.scientist_result["steps"],
        },
    )
    assert gw_app._references_from_accepted_literature_search(
        SimpleNamespace(rounds=[no_explicit_citation_round])) is None


def test_quarantine_strays_moves_offscript_files_only(tmp_path):
    art = tmp_path / "artifacts"
    for d in ("report", "figures", "tables", "process", "data"):
        (art / d).mkdir(parents=True)
    (art / "figures" / "umap.png").write_bytes(b"\x89PNG")
    # off-script junk the model dumped at the artifacts root
    (art / "qc_report.docx").write_bytes(b"PK\x03\x04")
    (art / "Single_Cell_Analysis_Report.zip").write_bytes(b"PK\x03\x04")

    moved = gw_app._quarantine_strays(art)
    assert set(moved) == {"qc_report.docx", "Single_Cell_Analysis_Report.zip"}
    # canonical dirs untouched; strays relocated (moved, not deleted)
    assert (art / "figures" / "umap.png").exists()
    assert not (art / "qc_report.docx").exists()
    assert (art / "extra" / "qc_report.docx").exists()
    # idempotent: a second pass finds nothing new (extra/ is itself canonical)
    assert gw_app._quarantine_strays(art) == []


def test_release_my_gpu_uses_squeue_not_stale_alloc(monkeypatch):
    """The release path finds live jobs via `squeue --me` over the SAME executor, so it
    works without a cached alloc and never needs a second (Duo) authentication."""
    from types import SimpleNamespace
    calls = []

    class FakeExec:
        """Stateful, because `_release_my_gpu` queries squeue TWICE with different meaning: once to
        discover live jobs, then again AFTER scancel to confirm the kill (scancel exits 0 even for a
        job that never existed, so only a re-query proves it died). A fake that returns the same id
        every time is claiming the job survived its own scancel."""

        username = "testuser"

        def __init__(self):
            self.cancelled = False

        def exec(self, cmd, *a, **k):
            calls.append(cmd)
            if cmd.startswith("squeue"):
                return SimpleNamespace(out="" if self.cancelled else "123456\n", ok=True)
            if cmd.startswith("scancel"):
                self.cancelled = True
            return SimpleNamespace(out="", ok=True)

    conn = SimpleNamespace(executor=FakeExec(), alloc=None)  # no cached allocation
    # The confirm loop sleeps between polls; the fake reports the job gone on the first one, so this
    # only skips the 1s the real Slurm state transition needs.
    monkeypatch.setattr(gw_app.time, "sleep", lambda _s: None)
    ids, err = gw_app._release_my_gpu(conn)
    assert err is None and ids == ["123456"]
    assert any(c.startswith("squeue --me") for c in calls)
    assert any(c == "scancel 123456" for c in calls)


# --- delete boundary: a user can ONLY delete inside their own folder ---------

def _seed_run(root, owner, run_id):
    d = root / owner / run_id / "artifacts"
    d.mkdir(parents=True)
    (d / "report.md").write_text("x", encoding="utf-8")
    return root / owner / run_id


def test_results_delete_is_forced_to_the_authenticated_owner(tmp_path, monkeypatch):
    import types
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    victim = _seed_run(tmp_path, "victim", "run1")
    mine = _seed_run(tmp_path, "attacker", "run2")
    # accounts on; the caller is "attacker"
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: types.SimpleNamespace(username="attacker", id=7))

    # attacker tries to delete the victim's run by passing owner="victim" — IGNORED.
    r = _client().post("/api/results/delete", json={"owner": "victim", "run_id": "run1"})
    assert r.status_code == 200
    assert victim.is_dir()                              # victim's run survives — owner was forced
    assert r.json()["deleted"] == "attacker/run1"       # it acted in the attacker's own folder

    # attacker can delete their OWN run
    r2 = _client().post("/api/results/delete", json={"run_id": "run2"})
    assert r2.status_code == 200 and not mine.is_dir()


def test_results_delete_requires_login_when_accounts_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: None)   # not logged in
    r = _client().post("/api/results/delete", json={"owner": "x", "run_id": "y"})
    assert r.status_code == 401


def test_cancel_alloc_scancels_own_job_and_skips_others():
    import types

    calls = []
    executor = types.SimpleNamespace(username="me", exec=lambda cmd: calls.append(cmd))
    # own job → scancel issued
    conn = types.SimpleNamespace(mock=False, executor=executor,
                                 alloc=types.SimpleNamespace(job_id="J1", owner="me"),
                                 emit=lambda *a, **k: None)
    gw_app._cancel_alloc(conn, "the connection failed")
    assert calls == ["scancel J1"]

    # someone else's job → never touched
    calls.clear()
    conn.alloc = types.SimpleNamespace(job_id="J2", owner="other")
    gw_app._cancel_alloc(conn)
    assert calls == []

    # no alloc / mock → no-op
    calls.clear()
    gw_app._cancel_alloc(types.SimpleNamespace(mock=False, executor=executor, alloc=None, emit=lambda *a, **k: None))
    assert calls == []


def test_results_delete_rejects_traversal_run_id(tmp_path, monkeypatch):
    import types
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: types.SimpleNamespace(username="attacker", id=7))
    r = _client().post("/api/results/delete", json={"run_id": "../victim/run1"})
    assert r.status_code == 400                          # non-alnum run_id rejected before any fs touch


# --- role split: reasoning roles may run on a different endpoint than the Scientist ----------

_LAB_ENV = ("BIOAGENT_LAB_LLM_BASE_URL", "BIOAGENT_LAB_LLM_MODEL", "BIOAGENT_LAB_LLM_API_KEY")
_BASE_ENV = ("BIOAGENT_LLM_BASE_URL", "BIOAGENT_LLM_API_KEY", "BIOAGENT_LLM_MODEL")


def _clear(monkeypatch, *groups):
    for g in groups:
        for v in g:
            monkeypatch.delenv(v, raising=False)


def test_lab_role_endpoint_splits_reasoning_from_tool_calling(monkeypatch):
    """PI/Critic go to the paid API; the Scientist stays on the session's local vLLM."""
    _clear(monkeypatch, _BASE_ENV, _LAB_ENV)
    monkeypatch.setenv("BIOAGENT_LAB_LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("BIOAGENT_LAB_LLM_MODEL", "vendor/big-model")
    monkeypatch.setenv("BIOAGENT_LAB_LLM_API_KEY", "sk-lab")
    seen: list[dict] = []
    monkeypatch.setattr(vllm_client, "complete",
                        lambda port, model, messages, **kw:
                        seen.append({"role": "lab", "port": port, "model": model,
                                     "base": kw.get("base_url"), "key": kw.get("api_key")}) or "x")
    monkeypatch.setattr(vllm_client, "chat_tools",
                        lambda port, model, messages, tools, **kw:
                        seen.append({"role": "sci", "port": port, "model": model,
                                     "base": kw.get("base_url")}) or {"content": "", "tool_calls": []})

    r = gw_app._lab_llm(_FakeConn())
    r.complete_fn([{"role": "user", "content": "plan"}])
    r.scientist_chat([{"role": "user", "content": "run"}], [])

    lab = next(s for s in seen if s["role"] == "lab")
    sci = next(s for s in seen if s["role"] == "sci")
    assert lab["base"] == "https://api.example.com/v1" and lab["model"] == "vendor/big-model"
    assert lab["key"] == "sk-lab" and lab["port"] == 0
    # the Scientist did NOT follow it off-cluster
    assert sci["base"] is None and sci["port"] == 37219
    assert sci["model"] == "QuantTrio/Qwen3.6-35B-A3B-AWQ"

    # the two exposures are reported separately: the guard keys on the Scientist endpoint
    assert r.lab_role_remote is True
    assert r.scientist_remote is False


def test_without_the_lab_override_both_roles_share_one_endpoint(monkeypatch):
    _clear(monkeypatch, _BASE_ENV, _LAB_ENV)
    r = gw_app._lab_llm(_FakeConn())
    assert r.lab_role_remote is False and r.scientist_remote is False
    assert r.lab_label == r.label


def test_a_loopback_lab_endpoint_is_not_reported_as_egress(monkeypatch):
    """Self-hosting a bigger model on the gateway box is a role split with no data leaving."""
    _clear(monkeypatch, _BASE_ENV, _LAB_ENV)
    monkeypatch.setenv("BIOAGENT_LAB_LLM_BASE_URL", "http://127.0.0.1:8001/v1")
    r = gw_app._lab_llm(_FakeConn())
    assert r.lab_role_remote is False
