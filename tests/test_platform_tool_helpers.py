"""Upload/report helpers stay available independently of retiring tool folders."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def test_platform_api_works_with_retiring_tool_modules_unavailable(tmp_path):
    vcf = tmp_path / "query.vcf"
    vcf.write_text('##fileformat=VCFv4.2\n##reference=GRCh38\n##contig=<ID=1,length=248956422>\n'
                   '#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t12\t.\tA\tG\t.\tPASS\t.\n')
    script = r'''
import importlib.abc,json,sys
from pathlib import Path
blocked = ("aiscientist.tools.inspect_dataset", "aiscientist.tools.make_schematic",
           "aiscientist.tools.run_marker_annotation", "aiscientist.tools.annotate_variants")
class BlockTools(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == root or fullname.startswith(root+".") for root in blocked):
            raise ImportError("Retiring tool must not be imported: "+fullname)
sys.meta_path.insert(0, BlockTools())
from aiscientist.tools import api
peek = api.peek_dataset(sys.argv[1])
assert peek["readable"] and api.describe_dataset(peek,chat_fn=None)["assembly"] == "GRCh38"
assert api.detect_assembly(Path(sys.argv[1]).read_text()) == "GRCh38"
assert "retina" in api.available_references()
assert api.load_marker_reference("retina")
assert api.load_marker_reference("not-a-reference") is None
assert "digraph workflow" in api.workflow_schematic_dot(["QC", "Clustering"])
from aiscientist.tools._lib import schematics
schematics.shutil.which = lambda name: None
figure = api.render_workflow_schematic(["QC", "Clustering"], Path(sys.argv[2]))
assert figure["status"] == "dependency_missing" and Path(figure["dot_path"]).is_file()
assert not any(name == root or name.startswith(root+".") for root in blocked for name in sys.modules)
print(json.dumps({"format":peek["detected_format"],"reference":"retina","figure":figure["status"]}))
'''
    result = subprocess.run([sys.executable, "-c", script, str(vcf), str(tmp_path / "figures")],
                            check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["reference"] == "retina"


def test_legacy_inspection_factory_still_uses_its_patchable_entrypoint(tmp_path, monkeypatch):
    from aiscientist.tools.inspect_dataset import tool

    query = tmp_path / "query.tsv"
    query.write_text("name\tvalue\nx\t1\n")
    seen = []
    monkeypatch.setattr(tool, "inspect_dataset", lambda path, **kwargs: seen.append(path) or {"status": "sentinel"})
    ctx = SimpleNamespace(workspace=tmp_path, decisions={"dataset_path": str(query)}, tunnel_port=None)
    result = tool.make_inspect_dataset_tool().executor({}, ctx)
    assert seen == [str(query)] and result["status"] == "sentinel"


def test_legacy_schematic_factory_and_shared_process_patches_remain_supported(tmp_path, monkeypatch):
    from aiscientist.tools._lib import schematics
    from aiscientist.tools.make_schematic import tool

    assert tool.shutil is schematics.shutil and tool.subprocess is schematics.subprocess
    seen = []
    monkeypatch.setattr(tool, "render_dot", lambda source, directory, **kwargs:
                        seen.append((source, directory)) or {"status": "sentinel"})
    result = tool.make_schematic_tool().executor({"source": "digraph { a -> b }"},
                                               SimpleNamespace(workspace=tmp_path))
    assert result["status"] == "sentinel"
    assert seen == [("digraph { a -> b }", tmp_path / "artifacts/figures")]


def test_curated_reference_copy_preserves_the_existing_resource():
    from aiscientist.tools._lib import marker_references
    from aiscientist.tools.run_marker_annotation import tool

    shared = marker_references._REFERENCES / "retina.json"
    legacy = Path(tool.__file__).with_name("references") / "retina.json"
    assert shared.read_bytes() == legacy.read_bytes()
    assert tool._load_reference("retina") == marker_references.load_reference("retina")


def test_cellranger_profiler_reads_the_packaged_shared_markers():
    from aiscientist.tools._lib import cellranger, marker_references

    assert cellranger._MARKER_REFERENCES == marker_references._REFERENCES
    assert "curated retina reference" in cellranger.tissue_hint(["RHO", "GNAT1", "PDE6B", "NRL"])
