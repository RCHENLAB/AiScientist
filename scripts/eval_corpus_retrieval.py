"""Operator entry point; scientific implementation lives in the configured MCP checkout."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _literature_runtime import operator_module  # noqa: E402


def main(argv=None):
    return operator_module("eval_corpus_retrieval").main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
