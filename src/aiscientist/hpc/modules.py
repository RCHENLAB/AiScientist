"""HPC3's software modules: what the agent looks up before anything is installed.

RCIC maintains about 360 environment modules on HPC3 (R 4.0.4-4.5.2, samtools, STAR, GATK, ...).
A step that needs software its image lacks should find it here first (docs/environments/README.md
§4, step 2). The catalog is read from RCIC's module files on the login node in ONE metadata
command: each file's category, name, version and description, and the programs it puts on
``PATH``, so a search for a command finds the module even when the names differ (``bedtools`` is
in ``bedtools2``, ``blastn`` in ``ncbi-blast``). It is cached for a day.

Module software runs with ``run_shell``, whose sandbox mounts RCIC's software read-only
(:class:`aiscientist.hpc.shell.ShellSandbox`); it does not exist inside ``run_code``'s image.

This module also reads a failed command's output for the signs of missing software, so the
result can say which module provides it instead of leaving the model to rewrite the method.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MODULE_ROOT = "/opt/rcic/Modules/modulefiles"
# Commands listed per PATH directory: enough for any tool's own programs, while a directory such
# as anaconda's bin (hundreds of entries) cannot flood the catalog.
MAX_COMMANDS_PER_DIR = 400


def catalog_ttl_s() -> int:
    try:
        return max(60, int(os.environ.get("AISCIENTIST_MODULE_CATALOG_TTL_S", "86400")))
    except ValueError:
        return 86400


def catalog_command(root: str = MODULE_ROOT) -> str:
    """The login-node command that prints the catalog, one record per line, tab-separated:
    ``M <category> <module path> <description>`` for each module file, then ``C <module path>
    <program>`` for each program in the directories it prepends to ``PATH``. Metadata only (``find``,
    ``sed``, ``ls`` over a few hundred small files), which RCIC allows on a login node."""
    r = shlex.quote(root.rstrip("/"))
    return (
        f"R={r}; "
        'find "$R" -mindepth 3 -type f ! -name ".*" 2>/dev/null | sort | while IFS= read -r f; do '
        'rel="${f#$R/}"; cat="${rel%%/*}"; mod="${rel#*/}"; '
        "desc=$(sed -n 's/^module-whatis \"Description____ \\(.*\\)\"[[:space:]]*$/\\1/p' \"$f\" | head -1); "
        "printf 'M\\t%s\\t%s\\t%s\\n' \"$cat\" \"$mod\" \"$desc\"; "
        "sed -n 's/^[[:space:]]*prepend-path[[:space:]][[:space:]]*PATH[[:space:]][[:space:]]*\\([^[:space:]]*\\).*/\\1/p' \"$f\" "
        '| while IFS= read -r d; do [ -d "$d" ] || continue; '
        f'ls -1 "$d" 2>/dev/null | head -{MAX_COMMANDS_PER_DIR} | while IFS= read -r c; do '
        "printf 'C\\t%s\\t%s\\n' \"$mod\" \"$c\"; done; done; done"
    )


def _version_key(version: str) -> tuple:
    """Natural order, so 4.10 sorts after 4.9 and 2.7.10a after 2.7.8a. A version without a number
    (``gcc/system``) sorts below every numbered one: it is never "the newest"."""
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.findall(r"\d+|[A-Za-z]+", version))
    return (1 if re.search(r"\d", version) else 0, parts)


@dataclass(frozen=True)
class ModuleVersion:
    load: str                    # what `module load` takes: "R/4.5.2", "qe/7.1/gcc.11.2.0-openmpi.4.1.2"
    version: str
    commands: tuple[str, ...] = ()


@dataclass
class SoftwareModule:
    name: str
    category: str
    description: str
    versions: list[ModuleVersion] = field(default_factory=list)

    def latest(self) -> ModuleVersion:
        return max(self.versions, key=lambda v: _version_key(v.version))

    def as_dict(self, matched_by: str = "") -> dict[str, Any]:
        ordered = sorted(self.versions, key=lambda v: _version_key(v.version), reverse=True)
        return {"name": self.name, "category": self.category, "description": self.description,
                "load": ordered[0].load, "versions": [v.load for v in ordered],
                **({"matched_by": matched_by} if matched_by else {})}


@dataclass
class ModuleCatalog:
    modules: dict[str, SoftwareModule]
    fetched_at: float = 0.0

    @classmethod
    def parse(cls, text: str, fetched_at: float = 0.0) -> "ModuleCatalog":
        """Read :func:`catalog_command`'s output."""
        mods: dict[str, SoftwareModule] = {}
        loads: dict[str, tuple[str, str]] = {}          # module path -> (name, version)
        commands: dict[str, list[str]] = {}
        for line in (text or "").splitlines():
            parts = line.rstrip("\n").split("\t")
            if parts[0] == "M" and len(parts) >= 3:
                category, path = parts[1], parts[2]
                desc = parts[3].strip() if len(parts) > 3 else ""
                name, _, version = path.partition("/")
                if not name or not version:
                    continue
                loads[path] = (name, version)
                mod = mods.setdefault(name.lower(), SoftwareModule(name=name, category=category, description=desc))
                if desc and not mod.description:
                    mod.description = desc
            elif parts[0] == "C" and len(parts) >= 3 and parts[2]:
                commands.setdefault(parts[1], []).append(parts[2])
        for path, (name, version) in loads.items():
            cmds = tuple(dict.fromkeys(commands.get(path, ())))
            mods[name.lower()].versions.append(ModuleVersion(load=path, version=version, commands=cmds))
        return cls(modules={k: m for k, m in mods.items() if m.versions}, fetched_at=fetched_at)

    def __len__(self) -> int:
        return len(self.modules)

    def find(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        """Modules for a program or package name, best first: the module's own name, then a
        program it puts on PATH, then a name that starts with or contains the query, then its
        description. Each module once, its newest version first."""
        q = (query or "").strip()
        if q.lower().startswith(("r:", "py:")):
            q = q.split(":", 1)[1]
        ql = q.lower()
        if not ql:
            return []
        scored: list[tuple[int, str, SoftwareModule, str]] = []
        word = re.compile(rf"(?<![\w-]){re.escape(ql)}(?![\w-])")
        for key, mod in self.modules.items():
            via, score = "", 0
            if key == ql:
                via, score = "name", 100
            elif any(c.lower() == ql for v in mod.versions for c in v.commands):
                via, score = f"program `{q}`", 90
            elif key.startswith(ql) and len(ql) >= 2:
                via, score = "name", 60
            elif ql.startswith(key) and len(key) >= 3:        # "macs2" -> the module "macs"
                via, score = "name", 50
            elif ql in key and len(ql) >= 3:
                via, score = "name", 40
            elif len(ql) >= 3 and word.search(mod.description.lower()):
                via, score = "description", 20
            if score:
                scored.append((score, key, mod, via))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [mod.as_dict(matched_by=via) for _, _, mod, via in scored[:limit]]


_CACHE: dict[str, ModuleCatalog] = {}


def cached_catalog(key: str, fetch: Callable[[], str], *, now: Callable[[], float] = time.time,
                   ttl_s: int | None = None) -> ModuleCatalog | None:
    """The catalog for ``key`` (one per cluster host), fetched with ``fetch()`` when there is none
    or it is older than the TTL. None when it cannot be read; a failure is not cached, so the next
    call tries again."""
    ttl = catalog_ttl_s() if ttl_s is None else ttl_s
    held = _CACHE.get(key)
    if held is not None and now() - held.fetched_at < ttl:
        return held
    try:
        text = fetch()
    except Exception:  # noqa: BLE001 - the catalog is a help, never a blocker
        return held
    cat = ModuleCatalog.parse(text, fetched_at=now())
    if not len(cat):
        return held
    _CACHE[key] = cat
    return cat


# --- the signs of missing software in a failed command's output ----------------------------------

_COMMAND_NOT_FOUND = re.compile(r"(?:^|\n)[^\n]*?([A-Za-z0-9][\w.+-]*): command not found")
_EXEC_NOT_FOUND = re.compile(r"FileNotFoundError: \[Errno 2\] No such file or directory: '([A-Za-z0-9][\w.+-]*)'")
_R_PACKAGE = re.compile(r"there is no package called [‘'\"]([A-Za-z][\w.]*)[’'\"]")


def missing_from_output(text: str) -> list[tuple[str, str]]:
    """``[(kind, name)]`` for the software a failed command lacked: ``command`` (bash's "command not
    found"; Python's FileNotFoundError for a bare program name raised by ``subprocess``) or
    ``r_package`` (R's "there is no package called"). A FileNotFoundError for a data file that
    ``open()`` could not find is not a program, so only one raised from ``subprocess`` counts."""
    text = text or ""
    found: list[tuple[str, str]] = []
    for m in _COMMAND_NOT_FOUND.finditer(text):
        found.append(("command", m.group(1)))
    if "subprocess" in text or "Popen" in text or "_execute_child" in text:
        for m in _EXEC_NOT_FOUND.finditer(text):
            found.append(("command", m.group(1)))
    for m in _R_PACKAGE.finditer(text):
        found.append(("r_package", m.group(1)))
    return list(dict.fromkeys(found))


_REQUEST = ("call request_environment with pinned packages (the user approves it on a card; it is built once "
            "on HPC3 and shared with the lab)")


def software_note(text: str, catalog: ModuleCatalog | None, *, where: str,
                  lab_find: "Callable[[str], list[dict[str, Any]]] | None" = None) -> list[dict[str, Any]]:
    """What to tell the model about each missing program or package in ``text``. ``where`` is the
    tool that failed (``run_code`` or ``run_shell``): module software runs only in ``run_shell``.

    ``lab_find(name)``: the lab environments that provide ``name`` (``hpc/environments.py``); given
    only when lab environments are enabled, and then the hint offers ``request_environment``."""
    notes: list[dict[str, Any]] = []
    for kind, name in missing_from_output(text)[:3]:
        envs = []
        if lab_find is not None:
            try:
                envs = lab_find(name)
            except Exception:  # noqa: BLE001 - a lookup aid only
                envs = []
        if envs:
            ids = [e["environment"] for e in envs]
            notes.append({"name": name, "kind": kind, "modules": [], "lab_environments": ids, "hint": (
                f"The lab environment {ids[0]} provides '{name}': pass environment='{ids[0]}' to {where} "
                "(or to run_shell / run_in_environment) and run it again.")})
            continue
        if kind == "r_package":
            how = (f"install it: {_REQUEST}, e.g. conda ['r-base=<version>', 'bioconductor-{name.lower()}=<version>'] "
                   f"or r ['bioc:{name}'] / ['cran:{name}==<version>'] / ['github:<owner>/<repo>@<tag>'], then pass "
                   "environment=<id> to run_code or run_shell"
                   if lab_find is not None else "there is no approved way to install it in this session")
            notes.append({"name": name, "kind": kind, "modules": [], "hint": (
                f"The R package '{name}' is not installed in this R. To use it, {how}. Do not hand-write a "
                "replacement of the method.")})
            continue
        matches = catalog.find(name, limit=3) if catalog is not None else []
        if matches:
            load = matches[0]["load"]
            if where == "run_shell":
                hint = (f"'{name}' is not loaded. HPC3 has it as the module {load}: run "
                        f"`module load {load} && {name} ...` in run_shell.")
            else:
                hint = (f"'{name}' is not in run_code's image. HPC3 has it as the module {load}: run it with "
                        f"run_shell (`module load {load} && {name} ...`) on files in your AiScientist folder "
                        "(write them from run_code to AISCIENTIST_WORK first), then read its output back here.")
        elif catalog is None:
            hint = (f"'{name}' is not available here, and the HPC3 module list could not be read: try "
                    f"find_software('{name}').")
        elif lab_find is not None:
            hint = (f"No HPC3 module or lab environment provides '{name}'. If it is a Python package, import it in "
                    f"run_code (the install is offered to the user). Otherwise {_REQUEST}, e.g. conda "
                    f"['{name.lower()}=<version>'] from bioconda, then pass environment=<id>. Do not hand-write a "
                    "replacement.")
        else:
            hint = (f"No HPC3 module provides '{name}'. If it is a Python package, import it in run_code "
                    "(the install is offered to the user). Otherwise there is no approved way to install it "
                    f"in this session yet: say that the step needs {name}, and do not hand-write a replacement.")
        notes.append({"name": name, "kind": kind, "modules": [m["load"] for m in matches], "hint": hint})
    return notes
