"""Application-owned bindings and deliverables for the seven analysis MCP services.

All paths admitted to a call come from the run's attachments or administrator
settings. The collector is stdlib-only Python executed inside the pinned image.
"""
from __future__ import annotations

import inspect
import os
from pathlib import PurePosixPath
import re
import shlex
from typing import Any
from urllib.parse import urlsplit

from .mcp_bridge import PreparedCall, READ_RESULT
from .mcp_result_summary import project_result

PREFIXES = {
    "cellqc": "CELLQC", "scrna-analysis": "SCRNA", "scgpt-annotation": "SCGPT",
    "variant-annotation": "VARIANT", "phenotype-diagnosis": "PHENOTYPE",
    "literature": "LITERATURE", "data-utilities": "DATA_UTILITIES",
}
# Settings are names, never registry values. Credentials continue through skill
# policy; model arguments cannot select environment variables or executables.
_PATHS = {
    "cellqc": set(),
    "scrna-analysis": {"SCRNA_GENESETS_DIR"},
    "scgpt-annotation": {"SCGPT_MODEL_DIR", "SCGPT_REFACTOR"},
    "variant-annotation": {"VARIANT_" + key + suffix for key in (
        "VEP_CACHE_DIR", "VEP_CLINVAR", "REF_FASTA", "VEP_CADD_SNV", "VEP_CADD_INDELS",
        "VEP_ALPHAMISSENSE", "VEP_REVEL", "VEP_PLUGINS_DIR", "SPLICEAI_MODELS",
        "IRD_HGMD", "IRD_RETINA_EXONS", "IRD_ATAC", "IRD_DBSCSNV")
        for suffix in ("", "_GRCH37", "_GRCH38")} | {"VARIANT_DEFAULT_REGIONS_BED", "VARIANT_SPLICEAI_BIN"},
    "phenotype-diagnosis": {"LIRICAL_DATA_DIR", "LIRICAL_EXOMISER_HG19", "LIRICAL_EXOMISER_HG38",
                            "PHENOTYPE_HPO_LEXICON"},
    "literature": {"LITERATURE_CORPUS_ROOT", "LITERATURE_PAPERS", "LITERATURE_CACHE_DIR",
                   "LITERATURE_MANIFEST", "LITERATURE_HGNC", "LITERATURE_HF_CACHE", "LITERATURE_INDEX_DIR"},
    "data-utilities": set(),
}
_VALUES = {
    "cellqc": set(), "scrna-analysis": set(), "scgpt-annotation": set(),
    "variant-annotation": {"VARIANT_ALLOW_REST", "VARIANT_VEP_FORK", "VARIANT_SPLICEAI_MAX_VARIANTS",
                           "VARIANT_SPLICEAI", "VARIANT_IRD_ANNOTATE", "VARIANT_VEP_PLUGINS",
                           "VARIANT_DEFAULT_GENE_PANEL", "VARIANT_DEFAULT_MAX_POP_AF"},
    "phenotype-diagnosis": {"LIRICAL_ASSEMBLY", "LIRICAL_TIMEOUT", "PHENOTYPE_MODEL_ENDPOINT",
                            "PHENOTYPE_MODEL", "PHENOTYPE_MODEL_TIMEOUT", "PHENOTYPE_MODEL_MAX_TOKENS",
                            "PHENOTYPE_LITERATURE_TIMEOUT"},
    "literature": {"LITERATURE_MODEL_ENDPOINT", "LITERATURE_MODEL", "LITERATURE_MODEL_TIMEOUT",
                   "LITERATURE_EMBEDDING", "LITERATURE_INDEX_NAME", "LITERATURE_REASONING_EFFORT",
                   "LITERATURE_SUMMARY_REASONING_EFFORT", "LITERATURE_ANSWER_LENGTH", "LITERATURE_MODEL_MAX_TOKENS",
                   "LITERATURE_EVIDENCE_K", "LITERATURE_MAX_SOURCES", "LITERATURE_SEARCH_COUNT", "LITERATURE_CONCURRENCY",
                   "LITERATURE_TEMPERATURE", "LITERATURE_LLM_TIMEOUT", "LITERATURE_AGENT_TIMEOUT"},
    "data-utilities": {"DATA_UTILITIES_MODEL_ENDPOINT", "DATA_UTILITIES_MODEL", "DATA_UTILITIES_MODEL_TIMEOUT"},
}

# The bundled phenotype image runs a separate literature MCP process with its
# own namespace. Only data/model settings cross this boundary, never commands.
_PATHS["phenotype-diagnosis"] |= _PATHS["literature"]
_VALUES["phenotype-diagnosis"] |= _VALUES["literature"]

