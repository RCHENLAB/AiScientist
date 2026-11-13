"""Opt-in local corpus retrieval in the registered, immutable literature image.

Only search_corpus is callable. The administrator pre-pulls the digest; requests
cannot select commands, images, mounts, models or network access. The gateway
does not import any scientific code from the server package.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable
from uuid import uuid4

from jsonschema import Draft202012Validator

from .literature_mcp import _confined, registered_server

_CLIENT = r'''
import asyncio, json, os, sys
from importlib.metadata import version
from mcp import Client
from mcp.client.stdio import StdioServerParameters
request = json.load(sys.stdin)
if version("aiscientist-mcp-literature") != request["version"]:
    raise ValueError("installed literature package differs from the registered release")
async def main():
    async with Client(StdioServerParameters(command="literature-mcp", env=dict(os.environ)),
                      read_timeout_seconds=540) as client:
        listed = (await client.list_tools()).tools
        actual = next(t for t in listed if t.name == "search_corpus")
        if actual.model_dump(by_alias=True)["inputSchema"] != request["schema"]:
            raise ValueError("live search_corpus schema differs from its registered schema")
        result = await client.call_tool("search_corpus", request["arguments"])
        if result.is_error:
            return {"status": "error", "error": " ".join(getattr(c, "text", "") for c in result.content)}
        return result.structured_content
print(json.dumps(asyncio.run(main()), allow_nan=False))
'''


def enabled() -> bool:
    return os.environ.get("AISCIENTIST_LOCAL_CORPUS_MCP", "").lower() == "docker"


def _mount(source: Path, target: str, *, readonly: bool) -> list[str]:
    value = str(source)
    if not source.is_absolute() or any(c in value for c in (",", "\0", "\n", "\r")):
        raise ValueError("local corpus mount must be an absolute path without separators")
    return ["--mount", f"type=bind,src={value},dst={target}" + (",readonly" if readonly else "")]


def command(docker: str, image: str, index: Path, hf_cache: Path, output: Path, name: str) -> list[str]:
    """Fixed resource/mount policy, independent of model-selected arguments."""
    args = [docker, "run", "--rm", "--pull=never", "--name", name, "--init", "-i",
            "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--cpus=2", "--memory=8g", "--memory-swap=8g", "--pids-limit=128",
            "--user", f"{os.getuid()}:{os.getgid()}", "--workdir=/output",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
            *_mount(index, "/corpus", readonly=True), *_mount(hf_cache, "/hf_cache", readonly=True),
            *_mount(output, "/output", readonly=False)]
    for group in sorted(set(os.getgroups())):
        args += ["--group-add", str(group)]
    settings = {"AISCI_DATA_DIR": "/output", "AISCI_EXECUTOR": "inline", "HOME": "/tmp",
                "LITERATURE_CORPUS_ROOT": "/corpus", "LITERATURE_ALLOWED_ROOTS": "/corpus:/hf_cache",
                "LITERATURE_PAPERS": "/corpus",
                "LITERATURE_CACHE_DIR": "/corpus/mechanism_cache", "LITERATURE_HF_CACHE": "/hf_cache",
                "HF_HOME": "/hf_cache", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2", "MKL_NUM_THREADS": "2",
                "TOKENIZERS_PARALLELISM": "false"}
    for filename in ("manifest.csv", "paperqa_manifest.csv"):
        if (index / filename).is_file():
            settings["LITERATURE_MANIFEST"] = "/corpus/" + filename
            break
    embedding = os.environ.get("LITERATURE_EMBEDDING") or os.environ.get("AISCIENTIST_PAPERQA_EMBEDDING")
    if embedding:
        settings["LITERATURE_EMBEDDING"] = embedding
    for key, value in settings.items():
        args += ["--env", f"{key}={value}"]
    return [*args, image.removeprefix("docker://"), "python", "-c", _CLIENT]


def _execute(args: list[str], payload: dict, output: Path, *, should_cancel: Callable[[], bool],
             timeout: float = 600) -> dict:
    """Bound output, wall time and cancellation, including the daemon-owned container."""
    name = args[args.index("--name") + 1]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                env={key: os.environ[key] for key in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT")
                                     if key in os.environ})
        try:
            proc.stdin.write(json.dumps(payload, allow_nan=False).encode())
            proc.stdin.close()
            deadline = time.monotonic() + timeout
            stop = None
            while proc.poll() is None:
                if should_cancel():
                    stop = "cancelled"
                elif time.monotonic() > deadline:
                    stop = "timeout"
                elif os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > 32 * 1024 * 1024:
                    stop = "output_limit"
                if stop:
                    break
                time.sleep(0.1)
            if stop:
                return {"status": "cancelled" if stop == "cancelled" else "error",
                        "error": "Local literature MCP stopped: " + stop}
            stdout.seek(0)
            if proc.returncode:
                return {"status": "error", "error": "Local literature MCP could not run; verify the pinned image and corpus deployment"}
            value = json.load(stdout)
            if not isinstance(value, dict):
                raise ValueError("local literature MCP returned no result object")
            if value.get("full_result"):
                relative = str(value["full_result"]).removeprefix("/output/")
                if not str(value["full_result"]).startswith("/output/literature/"):
                    raise ValueError("local literature evidence is outside its output directory")
                full = _confined(output, relative)
                if full.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("local literature evidence exceeds the read limit")
                value = json.loads(full.read_text())
                if not isinstance(value, dict):
                    raise ValueError("local literature evidence must be an object")
            return {key: val for key, val in value.items() if key not in {"artifact_root", "full_result"}}
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    subprocess.run([args[0], "rm", "-f", name], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=15, check=False)
                finally:
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)


def make_tool(*, should_cancel: Callable[[], bool] | None = None) -> Any:
    """Return the local typed search tool only for an explicit, usable deployment."""
    if not enabled():
        return None
    from .local_corpus import local_root, index_dir, has_local_index
    from ..agents.research_harness import HarnessTool
    from ..agents.skills import get_skill

    server = registered_server()
    docker = shutil.which("docker")
    if server is None or not docker or not has_local_index():
        return None
    skill = get_skill(server.skill)
    if skill is None or skill.image != server.image:
        return None
    entry = next((row for row in server.tools if row["name"] == "search_corpus"), None)
    if entry is None:
        return None
    schema = copy.deepcopy(entry["inputSchema"])
    validator = Draft202012Validator(schema)
    cancel = should_cancel or (lambda: False)

    def run(arguments, ctx):
        error = next(validator.iter_errors(arguments), None)
        if error is not None:
            return {"status": "error", "error": "Invalid MCP arguments: " + error.message}
        active_cancel = getattr(ctx, "should_cancel", cancel)
        if not callable(active_cancel):
            active_cancel = cancel
        if active_cancel():
            return {"status": "cancelled"}
        try:
            root, index = local_root(), index_dir()
            hf = root / "hf_cache"
            if not hf.is_dir():
                raise ValueError("the offline local embedding cache is unavailable")
            workspace = Path(ctx.workspace)
            if not workspace.is_absolute() or workspace.is_symlink():
                raise ValueError("local literature requires a private absolute run workspace")
            scratch = workspace / ".local-literature-mcp"
            if scratch.is_symlink():
                raise ValueError("local literature work directory cannot be a symlink")
            scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="search-", dir=scratch) as folder:
                output = Path(folder)
                argv = command(docker, server.image, index, hf, output, "aisci-lit-" + uuid4().hex)
                result = _execute(argv, {"version": server.version, "schema": schema,
                                         "arguments": arguments}, output, should_cancel=active_cancel)
            return {**result, "mcp": {**server.identity(), "execution_mode": "local-docker"}}
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return {"status": "error", "error": f"Local literature MCP unavailable: {type(exc).__name__}"}

    return HarnessTool(name="search_corpus", description=entry["description"], parameters=schema,
                       executor=run, category="literature", reads_private_data=False)
