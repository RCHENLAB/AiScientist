"""A run's manifest.json records what it ran with (RCHENLAB issue 29): the app release, the skills
release and skills, the images of the session's HPC3 jobs with their pinned SHA-256, and the models
with their pinned revisions (configs/hpc3_assets.yaml).
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("yaml")

from aiscientist.agents import provenance, run_manifest  # noqa: E402
from aiscientist.agents.research_harness import HarnessContext  # noqa: E402
from aiscientist.core import hpc3_assets  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SHA_A = "a" * 64
REV = "b" * 40

LIST = f"""
schema_version: 1
images:
  vllm-0.28.0: {{path: containers/vllm-0.28.0.sif, source: "docker://vllm/vllm-openai:v0.28.0", version: "0.28.0", sha256: {SHA_A}}}
  analysis: {{path: containers/analysis.sif, source: deploy/analysis/analysis.def, sha256: null}}
models:
  RedHatAI/Qwen3.8-27B-INT4:
    source: huggingface
    path: hf/hub/models--RedHatAI--Qwen3.8-27B-INT4
    revision: {REV}
    files: {{model-00001-of-00002.safetensors: {"1" * 64}, model-00002-of-00002.safetensors: {"2" * 64}}}
  MJPansa/DeepSeek-V4-Flash-0731-NVFP4: {{source: huggingface, revision: {"c" * 40}, files: {{}}}}
"""


@dataclass
class Settings:
    vllm_model: str = "RedHatAI/Qwen3.8-27B-INT4"
    vllm_image: str = "/dfs3b/lab/software/AiScientist/containers/vllm-0.28.0.sif"
    vllm_quantization: str = ""
    analysis_image: str = "/dfs3b/lab/software/AiScientist/containers/analysis.sif"
    report_image: str = "/somewhere/else/report-test.sif"
    vep_image: str = ""
    lirical_image: str = ""
    scgpt_image: str = ""
    vlreview_image: str = ""


@pytest.fixture
def pins(tmp_path, monkeypatch):
    path = tmp_path / "hpc3_assets.yaml"
    path.write_text(LIST)
    monkeypatch.setenv("AISCIENTIST_HPC3_ASSETS", str(path))
    return path


def test_the_repository_list_is_well_formed():
    """configs/hpc3_assets.yaml: every image has a path and a source, a checksum is a SHA-256, a
    model's revision is a 40-hex commit and its files map names to SHA-256."""
    import yaml
    doc = yaml.safe_load((ROOT / "configs" / "hpc3_assets.yaml").read_text())
    assert doc["schema_version"] == 1
    paths = []
    for name, img in doc["images"].items():
        assert img["path"].startswith("containers/") and img["path"].endswith(".sif"), name
        assert img["source"], name
        assert img["sha256"] is None or re.fullmatch(r"[0-9a-f]{64}", img["sha256"]), name
        paths.append(img["path"])
    assert len(paths) == len(set(paths))
    for repo, m in doc["models"].items():
        assert m["revision"] is None or re.fullmatch(r"[0-9a-f]{40}", m["revision"]), repo
        assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in (m.get("files") or {}).values()), repo
        assert m.get("image") in doc["images"], repo


def test_the_settings_defaults_name_listed_images():
    """The images the code uses by default are on the list, so a run records their pins."""
    from aiscientist.gateway.settings import HPCSettings

    s = HPCSettings()
    for field in ("vllm_image", "analysis_image", "report_image", "vep_image", "lirical_image",
                  "scgpt_image", "vlreview_image"):
        assert hpc3_assets.image_pin(getattr(s, field)) is not None, field
    assert hpc3_assets.model_pin(s.vllm_model) is not None


