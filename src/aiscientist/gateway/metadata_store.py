"""Croissant metadata in the registry: drafting, review, results, lineage, search, access tokens.

RCHENLAB/AiScientist issue 9. :mod:`aiscientist.metadata` builds and validates the documents; this
module keeps them in the account database beside the dataset rows and answers the questions the web
interface and the MCP server ask. Every read takes a ``user_id`` and is filtered on it, so a caller
can only ever see its own account's datasets and results — the same rule as ``/api/datasets``.

What is stored per dataset or result (:class:`~.models.MetadataRecord`): the facts measured when
it was made (files, sizes, checksums, table columns; for a result, its run), the fields the owner
entered in the review form, and the ``croissant.json`` rebuilt from both, with the per-field
provenance and confidence the drafting reported. Lineage edges (:class:`~.models.MetadataLink`)
say which datasets and earlier results a result was made from.

Drafting is best-effort by design, like the dataset history it describes: an upload or a run must
never fail because its metadata could not be written. A dataset left without a record (a drafting
error, or a row older than this feature) is drafted by :func:`ensure_dataset_records`, which the
gateway runs at start and on its scheduled re-check.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
import uuid as _uuid
from typing import Any, Iterable

from sqlalchemy import func, or_, select

from .. import metadata as md
from .db import session_scope
from .models import AccessToken, Dataset, LlmCall, MetadataLink, MetadataRecord, Run, User

TOKEN_PREFIX = "aisci_"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def base_url() -> str:
    """The public address of this deployment (``AISCIENTIST_PUBLIC_URL``), used for the ``url`` of
    each dataset; '' when it is not set."""
    return (os.environ.get("AISCIENTIST_PUBLIC_URL") or "").strip().rstrip("/")


def _clean_ref(ref: str) -> str:
    ref = (ref or "").strip()
    return ref[len("urn:uuid:"):] if ref.lower().startswith("urn:uuid:") else ref


def _find(s, user_id: int, ref: str, kind: "str | None" = None) -> "MetadataRecord | None":
    """The caller's record named by ``ref``: a UUID, a dataset's integer id or a run id."""
    ref = _clean_ref(ref)
    if not ref:
        return None
    q = select(MetadataRecord).where(MetadataRecord.user_id == user_id)
    if kind:
        q = q.where(MetadataRecord.kind == kind)
    conds = [MetadataRecord.uuid == ref.lower(), MetadataRecord.run_id == ref]
    if ref.isdigit():
        conds.append(MetadataRecord.dataset_id == int(ref))
    return s.scalars(q.where(or_(*conds)).limit(1)).first()


# --- access tokens (MCP clients the user allows) --------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(user_id: int, name: str) -> tuple[str, dict]:
    """A new personal access token for ``user_id``. Returns ``(token, its public row)``; the token
    itself is never stored, so this is the only time it can be shown."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with session_scope() as s:
        row = AccessToken(user_id=user_id, name=(name or "MCP client").strip()[:64] or "MCP client",
                          token_hash=_hash_token(token), prefix=token[:12], scope="mcp:read")
        s.add(row)
        s.commit()
        return token, row.public()


def list_tokens(user_id: int) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(AccessToken).where(AccessToken.user_id == user_id,
                                                   AccessToken.revoked_at.is_(None))
                         .order_by(AccessToken.id.desc())).all()
        return [r.public() for r in rows]


def revoke_token(user_id: int, token_id: int) -> bool:
    with session_scope() as s:
        row = s.get(AccessToken, token_id)
        if row is None or row.user_id != user_id or row.revoked_at is not None:
            return False
        row.revoked_at = _now()
        s.commit()
        return True


def user_for_token(token: str) -> "User | None":
    """The ACTIVE account a token acts for, or None (unknown, revoked, or the account disabled)."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    with session_scope() as s:
        row = s.scalars(select(AccessToken).where(AccessToken.token_hash == _hash_token(token))).first()
        if row is None or row.revoked_at is not None:
            return None
        user = s.get(User, row.user_id)
        if user is None or not user.is_active:
            return None
        row.last_used_at = _now()
        s.commit()
        s.expunge(user)
        return user


def authenticate(request) -> "User | None":
    """The account an MCP request acts as: a bearer token, else the console's session cookie."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return user_for_token(header[7:].strip())      # a bad token never falls back to the cookie
    from . import auth_routes
    try:
        return auth_routes.current_user(request)
    except Exception:  # noqa: BLE001
        return None


