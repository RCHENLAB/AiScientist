"""The console's Account page (its API usage tab and its Password tab) and the chat header's usage
counter, run under node.

The page is drawn by plain functions in ``frontend/console/app.js`` that return HTML (the summary
row, the budget tile, the daily chart, the breakdown lists) or a verdict (the password check). This
runs them under node with the page's other helpers stubbed, as ``test_console_plan_card_js.py``
does, and pins what a person reads there: the spend and tokens that an API key paid for, with the
cluster GPU apart and free (HPC3 bills it by the hour, not by token); where the spend stands
against the budget; one breakdown at a time with each row's cost and share; a message instead of an
empty table; a new password typed twice; and a chat counter that follows the same rule. Skipped
where node is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"

ESCAPE = r'(s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")'

HARNESS = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const sandbox = { escapeHtml: __ESCAPE__, fmtDate: (iso) => String(iso), chatTitle: () => "" };
const api = vm.runInNewContext(src + "\n;({ usageSummaryHtml, budgetTileHtml, usageChart, usageList, usageBreakdownHtml," +
  " usagePeriodTotalHtml, usageCycleText, passwordProblem, apiPart, fmtUsd, fmtDay, localIsoDay, accountInfoHtml," +
  " chatUsageText, usageTitle })", sandbox);
const tok = (o) => ({ input_tokens: 0, cache_write_tokens: 0, cache_read_tokens: 0, output_tokens: 0, total_tokens: 0, ...o });
const agg = (o = {}) => ({ calls: 0, errors: 0, spend_usd: 0, unpriced_calls: 0, ...tok(o), cluster: { calls: 0, ...tok({}) }, ...o });
const claude = agg({ calls: 86, errors: 1, spend_usd: 2.47, input_tokens: 187000, cache_write_tokens: 228000,
  cache_read_tokens: 1900000, output_tokens: 136000, total_tokens: 2451000 });
const cycle = { ...claude, calls: 87, input_tokens: 196000, output_tokens: 136900, total_tokens: 2460900,
  cluster: { calls: 1, ...tok({ input_tokens: 9000, output_tokens: 900, total_tokens: 9900 }) } };
const span = { start: "2026-10-01", end: "2026-10-31", days_elapsed: 17.5, days_total: 31 };
const noBudget = { cycle: span, spent_usd: 2.47, budget: null };
const budget = (over) => ({ cycle: span, spent_usd: 2.47, percent: 49.4, remaining_usd: 2.53, lasts_cycle: true,
  budget: { monthly_credit_usd: 5, thresholds: [50, 80, 100], hard_limit_usd: 4, hard_limit_mode: "block" }, ...over });
const totals = { today: agg({ spend_usd: 0.18 }), last_7_days: agg({ spend_usd: 0.85 }), cycle, period: cycle };
const out = {};
out.summary = api.usageSummaryHtml({ totals, budget: noBudget });
out.onTrack = api.budgetTileHtml(budget({}));
out.nearly = api.budgetTileHtml(budget({ percent: 84, remaining_usd: 0.8 }));
out.early = api.budgetTileHtml(budget({ percent: 60, remaining_usd: 2, lasts_cycle: false, projected_exhaustion: "2026-10-25" }));
out.over = api.budgetTileHtml(budget({ percent: 112, remaining_usd: -0.6, lasts_cycle: false, hard_limit_reached: true }));
const rows = [
  { model: "claude-opus-5-5", ...agg({ calls: 22, spend_usd: 1.31, input_tokens: 45000, output_tokens: 33000, total_tokens: 78000 }) },
  { model: "claude-<sonnet>", ...agg({ calls: 39, errors: 1, spend_usd: 1.16, total_tokens: 1000, unpriced_calls: 2 }) },
  { model: "qwen3.8", ...agg({ calls: 1, total_tokens: 9900, cluster: { calls: 1, ...tok({ total_tokens: 9900 }) } }) },
];
out.list = api.usageList(rows, "Model", (x) => "<code>" + sandbox.escapeHtml(x.model) + "</code>");
out.mixed = api.usageList([{ run_id: "r1", ...agg({ calls: 5, spend_usd: 0.4, total_tokens: 900, cluster: { calls: 2, ...tok({}) } }) }], "Run", (x) => x.run_id);
out.free = api.usageList([rows[2]], "Model", (x) => x.model);
out.emptyList = api.usageList([], "Run", (x) => x.run_id, { empty: "None of the calls in this period belonged to a run: they were chat replies." });
out.capped = api.usageList(Array.from({ length: 50 }, (_, i) => ({ model: "m" + i, ...agg({ calls: 1, spend_usd: 0.01 }) })), "Model", (x) => x.model, { capped: 50 });
const page = { period: "cycle", totals, errors: [{ error_type: "rate", calls: 1, last_at: "2026-10-18T18:44:57" }],
  by_model: rows, by_chat: [{ conversation_id: "2", title: "DDX41 <QC>", ...agg({ calls: 3, spend_usd: 0.2 }) }],
  by_run: [{ run_id: "a1b2", question: "Compare rods & cones", status: "done", conversation_id: "1", ...agg({ calls: 3, spend_usd: 0.3 }) }] };
out.byModel = api.usageBreakdownHtml(page, "model");
out.byRun = api.usageBreakdownHtml(page, "run");
out.byChat = api.usageBreakdownHtml(page, "chat");
out.noCalls = ["today", "7d", "cycle", "all"].map((period) => api.usageBreakdownHtml({ period, totals: { period: agg() }, by_model: [], by_run: [], by_chat: [] }));
out.periodTotal = api.usagePeriodTotalHtml(page);
out.cycleText = api.usageCycleText(span, "2026-10-18");
out.cycleFirst = api.usageCycleText(span, "2026-10-01");
out.apiPart = api.apiPart(cycle);
const today = api.localIsoDay();
const quiet = Array.from({ length: 30 }, (_, i) => ({ date: i === 29 ? today : "2026-09-" + String(i + 1).padStart(2, "0"), spend_usd: 0, total_tokens: 0, calls: 0 }));
out.chartEmpty = api.usageChart(quiet);
out.chartTokens = api.usageChart(quiet.map((d, i) => (i === 29 ? { ...d, total_tokens: 9900, calls: 1 } : d)));
out.chartSpend = api.usageChart(quiet.map((d, i) => (i >= 28 ? { ...d, spend_usd: i === 29 ? 0.18 : 0.09, total_tokens: 5000, calls: 2 } : d)));
out.usd = [0, 0.004, 0.18, 123.4].map((v) => api.fmtUsd(v));
out.pw = [["", "abcdef", "abcdef"], ["old", "abc", "abc"], ["old", "abcdef", "abcdeg"], ["old", "abcdef", "abcdef"]].map((a) => api.passwordProblem(...a));
out.me = api.accountInfoHtml({ username: "<eve>", role: "user", email: "e@uci.edu", created_at: "2026-10-16T19:59:04+00:00" });
// The chat counter, from /api/usage/chat/{id} or the usage_update push: its cluster share has only
// calls and total_tokens.
const chatTotals = (o, cluster) => ({ calls: 0, total_tokens: 0, spend_usd: 0, unpriced_calls: 0, ...o, ...(cluster ? { cluster } : {}) });
const apiOnly = { chat: chatTotals({ calls: 4, total_tokens: 12000, spend_usd: 0.18 }, { calls: 0, total_tokens: 0 }) };
const mixed = { chat: chatTotals({ calls: 6, total_tokens: 21900, spend_usd: 0.18 }, { calls: 2, total_tokens: 9900 }) };
const onCluster = { chat: chatTotals({ calls: 3, total_tokens: 9900 }, { calls: 3, total_tokens: 9900 }) };
const apiRun = chatTotals({ calls: 2, total_tokens: 3000, spend_usd: 0.05 }, { calls: 0, total_tokens: 0 });
const clusterRun = chatTotals({ calls: 2, total_tokens: 9900 }, { calls: 2, total_tokens: 9900 });
out.counter = {
  api: api.chatUsageText(apiOnly), mixed: api.chatUsageText(mixed), cluster: api.chatUsageText(onCluster),
  apiRunning: api.chatUsageText({ ...apiOnly, run: apiRun }, true),
  apiIdle: api.chatUsageText({ ...apiOnly, run: apiRun }, false),
  clusterRunning: api.chatUsageText({ ...mixed, run: clusterRun }, true),
  clusterOnlyRunning: api.chatUsageText({ ...onCluster, run: clusterRun }, true),
  none: api.chatUsageText({ chat: chatTotals({}) }), missing: api.chatUsageText(undefined),
  older: api.chatUsageText({ chat: chatTotals({ calls: 4, total_tokens: 12000, spend_usd: 0.18 }) }),
};
out.counterTitle = { api: api.usageTitle(apiOnly.chat), mixed: api.usageTitle(mixed.chat), cluster: api.usageTitle(onCluster.chat),
  unpriced: api.usageTitle({ ...apiOnly.chat, unpriced_calls: 2 }) };
console.log(JSON.stringify(out));
""".replace("__ESCAPE__", ESCAPE)


