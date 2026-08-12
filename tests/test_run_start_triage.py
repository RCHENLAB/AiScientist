"""Phase C — AUTO-DESCRIBE the bound files at run start (feature ② + feature ①).

``_triage_bound_datasets`` runs feature ①'s peek+describe over each bound file once a served model
exists (the GPU is already warm for the run — no extra provisioning), stamps a compact ``description``
onto each record, and sets ``decisions["content_modality"]``/``["content_confidence"]`` from the
PRIMARY so Phase-B routing uses the file's CONTENT. It degrades to a deterministic peek-only
description when the model is unreachable, and never raises.
"""

from __future__ import annotations

import types

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.gateway import vllm_client  # noqa: E402


_VCF = (
    "##fileformat=VCFv4.2\n"
    "##reference=GRCh37\n"
    "##contig=<ID=1,length=249250621>\n"
    "##source=GATK HaplotypeCaller\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tPROBAND\n"
    "1\t100\t.\tA\tT\t50\tPASS\t.\tGT\t0/1\n"
)


class _Conn:
    def __init__(self):
        self.executor = None
        self.tunnel_port = 40000
        self.settings = types.SimpleNamespace(lab_storage="/dfs/ruic20_lab")


def _write(tmp_path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# --- the modality drives routing ---------------------------------------------------------------


def test_triage_sets_primary_content_modality_from_the_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(vllm_client, "complete",
                        lambda *a, **k: '{"file_kind":"VCF callset","format":"VCF",'
                        '"likely_modality":"variants","one_line_summary":"A GRCh37 VCF.",'
                        '"confidence":"high"}')
    path = _write(tmp_path, "case.vcf", _VCF)
    decisions = {"datasets": [{"path": path, "name": "case.vcf", "role": None, "primary": True}]}
    gists = gw_app._triage_bound_datasets(_Conn(), decisions, "qwen")
    assert decisions["content_modality"] == "variants"
    assert decisions["content_confidence"] == "high"
    assert decisions["datasets"][0]["description"]["likely_modality"] == "variants"
    assert gists and "case.vcf" in gists[0]


def test_content_wins_over_a_misleading_extension(tmp_path, monkeypatch):
    """A file NAMED .txt but whose bytes are a VCF must read as variants — the whole point of Phase B+C:
    content, not the filename. The model is unreachable here, so this also proves the deterministic
    fallback still yields the right modality."""
    def _boom(*a, **k):
        raise RuntimeError("no model")
    monkeypatch.setattr(vllm_client, "complete", _boom)
    path = _write(tmp_path, "mystery.txt", _VCF)          # .txt extension, VCF content
    decisions = {"datasets": [{"path": path, "name": "mystery.txt", "primary": True}]}
    gw_app._triage_bound_datasets(_Conn(), decisions, "qwen")
    assert decisions["content_modality"] == "variants"    # detected from the bytes, not ".txt"
    assert decisions["datasets"][0]["description"]["source"] == "deterministic"


def test_triage_describes_every_bound_file_but_routes_off_the_primary(tmp_path, monkeypatch):
    # deterministic (no model) — modality comes from each file's content
    monkeypatch.setattr(vllm_client, "complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    vcf = _write(tmp_path, "case.vcf", _VCF)
    panel = _write(tmp_path, "panel.bed", "chr1\t100\t200\tGENEA\n")
    decisions = {"datasets": [
        {"path": vcf, "name": "case.vcf", "primary": True},
        {"path": panel, "name": "panel.bed", "primary": False}]}
    gists = gw_app._triage_bound_datasets(_Conn(), decisions, "qwen")
    assert len(gists) == 2                                  # both files skimmed
    assert decisions["datasets"][0]["description"]["likely_modality"] == "variants"
    assert "description" in decisions["datasets"][1]        # secondary described too
    assert decisions["content_modality"] == "variants"     # routing uses the PRIMARY's modality


def test_triage_is_a_noop_without_bound_files():
    decisions: dict = {}
    assert gw_app._triage_bound_datasets(_Conn(), decisions, "qwen") == []
    assert "content_modality" not in decisions


def test_triage_never_raises_on_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(vllm_client, "complete", lambda *a, **k: "{}")
    decisions = {"datasets": [{"path": str(tmp_path / "gone.vcf"), "name": "gone.vcf", "primary": True}]}
    # A missing file peeks as unreadable; describe still yields a (low-confidence) description — no crash.
    gw_app._triage_bound_datasets(_Conn(), decisions, "qwen")
    assert "description" in decisions["datasets"][0]
