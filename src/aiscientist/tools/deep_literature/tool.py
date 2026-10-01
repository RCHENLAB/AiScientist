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
    (env ``AISCIENTIST_PAPERQA_PAPERS``); without papers/metadata keys the search is limited.
  * Pick + pre-download the local embedding model (env ``AISCIENTIST_PAPERQA_EMBEDDING``) and
    confirm ``paper-qa[local]`` installs cleanly in the server env.
  * Verify end-to-end against the live Qwen tunnel and confirm nothing hits a cloud API
    (PaperQA ``verbosity=3`` logs every LLM/embedding call — use it to audit). The exact
    LiteLLM model string may need to match vLLM's ``--served-model-name``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
#load module from AISCIENTIST_PAPERQA_EMBEDDING if no then use default
# Local sentence-transformers embedding (runs in-process, nothing leaves the host).
# Overridable via env so the server can pin whatever model it has pre-downloaded.
# Default = biomedical PubMedBERT (768-dim); MUST match the model used to build the
# persistent index in deploy/paperqa/embed_corpus.slurm, or the vectors won't line up
# and PaperQA will silently re-embed the whole corpus per query.
_DEFAULT_LOCAL_EMBEDDING = os.environ.get(
    "AISCIENTIST_PAPERQA_EMBEDDING", "st-NeuML/pubmedbert-base-embeddings"
)
# Reuse the index the Slurm job pre-built (same name + directory + embedding + papers),
# instead of rebuilding on every query. Set AISCIENTIST_PAPERQA_INDEX_DIR to the Slurm
# job's INDEX_DIR (e.g. .../retigene/index_pubmedbert) to point at it.
_DEFAULT_INDEX_NAME = os.environ.get("AISCIENTIST_PAPERQA_INDEX_NAME", "retigene_full_pubmedbert")
_DEFAULT_INDEX_DIR = os.environ.get("AISCIENTIST_PAPERQA_INDEX_DIR")


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


# --- LLM call budget ----------------------------------------------------------------------
# lmi (PaperQA's LLM layer) gives each request 60 s unless told otherwise (``ModelSpec.timeout``).
# That is a chat model's budget, not a reasoning model's. On 2026-09-30 Qwen3.8, thinking at its
# default effort (xhigh), gathered 35 passages for an RP question and then never answered. Each
# answer attempt was 3 x 60 s, because the OpenAI SDK retries a timeout twice ("time taken=181 s").
# lmi retried that 3 more times, the whole rollout hit PaperQA's own budget, and the job ran
# 21 minutes for an empty answer. vLLM stops a request when its client gives up (vLLM 0.28 does not
# count that as an abort in /metrics), but the same request was re-sent every 60 s and thought from
# zero each time. Reproduced on HPC3: 80k generated tokens, about 18 GPU-minutes, thrown away. So:
#   * the per-request timeout covers the slowest legitimate call (the answer over ~20 sources);
#   * calls that write prose think at a LOW effort. It goes in the request body as vLLM's top-level
#     ``reasoning_effort`` via litellm's ``extra_body``, because litellm rejects a top-level
#     ``reasoning_effort`` for a model it has not mapped as a reasoning model;
#   * the per-passage summaries (evidence_k of them per question) do not think at all. Each one
#     only restates one excerpt and scores its relevance as JSON.
_DEFAULT_LLM_TIMEOUT = 600.0
_DEFAULT_REASONING_EFFORT = "low"
_DEFAULT_SUMMARY_REASONING_EFFORT = "off"
# PaperQA's time budget for the whole search -> evidence -> answer rollout. PaperQA's default is
# 500 s. When it runs out, PaperQA cancels what is running and answers from the evidence it has.
# Evidence gathering is all-or-nothing, so a cut there leaves no evidence at all.
_DEFAULT_AGENT_TIMEOUT = 500.0
# Effort levels passed through as-is (the list vllm_client.REASONING_EFFORTS uses). Models differ:
# Qwen3.8 takes low / medium / xhigh and answers "high" with HTTP 400. "off" switches thinking off
# through the chat template; any other value (e.g. "default") sends nothing, i.e. the model's own
# default.
_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "none", "minimal"})


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


def _env_seconds(name: str, default: float) -> float:
    """A positive number of seconds from the environment. Zero, negative or garbage falls back to
    ``default``: a zero timeout would fail every call at once."""
    val = _env_float(name, default)
    return val if val > 0 else default


def _thinking_body(effort: str) -> dict[str, Any]:
    """The request-body fields that set how hard the served model thinks, for litellm's
    ``extra_body`` (merged verbatim into the JSON vLLM receives). Empty means send nothing."""
    effort = (effort or "").strip().lower()
    if effort == "off":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    if effort in _REASONING_EFFORTS:
        return {"reasoning_effort": effort}
    return {}


