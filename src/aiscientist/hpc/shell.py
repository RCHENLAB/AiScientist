"""The agent's HPC3 filesystem + shell tools.

Before this, the Scientist catalog had **no ``ls``, no read, no find, no download**. The only
escape hatch was ``run_code`` — a Slurm *batch* job — so listing a directory cost minutes of queue
time. That is a plausible source of the model guessing at paths instead of looking at them.

**Placement is the safety property, and it is structural rather than guessed.** RCIC's rule is
that login nodes are for logging in and submitting jobs, so:

* **Typed metadata tools** (``list_dir``, ``stat_path``, ``find_files``, ``read_text``,
  ``disk_usage``) run on the **login node**. They are exactly the class the storage doc already
  calls acceptable there — cheap, read-only, user-initiated — and they answer in milliseconds
  with no allocation at all. A session that only ever looks around never costs a node.
* **Everything else** (``run_shell``, ``fetch_url``, ``install_package``) runs on the session's
  **held CPU worker** (see :mod:`aiscientist.gateway.worker`), allocated lazily on first use. There
  is no command allowlist to get wrong: the general shell simply never executes on a login node.

Two more fences, because a near-complete shell on a SHARED lab account is a real capability:

* **Path confinement.** Reads are confined to the user's own areas plus registered datasets and
  the shared read-only assets; writes to the user's own areas only. The lab account is shared, so
  "it's my account" is not the same as "it's my data".
* **HITL, not silent refusal.** When the agent needs to cross a line it raises a
  human-in-the-loop confirmation carrying the exact command, rather than failing opaquely or
  proceeding quietly. Yijun's instruction: give it a real shell, obey RCIC, and *ask* at the edge.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable

from ..tools.sdk import HarnessTool

# Byte caps. Generous enough for real files, small enough that one tool call cannot blow the
# model's context or drag a gigabyte through the SSH channel.
MAX_READ_BYTES = 64_000
MAX_ENTRIES = 500
MAX_OUTPUT_CHARS = 20_000

# Hosts the agent may download from WITHOUT asking. Reference data for this lab's work, all
# publicly served and all things a bioinformatician would fetch by hand. Anything else is a HITL
# prompt rather than a refusal — the point is that a human sees the URL, not that the list is
# exhaustive.
DEFAULT_FETCH_ALLOWLIST = (
    "ftp.ensembl.org", "ensembl.org", "ftp.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    "hgdownload.soe.ucsc.edu", "hgdownload.cse.ucsc.edu", "ftp.ebi.ac.uk", "ebi.ac.uk",
    "gnomad.broadinstitute.org", "storage.googleapis.com", "purl.obolibrary.org",
    "hpo.jax.org", "monarchinitiative.org", "github.com", "raw.githubusercontent.com",
    "files.pythonhosted.org", "pypi.org",
)

# Verbs that destroy or relocate data. Presence of one is enough to require confirmation: shell
# is not parseable well enough to prove a `rm` is harmless, and the cost of asking is one click.
#
# Deliberately NOT here: a redirection to an absolute path. Writing to an absolute path inside
# your own workspace is completely ordinary (`samtools sort in.bam > $TEMP/out.bam`), and
# treating it as dangerous would make the prompt fire on routine work — which trains people to
# approve without reading. Redirection targets are judged precisely by `_write_targets` +
# `can_write` instead, so an out-of-workspace write still asks.
_DESTRUCTIVE = re.compile(
    r"(?:^|[\s;&|(])(rm|rmdir|mv|dd|mkfs|shred|truncate|chown|chgrp)\b"
    r"|(?:^|[\s;&|(])chmod\s+-[a-zA-Z]*R", re.IGNORECASE)

# A write redirection anywhere in the command.
_REDIRECT = re.compile(r"(?<![0-9<>])>{1,2}(?!&)")

# Character devices that DISCARD or re-emit what is written to them. They are redirection targets
# constantly (`2>/dev/null` ends nearly every exploratory `find`), they hold no data, and nothing
# is destroyed by writing to them — so treating them as an out-of-workspace write is a pure false
# positive. It is not a cheap one: it blocks on a human confirmation, and one unanswered
# `find … 2>/dev/null` burned 600s of a production run before timing out into a refusal.
_DISCARD_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero"})


class HpcShellError(RuntimeError):
    """A tool-level failure with a message meant for the model to read and act on."""


@dataclass(frozen=True)
class ConfirmRequest:
    """What the user is being asked to approve, in terms they can judge."""

    kind: str          # 'write_outside' | 'destructive' | 'download' | 'install' | 'quota'
    summary: str       # one line, shown as the prompt
    detail: str        # the exact command / URL / path
    reversible: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "summary": self.summary, "detail": self.detail,
                "reversible": self.reversible}


@dataclass
class HpcWorkspace:
    """Where this session may read and write on HPC3.

    ``write_roots`` is deliberately a strict subset of ``read_roots``: the agent can consult the
    lab's shared reference data and the containers, but can only ever modify its own user's dirs.
    """

    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()

    def resolve(self, path: str, home: str = "") -> str:
        """Absolutise and normalise a path so containment can be decided on a string.

        Normalising FIRST is what makes the check meaningful: ``$HOME/../../etc/shadow`` is
        inside ``$HOME`` as text and outside it as a path, and only the normalised form tells
        the truth. Symlinks are handled separately (see ``_realpath_guard``).
        """
        p = (path or "").strip()
        if p.startswith("~"):
            p = home.rstrip("/") + p[1:] if home else p
        if not p.startswith("/"):
            p = posixpath.join(home or "/", p)
        return posixpath.normpath(p)

    def can_read(self, abs_path: str) -> bool:
        return _under_any(abs_path, self.read_roots)

    def can_write(self, abs_path: str) -> bool:
        return _under_any(abs_path, self.write_roots)


def _under_any(abs_path: str, roots: tuple[str, ...]) -> bool:
    for root in roots:
        r = posixpath.normpath(root or "")
        if not r or r == "/":
            continue
        if abs_path == r or abs_path.startswith(r.rstrip("/") + "/"):
            return True
    return False


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(truncated, {len(text) - limit} more characters)"


def _host_of(url: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)", url.strip())
    if not m:
        return ""
    return m.group(1).rsplit("@", 1)[-1].split(":")[0].lower()


def host_allowed(url: str, allowlist: tuple[str, ...]) -> bool:
    """Exact host or a subdomain of an allowlisted host.

    Suffix matching is anchored on a dot so ``evil-ensembl.org`` does not pass as
    ``ensembl.org`` — the classic way a naive ``endswith`` allowlist is defeated.
    """
    host = _host_of(url)
    if not host:
        return False
    return any(host == a or host.endswith("." + a) for a in allowlist)


@dataclass
class HpcShell:
    """Bound tool implementations for one session.

    ``worker_provider`` is a callable rather than a value so the CPU allocation is LAZY: a
    session that only inspects files never requests a node. ``confirm`` returning False means the
    user declined, which is reported to the model as a refusal it can plan around — not an error.
    """

    remote: Any                                   # RemoteExecutor (login node)
    workspace: HpcWorkspace
    worker_provider: Callable[[], Any] | None = None
    run_on_worker: Callable[[Any, str, int], Any] | None = None
    confirm: Callable[[ConfirmRequest], bool] | None = None
    emit: Callable[..., None] | None = None
    home: str = ""
    command_timeout_s: int = 900
    fetch_allowlist: tuple[str, ...] = DEFAULT_FETCH_ALLOWLIST
    max_fetch_mb: int = 2048
    package_cache: Any = None                     # SharedPackageCache | None
    _audit: list[dict] = field(default_factory=list)

    # --- plumbing ------------------------------------------------------------

    def _say(self, level: str, message: str) -> None:
        if self.emit:
            self.emit(level, "hpc_shell", message)

    def _ask(self, req: ConfirmRequest) -> bool:
        """Raise a HITL confirmation. With no confirm callback wired, the answer is NO.

        Denying by default is the only safe reading of a missing approver: a tool that silently
        proceeded when nobody could be asked would make the fence decorative in exactly the
        deployments (headless, cron) where it matters most.
        """
        self._audit.append({"confirm": req.as_dict()})
        if self.confirm is None:
            self._say("warning", f"Blocked ({req.kind}): nothing is wired to ask the user. {req.summary}")
            return False
        self._say("info", f"Waiting for your confirmation: {req.summary}")
        return bool(self.confirm(req))

    def _login(self, command: str, timeout: float = 60.0):
        """Run a METADATA-ONLY command on the login node. Never call this with anything that
        moves bytes or burns CPU — that is what :meth:`_worker` is for."""
        return self.remote.exec(command, timeout=timeout)

    def _worker(self, command: str, timeout_s: int | None = None):
        """Run on the session's held CPU node, allocating one on first use."""
        if self.worker_provider is None or self.run_on_worker is None:
            raise HpcShellError(
                "This action needs the session's CPU worker node, which is not enabled. "
                "Ask an operator to set AISCIENTIST_WORKER_NODE=1, or use a tool that only reads "
                "file metadata.")
        alloc = self.worker_provider()
        return self.run_on_worker(alloc, command, int(timeout_s or self.command_timeout_s))

    def _read_path(self, path: str) -> str:
        p = self.workspace.resolve(path, self.home)
        if not self.workspace.can_read(p):
            raise HpcShellError(
                f"{p} is outside the areas this session may read. Readable: "
                + ", ".join(self.workspace.read_roots))
        return p

    def _realpath_guard(self, abs_path: str, *, write: bool) -> str:
        """Re-check containment against the SYMLINK-RESOLVED path.

        String normalisation alone is not enough: a symlink inside the workspace can point
        anywhere, so a path that reads as contained can resolve outside it. Cheap (`readlink -f`)
        and it closes the one hole the textual check cannot see.
        """
        res = self._login(f"readlink -f {shlex.quote(abs_path)} 2>/dev/null || true", timeout=20)
        real = (getattr(res, "out", "") or "").strip() or abs_path
        ok = self.workspace.can_write(real) if write else self.workspace.can_read(real)
        if not ok:
            raise HpcShellError(
                f"{abs_path} resolves to {real}, which is outside the areas this session may "
                f"{'write' if write else 'read'}.")
        return real

    # --- metadata tools (login node) -----------------------------------------

    def list_dir(self, path: str, max_entries: int = 200) -> dict[str, Any]:
        p = self._read_path(path)
        self._realpath_guard(p, write=False)
        n = max(1, min(int(max_entries or 200), MAX_ENTRIES))
        # -A skips . and ..; the head cap bounds the payload before it ever reaches the model.
        res = self._login(
            f"ls -lAh --time-style=long-iso {shlex.quote(p)} 2>&1 | head -n {n + 1}", timeout=45)
        if getattr(res, "exit_status", 0) != 0 and "No such file" in (res.stdout + res.stderr):
            raise HpcShellError(f"{p} does not exist.")
        return {"path": p, "listing": _clip(res.stdout), "truncated_at": n,
                "note": "Login-node metadata read; no allocation used."}

    def stat_path(self, path: str) -> dict[str, Any]:
        p = self._read_path(path)
        res = self._login(
            f"stat -c '%n|%F|%s|%y|%U|%A' {shlex.quote(p)} 2>&1 || true", timeout=30)
        out = (getattr(res, "out", "") or "").strip()
        if not out or "|" not in out:
            return {"path": p, "exists": False}
        name, kind, size, mtime, owner, mode = (out.split("|") + [""] * 6)[:6]
        return {"path": name, "exists": True, "kind": kind, "size_bytes": int(size or 0),
                "modified": mtime, "owner": owner, "mode": mode}

    def find_files(self, root: str, pattern: str = "*", max_depth: int = 4,
                   max_results: int = 100) -> dict[str, Any]:
        """Bounded ``find``. ``-maxdepth`` and a result cap keep this in the metadata class the
        login node is for; an unbounded tree walk belongs on the worker (use ``run_shell``)."""
        p = self._read_path(root)
        depth = max(1, min(int(max_depth or 4), 8))
        n = max(1, min(int(max_results or 100), MAX_ENTRIES))
        res = self._login(
            f"find {shlex.quote(p)} -maxdepth {depth} -name {shlex.quote(pattern)} "
            f"-printf '%p\\t%s\\n' 2>/dev/null | head -n {n}", timeout=60)
        rows = [line for line in (getattr(res, "out", "") or "").splitlines() if line.strip()]
        return {"root": p, "pattern": pattern, "max_depth": depth,
                "matches": rows, "count": len(rows),
                "note": ("Result cap reached — narrow the pattern or search a subdirectory."
                         if len(rows) >= n else "")}

    def read_text(self, path: str, max_bytes: int = MAX_READ_BYTES,
                  tail: bool = False) -> dict[str, Any]:
        """Read a file's head or tail over SFTP — no shell involved at all.

        Going through the transfer channel rather than ``cat`` means reading a file is not a
        login-node process at all, and it reuses the executor's existing size/truncation handling.
        """
        p = self._read_path(path)
        self._realpath_guard(p, write=False)
        cap = max(1, min(int(max_bytes or MAX_READ_BYTES), MAX_READ_BYTES))
        try:
            size = int(self.remote.remote_size(p))
        except Exception as exc:  # noqa: BLE001 - report the path, not a stack trace
            raise HpcShellError(f"Could not stat {p}: {exc}") from exc

        if tail and size > cap:
            # tail needs a byte offset the SFTP helper does not expose, so this one case uses the
            # login node — still metadata-class work on a bounded read.
            res = self._login(f"tail -c {cap} {shlex.quote(p)}", timeout=60)
            text, truncated = res.stdout, True
        else:
            raw = self.remote.read_bytes(p, max_bytes=cap)
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            truncated = size > cap
        return {"path": p, "size_bytes": size, "truncated": truncated,
                "bytes_returned": min(size, cap), "text": _clip(text, cap)}

    def disk_usage(self, path: str = "") -> dict[str, Any]:
        p = self._read_path(path or self.home or "~")
        du = self._login(f"du -sh {shlex.quote(p)} 2>/dev/null || true", timeout=120)
        user = getattr(self.remote, "username", "") or ""
        quota = self._login(f"dfsquotas {shlex.quote(user)} dfs3b 2>/dev/null || true", timeout=60)
        return {"path": p, "used": (getattr(du, "out", "") or "").split("\t")[0],
                "quota_report": _clip((getattr(quota, "out", "") or ""), 2000),
                "note": "Lab dfs3b storage runs close to full; check the quota before large writes."}

    # --- worker-node tools ---------------------------------------------------

    def run_shell(self, command: str, timeout_s: int = 0,
                  workdir: str = "") -> dict[str, Any]:
        """Run a shell command on the session's CPU worker node.

        Always the worker, never the login node — that is what makes this safe to expose at all.
        Destructive verbs and write redirections raise a confirmation first, carrying the exact
        command so the person approving can read what they are approving.
        """
        cmd = (command or "").strip()
        if not cmd:
            raise HpcShellError("No command given.")

        risky = bool(_DESTRUCTIVE.search(cmd))
        writes = bool(_REDIRECT.search(cmd))
        outside = [t for t in _write_targets(cmd, self.workspace, self.home)
                   if not self.workspace.can_write(t)]
        if outside:
            if not self._ask(ConfirmRequest(
                    "write_outside",
                    f"Write outside your workspace: {', '.join(outside[:3])}",
                    cmd, reversible=False)):
                return {"status": "declined", "reason": "The user declined a write outside the workspace.",
                        "command": cmd}
        elif risky:
            if not self._ask(ConfirmRequest(
                    "destructive", "Run a command that can delete or move data", cmd,
                    reversible=False)):
                return {"status": "declined", "reason": "The user declined a destructive command.",
                        "command": cmd}
        elif writes:
            self._say("info", f"Writing to your workspace: {cmd[:120]}")

        cwd = self._read_path(workdir) if workdir else None
        res = self._worker(cmd, timeout_s or self.command_timeout_s)
        return {"status": "ok" if getattr(res, "exit_status", 1) == 0 else "failed",
                "exit_status": getattr(res, "exit_status", None),
                "stdout": _clip(getattr(res, "stdout", "")),
                "stderr": _clip(getattr(res, "stderr", ""), 4000),
                "ran_on": "worker_node", "workdir": cwd, "command": cmd}

    def fetch_url(self, url: str, dest_dir: str = "", max_mb: int = 0) -> dict[str, Any]:
        """Download a file **onto the worker node**, never over a login node.

        RCIC bans bulk transfer on login nodes, and this repo has already tripped that wire once
        (see the login-node transfer finding). Running the download as a step on a held
        allocation is what makes ``wget`` an ordinary operation again.
        """
        u = (url or "").strip()
        if not re.match(r"^https?://", u):
            raise HpcShellError("Only http:// and https:// URLs can be fetched.")

        dest = self._read_path(dest_dir) if dest_dir else self.workspace.resolve(
            posixpath.join(self.home or "~", "downloads"), self.home)
        if not self.workspace.can_write(dest):
            raise HpcShellError(f"{dest} is not a directory this session may write to.")

        cap_mb = max(1, int(max_mb or self.max_fetch_mb))
        if not host_allowed(u, self.fetch_allowlist):
            if not self._ask(ConfirmRequest(
                    "download",
                    f"Download from {_host_of(u) or 'an unrecognised host'} (not on the reference-data allowlist)",
                    u)):
                return {"status": "declined", "url": u,
                        "reason": "The user declined a download from a non-allowlisted host."}

        # --content-disposition keeps the server's filename; the cap is enforced by wget itself so
        # an over-large file is aborted mid-stream rather than after it has filled the quota.
        cmd = (f"mkdir -p {shlex.quote(dest)} && cd {shlex.quote(dest)} && "
               f"wget --no-verbose --content-disposition --timeout=60 --tries=2 "
               f"--quota={cap_mb}m {shlex.quote(u)} 2>&1")
        res = self._worker(cmd)
        ok = getattr(res, "exit_status", 1) == 0
        return {"status": "ok" if ok else "failed", "url": u, "dest_dir": dest,
                "ran_on": "worker_node", "quota_mb": cap_mb,
                "output": _clip(getattr(res, "stdout", "") + getattr(res, "stderr", ""), 4000)}

    def install_package(self, package: str, import_name: str = "") -> dict[str, Any]:
        if self.package_cache is None:
            raise HpcShellError("The shared package cache is not configured for this session.")
        return self.package_cache.ensure(self, package, import_name=import_name)

    def audit(self) -> list[dict]:
        """Every confirmation this session raised — for the technical report's Diagnostics."""
        return list(self._audit)