# --- datasets --------------------------------------------------------------------------------------------


def _store_built(row: MetadataRecord, built: "md.Built") -> None:
    row.document = _dumps(built.document)
    row.provenance = _dumps(built.provenance)
    row.confidence = _dumps(built.confidence)
    row.facets = _dumps(built.facets)
    row.search_text = built.search_text
    row.name = str(built.document.get("name") or row.name or "")[:255]
    row.validation = _dumps(md.validate(built.document))


def _dataset_facts(d: Dataset, owner: str, *, files: "list[dict] | None", peek: "dict | None",
                   source: str, derived_from: "dict | None") -> dict:
    return md.dataset_facts(
        name=d.name, kind=d.kind, size_bytes=int(d.size_bytes or 0), sha256=d.sha256,
        uploaded_at=d.uploaded_at.isoformat() if d.uploaded_at else _now().isoformat(),
        owner=owner, files=files, peek=peek, source=source, derived_from=derived_from)


def draft_dataset_record(dataset_id: int, *, files: "list[dict] | None" = None, peek: "dict | None" = None,
                         source: str = "upload", derived_from: "dict | None" = None) -> "str | None":
    """Draft (or re-draft from new facts) the Croissant metadata of one dataset row. Keeps what the
    owner already entered. Returns the dataset's UUID, or None when the row is gone. Never raises."""
    try:
        with session_scope() as s:
            d = s.get(Dataset, dataset_id)
            if d is None:
                return None
            if not d.uuid:
                d.uuid = str(_uuid.uuid4())
            owner = s.get(User, d.user_id)
            facts = _dataset_facts(d, owner.username if owner else "", files=files, peek=peek,
                                   source=source, derived_from=derived_from)
            row = s.scalars(select(MetadataRecord).where(MetadataRecord.uuid == d.uuid)).first()
            if row is None:
                row = MetadataRecord(uuid=d.uuid, kind="dataset", user_id=d.user_id, dataset_id=d.id,
                                     name=d.name[:255])
                s.add(row)
            user_fields = row.loads("user_fields") if row.user_fields else {}
            row.facts = _dumps(facts)
            _store_built(row, md.build_dataset(d.uuid, facts, user_fields, base_url=base_url(),
                                               description=row.loads("description")))
            if derived_from and derived_from.get("uuid"):
                _link(s, d.user_id, str(derived_from["uuid"]), d.uuid, "saved_from")
            s.commit()
            return d.uuid
    except Exception as exc:  # noqa: BLE001 - metadata must never fail an upload
        print(f"[metadata] drafting dataset {dataset_id} failed: {type(exc).__name__}: {exc}")
        return None


def ensure_dataset_records(user_id: "int | None" = None) -> int:
    """Draft metadata for every dataset row that has none (older rows, or a drafting that failed).
    These have only the registry's facts — name, size, kind, checksum — so they are marked as such.
    Returns how many were drafted."""
    with session_scope() as s:
        q = (select(Dataset.id).outerjoin(MetadataRecord, MetadataRecord.dataset_id == Dataset.id)
             .where(MetadataRecord.id.is_(None)))
        if user_id is not None:
            q = q.where(Dataset.user_id == user_id)
        missing = list(s.scalars(q).all())
    done = 0
    for dataset_id in missing:
        if draft_dataset_record(dataset_id, source="registry"):
            done += 1
    return done


def delete_for_dataset(s, dataset_id: int) -> None:
    """Remove a dataset's metadata and its lineage edges, inside the caller's session, so the row
    and its metadata go in one commit. Results that used the dataset keep their own records: their
    provenance still names the input by UUID, and the lineage view then says it was deleted."""
    for row in s.scalars(select(MetadataRecord).where(MetadataRecord.dataset_id == dataset_id)).all():
        for link in s.scalars(select(MetadataLink).where(MetadataLink.child_uuid == row.uuid)).all():
            s.delete(link)
        s.delete(row)


