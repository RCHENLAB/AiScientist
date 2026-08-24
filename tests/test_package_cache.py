"""The lab-shared, self-installing package cache.

Three properties carry the design, and each is pinned here:

1. **Shared means installed once.** The second person to need a package pays nothing — no
   network, no confirmation, no work.
2. **Concurrent installs converge.** Two sessions installing the same package must end with one
   published copy and no corruption; the atomic rename is the arbiter, not a lock file.
3. **The cache cannot shadow the image.** A shared import path is a code-execution channel
   between lab members, so publishing something the container already provides is refused, and a
   dependency that collides is pruned before publish.
"""

from __future__ import annotations

import json
import re

import pytest

from bioagent.gateway import package_cache as pc
from bioagent.gateway.executor import ExecResult
from bioagent.gateway.package_cache import PackageCacheError, SharedPackageCache
from bioagent.gateway.settings import LAB_STORAGE, REFERENCE_ROOT, SHARED_ROOT  # noqa: F401

ROOT = f"{SHARED_ROOT}/pkgs"
IMAGE = f"{SHARED_ROOT}/containers/analysis.sif"
KEY_DIR = f"{ROOT}/analysis.sif-py3.11"


class FakeShell:
    """Stands in for HpcShell: routes login vs worker commands and records both."""

    def __init__(self, *, provides=(), published=(), staged=(), confirm=True, publish_ok=True):
        self.remote = type("R", (), {"username": "alice"})()
        self.provides = set(provides)          # modules the IMAGE already has
        self.published = set(published)        # slugs already in the cache
        self.staged = list(staged)             # top-level names pip would drop in staging
        self.confirm = confirm
        self.publish_ok = publish_ok
        self.login: list[str] = []
        self.worker: list[str] = []
        self.asked: list = []

    # -- the two execution channels
    def _login(self, command, timeout=60.0):
        self.login.append(command)
        if command.startswith("test -f") and ".manifest.json" in command:
            slug = command.split("/")[-2] if "/" in command else ""
            return ExecResult(command, 0, "yes" if slug in self.published else "", "")
        if command.startswith("cat ") and ".manifest.json" in command:
            return ExecResult(command, 0, json.dumps({"installed_by": "bob",
                                                      "installed_at": "2026-08-01T00:00:00+00:00"}), "")
        if command.startswith("find ") and "-maxdepth 1 -printf" in command:
            return ExecResult(command, 0, "\n".join(self.staged), "")
        if command.startswith("find "):
            return ExecResult(command, 0, "\n".join(f"{KEY_DIR}/{s}" for s in sorted(self.published)), "")
        return ExecResult(command, 0, "", "")

    def _worker(self, command, timeout_s=900):
        self.worker.append(command)
        if "sys.version_info" in command:
            return ExecResult(command, 0, "3.11", "")
        # The module name reaches the wire shell-escaped ('"'"'name'"'"'), so skip any
        # non-identifier characters between `find_spec` and the name itself.
        m = re.search(r"find_spec[^A-Za-z_]*([A-Za-z_][A-Za-z0-9_]*)", command)
        if m:
            return ExecResult(command, 0 if m.group(1) in self.provides else 3, "", "")
        if "mv -T" in command:
            return ExecResult(command, 0 if self.publish_ok else 1, "", "destination not empty")
        return ExecResult(command, 0, "", "")

    def _ask(self, req):
        self.asked.append(req)
        return self.confirm

    def _say(self, *a, **k):
        pass


def _cache() -> SharedPackageCache:
    return SharedPackageCache(root=ROOT, image=IMAGE)


# --- requirement validation --------------------------------------------------


@pytest.mark.parametrize("req", [
    "pyranges", "scikit-bio==0.6.0", "pysam>=0.22", "requests[socks]", "some_pkg~=1.2",
])
def test_plain_requirements_are_accepted(req):
    assert pc._REQUIREMENT.match(req)


@pytest.mark.parametrize("req", [
    "git+https://github.com/x/y", "./local/path", "-e .", "--index-url http://evil/x",
    "pkg; os.system('x')", "pkg --extra-index-url http://evil", "http://evil/x.whl", "",
])
def test_anything_that_is_not_a_plain_requirement_is_refused(req):
    """This installs onto a path the whole lab imports from, so the input is narrowed hard: a
    VCS URL or an index override would be arbitrary code from an arbitrary place."""
    sh = FakeShell()
    with pytest.raises(PackageCacheError) as exc:
        _cache().ensure(sh, req)
    assert "plain package requirement" in str(exc.value)
    assert sh.worker == [], "nothing ran"


