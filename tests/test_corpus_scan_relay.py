"""The corpus scan on a user's own API model, and its pilot.

The job (``extract_haploinsufficient_genes``) never holds the key: it writes each prompt to a relay
folder, and the gateway (``gateway/llm_relay.ModelRelay``) calls the model, records the call and its
price, and writes the reply back. These tests run both halves against one local folder. The pilot
reads about 1% of the passages first, so a model that cannot do the job, or a scan that would cost
more than it may, stops after a handful of calls."""
from __future__ import annotations

import csv
import json
import re
import threading
import types
from pathlib import Path

import numpy as np
import pytest

from aiscientist.gateway import llm_relay
from aiscientist.gateway.errors import GatewayError
from aiscientist.tools.extract_haploinsufficient_genes import tool as hi

N_PAPERS = 40
REQUESTS = ["Heterozygous null alleles cause dominant retinal dystrophy through haploinsufficiency.",
            "One functional copy of the gene is not enough for the retina.",
            "Haploinsufficiency rather than a dominant-negative effect causes the disease."]


def _sentence(i: int) -> str:
    return (f"We conclude that haploinsufficiency of GENE{i} causes the dominant retinal dystrophy in this "
            "family, as the heterozygous null allele produces no protein.")


@pytest.fixture(autouse=True)
def _fast_relay(monkeypatch):
    monkeypatch.setattr(hi, "_RELAY_POLL_S", 0.01)


def _corpus(tmp_path: Path) -> Path:
    root = tmp_path / "retigene"
    cache = root / "mechanism_cache"
    cache.mkdir(parents=True)
    with (cache / "sentences.jsonl").open("w") as sf, (cache / "chunks.jsonl").open("w") as cf:
        for i in range(N_PAPERS):
            pmid = str(10000000 + i)
            sents = [f"Retinal dystrophies are clinically heterogeneous in cohort {i}.", _sentence(i),
                     "The proband's relatives were examined in the clinic."]
            cf.write(json.dumps({"pmid": pmid, "chunk": 0, "page": 1, "text": " ".join(sents)}) + "\n")
            for k, s in enumerate(sents):
                sf.write(json.dumps({"sid": f"{pmid}:0:{k}", "pmid": pmid, "chunk": 0, "sent": k, "page": 1,
                                     "text": s, "prev": sents[k - 1] if k else "",
                                     "next": sents[k + 1] if k + 1 < len(sents) else ""}) + "\n")
    (root / "papers").mkdir()
    with (root / "paperqa_manifest.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file_location", "title", "doc_id"])
        for i in range(N_PAPERS):
            w.writerow([f"{10000000 + i}.pdf", f"A family with retinal dystrophy number {i}", str(10000000 + i)])
    with (root / "hgnc_complete_set.txt").open("w") as fh:
        fh.write("hgnc_id\tsymbol\talias_symbol\tprev_symbol\n")
        for i in range(N_PAPERS):
            fh.write(f"HGNC:{i}\tGENE{i}\t\t\n")
    return root


def _embed(texts):
    return np.array([[1.0 if "haploinsufficiency" in t.lower() else 0.0, 0.1] for t in texts], dtype="float32")


def _ctx(tmp_path: Path, monkeypatch, root: Path, relay: Path | None = None):
    monkeypatch.setenv("AISCIENTIST_PAPERQA_PAPERS", str(root / "papers"))
    monkeypatch.setenv("AISCIENTIST_PAPERQA_MANIFEST", str(root / "paperqa_manifest.csv"))
    ws = tmp_path / "run"
    ws.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(model="claude-haiku-5-5", llm_base_url=None, tunnel_port=None, workspace=ws,
                                 llm_relay_dir=str(relay) if relay else None)