def merge_folder_files(dataset_id: int, files: "list[dict] | None") -> "list[dict] | None":
    """The file list of a folder dataset after ``files`` were added to it: what earlier uploads
    recorded (their checksums kept), updated by path with the new ones. ``None`` when neither
    says anything, so the draft falls back to the registry row."""
    with session_scope() as s:
        row = s.scalars(select(MetadataRecord).where(MetadataRecord.dataset_id == dataset_id)).first()
        old = (row.loads("facts").get("files") or []) if row is not None else []
    if not old and not files:
        return None
    merged = {str(f.get("path")): f for f in old if isinstance(f, dict) and f.get("path")}
    for f in files or []:
        if isinstance(f, dict) and f.get("path"):
            merged[md.file_fact(str(f["path"]), None)["path"]] = f
    return list(merged.values())


# --- review form -------------------------------------------------------------------------------------


def save_form(user_id: int, ref: str, fields: dict) -> "dict | None":
    """Apply the owner's review form to a dataset's metadata and rebuild its croissant.json. Returns
    the new view, None when the caller has no such dataset. Raises ``ValueError`` on a bad field."""
    clean = md.clean_user_fields(fields)                      # raises ValueError
    with session_scope() as s:
        row = _find(s, user_id, ref, kind="dataset")
        if row is None:
            return None
        merged = {**row.loads("user_fields"), **clean}
        row.user_fields = _dumps(merged)
        row.status = "reviewed"
        _store_built(row, md.build_dataset(row.uuid, row.loads("facts"), merged, base_url=base_url(),
                                           description=row.loads("description")))
        d = s.get(Dataset, row.dataset_id) if row.dataset_id else None
        if d is not None and clean.get("name"):
            d.name = clean["name"][:255]                       # the display name, everywhere
        s.commit()
        return _view(s, row, with_form=True)


# --- the dataset description, DATASET.md (issue 10) ----------------------------------------------

#: The longest DATASET.md an owner may save.
MAX_DESCRIPTION_CHARS = 100_000


def _description_state(row: MetadataRecord) -> dict:
    """What a dataset's DATASET.md is now: the owner's edit if there is one, else the PI's, as
    ``{"state", "markdown", ...}``; ``state`` is ``owner``, ``pi`` or ``none``."""
    from ..metadata import description as dmd
    rec = row.loads("description")
    facts = row.loads("facts")
    current_key = dmd.profile_key(facts)
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    pi_md = (dmd.render(dmd.from_record(rec, facts, row.loads("user_fields"), name=row.name))
             if rec.get("written_at") else "")
    out: dict[str, Any] = {
        "uuid": row.uuid, "dataset_id": row.dataset_id, "name": row.name,
        "state": "owner" if owner.get("markdown") else "pi" if pi_md else "none",
        "markdown": owner.get("markdown") or pi_md,
        "written_at": rec.get("written_at"), "model": rec.get("model"), "run_id": rec.get("run_id"),
        # The data no longer matches what the description was written from (files added, a file
        # replaced): the next run that reads the dataset writes the PI's part again.
        "stale": bool(pi_md) and rec.get("profile_key") != current_key,
    }
    if owner.get("markdown"):
        out.update(owner_edited_at=owner.get("edited_at"), owner_edited_by=owner.get("by"),
                   data_changed_after_edit=owner.get("profile_key") not in (None, current_key),
                   pi_markdown=pi_md)
    return out


def _rebuild(row: MetadataRecord) -> None:
    _store_built(row, md.build_dataset(row.uuid, row.loads("facts"), row.loads("user_fields"),
                                       base_url=base_url(), description=row.loads("description")))


def description_view(user_id: int, ref: str) -> "dict | None":
    """The caller's dataset's DATASET.md and its state, None when the caller has no such dataset."""
    with session_scope() as s:
        row = _find(s, user_id, ref, kind="dataset")
        return None if row is None else _description_state(row)