def validate_settings(server: str, names: Any) -> None:
    if (not isinstance(names, (list, tuple)) or any(not isinstance(v, str) for v in names)
            or len(set(names)) != len(names)
            or set(names) - (_PATHS.get(server, set()) | _VALUES.get(server, set()))):
        raise ValueError("MCP settings_env must list approved nonsecret settings for this server")


def setting_values(server) -> dict[str, str]:
    validate_settings(server.name, server.settings_env)
    defaults = {}
    if server.name == "variant-annotation":
        from .variant_settings import deployment_values
        defaults = deployment_values()
    elif server.name == "scgpt-annotation":
        from ..gateway.settings import HPCSettings
        defaults = {"SCGPT_MODEL_DIR": HPCSettings.from_env().scgpt_model_dir}
    elif server.name == "scrna-analysis":
        # Only an explicit administrator setting carries over. Never infer an
        # Eye Server package path as a path available on the compute host.
        defaults = {"SCRNA_GENESETS_DIR": os.environ.get("AISCIENTIST_GENESETS_DIR", "")}
    values = {}
    for name in server.settings_env:
        value = os.environ.get(name, defaults.get(name, ""))
        if not value:
            if server.name == "scrna-analysis" and name in os.environ:
                values[name] = ""  # explicit native setting disables the legacy fallback
            if server.name == "variant-annotation" and (name in defaults or name in os.environ):
                values[name] = ""  # an explicitly empty assembly-specific setting blocks base fallback
            continue
        if any(ch in value for ch in ("\x00", "\n", "\r")):
            raise ValueError(f"Invalid deployment setting {name}")
        if name in _PATHS[server.name]:
            if not value.startswith("/") or ":" in value or ".." in PurePosixPath(value).parts:
                raise ValueError(f"{name} must be an absolute deployment path")
        if name.endswith("_ENDPOINT"):
            parsed = urlsplit(value)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment):
                raise ValueError(f"{name} must be an HTTP endpoint without credentials or query parameters")
        values[name] = value
    return values


def setting_paths(server) -> tuple[str, ...]:
    """Trusted read-only mounts for the executor, including reference sidecars.

    Bind the parent so files, directories and chromosome templates all work on
    a remote execution host without probing its filesystem on the gateway.
    """
    paths = []
    for name, value in setting_values(server).items():
        if name in _PATHS[server.name] and value:
            parent = str(PurePosixPath(value).parent)
            if name == "VARIANT_SPLICEAI_BIN":
                # Administrator-managed SpliceAI needs its bin and lib trees;
                # model arguments cannot select this executable or mount.
                parent = str(PurePosixPath(value).parent.parent)
            if parent == "/":
                raise ValueError(f"{name} cannot require mounting the filesystem root")
            paths.append(parent)
    return tuple(dict.fromkeys(paths))


# The values in AISCIENTIST_* are executor-owned and already mounted. File roots
# remain exact files; they never grant access to siblings in an upload directory.
BOUND_SETUP = r'''AISCI_MCP_READ_ROOTS=$(python - <<'AISCI_MCP_ROOTS'
import json, os
roots = [os.environ.get(name, "") for name in ("AISCIENTIST_DATASET_ROOT", "AISCIENTIST_DATASET")]
roots += [row["path"] for row in json.loads(os.environ.get("AISCIENTIST_INPUTS", "[]")) if row.get("path")]
if any(":" in value or "\n" in value or "\r" in value for value in roots):
    raise ValueError("attachment path cannot contain a root-list separator")
print(":".join(dict.fromkeys(value for value in roots if value)))
AISCI_MCP_ROOTS
) || exit $?
export AISCI_MCP_READ_ROOTS
'''

# A path token begins at a string or after an opening delimiter, not after
# arbitrary punctuation in scientific names (for example "(LCA3)/Retinitis").
# A standalone slash is prose, not a path. Exact secrets and known output roots
# are handled separately before this final unrecognized-path filter.
_PATH_TOKEN = r'''[^\s"'<>{},;)\]]+'''
_PATH_ROOTS = r"(?:dfs[0-9][A-Za-z0-9]*|data|home|Users|private|tmp|var|opt|etc|usr|mnt|scratch|srv|run|proc|sys|root|work)"
_SERVER_PATH_PATTERN = (
    # An absolute value, or a quoted/assigned path, provides direct path evidence.
    rf'''(?:^|(?<=["'=]))/(?!/){_PATH_TOKEN}'''
    # In prose require a known filesystem root or multiple path segments.
    # "phenotype /genotype" and "(condition) / comparison" remain scientific text.
    rf'''|(?<![^\s:(\[])/(?!/)(?:{_PATH_ROOTS}(?=/|$|[\s"'<>{{}},;)\]])(?:/{_PATH_TOKEN})?|[^/\s"'<>{{}},;)\]]+/{_PATH_TOKEN})'''
)

