"""deploy/deploy_release.sh and deploy/deploy_skills.sh end to end, without a network or a real venv.

A release package is a tarball with a VERSION file; the "app" is a tiny HTTP server that answers
/health the way the gateway does (compact JSON with the release tag, whether accounts are on, and
the skills release skills/current points at). A package marked BROKEN does not start; one marked
NO_ACCOUNTS starts with accounts off, as a venv without the auth extra would. A skills release
marked REFUSE is not loaded by the app. The venv's python and pip are stand-ins that record how
they were called. The scripts run with GNU coreutils (CI); elsewhere the tests skip.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SCRIPT = DEPLOY / "deploy_release.sh"
SKILLS_SCRIPT = DEPLOY / "deploy_skills.sh"
SRC = Path(__file__).resolve().parents[1] / "src"

FAKE_APP = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

current, port = Path(sys.argv[1]), int(sys.argv[2])
root = current.parents[1]
if (current / "BROKEN").exists():
    sys.exit(3)
tag = dict(l.split("=", 1) for l in (current / "VERSION").read_text().split()).get("tag")

def skills():
    link = root / "skills" / "current"
    if not link.is_symlink() or (link / "REFUSE").exists():
        return None
    return "v" + os.path.basename(os.readlink(link))

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "version": tag, "accounts": not (current / "NO_ACCOUNTS").exists(),
                           "skills": skills()}, separators=(",", ":")).encode()
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

HTTPServer(("127.0.0.1", port), H).serve_forever()
'''

# Stands in for `python3 -m venv DIR`: a venv whose pip and python record their arguments. The venv
# python answers the version question, `active-runs` (FAKE_ACTIVE_RUNS) and records `migrate`.
FAKE_PYTHON = r"""#!/bin/bash
[ "$1" = "-m" ] && [ "$2" = "venv" ] || exit 1
mkdir -p "$3/bin"
cat > "$3/bin/pip" <<'EOF'
#!/bin/bash
echo "pip $*" >> "$FAKE_CALLS"
EOF
cat > "$3/bin/python" <<'EOF'
#!/bin/bash
case "$*" in
  *version_info*) echo "${FAKE_PY_VERSION:-3.11}" ;;
  *"admin_cli active-runs"*) echo "${FAKE_ACTIVE_RUNS:-0}" ;;
  *"admin_cli migrate"*) echo "migrate $PWD" >> "$FAKE_CALLS"; echo "database schema: 0001" ;;
esac
exit 0
EOF
chmod +x "$3/bin/pip" "$3/bin/python"
"""

RESTART = """#!/bin/bash
echo "app-restart" >> "$FAKE_CALLS"
if [ -f "{pid}" ]; then
  p=$(cat "{pid}"); kill "$p" 2>/dev/null
  while kill -0 "$p" 2>/dev/null; do sleep 0.1; done
