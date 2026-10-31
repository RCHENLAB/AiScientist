"""Lab environments: software the agent asks for when neither its image nor HPC3 has it.

docs/environments/README.md, steps 3 to 5. When a step needs a program or package that run_code's
image lacks and no HPC3 module provides, the agent writes a **recipe** with ``request_environment``:
pinned conda packages (conda-forge and bioconda, which carry most bioinformatics tools and R and
Bioconductor packages), pip packages (PyPI, or a GitHub repository at a tag or commit), R packages
(CRAN at a version, Bioconductor, or GitHub at a tag or commit), and an optional build script for
software released only as source. A container image (``docker://`` at a pinned tag) can be asked for
instead.

The user approves the recipe on the console's card (the website, never HPC3). Then one Slurm job on
a compute node builds it, which is how RCIC asks users to install conda and R software: in a job,
never on a login node and never in ``$HOME``. The job:

* runs every install command inside the micromamba image (pulled, never built from a definition
  file), with only the new environment and the job's own scratch writable, and the network on;
* builds the environment AT ITS FINAL PATH (the staging folder is bound there), because a conda
  prefix records its own path in scripts and libraries;
* writes a lock file (every conda package with its URL and md5), ``pip freeze`` and the R packages
  with their versions and GitHub commits;
* checks what the recipe says it ``provides`` with the network off and the environment read-only,
  exactly as a run will see it;
* publishes it with one ``mv -T``, so a half-built environment never appears and two builds of the
  same recipe at once leave one copy.

The folder name ends in a hash of the recipe, so the same recipe is found again and never built twice:
a later run, by any lab member, uses it with no card. ``run_shell``, ``run_code`` and
``run_in_environment`` take ``environment=<id>``: the folder is bound read-only and its ``bin`` put
first on ``PATH``.

This module builds commands and reads records; the gateway supplies the Slurm job runner and the
image provisioning (``gateway/app.py``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

# Bump when build_script changes what goes INTO an environment, so a run never reuses one built the
# old way.
RECIPE_VERSION = 1
# The image every install command runs in: micromamba on Debian, nothing else. Pulled into the shared
# containers folder on first use. The same image the tool-image builds start from
# (gateway/tool_images.BUILD_BASE).
BASE_IMAGE = "docker://mambaorg/micromamba:2.9.0-debian12-slim"
CHANNELS = ("conda-forge", "bioconda")
CRAN_URL = "https://cloud.r-project.org"
# Where a pip or R package may come from as source, and a container image from. A recipe naming
# another host is refused before anyone is asked.
SOURCE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")
IMAGE_REGISTRIES = ("quay.io", "ghcr.io", "docker.io")
# A git ref that moves: refused, because the same recipe would then build different code.
MOVING_REFS = frozenset({"main", "master", "develop", "dev", "devel", "trunk", "head", "latest"})

MAX_ITEMS = 40
MAX_PROVIDES = 20
MAX_VERIFY = 5
MAX_SCRIPT_CHARS = 20_000
MAX_REASON_CHARS = 600

BUILD_CPUS, BUILD_MEM_GB, BUILD_TIME_LIMIT = 8, 32, "04:00:00"
PULL_CPUS, PULL_MEM_GB, PULL_TIME_LIMIT = 2, 8, "02:00:00"

META = ".aiscientist"          # inside a published environment: record, lock, recipe, build log
STAGING = ".staging"           # inside the store: builds in progress (same filesystem as the store)
# The store: setgid (everything stays in the lab's group), group-writable (any member can build),
# sticky (only an environment's owner can remove it).
STORE_MODE = "3775"

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}-[0-9a-f]{12}$")
_CONDA = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.+-]*)(?P<op>==?)(?P<ver>[A-Za-z0-9][A-Za-z0-9_.*+!-]*)"
                    r"(?:=(?P<build>[A-Za-z0-9_.*+-]+))?$")
_PIP_PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9][A-Za-z0-9._*+!-]*$")
_PIP_GIT = re.compile(r"^(?:(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*@\s*)?git\+https://(?P<host>[a-z0-9.-]+)/"
                      r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?@(?P<ref>[A-Za-z0-9_.\-/]+)"
                      r"(?:#subdirectory=(?P<sub>[A-Za-z0-9_./-]+))?$")
_R_CRAN = re.compile(r"^(?:cran:)?(?P<name>[A-Za-z][A-Za-z0-9.]*)==(?P<ver>[0-9][0-9.\-]*)$")
_R_BIOC = re.compile(r"^bioc:(?P<name>[A-Za-z][A-Za-z0-9.]*)$")
_R_GITHUB = re.compile(r"^github:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)(?:/(?P<sub>[A-Za-z0-9_./-]+))?"
                       r"@(?P<ref>[A-Za-z0-9_.\-/]+)$")
_IMAGE = re.compile(r"^docker://(?:(?P<reg>[a-z0-9.-]+\.[a-z]{2,}(?::\d+)?)/)?(?P<repo>[a-z0-9._-]+(?:/[a-z0-9._-]+)*)"
                    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})|@sha256:(?P<digest>[0-9a-f]{64}))$")
_PROVIDES = re.compile(r"^(?:(?P<kind>cmd|R|py):)?(?P<name>[A-Za-z0-9_][A-Za-z0-9_.+-]*)$")


class RecipeError(ValueError):
    """A recipe the model must correct: the message says what is wrong and how to write it."""


# --- the recipe ----------------------------------------------------------------------------------


def _list(value: Any, field_name: str, limit: int = MAX_ITEMS) -> tuple[str, ...]:
    if value in (None, "", []):
        return ()
    if isinstance(value, str):
        value = [v for v in re.split(r"[\n,]+", value)]
    if not isinstance(value, (list, tuple)):
        raise RecipeError(f"`{field_name}` must be a list of strings.")
    items = tuple(str(v).strip() for v in value if str(v).strip())
    if len(items) > limit:
        raise RecipeError(f"`{field_name}` has {len(items)} entries; at most {limit} are allowed.")
    return items


def _git_ref(ref: str, what: str) -> str:
    if ref.lower() in MOVING_REFS:
        raise RecipeError(
            f"{what} names the branch '{ref}', which moves. Pin a release tag or a commit (e.g. @v1.2.0 or "
            "@3f2a9c1), so the same recipe always builds the same code.")
    return ref


@dataclass(frozen=True)
class Recipe:
    """What the agent asked for, validated. ``conda`` holds the specs as given; the build adds the
    tools the other parts need (:meth:`conda_specs`)."""

    name: str
    conda: tuple[str, ...] = ()
    pip: tuple[str, ...] = ()
    r: tuple[str, ...] = ()
    script: str = ""
    image: str = ""
    provides: tuple[str, ...] = ()
    verify: tuple[str, ...] = ()
    reason: str = ""

    @property
    def kind(self) -> str:
        return "image" if self.image else "conda"

    @classmethod
    def from_args(cls, args: dict[str, Any]) -> "Recipe":
        name = re.sub(r"[^a-z0-9._-]+", "-", str(args.get("name") or "").strip().lower()).strip("-._")[:40]
        if not name or not _NAME.match(name):
            raise RecipeError("Give the environment a short `name`, e.g. 'mosdepth' or 'deseq2-r44'.")
        conda = _list(args.get("conda"), "conda")
        pip = _list(args.get("pip"), "pip")
        r = _list(args.get("r"), "r")
        script = str(args.get("script") or "").strip()
        image = str(args.get("image") or "").strip()
        provides = _list(args.get("provides"), "provides", MAX_PROVIDES)
        raw_verify = args.get("verify")
        if isinstance(raw_verify, str):       # commands may contain commas: one per line
            raw_verify = raw_verify.splitlines()
        verify = _list(raw_verify, "verify", MAX_VERIFY)
        reason = " ".join(str(args.get("reason") or "").split())
        if not reason:
            raise RecipeError("Say in `reason` why the step needs this software (it is shown to the user).")
        if len(reason) > MAX_REASON_CHARS:
            raise RecipeError(f"`reason` is too long ({len(reason)} characters; at most {MAX_REASON_CHARS}).")
        if not provides:
            raise RecipeError(
                "List in `provides` what the environment must contain, checked after the build: a command "
                "('mosdepth'), an R package ('R:DESeq2') or a Python module ('py:pysam').")
        for p in provides:
            if not _PROVIDES.match(p):
                raise RecipeError(f"`provides` entry {p!r} is not a command name, 'R:<package>' or 'py:<module>'.")
        for v in verify:
            if len(v) > 300 or "\x00" in v:
                raise RecipeError("Each `verify` command must be one line of at most 300 characters.")
        if image:
            if conda or pip or r or script:
                raise RecipeError("Ask for either a container `image` or packages (`conda`, `pip`, `r`, "
                                        "`script`), not both.")
            m = _IMAGE.match(image)
            if not m:
                raise RecipeError(
                    f"`image` {image!r} is not a pinned image reference: write docker://<registry>/<repository>:<tag>, "
                    "e.g. docker://quay.io/biocontainers/mosdepth:0.3.10--h4e814b3_1.")
            reg = (m.group("reg") or "docker.io").split(":")[0]
            if reg == "registry-1.docker.io":
                reg = "docker.io"
            if reg not in IMAGE_REGISTRIES:
                raise RecipeError(f"Images can be pulled from {', '.join(IMAGE_REGISTRIES)} only, not {reg}.")
            if m.group("tag") and m.group("tag").lower() == "latest":
                raise RecipeError("`latest` moves: name the image's version tag (or its sha256 digest).")
        else:
            if not (conda or pip or r or script):
                raise RecipeError("The recipe is empty: list `conda`, `pip` or `r` packages, a build "
                                        "`script`, or a container `image`.")
            for spec in conda:
                if not _CONDA.match(spec):
                    raise RecipeError(
                        f"conda package {spec!r} is not pinned: write name=version (e.g. 'mosdepth=0.3.10', "
                        "'r-base=4.4', 'bioconductor-deseq2=1.46.0'). Channels are conda-forge and bioconda.")
            for req in pip:
                g = _PIP_GIT.match(req)
                if g:
                    if g.group("host") not in SOURCE_HOSTS:
                        raise RecipeError(f"pip source {req!r}: repositories on {', '.join(SOURCE_HOSTS)} only.")
                    _git_ref(g.group("ref"), f"pip source {req!r}")
                elif not _PIP_PIN.match(req):
                    raise RecipeError(
                        f"pip package {req!r} is not pinned: write name==version (PyPI), or "
                        "git+https://github.com/<owner>/<repo>@<tag or commit> for a package only on GitHub.")
            for pkg in r:
                g = _R_GITHUB.match(pkg)
                if g:
                    _git_ref(g.group("ref"), f"R package {pkg!r}")
                elif not (_R_CRAN.match(pkg) or _R_BIOC.match(pkg)):
                    raise RecipeError(
                        f"R package {pkg!r}: write cran:<name>==<version>, bioc:<name>, or "
                        "github:<owner>/<repo>@<tag or commit>. A package on conda (r-* / bioconductor-*) "
                        "is better listed under `conda`.")
            if r and not any(_spec_name(s) == "r-base" for s in conda):
                raise RecipeError(
                    "R packages need a pinned R: add 'r-base=<version>' (e.g. 'r-base=4.4') to `conda`. It also "
                    "fixes the Bioconductor release.")
            if len(script) > MAX_SCRIPT_CHARS or "\x00" in script:
                raise RecipeError(f"`script` is too long (at most {MAX_SCRIPT_CHARS} characters).")
        return cls(name=name, conda=conda, pip=pip, r=r, script=script, image=image,
                   provides=provides, verify=verify, reason=reason)

    def conda_specs(self) -> tuple[str, ...]:
        """The conda specs the build installs: the recipe's, plus the tools its other parts need
        (Python and pip for ``pip``, compilers and make for R packages and a build script, git for a
        GitHub source). Those are left unpinned and recorded in the lock file."""
        have = {_spec_name(s) for s in self.conda}
        extra: list[str] = []

        def need(*names: str) -> None:
            for n in names:
                if n not in have and n not in extra:
                    extra.append(n)

        uses_git = bool(self.script) or any(_PIP_GIT.match(p) for p in self.pip) \
            or any(_R_GITHUB.match(p) for p in self.r)
        if self.pip:
            need("python", "pip")
        if self.r:
            need("r-remotes", "compilers", "make")
            if any(_R_BIOC.match(p) for p in self.r):
                need("r-biocmanager")
        if self.script:
            need("compilers", "make", "cmake", "pkg-config")
        if uses_git:
            need("git")
        return self.conda + tuple(extra)

    def canonical(self) -> dict[str, Any]:
        """What the environment's content depends on, and nothing else: its hash names the folder."""
        if self.image:
            return {"v": RECIPE_VERSION, "image": self.image}
        return {"v": RECIPE_VERSION, "base": BASE_IMAGE, "channels": list(CHANNELS),
                "conda": sorted(self.conda_specs()), "pip": list(self.pip), "r": list(self.r),
                "script": self.script}

    @property
    def env_id(self) -> str:
        digest = hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()[:12]
        return f"{self.name}-{digest}"

    def card_detail(self, *, max_gb: int) -> str:
        """The approval card's body: everything that will be installed, and what happens to it."""
        lines = [f"Why: {self.reason}", ""]
        if self.image:
            lines.append(f"Container image: {self.image}")
        if self.conda:
            lines.append("conda (conda-forge, bioconda): " + ", ".join(self.conda))
        added = [s for s in self.conda_specs() if s not in self.conda]
        if added:
            lines.append("  build tools added: " + ", ".join(added))
        if self.pip:
            lines.append("pip: " + ", ".join(self.pip))
        if self.r:
            lines.append("R packages: " + ", ".join(self.r))
        if self.script:
            lines += ["", "Build script (runs in the build sandbox, $PREFIX = the environment):", self.script, ""]
        lines.append("Checked after the build: " + ", ".join(self.provides + self.verify))
        lines += ["", "Built once in a Slurm job on an HPC3 compute node (up to 4 hours), inside a container that can "
                  f"write only the new environment (at most {max_gb} GB). Then it is shared read-only with the lab: "
                  "later runs use it without asking."]
        return "\n".join(lines)