COLLECT = (READ_RESULT + "\n" + inspect.getsource(project_result)
           + "\nSERVER_PATH_PATTERN = " + repr(_SERVER_PATH_PATTERN)) + r'''
import fcntl, hashlib, re, shutil, stat, tempfile
server = os.environ["AISCI_MCP_SERVER"]
analysis = os.environ["AISCI_MCP_ANALYSIS"]
wire_tool = os.environ["AISCI_MCP_TOOL"]
status_call = wire_tool.endswith("_job_status")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", analysis):
    raise ValueError("invalid analysis identifier")
if result.get("analysis_id", analysis) != analysis:
    raise ValueError("server returned another analysis identifier")
requested_task = os.environ.get("AISCI_MCP_REQUEST_TASK", "")
if status_call and result.get("task_id") != requested_task:
    raise ValueError("server returned another task identifier")
returned_task = result.get("task_id", "")
if returned_task and (not isinstance(returned_task, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", returned_task)):
    raise ValueError("server returned an invalid task identifier")
def configured_root(name):
    root = Path(os.environ[name])
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("configured output root must be an absolute non-symlink directory")
    return root.resolve()
data = configured_root("AISCI_DATA_DIR")
base = data / server / analysis
artifacts = configured_root("AISCIENTIST_ARTIFACTS")
work = configured_root("AISCIENTIST_WORK")
relative = Path("mcp") / server / analysis
dest = artifacts / relative

def checked(path, root, directory=False, exists=True):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("server output path must be absolute")
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise ValueError("server output path is outside its analysis directory") from None
    if any(part in {".", ".."} for part in parts):
        raise ValueError("output path traversal is forbidden")
    current = root
    if root.is_symlink():
        raise ValueError("output root is a symlink")
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("output symlinks are forbidden")
    if path.resolve().is_relative_to(root.resolve()) is False:
        raise ValueError("output path escapes its root")
    if exists and (not path.is_dir() if directory else not path.is_file()):
        raise ValueError("server output is missing or not a regular file/directory")
    return path

# Check ancestors too: a symlinked server/analysis must never redirect copying.
checked(base, data, directory=True, exists=False)
checked(dest, artifacts, directory=True, exists=False)
operations = json.loads(os.environ.get("AISCI_MCP_OPERATIONS", "[]"))
operation = wire_tool if not status_call and wire_tool in operations else None
if status_call:
    # The payload's free-form `tool` field is scientific data, never authority
    # to select an operation alias. Read the confined native request instead.
    request_file = checked(base / "jobs" / requested_task / "request.json", base, exists=False)
    if request_file.is_file():
        request_record = json.loads(request_file.read_text())
        if not isinstance(request_record, dict):
            raise ValueError("native request metadata must be a JSON object")
        declared = request_record.get("operation", request_record.get("tool"))
        if declared is None and server == "cellqc":
            declared = "run_cellqc"
        if declared in operations:
            operation = declared
if not work.is_dir():
    raise ValueError("run checkpoint directory is unavailable")
if os.environ.get("AISCI_MCP_CASE_NOTE") == "1" and "text_source" in result:
    result["text_source"] = "attached_case_note"
failed = (str(result.get("status", "ok")).lower() not in {"ok", "empty", "success"}
          or str(result.get("state", "")).upper() in {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}
          or result.get("isError") is True or result.get("is_error") is True)
if failed:
    result["status"] = "error"
    result.setdefault("error", "MCP analysis did not complete successfully")
successful = (not failed and result.get("status") in {"ok", "empty", "success"}
              and str(result.get("state", "")).upper() in {"", "COMPLETED"})

# Validate the complete copy plan before creating any deliverable. Never follow
# symlinks, including ones that happen to point back inside the analysis.
copies, mapping, file_aliases = [], {}, {}
artifact_files = {}
source_root = result.get("artifact_root")
if source_root:
    source_root = checked(source_root, base, directory=True)
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise ValueError("artifact tree contains a symlink")
        if path.is_dir():
            continue
        checked(path, base)
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("artifact must be a regular file")
        target = dest / path.relative_to(source_root)
        checked(target, artifacts, exists=False)
        copies.append((path, target))
        rel = path.relative_to(source_root).as_posix()
        artifact_files[rel] = path
        file_aliases[rel] = (relative / rel).as_posix()
        file_aliases["artifacts/" + rel] = (relative / rel).as_posix()
    mapping[str(source_root)] = relative.as_posix()

# Existing report builders read these canonical paths. Keep the namespaced
# copy authoritative and publish only the current successful analysis there.
# Ownership plus checksums allow a rerun to retire its own stale aliases without
# deleting a later tool's output or a file the user has since edited.
compat_path = checked(artifacts / "mcp" / ".compatibility.json", artifacts, exists=False)
compat_lock = checked(work / ".mcp-artifact.lock", work, exists=False).open("a")
fcntl.flock(compat_lock, fcntl.LOCK_EX)
compat = json.loads(compat_path.read_text()) if compat_path.is_file() else {"files": {}, "current": {}}
if not isinstance(compat, dict) or not isinstance(compat.get("files"), dict) or not isinstance(compat.get("current"), dict):
    raise ValueError("invalid artifact compatibility record")
checkpoint_owners = compat.setdefault("checkpoints", {})
if not isinstance(checkpoint_owners, dict):
    raise ValueError("invalid checkpoint compatibility record")
evidence_owners = compat.setdefault("evidence", {})
if not isinstance(evidence_owners, dict):
    raise ValueError("invalid evidence compatibility record")
identity = {"analysis_id": analysis, "task_id": result.get("task_id", "")}
previous = compat["current"].get(server)
handoff = successful and (not os.environ["AISCI_MCP_TOOL"].endswith("_job_status")
                         or previous is None or previous == identity)
publish = handoff and source_root is not None
def alias_name(value):
    p = Path(value)
    return (not p.is_absolute() and len(p.parts) > 1
            and p.parts[0] in {"tables", "figures", "data", "result"}
            and all(part not in {".", ".."} and not part.startswith(".")
                    and not re.search(r"[\\\x00-\x1f]", part) for part in p.parts))
def checksum(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()
aliases = {}
if publish:
    for rel, source in artifact_files.items():
        if alias_name(rel) and (not rel.startswith("result/") or server == "cellqc"):
            if rel == "data/cellqc_result.json":
                continue  # Built below from the actual complete output tree.
            # Other servers cannot replace CellQC's gateway pointer.
            target = checked(artifacts / rel, artifacts, exists=False)
            copies.append((source, target))
            aliases[rel] = target

# Complete matrices belong in the run's result/ tree, outside the automatically
# downloaded artifact bundle. The source pointer grants no authority: inventory
# every regular file ourselves and derive its published path from trusted roots.
full_tree, full_target, cellqc_pointer = [], None, None
if publish and server == "cellqc" and result.get("result_directory"):
    source = checked(result["result_directory"], base, directory=True)
    if work.parent != artifacts.parent:
        raise ValueError("run work and artifact roots must share their result directory")
    run_root = artifacts.parent
    full_target = checked(run_root / "result" / "cellqc", run_root, directory=True, exists=False)
    for path in sorted(source.rglob("*")):
        checked(path, base, directory=path.is_dir())
        if path.is_dir():
            continue
        name = path.relative_to(source).as_posix()
        if (not stat.S_ISREG(path.stat().st_mode)
                or any(part.startswith(".") or re.search(r"[\\\x00-\x1f]", part) for part in Path(name).parts)):
            raise ValueError("CellQC results must contain regular files with safe relative names")
        full_tree.append((path, name, path.stat().st_size))
    if not full_tree:
        raise ValueError("CellQC complete result directory is empty")
    source_pointer = artifact_files.get("data/cellqc_result.json")
    metadata = json.loads(source_pointer.read_text()) if source_pointer else {}
    if not isinstance(metadata, dict):
        raise ValueError("CellQC result metadata must be an object")
    cellqc_pointer = {
        "path": str(full_target),
        "files": [{"name": name, "size": size} for _, name, size in full_tree],
        "size_bytes": sum(size for _, _, size in full_tree),
        "libraries": metadata.get("libraries", []),
        "matrices": [name for _, name, _ in full_tree if name.endswith(".h5ad")],
        "in_bundle": "result/",
        "not_in_bundle": [name for _, name, _ in full_tree if "result/" + name not in artifact_files],
        "created_at": metadata.get("created_at"), "saved_to": None,
        "mcp_provenance": {"server": server, **identity},
    }
    target = checked(artifacts / "data" / "cellqc_result.json", artifacts, exists=False)
    aliases["data/cellqc_result.json"] = target
    mapping[str(source)] = "result/cellqc"

stale = []
if publish:
    for rel, owner in compat["files"].items():
        if not alias_name(rel) or not isinstance(owner, dict):
            raise ValueError("invalid compatibility artifact name or owner")
        target = checked(artifacts / rel, artifacts, exists=False)
        if owner.get("server") == server and rel not in aliases:
            if target.is_file() and checksum(target) == owner.get("sha256"):
                stale.append(target)

checkpoints = []
primary = result.get("checkpoint_path")
if primary and handoff:
    primary = checked(primary, base)
    checkpoints.append(primary)
    values = result.get("checkpoints", {})
    if not isinstance(values, dict):
        raise ValueError("checkpoints must be an object")
    for value in values.values():
        name = value.get("checkpoint") if isinstance(value, dict) else value
        if name:
            candidate = Path(name)
            checkpoints.append(checked(candidate if candidate.is_absolute() else primary.parent / candidate, base))
for source in checkpoints:
    if not re.fullmatch(r"adata_[A-Za-z0-9_.-]+\.h5ad", source.name):
        raise ValueError("checkpoint must have an approved AnnData filename")
    target = checked(work / source.name, work, exists=False)
    copies.append((source, target))
    mapping[str(source)] = source.name
stale_checkpoints = []
if checkpoints and server == "cellqc":
    current_names = {path.name for path in checkpoints}
    for name, owner in checkpoint_owners.items():
        if not re.fullmatch(r"adata_[A-Za-z0-9_.-]+\.h5ad", name) or not isinstance(owner, dict):
            raise ValueError("invalid checkpoint compatibility owner")
        target = checked(work / name, work, exists=False)
        if owner.get("server") == server and name not in current_names:
            if target.is_file() and checksum(target) == owner.get("sha256"):
                stale_checkpoints.append(target)
# A server may bound its own response before transport. Preserve that full JSON
# too, but only if it is a regular JSON file inside this analysis.
full_server = None
source_result_sha256 = None
if result.get("full_result"):
    source = checked(result["full_result"], base)
    if source.suffix != ".json":
        raise ValueError("full_result must identify JSON evidence")
    full_server = json.loads(source.read_text())
    if not isinstance(full_server, dict):
        raise ValueError("full_result must contain a JSON object")
    source_result_sha256 = checksum(source)

dest.mkdir(parents=True, exist_ok=True)
for source, target in copies:
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement also avoids overwriting an existing hardlink's inode.
    fd, temp = tempfile.mkstemp(prefix=".mcp-copy-", dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temp)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

if full_target is not None:
    full_target.parent.mkdir(parents=True, exist_ok=True)
    # work/ and result/ are separate Apptainer bind mounts. Both publication
    # renames must stay inside result/, even when their host files share a disk.
    # stage_out refuses these internal directories if publication is interrupted.
    staging = Path(tempfile.mkdtemp(prefix=".mcp-cellqc-", dir=full_target.parent))
    backup = None
    try:
        for source, name, _ in full_tree:
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        if full_target.exists():
            backup = Path(tempfile.mkdtemp(prefix=".mcp-cellqc-old-", dir=full_target.parent))
            backup.rmdir()
            os.replace(full_target, backup)
        try:
            os.replace(staging, full_target)
        except BaseException:
            if backup is not None:
                os.replace(backup, full_target)
                backup = None
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup is not None:
            shutil.rmtree(backup)

secret_names = json.loads(os.environ.get("AISCI_MCP_SECRET_NAMES", "[]"))
secrets = [os.environ.get(name, "") for name in secret_names]
known = [(os.environ.get(name, ""), replacement) for name, replacement in (
    ("AISCIENTIST_DATASET", "[attached dataset]"), ("AISCIENTIST_DATASET_ROOT", "[attached folder]"),
    ("AISCIENTIST_WORK", "[run work]"), ("AISCIENTIST_ENV_DIR", "[server work]"))]
known += [(row.get("path", ""), "[attached input]")
          for row in json.loads(os.environ.get("AISCIENTIST_INPUTS", "[]"))]
known += list(mapping.items())
known.sort(key=lambda item: len(item[0]), reverse=True)
def clean(value):
    if isinstance(value, dict):
        return {str(clean(k)): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str):
        if value in file_aliases:
            return file_aliases[value]
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        for path, replacement in known:
            if path:
                value = value.replace(path, replacement)
        # Absolute paths elsewhere in server messages/settings are evidence
        # metadata, not permission to expose or copy that host location.
        value = re.sub(SERVER_PATH_PATTERN, "[server path]", value)
    return value
result = clean(result)
def write_json(target, value):
    checked(target, artifacts, exists=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".mcp-json-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
if cellqc_pointer is not None:
    # Only the gateway's internal pointer keeps this verified run-local path.
    # The public MCP result/evidence use a relative path and sanitized metadata.
    public_pointer = clean(cellqc_pointer)
    public_pointer["path"] = "result/cellqc"
    cellqc_pointer = {**public_pointer, "path": str(full_target)}
    write_json(aliases["data/cellqc_result.json"], cellqc_pointer)
    write_json(dest / "data" / "cellqc_result.json", public_pointer)
    result["cellqc_result"] = public_pointer
    result["result_directory"] = "result/cellqc"
elif server == "cellqc" and "data/cellqc_result.json" in artifact_files:
    # Historical/failed evidence must not expose its original worker directory.
    pointer = json.loads(artifact_files["data/cellqc_result.json"].read_text())
    write_json(dest / "data" / "cellqc_result.json", clean(pointer))
if publish:
    for target in stale:
        target.unlink()
    compat["files"] = {rel: owner for rel, owner in compat["files"].items()
                       if owner.get("server") != server}
    compat["files"].update({rel: {"server": server, **identity, "sha256": checksum(target)}
                            for rel, target in aliases.items()})
    compat["current"][server] = identity
    if checkpoints:
        for target in stale_checkpoints:
            target.unlink()
            checkpoint_owners.pop(target.name, None)
        checkpoint_owners.update({source.name: {"server": server, **identity, "sha256": checksum(work / source.name)}
                                  for source in checkpoints})
    write_json(compat_path, compat)
if checkpoints:
    result["checkpoint_path"] = Path(primary).name
    result["checkpoint"] = Path(primary).name
    result["exported_checkpoints"] = list(dict.fromkeys(path.name for path in checkpoints))
if full_server is not None:
    # The server may already have truncated its transport preview. Project the
    # confined full scientific result, not that preview's first few citations.
    # Live status/identity wins over a stored result from an earlier attempt.
    envelope = {key: result[key] for key in (
        "status", "state", "analysis_id", "task_id", "job_id", "elapsed_s", "note", "error",
        "isError", "is_error", "checkpoint", "checkpoint_path", "exported_checkpoints",
        "cellqc_result", "result_directory") if key in result}
    result = {**clean(full_server), **envelope, "source_result_sha256": source_result_sha256}
    result.pop("response_omitted", None)
full_name = ((Path("tasks") / returned_task / (wire_tool + ".json")).as_posix()
             if returned_task else wire_tool + ".json")
full = dest / full_name
checked(full, artifacts, exists=False)
write_json(full, result)
evidence_owner = {"server": server, **identity, "operation": operation, "sha256": checksum(full)}
evidence_owners[(relative / full_name).as_posix()] = evidence_owner
if operation is not None and successful:
    canonical_rel = (relative / (operation + ".json")).as_posix()
    canonical = checked(artifacts / canonical_rel, artifacts, exists=False)
    owner = evidence_owners.get(canonical_rel)
    # A new scientific call can advance its managed operation alias; a status
    # call can only refresh the same task. Untracked legacy files stay intact.
    tracked = (isinstance(owner, dict) and canonical.is_file()
               and checksum(canonical) == owner.get("sha256")
               and all(owner.get(key) == evidence_owner[key] for key in ("server", "analysis_id", "operation")))
    if (not canonical.exists() or (tracked and (not status_call or owner.get("task_id") == returned_task))):
        write_json(canonical, result)
        evidence_owners[canonical_rel] = {**evidence_owner, "sha256": checksum(canonical)}
write_json(compat_path, compat)
result["full_result"] = (relative / full_name).as_posix()
if len(json.dumps(result)) > 14000:
    result = project_result(server, result)
    result["response_omitted"] = True
    if failed:
        result["error"] = "MCP analysis failed; inspect full_result for the complete evidence"
print(json.dumps({"result": result}, separators=(",", ":")))
'''