def _block(source: str, start: str, end: str) -> str:
    return source[source.index(start):source.index(end, source.index(start))]


@pytest.fixture(scope="module")
def page() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    code = (_block(source, "function fmtTokens(n)", "\n}\n") + "\n}\n" +
            _block(source, "// ---- Account view", "// ---- init ---"))
    env = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8", "TZ": "America/Los_Angeles"}
    done = subprocess.run([node, "-e", HARNESS], input=code, capture_output=True, text=True, timeout=30, env=env)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_summary_counts_the_tokens_an_api_key_paid_for_and_shows_the_cluster_gpu_as_free(page):
    s = page["summary"]
    assert "Estimated API spend" in s and "~$2.47" in s and "Today ~$0.18 · last 7 days ~$0.85" in s
    assert "API tokens" in s and ">2.5M<" in s, "the cluster's 9.9K tokens are not in the API figure"
    assert "86 API calls" in s and '<span class="bad">1 failed</span>' in s
    assert "Cluster GPU: 1 call · 9.9K tokens · free" in s
    assert "On pace for ~$4.38 by Oct 31" in s                          # 2.47 / 17.5 days × 31
    assert page["apiPart"] == {"calls": 86, "input_tokens": 187000, "cache_write_tokens": 228000,
                               "cache_read_tokens": 1900000, "output_tokens": 136000, "total_tokens": 2451000}


