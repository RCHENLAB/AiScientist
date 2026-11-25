#!/usr/bin/env bash
#
# Start a service image as a Kubernetes deployment would, and check that it serves:
#
#   deploy/smoke_test_image.sh <image> <tag>
#
# The container gets a PostgreSQL of its own and an empty data volume prepared as a pod's volume
# with fsGroup 10001 is (owned by root, group 10001, group-writable), so the app has to create its
# state and data folders itself. Then:
#   - /health answers within AISCIENTIST_SMOKE_TIMEOUT seconds (default 240): status ok, the version
#     <tag>, accounts on, and the skills release the image's configs/skills_registry.json pins;
#   - the console page and /api/auth/config answer;
#   - the database is migrated: `aiscientist-admin active-runs` answers 0;
#   - the app runs as uid 10001, cannot write its own code, and keeps state/ at 0700 and the session
#     secret at 0600;
#   - after a restart (a replaced pod) /health answers again and the session secret is the same.
# Everything it starts is removed at the end. Needs docker, curl and python3.
set -euo pipefail

[ $# -eq 2 ] || { sed -n 2,17p "$0" >&2; exit 2; }
IMAGE="$1"; TAG="$2"
TIMEOUT="${AISCIENTIST_SMOKE_TIMEOUT:-240}"
RUN_ID="aisc-smoke-$$"
NET="$RUN_ID-net"; PG="$RUN_ID-pg"; APP="$RUN_ID-app"; VOL="$RUN_ID-data"
PGPASS="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"

cleanup() {
  docker rm -f "$APP" "$PG" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
fail() {
  echo "SMOKE TEST FAILED: $*" >&2
  echo "--- last lines of the app's log" >&2
  docker logs --tail 60 "$APP" >&2 2>&1 || true
  exit 1
}
ok() { echo "  ok  $*"; }

echo "==> PostgreSQL"
docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" -e POSTGRES_USER=aiscientist -e POSTGRES_DB=aiscientist \
  -e POSTGRES_PASSWORD="$PGPASS" postgres:16-alpine >/dev/null
for _ in $(seq 1 60); do
  docker exec "$PG" pg_isready -q -U aiscientist -d aiscientist 2>/dev/null && break
  sleep 1
done
docker exec "$PG" pg_isready -q -U aiscientist -d aiscientist || fail "PostgreSQL did not start"

echo "==> an empty volume, as a pod with fsGroup 10001 gets it"
docker volume create "$VOL" >/dev/null
docker run --rm --user 0 -v "$VOL:/v" --entrypoint sh "$IMAGE" -c 'chown 0:10001 /v && chmod 2775 /v'

start_app() {
  docker run -d --name "$APP" --network "$NET" -p 127.0.0.1::8800 \
    --mount "type=volume,src=$VOL,dst=/data/aiscientist,volume-nocopy" \
    -e AISCIENTIST_DATABASE_URL="postgresql+psycopg://aiscientist:$PGPASS@$PG:5432/aiscientist" \
    "$IMAGE" >/dev/null
}

wait_health() {
  local deadline=$((SECONDS + TIMEOUT)) port body
  while [ "$SECONDS" -lt "$deadline" ]; do
    [ "$(docker inspect -f '{{.State.Running}}' "$APP" 2>/dev/null)" = true ] || fail "the container stopped"
    port="$(docker port "$APP" 8800/tcp 2>/dev/null | head -n 1 | sed 's/.*://')"
    if [ -n "$port" ] && body="$(curl -fsS --max-time 5 "http://127.0.0.1:$port/health" 2>/dev/null)"; then
      BASE="http://127.0.0.1:$port"; HEALTH="$body"; return 0
    fi
    sleep 2
  done
  fail "/health did not answer within ${TIMEOUT}s"
}

echo "==> the app, from $IMAGE"
start_app
wait_health
echo "  /health: $HEALTH"
PINNED="$(docker exec "$APP" python -c 'import json; print(json.load(open("/app/configs/skills_registry.json"))["release_tag"])')"
python3 - "$HEALTH" "$TAG" "$PINNED" <<'PY' || fail "/health does not report what the release should"
import json, sys
health, tag, skills = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
problems = [f"{key} is {health.get(key)!r}, not {want!r}"
            for key, want in (("status", "ok"), ("version", tag), ("accounts", True), ("skills", skills))
            if health.get(key) != want]
if problems:
    sys.exit("; ".join(problems))
PY
ok "/health: version $TAG, accounts on, skills $PINNED"

page="$(curl -fsS --max-time 10 "$BASE/")" || fail "the console page did not answer"
grep -qi "<html" <<<"$page" || fail "the console page is not HTML"
ok "the console page"
curl -fsS --max-time 10 "$BASE/api/auth/config" | python3 -c 'import json,sys; json.load(sys.stdin)' \
  || fail "/api/auth/config did not answer with JSON"
ok "/api/auth/config"

runs="$(docker exec "$APP" aiscientist-admin active-runs 2>/dev/null | tail -n 1)" || fail "aiscientist-admin active-runs failed"
[ "$runs" = 0 ] || fail "aiscientist-admin active-runs answered '$runs', not 0"
ok "the database is migrated (active runs: 0)"

[ "$(docker exec "$APP" id -u)" = 10001 ] || fail "the app does not run as uid 10001"
if docker exec "$APP" sh -c 'touch /app/src/.smoke' 2>/dev/null; then fail "the app can write its own code"; fi
[ "$(docker exec "$APP" stat -c %a /data/aiscientist/state)" = 700 ] || fail "state/ is not 0700"
[ "$(docker exec "$APP" stat -c %a /data/aiscientist/state/session_secret)" = 600 ] || fail "the session secret is not 0600"
SECRET_SUM="$(docker exec "$APP" sha256sum /data/aiscientist/state/session_secret)"
ok "uid 10001, read-only code, state/ 0700, session secret 0600"

echo "==> a replaced container"
docker rm -f "$APP" >/dev/null
start_app
wait_health
[ "$(docker exec "$APP" sha256sum /data/aiscientist/state/session_secret)" = "$SECRET_SUM" ] \
  || fail "the session secret changed across a restart: every user would be signed out"
ok "/health again, the same session secret"

echo "==> smoke test passed: $IMAGE"
