"""Validate a ``croissant.json``: AiScientist's own rules always, mlcroissant's when it is installed.

The own rules are the ones a document must meet here whatever mlcroissant says: the required
Croissant properties, a checksum on every FileObject, a ``dataType`` and a resolvable source on
every Field, unique ``@id``s — and the development guideline's privacy rule, that no server path or
credential appears anywhere in the document.

mlcroissant (the ``metadata`` extra; CI installs it) is the reference validator. Without it the
gateway still validates with the own rules and says which validator ran.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from .facts import iso_now

_REQUIRED = ("name", "description", "license", "url", "creator", "datePublished", "distribution")
_SERVER_PATH = re.compile(r"(^|[\s\"'(=:])(/(data|home|dfs\d*|pub|tmp|scratch|opt|Users|var|srv|mnt)/|~/)")
_SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|aisci_[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY"
                     r"|(?i:password|passwd)\s*[:=])")


def _strings(node: Any, path: str = ""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            if k != "@context":
                yield from _strings(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")


def own_checks(doc: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if doc.get("@type") not in ("sc:Dataset", "https://schema.org/Dataset"):
        errors.append("@type must be sc:Dataset")
    conforms = doc.get("conformsTo")
    conforms = conforms if isinstance(conforms, list) else [conforms]
    if "http://mlcommons.org/croissant/1.1" not in conforms:
        errors.append("conformsTo must name http://mlcommons.org/croissant/1.1")
    for key in _REQUIRED:
        if not doc.get(key):
            errors.append(f"missing required property: {key}")
    ids: set[str] = set()
    for node in [*(doc.get("distribution") or []), *(doc.get("recordSet") or [])]:
        nid = str(node.get("@id") or "")
        if not nid:
            errors.append(f"{node.get('@type')} {node.get('name')!r} has no @id")
        elif nid in ids:
            errors.append(f"duplicate @id: {nid}")
        ids.add(nid)
        for field in node.get("field") or []:
            fid = str(field.get("@id") or "")
            if fid in ids:
                errors.append(f"duplicate @id: {fid}")
            ids.add(fid)
    files = {str(d.get("@id")): d for d in doc.get("distribution") or []}
    for d in doc.get("distribution") or []:
        kind = d.get("@type")
        if kind == "cr:FileObject":
            if not d.get("contentUrl"):
                errors.append(f"FileObject {d.get('@id')} has no contentUrl")
            if not (d.get("sha256") or d.get("md5")):
                errors.append(f"FileObject {d.get('@id')} has neither sha256 nor md5")
        elif kind == "cr:FileSet":
            if not d.get("includes"):
                errors.append(f"FileSet {d.get('@id')} has no includes")
        else:
            errors.append(f"distribution entry {d.get('@id')} is neither cr:FileObject nor cr:FileSet")
        if not d.get("encodingFormat"):
            errors.append(f"{kind} {d.get('@id')} has no encodingFormat")
    for rs in doc.get("recordSet") or []:
        if not rs.get("field"):
            errors.append(f"RecordSet {rs.get('@id')} has no field")
        for f in rs.get("field") or []:
            if not f.get("dataType"):
                errors.append(f"Field {f.get('@id')} has no dataType")
            src = f.get("source") or {}
            ref = (src.get("fileObject") or src.get("fileSet") or {}).get("@id")
            if ref and ref not in files:
                errors.append(f"Field {f.get('@id')} reads {ref}, which is not in distribution")
    for path, text in _strings(doc):
        if _SERVER_PATH.search(text):
            errors.append(f"{path} contains a server path; metadata must name files relative to the dataset")
        if _SECRET.search(text):
            errors.append(f"{path} looks like it contains a credential")
    if "how consent was obtained is not recorded" in str(doc.get("rai:personalSensitiveInformation") or "").lower():
        warnings.append("human data: how consent was obtained is not recorded")
    return errors, warnings


def mlcroissant_version() -> "str | None":
    try:
        from importlib.metadata import version
        import mlcroissant  # noqa: F401
        return version("mlcroissant")
    except Exception:  # noqa: BLE001 - not installed, or broken
        return None


def validate(doc: dict, *, use_mlcroissant: bool = True) -> dict:
    """``{"ok", "errors", "warnings", "validator", "checked_at"}`` for one document. Never raises."""
    try:
        errors, warnings = own_checks(doc)
    except Exception as exc:  # noqa: BLE001 - a malformed document is an error, not a crash
        errors, warnings = [f"could not check the document: {type(exc).__name__}: {exc}"], []
    validator = "aiscientist"
    version = mlcroissant_version() if use_mlcroissant else None
    if version:
        validator = f"aiscientist + mlcroissant {version}"
        try:
            import mlcroissant as mlc
            logging.getLogger("absl").setLevel(logging.ERROR)
            ds = mlc.Dataset(jsonld=copy.deepcopy(doc))
            warnings += sorted(str(w) for w in ds.metadata.ctx.issues.warnings)
        except Exception as exc:  # noqa: BLE001 - mlc.ValidationError and anything it raises on the way
            errors.append(f"mlcroissant: {str(exc).strip()[:2000]}")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "validator": validator,
            "checked_at": iso_now()}