@pytest.mark.parametrize("req,slug", [
    ("pyranges", "pyranges"),
    ("scikit-bio==0.6.0", "scikit-bio-0.6.0"),
    ("PySam>=0.22", "pysam-ge-0.22"),
    ("requests[socks]", "requests-socks"),
])
def test_slugs_are_deterministic_and_case_normalised(req, slug):
    """Two users asking for the same thing must land on the same directory, or the cache
    silently stops being shared."""
    assert pc.normalize_slug(req) == slug


def test_slug_is_filesystem_safe():
    assert "/" not in pc.normalize_slug("evil/../../etc==1.0")
    assert pc.normalize_slug("...").isprintable()


def test_import_name_is_guessed_but_overridable():
    assert pc.top_level_name("scikit-learn") == "scikit_learn"     # wrong on purpose — hence the override
    assert pc.top_level_name("pyranges>=0.1") == "pyranges"


# --- the shared-means-installed-once property --------------------------------


def test_a_package_someone_already_installed_costs_nothing():
    sh = FakeShell(published={"pyranges"})
    out = _cache().ensure(sh, "pyranges")

    assert out["status"] == "already_available"
    assert out["installed_by"] == "bob"
    assert sh.asked == [], "no confirmation for something already published"
    assert not any("pip install" in c for c in sh.worker), "nothing was downloaded"


def test_a_first_install_asks_because_it_publishes_code_others_will_import():
    sh = FakeShell()
    _cache().ensure(sh, "pyranges")
    assert sh.asked and sh.asked[0].kind == "install"
    assert "pyranges" in sh.asked[0].summary


def test_the_prompt_states_a_plan_aiscientist_will_carry_out():
    """The product promise is that a user never has to touch HPC3 themselves. A confirmation that
    reads like homework ("here is a pip line, go run it") breaks that promise even when the
    mechanism works."""
    sh = FakeShell()
    _cache().ensure(sh, "pyranges")
    detail = sh.asked[0].detail

    assert "AiScientist will do this for you" in detail
    assert "you do not need to log in anywhere" in detail
    assert not detail.lstrip().startswith("pip install"), "a plan to approve, not a command to run"
    # It should also say what it costs everyone else, since the area is shared.
    assert "one-time cost" in detail and "instantly" in detail


def test_a_declined_install_tells_the_model_what_NOT_to_do():
    """Left to itself a model answers a missing library by reimplementing it, which is the worst
    outcome in an analysis pipeline — silently wrong instead of visibly blocked."""
    sh = FakeShell(confirm=False)
    out = _cache().ensure(sh, "pyranges")
    assert "do NOT hand-write a replacement" in out["reason"]


def test_a_declined_install_does_nothing():
    sh = FakeShell(confirm=False)
    out = _cache().ensure(sh, "pyranges")
    assert out["status"] == "declined"
    assert not any("pip install" in c for c in sh.worker)


def test_the_install_is_contained_and_targets_the_shared_tree():
    sh = FakeShell()
    _cache().ensure(sh, "pyranges")
    install = next(c for c in sh.worker if "pip install" in c)

    assert "--containall" in install, "a package's setup code must not see the user's $HOME"
    assert "--net --network none" not in install, "installing needs network, unlike other jobs"
    # pip writes into STAGING, never the published path — the target only ever appears as the
    # destination of the atomic rename, which is what keeps published trees immutable.
    assert f"--target {KEY_DIR}/.staging/alice-" in install.replace("'", "")
    publish = next(c for c in sh.worker if "mv -T" in c)
    assert publish.replace("'", "").endswith(f"{KEY_DIR}/pyranges")


def test_the_cache_key_separates_images_and_python_versions():
    """A tree of wheels built for analysis.sif/3.11 must never be imported into vep.sif/3.9."""
    sh = FakeShell()
    assert _cache().cache_key(sh) == "analysis.sif-py3.11"
    other = SharedPackageCache(root=ROOT, image="/x/vep.sif")
    assert other.cache_key(FakeShell()) == "vep.sif-py3.11"


# --- concurrency -------------------------------------------------------------


def test_publishing_uses_an_atomic_rename_not_a_lock():
    """Advisory locking on a parallel filesystem is not worth betting correctness on; a
    directory rename that fails when the destination exists is."""
    sh = FakeShell()
    _cache().ensure(sh, "pyranges")
    assert any("mv -T" in c for c in sh.worker)