def description_for_run(user_id: int, uuid: str) -> "dict | None":
    """What a run starting on the dataset ``uuid`` needs: its DATASET.md if one is kept and still
    describes the data, and the dataset's facts and owner's fields for the PI to write one when it
    is not. None when the caller has no such dataset (or no record of it yet)."""
    with session_scope() as s:
        row = _find(s, user_id, uuid, kind="dataset")
        if row is None:
            return None
        view = _description_state(row)
        facts = row.loads("facts")
        return {**view, "facts": {k: facts.get(k) for k in ("name", "kind", "size_bytes", "files",
                                                             "file_count", "accessions")},
                "user_fields": row.loads("user_fields")}


def save_pi_description(user_id: int, uuid: str, *, profile: dict, inferred: dict, model: str,
                        written_at: str, run_id: str) -> bool:
    """Keep what the PI wrote about a dataset (its profile and statements; issue 10) and rebuild the
    croissant.json from it. The owner's edit, if any, is kept as it is. Never raises: a run must not
    fail over its description."""
    from ..metadata import description as dmd
    try:
        with session_scope() as s:
            row = _find(s, user_id, uuid, kind="dataset")
            if row is None:
                return False
            rec = row.loads("description")
            rec.update(version=dmd.VERSION, profile=profile, inferred=inferred, model=model,
                       written_at=written_at, run_id=run_id,
                       profile_key=dmd.profile_key(row.loads("facts")))
            row.description = _dumps(rec)
            _rebuild(row)
            s.commit()
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[metadata] the description of {uuid} was not kept: {type(exc).__name__}: {exc}")
        return False


def save_owner_description(user_id: int, ref: str, markdown: str, *, by: str = "") -> "dict | None":
    """The owner's own DATASET.md. Kept as written; a later run reads it and never overwrites it.
    Raises ValueError for an empty or over-long text; None when the caller has no such dataset."""
    from ..metadata import description as dmd
    text = (markdown or "").replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("The description is empty. To go back to the PI's version, reset it instead.")
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise ValueError(f"The description is longer than {MAX_DESCRIPTION_CHARS:,} characters.")
    with session_scope() as s:
        row = _find(s, user_id, ref, kind="dataset")
        if row is None:
            return None
        rec = row.loads("description")
        rec["owner"] = {"markdown": text + "\n", "by": by,
                        "edited_at": _now().isoformat(timespec="seconds"),
                        "profile_key": dmd.profile_key(row.loads("facts"))}
        row.description = _dumps(rec)
        _rebuild(row)
        s.commit()
        return _description_state(row)


def reset_owner_description(user_id: int, ref: str) -> "dict | None":
    """Drop the owner's edit: DATASET.md is the PI's again (or none)."""
    with session_scope() as s:
        row = _find(s, user_id, ref, kind="dataset")
        if row is None:
            return None
        rec = row.loads("description")
        rec.pop("owner", None)
        row.description = _dumps(rec) if rec else None
        _rebuild(row)
        s.commit()
        return _description_state(row)


# --- results ----------------------------------------------------------------------------------------


def result_uuid(run_id: str) -> str:
    return md.result_uuid(run_id)


def record_result(user_id: int, run_id: str, facts: dict, inputs: Iterable[dict]) -> "dict | None":
    """Store (or replace, for a run that was continued) the Croissant metadata of a run's result and
    its lineage edges. Returns the document, or None if it could not be stored. Never raises."""
    try:
        uid = result_uuid(run_id)
        inputs = [i for i in inputs if isinstance(i, dict) and i.get("uuid")]
        with session_scope() as s:
            models = sorted({m for (m,) in s.execute(select(LlmCall.model).where(
                LlmCall.run_id == run_id, LlmCall.model != "").distinct()).all() if m})
            run = s.scalars(select(Run).where(Run.run_id == run_id)).first()
            facts = {**facts, "run_id": run_id,
                     "models": sorted(set(facts.get("models") or []) | set(models)),
                     "started_at": facts.get("started_at") or (
                         run.created_at.isoformat() if run is not None and run.created_at else None),
                     "inputs": [{k: i.get(k) for k in ("uuid", "name", "kind", "role")} for i in inputs]}
            row = s.scalars(select(MetadataRecord).where(MetadataRecord.uuid == uid)).first()
            if row is None:
                row = MetadataRecord(uuid=uid, kind="result", user_id=user_id, run_id=run_id)
                s.add(row)
            elif row.user_id != user_id:
                return None
            row.facts = _dumps(facts)
            row.status = "generated"
            built = md.build_result(uid, facts, base_url=base_url())
            _store_built(row, built)
            for old in s.scalars(select(MetadataLink).where(MetadataLink.child_uuid == uid,
                                                            MetadataLink.relation == "input")).all():
                s.delete(old)
            for i in inputs:
                _link(s, user_id, str(i["uuid"]), uid, "input")
            s.commit()
            return built.document
    except Exception as exc:  # noqa: BLE001 - metadata must never fail a run
        print(f"[metadata] result metadata for run {run_id} failed: {type(exc).__name__}: {exc}")
        return None


