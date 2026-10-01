"""One folder per tool: every tools/<name>/TOOL.md manifest agrees with the code it describes.

The registry, the HPC3 routing, the fast-chat selection and the System page all read these
manifests, so a manifest that drifts from its tool misroutes it silently. These checks make the
drift loud. (They move with AiScientist-tools; the platform's use of the manifests is checked in
tests/test_registry_manifests.py.)"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from aiscientist.tools import catalog

TOOLS = Path(catalog.TOOLS_DIR)
ROOT = TOOLS.parents[2]          # the repository that holds src/aiscientist/tools and scripts/

# Directories under tools/ that are not tools: shared code and reference data.
NOT_TOOLS = {"_lib", "genesets", "gene_panels", "__pycache__"}


def test_every_tool_folder_has_a_manifest():
    folders = {p.name for p in TOOLS.iterdir() if p.is_dir() and p.name not in NOT_TOOLS}
    with_manifest = {p.name for p in TOOLS.iterdir() if (p / "TOOL.md").is_file()}
    assert folders == with_manifest, f"folders without TOOL.md: {sorted(folders - with_manifest)}"


def test_each_manifest_builds_its_own_tool():
    for m in catalog.manifests():
        tool = catalog.build(m)
        assert tool.name == m.name == m.folder.name
        assert tool.category == m.category, f"{m.name}: TOOL.md says {m.category}, the tool says {tool.category}"
        assert m.summary and len(m.summary) <= 200, f"{m.name}: summary must be one short line"


def test_composites_name_tools_that_exist():
    names = set(catalog.names())
    for m in catalog.manifests():
        for kw, src in m.injected():
            if src.startswith("tool:"):
                assert src[5:] in names, f"{m.name} needs {src}, which no folder provides"


def test_tool_docs_are_current():
    # TOOL.md's generated sections (parameters, where it runs, the model's description) and the
    # index tools/README.md are rendered from the code; regenerate with `python scripts/tool_docs.py`.
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "tool_docs.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, "stale tool docs, run `python scripts/tool_docs.py`:\n" + r.stdout + r.stderr