def _llm_config(model: str, api_base: str, effort: str) -> dict[str, Any]:
    """LiteLLM router config (the ``model_list`` shape lmi reads) that points ``model`` at the
    local vLLM ``/v1`` server. lmi maps ``timeout`` onto its per-request timeout and passes every
    other key, ``extra_body`` included, through to ``litellm.acompletion``."""
    params: dict[str, Any] = {
        "model": model,
        "api_base": api_base,
        "api_key": os.environ.get("AISCIENTIST_LLM_API_KEY", "sk-no-key-required"),
        # 0.0 by default: two identical questions must return the same papers,
        # or "did the retrieval improve?" is unanswerable.
        "temperature": _env_float("AISCIENTIST_PAPERQA_TEMPERATURE", 0.0),
        "timeout": _env_seconds("AISCIENTIST_PAPERQA_LLM_TIMEOUT", _DEFAULT_LLM_TIMEOUT),
    }
    body = _thinking_body(effort)
    if body:
        params["extra_body"] = body
    return {"model_list": [{"model_name": model, "litellm_params": params}]}


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
    base = getattr(ctx, "llm_base_url", None) or os.environ.get("AISCIENTIST_PAPERQA_LLM_BASE_URL")
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
    # Two configs for the same endpoint: the calls that write prose (search queries, the answer,
    # the closing tool call) think at a low effort; the per-passage summaries do not think.
    local_cfg = _llm_config(model, api_base, os.environ.get("AISCIENTIST_PAPERQA_REASONING_EFFORT")
                            or _DEFAULT_REASONING_EFFORT)
    summary_cfg = _llm_config(model, api_base,
                              os.environ.get("AISCIENTIST_PAPERQA_SUMMARY_REASONING_EFFORT")
                              or _DEFAULT_SUMMARY_REASONING_EFFORT)

    # Where the lab's PDFs live. PaperQA reads (never modifies) this directory.
    papers = os.environ.get("AISCIENTIST_PAPERQA_PAPERS")
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
    # AISCIENTIST_PAPERQA_SYNC_INDEX=1 for that maintenance run.
    index_cfg = IndexSettings(
        name=_DEFAULT_INDEX_NAME,
        paper_directory=papers,
        recurse_subdirectories=False,
        sync_with_paper_directory=(os.environ.get("AISCIENTIST_PAPERQA_SYNC_INDEX", "").strip()
                                   in ("1", "true", "yes")),
    )
    if _DEFAULT_INDEX_DIR:
        index_cfg.index_directory = _writable_index_root(_DEFAULT_INDEX_DIR, ctx)
    manifest = os.environ.get("AISCIENTIST_PAPERQA_MANIFEST")
    if manifest:
        index_cfg.manifest_file = manifest

    # Agent type. The default LLM-driven ToolSelector agent currently crashes with this
    # paper-qa/litellm combo ("'LiteLLMModel' object has no attribute 'get_router'" in
    # make_aviary_tool_selector). The "fake" agent runs the same RAG (paper_search ->
    # gather_evidence -> gen_answer) on the local embeddings + Qwen deterministically,
    # without that broken tool-selector, and produces the cited answer. Override with
    # AISCIENTIST_PAPERQA_AGENT_TYPE=ToolSelector once the version incompatibility is fixed.
    agent_type = os.environ.get("AISCIENTIST_PAPERQA_AGENT_TYPE", "fake")

    return Settings(
        llm=model,
        llm_config=local_cfg,
        summary_llm=model,
        summary_llm_config=summary_cfg,
        embedding=_DEFAULT_LOCAL_EMBEDDING,
        parsing=ParsingSettings(use_doc_details=False, multimodal=MultimodalOptions.OFF),
        # Widen every stage of the funnel (see the _DEFAULT_* block above). Leaving `answer`
        # unset means PaperQA's 5-source default, which structurally cannot answer a
        # "which genes cause X" question no matter how good the index is.
        answer=AnswerSettings(
            evidence_k=_env_int("AISCIENTIST_PAPERQA_EVIDENCE_K", _DEFAULT_EVIDENCE_K),
            answer_max_sources=_env_int("AISCIENTIST_PAPERQA_MAX_SOURCES", _DEFAULT_MAX_SOURCES),
            answer_length=os.environ.get("AISCIENTIST_PAPERQA_ANSWER_LENGTH") or _DEFAULT_ANSWER_LENGTH,
            max_concurrent_requests=_env_int("AISCIENTIST_PAPERQA_CONCURRENCY", _DEFAULT_CONCURRENCY),
        ),
        agent=AgentSettings(
            agent_type=agent_type,
            agent_llm=model,
            agent_llm_config=local_cfg,
            index=index_cfg,
            # How many DOCUMENTS the index search returns before evidence gathering. This is the
            # tightest gate of the three: at the default 8, a paper filed under "Stargardt
            # disease" never becomes a candidate for a "macular atrophy" query.
            search_count=_env_int("AISCIENTIST_PAPERQA_SEARCH_COUNT", _DEFAULT_SEARCH_COUNT),
            timeout=_env_seconds("AISCIENTIST_PAPERQA_AGENT_TIMEOUT", _DEFAULT_AGENT_TIMEOUT),
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
    if getattr(LiteLLMModel.select_tool, "_aiscientist_patched", False):
        return

    async def select_tool(self, *selection_args, **selection_kwargs):
        primary = self.llm_config.models[0]

        async def _acompletion(*args, **kw):
            kw.pop("model", None)                     # the primary's model always wins
            return await litellm.acompletion(**primary.to_litellm_kwargs(), **kw)

        selector = ToolSelector(model_name=self.name, acompletion=track_costs(_acompletion))
        return await selector(*selection_args, **selection_kwargs)

    select_tool._aiscientist_patched = True   # type: ignore[attr-defined]
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
    # How the rollout ended (paperqa AgentStatus: success / unsure / truncated / fail). "truncated"
    # means PaperQA's own time budget ran out and it answered from whatever it had gathered.
    status = getattr(resp, "status", None)
    agent_status = str(getattr(status, "value", status) or "")
    return {"formatted_answer": str(formatted), "answer": str(answer), "contexts": contexts,
            "agent_status": agent_status}

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
    agent_status = result.get("agent_status") or ""
    if not result.get("contexts"):
        # A retrieval that matched NOTHING used to come back as `status: ok` with an empty answer,
        # which is indistinguishable from success at the call site. On the DDX41 retina run that is
        # exactly what happened: one call, `ok: 0 context(s)`, and the step then quietly completed
        # on `literature_search` results while the report's Methods still claimed a deep RAG search
        # had run. Retrieving nothing is a failed lookup and has to say so — the caller can rephrase
        # or fall back, but it must not be able to mistake this for an answer.
        error = (
            "the indexed corpus returned 0 passages for this question, so there is no "
            "grounded answer. Two usual causes: (a) the question is a bag of identifiers "
            "(gene symbols, a pathway accession) rather than a sentence — this is a semantic "
            "retriever, so ask it a question; (b) the corpus does not cover the topic, in "
            "which case `literature_search` (live Europe PMC) is the right tool and the "
            "write-up must say the deep search returned nothing."
        )
        if agent_status == "truncated":
            # Possibly not the corpus: evidence gathering keeps nothing when the budget cuts it,
            # so a slow endpoint also ends with 0 passages. Rephrasing the question would not help.
            error = (
                "PaperQA's time budget ran out (agent status 'truncated') and it ended with 0 "
                "passages, so there is no grounded answer. Evidence gathering keeps nothing when "
                "it is cut, so this does not show that the corpus lacks the topic: the model "
                "endpoint was too slow. Retry later, or use `literature_search`."
            )
        return {
            "status": "failed",
            "question": question,
            "n_contexts": 0,
            "agent_status": agent_status,
            "error": error,
        }
    n_contexts = len(result.get("contexts") or [])
    if not (result.get("answer") or "").strip():
        # Passages but no answer: the answer call itself failed. On 2026-09-30 an RP question
        # gathered 35 passages, every answer attempt timed out, and the result still said `ok`.
        # The lab then accepted a bare citation list as the literature step and skipped the
        # Europe PMC fallback. `failed` sends the lab to its fallback. The passages stay in the
        # result: chat grounds its answer on them, and without them it told the user the corpus
        # had nothing on the topic.
        result.update({
            "status": "failed",
            "question": question,
            "n_contexts": n_contexts,
            "error": (
                f"PaperQA gathered {n_contexts} passage(s) but produced no answer: the answer "
                "step failed (the job log has the cause). The passages are evidence, not an "
                "answer."
            ),
        })
        return result
    result.update({"status": "ok", "question": question, "n_contexts": n_contexts})
    return result

#make run_paperqa a tool
def make_paperqa_tool() -> Any:
    """The deep-literature tool: grounded, cited answers via PaperQA2 over local Qwen.
    Imported lazily so ``tools.paperqa_search`` has no dependency on the agents package."""
    from ..sdk import HarnessTool

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