def note_handed_on(user_id: int, run_id: str, records: "dict[str, dict]", *,
                   staged_at: str = "") -> "dict | None":
    """A run's handed-on outputs were copied from HPC3 to the run's folder on the server (issue 6):
    say so in its result's metadata. ``records`` maps an output's name to its record (``location``,
    ``sha256``, ``saved_to``). Returns the rebuilt document, or None (no record, or not the caller's).
    Never raises."""
    try:
        uid = result_uuid(run_id)
        with session_scope() as s:
            row = s.scalars(select(MetadataRecord).where(MetadataRecord.uuid == uid)).first()
            if row is None or row.user_id != user_id:
                return None
            facts = json.loads(row.facts or "{}")
            changed = False
            for h in facts.get("handed_on") or []:
                rec = records.get(str(h.get("name") or "")) or {}
                if rec.get("location") == "gateway" or rec.get("saved_to"):
                    h["location"] = "server"
                    if rec.get("sha256"):
                        h["sha256"] = str(rec["sha256"])
                    if staged_at:
                        h["staged_out_at"] = staged_at
                    changed = True
            if not changed:
                return None
            row.facts = _dumps(facts)
            built = md.build_result(uid, facts, base_url=base_url())
            _store_built(row, built)
            s.commit()
            return built.document
    except Exception as exc:  # noqa: BLE001 - metadata must never fail a stage-out
        print(f"[metadata] stage-out of run {run_id} not recorded: {type(exc).__name__}: {exc}")
        return None


def delete_for_run(s, user_id: int, run_id: str) -> None:
    """Remove a run's result metadata and the lineage edges into it, inside the caller's session, so
    the run's row and its metadata go in one commit. A dataset saved from the result keeps its own
    record; its lineage then says the result was deleted."""
    for row in s.scalars(select(MetadataRecord).where(MetadataRecord.user_id == user_id,
                                                      MetadataRecord.run_id == run_id,
                                                      MetadataRecord.kind == "result")).all():
        for link in s.scalars(select(MetadataLink).where(MetadataLink.child_uuid == row.uuid)).all():
            s.delete(link)
        s.delete(row)


def _link(s, user_id: int, parent: str, child: str, relation: str) -> None:
    exists = s.scalars(select(MetadataLink.id).where(
        MetadataLink.user_id == user_id, MetadataLink.parent_uuid == parent,
        MetadataLink.child_uuid == child, MetadataLink.relation == relation)).first()
    if exists is None:
        s.add(MetadataLink(user_id=user_id, parent_uuid=parent, child_uuid=child, relation=relation))


def inputs_for_paths(user_id: int, paths: Iterable[str]) -> list[dict]:
    """The caller's datasets a run binds, by the paths the console sent, as
    ``[{"uuid", "name", "kind": "dataset", "path"}]`` (``path`` is for the caller's matching only and
    is never written into metadata). Unknown paths are skipped."""
    wanted = [p for p in paths if p]
    if not wanted:
        return []
    with session_scope() as s:
        rows = s.scalars(select(Dataset).where(Dataset.user_id == user_id, Dataset.path.in_(wanted))).all()
        by_path: dict[str, Dataset] = {}
        for d in rows:
            by_path.setdefault(d.path, d)
        out = []
        for p in wanted:
            d = by_path.get(p)
            if d is not None and d.uuid:
                out.append({"uuid": d.uuid, "name": d.name, "kind": "dataset", "path": p})
        return out


