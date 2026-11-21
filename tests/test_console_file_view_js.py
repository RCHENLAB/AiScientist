"""The file viewer and Markdown links and images (RCHENLAB/AiScientist issue 115), under node.

One preview by file type: a CSV is a table (sticky header, row numbers, types, sort, cut notice),
Markdown is rendered with a Source switch, JSON folds, YAML is highlighted. No preview runs HTML or script
from a file. A Markdown link is drawn only for the account's own files or an https address, and an image only
for the account's own files (an https image is shown as a link).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from test_console_data_menu_js import _function

CONSOLE = Path(__file__).resolve().parents[1] / "frontend" / "console"
VIEW = CONSOLE / "fileview.js"
APP = CONSOLE / "app.js"


def _node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _fv(body: str) -> str:
    return f'const assert = require("assert"); const FV = require({str(VIEW)!r});\n' + body + '\nconsole.log("ok");'


def test_the_kind_of_a_file_picks_its_view():
    out = _node(_fv(r'''
for (const [name, kind] of [["a.csv","csv"],["A.TSV","tsv"],["README.md","md"],["x.markdown","md"],["c.json","json"],
    ["c.yml","yaml"],["c.yaml","yaml"],["page.html","html"],["f.PNG","image"],["f.svg","image"],["r.pdf","pdf"],
    ["run.log","text"],["a.py","text"],["data.h5ad",""],["noext",""]]) assert.equal(FV.kind(name), kind, name);
'''))
    assert out == "ok"


def test_a_csv_is_a_table_with_sticky_header_row_numbers_types_and_counts():
    out = _node(_fv(r'''
const text = 'gene,logfc,padj,sig,note\r\nA,1.5,0.01,true,"has, comma"\r\nB,-2,0.5,false,"say ""hi"""\r\nC,,1e-3,true,\r\n';
const p = FV.parseDelimited(text, ",", false);
assert.deepEqual(p.columns, ["gene","logfc","padj","sig","note"]);
assert.deepEqual(p.rows[0], ["A","1.5","0.01","true","has, comma"]);
assert.deepEqual(p.rows[1], ["B","-2","0.5","false",'say "hi"']);
assert.equal(p.rows.length, 3);
assert.deepEqual(FV.columnTypes(p.columns, p.rows), ["text","number","number","boolean","text"]);
assert.deepEqual(FV.columnTypes(["n"], [["1"],["2"],[""]]), ["integer"]);
const html = FV.tableHtml(FV.tableState(p.columns, p.rows));
assert.ok(html.includes('class="ds-idx"'));                                  // sticky row-number column
assert.ok(html.includes('<span class="ds-type">number</span>'));
assert.ok(html.includes("5 columns, 3 rows"));
assert.ok(html.includes("<td>has, comma</td>") && html.includes("<td>say &quot;hi&quot;</td>"));
// a tab-separated file and a byte-order mark
const t = FV.parseDelimited("﻿a\tb\n1\t2\n", "\t", false);
assert.deepEqual(t.columns, ["a","b"]); assert.deepEqual(t.rows, [["1","2"]]);
'''))
    assert out == "ok"


def test_clicking_a_header_sorts_the_loaded_rows_numbers_as_numbers_and_empty_cells_last():
    out = _node(_fv(r'''
const rows = [["a","10"],["b","9"],["c",""],["d","-3"]];
const types = FV.columnTypes(["k","v"], rows);
const order = (col, dir) => FV.sortedRows(rows, col, dir, types[col]).map(([i, r]) => r[0]).join("");
assert.equal(order(1, 1), "dbac");                 // -3, 9, 10, then the empty cell
assert.equal(order(1, -1), "abdc");                // 10, 9, -3, the empty cell still last
assert.equal(order(0, -1), "dcba");
assert.equal(order(1, 0), "abcd");                 // no sort: the file's order
const st = FV.tableState(["k","v"], rows);
st.sort = { col: 1, dir: 1 };
const html = FV.tableHtml(st);
assert.ok(html.includes("sorted within the loaded rows") && html.includes("▲") && html.includes('aria-sort="ascending"'));
assert.ok(html.indexOf("<td>d</td>") < html.indexOf("<td>a</td>"));
// the row numbers stay the file's own
assert.ok(html.indexOf('<th class="ds-idx">4</th>') < html.indexOf('<th class="ds-idx">1</th>'));
'''))
    assert out == "ok"


def test_a_wide_csv_with_200_columns_keeps_every_column_and_scrolls_sideways():
    out = _node(_fv(r'''
