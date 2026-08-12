#!/usr/bin/env bash
#
# Start the AiScientist console (the web server). Run this AFTER ./deploy.sh.
#
# Run as the service account (<ucinetid> in dev, bioagent in prod) — NOT root.
# The process keeps running and listening until you stop it (Ctrl-C). For an
# always-on, auto-restart production service, use systemd instead (see below).
#
# On start it first STOPS any previous AiScientist console (so you never stack
# workers on the same port) and prints a code fingerprint so you can tell which
# build is live. Set BIOAGENT_NO_KILL=1 to skip the stop step.
#
# Configurable via the same env vars as deploy.sh:
#     BIOAGENT_ROOT  base dir   (default /data/BioAgent)
#     BIOAGENT_APP   app dir    (default $ROOT/app)
#     BIOAGENT_ENV   venv dir   (default $ROOT/env)
#     BIOAGENT_HOST  bind host  (default 127.0.0.1 — localhost only, SAFE)
#     BIOAGENT_PORT  bind port  (default 8800)
#
# SECURITY: the default 127.0.0.1 binds localhost only, so the console is NOT
# reachable from the network — reach it through an SSH tunnel (below) or an nginx
# reverse proxy. Only set BIOAGENT_HOST=0.0.0.0 once the console has web auth +
# a firewall + HTTPS in front; bare 0.0.0.0 on a public IP exposes it to anyone.
#
# View it from a laptop (the eye server has no GUI browser):
#     ssh -p <ADMIN_SSH_PORT> -L 8800:localhost:8800 <you>@<this-host>
#     # then open http://localhost:8800/
#
# Production (always-on) instead of this script — /etc/systemd/system/bioagent.service:
#     [Service]
#     User=bioagent
#     WorkingDirectory=/data/BioAgent/app
#     ExecStart=/data/BioAgent/env/bin/python -m bioagent.gateway --host 127.0.0.1 --port 8800
#     # ^ bind localhost; put nginx (HTTPS + auth) in front and proxy_pass to it
#     Restart=always
#     [Install]
#     WantedBy=multi-user.target
#   then: sudo systemctl enable --now bioagent
#
set -euo pipefail

ROOT="${BIOAGENT_ROOT:-/data/BioAgent}"
APP="${BIOAGENT_APP:-$ROOT/app}"
ENV_DIR="${BIOAGENT_ENV:-$ROOT/env}"
HOST="${BIOAGENT_HOST:-127.0.0.1}"
PORT="${BIOAGENT_PORT:-8800}"
# Dev hot-reload: BIOAGENT_RELOAD=1 ./start.sh  → uvicorn auto-restarts the worker when
# a backend .py changes (e.g. after ./scripts/push.sh). NOTE: a reload drops in-memory
# state — any live HPC3 connection/tunnel dies and must be reconnected, so use it for
# dev iteration, NOT while a research run is in flight. (Frontend HTML/JS/CSS is always
# hot — it's read from disk per request, so just refresh the browser.)
RELOAD_FLAG=""
[ "${BIOAGENT_RELOAD:-0}" = "1" ] && RELOAD_FLAG="--reload"

if [ ! -x "$ENV_DIR/bin/python" ] || ! "$ENV_DIR/bin/python" -c "import bioagent" 2>/dev/null; then
  echo "ERROR: AiScientist isn't installed in $ENV_DIR. Run ./deploy.sh first." >&2
  exit 1
fi

