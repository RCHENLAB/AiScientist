from __future__ import annotations

import base64
import os
import select
import shlex
import socket
import shutil
import socketserver
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import paramiko

from .errors import GatewayError, error_detail, public_error
from .executor import ExecResult

EmitFn = Callable[[str, str, str], None]  # (level, stage, message)


def _noop_emit(level: str, stage: str, message: str) -> None:
    return None


def _close_quietly(transport: "paramiko.Transport | None") -> None:
    if transport is None:
        return
    try:
        transport.close()
    except Exception:  # noqa: BLE001 - closing a dead transport must never raise
        pass


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

    A dropped transport is re-established in place (:meth:`_live_transport`) with the session's
    SSH key, so the objects a run holds on to — the Slurm executors, the tunnels — keep working
    on the same executor instead of failing on the first command after a network blip.
    """

    # Seconds to wait before each reconnect attempt: ~2 minutes in all, long enough for a network
    # blip on the gateway host (or, on a developer's laptop, a VPN or Wi-Fi hand-over) to come
    # back, short enough that a network that is really gone fails the run with a clear message
    # instead of hanging it.
    _reconnect_delays: tuple[float, ...] = (0.0, 5.0, 15.0, 30.0, 60.0)
    # The key a dropped session reconnects with: the login key on key auth, or one armed after a
    # password + Duo login (:meth:`arm_reconnect_key`). None = no silent reconnect is possible.
    _reconnect_pkey: "paramiko.PKey | None" = None

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
        # The same key as a FILE, for the OpenSSH fast path (see _native_sftp_argv). OpenSSH in batch
        # mode cannot be handed a passphrase, so an encrypted key is loaded once into an ssh-agent
        # that belongs to this session alone (see _agent_socket) and the passphrase is kept for that.
        self._transfer_key_path: str | None = None
        self._transfer_key_encrypted = False
        self._transfer_key_passphrase: str | None = None
        self._agent: subprocess.Popen | None = None
        self._agent_dir: str | None = None
        self._agent_failed = False
        self._agent_lock = threading.Lock()
        self._transfer_disabled = False  # set after the one-time fallback warning
        # Serializes reconnects: several threads hit a dead transport at once, and only ONE of
        # them should redial. ``_reconnect_rounds`` counts finished attempts, so a thread that
        # waited out someone else's failed round reports it instead of starting another.
        self._reconnect_lock = threading.Lock()
        self._reconnect_rounds = 0
        self._reconnect_error: GatewayError | None = None

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
                f"The AiScientist server could not open an SSH connection to {host}:{port}. "
                "HPC3 may be down or in maintenance: try again in a few minutes. (Only a gateway "
                "running on your own computer needs the UCI campus network or VPN.)",
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
        transport = self._open_transport(self.host, connect_timeout)
        self._emit("info", "ssh_connect", "SSH transport established; authenticating ...")

        if key_path:
            pkey = self._load_key(key_path, key_passphrase)
            # Never the key file's path: it is a file in the server's key store (issue 37).
            self._emit("step", "ssh_auth", "Authenticating with your SSH key ...")
            transport.auth_publickey(self.username, pkey)
            # Keep the LOADED key (not the path) so the data-transfer connection — and a reconnect
            # after a drop — can authenticate without re-reading the file or re-prompting for a
            # passphrase.
            self._transfer_pkey = pkey
            self._reconnect_pkey = pkey
            self._transfer_key_path = key_path
            self._transfer_key_encrypted = bool(key_passphrase)
            self._transfer_key_passphrase = key_passphrase or None
        else:
            self._interactive_auth(transport, password or "", duo_response, duo_callback)

        if not transport.is_authenticated():
            raise paramiko.AuthenticationException("Transport did not authenticate.")

        self._client._transport = transport  # reuse the authenticated transport
        self._keep_alive(transport)
        self._emit("success", "ssh_auth", f"Authenticated to {self.host} as {self.username}.")

    def _open_transport(self, host: str, timeout: float) -> paramiko.Transport:
        """TCP-connect to ``host`` and run the SSH handshake. Authentication is the caller's."""
        sock = socket.create_connection((host, self.port), timeout=timeout)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=timeout)
        return transport

    @staticmethod
    def _keep_alive(transport: paramiko.Transport) -> None:
        """Keep the transport (and every port-forward channel riding on it — the vLLM tunnel
        especially) alive across idle gaps. Without this, a user who walks away mid-run has the SSH
        connection reaped by the server/firewall, and the next vLLM call fails with a bare "Network
        error". 30s is well under typical idle timeouts."""
        try:
            transport.set_keepalive(30)
        except Exception:  # noqa: BLE001 - keepalive is best-effort, never block the connect
            pass

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
            raise GatewayError("The SSH key file was not found on the server. Add the key again.",
                               stage="ssh_auth")
        last_error: Exception | None = None
        for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return key_cls.from_private_key_file(str(path), password=passphrase or None)
            except paramiko.SSHException as exc:
                last_error = exc
        raise GatewayError(
            "Could not load the SSH key. Check its format and passphrase.",
            stage="ssh_auth",
            detail=error_detail(last_error) if last_error else None,
        )

    # -- reconnecting a dropped session ---------------------------------------

    def arm_reconnect_key(self, key_path: str, passphrase: str | None = None) -> bool:
        """Let a password + Duo session reconnect with the user's saved SSH key.

        Such a session has no key of its own, and redialling it would need another Duo push that
        nobody is watching for mid-run. A key the user already deployed to HPC3 authenticates with
        no prompt. Returns False when the key cannot be loaded; the session then just cannot
        reconnect silently, exactly as before."""
        try:
            self._reconnect_pkey = self._load_key(key_path, passphrase)
        except GatewayError:
            return False
        # The key is deployed in the user's HPC3 authorized_keys, and access-hpc3 shares that home,
        # so it opens the transfer host too: a password session with a saved key stops staging
        # over the login node (RCIC rule) instead of falling back to it.
        if self._transfer_pkey is None:
            self._transfer_pkey = self._reconnect_pkey
            self._transfer_key_path = key_path
            self._transfer_key_encrypted = bool(passphrase)
            self._transfer_key_passphrase = passphrase or None
        return True

    def _live_transport(self) -> paramiko.Transport:
        """The login session's transport, reconnected first if it has dropped.

        Raises ``GatewayError`` when the session is down and cannot be brought back."""
        transport = self._client.get_transport()
        if transport is not None and transport.is_active():
            return transport
        rounds_seen = self._reconnect_rounds
        with self._reconnect_lock:
            transport = self._client.get_transport()
            if transport is not None and transport.is_active():
                return transport                       # another thread reconnected while we waited
            if self._reconnect_rounds != rounds_seen and self._reconnect_error is not None:
                # Another thread just spent a whole round of attempts and failed; a second round
                # right behind it would only double the wait before the same error.
                err = self._reconnect_error
                raise GatewayError(err.message, stage=err.stage, detail=err.detail)
            try:
                self._reconnect()
                self._reconnect_error = None
            except GatewayError as exc:
                self._reconnect_error = exc
                raise
            finally:
                self._reconnect_rounds += 1
        return self._client.get_transport()

    def _reconnect(self) -> None:
        """Re-open the login session with the reconnect key and swap it in under ``self._client``.

        Everything that holds this executor (Slurm executors, job stores, tunnels) carries on over
        the new transport. Only a network failure is retried; HPC3 refusing the key is final."""
        pkey = self._reconnect_pkey
        if pkey is None:
            raise GatewayError(
                "SSH session is no longer active. It was opened with a password and Duo and there "
                "is no saved SSH key to reconnect with — reconnect from the login panel.",
                stage="ssh_exec")
        attempts = len(self._reconnect_delays)
        last_error: Exception | None = None
        for attempt, delay in enumerate(self._reconnect_delays, start=1):
            if delay:
                time.sleep(delay)
            self._emit("warning", "ssh_reconnect",
                       f"SSH session to {self.host} dropped — reconnecting with your SSH key "
                       f"(attempt {attempt}/{attempts}) ...")
            transport: paramiko.Transport | None = None
            try:
                transport = self._open_transport(self.host, min(self._connect_timeout, 15.0))
                transport.auth_publickey(self.username, pkey)
                if not transport.is_authenticated():
                    raise paramiko.AuthenticationException("Transport did not authenticate.")
            except paramiko.AuthenticationException as exc:
                _close_quietly(transport)
                raise GatewayError(
                    f"SSH session to {self.host} dropped, and HPC3 refused the saved SSH key when "
                    "reconnecting. Log in again from the login panel.",
                    stage="ssh_auth", detail=error_detail(exc)) from exc
            except (paramiko.SSHException, OSError, EOFError) as exc:
                _close_quietly(transport)
                last_error = exc
                continue
            self._keep_alive(transport)
            _close_quietly(self._client.get_transport())
            self._client._transport = transport
            self._emit("success", "ssh_reconnect", f"SSH session to {self.host} re-established.")
            return
        raise GatewayError(
            f"SSH session is no longer active, and {attempts} attempts to reconnect to {self.host} "
            f"over {int(sum(self._reconnect_delays))}s failed. The AiScientist server cannot reach "
            "HPC3 right now: reconnect from the login panel in a few minutes.",
            stage="ssh_exec", detail=error_detail(last_error) if last_error else None)

    def _open_channel(self, timeout: float) -> paramiko.Channel:
        transport = self._live_transport()
        try:
            return transport.open_session(timeout=timeout)
        except (paramiko.SSHException, EOFError, OSError):
            if transport.is_active():
                raise
            # The transport died between the liveness check and the channel open. Nothing has been
            # sent yet, so reconnecting and sending the command once is safe — unlike a drop after
            # exec_command, where a non-idempotent command (sbatch) may already have run.
            return self._live_transport().open_session(timeout=timeout)

    # -- commands -----------------------------------------------------------

    def exec(self, command: str, timeout: float = 60.0) -> ExecResult:
        started = time.monotonic()
        # When a lab group is configured, run under it (DFS lab dirs need it).
        wire_command = wrap_in_group(command, self.group) if self.group else command
        try:
            channel = self._open_channel(timeout)
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
            "AISCIENTIST_HPC_TRANSFER_HOST, or log in with an SSH key, to route them properly.",
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
            self._fall_back_to_login_node(f"{self.transfer_host} is not usable ({public_error(exc)})")
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
        client = self._transfer_session()
        if client is None:
            self._live_transport()   # staging over the login session: bring it back if it dropped
            client = self._client
        return client.open_sftp()

    def open_sftp(self) -> "paramiko.SFTPClient":
        """An SFTP channel on the transfer host that the caller keeps open and closes: for a loop
        of many small reads and writes (``gateway/llm_relay.py``, the model calls of a job)."""
        return self._open_sftp()

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

    def put_file_resumable(self, local_path: str, remote_path: str,
                           progress: "Callable[[int, int], None] | None" = None,
                           make_parent: bool = True) -> None:
        """:meth:`put_file` for large files. Writes ``<remote>.part``, continuing from where an
        interrupted copy stopped, then renames it into place, so a dropped 30 GB BAM transfer
        resumes and a half-written file is never mistaken for the real one. ``progress(sent, total)``
        is called as bytes land; an exception it raises aborts the copy and leaves the ``.part``.

        The bytes go through the OpenSSH client when it can authenticate on its own (see
        :meth:`_native_sftp_argv`; ~4x paramiko's speed), else through paramiko. A ``.part`` older
        than the source is a leftover of a different file and is restarted, never appended to."""
        import posixpath

        parent = posixpath.dirname(remote_path)
        if parent and make_parent:
            self.exec(f"mkdir -p {shlex.quote(parent)}")
        part = remote_path + ".part"
        local_stat = os.stat(local_path)
        total = local_stat.st_size
        sftp = self._open_sftp()
        try:
            offset = _resume_offset(sftp, part, total, local_stat.st_mtime)
            copied = offset == total and total > 0
            argv = self._native_sftp_argv()
            if not copied and argv is not None and _native_safe(local_path, part):
                copied = self._native_put(argv, local_path, part, offset, total, progress, sftp)
                if not copied:  # carry on with paramiko from wherever the OpenSSH client stopped
                    offset = _resume_offset(sftp, part, total, local_stat.st_mtime)
            if not copied:
                _paramiko_put(sftp, local_path, part, offset, total, progress)
            landed = int(sftp.stat(part).st_size or 0)
            if landed != total:
                raise GatewayError(
                    f"Upload of {local_path} stopped at {landed} of {total} bytes.",
                    stage="ssh_put",
                )
            if progress is not None:
                progress(total, total)
            try:
                sftp.posix_rename(part, remote_path)
            except (OSError, paramiko.SSHException):
                # A server without the posix-rename extension: plain rename refuses to overwrite.
                try:
                    sftp.remove(remote_path)
                except OSError:
                    pass
                sftp.rename(part, remote_path)
        except (OSError, paramiko.SSHException) as exc:
            raise GatewayError(
                f"Failed to upload {local_path} -> {remote_path}.",
                stage="ssh_put",
                detail=error_detail(exc),
            ) from exc
        finally:
            sftp.close()

    # Measured 2026-10-08, eyeserver -> access-hpc3 -> dfs3b, 8.6 GB of random data:
    #   paramiko (the SFTP above)             101 MB/s; bigger windows 99; 4 threads 75 in total (GIL)
    #   OpenSSH sftp / scp                    ~320 MB/s; scp -c aes128-gcm 438 MB/s
    #   4 OpenSSH sftp processes in parallel  763 MB/s in total
    # The eyeserver link is 10 Gb/s, so paramiko, not the network, was the ceiling.
    _NATIVE_CIPHERS = "aes128-gcm@openssh.com,chacha20-poly1305@openssh.com,aes128-ctr"

    def _native_sftp_argv(self) -> list[str] | None:
        """argv for the OpenSSH ``sftp`` client in batch mode, talking to the transfer host — or
        None when it cannot run unattended: no transfer host, no key FILE (a password session
        without a saved key), an encrypted key that could not be loaded into this session's agent,
        no ``sftp`` binary, or ``AISCIENTIST_NATIVE_SFTP=0``. Never the login node: that is
        paramiko's fallback to warn about, not something to speed up."""
        if self.transfer_host is None or self._transfer_disabled:
            return None
        if os.environ.get("AISCIENTIST_NATIVE_SFTP", "1").strip().lower() in ("0", "false", "no", "off"):
            return None
        key = getattr(self, "_transfer_key_path", None)
        if not key or not os.path.isfile(key):
            return None
        binary = shutil.which("sftp")
        if not binary:
            return None
        if getattr(self, "_transfer_key_encrypted", False):
            sock = self._agent_socket(key)
            if sock is None:
                return None
            identity = ["-o", f"IdentityAgent={sock}", "-o", "IdentityFile=none"]
        else:
            identity = ["-i", key, "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none"]
        state = os.environ.get("AISCIENTIST_STATE_DIR", "").strip()
        known_hosts = Path(state) / "ssh_known_hosts" if state else Path.home() / ".ssh" / "aiscientist_known_hosts"
        return [
            binary, "-q", "-b", "-", "-P", str(self.port), "-c", self._NATIVE_CIPHERS, *identity,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", f"ConnectTimeout={int(self._connect_timeout)}", "-o", "ServerAliveInterval=30",
            f"{self.username}@{self.transfer_host}",
        ]

    def _agent_socket(self, key: str) -> str | None:
        """The socket of an ``ssh-agent`` that holds this session's passphrase-protected key, started
        on first use. None when that is not possible; the copy then stays on paramiko.

        The agent is this session's alone: its socket sits in a directory only the gateway's account
        can enter, it holds one key, and :meth:`close` kills it. The passphrase reaches ``ssh-add``
        through an askpass helper and that one process's environment, never a command line; the
        decrypted key exists only in the agent's memory, as it already does in paramiko's."""
        import tempfile

        lock = getattr(self, "_agent_lock", None)
        passphrase = getattr(self, "_transfer_key_passphrase", None)
        if lock is None or not passphrase:
            return None
        with lock:
            agent = getattr(self, "_agent", None)
            if agent is not None and agent.poll() is None and self._agent_dir:
                return os.path.join(self._agent_dir, "agent.sock")
            if getattr(self, "_agent_failed", False):
                return None
            self._stop_agent()
            agent_bin, add_bin = shutil.which("ssh-agent"), shutil.which("ssh-add")
            why = "ssh-agent or ssh-add is not installed"
            if agent_bin and add_bin:
                home = tempfile.mkdtemp(prefix="aisc-agent-")        # 0700, ours alone
                sock = os.path.join(home, "agent.sock")
                self._agent_dir = home
                try:
                    self._agent = subprocess.Popen([agent_bin, "-D", "-a", sock], stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    deadline = time.monotonic() + 5.0
                    while not os.path.exists(sock) and time.monotonic() < deadline and self._agent.poll() is None:
                        time.sleep(0.02)
                    askpass = os.path.join(home, "askpass")
                    with open(os.open(askpass, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "w") as f:
                        # Answers once: asked again means the passphrase was wrong, and ssh-add
                        # would otherwise keep asking until the timeout.
                        f.write('#!/bin/sh\n[ -e "$0.asked" ] && exit 1\n: > "$0.asked"\n'
                                'printf \'%s\\n\' "$AISCIENTIST_KEY_PASSPHRASE"\n')
                    env = {**os.environ, "SSH_AUTH_SOCK": sock, "SSH_ASKPASS": askpass,
                           "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": os.environ.get("DISPLAY") or "none",
                           "AISCIENTIST_KEY_PASSPHRASE": passphrase}
                    try:
                        added = subprocess.run([add_bin, "-q", key], env=env, stdin=subprocess.DEVNULL,
                                               capture_output=True, timeout=20, start_new_session=True)
                    finally:
                        for leftover in (askpass, askpass + ".asked"):
                            try:
                                os.unlink(leftover)
                            except OSError:
                                pass
                    if added.returncode == 0:
                        return sock
                    why = (added.stderr.decode(errors="replace").strip().splitlines() or ["ssh-add failed"])[-1]
                except (OSError, subprocess.SubprocessError) as exc:
                    why = str(exc) or type(exc).__name__
            self._stop_agent()
            self._agent_failed = True
        self._emit("warning", "ssh_transfer",
                   f"The passphrase-protected key could not be loaded for fast copies ({why}); "
                   "copies use the slower built-in client.")
        return None

    def _stop_agent(self) -> None:
        agent, home = getattr(self, "_agent", None), getattr(self, "_agent_dir", None)
        self._agent = self._agent_dir = None
        if agent is not None:
            try:
                agent.kill()
                agent.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if home:
            shutil.rmtree(home, ignore_errors=True)

    def native_transfer_available(self) -> bool:
        """True when bulk copies run the OpenSSH client, so parallel copies really run in parallel;
        False when they fall back to paramiko, where extra threads only contend for the GIL."""
        return self._native_sftp_argv() is not None

    def _native_put(self, argv: list[str], local_path: str, part: str, offset: int, total: int,
                    progress: "Callable[[int, int], None] | None",
                    sftp: "paramiko.SFTPClient") -> bool:
        """Copy ``local_path`` to ``part`` with the OpenSSH client; True when it finished.

        ``put -a`` appends from the remote size but refuses a file that does not exist yet, hence a
        plain ``put`` for a fresh copy. Progress comes from polling the ``.part`` size over the
        paramiko channel; an exception from ``progress`` kills the client and propagates."""
        import tempfile

        verb = "put -a" if offset > 0 else "put"
        batch = f"{verb} {_sftp_quote(local_path)} {_sftp_quote(part)}\n"
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
            try:
                assert proc.stdin is not None
                proc.stdin.write(batch.encode())
                proc.stdin.close()
                while True:
                    try:
                        proc.wait(timeout=2.0)
                        break
                    except subprocess.TimeoutExpired:
                        if progress is not None:
                            try:
                                sent = int(sftp.stat(part).st_size or 0)
                            except OSError:
                                continue
                            progress(min(sent, total), total)
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            if proc.returncode == 0:
                return True
            err.seek(0)
            why = err.read().decode(errors="replace").strip().splitlines()
        self._emit("warning", "ssh_transfer",
                   f"The OpenSSH client could not copy {os.path.basename(local_path)} "
                   f"({why[-1] if why else f'exit {proc.returncode}'}); continuing with paramiko.")
        return False

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

    def read_stream(self, remote_path: str, block: int = 16 * 1024 * 1024) -> Iterator[bytes]:
        """Yield a remote file's bytes in order, over SFTP on the transfer host, for a download
        that is passed on while it is read.

        One window of ``block`` bytes is requested at a time (its 32 KB reads pipelined, so it is
        as fast as a prefetch) and the next only once this one has been handed on. ``prefetch()``
        would request the whole file at once and hold whatever the consumer had not taken yet: a
        multi-GB matrix read from dfs3b at 100 MB/s and sent to a browser on a home line would sit
        in this process's memory. Raises :class:`GatewayError` if the file ends early."""
        sftp = self._open_sftp()
        try:
            with sftp.open(remote_path, "rb") as handle:
                size = int(handle.stat().st_size or 0)
                offset = 0
                while offset < size:
                    for data in handle.readv([(offset, min(block, size - offset))]):
                        if not data:
                            raise GatewayError(
                                f"{remote_path} ended at {offset} of {size} bytes.", stage="ssh_get")
                        offset += len(data)
                        yield data
        except (OSError, paramiko.SSHException) as exc:
            raise GatewayError(f"Failed to read {remote_path}.", stage="ssh_get",
                               detail=error_detail(exc)) from exc
        finally:
            sftp.close()

    @contextmanager
    def open_read(self, remote_path: str, bufsize: int = 1 << 16) -> Iterator[Any]:
        """A remote file open for random-access reads over SFTP on the transfer host, for a reader
        that seeks: h5py reading an ``.h5ad``'s metadata and first rows pulls only the byte ranges it
        asks for, never the matrix. Each read that misses the buffer fetches at least ``bufsize``
        bytes, so the many small metadata reads cost a few dozen round trips, not hundreds."""
        sftp = self._open_sftp()
        try:
            with sftp.open(remote_path, "rb", bufsize=bufsize) as handle:
                yield handle
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

    def get_file_resumable(self, remote_path: str, local_path: str,
                           progress: "Callable[[int, int], None] | None" = None,
                           block: int = 16 * 1024 * 1024) -> None:
        """:meth:`get_file` for large files, the way back of :meth:`put_file_resumable`: writes
        ``<local>.part``, continuing from where an interrupted copy stopped, and renames it into
        place once it holds every byte. Over SFTP on the transfer host, one window of ``block``
        bytes at a time (as :meth:`read_stream`), so a 20 GB matrix never sits in memory.
        ``progress(received, total)`` is called after each window; an exception it raises aborts the
        copy and leaves the ``.part``."""
        part = local_path + ".part"
        sftp = self._open_sftp()
        try:
            with sftp.open(remote_path, "rb") as handle:
                total = int(handle.stat().st_size or 0)
                offset = os.path.getsize(part) if os.path.exists(part) else 0
                if offset > total:
                    offset = 0                      # not this file's: start again
                with open(part, "ab" if offset else "wb") as out:
                    while offset < total:
                        for data in handle.readv([(offset, min(block, total - offset))]):
                            if not data:
                                raise GatewayError(f"{remote_path} ended at {offset} of {total} bytes.",
                                                   stage="ssh_get")
                            out.write(data)
                            offset += len(data)
                        if progress is not None:
                            progress(offset, total)
            landed = os.path.getsize(part)
            if landed != total:
                raise GatewayError(f"Download of {remote_path} stopped at {landed} of {total} bytes.",
                                   stage="ssh_get")
            os.replace(part, local_path)
        except (OSError, paramiko.SSHException) as exc:
            raise GatewayError(
                f"Failed to download {remote_path} -> {local_path}.",
                stage="ssh_get",
                detail=error_detail(exc),
            ) from exc
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
        return self.open_tunnel_to(lambda: (remote_host, remote_port), local_port=local_port,
                                   label=f"{remote_host}:{remote_port}")

    def open_tunnel_to(self, target: "Callable[[], tuple[str, int] | None]", local_port: int = 0,
                       label: str = "") -> int:
        """Forward 127.0.0.1:local_port to wherever ``target()`` says, asked again for every
        forwarded connection. The local address therefore stays the same while the far end moves:
        the shared model server changes node and port each time its Slurm job is renewed, and
        whatever holds the local URL keeps working. ``target()`` returning None refuses the
        connection (nothing is serving right now)."""
        try:
            self._live_transport()
        except GatewayError as exc:
            raise GatewayError(f"Cannot open tunnel: {exc.message}", stage="ssh_tunnel",
                               detail=exc.detail) from exc

        executor = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                # Resolve the transport per forwarded connection, not once when the tunnel opens:
                # after a reconnect the same local port rides the new session, so whatever still
                # holds the old port (a run's tool context, a literature job's base_url) keeps working.
                transport = executor._client.get_transport()
                if transport is None or not transport.is_active():
                    return
                try:
                    dest = target()
                    if not dest:
                        return
                    channel = transport.open_channel(
                        "direct-tcpip",
                        (dest[0], int(dest[1])),
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
                "use (a fixed AISCIENTIST_LOCAL_TUNNEL_PORT serves only one session at a time) "
                "— free it or set AISCIENTIST_LOCAL_TUNNEL_PORT=0 for an auto port.",
                stage="ssh_tunnel",
            ) from exc
        local_port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, name=f"tunnel-{local_port}", daemon=True)
        thread.start()
        self._tunnels.append((local_port, server))
        self._emit(
            "success",
            "ssh_tunnel",
            f"Tunnel ready: 127.0.0.1:{local_port} -> {label or 'a moving target'} (via {self.host}).",
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
        lock = getattr(self, "_agent_lock", None)
        if lock is not None:
            with lock:
                self._stop_agent()       # the agent holds a decrypted key: it ends with the session
        try:
            self._client.close()
        except Exception:
            pass


# --- helpers for put_file_resumable ---------------------------------------------------------------

def _resume_offset(sftp: "paramiko.SFTPClient", part: str, total: int, local_mtime: float) -> int:
    """Bytes of ``part`` to keep. A ``.part`` that is longer than the source, or was last written
    before the source last changed, belongs to some other file: restart rather than append to it."""
    try:
        st = sftp.stat(part)
    except OSError:
        return 0
    size = int(st.st_size or 0)
    if size > total or (st.st_mtime is not None and st.st_mtime < int(local_mtime)):
        return 0
    return size


def _paramiko_put(sftp: "paramiko.SFTPClient", local_path: str, part: str, offset: int, total: int,
                  progress: "Callable[[int, int], None] | None") -> None:
    with open(local_path, "rb") as src, sftp.open(part, "r+b" if offset else "wb") as dst:
        dst.set_pipelined(True)
        src.seek(offset)
        dst.seek(offset)
        sent = offset
        while True:
            chunk = src.read(4 * 1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            sent += len(chunk)
            if progress is not None:
                progress(sent, total)


def _native_safe(*paths: str) -> bool:
    """The OpenSSH client globs a ``put`` source and reads its batch line by line: paths with glob
    characters or line breaks go through paramiko instead."""
    return not any(c in p for p in paths for c in "*?[]\n\r")


def _sftp_quote(path: str) -> str:
    """One argument of an OpenSSH sftp batch command."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
