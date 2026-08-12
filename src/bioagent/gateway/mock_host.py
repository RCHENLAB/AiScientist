from __future__ import annotations

from dataclasses import dataclass, field

from .executor import ExecResult


@dataclass
class MockState:
    """Mutable state of the simulated HPC3 + vLLM host."""

    llm_installed: bool = False
    models: set[str] = field(default_factory=set)
    serving: bool = False
    serve_port: int = 11434
    gpu_job_id: str | None = None
    gpu_node: str | None = None
    gpu_util: int = 0
    gpu_mem_used: int = 0
    gpu_mem_total: int = 49140  # ~48 GB, like an L40S
    gpu_name: str = "NVIDIA L40S"
    tick: int = 0
    staged_files: list[tuple[str, str]] = field(default_factory=list)  # (local, remote) put_file calls


class MockExecutor:
    """In-process fake remote host.

    Responds to the same command vocabulary the GPU/serve managers emit, with a
    small state machine: the LLM image starts absent, gets staged, a model is
    pulled, a GPU job is allocated, and the server starts. This lets the full UI
    flow run end-to-end with no real cluster.
    """

    def __init__(self, host: str = "hpc3-mock.rcic.uci.edu", username: str = "demo", *, preinstalled: bool = False) -> None:
        self.host = host
        self.username = username
        self.state = MockState(llm_installed=preinstalled)
        if preinstalled:
            self.state.models.add("qwen3")

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        st = self.state
        st.tick += 1
        cmd = command.strip()

        def ok(out: str = "", err: str = "") -> ExecResult:
            return ExecResult(command=cmd, exit_status=0, stdout=out, stderr=err, duration_ms=12)

        def fail(code: int, err: str = "", out: str = "") -> ExecResult:
            return ExecResult(command=cmd, exit_status=code, stdout=out, stderr=err, duration_ms=8)

        # --- file writes (heredoc) ----------------------------------------
        # A `cat > file <<'EOF' ... EOF` write may *contain* tool keywords like
        # "sbatch" inside the script body; treat it purely as
        # a file write with no side effects so those keywords don't misfire.
        if "BIOAGENT_EOF" in cmd or ("cat >" in cmd and "<<" in cmd):
            return ok()

        # --- Slurm GPU allocation (checked before generic shell) ----------
        if cmd.startswith("sbatch") or cmd.split("&&")[-1].strip().startswith("sbatch"):
            st.gpu_job_id = "1284756"
            st.gpu_node = "gpu-3-1"
            return ok("Submitted batch job 1284756")
        if cmd.startswith("salloc"):
            st.gpu_job_id = "1284756"
            st.gpu_node = "gpu-3-1"
            return ok("salloc: Granted job allocation 1284756\nsalloc: Nodes gpu-3-1 are ready for job")
        if "squeue" in cmd:
            if not st.gpu_job_id:
                return ok("")
            if "-j" in cmd:
                # ensure_serve_job polling format: %t|%N|%r
                return ok(f"R|{st.gpu_node}|None")
            # find_running_job format: %i|%u|%t|%N|%b|%M
            return ok(f"{st.gpu_job_id}|{self.username}|R|{st.gpu_node}|gpu:l40s:1|0:42")
        if "sinfo" in cmd:
            return ok("gpu*    up   3-00:00:00   8   idle   gpu-3-[1-8]")
        if cmd.startswith("scancel") or "scancel " in cmd:
            st.gpu_job_id = None
            st.gpu_node = None
            st.serving = False
            return ok()

        # --- GPU health (nvidia-smi) --------------------------------------
        if "nvidia-smi" in cmd:
            if st.gpu_job_id is None:
                return fail(9, err="No devices were found")
            st.gpu_util = [3, 11, 64, 88, 72, 41][st.tick % 6]
            st.gpu_mem_used = 2200 + st.gpu_util * 180
            return ok(f"{st.gpu_util}, {st.gpu_mem_used}, {st.gpu_mem_total}, {st.gpu_name}")

        # --- vLLM container image presence (vllm_client.ensure_installed) --
        if cmd.startswith("test -f") and ".sif" in cmd:
            st.serving = True
            return ok("__SIF_OK__")

        # --- dynamic serve-port file (gpu.read_serve_port) ----------------
        if cmd.startswith("cat ") and "vllm.port" in cmd:
            return ok(str(st.serve_port)) if st.gpu_job_id else ok("")

        # --- basic shell (after tools, so heredoc bodies don't misfire) ---
        if cmd in ("hostname", "uname -n"):
            return ok("hpc3-login-1")
        if cmd.startswith("echo "):
            return ok(cmd[5:].strip().strip('"').strip("'"))
        if cmd.startswith("mkdir"):
            return ok()

        # default: unknown command succeeds quietly (keeps the demo flowing)
        return ok()

    def put_file(self, local_path: str, remote_path: str) -> None:
        # No real remote FS in mock mode; record the staging request, do nothing.
        self.state.staged_files.append((local_path, remote_path))

    def get_file(self, remote_path: str, local_path: str) -> None:
        # No real remote FS in mock mode; nothing to fetch.
        return None

    def read_bytes(self, remote_path: str, max_bytes: "int | None" = None) -> bytes:
        # No real remote FS in mock mode; "missing file" is the honest answer, and it is the
        # same answer the real executor gives for one — callers already handle it.
        return b""

    def remote_size(self, remote_path: str) -> int:
        return 0

    def open_tunnel(self, remote_host: str, remote_port: int, local_port: int = 0) -> int:
        # No real socket in mock mode; echo a fixed local_port if pinned, else a
        # stable pseudo port.
        return local_port or 12434

    def close(self) -> None:
        self.state.serving = False