def _model(calls: list[str] | None = None, broken: bool = False):
    """``messages -> text``: writes the requests, and reads a passage the way the real model should."""
    def model(messages):
        system, user = messages[0]["content"], messages[1]["content"]
        if system == hi.GENE_REQUESTS_SYSTEM:
            genes = [line.split(" — ")[0] for line in user.splitlines()]
            return json.dumps({"requests": {g: f"Heterozygous loss of {g} causes retinal dystrophy." for g in genes}})
        if system.startswith("You are choosing exact search words"):
            return json.dumps({"terms": ["haploinsufficiency", "one functional copy"]})
        if system != hi.JUDGE_SYSTEM:
            return json.dumps({"requests": REQUESTS})
        pmid = re.search(r"PMID (\d+)", user).group(1)
        if calls is not None:
            calls.append(pmid)
        if broken:
            return "I cannot answer in JSON today."
        i = int(pmid) - 10000000
        return json.dumps({"items": [{"gene": f"GENE{i}", "mechanism_class": "haploinsufficiency", "explicit": True,
                                      "negated": False, "human_retinal": True, "phenotype": "retinal dystrophy",
                                      "sentence": 2, "quote": _sentence(i)}]})
    return model


def _relay(relay_dir: Path, model, *, price_per_call: float = 0.001, cap: float | None = None,
           fail=None, records: list | None = None) -> llm_relay.ModelRelay:
    def call(messages, max_tokens):
        if fail is not None:
            fail(messages)
        return model(messages), {"prompt_tokens": 1000, "completion_tokens": 100, "model": "claude-haiku-5-5"}

    def record(usage, duration_ms, error_type):
        if records is not None:
            records.append((usage, error_type))

    return llm_relay.ModelRelay(llm_relay.LocalFS, str(relay_dir), call, price=lambda u: price_per_call,
                                record=record, max_cost_usd=cap, concurrency=4, poll_s=0.01,
                                sleep=lambda s: None).start()


def _run(ctx, args=None, judge_fn=None):
    return hi.run_extract(args or {}, ctx, judge_fn=judge_fn, embed_fn=_embed, log=lambda s: None)


# --- the pilot --------------------------------------------------------------------------------------

def test_pilot_is_about_one_percent_spread_over_the_passages():
    ps = [hi.Passage(f"p{i}:0", f"p{i}", 0, 1, []) for i in range(857)]
    pilot = hi.pilot_sample(ps, 0.01)
    assert len(pilot) == 9 and pilot[0] is ps[0] and pilot[-1].pmid > "p7"          # 1% of 857, spread
    assert len(hi.pilot_sample(ps[:40], 0.01)) == 5                                  # at least 5
    assert len(hi.pilot_sample(ps * 10, 0.01)) == 25                                  # at most 25
    assert hi.pilot_sample(ps, 0) == [] and hi.pilot_sample([], 0.01) == []


def test_a_model_that_cannot_answer_is_stopped_by_the_pilot(tmp_path, monkeypatch):
    monkeypatch.setattr(hi.time, "sleep", lambda s: None)
    root = _corpus(tmp_path)
    calls: list[str] = []
    res = _run(_ctx(tmp_path, monkeypatch, root), judge_fn=_model(calls, broken=True))
    assert res["status"] == "failed" and res["pilot_failed"]
    assert "pilot failed" in res["error"] and "not the JSON asked for" in res["error"]
    assert res["n_passages"] == N_PAPERS and res["pilot"]["passages"] == 5 and res["pilot"]["read"] == 0
    # Only the pilot's 5 passages were sent (3 attempts each); the other 35 never were.
    assert len(set(calls)) == 5 and len(calls) == 15