def planner_notes(user_id: int, uuids: Iterable[str]) -> list[dict]:
    """What the research planner is told about the datasets a run binds: the owner's description and
    the ontology terms, each marked as reviewed or drafted. Small by design."""
    ids = [u for u in uuids if u]
    if not ids:
        return []
    with session_scope() as s:
        rows = s.scalars(select(MetadataRecord).where(MetadataRecord.user_id == user_id,
                                                      MetadataRecord.uuid.in_(ids))).all()
        return [md.planner_note(r.loads("facts"), r.loads("user_fields"), json.loads(r.document or "{}"),
                                r.loads("confidence"), reviewed=r.status == "reviewed") for r in rows]


# --- reads -------------------------------------------------------------------------------------------


def _summary(r: MetadataRecord) -> dict:
    facets = r.loads("facets")
    facts = r.loads("facts")
    return {"uuid": r.uuid, "kind": r.kind, "name": r.name, "status": r.status,
            "dataset_id": r.dataset_id, "run_id": r.run_id,
            "organism": facets.get("organism_label") or [], "tissue": facets.get("tissue_label") or [],
            "assay": facets.get("assay_label") or [], "accession": facets.get("accession") or [],
            "format": facets.get("format") or [], "size_bytes": facts.get("size_bytes"),
            "file_count": facts.get("file_count"), "valid": r.loads("validation").get("ok"),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "resource": f"aiscientist://{'results' if r.kind == 'result' else 'datasets'}/{r.uuid}/croissant.json"}


def list_summaries(user_id: int, *, kind: "str | None" = None, limit: int = 50,
                   offset: int = 0) -> tuple[list[dict], int]:
    with session_scope() as s:
        q = select(MetadataRecord).where(MetadataRecord.user_id == user_id)
        cq = select(func.count(MetadataRecord.id)).where(MetadataRecord.user_id == user_id)
        if kind:
            q, cq = q.where(MetadataRecord.kind == kind), cq.where(MetadataRecord.kind == kind)
        total = int(s.scalar(cq) or 0)
        rows = s.scalars(q.order_by(MetadataRecord.id.desc()).offset(offset).limit(limit)).all()
        return [_summary(r) for r in rows], total


def search(user_id: int, *, kind: "str | None" = None, limit: int = 20, query: str = "",
           organism: str = "", assay: str = "", tissue: str = "", accession: str = "") -> list[dict]:
    """Records matching every given filter. Free text matches the record's search text word by word;
    a facet filter matches a label or an ontology ID of that facet (case-insensitive substring)."""
    words = [w for w in (query or "").lower().split() if w]
    facet_filters = {k: v.lower() for k, v in (("organism", organism), ("assay", assay),
                                                ("tissue", tissue), ("accession", accession)) if v}
    out: list[dict] = []
    with session_scope() as s:
        q = select(MetadataRecord).where(MetadataRecord.user_id == user_id)
        if kind:
            q = q.where(MetadataRecord.kind == kind)
        for r in s.scalars(q.order_by(MetadataRecord.id.desc())).all():
            text = r.search_text or ""
            if any(w not in text for w in words):
                continue
            facets = r.loads("facets")
            if any(not any(v in str(x).lower() for x in (facets.get(k) or [])) for k, v in facet_filters.items()):
                continue
            out.append(_summary(r))
            if len(out) >= limit:
                break
    return out


def _view(s, r: MetadataRecord, *, with_form: bool = False) -> dict:
    out = {**_summary(r), "croissant": json.loads(r.document or "{}"),
           "provenance": r.loads("provenance"), "confidence": r.loads("confidence"),
           "validation": r.loads("validation")}
    if with_form and r.kind == "dataset":
        out["form"] = md.form_view(r.loads("facts"), r.loads("user_fields"), out["croissant"],
                                   out["provenance"], out["confidence"])
    return out


def metadata_view(user_id: int, ref: str, *, kind: "str | None" = None, with_form: bool = False) -> "dict | None":
    with session_scope() as s:
        r = _find(s, user_id, ref, kind=kind)
        return None if r is None else _view(s, r, with_form=with_form)


