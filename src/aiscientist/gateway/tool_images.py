"""Container images that tools declare in their ``TOOL.md``, provisioned on HPC3 on first use — so a
tool that needs R, Bioconductor, a pinned Python or any bioconda package ships a one-line
declaration instead of an install runbook. Two forms:

- ``image: "docker://quay.io/biocontainers/<package>:<version>--<build>"`` — a ready-made image,
  PULLED. A BioContainers image exists for every bioconda package, R and Bioconductor ones included.
- ``image: "bioconda:<spec> <spec> ..."`` — conda packages from conda-forge + bioconda, BUILT into an
  image. For when no published image fits: a combination, or a pin the published image lacks (the
  CellQC BioContainer ships Python 3.14, on which CellQC's nuclear-fraction step fails, so run_cellqc
  declares ``python=3.12``).

Either way the first job that needs the image provisions it on a COMPUTE node (RCIC bans downloads on
login nodes) into the shared containers directory; every later job, in any run, by any lab member,
reuses the file. Provisioning writes a per-job partial file and publishes it with ``mv -n``, so two
runs provisioning the same image at once cannot corrupt it: the first rename wins, the second is a
no-op.

A build never modifies an existing image (impossible unprivileged on HPC3: no fakeroot, overlays
read-only for readers, measured 2026-08-10). It unpacks a micromamba base image into a SANDBOX
directory the job owns, installs into that, and packs it into a new SIF — no root needed.
"""

from __future__ import annotations

import hashlib
import re
import shlex

# Pull jobs are small: a pull is network- and squashfs-bound, not compute-bound.
PULL_CPUS = 2
PULL_MEM_GB = 8
PULL_TIME_LIMIT = "02:00:00"
# A build solves and installs a conda environment (R + Bioconductor can be a few GB).
BUILD_CPUS = 8
BUILD_MEM_GB = 32
BUILD_TIME_LIMIT = "04:00:00"
# The image a ``bioconda:`` build starts from: micromamba on Debian, nothing else.
BUILD_BASE = "docker://mambaorg/micromamba:2.9.0-debian12-slim"
BUILD_CHANNELS = ("conda-forge", "bioconda")
BIOCONDA_PREFIX = "bioconda:"
# Part of a built image's name: bump it when build_command changes what goes INTO an image, so a
# run never reuses an image built the old way (1 -> 2: PATH for login shells, 2026-10-02).
BUILD_RECIPE_VERSION = 2


def is_build_ref(ref: str) -> bool:
    return ref.startswith(BIOCONDA_PREFIX)


def build_specs(ref: str) -> "list[str]":
    """``bioconda:cellqc=0.3.6 python=3.12`` -> ``["cellqc=0.3.6", "python=3.12"]``."""
    return ref[len(BIOCONDA_PREFIX):].split()


def image_sif_name(ref: str) -> str:
    """The file name an image is kept under in the shared containers directory.

    ``docker://quay.io/biocontainers/cellqc:0.3.6--pyhdfd78af_1`` -> ``cellqc_0.3.6--pyhdfd78af_1.sif``;
    a digest reference keeps the first 16 hex digits of the digest. A ``bioconda:`` build is named
    after its first package plus a hash of the whole recipe (specs, channels, base image), so a
    changed recipe is a new image and an unchanged one is found again."""
    if is_build_ref(ref):
        specs = build_specs(ref)
        recipe = "\n".join([f"v{BUILD_RECIPE_VERSION}", BUILD_BASE, *BUILD_CHANNELS, *specs])
        digest = hashlib.sha256(recipe.encode()).hexdigest()[:10]
        head = specs[0] if specs else "env"
        return re.sub(r"[^\w.\-]", "_", head.replace("=", "-")) + f"_{digest}.sif"
    body = ref.split("://", 1)[-1]
    last = body.rsplit("/", 1)[-1]
    if "@sha256:" in last:
        repo, digest = last.split("@sha256:", 1)
        stem = f"{repo}_sha256-{digest[:16]}"
    else:
        repo, _, tag = last.partition(":")
        stem = f"{repo}_{tag}" if tag else repo
    return re.sub(r"[^\w.\-]", "_", stem) + ".sif"


