"""Deep literature agent backed by PaperQA2 (the open-source engine behind Edison
Literature) — github.com/Future-House/paper-qa.

Where ``literature_search`` (Europe PMC) returns a *list of papers*, this tool returns a
*grounded, cited answer*: PaperQA runs the full RAG loop — search candidate papers, chunk +
embed them, gather evidence, re-rank, and synthesize an answer with in-text citations — so
the Scientist/PI can interpret findings against the real literature instead of guessing.

PRIVACY — keeps reasoning inside UCI:
  * The LLM (general / summary / agent) is pointed at THIS run's local Qwen vLLM endpoint
    (OpenAI-compatible ``/v1`` over the SSH tunnel ``ctx.tunnel_port``), via LiteLLM's
    ``model_list``. No prompt text goes to a cloud model.
  * The embedding model is a LOCAL sentence-transformers model (``st-`` prefix), so chunk
    text is never sent to a remote embedding API. Needs ``pip install paper-qa[local]``.
  * Only PaperQA's metadata/search clients (Crossref / Semantic Scholar / Unpaywall) touch
    the network, with public bibliographic queries — never the dataset. This matches the
    project's "public literature retrieval is allowed, private data never leaves" boundary.

OPTIONAL DEPENDENCY: ``paper-qa`` is heavy, so it is imported lazily. When it (or the local
extras) is absent, the tool returns ``status="dependency_missing"`` and the run continues —
exactly like the scanpy analysis line (``scrna_pack``).

OPEN ITEMS to confirm/finish on the server (cannot be settled from a dev laptop, so they are
left as env-configurable knobs, not hard-coded):
  * PaperQA reads a corpus of PDFs from ``paper_directory``. Decide where the lab's PDFs live
    (env ``BIOAGENT_PAPERQA_PAPERS``); without papers/metadata keys the search is limited.
  * Pick + pre-download the local embedding model (env ``BIOAGENT_PAPERQA_EMBEDDING``) and
    confirm ``paper-qa[local]`` installs cleanly in the server env.
  * Verify end-to-end against the live Qwen tunnel and confirm nothing hits a cloud API
    (PaperQA ``verbosity=3`` logs every LLM/embedding call — use it to audit). The exact
    LiteLLM model string may need to match vLLM's ``--served-model-name``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
#load module from BIOAGENT_PAPERQA_EMBEDDING if no then use default
# Local sentence-transformers embedding (runs in-process, nothing leaves the host).
# Overridable via env so the server can pin whatever model it has pre-downloaded.
# Default = biomedical PubMedBERT (768-dim); MUST match the model used to build the
# persistent index in deploy/paperqa/embed_corpus.slurm, or the vectors won't line up
# and PaperQA will silently re-embed the whole corpus per query.
_DEFAULT_LOCAL_EMBEDDING = os.environ.get(
    "BIOAGENT_PAPERQA_EMBEDDING", "st-NeuML/pubmedbert-base-embeddings"
)
# Reuse the index the Slurm job pre-built (same name + directory + embedding + papers),
# instead of rebuilding on every query. Set BIOAGENT_PAPERQA_INDEX_DIR to the Slurm
# job's INDEX_DIR (e.g. .../retigene/index_pubmedbert) to point at it.
_DEFAULT_INDEX_NAME = os.environ.get("BIOAGENT_PAPERQA_INDEX_NAME", "retigene_full_pubmedbert")
_DEFAULT_INDEX_DIR = os.environ.get("BIOAGENT_PAPERQA_INDEX_DIR")


# --- Retrieval breadth ------------------------------------------------------------------
# PaperQA's out-of-the-box defaults are tuned for a handful of papers, not a 1739-PDF corpus:
# search_count=8 candidate DOCS, evidence_k=10 chunks, answer_max_sources=5 sources in the
# final answer, answer_length="about 200 words". On a REVERSE question ("which genes cause
# macular atrophy?") the correct answer spans a dozen papers, so the 5-source gate silently
# truncates it — the classic genes were retrieved and then dropped, which reads as "the search
# is inaccurate". The same gate explains the run-to-run instability: among 1739 papers a wide
# band scores near-identically, so which 5 survive shifts between calls.
#
# These are env-tunable rather than hard-coded because the right value trades answer breadth
# against wall-clock: every extra evidence chunk is another summary-LLM round trip, and the
# only honest way to pick is to measure against a gold set on the real corpus.
_DEFAULT_SEARCH_COUNT = 40      # candidate documents pulled from the index
_DEFAULT_EVIDENCE_K = 40        # chunks summarized as evidence
_DEFAULT_MAX_SOURCES = 20       # sources allowed into the synthesized answer
_DEFAULT_ANSWER_LENGTH = (
    "about 500 words. "
    "When the answer is a list of genes, rank them by how many independent papers in the context support each one: give at most 10 as the primary list, strongest evidence first, and put everything else under 'also reported'. "
    "Every gene symbol you write must appear verbatim in the cited context - never infer, expand or abbreviate a symbol."
)
# The summary step is embarrassingly parallel; PaperQA's default of 4 leaves the served Qwen
# mostly idle and makes a wide evidence_k needlessly slow.
_DEFAULT_CONCURRENCY = 12


def _env_int(name: str, default: int) -> int:
    """An int from the environment, falling back to ``default`` on unset/garbage. A typo in a
    deployed .env must not crash the tool — deep_literature's whole contract is to degrade."""
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """Same contract as :func:`_env_int`, for the sampling temperature."""
    try:
        return float(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _missing(dep: str, note: str) -> dict[str, Any]:
    """Match the scrna_pack convention for a gracefully-skipped optional dependency."""
    return {"status": "dependency_missing", "dependency": dep, "note": note}


#local qwen endpoint
def _local_endpoint(ctx: Any) -> tuple[str, str] | None:
    """(litellm_model_name, api_base) for this run's local Qwen, or None if unavailable.

    The harness threads the session's tunnel port + served model name onto the context
    (``ctx.tunnel_port`` / ``ctx.model`` — the same ones the live ``chat_tools`` path uses),
    so PaperQA talks to the exact model the rest of the pipeline uses. The ``openai/`` prefix
    routes LiteLLM to the OpenAI-compatible vLLM ``/v1`` server.
    """
    model = getattr(ctx, "model", None)
    if not model:
        return None
    # OFFLOAD path: when PaperQA runs ON HPC3 (not on the eyeserver), it reaches the served
    # Qwen at the GPU node's own address:port rather than through the eyeserver SSH tunnel.
    # An explicit base URL on the context / env (e.g. http://<gpu-node>:<port>/v1) wins.
    base = getattr(ctx, "llm_base_url", None) or os.environ.get("BIOAGENT_PAPERQA_LLM_BASE_URL")
    if base:
        return f"openai/{model}", base.rstrip("/")
    # IN-PROCESS path: the harness threads the session's SSH tunnel port onto the context.
    port = getattr(ctx, "tunnel_port", None)
    if not port:
        return None
    return f"openai/{model}", f"http://127.0.0.1:{port}/v1"


def _build_settings(ctx: Any) -> Any:
    """Assemble a PaperQA ``Settings`` pinned to local Qwen + local embeddings."""
    from paperqa import Settings
    from paperqa.settings import (
        AgentSettings,
        AnswerSettings,
        IndexSettings,
        MultimodalOptions,
        ParsingSettings,
    )

    endpoint = _local_endpoint(ctx)
    if endpoint is None:
        raise RuntimeError(
            "no local Qwen endpoint on the context (tunnel_port/model unset) — PaperQA "
            "needs a local model; start the gateway serve job or inject a chat endpoint."
        )
    model, api_base = endpoint
    # LiteLLM router config that points the model name at the local vLLM /v1 server.
    local_cfg = {
        "model_list": [
            {
                "model_name": model,
                "litellm_params": {
                    "model": model,
                    "api_base": api_base,
                    "api_key": os.environ.get("BIOAGENT_LLM_API_KEY", "sk-no-key-required"),
                    # 0.0 by default: two identical questions must return the same papers,
                    # or "did the retrieval improve?" is unanswerable.
                    "temperature": _env_float("BIOAGENT_PAPERQA_TEMPERATURE", 0.0),
                },
            }
        ]
    }

    # Where the lab's PDFs live. PaperQA reads (never modifies) this directory.
    papers = os.environ.get("BIOAGENT_PAPERQA_PAPERS")
    if not papers:
        workspace = getattr(ctx, "workspace", None)
        papers = str(Path(workspace) / "papers") if workspace else "papers"

    # Point at the persistent index the Slurm job built so queries don't re-embed the
    # whole corpus. name + index_directory + paper_directory + embedding must all match
    # what embed_corpus.slurm used for PaperQA to load it instead of rebuilding.
    # Match scripts/build_paperqa_directory_index.py IndexSettings EXACTLY so PaperQA REUSES the
    # pre-built 211M index instead of creating a fresh empty one (mismatched settings make it
    # ignore the persistent index and retrieve 0 papers).
    # QUERY-time use of a PREBUILT, shared index. `sync_with_paper_directory=True` (the old
    # setting) makes PaperQA compare the papers/ dir against the index on every query and try to
    # ADD any file it does not find — i.e. open the shared index for WRITING as whoever is
    # asking. The index belongs to the lab member who built it; every other user's query then
    # died with "Failed to open file for read: .managed.json … PermissionDenied" (tantivy's
    # writer lock), and the answer came back "I cannot answer". Building/refreshing the index is
    # a maintenance job (Ziyao's), not something a research run does. Opt back in with
    # BIOAGENT_PAPERQA_SYNC_INDEX=1 for that maintenance run.
    index_cfg = IndexSettings(
        name=_DEFAULT_INDEX_NAME,
        paper_directory=papers,
        recurse_subdirectories=False,
        sync_with_paper_directory=(os.environ.get("BIOAGENT_PAPERQA_SYNC_INDEX", "").strip()
                                   in ("1", "true", "yes")),
    )
    if _DEFAULT_INDEX_DIR:
        index_cfg.index_directory = _writable_index_root(_DEFAULT_INDEX_DIR, ctx)
    manifest = os.environ.get("BIOAGENT_PAPERQA_MANIFEST")
    if manifest:
        index_cfg.manifest_file = manifest

    # Agent type. The default LLM-driven ToolSelector agent currently crashes with this
    # paper-qa/litellm combo ("'LiteLLMModel' object has no attribute 'get_router'" in
    # make_aviary_tool_selector). The "fake" agent runs the same RAG (paper_search ->
    # gather_evidence -> gen_answer) on the local embeddings + Qwen deterministically,
    # without that broken tool-selector, and produces the cited answer. Override with
    # BIOAGENT_PAPERQA_AGENT_TYPE=ToolSelector once the version incompatibility is fixed.
    agent_type = os.environ.get("BIOAGENT_PAPERQA_AGENT_TYPE", "fake")

    return Settings(
        llm=model,
        llm_config=local_cfg,
        summary_llm=model,
        summary_llm_config=local_cfg,
        embedding=_DEFAULT_LOCAL_EMBEDDING,
        parsing=ParsingSettings(use_doc_details=False, multimodal=MultimodalOptions.OFF),
        # Widen every stage of the funnel (see the _DEFAULT_* block above). Leaving `answer`
        # unset means PaperQA's 5-source default, which structurally cannot answer a
        # "which genes cause X" question no matter how good the index is.
        answer=AnswerSettings(
            evidence_k=_env_int("BIOAGENT_PAPERQA_EVIDENCE_K", _DEFAULT_EVIDENCE_K),
            answer_max_sources=_env_int("BIOAGENT_PAPERQA_MAX_SOURCES", _DEFAULT_MAX_SOURCES),
            answer_length=os.environ.get("BIOAGENT_PAPERQA_ANSWER_LENGTH") or _DEFAULT_ANSWER_LENGTH,
            max_concurrent_requests=_env_int("BIOAGENT_PAPERQA_CONCURRENCY", _DEFAULT_CONCURRENCY),
        ),
        agent=AgentSettings(
            agent_type=agent_type,
            agent_llm=model,
            agent_llm_config=local_cfg,
            index=index_cfg,
            # How many DOCUMENTS the index search returns before evidence gathering. This is the
            # tightest gate of the three: at the default 8, a paper filed under "Stargardt
            # disease" never becomes a candidate for a "macular atrophy" query.
            search_count=_env_int("BIOAGENT_PAPERQA_SEARCH_COUNT", _DEFAULT_SEARCH_COUNT),
        ),
    )

def _writable_index_root(shared_index_dir: str, ctx: Any) -> str:
    """The index root PaperQA is handed at query time.

    ``paperqa.ask`` does not only READ the corpus index: ``agent_query`` always writes the answer
    into a second index, ``<index_directory>/answers/``, and calls ``save_index()``. Pointed at
    the lab's shared corpus root, that write runs as whoever is asking — and the answers/ index
    there belongs to the member who built the corpus, so every other user's query died in
    tantivy's writer with "Failed to open file for read: .managed.json … PermissionDenied" and
    the answer surfaced as "I cannot answer". Observed on every deep_literature job once the
    workspace-mount and lmi/aviary faults ahead of it were fixed.

    When the shared root is writable by this process, use it as before. Otherwise build a
    per-run root under the workspace holding a SYMLINK to the corpus index (read-only use — the
    query path never writes it once ``sync_with_paper_directory`` is off) and let PaperQA create
    its answers/ index next to it, where it can. Falls back to the shared root if the shim cannot
    be built, so the failure mode never gets worse than it was."""
    shared = Path(shared_index_dir)
    # Do NOT short-circuit on os.access(shared, W_OK): the shared root is group-writable, yet the
    # answers/ index INSIDE it carries its builder's tantivy lock/meta files, and a second user's
    # writer still fails there. A shared corpus root is never written at query time, full stop —
    # when a workspace exists, the answers index goes there.
    ws = getattr(ctx, "workspace", None)
    if not ws:
        return str(shared)
    try:
        root = Path(ws) / "pqa_index"
        root.mkdir(parents=True, exist_ok=True)
        link = root / _DEFAULT_INDEX_NAME
        target = shared / _DEFAULT_INDEX_NAME
        if not target.exists():
            return str(shared)
        if link.is_symlink() or link.exists():
            if link.is_symlink() and os.readlink(link) != str(target):
                link.unlink()
                link.symlink_to(target)
        else:
            link.symlink_to(target)
        return str(root)
    except OSError:
        return str(shared)


def _patch_lmi_select_tool() -> None:
    """Work around an lmi <-> aviary mismatch inside paperqa.sif (paper-qa 2026.3.18).

    ``LiteLLMModel.select_tool`` builds an inner ``_acompletion(**kw)`` — keyword-only — and hands
    it to aviary's ``ToolSelector``, which calls it as ``acompletion(model_name, **kw)``: one
    positional argument, so ``TypeError: _acompletion() takes 0 positional arguments but 1 was
    given``. The fake agent hits this on its LAST, purely formal step (the "complete" call, after
    gather_evidence found 3 relevant papers and generate_answer already produced the answer), the
    rollout is marked FAIL and the answer is thrown away as "I cannot answer". Observed on every
    deep_literature job the moment the workspace-mount bug was fixed. This redefines select_tool
    with a positional-tolerant closure; the primary model's own kwargs still win. No-op when the
    installed lmi does not have the shape this targets. The proper fix is pinning compatible
    lmi/aviary versions in paperqa.sif (Ziyao's image)."""
    try:
        import litellm
        from lmi.cost_tracker import track_costs
        from lmi.llms import LiteLLMModel
        from aviary.core import ToolSelector
    except Exception:  # noqa: BLE001 - not the environment this targets
        return
    if getattr(LiteLLMModel.select_tool, "_bioagent_patched", False):
        return

    async def select_tool(self, *selection_args, **selection_kwargs):
        primary = self.llm_config.models[0]

        async def _acompletion(*args, **kw):
            kw.pop("model", None)                     # the primary's model always wins
            return await litellm.acompletion(**primary.to_litellm_kwargs(), **kw)

        selector = ToolSelector(model_name=self.name, acompletion=track_costs(_acompletion))
        return await selector(*selection_args, **selection_kwargs)

    select_tool._bioagent_patched = True   # type: ignore[attr-defined]
    LiteLLMModel.select_tool = select_tool  # type: ignore[method-assign]


#get answer from paperqa and package to a payload
def _extract_answer(resp: Any) -> dict[str, Any]:
    """Pull the cited answer + contexts off a PaperQA response, tolerating API drift
    across paper-qa versions (``resp`` vs ``resp.session``)."""
    session = getattr(resp, "session", resp)
    formatted = getattr(resp, "formatted_answer", None) or getattr(session, "formatted_answer", "")
    answer = getattr(resp, "answer", None) or getattr(session, "answer", "")
    raw_contexts = getattr(resp, "contexts", None) or getattr(session, "contexts", []) or []
    contexts = []
    for c in raw_contexts:
        # Each context carries the source doc + the summarized snippet used as evidence.
        doc = getattr(getattr(c, "text", None), "doc", None)
        contexts.append(
            {
                "citation": getattr(doc, "formatted_citation", None) or getattr(doc, "citation", ""),
                "summary": getattr(c, "context", ""),
                "score": getattr(c, "score", None),
            }
        )
    return {"formatted_answer": str(formatted), "answer": str(answer), "contexts": contexts}

#call paperqa
def run_paperqa(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Answer a question against the literature with grounded, cited evidence (PaperQA2).

    Never raises: returns a ``status`` dict so the agent loop can adapt or skip — matching
    the ``literature_search`` / ``scrna_pack`` graceful-degrade contract.
    """
    question = str(args.get("question", "")).strip()
    if not question:
        return {"status": "error", "error": "empty question"}

    try:
        from paperqa import ask  # heavy; lazy import
        _patch_lmi_select_tool()
    except ImportError:
        return _missing(
            "paper-qa",
            "`paper-qa` is not installed. On the server: `pip install paper-qa[local]` "
            "(adds the local sentence-transformers embedding so nothing leaves UCI).",
        )

    try:
        settings = _build_settings(ctx)
    except RuntimeError as exc:
        return {"status": "unavailable", "error": str(exc), "question": question}
    except ImportError:
        return _missing(
            "paper-qa[local]",
            "`paper-qa` is installed but the local embedding extra is missing. "
            "Run `pip install paper-qa[local]` (sentence-transformers).",
        )

    try:
        resp = ask(question, settings=settings)
    except Exception as exc:  # noqa: BLE001 - a literature failure must never kill the run
        # Carry the LAST frames of the traceback: "error: 0 context(s)" in the event log was the
        # only trace of 28 consecutive failures, and it named neither the missing workspace mount
        # nor the lmi/aviary mismatch that followed. Nobody reads a Slurm log they do not know
        # exists; the failure has to explain itself in the tool result.
        import traceback as _tb
        frames = _tb.extract_tb(exc.__traceback__)[-4:]
        where = " <- ".join(f"{f.name}@{f.filename.rsplit('/', 1)[-1]}:{f.lineno}" for f in frames)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}",
                "where": where, "question": question}

    result = _extract_answer(resp)
    result.update({"status": "ok", "question": question})
    return result

#make run_paperqa a tool
def make_paperqa_tool() -> Any:
    """The deep-literature tool: grounded, cited answers via PaperQA2 over local Qwen.
    Imported lazily so ``tools.paperqa_search`` has no dependency on the agents package."""
    from ..agents.research_harness import HarnessTool

    return HarnessTool(
        "deep_literature",
        "Answer a focused scientific question against the published literature with a "
        "grounded, CITED answer (PaperQA2 deep RAG: search -> gather evidence -> synthesize). "
        "Use this — not literature_search — when you need an actual answer with evidence "
        "(e.g. 'Is RHO downregulation linked to photoreceptor apoptosis in retinitis "
        "pigmentosa?'), not just a list of papers. Returns a cited answer plus the supporting "
        "contexts. Cite ONLY what it returns. Heavier/slower than literature_search.",
        {"type": "object", "properties": {
            "question": {"type": "string",
                         "description": "a focused, answerable scientific question"}},
         "required": ["question"]},
        run_paperqa,
        reads_private_data=False, category="literature", requires=("paper-qa",),
    )
