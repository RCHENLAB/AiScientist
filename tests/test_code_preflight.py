"""Static dependency preflight: read a snippet's imports BEFORE running it.

Why this exists is measured, not assumed. On HPC3 a snippet that needs a package the image lacks
gets one of three outcomes today, and a model cannot act sensibly on any of them:

* `ModuleNotFoundError` with nothing pointing at the tool that could fix it;
* a wheel that fails to build in the container's 64 MB `/tmp`;
* `pip returncode: 0` **immediately followed by** `ModuleNotFoundError` in the same snippet,
  because `--containall` leaves `~/.local/.../site-packages` off `sys.path`.

Moving the check to parse time turns all three into one question asked before any compute.
"""

from __future__ import annotations

import pytest

from bioagent.agents.code_imports import distribution_for, third_party_imports, top_level_imports
from bioagent.gateway.code_preflight import PreflightingExecutor


# --- reading the imports -----------------------------------------------------


def test_plain_and_from_imports_are_both_found():
    code = "import pyranges\nfrom scanpy import pp\nimport os, sys\n"
    assert top_level_imports(code) == ["pyranges", "scanpy", "os", "sys"]


def test_submodules_resolve_to_their_root():
    assert top_level_imports("import statsmodels.api as sm") == ["statsmodels"]
    assert top_level_imports("from matplotlib.pyplot import plot") == ["matplotlib"]


def test_stdlib_is_not_something_to_install():
    assert third_party_imports("import os, sys, json, pathlib, subprocess") == []


def test_relative_imports_are_never_installable():
    """`from . import helpers` is the snippet's own code, not a distribution."""
    assert third_party_imports("from . import helpers\nfrom .. import shared") == []


def test_imports_inside_functions_and_try_blocks_still_count():
    code = ("def go():\n    import pyranges\n\n"
            "try:\n    import cyvcf2\nexcept ImportError:\n    cyvcf2 = None\n")
    assert set(third_party_imports(code)) == {"pyranges", "cyvcf2"}


def test_import_shaped_text_in_strings_and_comments_is_ignored():
    """The reason this is an AST walk and not a regex."""
    code = '# import evil\ns = "import alsoevil"\nimport pyranges\n'
    assert third_party_imports(code) == ["pyranges"]


def test_unparseable_code_reports_nothing_rather_than_guessing():
    """A syntax error surfaces on execution with a far better message than this could invent."""
    assert top_level_imports("import (((") == []


def test_duplicates_collapse_in_first_seen_order():
    assert top_level_imports("import pandas\nimport pandas as pd\nimport numpy") == ["pandas", "numpy"]


@pytest.mark.parametrize("module,dist", [
    ("sklearn", "scikit-learn"), ("cv2", "opencv-python-headless"), ("PIL", "Pillow"),
    ("Bio", "biopython"), ("yaml", "PyYAML"), ("pyranges", "pyranges"), ("wibble", "wibble"),
])
def test_import_names_map_to_pip_names(module, dist):
    assert distribution_for(module) == dist


# --- the wrapper -------------------------------------------------------------


class _Cache:
    root = "/dfs3b/lab/AiScientist/pkgs"

    def __init__(self, missing=(), outcome="installed"):
        self.missing = list(missing)
        self.outcome = outcome
        self.asked_for: list[str] = []

    def published(self, _shell):
        return []

    def _contained(self, cmd, **kw):
        return cmd

    def ensure(self, _shell, req, import_name=""):
        self.asked_for.append(req)
        if self.outcome == "raise":
            raise RuntimeError("pip exploded")
        return {"status": self.outcome, "package": req, "reason": "user said no"}


class _Shell:
    """Reports a module as missing iff the cache says so, via the same exit-status contract."""

    def __init__(self, cache):
        self.cache = cache

    def _login(self, command, timeout=30):
        return type("R", (), {"out": "yes"})()          # the cache root exists

    def _worker(self, command, timeout_s=300):
        missing = any(f"'{m}'" in command for m in self.cache.missing)
        return type("R", (), {"exit_status": 3 if missing else 0})()


def _exec(missing=(), outcome="installed", ran=None):
    cache = _Cache(missing, outcome)
    inner = lambda code: (ran.append(code) if ran is not None else None) or {"status": "ok"}
    return PreflightingExecutor(inner=inner, shell=_Shell(cache), cache=cache), cache


def test_nothing_missing_means_no_prompt_and_no_extra_work():
    ex, cache = _exec(missing=())
    out = ex("import scanpy\nprint(1)")
    assert out == {"status": "ok"}
    assert cache.asked_for == []
    assert "dependency_preflight" not in out