def _spec_name(spec: str) -> str:
    m = _CONDA.match(spec)
    return (m.group("name") if m else re.split(r"[=<>!~ ]", spec, maxsplit=1)[0]).lower()


# --- what a build writes ------------------------------------------------------------------------


def r_install_script(recipe: Recipe, cpus: int = BUILD_CPUS) -> str:
    """The R code that installs ``recipe.r`` into the environment's own R library, failing on any
    package that did not install."""
    q = json.dumps
    lines = [
        f'options(repos = c(CRAN = {q(CRAN_URL)}), Ncpus = {int(cpus)}, warn = 1)',
        'lib <- .libPaths()[1]',
        'want <- character(0)',
    ]
    for pkg in recipe.r:
        g = _R_GITHUB.match(pkg)
        if g:
            # The archive URL, not the GitHub API: the API's 60 requests an hour are shared by every
            # job leaving the cluster through the same address.
            url = f"https://github.com/{g.group('owner')}/{g.group('repo')}/archive/{g.group('ref')}.tar.gz"
            sub = f", subdir = {q(g.group('sub'))}" if g.group("sub") else ""
            lines.append(f'remotes::install_url({q(url)}{sub}, upgrade = "never", dependencies = NA, lib = lib)')
            continue
        c = _R_CRAN.match(pkg)
        if c:
            lines.append(f'remotes::install_version({q(c.group("name"))}, version = {q(c.group("ver"))}, '
                         'upgrade = "never", dependencies = NA, lib = lib)')
            lines.append(f'want <- c(want, {q(c.group("name"))})')
            continue
        b = _R_BIOC.match(pkg)
        if b:
            lines.append(f'BiocManager::install({q(b.group("name"))}, update = FALSE, ask = FALSE, lib = lib)')
            lines.append(f'want <- c(want, {q(b.group("name"))})')
    lines.append('missing <- want[!vapply(want, requireNamespace, logical(1), quietly = TRUE)]')
    lines.append('if (length(missing)) stop("not installed: ", paste(missing, collapse = ", "))')
    return "\n".join(lines) + "\n"


