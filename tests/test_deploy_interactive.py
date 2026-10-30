"""scripts/deploy_interactive.sh refuses a server on the release layout: an rsync with --delete into
app/ would remove app/releases/, the running release included. Run with stand-ins for ssh (the
remote command runs here), rsync (recorded, nothing copied) and sudo (-n needs a password; -u runs
the command as the service account, which can see its own folder)."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

SSH = """#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in
    -O) exit 0 ;;
    -o|-p) shift 2 ;;
    -t) shift ;;
    *) break ;;
  esac
done
shift
[ $# -gt 0 ] || exit 0
exec bash -c "$*"
"""

RSYNC = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$RSYNC_LOG"
dest="${@: -1}"
case "$dest" in *:*) mkdir -p "${dest#*:}" ;; esac
"""

SUDO = """#!/usr/bin/env bash
[ "$1" = -n ] && exit 1
[ "$1" = -u ] && shift 2
chmod u+rwx "$FAKE_SVC_ROOT"
exec "$@"
"""

pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")


def _server(tmp_path: Path) -> tuple[Path, Path]:
    """A server root on the release layout: app/releases/<tag>/ and app/current."""
    root = tmp_path / "server"
    rel = root / "app" / "releases" / "v1.0.0"
    rel.mkdir(parents=True)
    (root / "app" / "current").symlink_to(rel)
    return root, root / "app"


def _run(tmp_path: Path, root: Path, app: Path) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", SSH), ("rsync", RSYNC), ("sudo", SUDO)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    checkout = tmp_path / "checkout" / "scripts"
    checkout.mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "deploy_interactive.sh", checkout / "deploy_interactive.sh")
    log = tmp_path / "rsync.log"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOST": "eyeserver",
           "PORT": "22", "APP_DIR": str(app), "ROOT_DIR": str(root), "STAGE": str(tmp_path / "stage"),
           "RSYNC_LOG": str(log), "FAKE_SVC_ROOT": str(root)}
    r = subprocess.run(["bash", str(checkout / "deploy_interactive.sh"), "--allow-dirty"], input="tester-admin\n",
                       env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    return r, (log.read_text().splitlines() if log.exists() else [])


def test_a_server_on_releases_is_refused_before_any_rsync(tmp_path):
    root, app = _server(tmp_path)
    r, rsyncs = _run(tmp_path, root, app)
    assert r.returncode == 1
    assert "holds app releases" in r.stderr and "deployed from tags" in r.stderr
    assert rsyncs == []
    assert (app / "releases" / "v1.0.0").is_dir() and (app / "current").is_symlink()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root sees through mode 000")
def test_when_the_folder_is_closed_the_service_account_refuses_before_copying_into_it(tmp_path):
    root, app = _server(tmp_path)
    root.chmod(0)                               # closed to the operator's own account
    try:
        r, rsyncs = _run(tmp_path, root, app)
    finally:
        root.chmod(stat.S_IRWXU)
    assert r.returncode == 1
    assert "cannot see" in r.stdout
    assert "holds app releases" in r.stderr
    # Only the copy into the operator's own staging folder ran, never one into the app folder.
    assert len(rsyncs) == 1 and rsyncs[0].endswith(f":{tmp_path / 'stage'}/")
    assert str(app) not in rsyncs[0]
    for kept in ("/releases/", "/current", "/previous", "/held"):
        assert f"--exclude {kept}" in rsyncs[0]
    assert (app / "releases" / "v1.0.0").is_dir() and (app / "current").is_symlink()