def test_a_missing_import_is_installed_before_the_snippet_runs():
    ran: list[str] = []
    ex, cache = _exec(missing=["pyranges"], ran=ran)
    out = ex("import pyranges\nprint(1)")

    assert cache.asked_for == ["pyranges"], "resolved from the code, not from a runtime failure"
    assert ran, "the snippet still ran afterwards"
    assert out["dependency_preflight"]["installed"][0]["module"] == "pyranges"


def test_several_missing_packages_are_resolved_in_one_pass():
    """One decision up front beats a mid-run interruption per package."""
    ex, cache = _exec(missing=["pyranges", "cyvcf2"])
    out = ex("import pyranges, cyvcf2, os")
    assert cache.asked_for == ["pyranges", "cyvcf2"]
    assert out["dependency_preflight"]["missing"] == ["pyranges", "cyvcf2"]


def test_the_import_name_is_translated_to_a_pip_name():
    ex, cache = _exec(missing=["sklearn"])
    ex("import sklearn")
    assert cache.asked_for == ["scikit-learn"]


def test_a_declined_install_still_runs_the_snippet_and_records_why():
    """The step is allowed to fail honestly. Blocking it here would hide the cause from the model,
    which then cannot choose a different approach."""
    ex, cache = _exec(missing=["pyranges"], outcome="declined")
    out = ex("import pyranges")
    assert out["status"] == "ok"
    assert out["dependency_preflight"]["unresolved"][0]["status"] == "declined"
    assert out["dependency_preflight"]["installed"] == []


def test_a_failing_install_does_not_stop_the_step():
    ex, _ = _exec(missing=["pyranges"], outcome="raise")
    out = ex("import pyranges")
    assert out["status"] == "ok"
    assert "pip exploded" in out["dependency_preflight"]["unresolved"][0]["error"]


def test_preflight_never_blocks_a_run_when_it_breaks():
    """It is an optimisation, not a gate — a broken probe must not cost the user their analysis."""
    class Boom:
        def published(self, _s): raise RuntimeError("cluster gone")

    ex = PreflightingExecutor(inner=lambda c: {"status": "ok"}, shell=object(), cache=Boom())
    assert ex("import pyranges") == {"status": "ok"}


def test_without_a_cache_the_wrapper_is_transparent():
    ex = PreflightingExecutor(inner=lambda c: {"status": "ok"})
    assert ex("import pyranges") == {"status": "ok"}


def test_the_wrapper_forwards_attributes_of_the_real_executor():
    """`mem_mb` feeds the run_code guidance; wrapping must stay invisible to callers."""
    class Inner:
        mem_mb = 65536
        def __call__(self, code): return {"status": "ok"}

    assert PreflightingExecutor(inner=Inner()).mem_mb == 65536


# --- probes must fail SAFE ---------------------------------------------------


def test_a_broken_probe_never_reports_a_package_as_missing():
    """Measured on HPC3: binding a directory that does not exist aborts the container with exit
    255, and treating any non-zero as "missing" offered to install `pandas`, which the image has.
    A confirmation the user learns to distrust is worse than no confirmation at all."""
    from bioagent.gateway.package_cache import missing_modules

    class Cache:
        root = "/nope"
        def published(self, _s): return []
        def _contained(self, cmd, **kw): return cmd

    class Shell:
        def _login(self, cmd, timeout=30): return type("R", (), {"out": ""})()
        def _worker(self, cmd, timeout_s=300): return type("R", (), {"exit_status": 255})()

    assert missing_modules(Shell(), Cache(), ["pandas", "numpy"]) == []


def test_only_the_definitive_missing_exit_code_counts():
    from bioagent.gateway.package_cache import missing_modules

    class Cache:
        root = "/root"
        def published(self, _s): return []
        def _contained(self, cmd, **kw): return cmd

    class Shell:
        def _login(self, cmd, timeout=30): return type("R", (), {"out": "yes"})()
        def _worker(self, cmd, timeout_s=300):
            return type("R", (), {"exit_status": 3 if "'gone'" in cmd else 0})()

    assert missing_modules(Shell(), Cache(), ["here", "gone"]) == ["gone"]


def test_a_missing_cache_root_is_not_bound():
    """The bind is what aborted the container; skip it until the directory exists."""
    from bioagent.gateway.package_cache import missing_modules
    seen = []

    class Cache:
        root = "/not/created/yet"
        def published(self, _s): return ["should-not-be-used"]
        def _contained(self, cmd, binds_ro=(), **kw):
            seen.append(binds_ro)
            return cmd

    class Shell:
        def _login(self, cmd, timeout=30): return type("R", (), {"out": ""})()
        def _worker(self, cmd, timeout_s=300): return type("R", (), {"exit_status": 3})()

    missing_modules(Shell(), Cache(), ["x"])
    assert seen == [()], "no bind, and no PYTHONPATH from an unreadable cache"


