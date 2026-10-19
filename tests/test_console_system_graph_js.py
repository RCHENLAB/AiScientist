"""The System page's live graph, run under node.

The layout, the placement of the running tasks and the run cards are plain functions in
``frontend/console/app.js``. This lays out the REAL graph (``system_info.workflow_graph()``) and
places runs on it the way ``/api/system/live`` describes them, and pins what a person depends on:
nothing is drawn off the canvas or on top of another node, every tool sits inside the box of the
place it runs, a task is drawn where it is (the tool it is running, the Critic, the review that waits
for its owner), and the text a run carries is escaped. Skipped where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from aiscientist.gateway import system_info

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"

HARNESS = r"""
const vm = require("vm");
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));
const api = vm.runInNewContext(input.src + "\n;({ sysGraphLayout, sysGraphSvg, sysTaskTargets, sysRunCardHtml, _sgDecorate, SG })", { escapeHtml });
const L = api.sysGraphLayout(input.graph);
const runs = api._sgDecorate(input.runs);
const out = { width: L.width, height: L.height, pos: L.pos, edges: L.edges, SG: api.SG };
out.svg = api.sysGraphSvg(input.graph, L);
out.targets = api.sysTaskTargets(input.graph, L, runs, input.now);
out.cards = runs.map((r) => api.sysRunCardHtml(r, input.now));
console.log(JSON.stringify(out));
"""

NOW = 1_000_000.0


def _run(runs: list[dict]) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index("// ---- Live workflow graph:"):source.index("function paintSystemRuns")]
    payload = json.dumps({"src": block, "graph": system_info.workflow_graph(), "runs": runs, "now": NOW})
    done = subprocess.run([node, "-e", HARNESS], input=payload, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _run_view(key: str, **kw) -> dict:
    base = {"key": key, "kind": "research", "stage": "execute", "stage_label": "Run steps",
            "stage_since": NOW - 30, "started": NOW - 300, "updated": NOW, "ended": None, "outcome": "",
            "waiting": "", "steps": {"total": 4, "accepted": 1}, "tools_called": 3, "tool_errors": 0,
            "llm": {"calls": 9, "thinking": []}, "active": [], "mine": True}
    base.update(kw)
    return base


def _box(p: dict) -> tuple[float, float, float, float]:
    return p["x"] - p["w"] / 2, p["y"] - p["h"] / 2, p["x"] + p["w"] / 2, p["y"] + p["h"] / 2


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def test_every_node_is_on_the_canvas_and_nothing_is_drawn_on_top_of_another():
    out = _run([])
    graph = system_info.workflow_graph()
    pos = out["pos"]
    assert set(pos) == {n["id"] for n in graph["nodes"]}, "every node of the graph is drawn"
    for nid, p in pos.items():
        x0, y0, x1, y1 = _box(p)
        assert 0 <= x0 and x1 <= out["width"] and 0 <= y0 and y1 <= out["height"], nid
    big = [nid for nid, p in pos.items() if p["kind"] != "tool"]
    for i, a in enumerate(big):
        for b in big[i + 1:]:
            assert not _overlap(_box(pos[a]), _box(pos[b])), (a, b)
    # every tool chip lies inside its place's box, under its title, and chips never overlap
    tools = {n["id"]: n for n in graph["nodes"] if n["type"] == "tool"}
    for tid, t in tools.items():
        chip, box = _box(pos[tid]), _box(pos[t["meta"]["place"]])
        assert box[0] <= chip[0] and chip[2] <= box[2] and box[1] + 20 <= chip[1] and chip[3] <= box[3], tid
    ids = list(tools)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            assert not _overlap(_box(pos[a]), _box(pos[b])), (a, b)
    # the research lane reads left to right in the stages' own order
    lane = [n["id"] for n in graph["nodes"] if n["type"] == "stage" and "research" in n["meta"]["kinds"]]
    xs = [pos[s]["x"] for s in lane]
    assert xs == sorted(xs) and len({pos[s]["y"] for s in lane}) == 1


def test_the_svg_has_a_focusable_node_per_graph_node_and_escapes_labels():
    out = _run([])
    graph = system_info.workflow_graph()
    for n in graph["nodes"]:
        assert f'data-node="{n["id"]}"' in out["svg"]
    assert out["svg"].count('tabindex="0"') == len(graph["nodes"])
    assert "Data &amp; compute" in out["svg"] and "Data & compute" not in out["svg"]
    assert 'id="sgTasks"' in out["svg"] and 'id="sgLinks"' in out["svg"]


def test_a_task_is_drawn_where_it_is():
    runs = [
        _run_view("a", active=[{"phase": "tool", "tool": "run_de", "since": NOW - 70, "step": "DE per cell type",
                                "n": 3, "specialist": "Clustering & cell-type specialist"},
                               {"phase": "critic", "tool": "", "since": NOW - 5, "step": "Literature", "n": 4,
                                "specialist": "Scientist"}]),
        _run_view("b", stage="approve", stage_label="Your review", waiting="plan"),
        _run_view("c", stage="plan", stage_label="Plan", llm={"calls": 1, "thinking": ["plan"]}),
        _run_view("d", kind="chat", stage="chat", stage_label="Chat answer", mine=False,
                  active=[{"phase": "tool", "tool": "literature_search", "since": NOW - 2}]),
        _run_view("e", stage="done", stage_label="Done", ended=NOW - 60, outcome="failed"),
    ]
    t = _run(runs)["targets"]
    at = {k["key"]: k["node"] for k in t["tokens"]}
    assert at == {"a": "tool:run_de", "a:4": "critic", "b": "approve", "c": "plan",
                  "d": "tool:literature_search", "e": "done"}
    assert t["hot"]["tool:run_de"] == "hot" and t["hot"]["critic"] == "hot"
    assert t["hot"]["approve"] == "waiting", "a run waiting for its owner is shown waiting"
    assert "done" not in t["hot"], "a finished run sits at Done without lighting it"
    links = {(x["from"], x["to"]) for x in t["links"]}
    assert ("scientist", "tool:run_de") in links and ("chat", "tool:literature_search") in links
    assert ("model", "plan") in links, "the model is drawn thinking for the stage that called it"
    assert "approve>execute" in t["flows"] and "request>chat" in t["flows"]
    tok = next(k for k in t["tokens"] if k["key"] == "e")
    assert "sg-out-failed" in tok["cls"]
    # two tasks at one node sit side by side, not on top of each other
    two = _run([_run_view("x", stage="plan"), _run_view("y", stage="plan")])["targets"]["tokens"]
    assert two[0]["node"] == two[1]["node"] == "plan" and two[0]["x"] != two[1]["x"]


def test_a_busy_node_shows_four_tasks_and_a_count_and_finished_ones_leave_done():
    busy = [_run_view(f"p{i}", stage="plan") for i in range(7)]
    ended = [_run_view(f"d{i}", stage="done", ended=NOW - 10 * i, outcome="finished") for i in range(12)]
    t = _run(busy + ended)["targets"]
    at_plan = [k for k in t["tokens"] if k["node"] == "plan"]
    assert len(at_plan) == 4 and {"node": "plan", "count": 3} == {k: v for k, v in t["more"][0].items() if k in ("node", "count")}
    # the "+3" sits beside the four dots, still on the canvas, not on a neighbour's
    assert t["more"][0]["x"] < min(k["x"] for k in at_plan) and t["more"][0]["x"] > 0
    # a finished run's dot stays on Done for 90 s (here: ended 0..90 s ago = 10 runs, 4 drawn + "+6")
    at_done = [k for k in t["tokens"] if k["node"] == "done"]
    assert len(at_done) == 4 and next(m["count"] for m in t["more"] if m["node"] == "done") == 6
    assert t["hot"].get("plan") == "hot" and "done" not in t["hot"]


def test_a_run_card_says_what_runs_and_escapes_what_the_run_carries():
    runs = [_run_view("a", question="<img src=x onerror=alert(1)> DDX41", active=[
        {"phase": "tool", "tool": "run_de<b>", "since": NOW - 70, "step": "DE <per> cell type", "n": 3,
         "specialist": "Clustering & cell-type specialist"}]),
            _run_view("b", mine=False, stage="approve", stage_label="Your review", waiting="plan")]
    cards = _run(runs)["cards"]
    assert "&lt;img src=x" in cards[0] and "<img" not in cards[0]
    assert "<code>run_de&lt;b&gt;</code>" in cards[0] and "DE &lt;per&gt; cell type" in cards[0]
    assert "<b>Step 3</b>" in cards[0] and "1m 10s" in cards[0] and "5m 00s" in cards[0]
    assert "1/4 steps accepted" in cards[0]
    assert "another account" in cards[1] and "Waiting for its owner to review the plan." in cards[1]


# The lists under the graph: pipelines, tools and skills, one line each with the whole text when a row
# is opened, and one search over all three (frontend/console/app.js, "System view").
CATALOG_HARNESS = r"""
const vm = require("vm");
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));
const ctx = { escapeHtml, renderMarkdown: escapeHtml, methodUnwrap: (s) => s, METHOD_DATA_TYPES: { scrna: "scRNA-seq" } };
const api = vm.runInNewContext(input.src + "\n;({ sysCatalogHtml, sysMatch, sysSearchText, sysToolItem, sysItemHtml })", ctx);
const out = { html: api.sysCatalogHtml(input.overview) };
out.match = input.queries.map(([q, parts]) => api.sysMatch(q, api.sysSearchText(parts)));
out.hostile = api.sysItemHtml(api.sysToolItem(input.hostile));
console.log(JSON.stringify(out));
"""


def test_the_catalog_lists_every_entry_one_line_each_and_its_search_and_escaping():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index("// ---- System view"):source.index("// ---- Live workflow graph:")]
    o = system_info.system_overview()
    hostile = {"name": "evil<b>", "summary": "<img src=x onerror=alert(1)>", "description": 'say "hi" & <script>',
               "where": "HPC3", "requires": [], "available": None, "reads_private_data": False, "category": "x", "code": ""}
    queries = [["run_de", ["run_de"]], ["cell type", ["celltype_annotation", "cell-type labels"]],
               ["hpc3 variant", ["annotate_variants", "HPC3 · variants (VEP)"]],
               ["hpc3 variant", ["run_de", "HPC3 · analysis"]], ["", ["anything"]]]
    payload = json.dumps({"src": block, "overview": o, "queries": queries, "hostile": hostile})
    done = subprocess.run([node, "-e", CATALOG_HARNESS], input=payload, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    html = out["html"]
    # every pipeline, tool and skill has its row, and the three lists say how many there are
    for p in o["pipelines"]:
        assert f'data-id="pipeline:{p["key"]}"' in html
    for t in o["tools"]:
        assert f'data-id="tool:{t["name"]}"' in html
    for s in o["skills"]:
        assert f'data-id="skill:{s["name"]}"' in html
    for key in ("pipelines", "tools", "skills"):
        assert f'data-total="{len(o[key])}"' in html
    assert html.count('<details class="sys-item') == len(o["pipelines"]) + len(o["tools"]) + len(o["skills"])
    # every term must be found, "cell type" finds cell-type and cell_type, an empty search shows all
    assert out["match"] == [True, True, True, False, True]
    # what a row carries is escaped, in the row and in the text the search reads
    assert "<img" not in out["hostile"] and "<script>" not in out["hostile"] and "evil&lt;b&gt;" in out["hostile"]
