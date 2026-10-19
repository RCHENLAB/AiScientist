"""The "＋ Data" popover lists the five newest uploads and searches all of them, under node.

"Recent uploads" used to list every upload the account ever made. It now shows the RECENT_UPLOADS
newest, and "Search in all uploaded datasets" opens a search of every upload by name (not path: a
managed dataset's path is users/<account>/datasets/<uuid>/…, shared by all of them). Closing the
popover goes back to the recent list. This runs those functions of ``frontend/console/app.js`` with
the page's elements stubbed. Skipped where node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"


def _function(source: str, name: str) -> str:
    """The whole text of ``function name(…) { … }``, by matching braces."""
    start = source.index(f"function {name}(")
    depth, i = 0, source.index("{", source.index(")", start))
    while True:
        ch = source[i]
        depth += ch == "{"
        depth -= ch == "}"
        i += 1
        if depth == 0:
            return source[start:i] + "\n"


HARNESS = r"""
const els = {};
const $ = (id) => els[id] || (els[id] = { id, value: "", textContent: "", innerHTML: "", focused: false,
  classes: new Set(id === "dataMenu" || id === "dataMenuSearchRow" || id === "dataMenuSearchBtn" ? ["hidden"] : []),
  focus() { this.focused = true; },
  get classList() { const c = this.classes; return { add: (k) => c.add(k), remove: (k) => c.delete(k),
    contains: (k) => c.has(k), toggle: (k, on) => { if (on === undefined ? !c.has(k) : on) c.add(k); else c.delete(k); } }; } });
const state = { datasets: [], boundPaths: [], dataSearch: null };
const names = ["retina_atlas.h5ad", "Sample1_WT/", "Sample2_KO/", "rods.h5ad", "pbmc3k.h5ad",
               "IRD_cohort.vcf.gz", "panel.bed", "Sample3_WT/", "sample1_wt_rerun.h5ad"];
state.datasets = names.map((n, i) => ({ name: n, path: `/data/users/u/datasets/uuid-${i}/${n}`,
  kind: n.endsWith("/") ? "folder" : "file", at: new Date(Date.now() - i * 864e5).toISOString(), size: 2048 }));
const shown = () => [...$("dataMenuRecent").innerHTML.matchAll(/class="dmr-name">([^<]*)</g)].map((m) => m[1]);
const hidden = (id) => $(id).classList.contains("hidden");
const out = {};

toggleDataMenu(true);
out.recent = { shown: shown(), label: $("dataMenuRecentLabel").textContent, searchBtn: !hidden("dataMenuSearchBtn"),
               count: $("dataMenuSearchCount").textContent, searchRow: !hidden("dataMenuSearchRow"),
               sub: $("dataMenuRecent").innerHTML.includes("dmr-sub") };

setDataSearch("");
out.all = { shown: shown(), label: $("dataMenuRecentLabel").textContent, searchBtn: !hidden("dataMenuSearchBtn"),
            searchRow: !hidden("dataMenuSearchRow"), focused: $("dataMenuSearch").focused,
            sub: $("dataMenuRecent").innerHTML.includes("1d ago · 2 KB") };

state.boundPaths = [state.datasets[8].path];
setDataSearch("WT sample1");
out.words = { shown: shown(), label: $("dataMenuRecentLabel").textContent,
              checked: $("dataMenuRecent").innerHTML.includes("data-menu-recent-item active") };
setDataSearch("uuid");
out.notPath = { shown: shown(), html: $("dataMenuRecent").innerHTML };

setDataSearch("rods");
toggleDataMenu(false);
toggleDataMenu(true);
out.reopened = { search: state.dataSearch, shown: shown().length, searchRow: !hidden("dataMenuSearchRow") };

state.datasets = [];
renderDataMenuRecent();
out.empty = { html: $("dataMenuRecent").innerHTML, searchBtn: !hidden("dataMenuSearchBtn") };
process.stdout.write(JSON.stringify(out));
"""


def test_recent_list_caps_at_five_and_search_finds_the_rest():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    code = "const RECENT_UPLOADS = 5;\n" + "".join(_function(source, n) for n in (
        "escapeHtml", "datasetIcon", "relTime", "fmtSize", "matchDatasets", "renderDataMenuRecent",
        "setDataSearch", "toggleDataMenu")) + HARNESS
    assert "const RECENT_UPLOADS = 5;" in source
    done = subprocess.run([node, "-"], input=code, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)

    # The popover opens on the five newest uploads, with the search offered below them.
    assert out["recent"] == {"shown": ["retina_atlas.h5ad", "Sample1_WT/", "Sample2_KO/", "rods.h5ad",
                                       "pbmc3k.h5ad"],
                             "label": "Recent uploads (click to add/remove)", "searchBtn": True,
                             "count": "9", "searchRow": False, "sub": False}
    # The search starts with every upload, newest first, and tells them apart by age and size.
    assert out["all"]["shown"] == ["retina_atlas.h5ad", "Sample1_WT/", "Sample2_KO/", "rods.h5ad",
                                   "pbmc3k.h5ad", "IRD_cohort.vcf.gz", "panel.bed", "Sample3_WT/",
                                   "sample1_wt_rerun.h5ad"]
    assert out["all"]["label"] == "All 9 uploads"
    assert out["all"]["searchBtn"] is False and out["all"]["searchRow"] is True
    assert out["all"]["focused"] is True and out["all"]["sub"] is True
    # Every word must be in the name, in any case; a bound upload stays checked in the results.
    assert out["words"] == {"shown": ["Sample1_WT/", "sample1_wt_rerun.h5ad"],
                            "label": "2 of 9 uploads match", "checked": True}
    # The shared path is not searched.
    assert out["notPath"]["shown"] == [] and "No upload matches that" in out["notPath"]["html"]
    # Closing the popover drops the search: it reopens on the recent list.
    assert out["reopened"] == {"search": None, "shown": 5, "searchRow": False}
    assert out["empty"] == {"html": '<div class="data-menu-empty">No uploads yet</div>', "searchBtn": False}
