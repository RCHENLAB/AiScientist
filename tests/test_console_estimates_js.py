"""Exercise plan estimates and completed usage with the console's actual JS helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"


@pytest.fixture(scope="module")
def estimates():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index("function estimateNumber("):source.index("function planSub(")]
    harness = r'''
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const api = vm.runInNewContext(src + "\n;({planEstimateMarkdown, runUsageMarkdown, planEstimateHtml})",
  {renderMarkdown: (md) => "<md>" + md + "</md>"});
const row = {seconds:[1500,2400],cpu_hours:[1,2],gpu_hours:[0,1],cost:[0.1,0.3],rough:true};
const estimate = {steps:[{...row,step:1,skill:"cellqc"}],total:row,
  rates_configured:true,currency:"USD",
  note:"Sequential compute time; parallel steps may overlap. HPC3 queue wait time is not included."};
const actual = {seconds:1800,cpu_hours:1.5,gpu_hours:0.5,cost:0.2,rough:true};
console.log(JSON.stringify({
  plan:api.planEstimateMarkdown(estimate),
  noRates:api.planEstimateMarkdown({...estimate,rates_configured:false}),
  actual:api.runUsageMarkdown({estimate,actual}),
  noActualRates:api.runUsageMarkdown({estimate:{...estimate,rates_configured:false},actual}),
  fallback:api.planEstimateMarkdown({...estimate,note:undefined}),
  note:api.planEstimateMarkdown({...estimate,note:"Note | with\nnewlines"}),
  empty:api.planEstimateMarkdown(null),
  html:api.planEstimateHtml(estimate),
  rounded:api.planEstimateMarkdown({steps:[{step:2,skill:"qc",seconds:[5400,9600],cpu_hours:[24.75,45.87],
    gpu_hours:[0,0],cost:[0,0],rough:false}],rates_configured:false}),
  noHtml:api.planEstimateHtml(null),
}));
'''
    done = subprocess.run(
        [node, "-e", harness], input=block, capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_plan_shows_step_total_ranges_and_backend_note(estimates):
    plan = estimates["plan"]
    assert "| Step | Skill | Time | CPU hours | GPU hours | Est. cost |" in plan
    assert "| 1 (rough) | cellqc | ~25–40 min | 1–2 | 0–1 | 0.1–0.3 USD |" in plan
    assert "| Total (rough) |" in plan
    assert "Sequential compute time; parallel steps may overlap." in plan
    assert "HPC3 queue wait time is not included." in plan
    assert "HPC3 queue wait time is not included." in estimates["fallback"]
    assert "Note   with newlines" in estimates["note"]
    assert estimates["empty"] == ""


def test_completed_usage_compares_estimates_and_actuals(estimates):
    actual = estimates["actual"]
    assert "| Time | ~25–40 min | 30 min |" in actual
    assert "| CPU hours | 1–2 | 1.5 |" in actual
    assert "| GPU hours | 0–1 | 0.5 |" in actual
    assert "| Cost | 0.1–0.3 USD | 0.2 USD |" in actual
    assert "Estimate: rough." in actual
    assert "Actual resource accounting: rough." in actual
    assert "summed tool compute time; model/report overhead" in actual


def test_missing_rates_are_not_presented_as_free_compute(estimates):
    assert "Rates not configured: no cost is shown." in estimates["noRates"]
    assert "| 1 (rough) | cellqc | ~25–40 min | 1–2 | 0–1 | — |" in estimates["noRates"]
    assert "USD" not in estimates["noRates"]
    assert "| Cost | — | — |" in estimates["noActualRates"]
    assert "Rates not configured: no cost is shown." in estimates["noActualRates"]
    assert "Rates not configured" not in estimates["plan"] + estimates["actual"]


def test_ranges_are_rounded_and_equal_ends_written_once(estimates):
    assert "| 2 | qc | ~1.5–2.7 hr | 24.8–45.9 | 0 | — |" in estimates["rounded"]


def test_the_plan_card_draws_the_estimate_in_its_own_block(estimates):
    assert estimates["html"].startswith('<div class="plan-estimate"><md>')
    assert "| Step | Skill | Time | CPU hours | GPU hours | Est. cost |" in estimates["html"]
    assert estimates["noHtml"] == ""
