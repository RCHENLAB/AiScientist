"""Compatibility alias: the package was renamed ``bioagent`` -> ``aiscientist`` on 2026-09-30.

Things outside this repository still name the old package: the prod systemd unit runs
``python -m bioagent.gateway``, a job queued on HPC3 before a deploy starts ``python -m bioagent...``,
and a checkpoint or pickle may have recorded a ``bioagent.*`` class path. This module keeps all of
them working. ``bioagent.<sub>`` IS ``aiscientist.<sub>`` — the same module object, never a second
copy — so classes, ``isinstance`` checks and module state are shared. New code imports
``aiscientist``.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

import aiscientist as _aiscientist  # noqa: F401  (also mirrors the BIOAGENT_*/AISCIENTIST_* env vars)

_OLD, _NEW = "bioagent", "aiscientist"


class _AliasLoader(importlib.abc.Loader):
    """Hands back the already-imported ``aiscientist.<sub>`` module for ``bioagent.<sub>``."""

    def __init__(self, target: str) -> None:
        self._target = target
        self._spec = None

    def create_module(self, spec):
        module = importlib.import_module(self._target)
        self._spec = module.__spec__  # the import system re-points __spec__ at the alias; undone below
        return module

    def exec_module(self, module) -> None:
        if self._spec is not None:
            module.__spec__ = self._spec

    # ``python -m bioagent.x`` (runpy) asks the loader for the code object directly.
    def get_code(self, fullname):
        spec = importlib.util.find_spec(self._target)
        return spec.loader.get_code(self._target) if spec and spec.loader else None

    def is_package(self, fullname) -> bool:
        spec = importlib.util.find_spec(self._target)
        return bool(spec and spec.submodule_search_locations is not None)


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + "."):
            return None
        new = _NEW + fullname[len(_OLD):]
        real = importlib.util.find_spec(new)
        if real is None:
            return None
        # origin = the real file: runpy uses it as sys.argv[0] for ``python -m bioagent.x``.
        return importlib.util.spec_from_loader(
            fullname, _AliasLoader(new), origin=real.origin,
            is_package=real.submodule_search_locations is not None)


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())
