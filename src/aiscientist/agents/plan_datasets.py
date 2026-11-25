"""The datasets a plan can name: the researcher's own uploads and the lab's shared datasets.

During plan review the planner is given this catalogue, so a reply such as "look for the datasets
again and write them into the plan" can be carried out: the planner names a real dataset in a step's
``dataset`` field (``plan_doc.DATASET_FIELDS``) instead of rewording the step with the reply. Code
checks every such name against the catalogue (``checker``), so a dataset that does not exist, a
version that was never published or a file the version does not hold is refused, never run.

The gateway builds the catalogue (``gateway.app._plan_dataset_catalogue``). An entry is::

    {"source": "own", "id": "<upload UUID>", "name": "retina_ko.h5ad", "kind": "file",
     "size_bytes": 123, "uploaded": "2026-11-02", "files": []}
    {"source": "shared", "id": "<slug>", "name": "HECA", "description": "...", "latest": "v2026.11.2",
     "versions": [{"label": "v2026.11.2", "status": "published",
                   "files": [{"name": "HRCA_snRNA_allcells.h5ad", "facets": {...}}]}]}

No server path is ever part of it: it goes into prompts and onto the plan card.

Pure data and helpers, no model call and no I/O.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

#: The most datasets of each kind, and the most files of one shared version, a prompt lists.
MAX_OWN = 40
MAX_SHARED = 20
MAX_FILES = 40


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize(entries: "Iterable[Mapping[str, Any]] | None") -> "list[dict[str, Any]]":
    """The catalogue entries that name a dataset, in their own order, each in the shape above."""
    out: list[dict[str, Any]] = []
    for e in entries or ():
        if not isinstance(e, Mapping):
            continue
        source = _text(e.get("source")).lower()
        name, ident = _text(e.get("name")), _text(e.get("id"))
        if source not in ("own", "shared") or not (name or ident):
            continue
        row: dict[str, Any] = {"source": source, "id": ident or name, "name": name or ident}
        if source == "own":
            row.update(kind=_text(e.get("kind")) or "file", size_bytes=int(e.get("size_bytes") or 0),
                       uploaded=_text(e.get("uploaded")),
                       files=[_text(f) for f in e.get("files") or () if _text(f)])
        else:
            versions = []
            for v in e.get("versions") or ():
                if not isinstance(v, Mapping) or not _text(v.get("label")):
                    continue
                files = [{"name": _text(f.get("name")),
                          "facets": dict(f.get("facets") or {}) if isinstance(f.get("facets"), Mapping) else {}}
                         for f in v.get("files") or () if isinstance(f, Mapping) and _text(f.get("name"))]
                versions.append({"label": _text(v["label"]), "status": _text(v.get("status")) or "published",
                                 "files": files})
            row.update(description=_text(e.get("description"))[:300], latest=_text(e.get("latest")),
                       versions=versions)
        out.append(row)
    return out


def _version(entry: Mapping[str, Any], label: str) -> "dict[str, Any] | None":
    wanted = (label or "").strip()
    if wanted.lower() in ("", "latest"):
        wanted = entry.get("latest") or ""
    return next((v for v in entry.get("versions") or () if v["label"] == wanted), None)


def _facets_line(facets: Mapping[str, Any]) -> str:
    bits = [str(facets[k]) for k in ("tissue", "modality") if facets.get(k)]
    if facets.get("n_obs") is not None:
        try:
            bits.append(f"{int(facets['n_obs']):,} cells")
        except (TypeError, ValueError):
            pass
    if facets.get("n_vars") is not None:
        try:
            bits.append(f"{int(facets['n_vars']):,} genes")
        except (TypeError, ValueError):
            pass
    cols = facets.get("obs_columns")
    if isinstance(cols, list) and cols:
        bits.append("obs: " + ", ".join(str(c) for c in cols[:12]) + (" …" if len(cols) > 12 else ""))
    return "; ".join(bits)


def catalogue_text(entries: "Iterable[Mapping[str, Any]] | None") -> str:
    """The catalogue for a prompt: every dataset the researcher can use, with the versions and files
    of each shared one. ``''`` when there is none."""
    rows = normalize(entries)
    own = [r for r in rows if r["source"] == "own"][:MAX_OWN]
    shared = [r for r in rows if r["source"] == "shared"][:MAX_SHARED]
    if not own and not shared:
        return ""
    lines = ["Datasets the researcher can use (name a step's data from this list only):"]
    if own:
        lines.append("Their own data (no versions; two that share a name are told apart by their id):")
        for r in own:
            size = f", {r['size_bytes'] / 1e9:.2f} GB" if r["size_bytes"] >= 1e8 else ""
            when = f", uploaded {r['uploaded']}" if r["uploaded"] else ""
            files = f"; files: {', '.join(r['files'][:12])}" if r["files"] else ""
            lines.append(f"  - {r['name']} ({r['kind']}{size}{when}) [id {r['id'][:8]}]{files}")
    if shared:
        lines.append("The lab's shared datasets (name, version, file):")
        for r in shared:
            labels = [v["label"] + (" (withdrawn)" if v["status"] == "withdrawn" else "")
                      for v in r["versions"]]
            lines.append(f"  - {r['name']} [{r['id']}]"
                         + (f" — {r['description']}" if r["description"] else "")
                         + (f"; versions: {', '.join(labels)}" if labels else "; no published version")
                         + (f"; latest: {r['latest']}" if r["latest"] else ""))
            latest = _version(r, "")
            for f in (latest or {}).get("files", [])[:MAX_FILES]:
                gist = _facets_line(f["facets"])
                lines.append(f"      {f['name']}" + (f" — {gist}" if gist else ""))
            if latest and len(latest["files"]) > MAX_FILES:
                lines.append(f"      … and {len(latest['files']) - MAX_FILES} more files")
    return "\n".join(lines)


def describe(ref: "Mapping[str, Any] | None") -> str:
    """A step's dataset in words, as the plan card and the chat say it: ``HECA v2026.11.1 ·
    HRCA_snRNA_allcells.h5ad``."""
    if not ref:
        return ""
    head = " ".join(x for x in (_text(ref.get("name")) or _text(ref.get("id")), _text(ref.get("version"))) if x)
    file = _text(ref.get("file"))
    return f"{head} · {file}" if file and file != _text(ref.get("name")) else head


def checker(entries: "Iterable[Mapping[str, Any]] | None") -> "Callable[[Mapping[str, str]], tuple[dict[str, str] | None, str]]":
    """A ``plan_doc.check_plan(datasets=...)`` check for this catalogue.

    It finds the dataset a step names, by name (or slug, or UUID), preferring the ``source`` written
    when two match, and gives it back as the catalogue names it: ``source`` and ``id`` filled in, a
    shared dataset's ``latest`` (or no version) read as the label it means now. ``(None, why)`` when
    no such dataset, version or file exists."""
    rows = normalize(entries)

    def check(ref: Mapping[str, str]) -> "tuple[dict[str, str] | None, str]":
        name = _text(ref.get("name"))
        ident = _text(ref.get("id"))
        source = _text(ref.get("source")).lower()
        said = name or ident

        def by_name(r: Mapping[str, Any]) -> bool:
            return bool(name) and name.casefold() in {r["name"].casefold(), r["id"].casefold()}

        def by_id(r: Mapping[str, Any]) -> bool:
            key, rid = ident.casefold(), r["id"].casefold()
            return bool(key) and (rid == key or (len(key) >= 8 and rid.startswith(key)))

        def narrow(found: "list[dict[str, Any]]") -> "list[dict[str, Any]]":
            return ([r for r in found if r["source"] == source] or found) if len(found) > 1 and source else found

        # The id first: two of the researcher's uploads may share a name. A name that names something
        # else than the id wins, though: it is the one a person edited.
        named, keyed = narrow([r for r in rows if by_name(r)]), narrow([r for r in rows if by_id(r)])
        hits = keyed if keyed and (not named or keyed[0] in named) else (named or keyed)
        if not hits:
            return None, (f"no dataset named {said!r} is in your data or the shared datasets"
                          if rows else "no datasets are available to this account")
        if len(hits) > 1 and all(r["source"] == "own" for r in hits):
            return None, (f"{len(hits)} of your datasets are named {said!r} ("
                          + "; ".join(f"id {r['id'][:8]}" + (f", uploaded {r['uploaded']}" if r.get("uploaded") else "")
                                      for r in hits[:4])
                          + "); give the step's dataset the id of the one to read")
        if len(hits) > 1:
            return None, (f"{said!r} names both one of your datasets and a shared dataset; "
                          "set its source to own or shared")
        entry = hits[0]
        out = {"name": entry["name"], "source": entry["source"], "id": entry["id"]}
        version, file = _text(ref.get("version")), _text(ref.get("file"))
        if entry["source"] == "own":
            if version and version.lower() != "latest":
                return None, f"{entry['name']} is your own dataset and has no versions (remove {version!r})"
            if file and file != entry["name"]:
                if entry["files"] and file not in entry["files"]:
                    return None, f"{entry['name']} has no file {file!r}"
                out["file"] = file
            return out, ""
        found = _version(entry, version)
        if found is None:
            return None, (f"{entry['name']} has no version {version!r}" if version and version.lower() != "latest"
                          else f"{entry['name']} has no published version yet")
        if found["status"] == "withdrawn":
            return None, f"{entry['name']} {found['label']} was withdrawn; choose another version"
        out["version"] = found["label"]
        names = [f["name"] for f in found["files"]]
        if file:
            if names and file not in names:
                close = [n for n in names if n.casefold() == file.casefold()]
                if not close:
                    return None, f"{entry['name']} {found['label']} has no file {file!r}"
                file = close[0]
            out["file"] = file
        elif len(names) == 1:
            out["file"] = names[0]
        return out, ""

    return check


def plan_datasets(steps: "Iterable[Any]") -> "list[dict[str, str]]":
    """The datasets a plan's steps name, each once, in the order the steps name them."""
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict[str, str]] = []
    for step in steps:
        ref = getattr(step, "dataset", None) if not isinstance(step, Mapping) else step.get("dataset")
        if not ref:
            continue
        key = (str(ref.get("source") or ""), str(ref.get("id") or ref.get("name") or ""),
               str(ref.get("version") or ""), str(ref.get("file") or ""))
        if key not in seen:
            seen.add(key)
            out.append(dict(ref))
    return out


def found_text(entries: "Iterable[Mapping[str, Any]] | None") -> str:
    """What a look-up found, for the chat: one line per dataset, ``''`` when it found none."""
    rows = normalize(entries)
    lines = []
    for r in rows[:MAX_OWN + MAX_SHARED]:
        if r["source"] == "own":
            lines.append(f"- {r['name']} (your data" + (f", uploaded {r['uploaded']}" if r["uploaded"] else "") + ")")
        else:
            latest = _version(r, "")
            n = len((latest or {}).get("files") or [])
            lines.append(f"- {r['name']} {r['latest'] or ''} (shared"
                         + (f", {n} file{'s' if n != 1 else ''}" if n else "") + ")")
    return "\n".join(line.replace("  (", " (") for line in lines)
