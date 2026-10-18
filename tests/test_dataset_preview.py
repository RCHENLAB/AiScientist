"""The Datasets tab's preview and download (RCHENLAB/AiScientist issue 10).

The readers (gateway/dataset_view.py) on real files, then the two routes: a dataset is read where it is
kept (this host or HPC3), only by its owner, never outside the owner's storage, and a file a user
uploaded is never shown in the page as anything but an image or a PDF.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import types
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from aiscientist.gateway import dataset_view as dv

HPC_UPLOADS = "/dfs3b/proj/AiScientist/uploads/yijus12"
HPC_LAB = "/dfs3b/ruic20_lab/yijus12"

VCF = (b"##fileformat=VCFv4.2\n##reference=GRCh38\n##contig=<ID=chr1,length=248956422>\n"
       b"##INFO=<ID=DP,Number=1,Type=Integer,Description=\"Depth\">\n"
       b"##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
       b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\n"
       + b"".join(b"chr1\t%d\t.\tA\tG\t50\tPASS\tDP=10\tGT\t0/1\t0/0\n" % (100 + i) for i in range(30)))


# --- the readers ------------------------------------------------------------------------------------

def test_only_images_and_pdfs_are_ever_shown_inline():
    assert dv.inline_type("umap.PNG") == "image/png"
    assert dv.inline_type("report.pdf") == "application/pdf"
    for name in ("figure.svg", "page.html", "notes.htm", "data.csv", "x.h5ad"):
        assert dv.inline_type(name) is None
    assert [dv.classify(n) for n in ("a.h5ad", "f.h5", "c.vcf.gz", "t.tsv.gz", "m.json", "r.txt", "b.bam")] == \
        ["h5ad", "hdf5", "vcf", "table", "json", "table", "binary"]


def test_a_table_shows_its_first_rows_and_column_types():
    rows = "\n".join(f"G{i},{i},{i / 3:.3f},x{i}" for i in range(250))
    body = ("gene,count,score,label\n" + rows + "\n").encode()
    p = dv.text_file_preview(body, "de.csv", len(body))
    assert p["type"] == "table" and p["columns"] == ["gene", "count", "score", "label"]
    assert p["column_types"] == ["text", "integer", "number", "text"]
    assert p["rows_shown"] == dv.TABLE_ROWS and p["more_rows"] is True


def test_a_cut_head_never_shows_a_half_line():
    body = b"a\tb\n" + b"1\t2\n" * 10 + b"3\t4"
    p = dv.text_file_preview(body[:-2], "x.tsv", len(body))          # the head ends inside the last line
    assert p["rows"][-1] == ["1", "2"] and p["more_rows"] is True


def test_a_bgzipped_vcf_shows_its_header_and_first_records():
    # bgzip is many gzip members: the preview decompresses across them
    data = b"".join(gzip.compress(VCF[i:i + 200]) for i in range(0, len(VCF), 200))
    p = dv.text_file_preview(data, "calls.vcf.gz", len(data))
    assert p["type"] == "vcf" and p["compressed"] is True
    assert (p["reference"], p["n_contigs"], p["samples"]) == ("GRCh38", 1, ["S1", "S2"])
    assert p["info_fields"] == ["DP"] and p["format_fields"] == ["GT"]
    assert len(p["rows"]) == dv.VCF_RECORDS and p["rows"][0][:2] == ["chr1", "100"]


def test_binary_and_json_files():
    assert dv.text_file_preview(bytes(range(256)) * 20, "x.dat", 5120)["type"] == "binary"
    j = json.dumps({"a": [1, 2]}).encode()
    assert '"a": [' in dv.text_file_preview(j, "m.json", len(j))["text"]


@pytest.fixture
def h5ad_files(tmp_path):
    ad = pytest.importorskip("anndata")
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    sp = pytest.importorskip("scipy.sparse")
    rng = np.random.default_rng(0)
    counts = sp.csr_matrix(rng.poisson(1.0, size=(40, 25)).astype("float32"))
    obs = pd.DataFrame({"sample": pd.Categorical(["ctrl", "ko"] * 20), "n_genes": np.arange(40)},
                       index=[f"AAAC-{i}" for i in range(40)])
    a = ad.AnnData(X=counts, obs=obs, var=pd.DataFrame(index=[f"Gene{i}" for i in range(25)]))
    a.layers["counts"] = counts.copy()
    a.obsm["X_umap"] = np.zeros((40, 2))
    raw_path = tmp_path / "raw.h5ad"
    a.write_h5ad(raw_path)
    a.X = sp.csr_matrix(np.log1p(counts.toarray()))
    norm_path = tmp_path / "norm.h5ad"
    a.write_h5ad(norm_path)
    return raw_path, norm_path


def test_an_h5ad_is_described_without_loading_its_matrix(h5ad_files):
    raw_path, norm_path = h5ad_files
    p = dv.h5ad_preview(str(raw_path))
    assert (p["n_obs"], p["n_vars"]) == (40, 25)
    assert p["X"]["encoding"] == "csr_matrix" and p["X"]["sample"]["all_integer"] is True
    sample = next(c for c in p["obs"]["columns"] if c["name"] == "sample")
    assert sample["dtype"] == "category" and sample["categories"] == ["ctrl", "ko"]
    assert p["obs"]["head"]["index"][:2] == ["AAAC-0", "AAAC-1"]
    assert p["obs"]["head"]["rows"][1][p["obs"]["head"]["columns"].index("sample")] == "ko"
    assert [x["name"] for x in p["layers"]] == ["counts"]
    assert p["obsm"] == [{"name": "X_umap", "shape": [40, 2]}]
    # log-transformed values are not whole numbers: the preview says so
    assert dv.h5ad_preview(str(norm_path))["X"]["sample"]["all_integer"] is False


def test_an_h5ad_is_read_through_a_file_object_as_over_sftp(h5ad_files):
    with open(h5ad_files[0], "rb") as fh:
        assert dv.h5ad_preview(fh)["n_obs"] == 40


def test_a_damaged_h5ad_is_described_not_raised(tmp_path):
    bad = tmp_path / "bad.h5ad"
    bad.write_bytes(b"\x89HDF\r\n\x1a\n" + b"\x00" * 100)
    assert "could not be read" in dv.h5ad_preview(str(bad))["note"]


def test_a_cell_ranger_h5_reports_features_and_barcodes(tmp_path):
    h5py = pytest.importorskip("h5py")
    path = tmp_path / "filtered_feature_bc_matrix.h5"
    with h5py.File(path, "w") as f:
        m = f.create_group("matrix")
        m["shape"] = [36601, 5000]
        m.create_group("features")["feature_type"] = [b"Gene Expression"] * 3 + [b"Antibody Capture"]
    p = dv.hdf5_preview(str(path))
    assert p["cellranger"] == {"features": 36601, "barcodes": 5000,
                               "feature_types": {"Gene Expression": 3, "Antibody Capture": 1}}


def test_a_path_inside_a_folder_cannot_leave_it():
    assert dv.safe_relpath("outs/./metrics_summary.csv") == "outs/metrics_summary.csv"
    for bad in ("../secret", "a/../../b", "/etc/passwd", "", None, "a\x00b"):
        assert dv.safe_relpath(bad) is None


def test_a_folder_listing_skips_links_and_hidden_files(tmp_path):
    root = tmp_path / "run1"
    (root / "outs").mkdir(parents=True)
    (root / "outs" / "matrix.h5").write_bytes(b"x" * 10)
    (root / "a.csv").write_bytes(b"1")
    (root / ".DS_Store").write_bytes(b"junk")
    outside = tmp_path / "secret.txt"
    outside.write_text("no")
    os.symlink(outside, root / "link.txt")
    got = dv.local_folder_entries(root)
    assert got["entries"] == [{"path": "a.csv", "size": 1}, {"path": "outs/matrix.h5", "size": 10}]
    assert (got["total_files"], got["total_bytes"], got["truncated"]) == (2, 11, False)
    assert dv.local_folder_entries(root, limit=1)["truncated"] is True
    listing = dv.parse_find_listing("outs/matrix.h5\t10\na.csv\t1\n../x\t5\n.hidden\t3\n")
    assert listing["entries"] == [{"path": "a.csv", "size": 1}, {"path": "outs/matrix.h5", "size": 10}]


# --- the routes ---------------------------------------------------------------------------------------

@pytest.fixture
def gw(tmp_path, monkeypatch):
    gw_app = pytest.importorskip("aiscientist.gateway.app")
    pytest.importorskip("httpx")
    rows: dict[int, dict] = {}
    monkeypatch.setattr(gw_app, "CONSOLE_RUNS_DIR", tmp_path)
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_optional_user",
                        lambda request: types.SimpleNamespace(username="yijus12", id=5, role="user"))
    monkeypatch.setattr(gw_app.auth_routes, "get_dataset_record",
                        lambda uid, did: dict(rows[did], id=did) if did in rows and rows[did]["user"] == uid else None)
    monkeypatch.setattr(gw_app, "_active_conn_for_user", lambda user: None)
    monkeypatch.setattr(gw_app, "_hpc_uploads_dirs", lambda c: (HPC_UPLOADS, f"{HPC_LAB}/uploads"))
    monkeypatch.setattr(gw_app, "_storage_base", lambda c: HPC_LAB)
    gw_app._test_rows = rows
    return gw_app


def _client(gw_app):
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


def _row(gw_app, did, path, kind="file", user=5, name=None, size=0):
    gw_app._test_rows[did] = {"user": user, "path": str(path), "kind": kind,
                              "name": name or Path(str(path)).name, "size_bytes": size}


def _uploads(tmp_path) -> Path:
    d = tmp_path / "yijus12" / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_an_upload_on_this_host_previews_and_downloads(tmp_path, gw):
    f = _uploads(tmp_path) / "de.csv"
    f.write_bytes(b"gene,padj\nRHO,0.001\n")
    _row(gw, 1, f)
    c = _client(gw)
    r = c.get("/api/datasets/1/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["dataset"]["location"] == "server" and body["preview"]["rows"] == [["RHO", "0.001"]]
    d = c.get("/api/datasets/1/download")
    assert d.status_code == 200 and d.content == f.read_bytes()
    assert d.headers["content-disposition"].startswith('attachment; filename="de.csv"')
    assert d.headers["x-content-type-options"] == "nosniff"
    part = c.get("/api/datasets/1/download", headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == b"gene"          # a download can resume


def test_another_accounts_dataset_reads_nothing(tmp_path, gw):
    f = _uploads(tmp_path) / "x.csv"
    f.write_bytes(b"a,b\n1,2\n")
    _row(gw, 2, f, user=6)
    c = _client(gw)
    assert c.get("/api/datasets/2/preview").status_code == 404
    assert c.get("/api/datasets/2/download").status_code == 404


def test_a_row_pointing_outside_the_owners_storage_is_never_read(tmp_path, gw, monkeypatch):
    secret = tmp_path / "state" / ".env"
    secret.parent.mkdir()
    secret.write_text("AISCIENTIST_SECRET_KEY=x")
    _row(gw, 3, secret)
    c = _client(gw)
    assert c.get("/api/datasets/3/download").status_code == 409            # no session: not read here
    ex = _Hpc({})
    monkeypatch.setattr(gw, "_active_conn_for_user", lambda user: types.SimpleNamespace(executor=ex))
    r = c.get("/api/datasets/3/download")
    assert r.status_code == 403 and ex.calls == []                        # and not on HPC3 either
    _row(gw, 4, f"{HPC_UPLOADS}/../../other/x.csv")
    assert c.get("/api/datasets/4/preview").status_code == 403


def test_a_folder_lists_its_files_and_downloads_as_one_zip(tmp_path, gw):
    root = _uploads(tmp_path) / "Sample3"
    (root / "outs").mkdir(parents=True)
    (root / "outs" / "metrics_summary.csv").write_bytes(b"Estimated Number of Cells\n5000\n")
    (root / "notes.txt").write_bytes(b"hello\n")
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("not yours")
    os.symlink(outside, root / "escape.txt")
    _row(gw, 5, root, kind="folder")
    c = _client(gw)
    listing = c.get("/api/datasets/5/preview").json()["folder"]
    assert [e["path"] for e in listing["entries"]] == ["notes.txt", "outs/metrics_summary.csv"]
    one = c.get("/api/datasets/5/preview", params={"file": "outs/metrics_summary.csv"}).json()
    assert one["file"]["name"] == "metrics_summary.csv" and one["preview"]["type"] == "text"
    assert c.get("/api/datasets/5/preview", params={"file": "../../x"}).status_code == 400
    assert c.get("/api/datasets/5/preview", params={"file": "escape.txt"}).status_code == 404
    z = c.get("/api/datasets/5/download")
    assert z.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert names == ["Sample3/notes.txt", "Sample3/outs/metrics_summary.csv"]


def test_only_images_and_pdfs_open_inline(tmp_path, gw):
    up = _uploads(tmp_path)
    (up / "umap.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (up / "fig.svg").write_bytes(b"<svg onload='alert(1)'/>")
    _row(gw, 6, up / "umap.png")
    _row(gw, 7, up / "fig.svg")
    c = _client(gw)
    png = c.get("/api/datasets/6/download", params={"inline": 1})
    assert png.headers["content-type"] == "image/png" and png.headers["content-disposition"].startswith("inline")
    svg = c.get("/api/datasets/7/download", params={"inline": 1})
    assert svg.headers["content-type"] == "application/octet-stream"
    assert svg.headers["content-disposition"].startswith("attachment")


def test_a_bound_server_folder_is_read_where_it_is(tmp_path, gw, monkeypatch):
    from aiscientist.gateway import server_data as sd
    shared = tmp_path / "Users" / "shared"
    (shared / "lab").mkdir(parents=True)
    (shared / "lab" / "meta.tsv").write_bytes(b"sample\tage\nS1\t3\n")
    monkeypatch.setattr(sd, "server_data_roots", lambda env=None: (shared.resolve(),))
    _row(gw, 8, shared / "lab", kind="server-folder")
    c = _client(gw)
    assert [e["path"] for e in c.get("/api/datasets/8/preview").json()["folder"]["entries"]] == ["meta.tsv"]


class _Hpc:
    """An HPC3 session holding ``files`` (path -> bytes): answers the probes and reads."""

    def __init__(self, files: dict[str, bytes], h5ad: "Path | None" = None):
        self.files, self.h5ad, self.calls, self.read = files, h5ad, [], []

    def _dirs(self):
        return {p.rsplit("/", i)[0] for p in self.files for i in range(1, p.count("/"))}

    def exec(self, cmd, timeout: float = 60.0):
        self.calls.append(cmd)
        out, ok = "", True
        if cmd.startswith("if test -d"):
            path = cmd.split("test -d ", 1)[1].split(";", 1)[0].strip("'")
            out = "dir" if path in self._dirs() else "file" if path in self.files else ""
        elif cmd.startswith("test -f "):
            ok = cmd[len("test -f "):].strip("'") in self.files
        elif cmd.startswith("find "):
            root = cmd.split("find ", 1)[1].split(" -type", 1)[0].strip("'")
            out = "".join(f"{p[len(root) + 1:]}\t{len(b)}\n" for p, b in self.files.items() if p.startswith(root + "/"))
        return types.SimpleNamespace(ok=ok, stdout=out, stderr="")

    def read_bytes(self, path, max_bytes=None):
        self.read.append(path)
        return self.files.get(path, b"")[:max_bytes]

    def remote_size(self, path):
        return len(self.files.get(path, b""))

    def read_stream(self, path):
        self.read.append(path)
        body = self.files[path]
        for i in range(0, len(body), 7):
            yield body[i:i + 7]

    @contextmanager
    def open_read(self, path):
        self.read.append(path)
        with open(self.h5ad, "rb") as fh:
            yield fh


def test_a_dataset_on_hpc3_is_read_there_through_the_owners_session(tmp_path, gw, monkeypatch):
    path = f"{HPC_UPLOADS}/run1/calls.vcf"
    ex = _Hpc({path: VCF, f"{HPC_UPLOADS}/run1/sheet.csv": b"s,c\nS1,ko\n"})
    monkeypatch.setattr(gw, "_active_conn_for_user", lambda user: types.SimpleNamespace(executor=ex))
    _row(gw, 9, f"{HPC_UPLOADS}/run1", kind="folder")
    c = _client(gw)
    folder = c.get("/api/datasets/9/preview").json()
    assert folder["dataset"]["location"] == "hpc3"
    assert [e["path"] for e in folder["folder"]["entries"]] == ["calls.vcf", "sheet.csv"]
    vcf = c.get("/api/datasets/9/preview", params={"file": "calls.vcf"}).json()["preview"]
    assert vcf["type"] == "vcf" and vcf["samples"] == ["S1", "S2"]
    d = c.get("/api/datasets/9/download", params={"file": "calls.vcf"})
    assert d.content == VCF and d.headers["content-length"] == str(len(VCF))
    names = zipfile.ZipFile(io.BytesIO(c.get("/api/datasets/9/download").content)).namelist()
    assert names == ["run1/calls.vcf", "run1/sheet.csv"]
    assert c.get("/api/datasets/9/preview", params={"file": "missing.csv"}).status_code == 404


def test_a_remote_h5ad_is_read_by_ranges_not_downloaded(tmp_path, gw, monkeypatch, h5ad_files):
    path = f"{HPC_LAB}/AiScientist-results/pbmc.processed.h5ad"
    ex = _Hpc({path: b"\x89HDF"}, h5ad=h5ad_files[0])
    monkeypatch.setattr(gw, "_active_conn_for_user", lambda user: types.SimpleNamespace(executor=ex))
    _row(gw, 10, path, kind="h5ad")
    p = _client(gw).get("/api/datasets/10/preview").json()["preview"]
    assert (p["n_obs"], p["n_vars"]) == (40, 25)
    assert ex.read == [path]                                             # opened once, by range


def test_a_dataset_on_hpc3_without_a_session_says_to_connect(tmp_path, gw):
    _row(gw, 11, f"{HPC_UPLOADS}/x.h5ad", kind="h5ad")
    r = _client(gw).get("/api/datasets/11/preview")
    assert r.status_code == 409 and "Connect to HPC3" in r.json()["error"]