def test_losing_the_publish_race_uses_the_winners_copy_and_cleans_up():
    """Two sessions installing the same package converge on one published copy."""
    sh = FakeShell(publish_ok=False)
    sh.published = set()

    calls = {"n": 0}
    real_login = sh._login

    def login(command, timeout=60.0):
        # The slug is absent on the first check and present by publish time — the other session.
        if command.startswith("test -f") and ".manifest.json" in command:
            calls["n"] += 1
            return ExecResult(command, 0, "" if calls["n"] == 1 else "yes", "")
        return real_login(command, timeout)

    sh._login = login
    out = _cache().ensure(sh, "pyranges")

    assert out["status"] == "already_available"
    assert "Another session published the same package first" in out["note"]
    assert any(c.startswith("rm -rf") for c in sh.worker), "our staging copy was discarded"


def test_a_genuine_publish_failure_is_reported_not_swallowed():
    sh = FakeShell(publish_ok=False)          # rename fails AND nobody else published
    with pytest.raises(PackageCacheError) as exc:
        _cache().ensure(sh, "pyranges")
    assert "Could not publish" in str(exc.value)


def test_staging_is_per_user_so_two_installers_never_share_a_directory():
    """On this filesystem one user cannot overwrite another's files, so concurrent installers
    must not write into the same place."""
    sh = FakeShell()
    _cache().ensure(sh, "pyranges")
    staged = next(c for c in sh.worker if ".staging/" in c)
    assert "/.staging/alice-" in staged


def test_published_trees_are_read_only_forever():
    """Immutability is what makes cross-user file ownership a non-issue: nothing writes twice."""
    sh = FakeShell()
    _cache().ensure(sh, "pyranges")
    publish = next(c for c in sh.worker if "mv -T" in c)
    assert "chmod -R a+rX" in publish and "a+rw" not in publish


# --- the cache cannot shadow the image ---------------------------------------


def test_publishing_something_the_image_already_has_is_refused():
    """If member A could put a `numpy` on member B's sys.path, A could run code as B."""
    sh = FakeShell(provides={"numpy"})
    with pytest.raises(PackageCacheError) as exc:
        _cache().ensure(sh, "numpy")
    assert "already provides" in str(exc.value)
    assert not any("pip install" in c for c in sh.worker)


def test_a_dependency_that_collides_with_the_image_is_pruned_before_publish():
    """`pip --target` drags dependencies in, so a request for one library can ship its own
    pandas. Left in place it would outrank the image's copy for every user of the cache."""
    sh = FakeShell(provides={"pandas", "numpy"}, staged=["pyranges", "pandas", "numpy",
                                                         "pyranges-1.0.dist-info", "__pycache__"])
    out = _cache().ensure(sh, "pyranges")

    assert set(out["pruned_to_image_versions"]) == {"pandas", "numpy"}
    removed = " ".join(c for c in sh.worker if c.startswith("rm -rf"))
    assert "pandas" in removed and "numpy" in removed
    assert "/pyranges " not in removed, "the requested package itself is kept"


def test_metadata_directories_are_not_treated_as_modules():
    sh = FakeShell(staged=["pkg", "pkg-1.0.dist-info", "pkg.egg-info", "__pycache__", "pkg.data"])
    _cache().ensure(sh, "pkg")
    probed = [c for c in sh.worker if "find_spec" in c]
    assert not any("dist-info" in c or "__pycache__" in c for c in probed)


# --- how a sandbox picks the cache up ----------------------------------------


def test_sitecustomize_appends_and_never_prepends():
    """PYTHONPATH sorts BEFORE site-packages, so using it directly would let a cached dependency
    silently outrank the image's copy. Appending in sitecustomize is what buys the right order."""
    assert "sys.path.append" in pc.SITECUSTOMIZE
    assert "sys.path.insert" not in pc.SITECUSTOMIZE


def test_the_preamble_resolves_the_cache_inside_the_container():
    """The key depends on the image's own Python version, so asking the image is both correct
    and free — the alternative would cost an allocation just to READ the cache."""
    pre = pc.cache_preamble(ROOT, "analysis.sif")
    assert "sys.version_info" in pre
    assert "AISCIENTIST_PKG_CACHE" in pre
    assert f"{ROOT}/_sitecustomize" in pre