# --- stop any previous console first ----------------------------------------
# The #1 dev annoyance: each ./start.sh stacked a new worker while the old one
# kept the port, so you'd hit "address already in use" and never know which
# build was live. We now stop our own previous gateway BEFORE starting. Set
# BIOAGENT_NO_KILL=1 to skip (e.g. if you intend to run a second instance on a
# different port). Only ever kills OUR gateway — never a stranger on the port.
if [ "${BIOAGENT_NO_KILL:-0}" != "1" ]; then
  # `[b]ioagent` keeps pgrep from matching its own command line.
  old="$(pgrep -f "[b]ioagent\.gateway" || true)"
  if [ -n "$old" ]; then
    echo "==> stopping previous console (pid: $(echo "$old" | tr '\n' ' '))"
    # shellcheck disable=SC2086
    kill $old 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      pgrep -f "[b]ioagent\.gateway" >/dev/null 2>&1 || break
      sleep 0.5
    done
    old="$(pgrep -f "[b]ioagent\.gateway" || true)"
    if [ -n "$old" ]; then
      echo "    still alive after SIGTERM — forcing (kill -9)"
      # shellcheck disable=SC2086
      kill -9 $old 2>/dev/null || true
      sleep 1
    fi
  fi
fi

# If the port is STILL held by something that ISN'T our gateway, say so clearly
# (and refuse to kill a stranger) instead of dying on an opaque bind error.
holder=""
if command -v lsof >/dev/null 2>&1; then
  holder="$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
elif command -v fuser >/dev/null 2>&1; then
  holder="$(fuser "$PORT"/tcp 2>/dev/null | tr -d ' ' || true)"
fi
if [ -n "${holder// /}" ]; then
  echo "ERROR: port $PORT is still in use by PID(s) $holder, which is NOT a AiScientist console." >&2
  echo "       Inspect with: lsof -i tcp:$PORT   — stop it manually, then retry." >&2
  exit 1
fi

# --- which build is this? print a fingerprint so a restart is verifiable ------
# Works even though push.sh rsyncs source but NOT .git: the fingerprint is a hash
# of the deployed bioagent/*.py, so it changes whenever new code lands. If a real
# git checkout is present (local dev), show the commit too.
gitline=""
# Only trust the commit when the tree is CLEAN. On the server push.sh rsyncs source
# but not .git, so .git is a stale old checkout and rsync leaves it "dirty" — its HEAD
# is NOT what we just deployed, so showing it would mislead. A clean tree (local dev)
# means the commit really matches the files; the fingerprint is the truth either way.
if command -v git >/dev/null 2>&1 && git -C "$APP" rev-parse --git-dir >/dev/null 2>&1 \
   && git -C "$APP" diff --quiet 2>/dev/null; then
  gitline="$(git -C "$APP" log -1 --format='%h %cd' --date=short 2>/dev/null || true)"
fi
fp="$("$ENV_DIR/bin/python" - "$APP" <<'PY'
import hashlib, glob, os, sys, datetime as dt
root = sys.argv[1]
h, newest = hashlib.sha256(), 0.0
for f in sorted(glob.glob(os.path.join(root, "src", "bioagent", "**", "*.py"), recursive=True)):
    h.update(open(f, "rb").read())
    newest = max(newest, os.path.getmtime(f))
ts = dt.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M") if newest else "?"
print(f"{h.hexdigest()[:12]} (newest src {ts})")
PY
)" || fp="?"
echo "==> AiScientist code fingerprint: $fp${gitline:+   git: $gitline}"

# Activate the venv so any child process (and your shell, if you `source` this)
# inherits it. Not strictly required — we exec the venv's python by absolute path
# below either way — but it makes the activation explicit. `set +u` guards the
# activate script's unbound PS1 reference under `set -u`.
if [ -f "$ENV_DIR/bin/activate" ]; then
  set +u
  # shellcheck disable=SC1091
  source "$ENV_DIR/bin/activate"
  set -u
fi

echo "==> starting AiScientist console on http://$HOST:$PORT/  (Ctrl-C to stop)"
echo "    from a laptop: ssh -p <ADMIN_SSH_PORT> -L $PORT:localhost:$PORT <you>@$(hostname)  then open http://localhost:$PORT/"
cd "$APP"
exec "$ENV_DIR/bin/python" -m bioagent.gateway --host "$HOST" --port "$PORT" $RELOAD_FLAG
