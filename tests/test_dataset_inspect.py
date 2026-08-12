"""Offline tests for the general file INGEST/TRIAGE module (``tools/dataset_inspect.py``).

These run on a BARE checkout: the module is import-clean without paramiko/gateway (like the HPO
mapper), the LLM is a scripted ``chat_fn``, and the fixtures are tiny files written to ``tmp_path``.
Do NOT import ``gateway.app`` here — that needs paramiko and defeats the point.
"""
from __future__ import annotations

import gzip
import json
import struct
import zlib

import pytest

from bioagent.tools.dataset_inspect import (
    describe_dataset,
    inspect_dataset,
    make_inspect_dataset_tool,
    peek_dataset,
)


# --- fixtures ----------------------------------------------------------------


_VCF = (
    "##fileformat=VCFv4.2\n"
    "##source=GATK HaplotypeCaller v4.2\n"
    "##reference=file:///refs/human_g1k_v37.fasta\n"
    "##contig=<ID=1,length=249250621,assembly=GRCh37>\n"
    "##contig=<ID=2,length=243199373,assembly=GRCh37>\n"
    "##INFO=<ID=DP,Number=1,Type=Integer,Description=\"depth\">\n"
    "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tNA12878\tNA12891\n"
    "1\t14370\trs6054257\tG\tA\t29\tPASS\tDP=14\tGT\t0|0\t1|0\n"
)


def _bgzf_block(data: bytes) -> bytes:
    """A single valid BGZF block (bgzip = gzip with the 'BC' extra subfield) around ``data``."""
    co = zlib.compressobj(6, zlib.DEFLATED, -15)          # raw deflate
    cdata = co.compress(data) + co.flush()
    bsize = 12 + 6 + len(cdata) + 8 - 1
    header = b"\x1f\x8b\x08\x04" + struct.pack("<IBB", 0, 0, 0xff) + struct.pack("<H", 6)
    extra = b"BC" + struct.pack("<H", 2) + struct.pack("<H", bsize)
    tail = struct.pack("<II", zlib.crc32(data) & 0xffffffff, len(data))
    return header + extra + cdata + tail


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return str(p)


class _Ctx:
    """Duck-typed HarnessContext (mirrors test_hpo_mapper's _Ctx)."""

    def __init__(self, tunnel_port=None, decisions=None):
        self.tunnel_port = tunnel_port
        self.model = "qwen3.6:35b-a3b"
        self.decisions = decisions or {}


# --- peek: VCF ----------------------------------------------------------------


def test_peek_plain_vcf_extracts_assembly_samples_caller(tmp_path):
    peek = peek_dataset(_write(tmp_path, "case.vcf", _VCF))
    assert peek["detected_format"] == "vcf"
    assert peek["bgzip"] is False
    vcf = peek["vcf"]
    assert vcf["assembly"] == "GRCh37"
    assert vcf["sample_ids"] == ["NA12878", "NA12891"]
    assert vcf["n_samples"] == 2
    assert vcf["caller"] == "GATK HaplotypeCaller"
    assert vcf["chr_prefix"] is False
    assert vcf["has_genotypes"] is True


def test_peek_bgzipped_vcf_is_detected_and_decompressed(tmp_path):
    """The bgzip magic must be recognised AND the header decompressed enough to read the samples —
    the normal on-disk form of a WGS callset is .vcf.gz."""
    peek = peek_dataset(_write(tmp_path, "wgs.vcf.gz", _bgzf_block(_VCF.encode("utf-8"))))
    assert peek["detected_format"] == "vcf"
    assert peek["magic"] == "bgzip"
    assert peek["bgzip"] is True
    assert peek["compression"] == "bgzip"
    assert peek["vcf"]["assembly"] == "GRCh37"
    assert peek["vcf"]["sample_ids"] == ["NA12878", "NA12891"]


