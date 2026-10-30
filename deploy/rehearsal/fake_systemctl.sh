#!/usr/bin/env bash
# Stand-in for systemctl in the release-layout rehearsal (deploy/rehearsal/release_layout.sh): no root,
# no systemd. Reads units from $SYSTEMD_DIR, applies drop-ins (<unit>.d/*.conf: the last non-empty
# ExecStart= and WorkingDirectory= win, as in systemd), runs ExecStart as the caller in the background
# and answers the verbs the scripts use. Every call is logged to $SYSTEMD_DIR/calls.log.
set -uo pipefail
D="${SYSTEMD_DIR:?}"
echo "$*" >> "$D/calls.log"
cmd="${1:-}"; shift || true
[ "${1:-}" = "--now" ] && { now=1; shift; } || now=0
u="${1:-}"

value() {   # value <key>: the effective setting of unit $u
  local f
  for f in "$D/$u" "$D/$u.d"/*.conf; do [ -f "$f" ] && sed -n "s/^$1=//p" "$f"; done | grep -v '^$' | tail -n 1
}
start() {
  [ -f "$D/$u" ] || { echo "Unit $u not found." >&2; return 5; }
  local wd ex
  wd="$(value WorkingDirectory)"; ex="$(value ExecStart)"
  cd "$wd" 2>/dev/null || { echo "Failed at step CHDIR spawning: $wd" >&2; return 200; }
  nohup $ex >> "$D/$u.log" 2>&1 < /dev/null &
  echo $! > "$D/$u.pid"
}
stop() {
  [ -f "$D/$u.pid" ] || return 0
  local p; p="$(cat "$D/$u.pid")"
  kill -INT "$p" 2>/dev/null
  for _ in $(seq 100); do kill -0 "$p" 2>/dev/null || break; sleep 0.1; done
  kill -9 "$p" 2>/dev/null
  rm -f "$D/$u.pid"
}
case "$cmd" in
  daemon-reload) exit 0 ;;
  enable)  touch "$D/enabled.$u"; [ "$now" = 1 ] && touch "$D/active.$u"; exit 0 ;;
  disable) rm -f "$D/enabled.$u" "$D/active.$u"; exit 0 ;;
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  is-active) [ -f "$D/$u.pid" ] && kill -0 "$(cat "$D/$u.pid")" 2>/dev/null && { echo active; exit 0; }; echo inactive; exit 3 ;;
  cat)     cat "$D/$u" "$D/$u.d"/*.conf 2>/dev/null ;;
  *) echo "fake systemctl: unsupported: $cmd" >&2; exit 1 ;;
esac