fi
nohup "{py}" "{app}" "{root}/app/current" {port} > "{log}" 2>&1 < /dev/null &
echo $! > "{pid}"
# Like the unit's ExecStartPost: return once the app answers (or after ~3 s).
for _ in $(seq 30); do curl -fs -o /dev/null "http://127.0.0.1:{port}/health" && break; sleep 0.1; done
"""

# The app's check of a skills release, run with this checkout's code.
CHECK_PY = """#!/bin/bash
PYTHONPATH="{src}" exec "{py}" "$@"
"""


def _gnu_tools() -> bool:
    if not all(shutil.which(t) for t in ("bash", "curl", "sha256sum", "tar")):
        return False
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d, "a"), Path(d, "b")
        os.symlink("x", a)
        os.symlink("y", b)
        r = subprocess.run(["mv", "-Tf", str(a), str(b)], capture_output=True)
        return r.returncode == 0 and os.readlink(b) == "x"


pytestmark = pytest.mark.skipif(not _gnu_tools(), reason="the deploy scripts need GNU coreutils (mv -T)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = tmp / "srv"
        self.pkgs = tmp / "pkgs"
        self.skill_pkgs = tmp / "skill-pkgs"
        self.port = _free_port()
        self.pid = tmp / "app.pid"
        self.calls = tmp / "calls.log"
        (self.root / "config").mkdir(parents=True)
        (self.root / "skills" / "releases").mkdir(parents=True)
        self.env_file = self.root / "config" / ".env"
        self.env_file.write_text(f"AISCIENTIST_RESULTS_DIR={tmp / 'results'}\n")
        genesets = self.root / "data" / "reference" / "genesets"
        genesets.mkdir(parents=True)
        (genesets / "Reactome_2022.gmt").write_text("PATHWAY\tdesc\tGENE1\tGENE2\n")
        self.pkgs.mkdir()
        (tmp / "fake_app.py").write_text(FAKE_APP)
        py = tmp / "fake_python"
        py.write_text(FAKE_PYTHON)
        py.chmod(0o755)
        check = tmp / "check_python"
        check.write_text(CHECK_PY.format(src=SRC, py=sys.executable))
        check.chmod(0o755)
        restart = tmp / "restart.sh"
        restart.write_text(RESTART.format(pid=self.pid, py=sys.executable, app=tmp / "fake_app.py",
                                          root=self.root, port=self.port, log=tmp / "app.log"))
        restart.chmod(0o755)
        self.env = {**os.environ,
                    "AISCIENTIST_ROOT": str(self.root), "AISCIENTIST_PACKAGE_DIR": str(self.pkgs),
                    "AISCIENTIST_SKILLS_PACKAGE_DIR": str(self.skill_pkgs),
                    "AISCIENTIST_SKILLS_PYTHON": str(check),
                    "AISCIENTIST_HOST": "127.0.0.1", "AISCIENTIST_PORT": str(self.port),
                    "AISCIENTIST_PYTHON": str(py), "AISCIENTIST_RESTART_CMD": str(restart),
                    "AISCIENTIST_HEALTH_TIMEOUT": "4", "AISCIENTIST_SERVICE": "aiscientist-test.service",
                    "FAKE_CALLS": str(self.calls)}

    def package(self, tag: str, *markers: str, skills_tag: str = "v0.1.0", lock: str | None = None) -> None:
        stage = self.tmp / "stage" / f"aiscientist-{tag}"
        stage.mkdir(parents=True)
        (stage / "VERSION").write_text(f"tag={tag}\nsha={'a' * 40}\n")
        (stage / "configs").mkdir()
        (stage / "configs" / "skills_registry.json").write_text(
            json.dumps({"release_tag": skills_tag, "allowed_tiers": ["core"], "releases_root": "skills/releases"}))
        (stage / "src" / "aiscientist" / "gateway").mkdir(parents=True)
        (stage / "src" / "aiscientist" / "gateway" / "admin_cli.py").write_text('sub.add_parser("migrate")\n')
        if lock is not None:
            (stage / "requirements.lock").write_text(lock)
        for m in markers:
            (stage / m).write_text("")
        name = f"aiscientist-{tag}.tar.gz"
        with tarfile.open(self.pkgs / name, "w:gz") as tf:
            tf.add(stage, arcname=stage.name)
        digest = hashlib.sha256((self.pkgs / name).read_bytes()).hexdigest()
        with open(self.pkgs / "SHA256SUMS", "a") as f:
            f.write(f"{digest}  {name}\n")

    def skills_package(self, tag: str, *markers: str, catalog_version: str | None = None) -> None:
        """A skills release as the registry publishes it, into the folder the script reads."""
        if self.skill_pkgs.exists():
            shutil.rmtree(self.skill_pkgs)
        self.skill_pkgs.mkdir()
        version = tag[1:]
        (self.skill_pkgs / "catalog.json").write_text(json.dumps(
            {"schema_version": 1, "registry": "aiscientist-skills",
             "registry_version": catalog_version or version, "skills": []}, indent=2))
        (self.skill_pkgs / f"aiscientist-skills-{version}.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
        files = ["catalog.json", f"aiscientist-skills-{version}.zip", *markers]
        for m in markers:
            (self.skill_pkgs / m).write_text("")
        (self.skill_pkgs / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256((self.skill_pkgs / n).read_bytes()).hexdigest()}  {n}\n" for n in files))

    def run(self, *args: str, script: Path = SCRIPT, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(script), *args], env={**self.env, **env}, capture_output=True,
                              text=True, timeout=120)

    def link(self, name: str, where: str = "app") -> str | None:
        p = self.root / where / name
        return os.path.basename(os.readlink(p)) if p.is_symlink() else None

    def served(self) -> str:
        return subprocess.run(["curl", "-fsS", f"http://127.0.0.1:{self.port}/health"],
                              capture_output=True, text=True).stdout

    def held(self, where: str = "app") -> list[str]:
        p = self.root / where / "held"
        return p.read_text().split() if p.exists() else []

    def stop(self) -> None:
        if self.pid.exists():
            try:
                os.kill(int(self.pid.read_text()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        for p in self.root.rglob("*"):          # read-only releases: let pytest clean up
            if p.is_dir() and not p.is_symlink():
                p.chmod(p.stat().st_mode | stat.S_IWUSR)


@pytest.fixture
def server(tmp_path):
    s = Server(tmp_path)
    yield s
    s.stop()


def test_install_switch_back_refuse_and_roll_back(server):
    for tag in ("v1.0.0", "v1.0.1"):
        server.package(tag)
    server.package("v1.0.2", "BROKEN")
    server.package("v1.0.3", "NO_ACCOUNTS")

    r = server.run("v1.0.0")
    assert r.returncode == 0, r.stderr
    assert (server.link("current"), server.link("previous")) == ("v1.0.0", None)
    assert os.readlink(server.root / "app" / "releases" / "v1.0.0" / ".env") == str(server.env_file)
    assert '"version":"v1.0.0"' in server.served()

    r = server.run("v1.0.1")
    assert r.returncode == 0, r.stderr
    assert (server.link("current"), server.link("previous")) == ("v1.0.1", "v1.0.0")

    # A release that does not start: back to v1.0.1, and previous still names v1.0.0, so a later
    # --rollback never lands on the release that just failed. It is held, so --auto leaves it.
    r = server.run("v1.0.2")
    assert r.returncode == 1
    assert "health check failed for v1.0.2" in r.stderr
    assert (server.link("current"), server.link("previous")) == ("v1.0.1", "v1.0.0")
    assert '"version":"v1.0.1"' in server.served()
    assert (server.root / "app" / "releases" / "v1.0.2").is_dir()      # kept for inspection
    assert server.held() == ["v1.0.2"]

    # A release that starts with accounts off is not kept: on the website it would have no login.
    r = server.run("v1.0.3")
    assert r.returncode == 1
    assert "accounts off" in r.stderr
    assert (server.link("current"), server.link("previous")) == ("v1.0.1", "v1.0.0")
    assert '"version":"v1.0.1"' in server.served()

    r = server.run("--rollback")
    assert r.returncode == 0, r.stderr
    assert (server.link("current"), server.link("previous")) == ("v1.0.0", "v1.0.1")
    assert '"version":"v1.0.0"' in server.served()
    assert server.held() == ["v1.0.2", "v1.0.3", "v1.0.1"]

    listing = server.run("--list").stdout
    assert "v1.0.0  <- current" in listing and "v1.0.1  <- previous  (held)" in listing

    r = server.run("v1.0.0")
    assert r.returncode == 0 and "already the current release" in r.stdout

    log = (server.root / "logs" / "deploy.log").read_text()
    assert "app switched v1.0.0 -> v1.0.1" in log and "app rolled back v1.0.1 -> v1.0.0" in log
    assert "app refused v1.0.2" in log


def test_refuses_without_config_or_results_dir_or_a_matching_checksum(server):
    server.package("v1.0.0")

    server.env_file.write_text("AISCIENTIST_STATE_DIR=/tmp/x\n")
    r = server.run("v1.0.0")
    assert r.returncode != 0 and "AISCIENTIST_RESULTS_DIR" in r.stderr

    server.env_file.unlink()
    r = server.run("v1.0.0")
    assert r.returncode != 0 and "not found" in r.stderr

    server.env_file.write_text("BIOAGENT_RESULTS_DIR=/tmp/results\n")     # the old name still counts
    (server.pkgs / "SHA256SUMS").write_text(f"{'0' * 64}  aiscientist-v1.0.0.tar.gz\n")
    r = server.run("v1.0.0")
    assert r.returncode != 0 and "checksum verification failed" in r.stderr
    assert server.link("current") is None
    assert not (server.root / "app" / "releases" / "v1.0.0").exists()


def test_a_release_is_installed_from_its_lock_migrated_and_made_read_only(server):
    lock = "# python: 3.11 (x86_64-manylinux_2_28)\nfastapi==0.115.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
    server.package("v1.1.0", lock=lock)

    r = server.run("--prepare", "v1.1.0")
    assert r.returncode == 0, r.stderr
    assert server.link("current") is None                    # prepared: nothing switched or restarted
    rel = server.root / "app" / "releases" / "v1.1.0"
    calls = server.calls.read_text()
    assert f"pip install -q --require-hashes -r {rel}/requirements.lock" in calls
    assert f"pip install -q --no-deps --no-build-isolation -e {rel}" in calls
    assert f"migrate {rel}" in calls
    assert (rel / "src/aiscientist/tools/genesets/Reactome_2022.gmt").read_text().startswith("PATHWAY")
    assert not (rel / "src/aiscientist/tools/genesets/Reactome_2022.gmt").is_symlink()
    assert os.readlink(rel / "skills" / "current") == str(server.root / "skills" / "current")
    assert not os.access(rel / "VERSION", os.W_OK) and not os.access(rel / "src", os.W_OK)

    server.calls.write_text("")
    r = server.run("v1.1.0")                                  # installed already: switch and migrate only
    assert r.returncode == 0, r.stderr
    assert server.link("current") == "v1.1.0"
    calls = server.calls.read_text()
    assert "pip" not in calls and f"migrate {rel}" in calls

    server.package("v1.1.1", lock=lock.replace("3.11", "3.12"))
    r = server.run("v1.1.1")
    assert r.returncode != 0 and "requirements.lock is for Python 3.12" in r.stderr
    assert server.link("current") == "v1.1.0"


def test_a_switch_waits_for_runs_in_progress(server):
    server.package("v1.0.0")
    server.package("v1.0.1")
    assert server.run("v1.0.0").returncode == 0

    r = server.run("v1.0.1", FAKE_ACTIVE_RUNS="2")
    assert r.returncode == 1 and "2 run(s) in progress" in r.stderr
    assert server.link("current") == "v1.0.0"
    assert (server.root / "app" / "releases" / "v1.0.1" / ".installed").exists()   # ready for later

    r = server.run("v1.0.1", FAKE_ACTIVE_RUNS="2", AISCIENTIST_FORCE="1")
    assert r.returncode == 0, r.stderr
    assert server.link("current") == "v1.0.1"


def test_auto_installs_the_newest_release_unless_held_busy_or_older(server):
    server.skills_package("v0.1.0")              # the skills release every package here pins
    server.package("v1.0.0")
    r = server.run("--auto")
    assert r.returncode == 0, r.stderr
    assert server.link("current") == "v1.0.0"

    server.package("v1.2.0")
    server.package("v1.10.0")                  # newer than v1.2.0: versions sort as numbers
    r = server.run("--auto", FAKE_ACTIVE_RUNS="1")
    assert r.returncode == 0 and "1 run(s) in progress" in r.stdout
    assert server.link("current") == "v1.0.0"

    r = server.run("--auto")
    assert r.returncode == 0, r.stderr
    assert server.link("current") == "v1.10.0"

    assert server.run("--rollback").returncode == 0
    assert server.link("current") == "v1.0.0"
    r = server.run("--auto")                   # rolled back from: held, not installed again
    assert r.returncode == 0 and "v1.10.0 is held" in r.stdout
    assert server.link("current") == "v1.0.0"

    r = server.run("v1.10.0")                  # by name: taken off the list
    assert r.returncode == 0, r.stderr
    assert server.link("current") == "v1.10.0" and server.held() == []

    (server.root / "config" / "deploy.env").write_text("AISCIENTIST_APP_TAG=v1.2.0\n")
    r = server.run("--auto")                   # a pin in config/deploy.env wins over the newest
    assert r.returncode == 0, r.stderr
    assert server.link("current") == "v1.2.0"


def test_skills_are_installed_checked_switched_and_rolled_back(server):
    server.package("v1.0.0")
    assert server.run("v1.0.0").returncode == 0

    server.skills_package("v0.1.0")
    r = server.run("v0.1.0", script=SKILLS_SCRIPT)
    assert r.returncode == 0, r.stderr
    assert server.link("current", "skills") == "0.1.0"
    assert '"skills":"v0.1.0"' in server.served()
    rel = server.root / "skills" / "releases" / "0.1.0"
    assert sorted(p.name for p in rel.iterdir()) == ["SHA256SUMS", "aiscientist-skills-0.1.0.zip", "catalog.json"]
    assert not os.access(rel / "catalog.json", os.W_OK)
    assert stat.S_IMODE(rel.stat().st_mode) == 0o555             # read-only, readable like skills/
    assert json.loads((rel.parent / "0.1.0.install.json").read_text())["tag"] == "v0.1.0"

    server.skills_package("v0.2.0")
    r = server.run("v0.2.0", script=SKILLS_SCRIPT)
    assert r.returncode == 0, r.stderr
    assert (server.link("current", "skills"), server.link("previous", "skills")) == ("0.2.0", "0.1.0")
    assert '"skills":"v0.2.0"' in server.served()

    # A release the app does not load: back to the one that ran, and held.
    server.skills_package("v0.3.0", "REFUSE")
    r = server.run("v0.3.0", script=SKILLS_SCRIPT)
    assert r.returncode == 1 and "did not load skills v0.3.0" in r.stderr
    assert (server.link("current", "skills"), server.link("previous", "skills")) == ("0.2.0", "0.1.0")
    assert server.held("skills") == ["v0.3.0"]

    r = server.run("--rollback", script=SKILLS_SCRIPT)
    assert r.returncode == 0, r.stderr
    assert server.link("current", "skills") == "0.1.0" and '"skills":"v0.1.0"' in server.served()
    assert "v0.2.0  <- previous  (held)" in server.run("--list", script=SKILLS_SCRIPT).stdout


def test_a_skills_release_that_fails_its_check_is_never_installed(server):
    server.skills_package("v0.4.0", catalog_version="0.3.9")      # the catalog of another release
    r = server.run("v0.4.0", script=SKILLS_SCRIPT)
    assert r.returncode == 1 and "does not match" in r.stderr
    assert not (server.root / "skills" / "releases" / "0.4.0").exists()

    server.skills_package("v0.4.0")
    (server.skill_pkgs / "catalog.json").write_text("{}")          # changed after SHA256SUMS
    r = server.run("v0.4.0", script=SKILLS_SCRIPT)
    assert r.returncode == 1 and "checksum verification failed" in r.stderr
    assert not (server.root / "skills" / "releases" / "0.4.0").exists()
    assert server.link("current", "skills") is None


def test_auto_brings_the_skills_to_the_release_the_app_pins(server):
    server.package("v1.0.0", skills_tag="v0.1.0")
    server.skills_package("v0.1.0")
    r = server.run("--auto")
    assert r.returncode == 0, r.stderr
    assert (server.link("current"), server.link("current", "skills")) == ("v1.0.0", "0.1.0")

    server.skills_package("v0.2.0")
    (server.root / "config" / "deploy.env").write_text('AISCIENTIST_SKILLS_TAG="v0.2.0"\n')
    r = server.run("--auto", FAKE_ACTIVE_RUNS="1")         # a skills switch waits for runs too
    assert r.returncode == 0 and "run(s) in progress" in r.stdout
    assert server.link("current", "skills") == "0.1.0"
    r = server.run("--auto")
    assert r.returncode == 0, r.stderr
    assert server.link("current", "skills") == "0.2.0" and '"skills":"v0.2.0"' in server.served()


def test_auto_with_nothing_to_install_is_not_a_failure(server):
    """No release yet and no package to install (the timer's first start, or a latest release made
    before the workflow existed): nothing happens, and the timer's run is not marked failed."""
    r = server.run("--auto")
    assert r.returncode == 0, r.stderr
    assert "no release to install" in r.stdout
    assert server.link("current") is None


def test_the_key_service_restarts_before_the_app(server):
    """With aiscientist-keys.service installed, a switch and a rollback restart it first: it runs the
    release's code, and the app reaches the API keys through it."""
    keys = server.tmp / "restart_keys.sh"
    keys.write_text('#!/bin/bash\necho "keys-restart" >> "$FAKE_CALLS"\n')
    keys.chmod(0o755)
    server.package("v1.0.0")
    server.package("v1.0.1")
    for args in (("v1.0.0",), ("v1.0.1",), ("--rollback",)):
        r = server.run(*args, AISCIENTIST_KEYS_RESTART_CMD=str(keys))
        assert r.returncode == 0, r.stderr
    restarts = [line for line in server.calls.read_text().split() if line.endswith("-restart")]
    assert restarts == ["keys-restart", "app-restart"] * 3