def test_peek_vcf_assembly_from_contig_length_without_explicit_token(tmp_path):
    """No assembly= / ##reference token — the chr1 contig length must still pin the build."""
    vcf = ("##fileformat=VCFv4.2\n"
           "##contig=<ID=chr1,length=248956422>\n"
           "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
           "chr1\t100\t.\tA\tT\t50\tPASS\t.\n")
    peek = peek_dataset(_write(tmp_path, "x.vcf", vcf))
    assert peek["vcf"]["assembly"].startswith("GRCh38")
    assert peek["vcf"]["chr_prefix"] is True
    assert peek["vcf"]["sample_ids"] == []          # sites-only VCF


# --- peek: HDF5 / h5ad --------------------------------------------------------


def test_peek_h5ad_structure_without_loading_matrices(tmp_path):
    h5py = pytest.importorskip("h5py")
    path = str(tmp_path / "adata.h5ad")
    with h5py.File(path, "w") as f:
        f.attrs["encoding-type"] = "anndata"
        f.create_dataset("X", data=[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])
        f.create_group("obs").create_dataset("cell_type", data=[0, 1, 0])
        f.create_group("var").create_dataset("gene_ids", data=[b"ENSG1", b"ENSG2"])
    peek = peek_dataset(path)
    assert peek["detected_format"] == "h5ad"
    assert peek["magic"] == "hdf5"
    hdf5 = peek["hdf5"]
    assert hdf5["h5py"] is True
    assert "obs" in hdf5["anndata"] and "var" in hdf5["anndata"]
    assert hdf5["anndata"]["X"]["shape"] == [3, 2]
    # the matrix itself is described (shape/dtype) but never read into memory
    assert any(d["name"] == "X" for d in hdf5["datasets"])


