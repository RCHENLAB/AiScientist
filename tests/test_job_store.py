"""Offline tests for the durable Slurm-job registry (JobStore/JobRecord).

No cluster, no SSH: pure filesystem round-trips against a tmp path. Verifies the
record/update/query surface plus the two safety properties that matter for a gateway
that can crash: atomic whole-file writes and tolerance of a missing/corrupt file.
"""

from __future__ import annotations

from bioagent.gateway.job_store import JobRecord, JobStore


def _store(tmp_path):
    return JobStore(tmp_path / "jobs" / "slurm_jobs.json")


def test_record_and_get_roundtrip(tmp_path):
    store = _store(tmp_path)
    store.record(JobRecord(job_id="1001", job_name="runcode_1", owner="alice", kind="runcode"))
    got = store.get("1001")
    assert got is not None
    assert got.job_name == "runcode_1" and got.owner == "alice"
    assert got.state == "SUBMITTED" and got.completed is False


def test_missing_file_reads_empty(tmp_path):
    store = _store(tmp_path)  # file never written
    assert store.all() == []
    assert store.get("nope") is None
    assert store.incomplete() == []


def test_corrupt_file_is_tolerated(tmp_path):
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ this is not json", encoding="utf-8")
    assert store.all() == []            # never raises
    store.record(JobRecord(job_id="1", job_name="j"))   # and recovers on next write
    assert store.get("1") is not None


def test_mark_updates_state_and_completed(tmp_path):
    store = _store(tmp_path)
    store.record(JobRecord(job_id="1002", job_name="runcode_2"))
    before = store.get("1002").updated_at
    rec = store.mark("1002", state="COMPLETED", node="node-9", completed=True)
    assert rec is not None and rec.state == "COMPLETED" and rec.node == "node-9"
    assert rec.completed is True
    assert store.get("1002").updated_at >= before


def test_mark_unknown_job_is_noop(tmp_path):
    store = _store(tmp_path)
    assert store.mark("ghost", state="FAILED") is None


def test_incomplete_excludes_terminal_and_filters(tmp_path):
    store = _store(tmp_path)
    store.record(JobRecord(job_id="1", job_name="a", owner="alice", kind="runcode", state="RUNNING"))
    store.record(JobRecord(job_id="2", job_name="b", owner="alice", kind="runcode", state="COMPLETED", completed=True))
    store.record(JobRecord(job_id="3", job_name="c", owner="bob", kind="runcode", state="PENDING"))
    store.record(JobRecord(job_id="4", job_name="d", owner="alice", kind="scgpt", state="RUNNING"))
    ids = {r.job_id for r in store.incomplete()}
    assert ids == {"1", "3", "4"}                                   # 2 is terminal
    assert {r.job_id for r in store.incomplete(owner="alice")} == {"1", "4"}
    assert {r.job_id for r in store.incomplete(owner="alice", kind="runcode")} == {"1"}


def test_terminal_state_string_marks_record_terminal(tmp_path):
    # A record whose state string is terminal counts as done even if completed wasn't set.
    store = _store(tmp_path)
    store.record(JobRecord(job_id="9", job_name="x", state="OUT_OF_MEMORY"))
    assert store.incomplete() == []
    assert store.get("9").terminal is True


def test_remove_and_prune_terminal(tmp_path):
    store = _store(tmp_path)
    store.record(JobRecord(job_id="1", job_name="a", state="RUNNING"))
    store.record(JobRecord(job_id="2", job_name="b", state="FAILED", completed=True))
    assert store.remove("2") is True
    assert store.remove("2") is False
    store.record(JobRecord(job_id="3", job_name="c", state="CANCELLED"))
    assert store.prune_terminal() == 1                              # only job 3 is terminal now
    assert {r.job_id for r in store.all()} == {"1"}


def test_write_is_atomic_no_partial_file(tmp_path):
    # After a completed write there is exactly one registry file and no leftover .tmp.
    store = _store(tmp_path)
    store.record(JobRecord(job_id="1", job_name="a"))
    files = sorted(p.name for p in store.path.parent.iterdir())
    assert files == ["slurm_jobs.json"]
