"""Estimated cost of one model call, from the price table in ``configs/llm_pricing.yaml``.

The table is configuration, not code: prices change more often than this module should, so an
operator edits the file (or points ``AISCIENTIST_LLM_PRICING_FILE`` at a copy outside the
deployment) and nothing is redeployed. The file is re-read when its modification time changes.

A call is priced when it is recorded and the estimate is stored with it, so a price change applies
to new calls and leaves the history as it was billed. A model the table does not list gets no
estimate (``None``) rather than a guess; its tokens are still counted.

These are ESTIMATES from AiScientist's own record of its requests. The provider's console is the
authoritative bill: it also sees the same key used outside AiScientist.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_FILE = _ROOT / "configs" / "llm_pricing.yaml"
_RATE_KEYS = ("input", "output", "cache_write", "cache_write_1h", "cache_read")

_lock = threading.Lock()
_cache: dict[str, Any] = {"path": None, "mtime": None, "table": None}


def pricing_file() -> Path:
    raw = os.environ.get("AISCIENTIST_LLM_PRICING_FILE", "").strip()
    return Path(raw) if raw else _DEFAULT_FILE


def _parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        import yaml
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("the pricing file must hold a mapping")
    rows = []
    for row in data.get("models") or []:
        if not isinstance(row, dict) or not str(row.get("match") or "").strip():
            continue
        rates = {k: float(row[k]) for k in _RATE_KEYS if row.get(k) is not None}
        if "input" not in rates or "output" not in rates:
            continue
        long_ctx = row.get("long_context")
        if isinstance(long_ctx, dict) and long_ctx.get("above_input_tokens"):
            long_ctx = {"above_input_tokens": int(long_ctx["above_input_tokens"]),
                        **{k: float(long_ctx[k]) for k in _RATE_KEYS if long_ctx.get(k) is not None}}
        else:
            long_ctx = None
        rows.append({"match": str(row["match"]).strip().lower(), **rates, "long_context": long_ctx})
    rows.sort(key=lambda r: len(r["match"]), reverse=True)   # longest prefix first
    return {"as_of": str(data.get("as_of") or ""), "source": str(data.get("source") or ""),
            "currency": str(data.get("currency") or "USD"), "models": rows}


def table() -> dict:
    """The parsed table (re-read when the file changes). An unreadable file yields an empty table
    and a printed reason: a broken price file must not stop a run from being recorded."""
    path = pricing_file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    with _lock:
        if _cache["table"] is not None and _cache["path"] == path and _cache["mtime"] == mtime:
            return _cache["table"]
        try:
            parsed = _parse(path) if mtime is not None else {"as_of": "", "source": "", "models": []}
        except Exception as exc:  # noqa: BLE001 - see the docstring
            print(f"[llm-pricing] {path} not used: {exc}")
            parsed = {"as_of": "", "source": "", "models": [], "error": str(exc)}
        _cache.update(path=path, mtime=mtime, table=parsed)
        return parsed


def normalize_model(model: str | None) -> str:
    """``anthropic/claude-opus-4.8`` (OpenRouter), ``anthropic.claude-opus-5-5`` (Bedrock) and
    ``claude-opus-5-5`` are the same model for pricing."""
    m = (model or "").strip().lower()
    m = m.rsplit("/", 1)[-1]
    m = m.removeprefix("anthropic.")
    return re.sub(r"(?<=\d)\.(?=\d)", "-", m)


def rates_for(model: str | None) -> dict | None:
    m = normalize_model(model)
    if not m:
        return None
    return next((r for r in table()["models"] if m.startswith(r["match"])), None)


def estimate(model: str | None, *, input_tokens: int, output_tokens: int,
             cache_write_tokens: int = 0, cache_write_1h_tokens: int = 0,
             cache_read_tokens: int = 0) -> float | None:
    """USD for one call, or None when the table has no price for ``model``.

    ``cache_write_tokens`` are 5-minute writes; ``cache_write_1h_tokens`` 1-hour writes (a subset
    the caller has already taken out of the 5-minute count)."""
    rates = rates_for(model)
    if rates is None:
        return None
    prompt = input_tokens + cache_read_tokens + cache_write_tokens + cache_write_1h_tokens
    long_ctx = rates.get("long_context")
    if long_ctx and prompt > long_ctx["above_input_tokens"]:
        rates = {**rates, **{k: v for k, v in long_ctx.items() if k in _RATE_KEYS}}
    inp = rates["input"]
    cost = (input_tokens * inp
            + output_tokens * rates["output"]
            + cache_write_tokens * rates.get("cache_write", inp * 1.25)
            + cache_write_1h_tokens * rates.get("cache_write_1h", inp * 2.0)
            + cache_read_tokens * rates.get("cache_read", inp * 0.1)) / 1_000_000
    return round(cost, 8)


def info() -> dict:
    """What the usage page says about the prices it used."""
    t = table()
    return {"as_of": t.get("as_of") or None, "source": t.get("source") or None,
            "models": [r["match"] for r in t.get("models") or []],
            "error": t.get("error")}
