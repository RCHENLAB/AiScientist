"""A lab-SHARED, self-installing Python package cache for the Singularity sandboxes.

The problem: ``analysis.sif`` is read-only and fixed at build time, so the moment a snippet needs
a library the image lacks, the step dies — and the model's usual recovery (reimplement it by
hand, or vendor a copy) is exactly what you do not want in an analysis pipeline. The requirement
(Yijun): let the sandbox install what it is missing, and make every install **public**, so a
package one member installs is instantly there for everyone and is never downloaded twice.

Three constraints shaped the design, and none of them are incidental:

**1. On HPC3 a shared directory is not a shared file.** Files created by user A cannot be
modified or deleted by user B, which is precisely why ``hpc_gc.SHARED_SUBDIRS`` are all per-user
(its own comment: "one shared copy would mean each overwriting a file the others own"). So the
cache is **immutable and content-keyed**: each package version installs into its own directory,
published by an atomic rename, and *nothing ever writes to a published directory again*.
Cross-user ownership stops mattering because there is no second write. Readers need only ``r-x``.

**2. Two people can install the same thing at the same time.** Each stages into its OWN
directory, then publishes with ``mv -T`` — a directory rename that fails with ``ENOTEMPTY`` if
the destination already exists. That failure IS the arbiter: the loser detects the winner's
directory, discards its staging copy, and uses the winner's. No lock file, which matters because
advisory locking on a parallel filesystem is not something to bet correctness on.

**3. A shared import path is a code-execution channel between lab members.** If member A can put
a ``numpy`` on member B's ``sys.path``, A can run code as B. Four fences:

  * the cache is for **missing** packages only — publishing a top-level module the image already
    provides is refused outright, so nothing in the image can be shadowed;
  * a dependency that collides with an image module is **pruned** from the staged tree before
    publish, so the image's version stays authoritative even transitively;
  * ``sys.path`` is extended by **appending** (via a generated ``sitecustomize``), never by
    ``PYTHONPATH`` alone — ``PYTHONPATH`` sorts *before* ``site-packages``, so using it directly
    would let a cached dependency silently outrank the image's copy;
  * only plain PyPI requirements are accepted — no VCS URLs, no local paths, no index override —
    and every publish records who did it.

Storage lives under ``<shared_root>/pkgs/<key>/``, keyed by image and Python version so a tree
built for ``analysis.sif``/3.11 is never imported into ``vep.sif``/3.9.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

# A pip requirement we are willing to install: a PEP 508 name, optional extras, optional simple
# version pin. Anything else — a URL, a path, a flag, an environment marker — is refused. This is
# the boundary that keeps "install a package" from becoming "run arbitrary installer code from an
# arbitrary place", on a path other people import from.
_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"          # name
    r"(?:\[[A-Za-z0-9,._-]+\])?"            # extras
    r"(?:\s*(?:==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9][A-Za-z0-9._*+!-]*)?$")

_MAX_PACKAGE_MB = 2048


class PackageCacheError(RuntimeError):
    """A failure the model should read and act on (bad requirement, refused shadow, install failed)."""


def normalize_slug(requirement: str) -> str:
    """A deterministic, filesystem-safe directory name for one requirement.

    Deterministic is the point: two users asking for ``scikit-bio==0.6.0`` must land on the same
    directory, or the cache silently stops being shared. Case and the ``_``/``-`` distinction are
    normalised the way PyPI itself treats them.
    """
    s = (requirement or "").strip().lower().replace(" ", "")
    s = s.replace("==", "-").replace(">=", "-ge-").replace("<=", "-le-")
    s = s.replace("~=", "-compat-").replace("!=", "-ne-").replace(">", "-gt-").replace("<", "-lt-")
    s = re.sub(r"[\[\],]+", "-", s)
    s = re.sub(r"[^a-z0-9._-]", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return s or "package"


def top_level_name(requirement: str) -> str:
    """The module a requirement is *expected* to provide, for the shadow check and verification.

    A guess — ``scikit-learn`` imports as ``sklearn`` — which is why ``ensure`` takes an explicit
    ``import_name`` override. Wrong only in the direction of asking, never of allowing.
    """
    name = re.split(r"[\[<>=!~]", (requirement or "").strip(), maxsplit=1)[0]
    return name.strip().replace("-", "_").lower()


@dataclass
class SharedPackageCache:
    """The lab-shared install cache for one container image.

    ``root`` is the shared parent (``<shared_root>/pkgs``); the per-image, per-Python subdirectory
    is derived, so several images can share one root without ever sharing a tree.
    """

    root: str
    image: str
    singularity_bin: str = "singularity"
    # The Lmod module that PUTS `singularity` on PATH. Not optional in practice on HPC3: the
    # cache's probes run through `HpcShell._worker`, i.e. a bare `srun ... bash -lc`, and a
    # compute node has no `singularity` binary until the module is loaded. The run_code sbatch
    # script loads it (build_analysis_script's `container_module`); this path did not, so every
    # probe exited 127 and the whole cache failed open and silent. Empty = do not load (mock
    # hosts, and any site where the binary is already on PATH).
    container_module: str = ""
    max_mb: int = _MAX_PACKAGE_MB
    _key: str | None = field(default=None, repr=False)
    #: Modules this session has CONFIRMED the container can import. Each probe is an ``srun`` +
    #: module load + ``singularity exec`` + interpreter start on a compute node, and nothing
    #: remembered the answer — so every ``run_code`` call re-probed the same handful of always-
    #: present packages, which is where a measured ten minutes per call went. Only a status-0
    #: probe is recorded: an image cannot LOSE a module mid-run, while a probe that merely failed
    #: to execute says nothing, and caching that would hide a real gap for the rest of the run.
    _present: set = field(default_factory=set, repr=False)

    # --- keying --------------------------------------------------------------

    def _image_tag(self) -> str:
        return image_tag_for(self.image)

    def cache_key(self, shell) -> str:
        """``<image>-py<X.Y>``, read once from the container itself.

        Asking the image rather than assuming means a rebuilt container that moved Python
        version gets a fresh tree automatically, instead of importing wheels built for an ABI it
        no longer has.
        """
        if self._key:
            return self._key
        probe = self._contained("python -c 'import sys; print(\"%d.%d\" % sys.version_info[:2])'",
                                binds_rw=())
        res = shell._worker(probe, 300)
        ver = (getattr(res, "out", "") or "").strip().splitlines()[-1:] or [""]
        version = ver[0].strip()
        if not re.match(r"^\d+\.\d+$", version):
            raise PackageCacheError(
                f"Could not read the Python version inside {self.image}: "
                f"{_tail(getattr(res, 'stderr', '') or getattr(res, 'stdout', ''))}")
        self._key = f"{self._image_tag()}-py{version}"
        return self._key

    def key_dir(self, shell) -> str:
        return f"{self.root.rstrip('/')}/{self.cache_key(shell)}"

    # --- container plumbing --------------------------------------------------

    def _contained(self, command: str, *, binds_rw: tuple[str, ...] = (),
                   binds_ro: tuple[str, ...] = (), network: bool = False) -> str:
        """A ``singularity exec`` line for one command inside the analysis image.

        ``--containall`` for the same reason every other job here uses it: the install must see
        only the directories we name, so a package's setup script cannot reach the user's ``$HOME``.
        """
        parts = [self.singularity_bin, "exec", "--containall", "--writable-tmpfs"]
        if not network:
            parts += ["--net", "--network", "none"]
        for p in binds_ro:
            parts += ["-B", f"{shlex.quote(p)}:{shlex.quote(p)}:ro"]
        for p in binds_rw:
            parts += ["-B", f"{shlex.quote(p)}:{shlex.quote(p)}"]
        parts += [shlex.quote(self.image), "bash", "-lc", shlex.quote(command)]
        line = " ".join(parts)
        if self.container_module:
            # Braces + `&&`, not a bare `;`: callers compose this into `mkdir -p X && <line>`,
            # and a statement separator there would run singularity even when the mkdir failed.
            # `|| true` inside, because a site with the binary already on PATH has no module to
            # load and must not be turned into a hard failure.
            line = (f"{{ module load {shlex.quote(self.container_module)} >/dev/null 2>&1 "
                    f"|| true; }} && {line}")
        return line

    # --- lookup --------------------------------------------------------------

    def published(self, shell) -> list[str]:
        """Every published package directory for this image. Login-node metadata read."""
        res = shell._login(
            f"find {shlex.quote(self.key_dir(shell))} -mindepth 1 -maxdepth 1 -type d "
            f"! -name '.staging' 2>/dev/null | sort", timeout=45)
        return [line.strip() for line in (getattr(res, "out", "") or "").splitlines() if line.strip()]

    def _is_published(self, shell, slug: str) -> bool:
        path = f"{self.key_dir(shell)}/{slug}/.manifest.json"
        res = shell._login(f"test -f {shlex.quote(path)} && echo yes || true", timeout=30)
        return (getattr(res, "out", "") or "").strip() == "yes"

    def manifest(self, shell, slug: str) -> dict[str, Any]:
        path = f"{self.key_dir(shell)}/{slug}/.manifest.json"
        res = shell._login(f"cat {shlex.quote(path)} 2>/dev/null || true", timeout=30)
        try:
            return json.loads((getattr(res, "out", "") or "").strip() or "{}")
        except ValueError:
            return {}

    # --- install -------------------------------------------------------------

    def ensure(self, shell, requirement: str, import_name: str = "") -> dict[str, Any]:
        """Make ``requirement`` importable inside the sandbox, installing it only if nobody has.

        The cache-hit path does no network, no confirmation and no work — which is the whole
        point of sharing: the second person to need a package pays nothing.
        """
        req = (requirement or "").strip()
        if not _REQUIREMENT.match(req):
            raise PackageCacheError(
                f"'{req}' is not a plain package requirement. Only PyPI names with an optional "
                "version are accepted (e.g. 'pyranges' or 'scikit-bio==0.6.0') — not URLs, paths, "
                "or pip flags, because this installs onto a path the whole lab imports from.")

        slug = normalize_slug(req)
        module = (import_name or "").strip() or top_level_name(req)
        key_dir = self.key_dir(shell)

        if self._is_published(shell, slug):
            info = self.manifest(shell, slug)
            return {"status": "already_available", "package": req, "module": module,
                    "path": f"{key_dir}/{slug}", "installed_by": info.get("installed_by"),
                    "installed_at": info.get("installed_at"),
                    "note": "Already in the lab-shared cache — nothing was downloaded."}

        # Publishing runs third-party installer code and puts importable code where every lab
        # member will load it. That is exactly the class of action the HITL gate exists for.
        #
        # The prompt states what AiScientist WILL DO, not a command for the user to run. The
        # product promise is that a user never has to touch HPC3 themselves, so a confirmation
        # that reads like homework ("here is a pip line, go run it") breaks the promise even when
        # the mechanism works.
        from ..hpc.shell import ConfirmRequest
        if not shell._ask(ConfirmRequest(
                "install",
                f"Install “{req}” on HPC3 so this analysis can continue",
                _install_plan(req, module, key_dir, slug))):
            return {"status": "declined", "package": req,
                    "reason": "The user declined the install, so this package is unavailable. "
                              "Use a different approach that relies only on what is already "
                              "installed — do NOT hand-write a replacement for the library."}

        if self._image_provides(shell, module):
            raise PackageCacheError(
                f"The container already provides '{module}'. The shared cache is for MISSING "
                "packages only — publishing this would shadow the image's own copy for every "
                "user. Import it directly, or ask an operator to rebuild the image if the "
                "version is wrong.")

        user = getattr(shell.remote, "username", "") or "user"
        staging = f"{key_dir}/.staging/{user}-{_token()}"
        target = f"{key_dir}/{slug}"

        shell._login(f"mkdir -p {shlex.quote(key_dir)}/.staging && "
                     f"chmod 2775 {shlex.quote(key_dir)} {shlex.quote(key_dir)}/.staging 2>/dev/null || true",
                     timeout=45)

        # TMPDIR must point at the bound dfs3b staging area, NOT the container's /tmp.
        # `--writable-tmpfs` gives a 64 MB tmpfs, and pip unpacks wheels through TMPDIR — measured
        # on HPC3, installing pyranges died with `[Errno 28] No space left on device` while
        # fetching numpy (16.9 MB) and pandas (11.3 MB). Any package with real dependencies would
        # have failed the same way.
        install = self._contained(
            f"mkdir -p {shlex.quote(staging)}/.tmp && export TMPDIR={shlex.quote(staging)}/.tmp && "
            f"python -m pip install --no-cache-dir --no-input --disable-pip-version-check "
            f"--target {shlex.quote(staging)} {shlex.quote(req)}",
            binds_rw=(key_dir,), network=True)
        res = shell._worker(f"mkdir -p {shlex.quote(staging)} && {install}", 1800)
        if getattr(res, "exit_status", 1) != 0:
            shell._worker(f"rm -rf {shlex.quote(staging)}", 120)
            raise PackageCacheError(
                f"pip could not install '{req}': "
                f"{_tail(getattr(res, 'stderr', '') or getattr(res, 'stdout', ''))}")

        # Drop pip's scratch before anything looks at the tree: it is not part of the package,
        # and leaving it would put a `.tmp` directory on the published import path.
        shell._worker(f"rm -rf {shlex.quote(staging)}/.tmp", 120)
        pruned = self._prune_image_collisions(shell, staging)
        published = self._publish(shell, staging, target, req, module, user, pruned)
        if not published:
            return {"status": "already_available", "package": req, "module": module,
                    "path": target,
                    "note": "Another session published the same package first; using theirs."}

        verify = self._verify_import(shell, module, target)
        return {"status": "installed", "package": req, "module": module, "path": target,
                "importable": verify, "pruned_to_image_versions": pruned,
                "note": ("Installed into the lab-shared cache — every user can import it now, "
                         "and nobody will download it again.")}

    # --- the safety fences ---------------------------------------------------

    def _image_provides(self, shell, module: str) -> bool:
        """Does the container ALREADY provide this top-level module?

        Run with the cache unbound, so the answer is about the image alone.
        """
        probe = self._contained(
            "python -c \"import importlib.util,sys; "
            f"sys.exit(0 if importlib.util.find_spec({module!r}) else 3)\"")
        res = shell._worker(probe, 300)
        return getattr(res, "exit_status", 3) == 0

    def _prune_image_collisions(self, shell, staging: str) -> list[str]:
        """Delete staged top-level modules the image already provides.

        ``pip --target`` installs dependencies alongside the package, so a request for one
        library can drag in its own ``pandas``. Left in place, that copy would outrank the
        image's for every user of the cache. Removing it keeps the image authoritative and keeps
        the cache to its stated job: supplying what is missing.
        """
        listing = shell._login(
            f"find {shlex.quote(staging)} -mindepth 1 -maxdepth 1 -printf '%f\\n' 2>/dev/null || true",
            timeout=60)
        names = []
        for entry in (getattr(listing, "out", "") or "").splitlines():
            e = entry.strip()
            if not e or e.endswith((".dist-info", ".egg-info", ".data")) or e == "__pycache__":
                continue
            names.append(e[:-3] if e.endswith(".py") else e)

        pruned: list[str] = []
        for name in names:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                continue
            if self._image_provides(shell, name):
                shell._worker(f"rm -rf {shlex.quote(staging)}/{shlex.quote(name)} "
                              f"{shlex.quote(staging)}/{shlex.quote(name)}.py", 120)
                pruned.append(name)
        return pruned

    def _publish(self, shell, staging: str, target: str, req: str, module: str,
                 user: str, pruned: list[str]) -> bool:
        """Atomically publish the staged tree. Returns False if someone else got there first.

        ``mv -T`` performs a real ``rename(2)`` of the directory, which fails when the
        destination exists and is non-empty. That failure is the concurrency arbiter — no lock,
        which matters because advisory locking on a parallel filesystem is not worth betting on.
        """
        from datetime import datetime, timezone
        manifest = json.dumps({
            "requirement": req, "module": module, "installed_by": user,
            # Stamped here rather than as a shell `$(date)`: the command is shlex-quoted, so a
            # substitution would be published verbatim as the literal string.
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "pruned_to_image_versions": pruned, "image": self.image, "cache_key": self._key,
        })
        # a+rX, not a+rw: a published tree is read-only forever, which is what makes cross-user
        # file ownership a non-issue.
        prep = (f"printf '%s' {shlex.quote(manifest)} > {shlex.quote(staging)}/.manifest.json && "
                f"chmod -R a+rX {shlex.quote(staging)}")
        res = shell._worker(f"{prep} && mv -T {shlex.quote(staging)} {shlex.quote(target)}", 300)
        if getattr(res, "exit_status", 1) == 0:
            return True
        if self._is_published(shell, target.rsplit("/", 1)[-1]):
            shell._worker(f"rm -rf {shlex.quote(staging)}", 120)     # the other session won
            return False
        raise PackageCacheError(
            f"Could not publish '{req}' into the shared cache: "
            f"{_tail(getattr(res, 'stderr', '') or getattr(res, 'stdout', ''))}")

    def _verify_import(self, shell, module: str, target: str) -> bool:
        probe = self._contained(
            f"PYTHONPATH={shlex.quote(target)} python -c 'import {module}'",
            binds_ro=(target,))
        res = shell._worker(probe, 300)
        return getattr(res, "exit_status", 1) == 0


# --- wiring the cache into a sandbox launch ----------------------------------


# The sitecustomize text is shared with the per-run dependency installer, which runs INSIDE the
# analysis container; it is defined with the tools so they need nothing from the gateway.
from ..tools.api import SITECUSTOMIZE  # noqa: E402,F401


def _install_plan(req: str, module: str, key_dir: str, slug: str) -> str:
    """The human-readable plan behind an install confirmation.

    Written for someone deciding, not for someone executing: what will happen, where, on whose
    behalf, what it costs, and what it means for everyone else. **AiScientist performs every
    step** — the user is approving, never running anything on the cluster themselves.
    """
    return (
        f"AiScientist will do this for you on HPC3 — you do not need to log in anywhere:\n"
        f"  1. Download {req} from PyPI on your session's compute node (not a login node).\n"
        f"  2. Install it into the lab's shared package area, as its own directory:\n"
        f"     {key_dir}/{slug}\n"
        f"  3. Drop any dependency the container already ships, so the existing versions of "
        f"pandas/numpy/scanpy stay in charge.\n"
        f"  4. Make it importable as `{module}` for this and every later step.\n\n"
        f"Because the area is shared, this is a one-time cost: anyone else in the lab who needs "
        f"{req} later gets it instantly, with no download and no prompt.\n"
        f"If you decline, the analysis continues without it and the step will be attempted a "
        f"different way."
    )


def _present_memo(cache) -> set:
    """The per-session set of modules confirmed importable, attached to ``cache`` on first use.

    Read through ``getattr`` rather than off the attribute directly: this function takes anything
    cache-SHAPED, and a stub without the field would otherwise raise an AttributeError that
    ``PreflightingExecutor`` swallows as "preflight skipped" — silently disabling dependency
    resolution instead of failing loudly. A stub that refuses the assignment simply gets no memo,
    i.e. exactly the previous behaviour.
    """
    memo = getattr(cache, "_present", None)
    if isinstance(memo, set):
        return memo
    memo = set()
    try:
        cache._present = memo
    except Exception:  # noqa: BLE001 - frozen/slotted stand-ins keep working, just without a memo
        return set()
    return memo


def missing_modules(shell, cache: "SharedPackageCache | None", modules: list[str]) -> list[str]:
    """Which of ``modules`` the sandbox cannot import today — image *and* shared cache together.

    Checked against the real container rather than a list we maintain, because the image is
    rebuilt independently of this code and a stale assumption here would either block an install
    that is needed or trigger one that is not.
    """
    if cache is None or not modules:
        return []
    present = _present_memo(cache)
    if all(m in present for m in modules):
        return []          # nothing to ask the cluster; skip the bind check too
    # Bind the cache root only if it EXISTS. Singularity aborts the whole container with exit 255
    # on a missing bind source — measured: on a first run, before any package had been published,
    # every probe failed that way and the caller offered to install `pandas`, which the image has.
    root = cache.root.rstrip("/")
    exists = shell._login(f"test -d {shlex.quote(root)} && echo yes || true", timeout=30)
    binds = (root,) if (getattr(exists, "out", "") or "").strip() == "yes" else ()
    path = ":".join(cache.published(shell)) if binds else ""

    missing: list[str] = []
    for mod in modules:
        if mod in present:                 # already confirmed importable this session
            continue
        probe = cache._contained(
            (f"PYTHONPATH={shlex.quote(path)} " if path else "")
            + "python -c \"import importlib.util,sys; "
              f"sys.exit(0 if importlib.util.find_spec({mod!r}) else 3)\"",
            binds_ro=binds)
        status = getattr(shell._worker(probe, 300), "exit_status", None)
        if status == 0:
            present.add(mod)
        if status == 3:
            missing.append(mod)
        elif status != 0:
            # The probe itself did not run (bad bind, image unreadable, srun failure). Treat that
            # as "present" rather than "missing": a false MISSING costs the user a bogus install
            # confirmation, and a confirmation the user learns to distrust is worse than no
            # confirmation at all.
            continue
    return missing


def sitecustomize_dir(root: str) -> str:
    return f"{root.rstrip('/')}/_sitecustomize"


def ensure_sitecustomize(remote, root: str) -> str:
    """Write the generated ``sitecustomize.py`` once, world-readable. Returns its directory.

    Idempotent and content-identical for every user, so whoever gets there first wins and the
    rest are no-ops — the same immutability rule the package trees follow, for the same reason
    (one user cannot overwrite another's file on this filesystem).

    Also the ONLY place ``root`` itself gets created, which is why it is chmod-ed here. Left to
    the default umask, whoever touched the cache first owned a ``drwxr-sr-x`` root — no group
    write — and since a directory can only be chmod-ed by its owner, every other lab member was
    permanently unable to create their image key dir inside it. Measured exactly that way:
    ``mkdir: cannot create directory '<root>/analysis.sif-py3.11': Permission denied``. 2775 is
    group-write + setgid, so the group is inherited and the next member can publish.
    """
    d = sitecustomize_dir(root)
    remote.exec(
        f"mkdir -p {shlex.quote(root.rstrip('/'))} && "
        f"chmod 2775 {shlex.quote(root.rstrip('/'))} 2>/dev/null || true; "
        f"mkdir -p {shlex.quote(d)} && "
        f"[ -f {shlex.quote(d)}/sitecustomize.py ] || "
        f"{{ printf '%s' {shlex.quote(SITECUSTOMIZE)} > {shlex.quote(d)}/sitecustomize.py.$$ && "
        f"chmod a+r {shlex.quote(d)}/sitecustomize.py.$$ && "
        f"mv -n {shlex.quote(d)}/sitecustomize.py.$$ {shlex.quote(d)}/sitecustomize.py; }} ; "
        f"rm -f {shlex.quote(d)}/sitecustomize.py.$$ ; chmod a+rX {shlex.quote(d)} 2>/dev/null || true",
        timeout=60)
    return d


def cache_preamble(root: str, image_tag: str) -> str:
    """Bash that wires the shared cache into the interpreter, run INSIDE the container.

    Resolved in-container on purpose: the cache key depends on the image's own Python version, so
    asking the image is both correct and free. The alternative — having the gateway probe the
    version before every sandbox launch — would cost a compute allocation just to *read* the
    cache, which defeats the point.

    Fails open in every branch (``|| true``, early ``return 0``): a missing or unreadable cache
    must degrade to "the image's packages only", never to a failed analysis step.
    """
    d = sitecustomize_dir(root)
    return (
        "_aisci_pkg_cache() {\n"
        f"  local root={shlex.quote(root.rstrip('/'))} tag={shlex.quote(image_tag)}\n"
        # `python3` first: plenty of images ship only python3, and a bare `python` there fails
        # silently, which would disable the cache with no visible symptom beyond an ImportError
        # the model cannot explain.
        "  local py; for py in python3 python; do command -v \"$py\" >/dev/null 2>&1 && break; py=; done\n"
        "  [ -n \"$py\" ] || return 0\n"
        "  local ver; ver=$(\"$py\" -c 'import sys;print(\"%d.%d\"%sys.version_info[:2])' 2>/dev/null) || return 0\n"
        "  local key=\"$root/$tag-py$ver\"\n"
        "  [ -d \"$key\" ] || return 0\n"
        "  local dirs; dirs=$(find \"$key\" -mindepth 1 -maxdepth 1 -type d ! -name '.staging' 2>/dev/null | sort | paste -sd: -)\n"
        "  [ -n \"$dirs\" ] || return 0\n"
        "  export AISCIENTIST_PKG_CACHE=\"$dirs\"\n"
        f"  export PYTHONPATH={shlex.quote(d)}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        "}\n"
        "_aisci_pkg_cache || true\n"
    )


def image_tag_for(image: str) -> str:
    """The cache-key prefix for an image path — must match :meth:`SharedPackageCache._image_tag`,
    since the writer and the in-container reader derive the same directory independently."""
    base = image.rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "-", base) or "image"


def _token() -> str:
    from uuid import uuid4
    return uuid4().hex[:10]


def _tail(text: str, n: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]