def activation(prefixes: "list[str] | tuple[str, ...]") -> str:
    """Bash that puts environments first on ``PATH`` (the first prefix wins) and runs their conda
    activation scripts (some packages set variables there, e.g. ``JAVA_HOME``). Never fails."""
    if not prefixes:
        return ""
    q = shlex.quote
    parts = [f'export PATH={":".join(q(p) + "/bin" for p in prefixes)}:"$PATH"',
             f"export AISCIENTIST_ENVS={q(':'.join(prefixes))}", f"export CONDA_PREFIX={q(prefixes[0])}"]
    for p in reversed(prefixes):
        # `|| true` also switches `set -e` off inside the sourced script.
        parts.append(f'for _f in {q(p)}/etc/conda/activate.d/*.sh; do if [ -r "$_f" ]; then . "$_f" '
                     '>/dev/null 2>&1 || true; fi; done')
    return "; ".join(parts) + "; "


def check_lines(provides: "tuple[str, ...]", verify: "tuple[str, ...]" = ()) -> list[str]:
    """Bash that checks each ``provides`` entry and ``verify`` command, printing one
    ``AISCIENTIST_ENV_CHECK ok|missing <what>`` line each, and the tail of a failed check's output."""
    q = shlex.quote
    lines = [
        "fail=0",
        'check() { out=$(eval "$2" 2>&1); rc=$?; if [ $rc -eq 0 ]; then echo "AISCIENTIST_ENV_CHECK ok $1"; '
        'else echo "AISCIENTIST_ENV_CHECK missing $1"; printf %s "$out" | tail -n 8 | sed "s/^/    /"; fail=1; fi; }',
    ]
    for p in provides:
        m = _PROVIDES.match(p)
        if not m:
            continue
        kind, name = m.group("kind") or "cmd", m.group("name")
        if kind == "R":
            cmd = f"Rscript -e {q(f'suppressPackageStartupMessages(library({name}))')}"
        elif kind == "py":
            cmd = f"python -c {q(f'import {name}')}"
        else:
            cmd = f"command -v {q(name)}"
        lines.append(f"check {q(p)} {q(cmd)}")
    for v in verify:
        lines.append(f"check {q('verify: ' + v)} {q(v)}")
    lines.append("exit $fail")
    return lines


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def build_script(recipe: Recipe, *, store: str, recipe_dir: str, base_sif: str, pkgs_cache: str = "",
                 container_bin: str = "apptainer", max_gb: int = 10, cpus: int = BUILD_CPUS) -> str:
    """The body of the Slurm job that builds and publishes ``recipe`` (conda kind).

    Every install command runs in the micromamba image with ``--containall --no-mount bind-paths``:
    writable are only the staging folder (bound at the environment's final path), the job's scratch on
    the node's local disk, and the download cache; the recipe files and the lab's shared conda download
    cache are read-only. Nothing else of lab storage is visible."""
    q = shlex.quote
    final = env_path(store, recipe.env_id)
    stage = f'{store.rstrip("/")}/{STAGING}/{recipe.env_id}.$SLURM_JOB_ID'
    setup = ("set -euo pipefail; export MAMBA_ROOT_PREFIX=/tmp/mamba-root "
             f"CONDA_PKGS_DIRS=/aisci-pkgs{',/aisci-shared-pkgs' if pkgs_cache else ''} "
             "HOME=/tmp/home PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "
             f"CPU_COUNT={int(cpus)} MAKEFLAGS=-j{int(cpus)}; mkdir -p /tmp/home; ")
    # The environment's activation, as runs get it (PATH, and the conda activation scripts that set CC,
    # CXX and the like; ``micromamba run`` does not set them, measured on HPC3, job 58636254). The
    # activation scripts are written for an interactive shell, so they run without ``set -u``.
    act = "set +u; " + activation([final]) + "set -u; "
    # conda-forge's compilers 2.0 install plain gcc/g++/gfortran and no activation script, so CC and the
    # like stay unset (measured on HPC3, job 58636360): set them for builds, pointing at the environment.
    toolchain = (f'export PREFIX={q(final)} CC="${{CC:-gcc}}" CXX="${{CXX:-g++}}" FC="${{FC:-gfortran}}" '
                 f'CPPFLAGS="${{CPPFLAGS:--I{final}/include}}" '
                 f'LDFLAGS="${{LDFLAGS:--L{final}/lib -Wl,-rpath,{final}/lib}}" '
                 f'PKG_CONFIG_PATH="{final}/lib/pkgconfig"; ')
    lines = [
        f"FINAL={q(final)}",
        f'STAGE="{stage}"',
        f"RECIPE={q(recipe_dir)}",
        f"MM={q(base_sif)}",
        'W="${TMPDIR:-/tmp}/aisci-env-$SLURM_JOB_ID"',
        "umask 0022",
        'cleanup() { rm -rf "$W"; if [ -d "$STAGE" ]; then chmod -R u+w "$STAGE" 2>/dev/null; rm -rf "$STAGE"; fi; }',
        "trap cleanup EXIT",
        'phase() { echo "AISCIENTIST_ENV_PHASE $1"; }',
        'fail() { echo "AISCIENTIST_ENV_FAILED $1"; exit 3; }',
        f'if [ -f "$FINAL/{META}/record.json" ]; then echo "AISCIENTIST_ENV_READY $FINAL"; exit 0; fi',
        'mkdir -p "$W/pkgs" "$W/tmp" "$W/src" "$W/cache" "$STAGE"',
        'export APPTAINER_CACHEDIR="$W/cache" APPTAINER_TMPDIR="$W/tmp"',
        'B=(-B "$W/tmp:/tmp" -B "$W/pkgs:/aisci-pkgs" -B "$RECIPE:/aisci-recipe:ro" -B "$W/src:/aisci-src")',
    ]
    if pkgs_cache:
        lines.append(f'if [ -d {q(pkgs_cache)} ]; then B+=(-B {q(pkgs_cache.rstrip("/") + ":/aisci-shared-pkgs:ro")}); fi')
    lines += [
        f'box() {{ {container_bin} exec --containall --no-mount bind-paths --pwd /aisci-src "${{B[@]}}" '
        f'-B "$STAGE:$FINAL" "$MM" /bin/bash -c "$1"; }}',
        "phase solve",
        f"box {q(setup + f'micromamba create -y -p {q(final)} --override-channels ' + ' '.join(f'-c {c}' for c in CHANNELS) + ' --file /aisci-recipe/conda.txt')} || fail solve",
    ]
    if recipe.pip:
        lines += ["phase pip",
                  f"box {q(setup + act + 'python -m pip install --no-input -r /aisci-recipe/requirements.txt')} || fail pip"]
    if recipe.r:
        lines += ["phase r",
                  f"box {q(setup + act + toolchain + 'Rscript /aisci-recipe/install.R')} || fail r"]
    if recipe.script:
        lines += ["phase script",
                  f"box {q(setup + act + toolchain + 'cd /aisci-src && bash -eo pipefail /aisci-recipe/build.sh')} || fail script"]
    record_cmds = [f"mkdir -p {q(final)}/{META}",
                   f"{{ micromamba env export -p {q(final)} --explicit --md5 2>/dev/null || "
                   f"micromamba env export -p {q(final)} --explicit; }} > {q(final)}/{META}/lock.txt"]
    if recipe.pip:
        record_cmds.append(f"python -m pip freeze > {q(final)}/{META}/pip-freeze.txt")
    if recipe.r:
        r_list = ('ip <- installed.packages(fields = "RemoteSha"); '
                  'write.table(ip[, c("Package", "Version", "RemoteSha")], sep = "\\t", quote = FALSE, row.names = FALSE)')
        record_cmds.append(f"Rscript -e {q(r_list)} > {q(final)}/{META}/r-packages.tsv")
    lines += ["phase lock", f"box {q(setup + act + '; '.join(record_cmds))} || fail lock"]
    if pkgs_cache:
        # Share what this build downloaded: archives only, never one that is there already (the same
        # rule as the image builds, gateway/tool_images.build_command).
        lines.append(f"if [ -d {q(pkgs_cache)} ] && [ -w {q(pkgs_cache)} ]; then rsync -a --ignore-existing "
                     "--include='*/' --include='*.conda' --include='*.tar.bz2' --exclude='*' --prune-empty-dirs "
                     f'"$W/pkgs/" {q(pkgs_cache.rstrip("/"))}/ 2>/dev/null || true; fi')
    verify = activation([final]) + "\n".join(check_lines(recipe.provides, recipe.verify))
    lines += [
        "phase verify",
        # As a run sees it: read-only, network off, PATH from the activation runs use.
        f'{container_bin} exec --containall --no-mount bind-paths --net --network none -B "$W/tmp:/tmp" '
        f'-B "$STAGE:$FINAL:ro" "$MM" /bin/bash -c {q(verify)} || fail verify',
        *_finish_lines(max_gb),
    ]
    return "\n".join(lines) + "\n"