def _write_targets(command: str, ws: HpcWorkspace, home: str) -> list[str]:
    """Best-effort extraction of paths the command would WRITE to.

    Deliberately over-inclusive: it collects redirection targets and the arguments of destructive
    verbs, and a false positive costs one confirmation prompt while a false negative costs data.
    Shell is not parseable in general, so this makes no claim of completeness — it is the trigger
    for asking a human, not a proof of safety.
    """
    targets: list[str] = []
    for m in re.finditer(r">{1,2}\s*([^\s;&|)]+)", command):
        targets.append(m.group(1))
    for m in re.finditer(r"(?:^|[\s;&|(])(?:rm|rmdir|mv|cp|chmod|chown|truncate|shred|dd)\s+([^\n;&|]+)",
                         command, re.IGNORECASE):
        try:
            args = shlex.split(m.group(1))
        except ValueError:
            args = m.group(1).split()
        targets.extend(a for a in args if not a.startswith("-") and "=" not in a)
    out: list[str] = []
    for t in targets:
        t = t.strip().strip("'\"")
        if not t or t.startswith("$"):     # an unexpanded variable tells us nothing
            continue
        if t in _DISCARD_DEVICES:          # writing to /dev/null destroys nothing — never ask
            continue
        out.append(ws.resolve(t, home))
    return out