const cols = Array.from({length: 200}, (_, i) => "c" + i);
const text = cols.join(",") + "\n" + cols.map((_, i) => i).join(",") + "\n";
const view = FV.textView(text, { name: "wide.csv", id: "t1" });
assert.equal((view.html.match(/data-fv-sort=/g) || []).length, 200);
assert.ok(view.html.includes("200 columns, 1 row"));
assert.ok(view.html.includes("ds-table-wrap fv-table-wrap"));                  // the wrapper scrolls (CSS: overflow auto)
assert.equal(view.state.types.filter((t) => t === "integer").length, 200);
'''))
    assert out == "ok"


def test_a_long_csv_says_it_was_cut_and_pages_through_the_loaded_rows():
    out = _node(_fv(r'''
const rows = Array.from({length: 5000}, (_, i) => i + ",x" + i).join("\n");
const text = "n,label\n" + rows + "\n5001,par";                // the read stopped in the middle of a line
const view = FV.textView(text, { name: "big.csv", id: "t2", shown: FV.LIMIT_BYTES, total: 40 * 1048576 });
assert.equal(view.state.rows.length, 5000);                    // the partial last line is dropped
assert.ok(view.html.includes("5,000 rows loaded (the file is longer)"));
assert.ok(view.html.includes("showing the first 200 rows"));
assert.ok(view.html.includes("Showing the first 1.0 MB of 40.0 MB. Download the file for the rest."));
assert.ok(view.html.includes("data-fv-more") && view.html.includes("Show 200 more rows"));
assert.equal((view.html.match(/<tr><th class="ds-idx">/g) || []).length, 200);
view.state.shown += FV.PAGE_ROWS;
assert.equal((FV.tableHtml(view.state).match(/<tr><th class="ds-idx">/g) || []).length, 400);
// a file read whole has no notice
const whole = FV.textView("a,b\n1,2\n", { name: "s.csv", id: "t3", shown: 8, total: 8 });
assert.ok(!whole.html.includes("Download the file for the rest"));
'''))
    assert out == "ok"


def test_hostile_cells_names_and_values_never_become_markup():
    out = _node(_fv(r'''
