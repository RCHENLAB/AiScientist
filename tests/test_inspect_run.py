"""The run-inspection scripts: what scripts/inspect_run.py collects, and the database report of
scripts/inspect_run_db.py against a real (SQLite) copy of the app's tables."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aiscientist.gateway import models
from aiscientist.gateway.db import Base

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ir = _load("inspect_run")
irdb = _load("inspect_run_db")

INFO = {"found": True, "run_dir": "/data/aiscientist/app/runs/console/BioAdmin/1e887ed2bf8e",
        "account": "BioAdmin", "window": ["2026-10-12 13:28", "2026-10-12 14:09"], "finished": True,
        "hpc_primary": "/dfs3b/ruic20_lab/software/AiScientist/uploads/yijus12/server-data/x/filtered.h5"}
CFG = dict(ir.DEFAULTS, HPC_USER="")


def test_deploy_env_parsing_and_hpc_user():
    env = ir.parse_deploy_env("# comment\nexport ADMIN_SSH=eyeserver-admin\nHPC_SSH='hpc3'  # inline\n\nBAD LINE\n")
    assert env == {"ADMIN_SSH": "eyeserver-admin", "HPC_SSH": "hpc3"}
    assert ir.hpc_user_from(INFO) == "yijus12"
    assert ir.hpc_user_from({"hpc_dataset_root": "/x/Temp/ziyaom2/analysis/r"}) == "ziyaom2"
    assert ir.hpc_user_from({}, "fallback") == "fallback"


def test_captures_cover_both_hosts_and_stay_read_only():
    caps = ir.build_captures("1e887ed2bf8e", INFO, "yijus12", CFG, "scripts/inspect_run_db.py")
    ids = [c[0] for c in caps]
    assert len(ids) == len(set(ids))
    assert {"events", "rounds", "state", "db"} <= {c[0] for c in caps if c[1] == "gateway"}
    assert {"sacct", "queue", "workspace", "jobfiles", "jobscripts"} <= {c[0] for c in caps if c[1] == "hpc"}
    text = "\n".join(c[3] for c in caps)
    assert "-S 2026-10-12T13:28 -E 2026-10-12T14:09" in text          # the run's own window
    assert "Temp/yijus12" in text and "1e887ed2bf8e" in text
    for verb in ("rm ", "mv ", "scancel", "sbatch ", "> /", ">> "):     # nothing that changes a host
        assert verb not in text, verb
    assert "None" not in text


def _db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(engine)
    t0 = dt.datetime(2026, 10, 12, 13, 34, 13)
    with Session(engine) as s:
        user = models.User(username="BioAdmin", password_hash="x")
        s.add(user)
        s.flush()
        conv = models.Conversation(user_id=user.id, title="Sample1_WT/ · Run CellQC")
        s.add(conv)
        s.flush()
        s.add_all([
            models.Message(conversation_id=conv.id, role="user", content="Run CellQC on the sample.", kind="text", seq=1),
            models.Message(conversation_id=conv.id, role="assistant", content="# Report\n\nbody", kind="text",
                           meta=json.dumps({"feed": [], "thinking": ""}), seq=2),
            models.Run(user_id=user.id, run_id="1e887ed2bf8e", conversation_id=str(conv.id),
                       question="Run CellQC on the sample.", status="incomplete", plan_mode=True),
            models.LlmCall(run_id="1e887ed2bf8e", user_id=user.id, role="plan", model="m", endpoint="m @ x",
                           prompt_tokens=100, completion_tokens=10, duration_ms=1500, created_at=t0),
            models.LlmCall(run_id="1e887ed2bf8e", user_id=user.id, role="critic", model="m", endpoint="m @ x",
                           prompt_tokens=50, completion_tokens=5, duration_ms=500, created_at=t0 + dt.timedelta(seconds=9)),
            models.LlmCall(run_id="other", user_id=user.id, role="plan", model="m", endpoint="m @ x", created_at=t0),
        ])
        s.commit()
    return engine


def test_db_report_on_sqlite(tmp_path):
    engine = _db(tmp_path)
    out = irdb.summary(engine, "1e887ed2bf8e")
    assert "run_id           1e887ed2bf8e" in out
    assert "status           incomplete" in out
    assert "Run CellQC on the sample." in out and "[feed,thinking]" in out
    assert "llm_calls: 2" in out                          # the other run's call is not counted
    assert "plan" in out and "critic" in out and "total" in out
    lines = [json.loads(x) for x in irdb.llm_jsonl(engine, "1e887ed2bf8e").splitlines()]
    assert [x["role"] for x in lines] == ["plan", "critic"]
    full = json.loads(irdb.messages_json(engine, "1e887ed2bf8e"))
    assert [m["seq"] for m in full["messages"]] == [1, 2]
    assert "no runs row" in irdb.summary(engine, "missing")
