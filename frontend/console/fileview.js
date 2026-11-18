/* File viewer: one preview by file type, for every place a file is opened (RCHENLAB/AiScientist issue 115).
 *
 *   csv, tsv   a table: sticky header and row numbers, column types, horizontal scroll, row and column
 *              counts, "first N rows of M" when cut, click a header to sort the loaded rows
 *   md         rendered Markdown (the console's renderer, passed in) with a Rendered / Source switch
 *   json       pretty-printed, highlighted and foldable
 *   yaml       highlighted
 *   html       source only, never executed
 *   other text the text with line numbers
 *   image, pdf shown inside the panel
 *
 * Every function here returns an HTML string built from escaped text, or works on plain data: nothing
 * in a file is ever inserted as markup, so no preview runs HTML or script from a file. Loaded in the
 * browser as ``window.FileView`` and in node (the tests) with ``require``.
 */
(function (root) {
  "use strict";
  const FV = {};

  // The part of a file that is read for a preview. A longer file is cut, and the view says so.
  FV.LIMIT_BYTES = 1024 * 1024;
  FV.PAGE_ROWS = 200;

  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  FV.escape = esc;

  const EXT = {
    csv: "csv", tsv: "tsv", md: "md", markdown: "md", json: "json", yaml: "yaml", yml: "yaml",
    html: "html", htm: "html", png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image",
    svg: "image", pdf: "pdf",
  };
  const TEXT_EXT = new Set(["py", "txt", "text", "log", "toml", "ini", "cfg", "conf", "r", "sh", "js", "css",
    "xml", "tex", "bib", "dot", "vcf", "bed", "fasta", "fa", "sql", "ipynb"]);

  /** The view a file gets, by its name: csv, tsv, md, json, yaml, html, image, pdf, text, or "" (no preview). */
  FV.kind = function (name) {
    const ext = String(name || "").split("?")[0].split(".").pop().toLowerCase();
    if (EXT[ext]) return EXT[ext];
    return TEXT_EXT.has(ext) ? "text" : "";
  };

  // ---- cut notice ---------------------------------------------------------------------------------

  const fmtBytes = (n) => (n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB");
  FV.cutNotice = function (shown, total) {
    if (!total || shown >= total) return "";
    return `<p class="fv-note">Showing the first ${fmtBytes(shown)} of ${fmtBytes(total)}. Download the file for the rest.</p>`;
  };

  // ---- delimited text ------------------------------------------------------------------------------

  /** Parse comma- or tab-separated text (quotes, doubled quotes, CRLF, a byte-order mark). With ``cut``
   *  the text ended mid-file, so its last, possibly partial, line is dropped. */
  FV.parseDelimited = function (text, delim, cut) {
    text = String(text).replace(/^﻿/, "");
    const rows = [];
    let row = [], field = "", quoted = false, i = 0;
    const n = text.length;
    let lastRowStart = 0;
    while (i < n) {
      const c = text[i];
      if (quoted) {
        if (c === '"') {
          if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
          quoted = false; i++; continue;
        }
        field += c; i++; continue;
      }
      if (c === '"' && field === "") { quoted = true; i++; continue; }
      if (c === delim) { row.push(field); field = ""; i++; continue; }
      if (c === "\n" || c === "\r") {
        if (c === "\r" && text[i + 1] === "\n") i++;
        row.push(field); field = "";
        if (!(row.length === 1 && row[0] === "")) rows.push(row);
        row = []; i++; lastRowStart = i; continue;
      }
      field += c; i++;
    }
    const tail = lastRowStart < n;
    if (tail && !(cut)) { row.push(field); if (!(row.length === 1 && row[0] === "")) rows.push(row); }
    const columns = rows.length ? rows[0].map((c) => String(c)) : [];
    return { columns, rows: rows.slice(1) };
  };

  /** A type per column from its first values: integer, number, boolean, text or empty. */
  FV.columnTypes = function (columns, rows) {
    return columns.map((_c, k) => {
      let seen = 0, ints = 0, nums = 0, bools = 0;
      for (let r = 0; r < rows.length && seen < 200; r++) {
        const v = (rows[r][k] ?? "").trim();
        if (v === "") continue;
        seen++;
        if (/^[+-]?\d+$/.test(v)) { ints++; nums++; }
        else if (/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(v) || /^(?:nan|inf|-inf)$/i.test(v)) nums++;
        else if (/^(?:true|false)$/i.test(v)) bools++;
      }
      if (!seen) return "empty";
      if (ints === seen) return "integer";
      if (nums === seen) return "number";
      if (bools === seen) return "boolean";
      return "text";
    });
  };

  /** The loaded rows sorted by column ``col`` (``dir`` 1 or -1), each as ``[original index, row]``; empty
   *  cells last whichever way. ``dir`` 0 keeps the file's order. */
  FV.sortedRows = function (rows, col, dir, type) {
    const indexed = rows.map((r, i) => [i, r]);
    if (!dir || col < 0) return indexed;
    const num = type === "integer" || type === "number";
    const key = (r) => { const v = (r[col] ?? "").trim(); return num ? parseFloat(v) : v.toLowerCase(); };
    return indexed.sort((a, b) => {
      const x = key(a[1]), y = key(b[1]);
      const ex = num ? Number.isNaN(x) : x === "", ey = num ? Number.isNaN(y) : y === "";
      if (ex || ey) return ex === ey ? a[0] - b[0] : ex ? 1 : -1;
      const c = num ? x - y : (x < y ? -1 : x > y ? 1 : 0);
      return c ? c * dir : a[0] - b[0];
    });
  };

  /** A CSV view's state: the parsed table plus how it is sorted and how many rows are shown. */
  FV.tableState = function (columns, rows, opts) {
    const o = opts || {};
    return { columns, rows, types: o.types || FV.columnTypes(columns, rows), sort: { col: -1, dir: 0 },
             shown: FV.PAGE_ROWS, totalRows: o.totalRows ?? null, cut: !!o.cut, index: o.index || null };
  };

  /** The table: a sticky header (with each column's type and a sort mark) and sticky row numbers. */
  FV.tableHtml = function (st) {
    const { columns, rows, types, sort } = st;
    if (!columns.length) return '<div class="fv-note">This file has no columns.</div>';
    const head = '<tr><th class="ds-idx" title="Row number"></th>' + columns.map((c, k) => {
      const mark = sort.col === k ? (sort.dir > 0 ? " ▲" : " ▼") : "";
      return `<th data-fv-sort="${k}" tabindex="0" role="columnheader" aria-sort="${sort.col === k ? (sort.dir > 0 ? "ascending" : "descending") : "none"}">` +
        `${esc(c)}${mark}<span class="ds-type">${esc(types[k] || "")}</span></th>`;
    }).join("") + "</tr>";
    const ordered = FV.sortedRows(rows, sort.col, sort.dir, types[sort.col]);
    const shown = ordered.slice(0, st.shown);
    const body = shown.map(([i, r]) => `<tr><th class="ds-idx">${esc(st.index ? (st.index[i] ?? "") : i + 1)}</th>` +
      columns.map((_c, k) => `<td>${esc(r[k] ?? "")}</td>`).join("") + "</tr>").join("");
    const counts = `${columns.length.toLocaleString()} ${columns.length === 1 ? "column" : "columns"}, ` +
      (st.cut || (st.totalRows != null && st.totalRows > rows.length)
        ? `${rows.length.toLocaleString()} rows loaded${st.totalRows != null ? ` of ${st.totalRows.toLocaleString()}` : " (the file is longer)"}`
        : `${rows.length.toLocaleString()} ${rows.length === 1 ? "row" : "rows"}`);
    const more = ordered.length > st.shown
      ? `<button type="button" class="ghost small" data-fv-more>Show ${Math.min(FV.PAGE_ROWS, ordered.length - st.shown)} more rows</button>` : "";
    return `<div class="fv-meta">${esc(counts)}${shown.length < ordered.length ? ` · showing the first ${shown.length.toLocaleString()} rows` : ""}` +
      `${sort.dir ? " · sorted within the loaded rows" : ""}</div>` +
      `<div class="ds-table-wrap fv-table-wrap"><table class="ds-table fv-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>${more}`;
  };

  // ---- JSON ----------------------------------------------------------------------------------------

  const MAX_CHILDREN = 200;
  function jsonNode(value, key, depth) {
    const k = key === null ? "" : `<span class="fv-key">${esc(JSON.stringify(key))}</span>: `;
    if (value === null) return `<div class="fv-line">${k}<span class="fv-null">null</span></div>`;
    if (Array.isArray(value) || typeof value === "object") {
      const arr = Array.isArray(value);
      const entries = arr ? value.map((v, i) => [i, v]) : Object.entries(value);
      const open = arr ? "[" : "{", close = arr ? "]" : "}";
      if (!entries.length) return `<div class="fv-line">${k}<span class="fv-punct">${open}${close}</span></div>`;
      const kids = entries.slice(0, MAX_CHILDREN).map(([ck, cv]) => jsonNode(cv, arr ? null : ck, depth + 1)).join("");
      const rest = entries.length > MAX_CHILDREN ? `<div class="fv-line fv-dim">… ${entries.length - MAX_CHILDREN} more</div>` : "";
      return `<details class="fv-fold"${depth < 2 ? " open" : ""}><summary>${k}<span class="fv-punct">${open}</span>` +
        `<span class="fv-dim"> ${entries.length} ${arr ? (entries.length === 1 ? "item" : "items") : (entries.length === 1 ? "key" : "keys")} </span>` +
        `<span class="fv-punct fv-close-inline">${close}</span></summary><div class="fv-kids">${kids}${rest}</div>` +
        `<div class="fv-line"><span class="fv-punct">${close}</span></div></details>`;
    }
    const cls = typeof value === "string" ? "fv-str" : typeof value === "number" ? "fv-num" : "fv-bool";
    return `<div class="fv-line">${k}<span class="${cls}">${esc(JSON.stringify(value))}</span></div>`;
  }

  /** Foldable, highlighted JSON. Text that does not parse (a file that was cut, say) is shown as text. */
  FV.jsonHtml = function (text) {
    let value;
    try { value = JSON.parse(text); } catch (err) {
      return `<p class="fv-note">This is not complete JSON (${esc(err.message)}), so it is shown as text.</p>` + FV.textHtml(text);
    }
    return `<div class="fv-tools"><button type="button" class="ghost small" data-fv-fold="open">Expand all</button>` +
      `<button type="button" class="ghost small" data-fv-fold="close">Collapse all</button></div>` +
      `<div class="fv-json">${jsonNode(value, null, 0)}</div>`;
  };

  // ---- YAML and plain text -------------------------------------------------------------------------

  function yamlLine(line) {
    const comment = /^(\s*)(#.*)$/.exec(line);
    if (comment) return esc(comment[1]) + `<span class="fv-comment">${esc(comment[2])}</span>`;
    const m = /^(\s*(?:-\s+)?)([^\s#:'"][^:#]*?|"[^"]*"|'[^']*')(\s*:)(\s+|$)(.*)$/.exec(line);
    if (!m) return highlightScalar(line);
    const [, lead, key, colon, gap, rest] = m;
    return esc(lead) + `<span class="fv-key">${esc(key)}</span><span class="fv-punct">${esc(colon)}</span>${esc(gap)}${highlightScalar(rest)}`;
  }
  function highlightScalar(s) {
    const m = /^(.*?)(\s+#.*)?$/.exec(s);
    const value = m[1], comment = m[2] || "";
    const t = value.trim();
    let cls = "";
    if (/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(t)) cls = "fv-num";
    else if (/^(?:true|false|yes|no|on|off)$/i.test(t)) cls = "fv-bool";
    else if (/^(?:null|~)$/i.test(t)) cls = "fv-null";
    else if (/^(["']).*\1$/.test(t)) cls = "fv-str";
    const body = cls ? esc(value).replace(esc(t), `<span class="${cls}">${esc(t)}</span>`) : esc(value);
    return body + (comment ? `<span class="fv-comment">${esc(comment)}</span>` : "");
  }

  function numbered(lines, fn) {
    return `<pre class="fv-text"><code>` + lines.map((l) => `<span class="fv-l">${fn(l)}</span>`).join("\n") + `</code></pre>`;
  }
  /** Highlighted YAML with line numbers. */
  FV.yamlHtml = function (text) { return numbered(String(text).replace(/\n$/, "").split("\n"), yamlLine); };
  /** The text with line numbers (the numbers are not part of the copied text). */
  FV.textHtml = function (text) { return numbered(String(text).replace(/\n$/, "").split("\n"), esc); };

  // ---- Markdown ------------------------------------------------------------------------------------

  /** Rendered Markdown with a Rendered / Source switch. ``render`` is the console's renderer. */
  FV.markdownHtml = function (text, render, id) {
    return `<div class="fv-switch" role="tablist"><button type="button" class="ghost small active" data-fv-mode="rendered" data-fv-for="${esc(id)}" role="tab" aria-selected="true">Rendered</button>` +
      `<button type="button" class="ghost small" data-fv-mode="source" data-fv-for="${esc(id)}" role="tab" aria-selected="false">Source</button></div>` +
      `<div class="fv-md" data-fv-pane="rendered">${render(text)}</div>` +
      `<div class="fv-src hidden" data-fv-pane="source">${FV.textHtml(text)}</div>`;
  };

  // ---- media ---------------------------------------------------------------------------------------

  /** An image or a PDF inside the panel. The image opens full size on click (``data-fv-open``). */
  FV.mediaHtml = function (kind, url, name) {
    if (kind === "image") return `<div class="fv-media"><img class="fv-img" src="${esc(url)}" alt="${esc(name)}" data-fv-open="${esc(url)}" data-fv-name="${esc(name)}" title="Click to open full size" /></div>`;
    return `<iframe class="fv-pdf" src="${esc(url)}" title="${esc(name)}"></iframe>`;
  };

  // ---- one entry point -----------------------------------------------------------------------------

  /** The HTML for a file's text. ``info``: ``{name, id, shown, total, render}``: ``shown``/``total`` are the bytes
   *  read and the file's size (a cut notice follows when they differ); ``render`` renders Markdown.
   *  Returns ``{html, state}``: ``state`` is the table's state for a csv/tsv, to be kept for sorting. */
  FV.textView = function (text, info) {
    const kind = FV.kind(info.name) || "text";
    const cut = info.total && info.shown && info.shown < info.total;
    const note = FV.cutNotice(info.shown, info.total);
    if (kind === "csv" || kind === "tsv") {
      const parsed = FV.parseDelimited(text, kind === "tsv" ? "\t" : ",", cut);
      const state = FV.tableState(parsed.columns, parsed.rows, { cut });
      return { html: `<div data-fv-table="${esc(info.id)}">${FV.tableHtml(state)}</div>${note}`, state, id: info.id };
    }
    if (kind === "md" && info.render) return { html: FV.markdownHtml(text, info.render, info.id) + note, state: null, id: info.id };
    if (kind === "json") return { html: FV.jsonHtml(text) + note, state: null, id: info.id };
    if (kind === "yaml") return { html: FV.yamlHtml(text) + note, state: null, id: info.id };
    if (kind === "html") return { html: '<p class="fv-note">HTML is shown as source and never run.</p>' + FV.textHtml(text) + note, state: null, id: info.id };
    return { html: FV.textHtml(text) + note, state: null, id: info.id };
  };

  if (typeof module !== "undefined" && module.exports) module.exports = FV;
  else root.FileView = FV;
})(typeof window !== "undefined" ? window : globalThis);
