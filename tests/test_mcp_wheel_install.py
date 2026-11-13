"""Install the actual wheel plus only its base dependencies, entirely offline.

Dependency wheels are reconstructed from installed distributions, including their
real METADATA/WHEEL and native libraries. pip resolves the app's wheel metadata in
a fresh venv; no source path or system-site-packages can mask missing runtime data
or dependencies. The input build tree matches Docker's Python package inputs.
"""
import base64
import csv
from email import message_from_bytes
import hashlib
from importlib import metadata
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
import zipfile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parents[1]


def dependency_wheel(dist, destination):
    """Repackage the installed distribution's files, never invent dependency metadata."""
    wheel_file = next(f for f in dist.files if str(f).endswith(".dist-info/WHEEL"))
    info_dir = str(wheel_file).rsplit("/", 1)[0]
    tags = message_from_bytes(dist.locate_file(wheel_file).read_bytes()).get_all("Tag")
    name = canonicalize_name(dist.metadata["Name"]).replace("-", "_")
    target = destination / f"{name}-{dist.version}-{tags[0]}.whl"
    records = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in dist.files:
            path = Path(str(entry))
            if path.is_absolute() or ".." in path.parts or path.suffix == ".pyc" or path.name == "RECORD":
                continue
            data = dist.locate_file(entry).read_bytes()
            archive.writestr(str(entry), data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
            records.append((str(entry), "sha256=" + digest, len(data)))
        record = info_dir + "/RECORD"
        stream = io.StringIO()
        csv.writer(stream).writerows([*records, (record, "", "")])
        archive.writestr(record, stream.getvalue())


def test_base_wheel_contains_registry_and_can_register_atlas(tmp_path, skill_release):
    source, wheels, empty = (tmp_path / n for n in ("build", "wheels", "empty"))
    for path in (source, wheels, empty):
        path.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(REPO / name, source / name)
    shutil.copytree(REPO / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    subprocess.run([sys.executable, "-c", "from setuptools.build_meta import build_wheel; "
                    f"build_wheel({str(wheels)!r})"], cwd=source, capture_output=True, text=True, check=True)
    app_wheel, = wheels.glob("aiscientist-*.whl")
    with zipfile.ZipFile(app_wheel) as archive:
        info = message_from_bytes(archive.read(next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))))
        assert "aiscientist/agents/mcp_catalog/mcp_servers.json" in archive.namelist()
        assert "aiscientist/agents/mcp_catalog/mcp/atlas-expression-exploration-0.0.3.tools.json" in archive.namelist()
        assert "aiscientist/tools/run_scanpy_qc/TOOL.md" in archive.namelist()
        assert "aiscientist/tools/_lib/marker_references/retina.json" in archive.namelist()

    pending = list(info.get_all("Requires-Dist", []))
    installed = set()
    environment = {**default_environment(), "extra": ""}
    while pending:
        requirement = Requirement(pending.pop())
        name = canonicalize_name(requirement.name)
        if name in installed or (requirement.marker and not requirement.marker.evaluate(environment)):
            continue
        dist = metadata.distribution(requirement.name)
        assert requirement.specifier.contains(dist.version, prereleases=True)
        dependency_wheel(dist, wheels)
        installed.add(name)
        pending.extend(dist.requires or [])
    assert "jsonschema" in installed and not {"fastapi", "mcp", "pyyaml"} & installed

    prefix = tmp_path / "base-env"
    venv.EnvBuilder(with_pip=True).create(prefix)
    python = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AISCIENTIST_", "PYTHON", "PIP_"))}
    subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--find-links", str(wheels),
                    f"aiscientist=={info['Version']}"], cwd=empty, env=env, capture_output=True, text=True, check=True)
    # No runtime config is supplied for MCP. Only the existing skill release uses
    # its deployment override, as a wheel installation's operator would configure it.
    release = skill_release(REPO / "tests/fixtures/atlas_expression", tag="v0.2.0")
    skill_config = tmp_path / "skills.json"
    skill_config.write_text(json.dumps({"release_tag": "v0.2.0", "releases_root": str(release.parent),
                                        "allowed_tiers": ["community"], "current": ""}))
    env["AISCIENTIST_SKILLS_REGISTRY_CONFIG"] = str(skill_config)
    env["AISCIENTIST_ATLAS_FILES"] = "fixture/v1=/fixture/atlas.h5ad"
    probe = '''
import importlib.util, json, sys
from pathlib import Path
import aiscientist
from aiscientist.agents.mcp_bridge import DEFAULT_CONFIG, load_servers
from aiscientist.agents.registry import build_scientist_catalog
from aiscientist.gateway.system_info import system_overview
assert Path(aiscientist.__file__).is_relative_to(Path(sys.prefix))
assert DEFAULT_CONFIG.is_relative_to(Path(sys.prefix))
from aiscientist.tools import api
assert 'retina' in api.available_references()
assert api.load_marker_reference('retina')['species']
assert api.detect_assembly('##contig=<ID=1,length=248956422>') == 'GRCh38'
assert 'digraph workflow' in api.workflow_schematic_dot(['QC', 'Clustering'])
query = Path('query.tsv')
query.write_text('name\\tvalue\\nx\\t1\\n')
assert api.peek_dataset(str(query))['readable']
assert all(importlib.util.find_spec(name) is None for name in ('mcp', 'fastapi', 'yaml'))
servers = load_servers()
assert {server.name for server in servers} >= {'atlas-expression-exploration', 'variant-annotation', 'scgpt-annotation'}
assert len(servers[0].tools) == 9
plain = {tool.name for tool in build_scientist_catalog()}
assert 'run_scanpy_qc' in plain and 'resolve_genes' not in plain
assert any(tool['name'] == 'run_scanpy_qc' for tool in system_overview()['tools'])
class Runner:
    supports_skill_policy = True
    def run_in_image(self, *args, **kwargs):
        raise AssertionError('registration must not execute a job')
enabled = {tool.name for tool in build_scientist_catalog(code_executor=Runner())}
assert {servers[0].planner_name(tool['name']) for tool in servers[0].tools} <= enabled
print(json.dumps({'base_tools': len(plain), 'atlas_tools': len(servers[0].tools)}))
'''
    result = subprocess.run([str(python), "-I", "-c", probe], cwd=empty, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["atlas_tools"] == 9
