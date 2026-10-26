#!/usr/bin/env bash
# Rehearse scripts/migrate_prod_root.sh end to end in a throwaway container, as root, unmodified.
#
#     deploy/rehearsal/run.sh                 # needs Docker; ~4 min (apt + a real editable venv)
#     IMAGE=postgres:15 deploy/rehearsal/run.sh   # any Debian/Ubuntu image with apt and Python >= 3.10
#
# setup.sh builds a miniature of the prod host: the `aiscientist` account, /data/BioAgent with the
# same layout and ACLs (group `users` rwx), a REAL `pip install -e` venv, the real gateway running
# under bioagent.service, a database and a run_state.json holding old-root paths, and two sudoers
# files that name the old unit. drive.sh then runs the script through every mode and asserts the
# outcome: preflight, the refusal while a run is writing, --apply, a refused second --apply,
# --rollback, --apply again, --harden. `systemctl` here is a stand-in (containers have no systemd):
# it runs a unit's ExecStart as its User in its WorkingDirectory, so the service that answers the
# health check is the real gateway started from the new root.
#
# Not covered: real systemd, PostgreSQL (the rehearsal database is SQLite; the two UPDATE
# statements were previewed read-only against prod's Postgres), and prod's actual sudoers files,
# which only root can read — the preflight prints them.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
IMAGE="${IMAGE:-debian:trixie}"
NAME="aisci-rehearsal-$$"
W="$(mktemp -d)"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$W"' EXIT

git -C "$REPO" archive HEAD -o "$W/src.tar"            # the committed tree becomes the "deployed" app
cp "$REPO/scripts/migrate_prod_root.sh" "$HERE/systemctl" "$HERE/setup.sh" "$HERE/drive.sh" "$W/"
# SYS_PTRACE: lets root in the container read another user's /proc/<pid>/cwd, as root on a host can.
docker run -d --name "$NAME" --cap-add SYS_PTRACE --entrypoint sleep -v "$W":/in:ro "$IMAGE" infinity >/dev/null
docker exec "$NAME" bash /in/setup.sh
docker exec "$NAME" bash /in/drive.sh