def test_pins_by_path_and_by_model(pins):
    assert hpc3_assets.image_pin("/x/containers/vllm-0.28.0.sif") == {
        "name": "vllm-0.28.0", "sha256": SHA_A, "source": "docker://vllm/vllm-openai:v0.28.0", "version": "0.28.0"}
    assert hpc3_assets.image_pin("/x/containers/other.sif") is None
    assert hpc3_assets.image_pin("/x/not-containers/vllm-0.28.0.sif") is None
    pin = hpc3_assets.model_pin("RedHatAI/Qwen3.8-27B-INT4")
    assert pin["revision"] == REV and pin["files"] == 2 and re.fullmatch(r"[0-9a-f]{64}", pin["files_sha256"])
    assert hpc3_assets.model_pin("nobody/nothing") is None

    pins.write_text("images: [not a mapping")                  # a broken list never breaks a run
    assert hpc3_assets.image_pin("/x/containers/vllm-0.28.0.sif") is None


def test_a_run_records_its_app_images_and_models(pins, tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("tag=v0.4.0\nsha=" + "d" * 40 + "\n")
    monkeypatch.setattr(provenance, "_REPO_ROOT", tmp_path)
    ctx = HarnessContext(workspace=tmp_path / "run")
    run_manifest.init(ctx, "v0.1.0")
    run_manifest.record_environment(ctx, Settings())
    run_manifest.record_models(ctx, [{"model": "RedHatAI/Qwen3.8-27B-INT4", "provider": "vllm"},
                                     {"model": "MJPansa/DeepSeek-V4-Flash-0731-NVFP4", "provider": "vllm"},
                                     {"model": "claude-sonnet-5-5", "provider": "anthropic"}])
    doc = json.loads((ctx.workspace / "manifest.json").read_text())
    assert doc["app"] == {"tag": "v0.4.0", "sha": "d" * 40}
    assert doc["registry_release"] == "v0.1.0" and doc["skills"] == []
    assert doc["images"] == {
        "analysis": {"path": Settings.analysis_image, "name": "analysis", "sha256": None},
        "report": {"path": "/somewhere/else/report-test.sif", "name": None, "sha256": None},
        "model_server": {"path": Settings.vllm_image, "name": "vllm-0.28.0", "sha256": SHA_A}}
    server, *called = doc["models"]
    assert server["role"] == "session model server" and server["revision"] == REV
    assert server["image"] == Settings.vllm_image and server["files_sha256"]
    assert [(m["id"], m["revision"]) for m in called] == [
        ("RedHatAI/Qwen3.8-27B-INT4", REV), ("MJPansa/DeepSeek-V4-Flash-0731-NVFP4", "c" * 40),
        ("claude-sonnet-5-5", None)]
    assert called[2]["provider"] == "anthropic"

    run_manifest.record_models(ctx, [{"model": "claude-sonnet-5-5", "provider": "anthropic"}])
    doc = json.loads((ctx.workspace / "manifest.json").read_text())   # the end's record replaces itself
    assert [m["id"] for m in doc["models"]] == ["RedHatAI/Qwen3.8-27B-INT4", "claude-sonnet-5-5"]


def test_parallel_steps_add_skills_without_losing_one(pins, tmp_path):
    from aiscientist.agents import skills_registry
    from aiscientist.agents.skills import Skill

    ctx = HarnessContext(workspace=tmp_path / "run")
    skills = [Skill(name=f"s{i}", summary="", doc="", version="1.0.0", registry_release="v0.1.0",
                    image="docker://x/y:1" if i == 0 else "") for i in range(12)]
    threads = [threading.Thread(target=skills_registry.record_in_manifest, args=(ctx, s)) for s in skills]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    doc = json.loads((ctx.workspace / "manifest.json").read_text())
    assert sorted(s["name"] for s in doc["skills"]) == sorted(f"s{i}" for i in range(12))
    assert next(s for s in doc["skills"] if s["name"] == "s0")["image"] == "docker://x/y:1"
    assert "image" not in next(s for s in doc["skills"] if s["name"] == "s1")


def _check_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hpc3_assets_check", ROOT / "scripts" / "hpc3_assets_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_hpc3_check_reads_revisions_and_checksums_off_the_cache(tmp_path, capsys):
    """scripts/hpc3_assets_check.py: an HF cache gives the revision and each weight's SHA-256 without
    reading the weights; a changed image or a missing file is reported."""
    import hashlib
    check = _check_module()
    root = tmp_path / "shared"
    (root / "containers").mkdir(parents=True)
    (root / "containers" / "vllm.sif").write_bytes(b"image bytes")
    model = root / "hf" / "hub" / "models--org--m"
    (model / "refs").mkdir(parents=True)
    (model / "refs" / "main").write_text(REV)
    (model / "blobs").mkdir()
    snap = model / "snapshots" / REV
    snap.mkdir(parents=True)
    for name, data in (("model-00001.safetensors", b"w1"), ("config.json", b"{}")):
        digest = hashlib.sha256(data).hexdigest()
        (model / "blobs" / digest).write_bytes(data)
        (snap / name).symlink_to(f"../../blobs/{digest}")
    doc = {"images": {"vllm": {"path": "containers/vllm.sif", "sha256": "0" * 64}},
           "models": {"org/m": {"path": "hf/hub/models--org--m", "revision": None, "files": {}}}}
    lst = tmp_path / "list.json"
    lst.write_text(json.dumps(doc))

    assert check.main([str(lst), "--root", str(root)]) == 0          # no --hash: images only exist
    found = json.loads(capsys.readouterr().out)
    assert found["models"]["org/m"] == {"revision": REV,
                                        "files": {"model-00001.safetensors": hashlib.sha256(b"w1").hexdigest()}}

    assert check.main([str(lst), "--root", str(root), "--hash"]) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["images"]["vllm"]["sha256"] == hashlib.sha256(b"image bytes").hexdigest()
    assert "DIFFERS: image vllm" in out.err

    doc["images"]["vllm"]["sha256"] = hashlib.sha256(b"image bytes").hexdigest()
    doc["models"]["org/m"].update(revision=REV, files={"model-00001.safetensors": hashlib.sha256(b"w1").hexdigest(),
                                                       "model-00002.safetensors": "f" * 64})
    lst.write_text(json.dumps(doc))
    assert check.main([str(lst), "--root", str(root), "--hash"]) == 1
    assert "model-00002.safetensors is missing" in capsys.readouterr().err


def test_the_hpc3_check_reads_a_local_dir_download(tmp_path, capsys):
    """Weights downloaded with `hf download --local-dir` carry their revision and SHA-256 in
    .cache/huggingface/download/<file>.metadata (commit, etag, time); with --hash the files are
    checked against it."""
    import hashlib
    check = _check_module()
    folder = tmp_path / "shared" / "vlreview_model"
    meta = folder / ".cache" / "huggingface" / "download"
    meta.mkdir(parents=True)
    (folder / "model-00001-of-00001.safetensors").write_bytes(b"weights")
    (folder / "config.json").write_text("{}")
    good = hashlib.sha256(b"weights").hexdigest()
    (meta / "model-00001-of-00001.safetensors.metadata").write_text(f"{REV}\n{good}\n1783026760.9\n")
    (meta / "config.json.metadata").write_text(f"{REV}\n{'e' * 40}\n1783026760.9\n")     # a git blob id
    lst = tmp_path / "list.json"
    lst.write_text(json.dumps({"images": {}, "models": {"Qwen/VL": {"path": "vlreview_model", "files": {}}}}))

    assert check.main([str(lst), "--root", str(tmp_path / "shared")]) == 0
    assert json.loads(capsys.readouterr().out)["models"]["Qwen/VL"] == {
        "revision": REV, "files": {"model-00001-of-00001.safetensors": good}}

    (folder / "model-00001-of-00001.safetensors").write_bytes(b"changed")
    assert check.main([str(lst), "--root", str(tmp_path / "shared"), "--hash"]) == 1
    assert "its download recorded" in capsys.readouterr().err
