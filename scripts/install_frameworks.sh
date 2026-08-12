#!/usr/bin/env bash
#
# Install the REAL Biomni runtime on the eye server, under /data so the project
# tree stays tidy. Run this AFTER ./deploy.sh has built the app venv.
#
# (Kosmos is gone — the project dissolved that layer; Biomni is the research
# backend. The LLM is served by vLLM over the HPC3 tunnel, configured separately;
# see scripts/hpc3_vllm_setup.sh and configs/aiscientist.example.env.)
#
# Run as the SERVICE account (<ucinetid> in dev, bioagent in prod) — NOT root.
# One-time prerequisite (needs admin, the only sudo step): a system Python with
# venv support, plus git:   sudo apt install -y python3-venv python3-full git
#
# Idempotent: re-run any time to pull updates and refresh deps.
#
# Layout it creates (matches the runtime config defaults — no code change needed):
#     $ROOT/biomni/         # Biomni source clone, pip-installed into the app env
#     $ROOT/biomni_data/    # Biomni data lake (~11GB, only if a run opts in)
#
# Env knobs (all optional):
#     BIOAGENT_ROOT     base dir            (default /data/BioAgent)
#     BIOAGENT_ENV      app venv dir        (default $BIOAGENT_ROOT/env)
#     BIOAGENT_PYTHON   python for the app venv     (default python3)
#     BIOMNI_REPO       Biomni git URL      (default snap-stanford/Biomni)
#     SKIP_BIOMNI=1     skip the install (no-op)
#
set -euo pipefail

ROOT="${BIOAGENT_ROOT:-/data/BioAgent}"
ENV_DIR="${BIOAGENT_ENV:-$ROOT/env}"
PYTHON="${BIOAGENT_PYTHON:-python3}"
BIOMNI_REPO="${BIOMNI_REPO:-https://github.com/snap-stanford/Biomni.git}"

echo "==> AiScientist framework install (Biomni)"
echo "    root        : $ROOT"
echo "    app venv    : $ENV_DIR"
echo "    python      : $("$PYTHON" --version 2>&1)"

command -v git >/dev/null || { echo "ERROR: git not found. sudo apt install -y git" >&2; exit 1; }
mkdir -p "$ROOT"

# clone_or_update <url> <dest>
clone_or_update() {
  local url="$1" dest="$2"
  if [ -d "$dest/.git" ]; then
    echo "==> updating $(basename "$dest") (git pull)"
    git -C "$dest" pull --ff-only || echo "    (skipped pull — local changes or detached head)"
  else
    echo "==> cloning $(basename "$dest") from $url"
    git clone --depth 1 "$url" "$dest"
  fi
}

# --- Biomni: source clone + install into the APP venv ------------------------
if [ "${SKIP_BIOMNI:-0}" != "1" ]; then
  if [ ! -x "$ENV_DIR/bin/python" ]; then
    echo "ERROR: app venv missing at $ENV_DIR. Run ./deploy.sh first." >&2
    exit 1
  fi
  clone_or_update "$BIOMNI_REPO" "$ROOT/biomni"
  mkdir -p "$ROOT/biomni_data"
  echo "==> pip install -e biomni into the app venv"
  "$ENV_DIR/bin/pip" install -U pip
  "$ENV_DIR/bin/pip" install -e "$ROOT/biomni"
  echo "    note: Biomni's data lake (~11GB) downloads only if a run sets BIOAGENT_BIOMNI_LOAD_DATA_LAKE=1"
else
  echo "==> skipping Biomni (SKIP_BIOMNI=1)"
fi

cat <<EOF

==> framework install done.

Quick check Biomni is importable (no LLM needed):
    "$ENV_DIR/bin/python" -c "import biomni; print('biomni OK')"

Enable execution in .env (default stays plan-only):
    BIOAGENT_BIOMNI_EXECUTE=1   BIOAGENT_BIOMNI_RUNTIME=real

Model + endpoint are per-session, not hardcoded:
  - From the CONSOLE: the model you pick in the UI AND this session's live vLLM
    tunnel port are fed to Biomni automatically — nothing to set.
  - BIOAGENT_VLLM_MODEL (then BIOAGENT_BIOMNI_MODEL) only sets the FALLBACK default
    for non-console paths (e.g. the standalone sanity probe / debug runner).
  - Standalone probe: start the console (note its "Tunnel ready: 127.0.0.1:<port>"
    line), then:  "$ENV_DIR/bin/python" -m bioagent.framework_sanity --ollama-port <port>

On a laptop, leave EXECUTE unset (plan-only) or use BIOAGENT_BIOMNI_RUNTIME=mock.
See configs/aiscientist.example.env.
EOF
