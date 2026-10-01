"""Resolve a snippet's missing dependencies BEFORE it runs.

Measured on HPC3 (2026-08-10), a snippet that needs a package the image lacks currently gets one
of three outcomes, and the model cannot act sensibly on any of them:

* ``ModuleNotFoundError``, with nothing in the message pointing at the tool that could fix it;
* a wheel that fails to build, because the container's ``/tmp`` is a 64 MB tmpfs;
* worst of all, ``pip returncode: 0`` **followed immediately by** ``ModuleNotFoundError`` in the
  same snippet — ``--containall`` gives an empty ``$HOME``, so ``~/.local/.../site-packages`` did
  not exist at interpreter start and is not on ``sys.path``. A model reading "install succeeded"
  and "module missing" together will loop.

And even a genuine success evaporates: the next step is a fresh container.

This module moves the decision to the front. The code is parsed, its third-party imports are
checked against the container *and* the shared cache, and anything missing is resolved in **one**
confirmation before a single CPU-second is spent. That is better for the user (one clear
question, not a mid-run interruption per package), better for the model (no failure to interpret),
and cheaper (no wasted step).

The wrapper is transparent: with no cache or no missing imports it calls straight through, so a
run that needs nothing behaves exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class PreflightingExecutor:
    """A ``CodeExecutor`` that resolves missing imports first, then delegates.

    ``inner`` is the real executor (``SlurmCodeExecutor`` or the local ``CodeSandbox``); ``shell``
    and ``cache`` supply the container probe, the install path, and the confirmation channel.
    """

    inner: Callable[[str], dict[str, Any]]
    shell: Any = None
    cache: Any = None
    emit: Callable[..., None] | None = None
    #: Packages this run already offered to install and had refused. Asking again on the next step
    #: cannot succeed — the answer is a property of the deployment, not of the snippet — and the
    #: asking is not free: each unresolved module is re-probed inside the container on every
    #: ``run_code`` call. One run spent ten minutes per call re-discovering the same declined
    #: package. Scoped to this executor, i.e. to this run: a fresh run asks again.
    _declined: set = field(default_factory=set)

    def __getattr__(self, name: str) -> Any:
        # Callers read attributes off the executor (``mem_mb`` feeds the run_code guidance, tests
        # reach for internals). Forward anything we do not define so wrapping stays invisible.
        return getattr(self.__dict__["inner"], name)

    def _say(self, level: str, message: str) -> None:
        if self.emit:
            self.emit(level, "preflight", message)

    def __call__(self, code: str) -> dict[str, Any]:
        try:
            report = self._resolve(code)
        except Exception as exc:  # noqa: BLE001 - preflight is an optimisation, never a blocker
            self._say("warning", f"Dependency preflight skipped ({type(exc).__name__}: {exc}).")
            report = None

        result = self.inner(code)
        if report:
            # Recorded on the result so the transcript shows a package was installed for this
            # step — otherwise it looks like the snippet simply worked, and the next reader has
            # no idea the environment changed.
            result = {**result, "dependency_preflight": report}
        return result

    def _resolve(self, code: str) -> dict[str, Any] | None:
        from ..agents.code_imports import distribution_for, third_party_imports
        from .package_cache import missing_modules

        if self.cache is None or self.shell is None:
            return None
        wanted = third_party_imports(code)
        if not wanted:
            return None

        # Never re-probe a package this run has already been refused: the probe is the expensive
        # half (a container start per module), and the answer cannot have changed.
        wanted = [m for m in wanted if m not in self._declined]
        if not wanted:
            return None
        missing = missing_modules(self.shell, self.cache, wanted)
        if not missing:
            return None

        self._say("step", "This step needs "
                          + ", ".join(missing)
                          + " — not in the analysis image. Asking before installing.")
        installed, refused = [], []
        for module in missing:
            req = distribution_for(module)
            try:
                out = self.cache.ensure(self.shell, req, import_name=module)
            except Exception as exc:  # noqa: BLE001 - report per package, keep going
                refused.append({"module": module, "package": req, "status": "failed",
                                "error": str(exc)[:300]})
                self._say("warning", f"Could not install {req}: {exc}")
                continue
            if out.get("status") in ("installed", "already_available"):
                installed.append({"module": module, "package": req, "status": out["status"]})
                if out["status"] == "installed":
                    self._say("success", f"Installed {req} into the lab-shared cache — "
                                         "nobody in the lab will have to download it again.")
            else:
                refused.append({"module": module, "package": req, "status": out.get("status"),
                                "error": out.get("reason")})
                self._declined.add(module)     # do not ask again for the rest of this run
        return {"required": wanted, "missing": missing,
                "installed": installed, "unresolved": refused} or None