def image_script(recipe: Recipe, *, store: str, recipe_dir: str, sif: str, container_bin: str = "apptainer") -> str:
    """The body of the Slurm job that pulls ``recipe.image`` into the shared containers folder (unless it
    is there), checks ``provides`` inside it with the network off, and publishes the record."""
    q = shlex.quote
    final = env_path(store, recipe.env_id)
    checks = "\n".join(check_lines(recipe.provides, recipe.verify))
    return "\n".join([
        f"FINAL={q(final)}",
        f'STAGE="{store.rstrip("/")}/{STAGING}/{recipe.env_id}.$SLURM_JOB_ID"',
        f"RECIPE={q(recipe_dir)}",
        f"SIF={q(sif)}",
        'W="${TMPDIR:-/tmp}/aisci-env-$SLURM_JOB_ID"',
        "umask 0022",
        'cleanup() { rm -rf "$W"; [ -d "$STAGE" ] && rm -rf "$STAGE"; true; }',
        "trap cleanup EXIT",
        'phase() { echo "AISCIENTIST_ENV_PHASE $1"; }',
        'fail() { echo "AISCIENTIST_ENV_FAILED $1"; exit 3; }',
        f'if [ -f "$FINAL/{META}/record.json" ]; then echo "AISCIENTIST_ENV_READY $FINAL"; exit 0; fi',
        'mkdir -p "$W/cache" "$W/tmp" "$STAGE"',
        'export APPTAINER_CACHEDIR="$W/cache" APPTAINER_TMPDIR="$W/tmp"',
        "phase pull",
        'if [ ! -s "$SIF" ]; then',
        f'  {container_bin} pull --name "$SIF.partial.$SLURM_JOB_ID" {q(recipe.image)} || fail pull',
        '  mv -n "$SIF.partial.$SLURM_JOB_ID" "$SIF"; rm -f "$SIF.partial.$SLURM_JOB_ID"',
        "fi",
        '[ -s "$SIF" ] || fail pull',
        "phase verify",
        f'{container_bin} exec --containall --no-mount bind-paths --net --network none "$SIF" /bin/sh -c {q(checks)} '
        "|| fail verify",
        f'mkdir -p "$STAGE/{META}"',
        *_finish_lines(0, size='stat -c %s "$SIF"'),
    ]) + "\n"