# --- tool factories ----------------------------------------------------------


def _ok(fn):
    """Turn an HpcShellError into a structured tool result instead of a traceback, so the model
    reads the reason and can choose a different path."""
    def wrapped(args, ctx):
        try:
            return fn(args, ctx)
        except HpcShellError as exc:
            return {"status": "error", "error": str(exc)}
    return wrapped


def hpc_shell_catalog(shell: HpcShell | None) -> list[HarnessTool]:
    """The filesystem/shell toolset for one session, or ``[]`` when no HPC session is bound.

    Returning an empty catalog (rather than tools that fail at call time) keeps a local/offline
    run's tool roster honest — the model is never offered a tool that cannot work.
    """
    if shell is None:
        return []

    def _str(a, k, d=""):
        v = a.get(k, d)
        return v if isinstance(v, str) else ("" if v is None else str(v))

    def _int(a, k, d=0):
        try:
            return int(a.get(k, d) or d)
        except (TypeError, ValueError):
            return d

    return [
        HarnessTool(
            name="list_dir",
            description=("List a directory on HPC3 with sizes and modification times. Fast "
                         "(login-node metadata read, no Slurm queue). Use this to find out what "
                         "files actually exist before assuming a path."),
            parameters={"type": "object", "properties": {
                "path": {"type": "string", "description": "Absolute path or ~-relative path on HPC3."},
                "max_entries": {"type": "integer", "description": "Cap on entries returned (default 200)."},
            }, "required": ["path"]},
            executor=_ok(lambda a, _c: shell.list_dir(_str(a, "path"), _int(a, "max_entries", 200))),
            category="filesystem",
        ),
        HarnessTool(
            name="stat_path",
            description="Does a path exist on HPC3, and is it a file or a directory? Returns size, owner, mode, mtime.",
            parameters={"type": "object", "properties": {
                "path": {"type": "string"}}, "required": ["path"]},
            executor=_ok(lambda a, _c: shell.stat_path(_str(a, "path"))),
            category="filesystem",
        ),
        HarnessTool(
            name="find_files",
            description=("Find files by name pattern under a directory on HPC3, bounded by depth "
                         "and result count. Use it to locate VCFs, h5ad files, or sample sheets "
                         "without guessing paths."),
            parameters={"type": "object", "properties": {
                "root": {"type": "string"},
                "pattern": {"type": "string", "description": "Glob such as '*.vcf.gz' (default '*')."},
                "max_depth": {"type": "integer", "description": "1-8, default 4."},
                "max_results": {"type": "integer", "description": "Default 100."},
            }, "required": ["root"]},
            executor=_ok(lambda a, _c: shell.find_files(
                _str(a, "root"), _str(a, "pattern", "*") or "*", _int(a, "max_depth", 4),
                _int(a, "max_results", 100))),
            category="filesystem",
        ),
        HarnessTool(
            name="read_text",
            description=("Read the head (or tail) of a text file on HPC3 — a VCF header, a log, a "
                         "sample sheet, a README. Bounded in bytes; not for large matrices."),
            parameters={"type": "object", "properties": {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "description": f"Default and maximum {MAX_READ_BYTES}."},
                "tail": {"type": "boolean", "description": "Read the END of the file instead of the start."},
            }, "required": ["path"]},
            executor=_ok(lambda a, _c: shell.read_text(
                _str(a, "path"), _int(a, "max_bytes", MAX_READ_BYTES), bool(a.get("tail")))),
            reads_private_data=True,
            category="filesystem",
        ),
        HarnessTool(
            name="disk_usage",
            description=("How much space a directory uses, plus this user's dfs3b quota. Check "
                         "before writing large outputs — the lab's storage runs close to full."),
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            executor=_ok(lambda a, _c: shell.disk_usage(_str(a, "path"))),
            category="filesystem",
        ),
        HarnessTool(
            name="run_shell",
            description=("Run a shell command on this session's dedicated HPC3 compute node "
                         "(NOT a login node, and no Slurm queue wait). Use it for real work: "
                         "decompressing, grepping across files, bcftools/samtools, checksums. "
                         "Commands that delete or move data, or write outside your workspace, "
                         "ask the user for confirmation first."),
            parameters={"type": "object", "properties": {
                "command": {"type": "string", "description": "The shell command line."},
                "workdir": {"type": "string", "description": "Optional directory to run in."},
                "timeout_s": {"type": "integer", "description": "Per-command wall limit."},
            }, "required": ["command"]},
            executor=_ok(lambda a, _c: shell.run_shell(
                _str(a, "command"), _int(a, "timeout_s", 0), _str(a, "workdir"))),
            reads_private_data=True,
            category="shell",
            requires=("hpc_worker",),
        ),
        HarnessTool(
            name="fetch_url",
            description=("Download a file to HPC3 (reference genomes, annotation tracks, gene "
                         "panels). Runs on the compute node, never a login node. Well-known "
                         "reference hosts download directly; anything else asks the user first."),
            parameters={"type": "object", "properties": {
                "url": {"type": "string"},
                "dest_dir": {"type": "string", "description": "Where to put it (defaults to ~/downloads)."},
                "max_mb": {"type": "integer", "description": "Abort past this size."},
            }, "required": ["url"]},
            executor=_ok(lambda a, _c: shell.fetch_url(
                _str(a, "url"), _str(a, "dest_dir"), _int(a, "max_mb", 0))),
            category="shell",
            requires=("hpc_worker",),
        ),
        HarnessTool(
            name="install_package",
            description=("Install a missing Python package so later run_code snippets can import "
                         "it. Installs go to a LAB-SHARED cache, so a package someone already "
                         "installed is available instantly and is never downloaded twice. Call "
                         "this when an import fails — do not vendor or reimplement the library."),
            parameters={"type": "object", "properties": {
                "package": {"type": "string", "description": "A pip requirement, e.g. 'pyranges' or 'scikit-bio==0.6.0'."},
                "import_name": {"type": "string", "description": "Module name to verify, if it differs from the package name."},
            }, "required": ["package"]},
            executor=_ok(lambda a, _c: shell.install_package(_str(a, "package"), _str(a, "import_name"))),
            category="shell",
            requires=("hpc_worker",),
        ),
    ]