def test_a_healthy_model_reads_the_pilot_then_the_rest(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    calls: list[str] = []
    res = _run(_ctx(tmp_path, monkeypatch, root), judge_fn=_model(calls))
    assert res["status"] == "ok" and res["n_genes"] == N_PAPERS
    assert res["pilot"] == {"passages": 5, "read": 5, "calls": 0, "cost_usd": 0.0}
    assert sorted(calls) == sorted(set(calls)) and len(calls) == N_PAPERS           # each passage once


# --- the relay ---------------------------------------------------------------------------------------

def test_the_relay_makes_the_calls_records_and_prices_them(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    relay_dir = tmp_path / "relay"
    records: list = []
    relay = _relay(relay_dir, _model(), records=records)
    try:
        res = _run(_ctx(tmp_path, monkeypatch, root, relay=relay_dir))
    finally:
        stats = relay.stop()
    assert res["status"] == "ok", res
    assert res["n_genes"] == N_PAPERS
    # requests (1) + search words (1) + passages (40) + round-two request batches (2 of 25 genes); round
    # two finds nothing new.
    assert stats.calls == res["api_usage"]["calls"] == 2 + N_PAPERS + 2
    assert res["api_usage"]["cost_usd"] == pytest.approx(stats.calls * 0.001)
    assert res["api_usage"]["input_tokens"] == 1000 * stats.calls
    assert len(records) == stats.calls and all(err is None for _, err in records)
    assert res["pilot"]["calls"] == 5 and res["pilot"]["estimated_total_usd"] == pytest.approx(0.001 * 2 * N_PAPERS)
    # Nothing is left in the relay folder.
    assert not list((relay_dir / "req").glob("*.json")) and not list((relay_dir / "resp").glob("*.json"))


def test_the_spending_cap_stops_the_scan_and_keeps_what_was_read(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    relay_dir = tmp_path / "relay"
    relay = _relay(relay_dir, _model(), price_per_call=0.01, cap=0.035)
    try:
        res = _run(_ctx(tmp_path, monkeypatch, root, relay=relay_dir), args={"pilot_fraction": 0})
    finally:
        stats = relay.stop()
    assert res["status"] == "failed" and res["stopped"] and not res.get("model_unreachable")
    assert "spending cap" in res["error"] and "$0.04" in res["error"].replace("$0.035", "$0.04")
    assert 4 <= stats.calls <= 4 + 4          # the cap is checked before each call; 4 may be in flight
    assert 0 < res["n_judged"] < N_PAPERS
    # A rerun with a higher cap reads only the rest.
    calls: list[str] = []
    relay2 = _relay(relay_dir, _model(calls), price_per_call=0.01)
    try:
        res2 = _run(_ctx(tmp_path, monkeypatch, root, relay=relay_dir), args={"pilot_fraction": 0})
    finally:
        relay2.stop()
    assert res2["status"] == "ok" and len(calls) == N_PAPERS - res["n_judged"]


def test_the_pilot_stops_a_scan_that_would_cost_more_than_it_may(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    relay_dir = tmp_path / "relay"
    calls: list[str] = []
    relay = _relay(relay_dir, _model(calls), price_per_call=0.01)
    try:
        res = _run(_ctx(tmp_path, monkeypatch, root, relay=relay_dir), args={"max_cost_usd": 0.5})
    finally:
        relay.stop()
    # 5 pilot calls at $0.01 project 0.01 x 2 x 40 = $0.80 > $0.50: stop before the other 35.
    assert res["status"] == "failed" and res["over_budget"] and res["stopped"]
    assert res["pilot"]["estimated_total_usd"] == pytest.approx(0.8)
    assert len(calls) == 5 and "$0.80" in res["error"]


def test_a_refused_key_stops_the_scan_at_once(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    relay_dir = tmp_path / "relay"
    records: list = []

    def refuse(messages):
        raise GatewayError("Anthropic rejected this API key (unauthorized).", stage="anthropic_auth")

    relay = _relay(relay_dir, _model(), fail=refuse, records=records)
    try:
        res = _run(_ctx(tmp_path, monkeypatch, root, relay=relay_dir))
    finally:
        stats = relay.stop()
    assert res["status"] == "failed" and res["stopped"] and "unauthorized" in res["error"]
    assert stats.calls == 0 and stats.failed == 1 and records == [(None, "auth")]


def test_a_rate_limit_is_waited_out_on_the_gateway(tmp_path, monkeypatch):
    root = _corpus(tmp_path)
    relay_dir = tmp_path / "relay"
    state = {"n": 0}
    lock = threading.Lock()

    def flaky(messages):
        with lock:
            state["n"] += 1
            first = state["n"] == 1
        if first:
            raise GatewayError("Anthropic is rate-limiting this key.", stage="anthropic_rate")

    records: list = []
    relay = _relay(relay_dir, _model(), fail=flaky, records=records)
    try:
        res = _run(_ctx(tmp_path, monkeypatch, root, relay=relay_dir))
    finally:
        stats = relay.stop()
    assert res["status"] == "ok" and stats.failed == 0
    assert records[0] == (None, "rate") and all(err is None for _, err in records[1:])