def test_the_preamble_fails_open():
    """A missing or unreadable cache must degrade to 'the image's packages only', never to a
    failed analysis step."""
    pre = pc.cache_preamble(ROOT, "analysis.sif")
    assert pre.strip().endswith("|| true")
    assert pre.count("return 0") >= 3


def test_writer_and_in_container_reader_derive_the_same_directory():
    """They compute the key independently, so a mismatch would silently disable the cache."""
    sh = FakeShell()
    cache = _cache()
    key = cache.cache_key(sh)
    assert key.startswith(pc.image_tag_for(IMAGE) + "-py")
    assert pc.image_tag_for(IMAGE) in pc.cache_preamble(ROOT, pc.image_tag_for(IMAGE))


def test_sitecustomize_is_written_once_and_not_overwritten():
    """Same immutability rule as the package trees, for the same reason: one user cannot
    overwrite another's file here."""
    cmds = []
    remote = type("R", (), {"exec": lambda self, c, timeout=60.0: cmds.append(c)})()
    d = pc.ensure_sitecustomize(remote, ROOT)

    assert d == f"{ROOT}/_sitecustomize"
    assert "mv -n" in cmds[0], "-n: never clobber an existing copy"
    assert "[ -f" in cmds[0]


def test_the_append_indirection_actually_prevents_a_hijack(tmp_path):
    """Executed, not asserted: run a real interpreter against a real cache tree.

    The tree mimics what `pip install --target <slug>` produces, including a dependency
    (`json.py`) sitting beside the package — the realistic shape of "member A puts code on
    member B's sys.path". The same file is then tried through a plain PYTHONPATH to show the
    naive approach really does get hijacked, so this indirection can't be simplified away
    later on the assumption that it was decorative.
    """
    import os
    import subprocess
    import sys

    root = tmp_path / "pkgs"
    slug = root / f"fakeimg-py{sys.version_info[0]}.{sys.version_info[1]}" / "pyranges"
    (slug / "pyranges").mkdir(parents=True)
    (slug / "pyranges" / "__init__.py").write_text('VALUE = "from-shared-cache"\n')
    (slug / "json.py").write_text('ORIGIN = "HIJACKED"\n')

    d = tmp_path / "pkgs" / "_sitecustomize"
    d.mkdir(parents=True)
    (d / "sitecustomize.py").write_text(pc.SITECUSTOMIZE)

    probe = ("import json, pyranges;"
             "print(pyranges.VALUE, getattr(json, 'ORIGIN', 'STDLIB'))")
    script = pc.cache_preamble(str(root), "fakeimg") + f"\n{sys.executable} -c {probe!r}\n"
    got = subprocess.run(["bash", "-lc", script], capture_output=True, text=True, timeout=120)

    assert got.stdout.strip() == "from-shared-cache STDLIB", got.stderr
    # ... whereas the naive route loses:
    env = {**os.environ, "PYTHONPATH": str(slug)}
    naive = subprocess.run([sys.executable, "-c", "import json;print(getattr(json,'ORIGIN','STDLIB'))"],
                           capture_output=True, text=True, env=env, timeout=120)
    assert naive.stdout.strip() == "HIJACKED", "if this ever says STDLIB the counterfactual is stale"


def test_the_preamble_does_not_assume_a_bare_python_exists():
    """Plenty of images ship only `python3`; a bare `python` there fails silently and the cache
    would be disabled with no symptom beyond an ImportError the model cannot explain."""
    pre = pc.cache_preamble(ROOT, "analysis.sif")
    assert "for py in python3 python" in pre


def test_pip_unpacks_through_the_bound_staging_dir_not_the_container_tmpfs():
    """Measured on HPC3: `--writable-tmpfs` gives a 64 MB /tmp, and pip unpacks wheels through
    TMPDIR — installing pyranges died with `[Errno 28] No space left on device` while fetching
    numpy (16.9 MB) and pandas (11.3 MB). Any package with real dependencies fails without this."""
    sh = FakeShell()
    _cache().ensure(sh, "pyranges")
    install = next(c for c in sh.worker if "pip install" in c)
    assert "TMPDIR=" in install
    assert f"{KEY_DIR}/.staging/" in install.replace("'", "")


def test_pips_scratch_never_reaches_the_published_tree():
    sh = FakeShell()
    _cache().ensure(sh, "pyranges")
    order = [i for i, c in enumerate(sh.worker) if "rm -rf" in c and ".tmp" in c]
    publish = next(i for i, c in enumerate(sh.worker) if "mv -T" in c)
    assert order and order[0] < publish, "the scratch is dropped before the tree is published"
