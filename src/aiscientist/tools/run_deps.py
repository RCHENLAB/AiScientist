"""Install a tool's missing dependency for ONE run, inside the analysis container.

A curated tool that needs a library the image lacks reports ``dependency_missing`` and stops, and
until now the only fix was an image rebuild. Measured on HPC3 (2026-09-30): ``analysis.sif`` has no
scikit-image, so ``run_doublet_detection`` has never been able to run there.

The gateway's :class:`~aiscientist.gateway.slurm_analysis.SlurmAnalysisExecutor` calls :func:`install`
(through ``scrna_cli``, as a Slurm job in the same image) when a tool reports a dependency listed in
:data:`ALLOWED`, then retries the tool once. The rules (Yijun, 2026-09-30: install temporarily,
clean up when the run ends):

* **Only what a tool declares.** The package and its version are fixed here, keyed by the name the
  tool reports. A model-chosen name never reaches this path: run_code keeps its one-confirmation
  preflight, because a hallucinated name can be a typosquatted package.
* **This run only.** The install goes into the run's own HPC3 workspace (``<run>/_deps``). The
  gateway deletes it when the run is published, and the Temp sweep is the backstop. No other run
  or lab member imports from it.
* **The image stays in charge.** Wheels only, so no install-time scripts run. Every top-level
  module the image already provides is removed before use. The directory is APPENDED to
  ``sys.path`` by the generated ``sitecustomize``, never prepended, so nothing installed here can
  outrank the image's numpy, scipy or pandas.
* **Proven before use.** The package must import in a fresh interpreter set up exactly like the
  tool job, from the installed copy, or the install is reported as failed and removed.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

#: Dependency name as a tool reports it -> (pinned pip requirement, import name). Both spellings a
#: tool can report are listed: scanpy 1.12 says "scikit-image", scanpy 1.11.5 says "skimage".
ALLOWED: dict[str, tuple[str, str]] = {
    "scikit-image": ("scikit-image==0.25.2", "skimage"),
    "skimage": ("scikit-image==0.25.2", "skimage"),
}

DEPS_DIRNAME = "_deps"
PIP_TIMEOUT_S = 900
VERIFY_TIMEOUT_S = 300


def resolve(dependency: str) -> tuple[str, str] | None:
    """The (requirement, import name) a tool-reported dependency maps to, or None if undeclared."""
    return ALLOWED.get((dependency or "").strip())


def deps_root(workspace: str | Path) -> Path:
    return Path(workspace) / DEPS_DIRNAME


def _slug(requirement: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", requirement.lower().replace("==", "-")).strip("-.")


def _tail(text: str, n: int = 800) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def _top_level_name(entry: Path) -> str | None:
    """The import name a top-level entry of a ``--target`` tree provides, or None for metadata."""
    name = entry.name
    if name.endswith((".dist-info", ".egg-info", ".data")) or name in ("bin", "__pycache__"):
        return None
    if entry.is_dir():
        # `numpy.libs/`, `scikit_image.libs/`: the shared libraries a manylinux wheel vendors. Not
        # importable, and looking one up would import its parent package, so it is left alone.
        return None if "." in name else name
    if name.endswith(".py"):
        return name[:-3]
    if name.endswith((".so", ".pyd")):
        return name.split(".", 1)[0]
    return None


def _image_provides(module: str) -> bool:
    """True if this interpreter (the image, plus the live source and pydeps) can already import it.
    The staging tree is never on ``sys.path`` here, so it cannot answer for itself."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _dist_top_levels(dist_info: Path) -> set[str]:
    top = dist_info / "top_level.txt"
    if top.is_file():
        return {ln.strip() for ln in top.read_text(encoding="utf-8").splitlines() if ln.strip()}
    record = dist_info / "RECORD"
    names: set[str] = set()
    if record.is_file():
        for ln in record.read_text(encoding="utf-8").splitlines():
            first = ln.split(",", 1)[0].split("/", 1)[0]
            if first and not first.endswith((".dist-info", ".data")) and first != "..":
                names.add(first[:-3] if first.endswith(".py") else first.split(".", 1)[0])
    return names


def _prune_image_modules(tree: Path) -> list[str]:
    """Remove every top-level module the image already provides, and the metadata of packages left
    with nothing. Returns the module names removed."""
    pruned: list[str] = []
    for entry in sorted(tree.iterdir()):
        if entry.name in ("bin", "__pycache__"):
            shutil.rmtree(entry, ignore_errors=True)
            continue
        module = _top_level_name(entry)
        if module and _image_provides(module):
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            pruned.append(module)
    remaining = {n for n in (_top_level_name(e) for e in tree.iterdir()) if n}
    for dist in tree.glob("*.dist-info"):
        tops = _dist_top_levels(dist)
        if tops and not (tops & remaining):
            # The pruned package's vendored shared libraries (`numpy.libs/` …) go with it: nothing
            # loads them once the image's own copy is used. Measured on HPC3: 116 MB of the
            # scikit-image install was numpy/scipy/pillow `.libs`.
            for libs in _record_libs_dirs(dist):
                shutil.rmtree(tree / libs, ignore_errors=True)
            shutil.rmtree(dist, ignore_errors=True)
    return pruned


