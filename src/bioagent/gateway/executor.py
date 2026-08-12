from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ExecResult:
    """Result of a single remote command, with both streams fully captured."""

    command: str
    exit_status: int
    stdout: str
    stderr: str
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()


class RemoteExecutor(Protocol):
    """A connected remote host we can run commands on and tunnel into.

    Implemented by ``SSHExecutor`` (real paramiko session) and ``MockExecutor``
    (in-process simulated HPC3 host). The gateway logic depends only on this
    interface, so the exact same flow runs in mock and live mode.
    """

    host: str
    username: str

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        """Run ``command`` and return its full stdout/stderr/exit status."""
        ...

    def put_file(self, local_path: str, remote_path: str) -> None:
        """Upload a local file to ``remote_path`` over the authenticated session, creating
        the remote parent directory if needed (used to stage a dataset onto shared DFS)."""
        ...

    def get_file(self, remote_path: str, local_path: str) -> None:
        """Download ``remote_path`` to ``local_path`` (used to pull job outputs back)."""
        ...

    def read_bytes(self, remote_path: str, max_bytes: int | None = None) -> bytes:
        """Read (at most ``max_bytes`` of) a remote file. ``b""`` when missing/unreadable.

        Prefer this over ``exec("cat …")`` for file CONTENT: content is a transfer, and on HPC3
        transfers must stay off the login node (see ``SSHExecutor._open_sftp``)."""
        ...

    def remote_size(self, remote_path: str) -> int:
        """Size of a remote file in bytes; 0 when missing or unreadable."""
        ...

    def open_tunnel(self, remote_host: str, remote_port: int, local_port: int = 0) -> int:
        """Forward ``local_port`` (0 = auto) to ``remote_host:remote_port``; return the local port."""
        ...

    def close(self) -> None:
        ...