def _finish_lines(max_gb: int, size: str = 'du -sb "$STAGE" | cut -f1') -> list[str]:
    """Size check, record, permissions and the one ``mv -T`` that publishes the environment."""
    lines = ["phase publish", f"SIZE=$({size})", 'FILES=$(find "$STAGE" | wc -l)']
    if max_gb:
        lines.append(f'[ "$SIZE" -le {int(max_gb) * 1024 ** 3} ] || fail "size $SIZE bytes is over {int(max_gb)} GB"')
    lines += [
        f'mkdir -p "$STAGE/{META}"',
        f'cp "$RECIPE"/* "$STAGE/{META}/" 2>/dev/null || true',
        f'sed -e "s/__SIZE__/$SIZE/" -e "s/__FILES__/$FILES/" -e "s/__JOB__/$SLURM_JOB_ID/" '
        f'-e "s/__BUILT_AT__/$(date -u +%Y-%m-%dT%H:%M:%SZ)/" -e "s/__NODE__/$(hostname -s)/" '
        f'"$RECIPE/record.json" > "$STAGE/{META}/record.json"',
        'chmod -R a+rX,go-w "$STAGE" 2>/dev/null || true',
        'mkdir -p "$(dirname "$FINAL")"',
        'if mv -T "$STAGE" "$FINAL" 2>/dev/null; then echo "AISCIENTIST_ENV_PUBLISHED $FINAL"',
        f'elif [ -f "$FINAL/{META}/record.json" ]; then echo "AISCIENTIST_ENV_READY $FINAL"',
        "else fail publish; fi",
    ]
    return lines


