"""Each species result has its own Save to my data control."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"


def _render(row: dict) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index("function resultHandoffHtml("):source.index("async function showResultHandoff(")]
    script = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const api = vm.runInNewContext(src + "\n;({ resultHandoffHtml })", {
  escapeHtml: (s) => String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;"),
  icon: () => "",
});
console.log(api.resultHandoffHtml("run", __ROW__));
""".replace("__ROW__", json.dumps(row))
    done = subprocess.run([node, "-e", script], input=block, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_each_species_result_has_a_separate_save_control():
    html = _render({"name": "mouse.h5ad", "datasets": [
        {"name": "mouse.h5ad", "what": "dataset:mouse", "n_cells": 12, "n_genes": 5},
        {"name": "human.h5ad", "what": "dataset:human", "n_cells": 8, "n_genes": 7},
    ]})
    assert html.count("mouse.h5ad") == 1
    assert html.count("human.h5ad") == 1
    assert 'data-what="dataset:mouse"' in html
    assert 'data-what="dataset:human"' in html
    assert html.count("data-save-result=") == 2


def test_legacy_single_dataset_keeps_its_save_control():
    html = _render({"name": "legacy.h5ad", "n_cells": 12, "n_genes": 5})
    assert "legacy.h5ad" in html
    assert 'data-what="dataset"' in html
    assert html.count("data-save-result=") == 1


SERVER_FOLDER = "/data/aiscientist/data/users/alice/datasets/3f2c9a1e-5b7d-4c8e-9f0a-1b2c3d4e5f60/original"


def test_a_saved_result_is_named_never_shown_by_its_server_folder():
    """Issue 37: "Saved to my data" used to carry the server folder as its tooltip."""
    html = _render({"name": "pbmc.processed.h5ad", "datasets": [
        {"name": "pbmc.processed.h5ad", "what": "dataset", "n_cells": 12, "n_genes": 5,
         "saved_to": f"{SERVER_FOLDER}/pbmc.processed.h5ad"}],
        "tables": {"name": "retigene_curation.r7", "files": ["genes.csv"], "n_genes": 1, "saved_to": SERVER_FOLDER}})
    assert "/data/" not in html
    assert 'title="In Files → Datasets as pbmc.processed.h5ad"' in html
    assert 'title="In Files → Datasets as retigene_curation.r7"' in html


def test_the_save_toast_names_the_dataset_not_the_reply_s_path():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    block = source[source.index("async function onSaveResultClick("):source.index("function onRunsClick(")]
    reply = {"ok": True, "path": f"{SERVER_FOLDER}/pbmc.processed.h5ad", "dataset_id": 3,
             "name": "pbmc.processed.h5ad", "kind": "file"}
    script = r"""
const vm = require("vm");
const src = require("fs").readFileSync(0, "utf8");
const toasts = [];
const api = vm.runInNewContext(src + "\n;({ onSaveResultClick })", {
  toast: (m) => toasts.push(m),
  fetch: async () => ({ ok: true, json: async () => (__REPLY__) }),
  loadResultHandoffs: async () => ({}), showResultHandoff: () => {}, outputsShown: () => false,
  loadOutputs: () => {}, loadRuns: () => {}, loadDatasets: () => {}, encodeURIComponent,
});
const button = { dataset: { saveResult: "r7", what: "dataset", label: "Save to my data" },
                 disabled: false, textContent: "", closest: () => null };
api.onSaveResultClick({ target: { closest: () => button } }).then(() => console.log(JSON.stringify(toasts)));
""".replace("__REPLY__", json.dumps(reply))
    done = subprocess.run([node, "-e", script], input=block, capture_output=True, text=True, encoding="utf-8",
                          timeout=30)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ["Saved “pbmc.processed.h5ad” to your data: it is in Files → Datasets now."]
