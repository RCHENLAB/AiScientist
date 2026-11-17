"""Datasets a request seems to mean when the chat has none attached.

A request sent with no data used to go on without any ("No dataset attached"), or was refused at
planning. Here the request is read against the shared datasets and the account's own uploads; when
it points at some, the gateway asks which to use before anything is planned (``gateway/app.py``,
``_offer_unattached_data``). Shared datasets come first.

What counts as pointing at a dataset, deliberately narrow so a plain question is not interrupted:

* a shared dataset: its slug or name is in the request, or the request names one of its files, or a
  tissue or assay only its files carry (``shared_members.select``);
* an upload: its name (with or without the extension) is in the request, the request names its file
  type ("the VCF", "my h5ad"), or the request speaks of the user's own data ("my dataset", "the file
  I uploaded") — then the newest uploads are offered.

Pure functions over plain dicts; the database and the card are the caller's.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import shared_members as sm

MAX_SHARED = 4
MAX_OWN = 4
NONE_OPTION = "Continue without a dataset"

# File types a request names in words -> the endings an upload of that type has.
_TYPE_WORDS = {
    "vcf": (".vcf", ".vcf.gz", ".vcf.bgz", ".bcf"),
    "h5ad": (".h5ad",),
    "anndata": (".h5ad",),
    "loom": (".loom",),
    "h5": (".h5", ".hdf5"),
    "mtx": (".mtx", ".mtx.gz"),
    "bam": (".bam", ".cram"),
    "fastq": (".fastq", ".fastq.gz", ".fq", ".fq.gz"),
    "csv": (".csv", ".csv.gz"),
    "tsv": (".tsv", ".tsv.gz", ".txt"),
}
_OWN_DATA = re.compile(
    r"\b(?:my|our|the)\s+(?:own\s+)?(?:uploaded\s+)?(?:data(?:set)?s?|files?|uploads?|samples?)\b"
    r"|\b(?:i|we)\s+(?:just\s+)?uploaded\b|\buploaded\s+(?:data(?:set)?|files?)\b"
    r"|我(?:们)?(?:的|上传的|上传过的)(?:数据集?|文件|样本)|上传(?:的|过的)(?:数据集?|文件)",
    re.IGNORECASE)


@dataclass
class Offer:
    """One choice on the card. ``kind`` "shared": ``slug``, ``version`` (a label) and ``files`` (member
    names, or None for the whole version: the member card follows). ``kind`` "own": ``dataset_id``,
    ``path``, ``name``."""

    kind: str
    text: str
    slug: str = ""
    version: str = ""
    files: "list[str] | None" = None
    dataset_id: int = 0
    path: str = ""
    name: str = ""
    why: str = ""
    extra: dict = field(default_factory=dict)


def _mentions(question: str, word: str) -> bool:
    word = (word or "").strip()
    return bool(word) and re.search(
        rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", question, re.IGNORECASE) is not None


def _size(n: int) -> str:
    n = int(n or 0)
    if n >= 1e8:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e5:
        return f"{n / 1e6:.1f} MB"
    return f"{max(n, 0) / 1e3:.1f} kB" if n else ""


def shared_offers(question: str, datasets: "list[dict]") -> "list[Offer]":
    """``datasets``: ``[{"slug", "name", "label", "files": [{"name", "size_bytes", "facets"}]}]``, the
    latest published version of each, ``files`` being the ones a run can analyse."""
    question = question or ""
    out: list[Offer] = []
    for ds in datasets:
        files = list(ds.get("files") or [])
        if not files:
            continue
        title = f"{ds.get('name') or ds['slug']} {ds.get('label') or ''}".strip()
        named = _mentions(question, ds["slug"]) or _mentions(question, str(ds.get("name") or ""))
        sel = sm.select(question, files)
        narrowed = len(files) > 1 and (
            (sel.status == "chosen" and sel.chosen) or 0 < len(sel.candidates) < len(files))
        if not (named or narrowed):
            continue
        by_name = {f["name"]: f for f in files}
        if sel.status == "chosen" and sel.chosen and (len(files) > 1 or named):
            names = list(sel.chosen)
            text = " + ".join(sm.describe(by_name[n]) for n in names)
            out.append(Offer("shared", f"Shared · {title} · {text}", slug=ds["slug"],
                             version=str(ds.get("label") or ""), files=names, why=sel.how))
        elif narrowed:
            for n in sel.candidates[:MAX_SHARED]:
                out.append(Offer("shared", f"Shared · {title} · {sm.describe(by_name[n])}", slug=ds["slug"],
                                 version=str(ds.get("label") or ""), files=[n], why=sel.reason))
        else:
            out.append(Offer("shared", f"Shared · {title} ({len(files)} files; you choose the file next)",
                             slug=ds["slug"], version=str(ds.get("label") or ""), files=None,
                             why="the dataset is named in the request"))
    return out[:MAX_SHARED]


def own_offers(question: str, datasets: "list[dict]") -> "list[Offer]":
    """``datasets``: the account's uploads, ``[{"id", "name", "path", "size_bytes", "uploaded_at"}]``,
    newest first."""
    question = question or ""
    by_name, by_type = [], []
    for d in datasets:
        name = str(d.get("name") or "")
        base = os.path.basename(name.rstrip("/"))
        stem = base.split(".")[0]
        low = base.lower()
        if any(_mentions(question, w) for w in {base, stem} if len(w) >= 3):
            by_name.append((d, "named in the request"))
        elif any(_mentions(question, word) and low.endswith(ends) for word, ends in _TYPE_WORDS.items()):
            by_type.append((d, "its file type is named in the request"))
    picked = by_name + by_type
    if not picked and _OWN_DATA.search(question):
        picked = [(d, "your newest uploads") for d in datasets]
    out = []
    for d, why in picked[:MAX_OWN]:
        bits = [b for b in (_size(d.get("size_bytes") or 0), str(d.get("uploaded_at") or "")[:10]) if b]
        text = f"Your upload · {d['name']}" + (f" ({', '.join(bits)})" if bits else "")
        out.append(Offer("own", text, dataset_id=int(d["id"]), path=str(d.get("path") or ""),
                         name=str(d["name"]), why=why))
    return out


def offers(question: str, shared: "list[dict]", own: "list[dict]") -> "list[Offer]":
    """Every choice for the card, shared datasets first. Empty: the request points at none."""
    return shared_offers(question, shared) + own_offers(question, own)


def answer(feedback: str, choices: "list[Offer]") -> "Offer | None | bool":
    """What the card's answer picks: an :class:`Offer`; ``None`` for "go on without a dataset" (the
    chip, "no", "none", "skip", "without"); ``False`` when it says neither (the card is put again).
    The answer is a chip (``"<question>?: <option>"``), its number, or words typed in the box."""
    said = str(feedback or "").rsplit("?:", 1)[-1].strip()
    if not said:
        return False
    if said == NONE_OPTION or re.fullmatch(
            r"(?i)(?:no(?:ne)?|skip|without(?: (?:a |any )?(?:data(?:set)?|file))?|neither|不用|不需要|都不是|无)[.!。]?", said):
        return None
    for o in choices:
        if said == o.text:
            return o
    number = re.fullmatch(r"\s*(?:option\s*)?(\d+)\s*[.)]?\s*", said, re.IGNORECASE)
    if number:
        i = int(number.group(1))
        if 1 <= i <= len(choices):
            return choices[i - 1]
        return None if i == len(choices) + 1 else False
    hits = [o for o in choices
            if any(_mentions(said, w) for w in ([o.name, os.path.basename(o.name).split(".")[0]] if o.kind == "own"
                                               else [*(o.files or []), *[n.split(".")[0] for n in (o.files or [])], o.slug]))]
    return hits[0] if len(hits) == 1 else False
