"""Query a local PaperQA directory index.

This is the second step after ``build_paperqa_directory_index.py``:

1. build_paperqa_directory_index.py creates/updates the embedding index;
2. this script asks that index a question.

By default it runs in retrieval mode, which does not need an LLM and prints the
top cited papers/snippets. Full answer synthesis requires a running local
OpenAI-compatible LLM endpoint and ``--mode answer``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from paperqa import Settings
from paperqa.agents.search import get_directory_index
from paperqa.settings import AgentSettings, IndexSettings, MultimodalOptions, ParsingSettings


DEFAULT_PILOT_DIR = Path("output/retigene_papers/paperqa_pilot_50")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Tiny .env loader so the script can see OpenRouter settings locally.

    Existing shell environment variables win; values are never printed.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def default_llm() -> str:
    if os.environ.get("BIOAGENT_PAPERQA_LLM"):
        return os.environ["BIOAGENT_PAPERQA_LLM"]
    if os.environ.get("OPENROUTER_MODEL"):
        return f"openai/{os.environ['OPENROUTER_MODEL']}"
    return "openai/local-no-llm"


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Query a PaperQA index for a local PDF corpus.")
    parser.add_argument("question", help="Question to ask against the indexed PDF corpus.")
    parser.add_argument(
        "--papers",
        default=str(DEFAULT_PILOT_DIR / "papers"),
        help="Directory containing PDFs.",
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_PILOT_DIR / "paperqa_manifest.csv"),
        help="PaperQA manifest CSV.",
    )
    parser.add_argument(
        "--index-dir",
        default=str(DEFAULT_PILOT_DIR / "index_minilm"),
        help="Directory where PaperQA stores the index.",
    )
    parser.add_argument(
        "--embedding",
        default=os.environ.get("BIOAGENT_PAPERQA_EMBEDDING", "st-multi-qa-MiniLM-L6-cos-v1"),
        help="Local PaperQA embedding model, usually an st- sentence-transformers model.",
    )
    parser.add_argument("--name", default="retigene_pilot_50_minilm", help="PaperQA index name.")
    parser.add_argument("--top-n", type=int, default=3, help="Number of retrieval hits to print.")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=700,
        help="Maximum tokens for the generated answer in answer mode.",
    )
    parser.add_argument(
        "--answer-snippet-chars",
        type=int,
        default=550,
        help="Characters kept from each retrieved snippet before sending to the LLM.",
    )
    parser.add_argument(
        "--mode",
        choices=("retrieve", "answer"),
        default="retrieve",
        help="retrieve prints cited snippets without an LLM; answer uses an LLM to synthesize text.",
    )
    parser.add_argument(
        "--llm",
        default=default_llm(),
        help="LiteLLM model name for answer mode, e.g. openai/qwen-local.",
    )
    parser.add_argument(
        "--api-base",
        default=(
            os.environ.get("BIOAGENT_PAPERQA_API_BASE")
            or os.environ.get("OPENROUTER_BASE_URL")
            or "http://127.0.0.1:9/v1"
        ),
        help="OpenAI-compatible local LLM endpoint for answer mode.",
    )
    parser.add_argument(
        "--api-key",
        default=(
            os.environ.get("BIOAGENT_PAPERQA_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("BIOAGENT_LLM_API_KEY")
            or "sk-local-disabled"
        ),
        help="API key for the local OpenAI-compatible endpoint.",
    )
    return parser.parse_args()


def build_settings(args: argparse.Namespace) -> Settings:
    papers = Path(args.papers).resolve()
    manifest = Path(args.manifest).resolve()
    index_dir = Path(args.index_dir).resolve()

    llm_config = {
        "model_list": [
            {
                "model_name": args.llm,
                "litellm_params": {
                    "model": args.llm,
                    "api_base": args.api_base,
                    "api_key": args.api_key,
                    "temperature": 0.1,
                },
            }
        ]
    }

    return Settings(
        llm=args.llm,
        llm_config=llm_config,
        summary_llm=args.llm,
        summary_llm_config=llm_config,
        embedding=args.embedding,
        parsing=ParsingSettings(
            use_doc_details=False,
            multimodal=MultimodalOptions.OFF,
        ),
        agent=AgentSettings(
            index=IndexSettings(
                name=args.name,
                paper_directory=str(papers),
                manifest_file=str(manifest),
                index_directory=str(index_dir),
                recurse_subdirectories=False,
                concurrency=2,
                batch_size=1,
                sync_with_paper_directory=True,
            )
        ),
    )


def compact_text(text: str, limit: int = 700) -> str:
    cleaned = " ".join(str(text).split())
    return cleaned[:limit] + ("..." if len(cleaned) > limit else "")


def load_manifest_citations(manifest: str | Path) -> dict[str, str]:
    citations: dict[str, str] = {}
    with Path(manifest).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            citation = row.get("citation", "")
            if row.get("doc_id"):
                citations[str(row["doc_id"]).strip()] = citation
            if row.get("docname"):
                citations[str(row["docname"]).removeprefix("PMID").strip()] = citation
            if row.get("file_location"):
                citations[str(row["file_location"]).strip()] = citation
    return citations


def doc_to_record(doc: Any, manifest_citations: dict[str, str], filename: str | None = None) -> dict[str, Any]:
    dockey = str(getattr(doc, "dockey", "") or "")
    manifest_citation = manifest_citations.get(dockey) or manifest_citations.get(str(filename or ""))
    return {
        "dockey": dockey,
        "citation": (
            manifest_citation
            or getattr(doc, "formatted_citation", None)
            or getattr(doc, "citation", "")
        ),
    }


async def retrieve(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    index = await get_directory_index(settings=settings, build=False)
    hits = await index.query(args.question, top_n=args.top_n, keep_filenames=True)
    manifest_citations = load_manifest_citations(args.manifest)
    records: list[dict[str, Any]] = []
    for hit in hits:
        docs_obj, filename = hit if isinstance(hit, tuple) else (hit, None)
        docs = list(getattr(docs_obj, "docs", {}).values())
        texts = list(getattr(docs_obj, "texts", []) or [])
        records.append(
            {
                "file": filename,
                "papers": [doc_to_record(doc, manifest_citations, filename) for doc in docs],
                "snippets": [
                    {
                        "name": getattr(text, "name", ""),
                        "text": compact_text(getattr(text, "text", "")),
                    }
                    for text in texts[:3]
                ],
            }
        )
    return {"mode": "retrieve", "question": args.question, "hits": records}


def source_blocks(retrieval: dict[str, Any], snippet_chars: int) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    for hit in retrieval.get("hits", []):
        citation = ""
        papers = hit.get("papers") or []
        if papers:
            citation = papers[0].get("citation", "")
        for snippet in hit.get("snippets", [])[:1]:
            blocks.append(
                {
                    "id": f"S{len(blocks) + 1}",
                    "citation": citation,
                    "file": str(hit.get("file") or ""),
                    "text": compact_text(snippet.get("text", ""), limit=snippet_chars),
                }
            )
    return blocks


def openrouter_model_name(llm: str, api_base: str) -> str:
    """LiteLLM accepts openai/model, but OpenRouter's HTTP API wants model."""
    if "openrouter.ai" in api_base and llm.startswith("openai/"):
        return llm.removeprefix("openai/")
    return llm