# --- guarded imports are not dependencies ------------------------------------
# One run spent ~605 seconds on EVERY run_code call re-discovering that `torch` was absent,
# asking to install it, and being declined. `torch` is imported by the determinism preamble
# (agents/provenance.py) inside `try: … except Exception: pass`, purely to seed an RNG that may
# not exist — nothing waits on it. Six calls; roughly 60 of the run's 85 minutes.

_PREAMBLE = (
    "import os as _bio_os; _bio_os.environ.setdefault('PYTHONHASHSEED', '0')\n"
    "try:\n    import random as _bio_r; _bio_r.seed(0)\nexcept Exception: pass\n"
    "try:\n    import numpy as _bio_np; _bio_np.random.seed(0)\nexcept Exception: pass\n"
    "try:\n    import torch as _bio_t; _bio_t.manual_seed(0)\nexcept Exception: pass\n"
)


def test_the_determinism_preamble_no_longer_demands_torch():
    from bioagent.agents.code_imports import optional_imports, third_party_imports

    code = _PREAMBLE + "import anndata as ad, pandas as pd\nprint(ad, pd)\n"
    assert "torch" in optional_imports(code)
    assert "torch" not in third_party_imports(code)
    # The analysis code's own imports are unguarded and stay required.
    assert "anndata" in third_party_imports(code)
    assert "pandas" in third_party_imports(code)


def test_a_module_imported_both_ways_stays_required():
    from bioagent.agents.code_imports import optional_imports, third_party_imports

    # The unguarded import is the one that would fail, so tolerance elsewhere does not excuse it.
    code = "try:\n    import scvi\nexcept ImportError: pass\nimport scvi\n"
    assert optional_imports(code) == []
    assert "scvi" in third_party_imports(code)


def test_only_import_tolerant_handlers_count():
    from bioagent.agents.code_imports import optional_imports

    assert "scvi" in optional_imports("try:\n    import scvi\nexcept ImportError: pass\n")
    assert "scvi" in optional_imports("try:\n    import scvi\nexcept ModuleNotFoundError: pass\n")
    assert "scvi" in optional_imports("try:\n    import scvi\nexcept Exception: pass\n")
    assert "scvi" in optional_imports("try:\n    import scvi\nexcept: pass\n")          # bare
    assert "scvi" in optional_imports("try:\n    import scvi\nexcept (KeyError, ImportError): pass\n")
    # A handler that cannot catch an import failure leaves the import a hard dependency.
    assert optional_imports("try:\n    import scvi\nexcept ValueError: pass\n") == []


def test_a_handler_that_binds_a_fallback_keeps_the_import_required():
    from bioagent.agents.code_imports import optional_imports, third_party_imports

    # `except ImportError: cyvcf2 = None` says the snippet means to USE cyvcf2; failing to install
    # it produces a baffling `None` downstream, so preflight should still offer. Only a handler
    # that gives up entirely (pass / ...) marks an import as genuinely optional.
    fallback = "try:\n    import cyvcf2\nexcept ImportError:\n    cyvcf2 = None\n"
    assert optional_imports(fallback) == []
    assert "cyvcf2" in third_party_imports(fallback)

    # Same for a handler that reaches for an alternative package.
    alt = "try:\n    import ujson\nexcept ImportError:\n    import simplejson\n"
    assert optional_imports(alt) == []
    assert "ujson" in third_party_imports(alt)


def test_a_declined_package_is_not_re_probed_on_the_next_step():
    from bioagent.gateway.code_preflight import PreflightingExecutor

    probed: list[list[str]] = []

    class _Cache:
        def ensure(self, shell, req, import_name=None):
            return {"status": "declined", "reason": "not approved for this deployment"}

    def fake_missing(shell, cache, modules):
        probed.append(list(modules))
        return list(modules)

    import bioagent.gateway.package_cache as pc
    original = pc.missing_modules
    pc.missing_modules = fake_missing
    try:
        ex = PreflightingExecutor(inner=lambda code: {"status": "ok"}, shell=object(),
                                  cache=_Cache())
        code = "import somepkg\nprint(somepkg)\n"
        first = ex(code)
        second = ex(code)
    finally:
        pc.missing_modules = original

    assert first["dependency_preflight"]["unresolved"][0]["status"] == "declined"
    # The probe is the expensive half — a container start per module — so the second call must
    # not reach it at all, and must not re-report an install it already knows is refused.
    assert probed == [["somepkg"]], f"re-probed after a decline: {probed}"
    assert "dependency_preflight" not in second
