"""Literature tables saved as datasets without removing their run artifacts."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    from aiscientist.gateway import app as gw
    monkeypatch.setattr(gw, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw, "_optional_user", lambda request: SimpleNamespace(username="alice", id=1))
    monkeypatch.setattr(gw, "_active_conn_for_user", lambda user: None)
    monkeypatch.setattr(gw.auth_routes, "dataset_path_recorded", lambda *args: False)
    records = []
    monkeypatch.setattr(gw.auth_routes, "record_dataset", lambda *args, **kwargs: records.append((args, kwargs)) or 7)
    conn = SimpleNamespace(owner="alice", settings=SimpleNamespace(checkpoint_ttl_days=5))
    art = gw.CONSOLE_RUNS_DIR / "alice" / "run7" / "artifacts"
    return gw, conn, art, records


@pytest.mark.parametrize("gene_dir", ["literature", "haploinsufficiency"])
def test_literature_tables_list_and_save_with_originals_preserved(env, gene_dir):
    from fastapi.testclient import TestClient
    gw, conn, art, records = env
    genes = art / gene_dir / "genes.csv"
    genes.parent.mkdir(parents=True)
    genes.write_text("gene,evidence,citation\nPAX6,quote,PMID 123 p. 2\n", encoding="utf-8")
    papers = art / "literature" / "papers.csv"
    papers.parent.mkdir(exist_ok=True)
    papers.write_text("pmid,title\n123,title\n", encoding="utf-8")
    rec = gw._stamp_literature_result(conn, "run7", art)
    assert len(rec["files"]) == 2
    client = TestClient(gw.app)
    row, = client.get("/api/result-datasets").json()["result_datasets"]
    assert row["literature"]["n_files"] == 2
    result = client.post("/api/result-datasets/run7/save?what=literature")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["kind"] == "folder" and body["dataset_id"] == 7
    dest = Path(body["path"])
    assert gw.dataset_storage.is_owned(dest, "alice")
    assert (dest / "genes.csv").read_bytes() == genes.read_bytes()
    assert (dest / "papers.csv").read_bytes() == papers.read_bytes()
    assert all(f["sha256"] for f in records[0][1]["files"])
    assert client.post("/api/result-datasets/run7/save?what=literature").json()["already"]
    assert gw._stamp_literature_result(conn, "run7", art)["saved_to"] == str(dest)


def test_literature_save_rejects_pointer_to_another_run(env):
    gw, conn, art, _ = env
    papers = art / "literature" / "papers.csv"
    papers.parent.mkdir(parents=True)
    papers.write_text("pmid,title\n")
    rec = gw._stamp_literature_result(conn, "run7", art)
    rec["path"] = str(art.parent.parent / "someoneelse" / "result" / "literature")
    gw._result_pointer_path("alice", "run7", "literature").write_text(json.dumps(rec))
    assert gw._save_run_result("alice", 1, "run7", "literature", None)[0] == 409


def test_no_tables_creates_no_pointer(env):
    gw, conn, art, _ = env
    assert gw._stamp_literature_result(conn, "run7", art) is None
    assert not gw._result_pointer_path("alice", "run7", "literature").exists()
