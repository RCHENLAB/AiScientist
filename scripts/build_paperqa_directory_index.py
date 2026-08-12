"""Build a PaperQA directory index for a local PDF corpus.

This script is intentionally conservative for the RetiGene pilot:
- it uses a local sentence-transformers embedding model;
- it reads a manifest with citation/title/doi so PaperQA does not need to infer
  citation metadata with an LLM;
- it points any accidental LLM call at a local dead endpoint, so a misconfigured
  run fails locally instead of falling back to a cloud model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from paperqa import Settings
from paperqa.agents.search import get_directory_index
from paperqa.settings import AgentSettings, IndexSettings, MultimodalOptions, ParsingSettings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a PaperQA index for a PDF directory.")
    parser.add_argument("--papers", required=True, help="Directory containing PDFs.")
    parser.add_argument("--manifest", required=True, help="PaperQA manifest CSV.")
    parser.add_argument("--index-dir", required=True, help="Directory where PaperQA stores the index.")
    parser.add_argument(
        "--embedding",
        default=os.environ.get("BIOAGENT_PAPERQA_EMBEDDING", "st-NeuML/pubmedbert-base-embeddings"),
        help="Local PaperQA embedding model, usually an st- sentence-transformers model. "
        "Default = biomedical PubMedBERT; MUST match tools/paperqa_search.py's embedding.",
    )
    parser.add_argument("--name", default="retigene_full_pubmedbert", help="PaperQA index name.")
    parser.add_argument("--concurrency", type=int, default=2, help="PDF indexing concurrency.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    papers = Path(args.papers).resolve()
    manifest = Path(args.manifest).resolve()
    index_dir = Path(args.index_dir).resolve()
    index_dir.mkdir(parents=True, exist_ok=True)

    dead_local_llm_config = {
        "model_list": [
            {
                "model_name": "openai/local-no-llm",
                "litellm_params": {
                    "model": "openai/local-no-llm",
                    "api_base": "http://127.0.0.1:9/v1",
                    "api_key": "sk-local-disabled",
                },
            }
        ]
    }

    settings = Settings(
        llm="openai/local-no-llm",
        llm_config=dead_local_llm_config,
        summary_llm="openai/local-no-llm",
        summary_llm_config=dead_local_llm_config,
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
                concurrency=args.concurrency,
                batch_size=1,
                sync_with_paper_directory=True,
            )
        ),
    )

    index = await get_directory_index(settings=settings, build=True)
    indexed_files = await index.index_files
    report = {
        "papers": str(papers),
        "manifest": str(manifest),
        "index_dir": str(index_dir),
        "index_name": args.name,
        "embedding": args.embedding,
        "indexed_file_count": len(indexed_files),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