def test_with_no_budget_the_tile_says_so_and_offers_to_set_one(page):
    s = page["summary"]
    assert "Not set" in s and "data-budget-edit" in s and "Set your monthly credit" in s


def test_the_budget_tile_says_where_the_spend_stands(page):
    on = page["onTrack"]
    assert "On track" in on and "~$2.53 left" in on and "~$2.47 of $5.00" in on and "49%" in on
    assert "lasts until the reset on Nov 1" in on
    assert "Hard limit $4.00 · then new runs on your key are blocked" in on
    assert 'style="left:50%"' in on and 'style="left:80%"' in on and 'left:100%' not in on
    assert "84% used" in page["nearly"] and "tone-warn" in page["nearly"]
    assert "Runs out early" in page["early"] and "runs out around <strong>Oct 25</strong>" in page["early"]
    over = page["over"]
    assert "Credit used up" in over and "Over by ~$0.60" in over and "width:100%" in over
    assert "tone-over" in over and '<span class="bad">reached</span>' in over
    # "ok" alone is a global status class in the console (green, bold): the tile never uses it bare.
    for html in (on, page["nearly"], over):
        assert ' ok"' not in html and '"ok ' not in html and 'class="ok' not in html


def test_a_breakdown_row_has_its_cost_its_share_and_its_tokens_by_class(page):
    lst = page["list"]
    assert "Est. cost · share" in lst
    assert "~$1.31" in lst and "53%" in lst and "47%" in lst
    assert "22 calls · 78K tokens" in lst and "45K input · 33K output" in lst
    assert "claude-&lt;sonnet&gt;" in lst and "<sonnet>" not in lst
    assert '<span class="bad">1 failed</span>' in lst and "usage-unpriced" in lst
    assert 'style="width:53.0%"' in lst


def test_a_row_on_the_cluster_gpu_reads_free_and_a_mixed_one_says_how_many(page):
    assert page["free"].count(">Free<") == 1 and "cluster GPU" in page["free"] and "$0" not in page["free"]
    assert "Free" in page["list"].split("qwen3.8")[1]
    mixed = page["mixed"]
    assert "2 on the cluster GPU, free" in mixed and "~$0.40" in mixed and ">Free<" not in mixed


def test_an_empty_list_says_why_and_a_capped_one_points_at_the_csv(page):
    assert page["emptyList"] == ('<p class="usage-empty">None of the calls in this period belonged to a run: '
                                 'they were chat replies.</p>')
    assert "The 50 largest are shown; Export CSV has every call." in page["capped"]


def test_the_breakdown_shows_one_list_at_a_time_with_failures_first(page):
    by_model, by_run, by_chat = page["byModel"], page["byRun"], page["byChat"]
    assert by_model.index("1 failed request") < by_model.index("usage-seg") < by_model.index("usage-list")
    assert 'data-usage-by="model">Models <span class="usage-seg-n">3</span>' in by_model
    assert 'aria-pressed="true" data-usage-by="model"' in by_model
    assert 'aria-pressed="true" data-usage-by="run"' in by_run
    assert 'data-open-chat="1"' in by_run and "Compare rods &amp; cones" in by_run
    assert 'class="usage-status done">done<' in by_run and "claude-opus" not in by_run
    assert 'data-open-chat="2">DDX41 &lt;QC&gt;</a>' in by_chat


def test_with_no_calls_the_breakdown_says_so_for_the_period(page):
    today, week, cyc, ever = page["noCalls"]
    assert "No model calls today." in today and "Pick a longer period" in today
    assert "No model calls in the last 7 days." in week
    assert "No model calls in this billing cycle." in cyc
    assert "No model calls yet." in ever and "Pick a longer period" not in ever
    assert all("usage-list" not in html for html in page["noCalls"])


