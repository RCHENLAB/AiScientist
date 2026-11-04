"""Shared test fixtures.

Curated skills come only from the pinned aiscientist-skills release (``configs/skills_registry.json``),
which is installed on the server and not in git. A test never reads an installed release:
``skill_release`` publishes one from skill folders the test writes, the way the registry's release
workflow does, and points the loader at it. ``tests/fixtures/skills/`` holds the skills several
test files share (the CellQC skill ``run_cellqc`` reads its environment and settings from).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

FIXTURE_SKILLS = Path(__file__).resolve().parent / "fixtures" / "skills"


def write_release(releases_root: Path, source: Path, *, tag: str = "v0.1.0",
                  status: str = "community") -> Path:
    """Publish every skill under ``source`` (a folder holding a ``SKILL.md``, at any depth) as
    release ``tag`` in ``releases_root/<version>/``, replacing what was there:
    ``aiscientist-skills-<version>.zip`` with each skill at ``skills/<category>/<name>/``,
    ``catalog.json`` built from the SKILL.md front matter, and ``SHA256SUMS`` over the two. A skill's
    tier is its ``metadata.status``, else ``status``. Returns the release folder."""
    from aiscientist.agents.skills import _parse_front_matter, _skill_md_files

    version = tag[1:]
    root = releases_root / version
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    rows, members = [], {}
    for skill_md in _skill_md_files(source):
        folder = skill_md.parent
        meta, body = _parse_front_matter(skill_md.read_text(encoding="utf-8"))
        md = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
        name = str(meta.get("name") or folder.name)
        parent = folder.parent.relative_to(source).as_posix()
        category = str(md.get("category") or ("general" if parent == "." else parent))
        path = f"skills/{category}/{name}"
        for file in sorted(folder.rglob("*")):
            if file.is_file():
                members[f"{path}/{file.relative_to(folder).as_posix()}"] = file.read_bytes()
        secrets = [s.strip() for s in str(md.get("requires-secrets") or "").split(",")]
        rows.append({
            "name": name, "category": category, "path": path,
            "description": str(meta.get("description") or body.strip().splitlines()[0]),
            "version": str(md.get("version") or "1.0.0"), "status": str(md.get("status") or status),
            "tags": [t.strip() for t in str(md.get("tags") or "").split(",") if t.strip()],
            "requirements": {"compute": str(md.get("compute") or ""),
                             "gpu": str(md.get("requires-gpu") or "false").lower() == "true",
                             "scheduler": str(md.get("scheduler") or "none"),
                             "network": str(md.get("requires-network") or "none"),
                             "secrets": [s for s in secrets if s and s != "none"],
                             "allowed_tools": str(meta.get("allowed-tools") or "")}})
    bundle = f"aiscientist-skills-{version}.zip"
    with zipfile.ZipFile(root / bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in sorted(members.items()):
            info = zipfile.ZipInfo(rel, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    (root / "catalog.json").write_text(json.dumps({
        "schema_version": 1, "registry": "aiscientist-skills", "registry_version": version,
        "skills": rows}, indent=2) + "\n", encoding="utf-8")
    (root / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256((root / n).read_bytes()).hexdigest()}  {n}\n"
        for n in (bundle, "catalog.json")), encoding="utf-8")
    return root


@pytest.fixture
def skill_release(tmp_path, monkeypatch):
    """``skill_release(source, **kw)`` installs a test release of the skills under ``source`` (see
    :func:`write_release`), points the loader at it, and refreshes the library (``refresh=False``
    leaves that to the test). The releases root starts empty, so the library is empty until a test
    installs one. Everything is restored after the test."""
    from aiscientist.agents import skills as sk

    releases = tmp_path / "skill-releases"
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT", str(releases))
    # No skills/current link unless the test makes one: the pinned tag decides.
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_CURRENT", str(tmp_path / "skills-current"))
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_STATS", str(tmp_path / "skill_versions.sqlite3"))

    def install(source: Path, *, refresh: bool = True, **kw) -> Path:
        root = write_release(releases, source, **kw)
        if refresh:
            sk.refresh_skills()
        return root

    yield install
    monkeypatch.undo()
    sk.refresh_skills()


@pytest.fixture
def cellqc_release(skill_release):
    """A release holding the fixture CellQC skill, the one ``run_cellqc`` names in its TOOL.md."""
    return skill_release(FIXTURE_SKILLS)


@pytest.fixture(autouse=True)
def no_account_storage_limit(monkeypatch):
    """The per-account limit (issue 74) is off unless a test turns it on: it reads the account's
    datasets, which most tests save or upload without a database."""
    monkeypatch.setenv("AISCIENTIST_ACCOUNT_STORAGE_GB", "0")


@pytest.fixture
def lab_gateway(tmp_path, monkeypatch):
    """Exercise saved plans through the real gateway with per-test model/tool doubles."""
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    pytest.importorskip("fastapi")
    from aiscientist.agents import registry, research_lab
    from aiscientist.agents.research_harness import HarnessTool
    from aiscientist.gateway import app as gw, saved_plans
    from aiscientist.gateway.settings import HPCSettings

    calls, models, cards, records = [], [], [], []
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    monkeypatch.setenv("AISCIENTIST_PLAN_YAML", "1")
    monkeypatch.setenv("AISCIENTIST_PLAN_REVIEW", "0")
    monkeypatch.setenv("AISCIENTIST_MAX_RUNS_PER_SESSION", "3")

    def complete(messages, **kwargs):
        system = messages[0]["content"]
        models.append(system)
        if "Principal Investigator of a bioinformatics lab" in system:
            return json.dumps({"agenda": ["Filter cells with `run_scanpy_qc` at max_pct_mt 20."]})
        if "filling in a form" in system:
            return '[{"step":1,"settings":{"max_pct_mt":20}}]'
        if "scientific Critic" in system:
            return '{"verdict":"accept","score":0.9,"critique":"ok"}'
        if "change" in system.lower() or "revis" in system.lower():
            return '{"ops":[{"op":"change","step":"s1","settings":{"min_genes":300}}]}'
        return "Report."

    def scientist_chat(messages, tools, **kwargs):
        done = any(m.get("role") == "tool" for m in messages)
        return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
            "name": "finish" if done else "run_scanpy_qc",
            "arguments": json.dumps({"answer": "done"} if done else {"max_pct_mt": 5})}}]}

    def tool(args, ctx):
        calls.append(dict(args))
        return {"status": "ok"}

    catalog = [HarnessTool("run_scanpy_qc", "Filter cells", {"type": "object", "properties": {
        "max_pct_mt": {"type": "number"}, "min_genes": {"type": "integer"}}}, tool)]
    monkeypatch.setattr(registry, "build_scientist_catalog", lambda **kw: catalog)
    # The step classifier caches the catalog's names; this one-tool catalog must not outlive the test.
    research_lab._known_tool_names.cache_clear()
    original_lab = research_lab.ResearchLab

    def make_lab(ctx, config, **kw):
        return original_lab(ctx, replace(config, auto_select_skill=False, plan_review=False), **kw)

    monkeypatch.setattr(research_lab, "ResearchLab", make_lab)
    monkeypatch.setattr(gw, "_lab_llm", lambda c: SimpleNamespace(
        complete_fn=complete, scientist_chat=scientist_chat, model="offline", label="offline",
        count_tokens=None, scientist_remote=False, lab_role_remote=False,
        shared_endpoint=False, context_window=None))
    monkeypatch.setattr(gw, "_finish_run_record", lambda *a: records.append(a))

    async def roster(conn, tools, *args):
        return tools

    monkeypatch.setattr(gw, "_prepare_session_roster", roster)
    # Report rendering is outside the plan lifecycle under test. The real lab still executes
    # and pins the saved settings before this gateway tail is skipped.
    monkeypatch.setattr(gw, "_run_produced_nothing", lambda *args: True)
    conns = []

    def connection():
        conn = gw.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
        conn.workspace, conn.status, conn.executor = tmp_path / "workspace", "ready", object()
        gw.CONNECTIONS[conn.id] = conn
        conns.append(conn)
        publish = conn._publish

        def check_card(payload):
            if payload.get("type") in {"plan_prompt", "plan_clarify"}:
                rid = payload["run_id"]
                art = conn.workspace / rid / "artifacts"
                current = saved_plans.load(art)
                state = json.loads((art / "process" / "run_state.json").read_text(encoding="utf-8"))
                assert current is not None and state["rounds"] == []
                assert payload["plan"]["yaml"] == current["yaml"]
                cards.append(dict(payload))
            publish(payload)

        monkeypatch.setattr(conn, "_publish", check_card)
        return conn

    yield SimpleNamespace(connection=connection, calls=calls, models=models, cards=cards, records=records)
    for conn in conns:
        gw.CONNECTIONS.pop(conn.id, None)
        conn.loop.close()
    gw._SAVED_PLAN_TASKS.clear()
    research_lab._known_tool_names.cache_clear()
