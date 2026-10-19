"""Token usage and estimated cost (RCHENLAB/AiScientist issue 8, Part B).

Offline: SQLite, a TestClient, no provider. Covers the price table, what one recorded call holds,
the usage page and its CSV, a chat's and a run's totals, the budget (thresholds, projection, hard
limit), the admin overview, who can see what, and what deleting an account removes.
"""

from __future__ import annotations

import datetime as dt
import importlib

import pytest

pytest.importorskip("bcrypt")
pytest.importorskip("itsdangerous")
pytest.importorskip("sqlalchemy")


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'u.db').as_posix()}")
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AISCIENTIST_ADMIN_USER", "root")
    monkeypatch.setenv("AISCIENTIST_ADMIN_PASSWORD", "rootpass1")
    monkeypatch.setenv("AISCIENTIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AISCIENTIST_LLM_PRICING_FILE", raising=False)
    from aiscientist.gateway import auth, auth_routes, db, llm_usage, models
    importlib.reload(db)
    importlib.reload(models)
    importlib.reload(auth)
    importlib.reload(auth_routes)
    importlib.reload(llm_usage)
    db.reset(f"sqlite:///{(tmp_path / 'u.db').as_posix()}")
    db.init_db()
    auth_routes.ensure_bootstrap_admin()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(llm_usage.router)
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": "root", "password": "rootpass1"}).status_code == 200
    assert client.post("/api/admin/users", json={"username": "alice", "password": "alicepw1",
                                                 "role": "user"}).status_code == 200
    with db.session_scope() as s:
        from sqlalchemy import select
        ids = dict(s.execute(select(models.User.username, models.User.id)).all())
    return client, auth_routes, llm_usage, db, models, ids


def _as(client, user, pw):
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200