def test_the_period_and_cycle_lines(page):
    assert page["periodTotal"] == ("This billing cycle: ~$2.47 · 2.5M API tokens · 86 API calls · "
                                   "1 call on the cluster GPU (free)")
    assert page["cycleText"] == "Oct 1 – Oct 31, 2026 · day 18 of 31 · resets Nov 1"
    assert "day 1 of 31" in page["cycleFirst"]


def test_the_daily_chart_shows_spend_tokens_or_says_nothing_happened(page):
    assert "No model calls in these 30 days." in page["chartEmpty"] and "daily estimated spend" in page["chartEmpty"]
    assert "daily tokens" in page["chartTokens"] and "peak 9.9K tokens" in page["chartTokens"]
    spend = page["chartSpend"]
    assert "daily estimated spend" in spend and "peak ~$0.18" in spend
    assert spend.count('class="usage-day today"') == 1 and spend.count("usage-day zero") == 28
    assert spend.endswith("<span>Today</span></div></div>")


def test_amounts_under_a_cent_carry_no_tilde(page):
    assert page["usd"] == ["$0", "<$0.01", "~$0.18", "~$123"]


def test_a_new_password_is_typed_twice_and_checked_before_it_is_sent(page):
    assert page["pw"] == ["Enter your current password.", "The new password needs at least 6 characters.",
                          "The two new passwords are not the same.", ""]


def test_the_account_line_escapes_the_name_and_dates_the_account(page):
    me = page["me"]
    assert "&lt;eve&gt;" in me and "<eve>" not in me
    assert '<span class="role-chip">user</span>' in me and "e@uci.edu · Member since Oct 16, 2026" in me


def test_password_and_usage_are_two_tabs_of_the_account_page():
    html = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    view = html[html.index('<section id="view-account"'):html.index("<!-- ===== SYSTEM view")]
    usage = view[view.index('id="accountPane-usage"'):view.index('id="accountPane-password"')]
    password = view[view.index('id="accountPane-password"'):]
    assert 'role="tab" aria-selected="true" aria-controls="accountPane-usage"' in view
    assert 'aria-controls="accountPane-password"' in view
    for el in ('id="usageBody"', 'id="usageBreakdown"', 'id="usagePeriod"', 'id="usageExport"',
               'id="budgetForm"', 'id="accountKeysBtn"'):
        assert el in usage and el not in password
    for el in ('id="changePwForm"', 'id="cpOld"', 'id="cpNew"', 'id="cpNew2"'):
        assert el in password and el not in usage
    source = APP_JS.read_text(encoding="utf-8")
    assert 'fetch("/api/account/password", { method: "POST"' in source
    assert "payload = { old_password: $(\"cpOld\").value, new_password: $(\"cpNew\").value }" in source


def test_the_chat_counter_counts_api_tokens_and_shows_the_cluster_gpu_as_free(page):
    c = page["counter"]
    assert c["api"] == "12K API tokens · ~$0.18"
    assert c["mixed"] == "12K API tokens · ~$0.18 · cluster GPU free"     # 21.9K − the cluster's 9.9K
    assert c["cluster"] == "9.9K tokens · free on the cluster GPU"       # not "0 API tokens · $0"
    # A run's running cost while it goes; a run with no API call adds nothing the chat does not say.
    assert c["apiRunning"] == "12K API tokens · ~$0.18 · this run ~$0.05"
    assert c["apiIdle"] == c["api"]
    assert c["clusterRunning"] == c["mixed"] and c["clusterOnlyRunning"] == c["cluster"]
    # Nothing to count hides the counter; a total without the sub-total reads as API only.
    assert c["none"] == "" and c["missing"] == ""
    assert c["older"] == c["api"]


def test_the_chat_counter_tooltip_separates_api_calls_from_the_free_cluster_gpu(page):
    t = page["counterTitle"]
    assert t["api"].startswith("4 API calls · 12,000 API tokens · ~$0.18 estimated.\n")
    assert "Claude Console is the authoritative bill" in t["api"] and "Cluster GPU" not in t["api"]
    assert t["mixed"].startswith("4 API calls · 12,000 API tokens · ~$0.18 estimated.\n"
                                 "Cluster GPU: 2 calls · 9,900 tokens · free. HPC3 charges the cluster GPU")
    assert t["cluster"] == ("Cluster GPU: 3 calls · 9,900 tokens · free. HPC3 charges the cluster GPU by the time it "
                            "runs, not by token: these calls are in no dollar figure or budget.\n"
                            "Click for Account › API usage.")
    assert "2 call(s) on a model the price table does not list" in t["unpriced"]
