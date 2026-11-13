"""Legacy inspection tool factory and compatibility imports.

Upload/describe helpers live independently in tools._lib.dataset_inspection so the
platform can retain them when this model-facing tool is retired after website validation.
"""
from __future__ import annotations

import os
from typing import Any
from .._lib import dataset_inspection as _inspection
from .._lib.dataset_inspection import (
    ChatFn,
    describe_dataset as describe_dataset,
    inspect_dataset as inspect_dataset,
    peek_dataset as peek_dataset,
)


def __getattr__(name: str) -> Any:
    # Preserve imports of existing helper names while the legacy tool remains supported.
    return getattr(_inspection, name)


def make_inspect_dataset_tool() -> Any:
    """The ``inspect_dataset`` tool: skim ANY uploaded file (known type or not) and report a
    structured "what is this". Runs IN-PROCESS on the gateway (no HPC3): reads a bounded head and,
    when this session has a served model, describes it via ``ctx.tunnel_port`` (``think=False``,
    load-bearing — the served Qwen3.6 is a reasoning model and a bounded call with thinking ON can
    return empty content). Follows the ``make_hpo_mapping_tool`` template."""
    from ..sdk import HarnessTool, confine_read, session_chat_fn

    def _exec(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        decisions = getattr(ctx, "decisions", None) or {}
        # The run's bound dataset is authoritative; an explicit `path` is only a fallback. This tool
        # runs HERE, on the gateway, so a model-typed path usually names the HPC3 copy (/dfs3b/...),
        # which does not exist on this host: honouring it first made the tool report the run's own
        # dataset "unreadable" (28 times in 12 prod runs). Use the model's path only when nothing is
        # bound, or when it is a real file on this host that differs from the bound one — and only
        # inside this run's files, its bound datasets or the owner's uploads (confine_read).
        bound = str(decisions.get("dataset_path") or "").strip()
        asked = str(args.get("path") or "").strip()
        path = bound
        if asked:
            confined, why = confine_read(asked, ctx)
            if confined is not None:
                if confined.is_file() or not bound:
                    path = str(confined)
            elif not bound or os.path.lexists(asked):
                return {"status": "error", "tool": "inspect_dataset", "error": why}
        if not path:
            return {"status": "error", "tool": "inspect_dataset",
                    "error": "inspect_dataset needs a file path — pass `path`, or bind a dataset to "
                             "the run. It does not guess."}

        # Bound only when this session serves a model (None otherwise -> deterministic peek only).
        # think=False is LOAD-BEARING (same reason as map_phenotype_to_hpo): a bounded call to
        # the served REASONING model can spend its whole budget on the thinking trace and
        # return empty content. Triage is a structured-JSON task needing no chain-of-thought.
        chat_fn: "ChatFn | None" = session_chat_fn(ctx, max_tokens=1200, timeout=90.0, think=False)

        return inspect_dataset(path, chat_fn=chat_fn)

    return HarnessTool(
        "inspect_dataset",
        "Skim an uploaded data file — ANY type, known extension or not — and return a structured "
        "'what is this': format, genome assembly and sample id(s) for a VCF, the group tree for an "
        ".h5ad/HDF5 matrix, columns for a table, or a best-effort descriptor for an unknown/binary "
        "file. Use this FIRST when you are unsure what a bound dataset actually is, instead of "
        "assuming from the filename. Reads only a bounded head; never loads the whole file. Returns "
        "`peek` (deterministic evidence) and `description` (file_kind, likely_modality, "
        "one_line_summary, confidence) — do not invent facts beyond what it reports.",
        {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "server-side path of the file to skim: a file of this run, a "
                                    "dataset bound to it, or one of your uploads. Omit to use the "
                                    "dataset already bound to this run."}}},
        _exec,
        reads_private_data=True, category="qc",
    )