def provision_command(ref: str, sif: str, container_bin: str = "singularity",
                      pkgs_cache: str = "") -> str:
    """Shell that makes ``sif`` exist: a pull for a ``docker://`` image, a build for ``bioconda:``
    (reusing the lab's shared conda download cache ``pkgs_cache`` when one is given)."""
    if is_build_ref(ref):
        return build_command(build_specs(ref), sif, container_bin, pkgs_cache=pkgs_cache)
    return pull_command(ref, sif, container_bin)


def lock_path(sif: str) -> str:
    """Where a built image's lock file is kept: the exact packages the solve chose, with their URLs
    (and md5 where micromamba gives it), enough to rebuild the same environment if the image is lost
    and to name the versions in a report's Methods."""
    return f"{sif}.lock.txt"


def provision_resources(ref: str) -> "tuple[int, int, str]":
    """``(cpus, mem_gb, time_limit)`` for the job that provisions ``ref``."""
    if is_build_ref(ref):
        return BUILD_CPUS, BUILD_MEM_GB, BUILD_TIME_LIMIT
    return PULL_CPUS, PULL_MEM_GB, PULL_TIME_LIMIT


def build_command(specs: "list[str]", sif: str, container_bin: str = "singularity",
                  pkgs_cache: str = "") -> str:
    """Shell that builds ``sif`` from conda ``specs`` without root, inside a Slurm job.

    The base image is unpacked into a sandbox under the node's local scratch (the job owns every
    file, so ``exec --writable`` needs no privilege), micromamba installs the specs into its base
    environment, the package list and a lock file are written into the image for provenance, and the
    sandbox is packed into a SIF published with ``mv -n``. The sandbox stays on local scratch and is
    removed on exit, so only the finished image (and its lock file, beside it) reaches dfs3b.

    ``pkgs_cache`` is the lab's shared conda download cache. It is mounted READ-ONLY as a second
    package directory, so a package any earlier build downloaded is not fetched again, while this
    build downloads into its own local directory; afterwards only the new archives are copied in,
    each published by rsync's temp-file-and-rename, so two builds at once cannot leave a half-written
    archive for a third to read. Every image is still a fresh install: only the downloads are shared."""
    q_sif, q_partial = shlex.quote(sif), f'"{sif}.partial.$SLURM_JOB_ID"'
    q_lock = shlex.quote(lock_path(sif))
    channels = " ".join(f"-c {c}" for c in BUILD_CHANNELS)
    pkgs_dirs = "/aisci-pkgs,/aisci-shared-pkgs" if pkgs_cache else "/aisci-pkgs"
    install = (
        f"set -euo pipefail; export CONDA_PKGS_DIRS={pkgs_dirs} MAMBA_ROOT_PREFIX=/opt/conda; "
        f"micromamba install -y -n base {channels} {' '.join(shlex.quote(s) for s in specs)}; "
        "micromamba list -n base > /opt/conda/aiscientist-packages.txt; "
        "{ micromamba env export -n base --explicit --md5 2>/dev/null "
        "|| micromamba env export -n base --explicit; } > /opt/conda/aiscientist-lock.txt")
    shared_bind = ""
    lines = [
        'W="${TMPDIR:-/tmp}/aisci-build-$SLURM_JOB_ID"',
        'mkdir -p "$W/tmp" "$W/pkgs" "$W/cache"',
        'trap \'rm -rf "$W"\' EXIT',
        'export SINGULARITY_CACHEDIR="$W/cache" APPTAINER_CACHEDIR="$W/cache"',
        'export SINGULARITY_TMPDIR="$W/tmp" APPTAINER_TMPDIR="$W/tmp"',
    ]
    if pkgs_cache:
        q_cache = shlex.quote(pkgs_cache.rstrip("/"))
        lines.append(f"mkdir -p {q_cache}")
        shared_bind = f"-B {q_cache}:/aisci-shared-pkgs:ro "
    lines += [
        f"if [ ! -s {q_sif} ]; then",
        f'  {container_bin} build --sandbox "$W/box" {shlex.quote(BUILD_BASE)}',
        # HPC3's singularity.conf binds site paths (/data, ...) into every container, and a writable
        # sandbox cannot create their mount points ("destination /data doesn't exist in container").
        # The install needs none of them; the mount points are created for later runs of the image.
        '  mkdir -p "$W/box/aisci-pkgs" "$W/box/aisci-shared-pkgs" "$W/box/data" "$W/box/dfs3b" '
        '"$W/box/pub"',
        f'  {container_bin} exec --writable --containall --no-mount bind-paths '
        f'-B "$W/pkgs:/aisci-pkgs" {shared_bind}"$W/box" /bin/bash -c {shlex.quote(install)}',
    ]
    if pkgs_cache:
        # Share what this build downloaded: the archives only (never the unpacked trees), in the
        # layout micromamba 2 keeps them (pkgs/https/<host>/<channel>/<subdir>/<pkg>.conda), never
        # overwriting one that is there already. Checked on HPC3 (2026-10-05): a second build reading
        # such an archives-only, read-only cache reports its packages as "Cached" and downloads none.
        # (micromamba logs that it cannot lock the read-only cache; that is harmless.)
        lines.append(f"  rsync -a --ignore-existing --include='*/' --include='*.conda' "
                     f"--include='*.tar.bz2' --exclude='*' --prune-empty-dirs "
                     f"\"$W/pkgs/\" {q_cache}/ || true")
    lines += [
        f'  cp "$W/box/opt/conda/aiscientist-lock.txt" {q_lock} 2>/dev/null || true',
        # `exec` skips the image's entrypoint, which is what puts the env on PATH in Docker. The
        # jobs also run `bash -lc`, and Debian's /etc/profile resets PATH for a login shell, so the
        # same line goes into /etc/profile.d as well (sourced after that reset).
        '  mkdir -p "$W/box/.singularity.d/env" "$W/box/etc/profile.d"',
        "  printf 'export PATH=/opt/conda/bin:$PATH\\n' > \"$W/box/.singularity.d/env/90-aiscientist-conda.sh\"",
        "  printf 'export PATH=/opt/conda/bin:$PATH\\n' > \"$W/box/etc/profile.d/90-aiscientist-conda.sh\"",
        f'  {container_bin} build {q_partial} "$W/box"',
        f"  mv -n {q_partial} {q_sif}",
        "fi",
        f"rm -f {q_partial}",
        f"test -s {q_sif}",
        f'echo "image ready: {sif}"',
    ]
    return "\n".join(lines)