class AnalysisAdapter:
    description = (
        " Uses this run's bound inputs; host paths and reference/model settings cannot be supplied. "
        "The outer executor waits for completion. Deliverables are under mcp/<server>/<analysis_id>/; "
        "AnnData checkpoints enter the run's work directory for subsequent typed tools. "
        "After re-running upstream QC, start a new scRNA analysis_id; changed imported checkpoints "
        "are refused so an existing analysis cannot silently reuse stale results.")

    def __init__(self, server):
        if server.name not in PREFIXES:
            raise ValueError("Unknown analysis MCP server")
        self.server = server

    def authoritative(self, args: dict, ctx: Any, tool: str, executor: Any) -> dict:
        args = dict(args)
        decisions = getattr(ctx, "decisions", None) or {}
        if self.server.name == "phenotype-diagnosis":
            if tool == "map_phenotype_to_hpo" and decisions.get("case_note"):
                args["text"] = str(decisions["case_note"]).strip()
            elif tool in {"run_lirical", "diagnose_disease"} and not args.get("vcf_path"):
                dataset = str(getattr(executor, "dataset_path", "") or "")
                if dataset.lower().endswith((".vcf", ".vcf.gz")):
                    args["vcf_path"] = dataset
        if self.server.name == "variant-annotation" and tool == "annotate_variants":
            from .variant_settings import default_arguments
            args = {**default_arguments(setting_values(self.server), args), **args}
            assembly = decisions.get("mcp_variant_assembly")
            if assembly in {"GRCh37", "GRCh38"}:
                args["assembly"] = assembly
            if args.get("mode", "offline") == "offline":
                args["max_variants"] = 0
        return args

    def prepare(self, args: dict, executor: Any, *, ctx: Any = None, tool: str = "") -> PreparedCall:
        import json
        from .skills import get_skill

        args = dict(args)
        analysis = args.get("analysis_id", "default")
        if not isinstance(analysis, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", analysis):
            raise ValueError("analysis_id must be a simple identifier")
        task = args.get("task_id", "") if tool.endswith("_job_status") else ""
        if tool.endswith("_job_status") and (not isinstance(task, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task)):
            raise ValueError("task_id must be a simple identifier")
        dataset = str(getattr(executor, "dataset_path", "") or "")
        root = str(getattr(executor, "dataset_root", "") or "")
        inputs = [row for row in (getattr(executor, "inputs", ()) or ()) if isinstance(row, dict) and row.get("path")]
        attached = [(dataset, dataset), (str(getattr(executor, "dataset_source", "") or ""), dataset)]
        if root:
            attached.append((root, root))
        for row in inputs:
            path = str(row["path"])
            attached.extend([(path, path), (str(row.get("name") or ""), path), (PurePosixPath(path).name, path)])
        if dataset:
            attached.append((PurePosixPath(dataset).name, dataset))

        def bind(value):
            matches = {path for alias, path in attached if alias and value == alias and path}
            if len(matches) != 1:
                raise ValueError("input path must identify exactly one attached dataset or input")
            return matches.pop()

        values = setting_values(self.server)
        trusted_bed = values.get("VARIANT_DEFAULT_REGIONS_BED", "") if self.server.name == "variant-annotation" else ""
        for key in ("dataset", "vcf_path", "regions_bed", "path"):
            if args.get(key):
                if key == "regions_bed" and trusted_bed and args[key] == trusted_bed:
                    continue  # exact administrator reference, mounted by setting_paths()
                args[key] = bind(args[key])
                if key == "dataset" and args[key] in {dataset, root}:
                    args.pop(key)  # container's executor-owned DATASET/ROOT is authoritative
                elif args[key] == dataset and ((key == "vcf_path" and self.server.name == "variant-annotation")
                                             or (key == "path" and self.server.name == "data-utilities")):
                    args.pop(key)
        if args.get("dataset_paths"):
            args["dataset_paths"] = [bind(value) for value in args["dataset_paths"]]
        if args.get("model_dir"):
            raise ValueError("model_dir is deployment configuration; omit it to use the approved model")
        # These are run-local filenames, never arbitrary paths. Existing scRNA
        # algorithms resolve them against their private work/artifact directories.
        if args.get("input"):
            value = args["input"]
            parts = value.split("/") if isinstance(value, str) else []
            if (not parts or any(part in {"", ".", ".."} for part in parts)
                    or (len(parts) > 1 and parts[0] not in {"work", "artifacts"})
                    or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[:-1])
                    or not re.fullmatch(r"[A-Za-z0-9_.*?\[\]-]+\.(?:h5ad|rnk)", parts[-1])):
                raise ValueError("input must be a filename or a relative path inside work/ or artifacts/")
        prefix = PREFIXES[self.server.name]
        from .mcp_model import prepare_model
        model_binding, model_setup = prepare_model(self.server.name, tool, executor, values, arguments=args)
        setup = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in values.items())
        setup += model_setup
        setup += BOUND_SETUP
        setup += f'export {prefix}_DATASET="${{AISCIENTIST_DATASET:-}}" {prefix}_READ_ROOTS="$AISCI_MCP_READ_ROOTS"\n'
        if trusted_bed:
            setup += f'export VARIANT_READ_ROOTS="$AISCI_MCP_READ_ROOTS"{shlex.quote(":" + trusted_bed)}\n'
        if self.server.name == "cellqc":
            setup += 'export CELLQC_DATASET="${AISCIENTIST_DATASET_ROOT:-${AISCIENTIST_DATASET:-}}"\n'
        if self.server.name == "scrna-analysis":
            setup += 'export SCRNA_CHECKPOINT_ROOT="$AISCIENTIST_WORK"\n'
        if self.server.name in {"phenotype-diagnosis", "literature"}:
            # Trusted reference roots are distinct from the model-visible paths:
            # prepare() still admits vcf_path only from attachments.
            refs = ":".join(setting_paths(self.server))
            setup += f'export {prefix}_ALLOWED_ROOTS="$AISCI_MCP_READ_ROOTS"{shlex.quote(":" + refs if refs else "")}\n'
        if self.server.name == "phenotype-diagnosis":
            setup += 'export LITERATURE_ALLOWED_ROOTS="${PHENOTYPE_ALLOWED_ROOTS:-}"\n'
        skill = get_skill(self.server.skill)
        secret_names = list(skill.requirements.get("secrets", [])) if skill else []
        if model_binding is not None and getattr(model_binding.binding, "api_key", ""):
            secret_names = list(dict.fromkeys([*secret_names, model_binding.secret_name]))
        setup += f"export AISCI_MCP_SECRET_NAMES={shlex.quote(json.dumps(secret_names))}\n"
        setup += f"export AISCI_MCP_ANALYSIS={shlex.quote(analysis)}\n"
        setup += f"export AISCI_MCP_REQUEST_TASK={shlex.quote(task)}\n"
        operations = [entry["name"] for entry in self.server.tools if not entry["name"].endswith("_job_status")]
        setup += f"export AISCI_MCP_OPERATIONS={shlex.quote(json.dumps(operations))}\n"
        case_note = (self.server.name == "phenotype-diagnosis" and tool == "map_phenotype_to_hpo"
                     and bool((getattr(ctx, "decisions", None) or {}).get("case_note")))
        setup += f"export AISCI_MCP_CASE_NOTE={int(case_note)}\n"
        return PreparedCall(args, setup=setup, collector=COLLECT, tool_cache=self.server.name == "cellqc",
                            context={"model_binding": model_binding})

    def failure(self, out: dict) -> dict:
        # Nonzero CLI/collector exits bypass COLLECT. Their traceback and logs
        # must not expose host locations either; skill policy already redacts keys.
        def clean(value):
            if isinstance(value, str):
                return re.sub(_SERVER_PATH_PATTERN, "[server path]", value)
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value
        return clean(out)

    def finish(self, out: dict, payload: dict, ctx: Any, prepared: PreparedCall) -> dict:
        out = self.failure(out)
        result = payload["result"]
        reserved = {"status", "error", "stdout", "stderr", "returncode", "artifacts", "provenance",
                    "environment", "execution_mode", "skill", "mcp"}
        response = {**{key: value for key, value in result.items() if key not in reserved},
                    **{key: value for key, value in out.items() if key != "stdout"}, "result": result}
        if (str(result.get("status", "ok")).lower() not in {"ok", "empty", "success"}
                or str(result.get("state", "")).upper() in {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}
                or result.get("isError") is True or result.get("is_error") is True):
            response.update(status="error", error=result.get("error") or "MCP analysis failed")
        if self.server.name == "phenotype-diagnosis" and (getattr(ctx, "decisions", None) or {}).get("case_note"):
            if "text_source" in result:
                response["text_source"] = result["text_source"] = "attached_case_note"
        if self.server.name == "cellqc":
            declaration = next((entry for entry in self.server.tools if entry["name"] == "run_cellqc"), None)
            if declaration is not None:
                response["parameter_schema"] = {
                    "tool": self.server.planner_name("run_cellqc"),
                    "sha256": self.server.tools_sha256,
                    "declared_parameters": sorted(declaration["inputSchema"].get("properties", {})),
                    "note": "Supported parameters, not evidence of the values used by a completed task.",
                }
        # The public flat compatibility fields and nested evidence may duplicate
        # rows. Bound the complete response as well, leaving room for MCP identity.
        import json
        if len(json.dumps(response)) > 12500:
            metadata = {key: response[key] for key in
                        ("status", "returncode", "slurm_state", "slurm_job_id", "slurm_node", "node",
                         "provenance", "environment", "execution_mode", "skill", "parameter_schema") if key in response}
            error = response.get("error")
            if metadata.get("status") == "error":
                metadata["error"] = (str(error)[:1500] if error else
                                     "MCP analysis failed; inspect full_result for the complete evidence")
            # First remove duplicate flat rows and optional artifact listings.
            # A 9KB native result often fits intact once it is no longer repeated.
            flat = project_result(self.server.name, result, 2200)
            response = {**flat, **metadata, "result": result, "response_compacted": True}
            if not result.get("response_omitted"):
                response["response_summary"]["scope"] = "flat_compatibility_fields"
                response["response_summary"]["note"] = "The complete native response is retained in result."
            if len(json.dumps(response)) > 12500:
                summary = project_result(self.server.name, result, 4500)
                summary["response_omitted"] = True
                response = {**summary, **metadata, "result": summary, "response_compacted": True}
                if len(json.dumps(response)) > 12500:
                    # Unicode escaping can expand a byte-bounded preview. Keep
                    # one nested copy plus the small compatibility view in that case.
                    flat = project_result(self.server.name, summary, 1400)
                    response = {**flat, **metadata, "result": summary, "response_compacted": True}
            # Artifact paths are optional navigation, never a reason to discard
            # scientific evidence. Every artifact remains in the full manifest.
            response["artifacts"] = []
            paths = out.get("artifacts", [])
            for path in paths[:50]:
                response["artifacts"].append(path)
                if len(json.dumps(response)) > 12450:
                    response["artifacts"].pop()
                    break
            if len(response["artifacts"]) < len(paths):
                response["artifacts_omitted"] = True
        return response