const evil = '<img src=x onerror=alert(1)>';
const csv = FV.textView('<script>alert(1)</script>,b\n' + evil + ',"\'><svg onload=alert(2)>"\n', { name: "e.csv", id: "t4" }).html;
assert.ok(!/<(?:script|img|svg)\b/i.test(csv) && csv.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
const json = FV.textView(JSON.stringify({ "<b>k</b>": evil, list: ["</script><script>x"] }), { name: "e.json", id: "t5" }).html;
assert.ok(!/<(?:script|img|b)\b/i.test(json));
const yaml = FV.textView("key: " + evil + "\n# <script>x</script>\n- <b>item</b>\n", { name: "e.yaml", id: "t6" }).html;
assert.ok(!/<(?:script|img|b)\b/i.test(yaml));
const html = FV.textView("<html><script>alert(1)</script><body onload=x></body></html>", { name: "page.html", id: "t7" }).html;
assert.ok(!/<(?:script|html|body)\b/i.test(html) && html.includes("never run"));
const text = FV.textView(evil + "\n", { name: "e.log", id: "t8" }).html;
assert.ok(!/<img/i.test(text));
const media = FV.mediaHtml("image", 'x" onerror="alert(1)', 'a"b');
assert.ok(!/onerror="alert/.test(media) && media.includes("&quot;"));
'''))
    assert out == "ok"


def test_json_folds_and_pretty_prints_and_a_cut_file_falls_back_to_text():
    out = _node(_fv(r'''
const html = FV.jsonHtml(JSON.stringify({ a: 1, b: { c: [1, 2, { d: null }], e: "x" }, f: true, g: [] }));
assert.ok(html.includes("<details"));
assert.ok(html.includes('<span class="fv-key">&quot;a&quot;</span>: <span class="fv-num">1</span>'));
assert.ok(html.includes('<span class="fv-str">&quot;x&quot;</span>') && html.includes('class="fv-null"') && html.includes('class="fv-bool"'));
assert.ok(html.includes("Expand all") && html.includes("Collapse all"));
assert.ok(html.includes("3 keys") || html.includes("4 keys"));
const cut = FV.textView('{"a": [1, 2, 3', { name: "c.json", id: "j1", shown: 14, total: 9999 }).html;
assert.ok(cut.includes("not complete JSON") && cut.includes('class="fv-text"') && cut.includes("Download the file for the rest"));
const big = FV.jsonHtml(JSON.stringify(Array.from({ length: 500 }, (_, i) => i)));
assert.ok(big.includes("300 more"));
'''))
    assert out == "ok"


def test_yaml_is_highlighted_and_other_text_gets_line_numbers():
    out = _node(_fv(r'''
const y = FV.yamlHtml('# comment\nname: demo\nsteps:\n  - id: 1\n    ok: true  # trailing\n  - "quoted"\nnone: null\n');
assert.ok(y.includes('<span class="fv-comment"># comment</span>'));
assert.ok(y.includes('<span class="fv-key">name</span>') && y.includes('<span class="fv-key">steps</span>'));
assert.ok(y.includes('<span class="fv-num">1</span>') && y.includes('<span class="fv-bool">true</span>') && y.includes('<span class="fv-null">null</span>'));
assert.equal((y.match(/class="fv-l"/g) || []).length, 7);
const t = FV.textHtml("one\ntwo\nthree\n");
assert.equal((t.match(/class="fv-l"/g) || []).length, 3);
assert.ok(!t.includes(">1<"));                                              // numbers come from CSS, not the copied text
'''))
    assert out == "ok"


# --- Markdown links and images (renderMarkdown) --------------------------------------------------------


def _md(body: str) -> str:
    source = APP.read_text(encoding="utf-8")
    start = source.index("const EMOJI_SYMBOL")
    emoji = source[start:source.index("function withSymbols(")]
    code = (_function(source, "escapeHtml") + emoji + _function(source, "withSymbols") + _function(source, "mdTables")
            + source[source.index("const MD_URL_ALLOWED"):source.index("function safeMdUrl(")]
            + _function(source, "safeMdUrl") + _function(source, "mdLinks") + _function(source, "renderMarkdown")
            + "const MD_BLOCK_OPEN = /\\n+(<(?:h2|h3|h4|ul|ol|table|pre|blockquote|hr)[\\s>])/g;\n"
              "const MD_BLOCK_CLOSE = /(<\\/(?:h2|h3|h4|ul|ol|table|pre|blockquote)>|<hr>)\\n+/g;\n")
    return _node('const assert = require("assert");\n' + code + body + '\nconsole.log("ok");')


def test_markdown_draws_links_and_images_only_for_own_files_and_https():
    out = _md(r'''
const own = renderMarkdown("![fig](/api/file/alice/run1/figures/umap.png) and [the report](/api/file/alice/run1/report/report.md)");
assert.ok(own.includes('<img class="md-img" src="/api/file/alice/run1/figures/umap.png" alt="fig"'));
assert.ok(own.includes('<a class="md-link" href="/api/file/alice/run1/report/report.md" target="_blank" rel="noopener noreferrer">the report</a>'));
const ext = renderMarkdown("[paper](https://doi.org/10.1/x?a=1&b=2) ![w](https://example.org/p.png)");
assert.ok(ext.includes('href="https://doi.org/10.1/x?a=1&amp;b=2"'));
// an image on another site is a link, never an <img> the browser would fetch when the reply shows
assert.ok(!/<img\b/.test(ext) && !/src=/.test(ext), ext);
assert.ok(ext.includes('<a class="md-link md-img-link" href="https://example.org/p.png" target="_blank" rel="noopener noreferrer">w</a>'), ext);
const leak = renderMarkdown("![](https://some.host/x.png?q=the%20patient%20id) ![a\"b](https://some.host/y.png)");
assert.ok(!/<img\b/.test(leak) && leak.includes('>https://some.host/x.png?q=the%20patient%20id</a>') && leak.includes(">a&quot;b</a>"), leak);
// the account's own chat images and dataset files are still drawn
assert.ok(renderMarkdown("![s](/api/chat-images/7/0123456789abcdef.png)").includes('<img class="md-img" src="/api/chat-images/7/0123456789abcdef.png"'));
const ds = renderMarkdown("[data](/api/datasets/12/download)");
assert.ok(ds.includes('href="/api/datasets/12/download"'));
// everything else is shown as plain text, never as a link or image
for (const bad of ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "http://example.org/x.png",
    "//evil.example/x.png", "/api/admin/users", "/static/app.js", "/api/file/alice/run1/../../bob/run2/x.png",
    "/api/file/alice", "https:///x", "ftp://example.org/f", "file:///etc/passwd", "vbscript:x", " javascript:alert(1)"]) {
  const html = renderMarkdown(`[click](${bad}) ![pic](${bad})`);
  assert.ok(!/<a\b|<img\b|href=|src=/.test(html), bad + " -> " + html);
  assert.ok(html.includes("click") && html.includes("pic"), bad);
}
''')
    assert out == "ok"


def test_a_relative_image_in_a_run_markdown_file_resolves_inside_that_run_only():
    out = _md(r'''
const base = "/api/file/alice/run1/report/report.md";
const ok = renderMarkdown("![umap](../figures/umap.png) ![same](chart.png)", { base });
assert.ok(ok.includes('src="/api/file/alice/run1/figures/umap.png"') && ok.includes('src="/api/file/alice/run1/report/chart.png"'));
const out = renderMarkdown("![a](../../run2/figures/x.png) ![b](../../../bob/run9/x.png) ![c](/api/file/bob/run9/x.png)", { base });
assert.ok(!/src="\/api\/file\/alice\/run2/.test(out) && !/src="\/api\/file\/bob\/run9\/x.png"[^>]*>.*src="\/api\/file\/bob\/run9\/x.png"/.test(out));
assert.ok(!out.includes("alice/run2"), out);
assert.ok(!out.includes("bob/run9/x.png") || out.includes('src="/api/file/bob/run9/x.png"'));   // an absolute path is the allow-list's to judge
// with no base a relative path means nothing
assert.ok(!/<img\b/.test(renderMarkdown("![x](figures/a.png)")));
''')
    assert out == "ok"


def test_the_written_report_reads_its_figures_from_the_run_folder():
    # report/report.md names its figures from the run's folder (figures/..., an MCP server's
    # extra/<server>/...), as the PDF was rendered; any other Markdown file from its own folder
    source = APP.read_text(encoding="utf-8")
    out = _md(_function(source, "fileMdBase") + r'''
const md = "![dot](extra/gene_expression/q1/dot.png) ![umap](figures/umap.png) ![up](../../run2/x.png)";
for (const name of ["report.md", "technical_report.md"]) {
  const html = renderMarkdown(md, { base: fileMdBase(`/api/file/alice/run1/report/${name}`) });
  assert.ok(html.includes('src="/api/file/alice/run1/extra/gene_expression/q1/dot.png"'), html);
  assert.ok(html.includes('src="/api/file/alice/run1/figures/umap.png"'), html);
  assert.ok(!html.includes("run2"), html);                      // still inside the run only
}
for (const url of ["/api/file/alice/run1/report/summary.md", "/api/file/alice/run1/extra/gene_expression/q1/report.md",
    "/api/file/alice/run1/report/sub/report.md"]) assert.equal(fileMdBase(url), url);
assert.equal(fileMdBase(undefined), undefined);
''')
    assert out == "ok"


def test_markdown_with_a_table_renders_and_hostile_markdown_runs_nothing():
    out = _md(r'''
const html = renderMarkdown("# Title\n\n| gene | n |\n|---|---|\n| A | 1 |\n\n![f](chart.png)\n", { base: "/api/file/a/r/x.md" });
assert.ok(html.includes("<table") && html.includes("<h2>Title</h2>") && html.includes('src="/api/file/a/r/chart.png"'));
const evil = renderMarkdown('<script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:alert(1)) ![a" onerror="alert(1)](https://example.org/a.png) [<b>t</b>](https://example.org/)');
assert.ok(!/<script|<img src=x|onerror=alert/i.test(evil.replace(/&lt;[^]*?&gt;/g, "")), evil);
assert.ok(!/<(?:script|b)\b/.test(evil));
assert.ok(!/ onerror="/.test(evil), evil);
// a link inside code stays code
assert.ok(renderMarkdown("`[a](https://example.org)`").includes("<code>[a](https://example.org)</code>"));
// underscores in a path are not turned into italics
assert.ok(renderMarkdown("![f](/api/file/a/r/_x_/fig_a_b.png)").includes('src="/api/file/a/r/_x_/fig_a_b.png"'));
''')
    assert out == "ok"