def recipe_files(recipe: Recipe, record: dict[str, Any], cpus: int = BUILD_CPUS) -> dict[str, str]:
    """``{file name: text}`` staged next to the build job: what it installs, and the record template
    the job completes (``__SIZE__`` and the like)."""
    files = {"record.json": json.dumps(record, indent=2, sort_keys=True) + "\n"}
    if recipe.image:
        files["image.txt"] = recipe.image + "\n"
        return files
    files["conda.txt"] = "\n".join(recipe.conda_specs()) + "\n"
    if recipe.pip:
        files["requirements.txt"] = "\n".join(recipe.pip) + "\n"
    if recipe.r:
        files["install.R"] = r_install_script(recipe, cpus)
    if recipe.script:
        files["build.sh"] = recipe.script.rstrip("\n") + "\n"
    return files


# --- the store ----------------------------------------------------------------------------------


def env_path(store: str, env_id: str) -> str:
    return f"{store.rstrip('/')}/{env_id}"


@dataclass(frozen=True)
class EnvRef:
    """A published environment a call binds: ``kind`` ``conda`` (a prefix bound read-only) or
    ``image`` (a container the command runs in)."""

    id: str
    path: str
    kind: str = "conda"
    image: str = ""
    provides: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out = {"id": self.id, "kind": self.kind, "path": self.path}
        if self.image:
            out["image"] = self.image
        return out


# The environments the current tool call binds (run_code, run_in_environment), read by the Slurm
# executor that builds the job. A context variable, like the skill policy: the executor chain between
# the tool and the job (preflight, skill policy) passes only the code.
ACTIVE: ContextVar[tuple[EnvRef, ...]] = ContextVar("aiscientist_lab_environments", default=())


@contextmanager
def using(refs: "tuple[EnvRef, ...] | list[EnvRef]") -> Iterator[None]:
    token = ACTIVE.set(tuple(refs))
    try:
        yield
    finally:
        ACTIVE.reset(token)