def pull_command(ref: str, sif: str, container_bin: str = "singularity") -> str:
    """Shell that pulls ``ref`` to ``sif`` atomically, inside a Slurm job. The layer cache goes to
    the node's local scratch and is removed afterwards, so nothing but the finished image lands on
    the shared filesystem."""
    # The partial name carries $SLURM_JOB_ID, so it is double-quoted for the shell to expand.
    q_sif, q_partial = shlex.quote(sif), f'"{sif}.partial.$SLURM_JOB_ID"'
    return "\n".join([
        'export SINGULARITY_CACHEDIR="${TMPDIR:-/tmp}/sing-cache-$SLURM_JOB_ID"',
        'export APPTAINER_CACHEDIR="$SINGULARITY_CACHEDIR"',
        'export SINGULARITY_TMPDIR="${TMPDIR:-/tmp}"',
        'mkdir -p "$SINGULARITY_CACHEDIR"',
        f"if [ ! -s {q_sif} ]; then",
        f"  {container_bin} pull --name {q_partial} {shlex.quote(ref)}",
        f"  mv -n {q_partial} {q_sif}",
        "fi",
        f"rm -f {q_partial}",
        'rm -rf "$SINGULARITY_CACHEDIR"',
        f"test -s {q_sif}",
        f'echo "image ready: {sif}"',
    ])
