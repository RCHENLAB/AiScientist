#!/bin/sh
#
# Entry point of the service image (deploy/Dockerfile), run by tini before the command. A new volume
# mounted at /data/aiscientist is empty: make the folders the app writes there, with the mode the lab
# server gives the state folder (0700: it holds the session secret and every user's saved HPC3 and
# API keys), then run the command.
set -eu

if [ -n "${AISCIENTIST_STATE_DIR:-}" ]; then
  mkdir -p "$AISCIENTIST_STATE_DIR"
  chmod 0700 "$AISCIENTIST_STATE_DIR"
  chmod g-s "$AISCIENTIST_STATE_DIR"
fi
if [ -n "${AISCIENTIST_DATA_ROOT:-}" ]; then
  mkdir -p "$AISCIENTIST_DATA_ROOT"
fi

exec "$@"