def test_peek_hdf5_degrades_cleanly_when_h5py_absent(tmp_path, monkeypatch):
    """If h5py cannot import, the HDF5 magic is still reported and the peek notes the gap — no crash."""
    import builtins
    real_import = builtins.__import__

    def _no_h5py(name, *a, **k):
        if name == "h5py":
            raise ImportError("simulated: h5py not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_h5py)
    # an HDF5 magic header (no real body needed — h5py import fails before opening)
    path = _write(tmp_path, "adata.h5ad", b"\x89HDF\r\n\x1a\n" + b"\x00" * 64)
    peek = peek_dataset(path)
    assert peek["detected_format"] == "h5ad"
    assert peek["hdf5"]["h5py"] is False
    assert "h5py" in peek["hdf5"]["note"]


# --- peek: tabular + gz -------------------------------------------------------


def test_peek_csv_columns_and_rows(tmp_path):
    csv_text = "gene,logFC,pval\nRPE65,2.1,0.001\nABCA4,-1.4,0.02\nCRB1,0.3,0.9\n"
    peek = peek_dataset(_write(tmp_path, "de.csv", csv_text))
    assert peek["detected_format"] == "tabular"
    tab = peek["tabular"]
    assert tab["delimiter"] == "comma"
    assert tab["columns"] == ["gene", "logFC", "pval"]
    assert tab["n_columns"] == 3
    assert tab["rows_sample"][0] == ["RPE65", "2.1", "0.001"]


def test_peek_tsv_uses_tab_delimiter(tmp_path):
    tsv = "chrom\tpos\tref\talt\n1\t100\tA\tT\n"
    peek = peek_dataset(_write(tmp_path, "sites.tsv", tsv))
    assert peek["detected_format"] == "tabular"
    assert peek["tabular"]["delimiter"] == "tab"
    assert peek["tabular"]["columns"] == ["chrom", "pos", "ref", "alt"]


def test_peek_plain_gzip_csv_head_is_decompressed(tmp_path):
    csv_text = "sample,value\nA,1\nB,2\n"
    peek = peek_dataset(_write(tmp_path, "counts.csv.gz", gzip.compress(csv_text.encode())))
    assert peek["compression"] == "gzip"
    assert peek["detected_format"] == "tabular"
    assert peek["tabular"]["columns"] == ["sample", "value"]


# --- peek: unknown / binary / broken -----------------------------------------


def test_peek_unknown_binary_blob_is_a_safe_descriptor(tmp_path):
    """A file with no recognised magic and NUL bytes must yield a hexdump descriptor, never a crash."""
    blob = bytes([0x00, 0x01, 0x02, 0xff, 0xfe] * 40)
    peek = peek_dataset(_write(tmp_path, "mystery.bin", blob))
    assert peek["detected_format"] == "binary"
    assert peek["readable"] is True
    assert "hexdump" in peek
    assert peek["notes"]        # a note explaining it's opaque


def test_peek_nonexistent_path_never_raises(tmp_path):
    peek = peek_dataset(str(tmp_path / "nope.vcf"))
    assert peek["exists"] is False
    assert peek["readable"] is False
    assert peek["detected_format"] == "unknown"


def test_peek_broken_h5ad_degrades_to_safe_descriptor(tmp_path):
    """A file NAMED .h5ad but full of garbage (not real HDF5) must degrade with a note, not raise."""
    peek = peek_dataset(_write(tmp_path, "corrupt.h5ad", b"this is not hdf5 at all" * 10))
    # suffix routes it to the HDF5 branch; the open fails and is captured as a note
    assert peek["detected_format"] in ("h5ad", "hdf5")
    assert "note" in peek["hdf5"]
    assert peek["readable"] is True


def test_peek_corrupt_gzip_body_does_not_crash(tmp_path):
    """Valid gzip magic but a garbage body: _gunzip_head returns what it can (maybe nothing) — safe."""
    data = b"\x1f\x8b\x08\x00" + b"\x00" * 8 + b"garbage-not-deflate" * 5
    peek = peek_dataset(_write(tmp_path, "bad.gz", data))
    assert peek["compression"] == "gzip"
    assert peek["detected_format"] in ("gzip", "tabular", "text", "vcf")   # whatever it degraded to
    assert peek["readable"] is True


def test_peek_plain_text_readme(tmp_path):
    peek = peek_dataset(_write(tmp_path, "README", "This dataset contains retina snRNA-seq.\nBatch 3.\n"))
    assert peek["detected_format"] == "text"
    assert "retina" in peek["text_sample"]
    assert peek["line_sample"][0].startswith("This dataset")


# --- describe (LLM mocked) ----------------------------------------------------


def _scripted(reply):
    seen = []

    def chat_fn(messages):
        seen.append(messages)
        return reply

    chat_fn.seen = seen
    return chat_fn


def test_describe_uses_llm_reply_and_marks_source(tmp_path):
    peek = peek_dataset(_write(tmp_path, "case.vcf", _VCF))
    chat_fn = _scripted(json.dumps({
        "file_kind": "germline VCF callset",
        "format": "VCF",
        "assembly": "GRCh37",
        "sample_ids": ["NA12878", "NA12891"],
        "likely_modality": "variants",
        "key_facts": ["two samples", "GATK-called"],
        "one_line_summary": "A two-sample GRCh37 germline VCF called by GATK.",
        "confidence": "high",
    }))
    desc = describe_dataset(peek, chat_fn=chat_fn)
    assert desc["source"] == "llm"
    assert desc["likely_modality"] == "variants"
    assert desc["one_line_summary"].startswith("A two-sample")
    # the system prompt actually carried the evidence to the model
    assert "case.vcf" in json.dumps(chat_fn.seen[0])


def test_describe_grounds_llm_against_the_peek_evidence(tmp_path):
    """If the model contradicts the deterministic evidence (wrong assembly), CODE wins."""
    peek = peek_dataset(_write(tmp_path, "case.vcf", _VCF))
    chat_fn = _scripted(json.dumps({
        "file_kind": "VCF", "format": "VCF",
        "assembly": "GRCh38",                       # WRONG — peek proved GRCh37
        "sample_ids": ["madeup"],                   # WRONG — peek has NA12878/NA12891
        "likely_modality": "variants", "key_facts": [], "one_line_summary": "x", "confidence": "high",
    }))
    desc = describe_dataset(peek, chat_fn=chat_fn)
    assert desc["assembly"] == "GRCh37"
    assert desc["sample_ids"] == ["NA12878", "NA12891"]


def test_describe_falls_back_deterministically_without_a_model(tmp_path):
    peek = peek_dataset(_write(tmp_path, "case.vcf", _VCF))
    desc = describe_dataset(peek, chat_fn=None)
    assert desc["source"] == "deterministic"
    assert desc["format"] == "vcf"
    assert desc["assembly"] == "GRCh37"
    assert desc["likely_modality"] == "variants"
    assert "GRCh37" in desc["one_line_summary"]


def test_describe_survives_a_junk_llm_reply(tmp_path):
    peek = peek_dataset(_write(tmp_path, "de.csv", "gene,logFC\nA,1\n"))
    desc = describe_dataset(peek, chat_fn=_scripted("I think this is maybe a spreadsheet?"))
    assert desc["source"] == "deterministic"        # unusable JSON → fallback, no crash
    assert desc["format"] == "tabular"


def test_describe_survives_an_llm_exception(tmp_path):
    peek = peek_dataset(_write(tmp_path, "x.txt", "hello"))
    def boom(_messages):
        raise RuntimeError("vLLM down")
    desc = describe_dataset(peek, chat_fn=boom)
    assert desc["source"] == "deterministic"
    assert "note" in desc


def test_describe_unknown_binary_is_still_describable(tmp_path):
    peek = peek_dataset(_write(tmp_path, "blob.dat", bytes([0]) + b"\xff" * 200))
    desc = describe_dataset(peek, chat_fn=None)
    assert desc["likely_modality"] == "unknown"
    assert desc["confidence"] == "low"
    assert desc["one_line_summary"]                 # still says SOMETHING best-effort


# --- inspect_dataset + the tool ----------------------------------------------


def test_inspect_dataset_combines_peek_and_description(tmp_path):
    out = inspect_dataset(_write(tmp_path, "case.vcf", _VCF), chat_fn=None)
    assert out["status"] == "ok"
    assert out["peek"]["detected_format"] == "vcf"
    assert out["description"]["source"] == "deterministic"
    assert out["raw_data_to_llm"] is False


def test_tool_schema_and_metadata_follow_the_pattern():
    tool = make_inspect_dataset_tool()
    assert tool.name == "inspect_dataset"
    fn = tool.schema()["function"]
    assert fn["name"] == "inspect_dataset" and fn["description"]
    assert "path" in fn["parameters"]["properties"]
    assert tool.reads_private_data is True
    assert isinstance(tool.requires, tuple)


def test_tool_runs_without_a_served_model(tmp_path):
    """ctx with no tunnel_port (tests, or a session before the GPU is up) -> peek + deterministic
    description, not a crash and not an LLM call."""
    tool = make_inspect_dataset_tool()
    path = _write(tmp_path, "case.vcf", _VCF)
    out = tool.executor({"path": path}, _Ctx(tunnel_port=None))
    assert out["status"] == "ok"
    assert out["description"]["source"] == "deterministic"
    assert out["peek"]["vcf"]["assembly"] == "GRCh37"


def test_tool_reads_bound_dataset_when_no_path_arg(tmp_path):
    path = _write(tmp_path, "bound.vcf", _VCF)
    tool = make_inspect_dataset_tool()
    out = tool.executor({}, _Ctx(tunnel_port=None, decisions={"dataset_path": path}))
    assert out["peek"]["name"] == "bound.vcf"


def test_tool_errors_without_any_path():
    out = make_inspect_dataset_tool().executor({}, _Ctx(tunnel_port=None))
    assert out["status"] == "error"
    assert "path" in out["error"]
