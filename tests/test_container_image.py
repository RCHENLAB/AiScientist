"""The service image, its Kubernetes manifests and the release job that publishes it keep the shape
deploy/k8s/README.md describes.

The gateway holds each session's SSH connection to HPC3 in memory, so the Deployment is a singleton.
The image is one release package installed from its lock; the release workflow builds it after the
package job, smoke-tests it before it pushes, and uses no third-party action with a write token.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DOCKERFILE = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
GUIDE = (DEPLOY / "k8s" / "README.md").read_text(encoding="utf-8")


def _objects(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((DEPLOY / "k8s" / name).read_text(encoding="utf-8")) if d]


def _deployment() -> dict:
    return next(d for d in _objects("aiscientist.yaml") if d["kind"] == "Deployment")


def _container() -> dict:
    return _deployment()["spec"]["template"]["spec"]["containers"][0]


def test_the_deployment_is_a_singleton():
    spec = _deployment()["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"] == {"type": "Recreate"}


def test_the_probes_read_health():
    container = _container()
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["path"] == "/health"


def test_the_image_is_set_to_a_digest_by_the_guides_command():
    placeholder = "ghcr.io/rchenlab/aiscientist@sha256:<digest>"
    assert _container()["image"] == placeholder
    assert f'sed "s#{placeholder}#$IMAGE#" deploy/k8s/aiscientist.yaml' in GUIDE


def test_the_pod_matches_the_image():
    user = re.search(r"^USER (\d+):(\d+)$", DOCKERFILE, re.M)
    assert user, "the Dockerfile names no numeric USER"
    pod = _deployment()["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == int(user.group(1))
    assert pod["securityContext"]["fsGroup"] == int(user.group(2))
    container = pod["containers"][0]
    (mount,) = (m["mountPath"] for m in container["volumeMounts"])
    assert f"AISCIENTIST_STATE_DIR={mount}/state" in DOCKERFILE
    assert container["ports"][0]["containerPort"] == 8800
    assert '"--port", "8800"' in DOCKERFILE


def test_every_secret_and_configmap_the_manifests_read_is_made_in_the_guide():
    names = set()
    for name in ("aiscientist.yaml", "postgres.yaml"):
        for obj in _objects(name):
            text = yaml.safe_dump(obj)
            names |= set(re.findall(r"(?:secretRef|configMapRef|secretKeyRef):\s*\n\s*(?:key: \S+\s*\n\s*)?name: (\S+)", text))
    assert names == {"aiscientist-secrets", "aiscientist-config", "aiscientist-postgres"}
    for name in names:
        assert re.search(rf"create (?:secret generic|configmap) {name}\b", GUIDE), name


def test_the_image_is_the_release_installed_from_its_lock():
    assert "--require-hashes -r requirements.lock" in DOCKERFILE
    assert "--no-deps --no-build-isolation -e /app" in DOCKERFILE
    assert "bash deploy/deploy_skills.sh" in DOCKERFILE
    assert "deploy/container_entrypoint.sh" in DOCKERFILE
    runtime = re.search(r"^RUNTIME=\(([^)]*)\)", (DEPLOY / "build_package.sh").read_text(), re.M | re.S)
    assert {"src", "frontend", "configs", "deploy", "requirements.lock"} <= set(runtime.group(1).split())


def test_the_release_job_tests_the_image_before_it_publishes_it():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    image = jobs["image"]
    assert image["needs"] == "package"
    assert image["permissions"] == {"contents": "write", "packages": "write"}
    uses = {step["uses"] for job in jobs.values() for step in job["steps"] if "uses" in step}
    assert uses == {"actions/checkout@v4"}
    runs = [step.get("run", "") for step in image["steps"]]
    first = lambda needle: next(i for i, run in enumerate(runs) if needle in run)  # noqa: E731
    assert first("deploy/build_image.sh") < first("deploy/smoke_test_image.sh") < first("docker push")
    assert first("docker push") < first("ghcr.io/token") < first("gh release edit")
    assert not any("--force" in run for run in runs)
