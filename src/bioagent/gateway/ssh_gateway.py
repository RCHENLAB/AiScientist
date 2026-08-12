from __future__ import annotations

import base64
import select
import shlex
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Callable

import paramiko

from .errors import GatewayError, error_detail
from .executor import ExecResult

EmitFn = Callable[[str, str, str], None]  # (level, stage, message)


def _noop_emit(level: str, stage: str, message: str) -> None:
    return None


def wrap_in_group(command: str, group: str) -> str:
    """Run ``command`` under a different UNIX group via ``sg``.

    Needed for paths under ``/dfs3b/ruic20_lab/`` which require an active
    ``ruic20_hpc`` group (the email's ``newgrp ruic20_hpc`` note). We base64 the
    whole command and pipe it into ``sg <group> -c 'bash -s'`` so arbitrary
    commands — including heredocs and quotes — survive without escaping issues.
    sg needs no password for a group you already belong to.
    """
    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    return f"printf %s {encoded} | base64 --decode | sg {group} -c 'bash -s'"


class SSHExecutor:
    """A real, connected SSH session to an HPC login node.

    Supports the two RCIC-documented login paths:
      * password + Duo 2FA via keyboard-interactive (you approve the push), and
      * SSH key with a passphrase.

    Commands capture full stdout/stderr/exit status. Local port forwarding lets
    the gateway reach a vLLM server running on a compute node.
    """

    def __init__(
        self,
        host: str,
        username: str,
        *,
        port: int = 22,
        password: str | None = None,
        key_path: str | None = None,
        key_passphrase: str | None = None,
        duo_response: str = "1",
        duo_callback: "Callable[[str], str] | None" = None,
        group: str | None = None,
        connect_timeout: float = 30.0,
        emit: EmitFn = _noop_emit,
        transfer_host: str | None = None,
    ) -> None:
        self.host = host
        self.username = username
        self.port = port
        self.group = group  # run commands under this UNIX group (DFS lab dirs)
        self._emit = emit
        self._tunnels: list[tuple[int, socketserver.TCPServer]] = []
        # Data plane (see _open_sftp): a SECOND connection used only for file staging, so
        # bulk transfers stay off the login node. "" / None = stage over the login session.
        self.transfer_host = (transfer_host or "").strip() or None
        self._connect_timeout = connect_timeout
        self._transfer_client: paramiko.SSHClient | None = None
        self._transfer_pkey: paramiko.PKey | None = None  # set on key auth; reused for the DTN
        self._transfer_disabled = False  # set after the one-time fallback warning

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            self._connect(
                password=password,
                key_path=key_path,
                key_passphrase=key_passphrase,
                duo_response=duo_response,
                duo_callback=duo_callback,
                connect_timeout=connect_timeout,
            )
        except paramiko.AuthenticationException as exc:
            raise GatewayError(
                "SSH authentication failed. Check your UCInetID, password, Duo "
                "approval, or SSH key passphrase.",
                stage="ssh_auth",
                detail=error_detail(exc),
            ) from exc
        except (paramiko.SSHException, socket.error, OSError) as exc:
            raise GatewayError(
                f"Could not open an SSH connection to {host}:{port}. Confirm you "
                "are on the UCI campus network or VPN and the host is reachable.",
                stage="ssh_connect",
                detail=error_detail(exc),
            ) from exc

    # -- connection ---------------------------------------------------------

    def _connect(
        self,
        *,
        password: str | None,
        key_path: str | None,
        key_passphrase: str | None,
        duo_response: str,
        duo_callback: "Callable[[str], str] | None",
        connect_timeout: float,
    ) -> None:
        self._emit("step", "ssh_connect", f"Opening TCP socket to {self.host}:{self.port} ...")
        sock = socket.create_connection((self.host, self.port), timeout=connect_timeout)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=connect_timeout)
        self._emit("info", "ssh_connect", "SSH transport established; authenticating ...")

        if key_path:
            pkey = self._load_key(key_path, key_passphrase)
            self._emit("step", "ssh_auth", f"Authenticating with SSH key {key_path} ...")
            transport.auth_publickey(self.username, pkey)
            # Keep the LOADED key (not the path) so the data-transfer connection can authenticate
            # without re-reading the file or re-prompting for a passphrase.
            self._transfer_pkey = pkey
        else:
            self._interactive_auth(transport, password or "", duo_response, duo_callback)

        if not transport.is_authenticated():
            raise paramiko.AuthenticationException("Transport did not authenticate.")

        self._client._transport = transport  # reuse the authenticated transport
        # Keep the transport (and every port-forward channel riding on it — the vLLM
        # tunnel especially) alive across idle gaps. Without this, a user who walks away
        # mid-run has the SSH connection reaped by the server/firewall, and the next vLLM
        # call fails with a bare "Network error". 30s is well under typical idle timeouts.
        try:
            transport.set_keepalive(30)
        except Exception:  # noqa: BLE001 - keepalive is best-effort, never block the connect
            pass
        self._emit("success", "ssh_auth", f"Authenticated to {self.host} as {self.username}.")

    def _interactive_auth(
        self,
        transport: paramiko.Transport,
        password: str,
        duo_response: str,
        duo_callback: "Callable[[str], str] | None",
    ) -> None:
        def handler(title, instructions, prompt_list):
            context = "\n".join(
                part.strip() for part in (title or "", instructions or "") if part and part.strip()
            )
            responses = []
            for prompt, _echo in prompt_list:
                normalized = prompt.strip().lower()
                if "password" in normalized:
                    responses.append(password)
                elif any(token in normalized for token in ("passcode", "duo", "option", "factor", "push")):
                    full_prompt = (context + "\n" + prompt).strip()
                    if duo_callback is not None:
                        # Pause here and let the UI collect the Duo choice/passcode.
                        responses.append(duo_callback(full_prompt))
                    else:
                        self._emit(
                            "warning",
                            "ssh_auth",
                            "Duo two-factor prompt received — approve the push on your phone now.",
                        )
                        responses.append(duo_response)
                else:
                    # Unknown prompt: ask the user if we can, else best-effort.
                    if duo_callback is not None:
                        responses.append(duo_callback((context + "\n" + prompt).strip()))
                    else:
                        responses.append(password or duo_response)
            return responses

        try:
            transport.auth_interactive(self.username, handler)
        except paramiko.BadAuthenticationType:
            # Server only offers plain password auth.
            self._emit("info", "ssh_auth", "Falling back to password authentication ...")
            transport.auth_password(self.username, password)

    @staticmethod
    def _load_key(key_path: str, passphrase: str | None) -> paramiko.PKey:
        path = Path(key_path).expanduser()
        if not path.exists():
            raise GatewayError(f"SSH key not found at {path}", stage="ssh_auth")
        last_error: Exception | None = None
        for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return key_cls.from_private_key_file(str(path), password=passphrase or None)
            except paramiko.SSHException as exc:
                last_error = exc
        raise GatewayError(
            f"Could not load SSH key {path}. Check the format and passphrase.",
            stage="ssh_auth",
            detail=error_detail(last_error) if last_error else None,
        )

    # -- commands -----------------------------------------------------------

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            raise GatewayError("SSH session is no longer active.", stage="ssh_exec")
        started = time.monotonic()
        # When a lab group is configured, run under it (DFS lab dirs need it).
        wire_command = wrap_in_group(command, self.group) if self.group else command
        try:
            channel = transport.open_session(timeout=timeout)
            channel.settimeout(timeout)
            channel.exec_command(wire_command)
            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []
            while True:
                if channel.recv_ready():
                    stdout_chunks.append(channel.recv(32768))
                if channel.recv_stderr_ready():
                    stderr_chunks.append(channel.recv_stderr(32768))
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                select.select([channel], [], [], 0.2)
                if time.monotonic() - started > timeout:
                    channel.close()
                    raise GatewayError(
                        f"Command timed out after {timeout:.0f}s: {command}",
                        stage="ssh_exec",
                    )
            # drain any remainder
            while channel.recv_ready():
                stdout_chunks.append(channel.recv(32768))
            while channel.recv_stderr_ready():
                stderr_chunks.append(channel.recv_stderr(32768))
            exit_status = channel.recv_exit_status()
            channel.close()
        except paramiko.SSHException as exc:
            raise GatewayError(
                f"Failed to run remote command: {command}",
                stage="ssh_exec",
                detail=error_detail(exc),
            ) from exc
        return ExecResult(
            command=command,
            exit_status=exit_status,
            stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # -- file staging (SFTP over the DEDICATED transfer host) ----------------

    def _fall_back_to_login_node(self, why: str) -> None:
        """Give up on the transfer host for the rest of this session and say so ONCE.

        Staging still works — it just rides the login session again. We stop retrying so a
        dead/unreachable transfer host does not add a connect timeout to every single file.
        """
        self._transfer_disabled = True
        self._emit(
            "warning", "ssh_transfer",
            f"Staging files over the login node {self.host} because {why}. RCIC asks that bulk "
            "transfers (rsync/SFTP/rclone/wget) not run on a login node — set "
            "BIOAGENT_HPC_TRANSFER_HOST, or log in with an SSH key, to route them properly.",
        )

    def _transfer_session(self) -> "paramiko.SSHClient | None":
        """The lazily-opened connection to ``transfer_host``, or None to use the login session."""
        if self.transfer_host is None or self._transfer_disabled:
            return None
        if self._transfer_client is not None:
            existing = self._transfer_client.get_transport()
            if existing is not None and existing.is_active():
                return self._transfer_client
            self._transfer_client = None  # reaped mid-session; reopen below

        if self._transfer_pkey is None:
            # Password sessions would need a SECOND Duo push, and firing one in the middle of a
            # transfer is indistinguishable from a hang. Those stage over the login node until the
            # user mints a key — which the gateway offers on their first password login.
            self._fall_back_to_login_node("this session authenticated with a password, not an SSH key")
            return None

        try:
            sock = socket.create_connection((self.transfer_host, self.port), timeout=self._connect_timeout)
            transport = paramiko.Transport(sock)
            transport.start_client(timeout=self._connect_timeout)
            transport.auth_publickey(self.username, self._transfer_pkey)
            if not transport.is_authenticated():
                raise paramiko.AuthenticationException("Transfer host did not authenticate.")
            try:
                transport.set_keepalive(30)
            except Exception:  # noqa: BLE001 - keepalive is best-effort
                pass
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client._transport = transport
        except Exception as exc:  # noqa: BLE001 - ANY failure here must degrade, never break a run
            self._fall_back_to_login_node(f"{self.transfer_host} is not usable ({error_detail(exc)})")
            return None

        self._transfer_client = client
        self._emit("info", "ssh_transfer",
                   f"File staging routed through {self.transfer_host} (not the login node).")
        return client

    def _open_sftp(self) -> "paramiko.SFTPClient":
        """An SFTP channel for bulk transfer — on ``transfer_host`` when we can reach it.

        RCIC's rule is that login nodes are for login + Slurm submission, NOT data transfer, so
        staging gets its own connection to access-hpc3 rather than riding the login-node transport
        that ``exec`` uses. That host runs a RESTRICTED shell (``echo`` is refused), so ONLY the
        SFTP subsystem is available there — every ``exec``, Slurm call and tunnel stays on the login
        node. It mounts the same $HOME and /dfs3b, so remote paths are unchanged either way.
        """
        client = self._transfer_session() or self._client
        return client.open_sftp()

    def put_file(self, local_path: str, remote_path: str) -> None:
        """Upload ``local_path`` to ``remote_path`` via SFTP, mkdir-ing the remote parent
        first (SFTP does not auto-create dirs). Used to stage a dataset onto shared DFS for
        an HPC3 batch job."""
        import posixpath

        parent = posixpath.dirname(remote_path)
        if parent:
            # The mkdir stays on the LOGIN session on purpose: it is a control-plane command, not a
            # transfer, and the transfer host's restricted shell would refuse to run it anyway.
            self.exec(f"mkdir -p {shlex.quote(parent)}")
        sftp = self._open_sftp()
        try:
            sftp.put(local_path, remote_path)
        except (OSError, paramiko.SSHException) as exc:
            raise GatewayError(
                f"Failed to upload {local_path} -> {remote_path}.",
                stage="ssh_put",
                detail=error_detail(exc),
            ) from exc
        finally:
            sftp.close()

    def read_bytes(self, remote_path: str, max_bytes: int | None = None) -> bytes:
        """Read (at most ``max_bytes`` of) a remote file, over SFTP.

        Reading file CONTENT is a transfer, so it belongs on the transfer host next to
        put/get — not in a ``cat``/``head -c … | base64`` pipeline spawned on a login node.
        SFTP is also better at the job: it fetches a bounded prefix without touching the rest
        of the file, so peeking at a 1.1 GB WGS VCF costs the same as peeking at a small one,
        and the bytes stay binary instead of being inflated a third by base64.

        A missing or unreadable file returns ``b""``, matching the ``cat … 2>/dev/null`` this
        replaces — callers that read "absent" as "nothing yet" keep working unchanged.
        """
        sftp = self._open_sftp()
        try:
            with sftp.open(remote_path, "rb") as handle:
                if max_bytes is None:
                    handle.prefetch()
                    return handle.read()
                # Prefetch EXACTLY the window asked for: an unbounded prefetch would try to pull
                # a whole multi-GB VCF back for a 256 KB peek.
                handle.prefetch(max_bytes)
                return handle.read(max_bytes)
        except (OSError, paramiko.SSHException):
            return b""
        finally:
            sftp.close()

    def remote_size(self, remote_path: str) -> int:
        """Size of a remote file in bytes (0 when it is missing or unreadable)."""
        sftp = self._open_sftp()
        try:
            return int(sftp.stat(remote_path).st_size or 0)
        except (OSError, paramiko.SSHException, TypeError, ValueError):
            return 0
        finally:
            sftp.close()

    def get_file(self, remote_path: str, local_path: str) -> None:
        """Download ``remote_path`` to ``local_path`` via SFTP (pull a job's outputs back)."""
        sftp = self._open_sftp()
        try:
            sftp.get(remote_path, local_path)
        except (OSError, paramiko.SSHException) as exc:
            raise GatewayError(
                f"Failed to download {remote_path} -> {local_path}.",
                stage="ssh_get",
                detail=error_detail(exc),
            ) from exc
        finally:
            sftp.close()

    # -- tunneling ----------------------------------------------------------

    def open_tunnel(self, remote_host: str, remote_port: int, local_port: int = 0) -> int:
        """Forward 127.0.0.1:local_port -> remote_host:remote_port over the SSH
        session. ``local_port=0`` lets the OS pick a free ephemeral port (default);
        a fixed port gives Biomni/Kosmos a stable base_url to reach Qwen3.6."""
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            raise GatewayError("Cannot open tunnel: SSH session is not active.", stage="ssh_tunnel")

        ssh_transport = transport

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    channel = ssh_transport.open_channel(
                        "direct-tcpip",
                        (remote_host, remote_port),
                        self.request.getpeername(),
                    )
                except Exception:
                    return
                if channel is None:
                    return
                try:
                    while True:
                        r, _, _ = select.select([self.request, channel], [], [], 1.0)
                        if self.request in r:
                            data = self.request.recv(32768)
                            if not data:
                                break
                            channel.sendall(data)
                        if channel in r:
                            data = channel.recv(32768)
                            if not data:
                                break
                            self.request.sendall(data)
                finally:
                    channel.close()

        class _Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            server = _Server(("127.0.0.1", local_port), _Handler)
        except OSError as exc:
            raise GatewayError(
                f"Cannot bind local tunnel port {local_port}: {exc}. It may already be in "
                "use (a fixed BIOAGENT_LOCAL_TUNNEL_PORT serves only one session at a time) "
                "— free it or set BIOAGENT_LOCAL_TUNNEL_PORT=0 for an auto port.",
                stage="ssh_tunnel",
            ) from exc
        local_port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, name=f"tunnel-{local_port}", daemon=True)
        thread.start()
        self._tunnels.append((local_port, server))
        self._emit(
            "success",
            "ssh_tunnel",
            f"Tunnel ready: 127.0.0.1:{local_port} -> {remote_host}:{remote_port} (via {self.host}).",
        )
        return local_port

    # -- teardown -----------------------------------------------------------

    def close(self) -> None:
        for _port, server in self._tunnels:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        self._tunnels.clear()
        if self._transfer_client is not None:
            try:
                self._transfer_client.close()
            except Exception:
                pass
            self._transfer_client = None
        try:
            self._client.close()
        except Exception:
            pass