def _claude_usage(uncached=1000, write=2000, read=10_000, out=500, model="claude-opus-5-5"):
    # What anthropic_client._usage hands the recorder.
    prompt = uncached + write + read
    return {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out,
            "input_tokens": uncached, "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read, "cache_creation_1h_input_tokens": 0, "model": model}


def _record(routes, uid, **kw):
    base = dict(run_id=None, user_id=uid, role="reason", model="claude-opus-5-5",
                endpoint="claude-opus-5-5 @ Anthropic", remote=True, usage=_claude_usage(),
                max_tokens=None, duration_ms=10, provider="anthropic")
    base.update(kw)
    routes.record_llm_call(**base)


# --- the price table ------------------------------------------------------------------------------


def test_the_price_table_prices_every_token_class_and_picks_the_longest_prefix(monkeypatch):
    monkeypatch.delenv("AISCIENTIST_LLM_PRICING_FILE", raising=False)
    from aiscientist.gateway import llm_pricing
    # Opus 5.5: $4 in, $20 out, $5 5-minute writes, $0.20 reads per million.
    got = llm_pricing.estimate("claude-opus-5-5", input_tokens=1_000_000, output_tokens=1_000_000,
                               cache_write_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert got == pytest.approx(4 + 20 + 5 + 0.20)
    # Not priced as its prefix "claude-opus-5" ($5 / $25).
    assert llm_pricing.estimate("claude-opus-5", input_tokens=1_000_000, output_tokens=0) == pytest.approx(5)
    # OpenRouter and Bedrock spellings are the same model.
    assert llm_pricing.estimate("anthropic/claude-opus-4.8", input_tokens=1_000_000, output_tokens=0) == pytest.approx(5)
    assert llm_pricing.estimate("anthropic.claude-sonnet-5-5", input_tokens=0, output_tokens=1_000_000) == pytest.approx(10)
    # Sonnet 5.5 reads its cache at 0.05x ($0.10), Sonnet 5 at 0.1x; Opus 4 / 4.1 cost three times Opus 4.5+.
    assert llm_pricing.estimate("claude-sonnet-5-5", input_tokens=0, output_tokens=0,
                                cache_read_tokens=1_000_000) == pytest.approx(0.10)
    assert llm_pricing.estimate("claude-sonnet-5", input_tokens=0, output_tokens=0,
                                cache_read_tokens=1_000_000) == pytest.approx(0.20)
    assert llm_pricing.estimate("claude-opus-4-1", input_tokens=1_000_000, output_tokens=0) == pytest.approx(15)
    assert llm_pricing.estimate("claude-opus-4-6", input_tokens=1_000_000, output_tokens=0) == pytest.approx(5)
    # A model the table does not list gets no number, not a guess.
    assert llm_pricing.estimate("qwen/qwen3.6-35b", input_tokens=10, output_tokens=10) is None


def test_haiku_switches_to_its_long_context_prices_above_100k_prompt_tokens():
    from aiscientist.gateway import llm_pricing
    short = llm_pricing.estimate("claude-haiku-5-5", input_tokens=100_000, output_tokens=0)
    long = llm_pricing.estimate("claude-haiku-5-5", input_tokens=100_001, output_tokens=0)
    assert short == pytest.approx(0.01) and long == pytest.approx(100_001 * 0.5 / 1e6)


def test_the_price_table_is_configuration(tmp_path, monkeypatch):
    f = tmp_path / "prices.yaml"
    f.write_text("as_of: '2030-01-01'\nmodels:\n  - match: claude-opus-5-5\n    input: 1\n    output: 2\n")
    monkeypatch.setenv("AISCIENTIST_LLM_PRICING_FILE", str(f))
    from aiscientist.gateway import llm_pricing
    assert llm_pricing.estimate("claude-opus-5-5", input_tokens=1_000_000, output_tokens=1_000_000) == pytest.approx(3)
    assert llm_pricing.info()["as_of"] == "2030-01-01"


# --- one recorded call ----------------------------------------------------------------------------


def test_a_claude_call_records_each_token_class_its_chat_step_and_an_estimate(ctx):
    client, routes, usage, db, models, ids = ctx
    _record(routes, ids["alice"], run_id="r1", conversation_id="42", step="Step 2: Cluster")
    with db.session_scope() as s:
        row = s.query(models.LlmCall).one()
    assert (row.input_tokens, row.cache_write_tokens, row.cache_read_tokens, row.completion_tokens) == \
        (1000, 2000, 10_000, 500)
    assert row.prompt_tokens == 13_000 and row.conversation_id == "42" and row.step == "Step 2: Cluster"
    assert row.provider == "anthropic" and row.status == "ok" and row.cost_usd is None
    want = (1000 * 4 + 2000 * 5 + 10_000 * 0.2 + 500 * 20) / 1e6
    assert row.est_cost_usd == pytest.approx(want)


def test_a_failed_call_is_recorded_with_its_cause_and_no_cost(ctx):
    client, routes, usage, db, models, ids = ctx
    _record(routes, ids["alice"], usage=None, error_type="credit")
    with db.session_scope() as s:
        row = s.query(models.LlmCall).one()
    assert row.status == "error" and row.error_type == "credit" and row.total_tokens == 0
    assert row.est_cost_usd == 0.0


def test_a_call_on_the_cluster_gpu_costs_the_user_nothing(ctx):
    client, routes, usage, db, models, ids = ctx
    _record(routes, ids["alice"], remote=False, provider="vllm", model="qwen3.8",
            usage={"prompt_tokens": 9000, "completion_tokens": 100, "total_tokens": 9100})
    with db.session_scope() as s:
        row = s.query(models.LlmCall).one()
    assert row.est_cost_usd == 0.0 and row.provider == "vllm" and row.input_tokens == 9000


def test_the_provider_figure_wins_over_the_estimate(ctx):
    client, routes, usage, db, models, ids = ctx
    _record(routes, ids["alice"], provider="openai",
            usage={"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.5,
                   "prompt_tokens_details": {"cached_tokens": 40}})
    with db.session_scope() as s:
        row = s.query(models.LlmCall).one()
        assert row.spend_usd == 0.5 and row.cache_read_tokens == 40 and row.input_tokens == 60


# --- the usage page -------------------------------------------------------------------------------


def test_the_usage_page_totals_breaks_down_and_says_it_is_an_estimate(ctx):
    client, routes, usage, db, models, ids = ctx
    _record(routes, ids["alice"], run_id="r1", conversation_id="1", step="Planning")
    _record(routes, ids["alice"], run_id="r1", conversation_id="1", step="Step 1: QC",
            model="claude-haiku-5-5", usage=_claude_usage(model="claude-haiku-5-5"))
    _record(routes, ids["alice"], conversation_id="2", step="Chat", usage=None, error_type="rate")
    _record(routes, ids["root"])                                   # someone else's call
    _as(client, "alice", "alicepw1")
    d = client.get("/api/usage?tz=America/Los_Angeles").json()
    assert d["totals"]["today"]["calls"] == 3 and d["totals"]["cycle"]["calls"] == 3
    assert d["totals"]["today"]["errors"] == 1
    assert {m["model"] for m in d["by_model"]} == {"claude-opus-5-5", "claude-haiku-5-5"}
    assert {c["conversation_id"] for c in d["by_chat"]} == {"1", "2"}
    assert d["by_run"][0]["run_id"] == "r1" and d["by_run"][0]["calls"] == 2
    assert len(d["errors"]) == 1 and d["errors"][0]["error_type"] == "rate"
    assert len(d["daily"]) == 30 and d["daily"][-1]["calls"] == 3
    assert "Claude Console" in d["note"] and d["console_url"].startswith("https://")
    assert d["pricing"]["as_of"]


def test_the_cluster_gpu_share_is_counted_apart_from_what_an_api_key_paid_for(ctx):
    # HPC3 bills the cluster GPU by the hour, not by token: the page counts API tokens only and
    # shows the cluster calls beside them, so every total and every row carries them apart.
    client, routes, usage, db, models, ids = ctx
    _record(routes, ids["alice"], run_id="r1", conversation_id="1")
    _record(routes, ids["alice"], run_id="r1", conversation_id="1", remote=False, provider="vllm",
            model="qwen3.8", usage={"prompt_tokens": 9000, "completion_tokens": 100, "total_tokens": 9100})
    _as(client, "alice", "alicepw1")
    d = client.get("/api/usage?tz=UTC").json()
    cyc = d["totals"]["cycle"]
    assert cyc["calls"] == 2 and cyc["cluster"]["calls"] == 1
    assert cyc["cluster"]["total_tokens"] == 9100 and cyc["cluster"]["input_tokens"] == 9000
    assert cyc["total_tokens"] - cyc["cluster"]["total_tokens"] == 13_500     # the Claude call alone
    rows = {m["model"]: m for m in d["by_model"]}
    assert rows["qwen3.8"]["cluster"]["calls"] == rows["qwen3.8"]["calls"] == 1
    assert rows["qwen3.8"]["spend_usd"] == 0 and rows["claude-opus-5-5"]["cluster"]["calls"] == 0
    assert d["by_run"][0]["cluster"]["calls"] == 1 and d["by_run"][0]["calls"] == 2
    assert client.get("/api/usage/run/r1").json()["cluster"]["output_tokens"] == 100


def test_the_chat_counter_sums_the_cluster_gpu_apart_and_its_live_push_agrees(ctx):
    # The chat header counter reads /api/usage/chat/{id} on load and the usage_update push after
    # each call: both sum in the database, and both carry the cluster share apart, as the page does.
    client, routes, usage, db, models, ids = ctx
    qwen = dict(remote=False, provider="vllm", model="qwen3.8",
                usage={"prompt_tokens": 9000, "completion_tokens": 100, "total_tokens": 9100})
    _record(routes, ids["alice"], run_id="r5", conversation_id="5")
    _record(routes, ids["alice"], run_id="r5", conversation_id="5", **qwen)
    _record(routes, ids["alice"], conversation_id="5", step="Chat", role="chat", **qwen)
    _record(routes, ids["root"], conversation_id="5", **qwen)            # another account's chat 5
    _record(routes, ids["alice"], conversation_id="6", step="Chat", role="chat", **qwen)
    _as(client, "alice", "alicepw1")
    chat = client.get("/api/usage/chat/5").json()
    assert chat["calls"] == 3 and chat["cluster"] == {"calls": 2, "total_tokens": 18_200}
    assert chat["total_tokens"] - chat["cluster"]["total_tokens"] == 13_500   # the Claude call alone
    assert chat["spend_usd"] > 0
    live = usage.live_totals(ids["alice"], "5", "r5")
    assert {"conversation_id": "5", **live["chat"]} == chat
    assert live["run"]["cluster"] == {"calls": 1, "total_tokens": 9100}
    run = client.get("/api/usage/run/r5").json()       # summed row by row, as the page does
    assert live["run"]["calls"] == run["calls"] and live["run"]["total_tokens"] == run["total_tokens"]
    assert run["cluster"]["calls"] == 1 and run["cluster"]["total_tokens"] == 9100
    # A chat entirely on the cluster GPU: every call and token is in the cluster share, at $0.
    only = client.get("/api/usage/chat/6").json()
    assert only == {"conversation_id": "6", "calls": 1, "total_tokens": 9100, "spend_usd": 0.0,
                    "unpriced_calls": 0, "cluster": {"calls": 1, "total_tokens": 9100}}
    assert usage.live_totals(ids["alice"], "6", None) == {"chat": {k: v for k, v in only.items()
                                                                   if k != "conversation_id"}}


def test_the_csv_export_has_one_line_per_call_of_the_user(ctx):
    client, routes, usage, db, models, ids = ctx
    _record(routes, ids["alice"], conversation_id="1")
    _record(routes, ids["alice"], conversation_id="1", usage=None, error_type="auth")
    _record(routes, ids["root"])
    _as(client, "alice", "alicepw1")
    res = client.get("/api/usage/export.csv?period=all")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    lines = res.text.strip().splitlines()
    assert lines[0].startswith("time_local,model,role,step") and len(lines) == 3
    assert ",error,auth," in lines[2]


def test_a_chat_and_a_run_have_their_own_totals_and_a_run_its_steps(ctx):
    client, routes, usage, db, models, ids = ctx
    _record(routes, ids["alice"], run_id="r9", conversation_id="7", step="Planning")
    _record(routes, ids["alice"], run_id="r9", conversation_id="7", step="Step 1: QC", role="scientist")
    _record(routes, ids["alice"], run_id="r9", conversation_id="7", step="Step 1: QC", role="critic")
    _record(routes, ids["alice"], conversation_id="7", step="Chat", role="chat")
    _as(client, "alice", "alicepw1")
    chat = client.get("/api/usage/chat/7").json()
    run = client.get("/api/usage/run/r9").json()
    assert chat["calls"] == 4 and run["calls"] == 3
    assert [s["step"] for s in run["by_step"]] == ["Planning", "Step 1: QC"]
    assert run["by_step"][1]["calls"] == 2
    assert {r["role"] for r in run["by_role"]} == {"reason", "scientist", "critic"}
    # The live counter's database sums agree with the page's.
    live = usage.live_totals(ids["alice"], "7", "r9")
    assert live["chat"]["calls"] == 4 and live["run"]["calls"] == 3
    assert live["chat"]["spend_usd"] == pytest.approx(chat["spend_usd"])
    assert live["run"]["total_tokens"] == run["total_tokens"]
    # Another account's run reads as empty.
    _as(client, "root", "rootpass1")
    assert client.get("/api/usage/run/r9").json()["calls"] == 0


# --- budget ---------------------------------------------------------------------------------------


def test_the_billing_cycle_follows_the_reset_day():
    from aiscientist.gateway import llm_usage
    assert llm_usage.billing_cycle(dt.date(2026, 10, 16), 1) == (dt.date(2026, 10, 1), dt.date(2026, 10, 31))
    assert llm_usage.billing_cycle(dt.date(2026, 10, 16), 20) == (dt.date(2026, 9, 20), dt.date(2026, 10, 19))
    assert llm_usage.billing_cycle(dt.date(2026, 1, 5), 20) == (dt.date(2025, 12, 20), dt.date(2026, 1, 19))
    # The 31st in a 30-day month is its last day.
    assert llm_usage.billing_cycle(dt.date(2026, 11, 30), 31) == (dt.date(2026, 11, 30), dt.date(2026, 12, 30))


def test_thresholds_show_once_per_cycle_and_the_projection_is_given(ctx):
    client, routes, usage, db, models, ids = ctx
    _as(client, "alice", "alicepw1")
    assert client.put("/api/usage/budget", json={"monthly_credit_usd": 0.05, "reset_day": 1,
                                                 "thresholds": [50, 80, 100]}).status_code == 200
    _record(routes, ids["alice"])          # ≈ $0.026 of $0.05
    s = client.get("/api/usage/summary").json()
    assert s["crossed"] == [50] and s["new_notices"] == [50]
    assert s["percent"] > 50 and s["remaining_usd"] > 0 and s["lasts_cycle"] is not None
    assert client.post("/api/usage/notices/ack", json={"thresholds": [50]}).status_code == 200
    assert client.get("/api/usage/summary").json()["new_notices"] == []
    _record(routes, ids["alice"])          # over 100 %
    s = client.get("/api/usage/summary").json()
    assert s["new_notices"] == [80, 100] and s["projected_exhaustion"] is not None


def test_a_bad_budget_is_refused(ctx):
    client, *_ = ctx
    _as(client, "alice", "alicepw1")
    assert client.put("/api/usage/budget", json={"reset_day": 40}).status_code == 400
    assert client.put("/api/usage/budget", json={"monthly_credit_usd": -1}).status_code == 400
    assert client.put("/api/usage/budget", json={"hard_limit_mode": "maybe"}).status_code == 400


def test_the_hard_limit_asks_or_blocks_once_the_cycle_spend_reaches_it(ctx):
    client, routes, usage, db, models, ids = ctx
    uid = ids["alice"]
    _as(client, "alice", "alicepw1")
    client.put("/api/usage/budget", json={"hard_limit_usd": 0.01, "hard_limit_mode": "confirm"})
    assert usage.budget_gate(uid) is None                       # nothing spent yet
    _record(routes, uid)
    gate = usage.budget_gate(uid)
    assert gate["mode"] == "confirm" and "limit" in gate["message"]
    client.put("/api/usage/budget", json={"hard_limit_usd": 0.01, "hard_limit_mode": "block"})
    assert usage.budget_gate(uid)["mode"] == "block"
    client.put("/api/usage/budget", json={"hard_limit_usd": None})
    assert usage.budget_gate(uid) is None


def test_typical_run_cost_comes_from_finished_runs_on_the_same_model(ctx):
    client, routes, usage, db, models, ids = ctx
    uid = ids["alice"]
    for rid in ("a1", "a2", "a3"):
        routes.record_run_start(uid, rid, "q", False)
        _record(routes, uid, run_id=rid)
    with db.session_scope() as s:
        for r in s.query(models.Run).all():
            r.status = "done"
        s.commit()
    est = usage.typical_run_cost(uid, "claude-opus-5-5")
    assert est["runs"] == 3 and est["median"] > 0
    assert usage.typical_run_cost(uid, "claude-sonnet-5-5") is None


# --- admin + privacy -------------------------------------------------------------------------------


def test_admins_see_usage_by_user_model_and_day_and_users_do_not(ctx):
    client, routes, usage, db, models, ids = ctx
    _record(routes, ids["alice"])
    _record(routes, ids["root"], model="claude-sonnet-5-5")
    d = client.get("/api/admin/usage").json()                  # still signed in as root
    assert {u["username"] for u in d["by_user"]} == {"alice", "root"}
    assert {m["model"] for m in d["by_model"]} == {"claude-opus-5-5", "claude-sonnet-5-5"}
    assert len(d["by_day"]) == 30 and sum(x["calls"] for x in d["by_day"]) == 2   # quiet days at 0
    assert d["totals"]["calls"] == 2
    flat = str({k: v for k, v in d.items() if k != "note"})
    assert "sk-" not in flat and "api_key" not in flat and "key_hint" not in flat
    _as(client, "alice", "alicepw1")
    assert client.get("/api/admin/usage").status_code == 403
    assert client.get("/api/usage").json()["totals"]["today"]["calls"] == 1


def test_usage_needs_a_signed_in_account(ctx):
    client, *_ = ctx
    client.post("/api/auth/logout")
    assert client.get("/api/usage").status_code == 401
    assert client.get("/api/usage/summary").status_code == 401


def test_deleting_an_account_removes_its_usage_budget_and_saved_keys(ctx, tmp_path):
    client, routes, usage, db, models, ids = ctx
    from aiscientist.gateway import llm_credentials
    importlib.reload(llm_credentials)
    uid = ids["alice"]
    _record(routes, uid)
    _as(client, "alice", "alicepw1")
    client.put("/api/usage/budget", json={"monthly_credit_usd": 100})
    cred = llm_credentials.create("alice", provider="anthropic", base_url="https://api.anthropic.com",
                                  model="claude-opus-5-5", api_key="sk-ant-example-0000")
    key_dir = tmp_path / "state" / "llm_creds" / "alice"
    assert (key_dir / f"{cred['id']}.key").is_file()
    (tmp_path / "state" / "ssh_creds" / "alice").mkdir(parents=True)
    (tmp_path / "state" / "ssh_creds" / "alice" / "index.json").write_text("[]")
    _as(client, "root", "rootpass1")
    assert client.delete(f"/api/admin/users/{uid}").status_code == 200
    assert not key_dir.exists() and not (tmp_path / "state" / "ssh_creds" / "alice").exists()
    with db.session_scope() as s:
        assert s.query(models.LlmCall).filter_by(user_id=uid).count() == 0
        assert s.get(models.UsageBudget, uid) is None


def test_a_key_folder_shared_by_two_accounts_survives_deleting_one(ctx, tmp_path):
    client, routes, usage, db, models, ids = ctx
    from aiscientist.gateway import llm_credentials
    importlib.reload(llm_credentials)
    assert client.post("/api/admin/users", json={"username": "a.b", "password": "abpw12",
                                                 "role": "user"}).status_code == 200
    assert client.post("/api/admin/users", json={"username": "ab", "password": "abpw12",
                                                 "role": "user"}).status_code == 200
    llm_credentials.create("ab", provider="custom", base_url="https://x/v1", model="m",
                           api_key="sk-example-shared")
    with db.session_scope() as s:
        dot = s.query(models.User).filter_by(username="a.b").one().id
    assert client.delete(f"/api/admin/users/{dot}").status_code == 200
    assert (tmp_path / "state" / "llm_creds" / "ab" / "index.json").is_file()