def chat_completion(args: argparse.Namespace, sources: list[dict[str, str]]) -> str:
    api_base = args.api_base.rstrip("/")
    model = openrouter_model_name(args.llm, api_base)
    source_text = "\n\n".join(
        f"[{src['id']}]\nCitation: {src['citation']}\nFile: {src['file']}\nSnippet: {src['text']}"
        for src in sources
    )
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": args.max_tokens,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You answer biomedical literature questions using only the provided "
                    "retrieved paper snippets. If the snippets do not support an answer, "
                    "say that the retrieved pilot corpus is insufficient. Write citations "
                    "inside the prose using the citation metadata, for example: "
                    "(Dattani MT et al., Nat Genet 1998, PMID:9620767, DOI:10.1038/477). "
                    "Also include the source ID like [S1] after the citation so the user can "
                    "trace it back to the retrieved snippet. Do not invent citations."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {args.question}\n\n"
                    f"Retrieved sources:\n{source_text}\n\n"
                    "Write a concise answer with in-text paper citations. End with a short "
                    "'References' section listing only the sources actually used."
                ),
            },
        ],
    }
    req = urllib.request.Request(
        f"{api_base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {args.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "http://localhost/bioagent-prototype"),
            "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "BioAgent Prototype"),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM request failed: HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM request failed: {exc.reason}") from exc
    return str(data["choices"][0]["message"]["content"]).strip()


async def answer(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    retrieval = await retrieve(args, settings)
    sources = source_blocks(retrieval, args.answer_snippet_chars)
    if not sources:
        return {
            "mode": "answer",
            "question": args.question,
            "formatted_answer": "",
            "contexts": [],
            "error": "No retrieval hits from the PaperQA index.",
        }
    try:
        formatted_answer = chat_completion(args, sources)
    except RuntimeError as exc:
        return {
            "mode": "answer",
            "question": args.question,
            "formatted_answer": "",
            "contexts": sources,
            "error": str(exc),
        }
    return {
        "mode": "answer",
        "question": args.question,
        "formatted_answer": formatted_answer,
        "references": [
            {
                "id": src["id"],
                "citation": src["citation"],
                "file": src["file"],
            }
            for src in sources
        ],
        "contexts": sources,
    }


async def main() -> None:
    args = parse_args()
    settings = build_settings(args)
    if args.mode == "retrieve":
        result = await retrieve(args, settings)
    else:
        result = await answer(args, settings)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