def _record_libs_dirs(dist_info: Path) -> set[str]:
    """The ``<name>.libs`` directories a distribution's RECORD lists."""
    record = dist_info / "RECORD"
    if not record.is_file():
        return set()
    firsts = (ln.split(",", 1)[0].split("/", 1)[0]
              for ln in record.read_text(encoding="utf-8").splitlines())
    return {f for f in firsts if f.endswith(".libs") and "/" not in f and f not in (".", "..")}


# Also the lab-shared package cache's sitecustomize (gateway/package_cache.py imports it from here).
SITECUSTOMIZE = '''\
"""Generated by AiScientist — extends sys.path with the lab-shared package cache.

APPENDED, never prepended: entries are added AFTER the image's own site-packages so a cached
package can only ever fill a gap, never outrank what the container ships. Using PYTHONPATH for
this would do the opposite, because PYTHONPATH sorts ahead of site-packages.
"""
import os
import sys

for _p in os.environ.get("AISCIENTIST_PKG_CACHE", "").split(os.pathsep):
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)
'''


def _write_sitecustomize(site: Path) -> None:
    site.mkdir(parents=True, exist_ok=True)
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")


def job_env(pkg_dirs: list[str], site: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a tool job gets once packages are installed for its run: the package dirs
    for ``sitecustomize`` to append, and the ``sitecustomize`` dir first on ``PYTHONPATH``."""
    env = dict(os.environ if base is None else base)
    env["AISCIENTIST_PKG_CACHE"] = os.pathsep.join(pkg_dirs)
    rest = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join([site, *rest])
    return env


def _verify(module: str, pkg_dir: Path, site: Path) -> tuple[bool, str]:
    """Import ``module`` in a fresh interpreter set up like the tool job; it must load FROM the
    installed copy."""
    code = f"import {module}; print({module}.__file__)"
    try:
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              timeout=VERIFY_TIMEOUT_S,
                              env=job_env([str(pkg_dir)], str(site)))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, _tail(proc.stderr or proc.stdout)
    if str(pkg_dir) not in proc.stdout:
        return False, f"`{module}` imported from {proc.stdout.strip()!r}, not from the install"
    return True, ""


def install(workspace: str | Path, dependency: str) -> dict[str, Any]:
    """Install a declared dependency into ``<workspace>/_deps`` for this run and prove it imports."""
    entry = resolve(dependency)
    if entry is None:
        return {"status": "refused", "dependency": dependency,
                "error": (f"'{dependency}' is not a dependency any tool declares, so it is not "
                          "installed automatically. Declared: " + ", ".join(sorted(ALLOWED)) + ".")}
    requirement, module = entry
    root = deps_root(workspace)
    site = root / "site"
    pkg_dir = root / "pkgs" / _slug(requirement)
    scope = "installed for this run only; removed when the run is published"
    base = {"dependency": dependency, "installed": requirement, "module": module,
            "path": str(pkg_dir), "site": str(site), "scope": scope}

    if pkg_dir.is_dir():
        _write_sitecustomize(site)
        ok, why = _verify(module, pkg_dir, site)
        if ok:
            return {"status": "already_available", **base}
        shutil.rmtree(pkg_dir, ignore_errors=True)

    staging = root / f".staging-{uuid.uuid4().hex[:10]}"
    tree, tmp = staging / "tree", staging / "tmp"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        # TMPDIR on the bound workspace, not the container's /tmp: `--writable-tmpfs` gives a
        # 64 MB tmpfs and pip unpacks wheels through TMPDIR (measured: numpy + pandas overflow it).
        env = {**os.environ, "TMPDIR": str(tmp), "PIP_NO_INPUT": "1"}
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-input",
                 "--disable-pip-version-check", "--only-binary=:all:", "--target", str(tree),
                 requirement],
                capture_output=True, text=True, timeout=PIP_TIMEOUT_S, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "error", **base, "error": f"pip did not finish: {type(exc).__name__}: {exc}"}
        if proc.returncode != 0 or not tree.is_dir():
            return {"status": "error", **base,
                    "error": f"pip install {requirement} failed: {_tail(proc.stderr or proc.stdout)}"}
        pruned = _prune_image_modules(tree)
        _write_sitecustomize(site)
        pkg_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(pkg_dir, ignore_errors=True)
        os.replace(tree, pkg_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    ok, why = _verify(module, pkg_dir, site)
    if not ok:
        shutil.rmtree(pkg_dir, ignore_errors=True)
        return {"status": "error", **base,
                "error": f"{requirement} installed but `import {module}` failed: {why}"}
    size = sum(f.stat().st_size for f in pkg_dir.rglob("*") if f.is_file())
    return {"status": "ok", **base, "pruned_image_modules": pruned[:30],
            "size_mb": round(size / 1e6, 1)}