def document_for(user_id: int, ref: str, *, kind: "str | None" = None) -> "dict | None":
    with session_scope() as s:
        r = _find(s, user_id, ref, kind=kind)
        return None if r is None else json.loads(r.document or "{}")


def record_location(user_id: int, ref: str) -> "dict | None":
    """Where a record's files are, for the preview: never returned to a client."""
    with session_scope() as s:
        r = _find(s, user_id, ref)
        if r is None:
            return None
        d = s.get(Dataset, r.dataset_id) if r.dataset_id else None
        return {"uuid": r.uuid, "kind": r.kind, "run_id": r.run_id, "facts": r.loads("facts"),
                "document": json.loads(r.document or "{}"),
                "dataset_path": d.path if d is not None and d.user_id == user_id else None,
                "dataset_kind": d.kind if d is not None else None}


def lineage(user_id: int, ref: str, *, direction: str = "both", depth: int = 3) -> "dict | None":
    """The lineage graph around one record: nodes (datasets, results, and inputs that were deleted)
    and edges ``{"from": parent, "to": child, "relation"}``, up to ``depth`` hops each way."""
    with session_scope() as s:
        start = _find(s, user_id, ref)
        if start is None:
            return None
        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        seen_edges: set[tuple[str, str, str]] = set()

        def node(uuid: str) -> None:
            if uuid in nodes:
                return
            r = s.scalars(select(MetadataRecord).where(MetadataRecord.user_id == user_id,
                                                       MetadataRecord.uuid == uuid)).first()
            if r is None:
                nodes[uuid] = {"uuid": uuid, "kind": "unknown", "name": None, "deleted": True}
                return
            n = {"uuid": r.uuid, "kind": r.kind, "name": r.name, "status": r.status}
            if r.kind == "result":
                facts = r.loads("facts")
                n.update({k: facts.get(k) for k in ("run_id", "pipeline", "software_version", "models",
                                                    "started_at", "finished_at", "status")})
                n["parameters"] = facts.get("parameters") or []
            nodes[uuid] = n

        def walk(uuid: str, up: bool, left: int) -> None:
            if left <= 0:
                return
            col = MetadataLink.child_uuid if up else MetadataLink.parent_uuid
            for link in s.scalars(select(MetadataLink).where(MetadataLink.user_id == user_id, col == uuid)).all():
                key = (link.parent_uuid, link.child_uuid, link.relation)
                nxt = link.parent_uuid if up else link.child_uuid
                node(nxt)
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append({"from": link.parent_uuid, "to": link.child_uuid, "relation": link.relation})
                    walk(nxt, up, left - 1)

        node(start.uuid)
        if direction in ("upstream", "both"):
            walk(start.uuid, True, depth)
        if direction in ("downstream", "both"):
            walk(start.uuid, False, depth)
        return {"root": start.uuid, "direction": direction, "nodes": list(nodes.values()), "edges": edges}


# --- the scheduled re-check ---------------------------------------------------------------------------


def recheck_all() -> dict:
    """Draft what is missing, then rebuild and re-validate every stored document (the drafting code
    and the validator may both have changed since it was written). Returns counts for the log."""
    drafted = ensure_dataset_records()
    checked = invalid = 0
    with session_scope() as s:
        for r in s.scalars(select(MetadataRecord)).all():
            try:
                if r.kind == "dataset":
                    built = md.build_dataset(r.uuid, r.loads("facts"), r.loads("user_fields"), base_url=base_url(),
                                             description=r.loads("description"))
                else:
                    built = md.build_result(r.uuid, r.loads("facts"), base_url=base_url())
                _store_built(r, built)
            except Exception as exc:  # noqa: BLE001 - one bad record must not stop the sweep
                r.validation = _dumps({"ok": False, "errors": [f"rebuild failed: {type(exc).__name__}: {exc}"],
                                       "warnings": [], "validator": "aiscientist",
                                       "checked_at": _now().isoformat(timespec="seconds")})
            checked += 1
            if not r.loads("validation").get("ok"):
                invalid += 1
        s.commit()
    return {"drafted": drafted, "checked": checked, "invalid": invalid}
