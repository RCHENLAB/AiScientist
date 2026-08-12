# Material Symbols — icon material library (素材库)

The console's one icon set, **bundled locally** so it renders offline / under a strict CSP (no
`fonts.googleapis.com` runtime dependency).

- **Source:** [Google Fonts · Material Symbols](https://fonts.google.com/icons) — **Rounded** style
  (closest to Apple SF Symbols / the Claude aesthetic).
- **File:** `material-symbols-rounded.woff2` — the **full variable font** (weight 100–700, plus the
  `FILL` / `GRAD` / `opsz` axes). Because it's the complete set, **any icon name on
  fonts.google.com/icons works** — no re-download to add one.
- **License:** Apache License 2.0 (Google). Redistribution + bundling permitted.

## How to use an icon

Put the icon's name (as shown on fonts.google.com/icons) as the text of a span with the icon class.
The `liga` feature turns the name into the glyph:

```html
<span class="material-symbols-outlined msym">science</span>
```

In JS use the helpers in `app.js`:

```js
icon("download")                 // -> a sized inline icon span
summaryRow("psychology", "…")    // -> a <details> summary: leading icon + label + rotating chevron
```

`@font-face` + the `.material-symbols-outlined` / `.material-symbols-rounded` / `.msym` classes and
the default `font-variation-settings` live in `../../styles.css`. (The class is still named
`…-outlined` for backwards compatibility, but it now points at the bundled **Rounded** font.)

## Icons currently used

| name | where |
|------|-------|
| `science` | nav rail (research) · run "Steps & code" summary |
| `psychology` | model "Thinking & activity" summary |
| `data_object` | "Step code" summary |
| `folder_open` | results/artifacts title |
| `download` | download-bundle links, zip button |
| `expand_more` | the rotating chevron on every collapsible `<details>` |
| `folder`, `bar_chart`, `extension`, `settings`, `person`, `add`, `upload_file`, `drive_folder_upload`, `search`, `keyboard_arrow_down` | nav rail, data menu, search (see `index.html`) |

## Updating / re-downloading

The woff2 is the variable font Google serves for the Rounded family. To refresh it:

```bash
# get the current woff2 URL (browser UA makes CSS2 return the variable font), then download it:
curl -s -A "Mozilla/5.0 … Chrome/120" \
  "https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200" \
  | grep -o 'https://[^)]*\.woff2'
curl -s -o material-symbols-rounded.woff2 "<that-url>"
```

It's ~5MB (the whole set). The gateway serves it with a long immutable `Cache-Control`
(`app.py` static route), so it's fetched once and cached — not re-downloaded per page load. If page
weight ever matters more than "every icon available", subset it with `fonttools`
(`pyftsubset … --unicodes=… --layout-features='liga,dlig,ccmp,locl,rlig'`) and swap the file in.