def _matches(record: dict[str, Any], query: str) -> bool:
    ql = query.lower().strip()
    if not ql:
        return False
    names = [record.get("name", "")] + list(record.get("provides") or [])
    for p in names:
        p = str(p).lower()
        if p == ql or p.split(":", 1)[-1] == ql:
            return True
    recipe = record.get("recipe") or {}
    for spec in list(recipe.get("conda") or []) + list(recipe.get("pip") or []) + list(recipe.get("r") or []):
        s = str(spec).lower()
        base = re.split(r"[=<>@ ]", s.split(":", 1)[-1], maxsplit=1)[0]
        if base in (ql, f"r-{ql}", f"bioconductor-{ql}") or base.rsplit("/", 1)[-1] == ql:
            return True
    return False


@dataclass
class LabEnvironments:
    """The lab's environment store for one run: find, build and resolve environments.

    ``run_job(script, job_name, cpus, mem_gb, time_limit)`` submits ``script`` as a Slurm batch job and
    waits for it, returning ``{"state", "job_id", "log"}``; ``base_image()`` is the path of the
    micromamba image (pulled on first use); ``image_sif(ref)`` where an image ``ref`` is kept in the
    shared containers folder. ``remote`` answers metadata only (login node)."""

    remote: Any
    store: str
    run_job: Callable[[str, str, int, int, str], dict[str, Any]]
    base_image: Callable[[], str]
    image_sif: Callable[[str], str]
    stage_dir: str
    pkgs_cache: str = ""
    container_bin: str = "apptainer"
    max_gb: int = 10
    approval: str = "ask"              # ask: a card per new environment; auto: build without asking
    requested_by: str = ""             # the HPC3 user
    account: str = ""                  # the console account
    run_id: str = ""
    session_id: str = ""
    emit: Callable[..., None] | None = None
    records_ttl_s: float = 120.0
    _records: "list[dict[str, Any]] | None" = field(default=None, repr=False)
    _records_at: float = field(default=0.0, repr=False)
    _counter: int = field(default=0, repr=False)

    def _say(self, level: str, message: str) -> None:
        if self.emit:
            self.emit(level, "environments", message)

    # -- reading ------------------------------------------------------------------------------

    def record(self, env_id: str) -> "dict[str, Any] | None":
        if not _ID.match(env_id or ""):
            return None
        path = f"{env_path(self.store, env_id)}/{META}/record.json"
        res = self.remote.exec(f"cat {shlex.quote(path)} 2>/dev/null", timeout=30)
        text = (getattr(res, "stdout", "") or "").strip()
        if not getattr(res, "ok", getattr(res, "exit_status", 1) == 0) or not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def records(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Every published environment's record (login-node reads of small JSON files), kept two minutes."""
        if not refresh and self._records is not None and time.monotonic() - self._records_at < self.records_ttl_s:
            return self._records
        store = shlex.quote(self.store.rstrip("/"))
        res = self.remote.exec(
            f'for f in {store}/*/{META}/record.json; do [ -r "$f" ] || continue; echo "@@AISCIENTIST_RECORD@@"; '
            'head -c 65536 "$f"; echo; done 2>/dev/null | head -c 4000000', timeout=60)
        out: list[dict[str, Any]] = []
        for block in (getattr(res, "stdout", "") or "").split("@@AISCIENTIST_RECORD@@"):
            block = block.strip()
            if not block:
                continue
            try:
                data = json.loads(block)
            except ValueError:
                continue
            if isinstance(data, dict) and _ID.match(str(data.get("id") or "")):
                out.append(data)
        self._records, self._records_at = out, time.monotonic()
        return out

    def find(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Published environments that provide ``query`` (a command, an R package, a module, a package
        name), as short entries the model can act on."""
        hits = [r for r in self.records() if _matches(r, query)]
        return [{"environment": r["id"], "kind": r.get("kind", "conda"), "provides": r.get("provides", []),
                 "packages": (r.get("recipe") or {}).get("conda", [])[:12] or [r.get("image", "")],
                 "built": r.get("built_at", "")} for r in hits[:limit]]

    def resolve(self, ids: "list[str] | tuple[str, ...] | str") -> list[EnvRef]:
        """Published environments by id, for a call to bind. Raises ``RecipeError`` naming any id
        that is not a published environment."""
        if isinstance(ids, str):
            ids = [i for i in re.split(r"[\s,]+", ids) if i]
        refs: list[EnvRef] = []
        for env_id in list(dict.fromkeys(str(i).strip() for i in ids if str(i).strip()))[:4]:
            rec = self.record(env_id)
            if rec is None:
                raise RecipeError(
                    f"No lab environment '{env_id}'. Use the id request_environment returned, or find one with "
                    "find_software.")
            refs.append(EnvRef(id=env_id, path=env_path(self.store, env_id), kind=str(rec.get("kind") or "conda"),
                               image=str(rec.get("image") or ""), provides=tuple(rec.get("provides") or ())))
        return refs

    # -- building -----------------------------------------------------------------------------

    def ready_result(self, recipe: Recipe, record: dict[str, Any], *, reused: bool) -> dict[str, Any]:
        how = ("Pass environment='{id}' to run_in_environment (with a command) to run in this container."
               if record.get("kind") == "image" else
               "Pass environment='{id}' to run_shell, run_code or run_in_environment: the environment is "
               "mounted read-only and its bin/ comes first on PATH (its own python, Rscript and programs).")
        return {"status": "ready", "environment": record.get("id", recipe.env_id),
                "kind": record.get("kind", recipe.kind), "reused": reused,
                "provides": record.get("provides", list(recipe.provides)),
                "built": record.get("built_at", ""), "size_bytes": record.get("size_bytes"),
                "how_to_use": how.format(id=record.get("id", recipe.env_id))}

    def build(self, recipe: Recipe, *, approved_by: str) -> dict[str, Any]:
        """Build and publish ``recipe`` in a Slurm job, and return the result for the model."""
        env_id = recipe.env_id
        self._counter += 1
        rdir = f"{self.stage_dir.rstrip('/')}/{env_id}-{int(time.time())}-{self._counter}"
        record = {
            "id": env_id, "name": recipe.name, "kind": recipe.kind, "path": env_path(self.store, env_id),
            "recipe": {"conda": list(recipe.conda_specs()), "pip": list(recipe.pip), "r": list(recipe.r),
                       "script": recipe.script, "channels": list(CHANNELS), "base": BASE_IMAGE} if not recipe.image
            else {"image": recipe.image},
            "image": recipe.image, "provides": list(recipe.provides), "verify": list(recipe.verify),
            "reason": recipe.reason, "requested_by": self.requested_by, "account": self.account,
            "approved_by": approved_by, "run_id": self.run_id, "session_id": self.session_id,
            "recipe_version": RECIPE_VERSION, "size_bytes": "__SIZE__", "files": "__FILES__",
            "build_job": "__JOB__", "built_at": "__BUILT_AT__", "built_on": "__NODE__",
        }
        files = recipe_files(recipe, record)
        # "__SIZE__" and "__FILES__" are numbers once the job fills them in.
        files["record.json"] = files["record.json"].replace('"__SIZE__"', "__SIZE__").replace('"__FILES__"', "__FILES__")
        q = shlex.quote
        store = self.store.rstrip("/")
        # The store and its staging folder are group-writable, setgid and sticky (STORE_MODE); only the
        # member who created them can set that, so a failed chmod is not an error.
        staged = [f"mkdir -p {q(rdir)} {q(store)}/{STAGING}",
                  f"{{ chmod {STORE_MODE} {q(store)} {q(store)}/{STAGING} 2>/dev/null || true; }}"]
        staged += [f"printf %s {q(_b64(text))} | base64 -d > {q(rdir + '/' + name)}" for name, text in files.items()]
        made = self.remote.exec(" && ".join(staged), timeout=60)
        if not getattr(made, "ok", getattr(made, "exit_status", 1) == 0):
            return {"status": "failed", "phase": "stage", "environment": env_id,
                    "error": f"could not stage the recipe on HPC3: {(getattr(made, 'stderr', '') or '')[-500:]}"}
        try:
            if recipe.image:
                script = image_script(recipe, store=store, recipe_dir=rdir, sif=self.image_sif(recipe.image),
                                      container_bin=self.container_bin)
                cpus, mem, limit = PULL_CPUS, PULL_MEM_GB, PULL_TIME_LIMIT
            else:
                script = build_script(recipe, store=store, recipe_dir=rdir, base_sif=self.base_image(),
                                      pkgs_cache=self.pkgs_cache, container_bin=self.container_bin,
                                      max_gb=self.max_gb)
                cpus, mem, limit = BUILD_CPUS, BUILD_MEM_GB, BUILD_TIME_LIMIT
            self._say("info", f"Building environment {env_id} in a Slurm job on HPC3 (once for the lab).")
            job = self.run_job(script, f"aiscientist_env_{self._counter}", cpus, mem, limit)
        except Exception as exc:  # noqa: BLE001 - the model reads the reason and may correct the recipe
            if type(exc).__name__ == "JobCancelled":
                return {"status": "cancelled", "environment": env_id, "error": "Stopped by the user."}
            return {"status": "failed", "phase": "job", "environment": env_id, "error": f"{type(exc).__name__}: {exc}"}
        log = str(job.get("log") or "")
        built = self.record(env_id)
        self._records = None
        if built is not None:
            self._say("success", f"Environment {env_id} is ready.")
            return self.ready_result(recipe, built, reused="AISCIENTIST_ENV_PUBLISHED" not in log)
        phase = next((ln.split(" ", 1)[1].strip() for ln in reversed(log.splitlines())
                      if ln.startswith("AISCIENTIST_ENV_FAILED ")), "") or str(job.get("state") or "job")
        checks = [ln.strip() for ln in log.splitlines() if ln.startswith("AISCIENTIST_ENV_CHECK ")]
        return {"status": "failed", "environment": env_id, "phase": phase, "job": job.get("job_id", ""),
                "slurm_state": job.get("state", ""), "checks": checks[:MAX_PROVIDES + MAX_VERIFY],
                "log_tail": _tail(log, 40),
                "next": ("Nothing was published. Read the log, correct the recipe once (a version that does not "
                         "exist, a package name, a wrong `provides`) and call request_environment again; if it "
                         "cannot be built, say that the step needs this software and do not hand-write a "
                         "replacement.")}


def _tail(text: str, n: int) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])
