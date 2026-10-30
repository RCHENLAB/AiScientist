#!/usr/bin/env bash
#
# Rehearse deploy/migrate_release_layout.sh and the deploys after it on a scratch root, as an ordinary
# user: no root, no systemd (deploy/rehearsal/fake_systemctl.sh stands in), the real gateway. It
# builds a miniature of the Eye Server in its rsync layout (the code prod runs, its own venv, a
# SQLite database with rows, gene sets, the skills release), then runs every mode and checks it:
# preflight, --prepare (the site stays up), the refusal while a run is in progress, --apply, an
# app update and its rollback through deploy_release.sh, a skills switch through deploy_skills.sh,
# the migration's --rollback, and --apply again.
#
#   deploy/rehearsal/release_layout.sh <work dir>
#
# Inputs, in the environment:
#   OLD_TAR      a tar of the rsync layout's code (git archive <the commit prod runs>)
#   PKG_DIR      packages from deploy/build_package.sh: aiscientist-$TAG.tar.gz and -$TAG2.tar.gz, SHA256SUMS
#   TAG, TAG2    the release migrated to, and the update deployed after it (TAG2 > TAG)
#   SKILLS_DIR   the files of an aiscientist-skills release (SHA256SUMS, catalog.json, the zip): v$SKILLS_V
#   SKILLS2_DIR  another release of it, v$SKILLS2_V (optional: the skills switch is skipped without it)
#   PYTHON       the Python requirements.lock is for (default python3)
#   PORT         the scratch site's port (default 8811)
# Needs GNU coreutils, curl, and PyPI. Takes a few minutes: it installs two venvs.
set -uo pipefail

WORK="$(mkdir -p "${1:?usage: $0 <work dir>}" && cd "$1" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${OLD_TAR:?}" "${PKG_DIR:?}" "${TAG:?}" "${TAG2:?}" "${SKILLS_DIR:?}"
PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8811}"
SKILLS_V="${SKILLS_V:-0.1.0}"
R="$WORK/root"
export SYSTEMD_DIR="$WORK/systemd"
SYSCTL="$WORK/bin/systemctl"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS  $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $*"; }
t()   { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }
nt()  { local d="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$d"; else ok "$d"; fi; }
health() { curl -fsS --max-time 4 "http://127.0.0.1:$PORT/health" 2>/dev/null; }
export PORT; export -f health          # the checks below run it in `bash -c`
wait_health() { for _ in $(seq 90); do [ -n "$(health)" ] && return 0; sleep 1; done; return 1; }
sql() { "$PYTHON" - "$R/state/rehearsal.db" "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
rows = c.execute(sys.argv[2]).fetchall()
c.commit()
print("|".join(",".join(str(v) for v in r) for r in rows))
PY
}
# The move runs as in the runbook: the script from inside the package of the release it moves to.
PKGX="$WORK/package-$TAG"
rm -rf "$PKGX" && mkdir -p "$PKGX" && tar -C "$PKGX" -xzf "$PKG_DIR/aiscientist-$TAG.tar.gz"
MOVE="$PKGX/aiscientist-$TAG/deploy/migrate_release_layout.sh"
MIGRATE=(env AISCIENTIST_ROOT="$R" SYSTEMD_DIR="$SYSTEMD_DIR" SUDOERS_DIR="$WORK/sudoers" SYSTEMCTL="$SYSCTL"
         REQUIRE_ROOT=0 AS_SVC= BIND_HOST=127.0.0.1 PORT="$PORT" AISCIENTIST_PYTHON="$PYTHON"
         HEALTH_TIMEOUT=120)
DEPLOY=(env AISCIENTIST_ROOT="$R" AISCIENTIST_HOST=127.0.0.1 AISCIENTIST_PORT="$PORT" AISCIENTIST_PYTHON="$PYTHON"
        AISCIENTIST_PACKAGE_DIR="$PKG_DIR" AISCIENTIST_RESTART_CMD="$SYSCTL restart aiscientist.service"
        AISCIENTIST_KEYS_RESTART_CMD="$SYSCTL restart aiscientist-keys.service"
        AISCIENTIST_HEALTH_TIMEOUT=120 SYSTEMD_DIR="$SYSTEMD_DIR")

echo "=== 0. a miniature of the server in its rsync layout ($R)"
"$SYSCTL" stop aiscientist.service >/dev/null 2>&1 || true
"$SYSCTL" stop aiscientist-keys.service >/dev/null 2>&1 || true
[ ! -d "$R" ] || chmod -R u+w "$R"          # installed releases are read-only
rm -rf "$R" "$SYSTEMD_DIR" "$WORK/sudoers" "$WORK/bin"
mkdir -p "$R"/{app,state,config,logs,backups,skills/releases/"$SKILLS_V",data/analysis_results} "$SYSTEMD_DIR" "$WORK/sudoers" "$WORK/bin"
cp "$HERE/fake_systemctl.sh" "$SYSCTL" && chmod +x "$SYSCTL"
chmod 700 "$R/state" "$R/config"
tar -C "$R/app" -xf "$OLD_TAR"
echo "rehearsal-old-commit $(date -u +%FT%TZ)" > "$R/app/.deployed_sha"
mkdir -p "$R/app/src/aiscientist/tools/genesets"
printf 'HALLMARK_X\tdesc\tGENE1\tGENE2\n' > "$R/app/src/aiscientist/tools/genesets/MSigDB_Hallmark_2020.gmt"
cp "$SKILLS_DIR"/* "$R/skills/releases/$SKILLS_V/" && chmod -R a-w "$R/skills/releases/$SKILLS_V"
"$PYTHON" -m venv "$R/env" && "$R/env/bin/pip" install -q -e "$R/app[gateway,auth]" || { echo "old venv failed"; exit 1; }
PW="reh-$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
cat > "$R/app/.env" <<ENV
# rehearsal settings: throwaway values, generated here
AISCIENTIST_DATABASE_URL=sqlite:///$R/state/rehearsal.db
AISCIENTIST_STATE_DIR=$R/state
AISCIENTIST_RESULTS_DIR=$R/data/analysis_results
AISCIENTIST_DATA_ROOT=$R/data
AISCIENTIST_INDUCED_SKILLS_DIR=$R/state/induced_skills
AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT=$R/skills/releases
AISCIENTIST_ADMIN_USER=reh
AISCIENTIST_ADMIN_PASSWORD=$PW
ENV
chmod 640 "$R/app/.env"
cat > "$SYSTEMD_DIR/aiscientist.service" <<UNIT
[Service]
WorkingDirectory=$R/app
EnvironmentFile=-$R/app/.env
ExecStart=$R/env/bin/python -m aiscientist.gateway --host 127.0.0.1 --port $PORT
UNIT
# A stand-in for the key service (deploy/keys/), installed before the move with the old venv's Python,
# as install_key_service.sh would: the move must point it at app/current and restart it first.
KEYS_PORT=$((PORT + 1))
cat > "$SYSTEMD_DIR/aiscientist-keys.service" <<UNIT
[Service]
WorkingDirectory=$WORK
ExecStart=$R/env/bin/python -m http.server $KEYS_PORT --bind 127.0.0.1
UNIT
keys_up() { curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:$KEYS_PORT/" 2>/dev/null; }
keys_runs() { ps -o args= -p "$(cat "$SYSTEMD_DIR/aiscientist-keys.service.pid" 2>/dev/null || echo 1)" 2>/dev/null | grep -qF "$1"; }
export -f keys_up keys_runs; export KEYS_PORT SYSTEMD_DIR
"$SYSCTL" start aiscientist-keys.service
"$SYSCTL" start aiscientist.service
wait_health && ok "the old site answers: $(health)" || { bad "the old site did not start"; tail -20 "$SYSTEMD_DIR/aiscientist.service.log"; exit 1; }
uid="$(sql "select id from users where username='reh'")"
sql "insert into datasets (user_id, uuid, status, name, path, size_bytes, kind) values ($uid, '00000000-0000-4000-8000-000000000001', 'complete', 'a.h5ad', '$R/data/a.h5ad', 6, 'h5ad')" >/dev/null
sql "insert into runs (user_id, run_id, question, status, plan_mode) values ($uid, 'run1', 'q', 'done', 0)" >/dev/null
t "seeded: one dataset, one finished run" test "$(sql 'select count(*) from datasets')|$(sql 'select count(*) from runs')" = "1|1"
nt "the old database has no revision table" grep -q alembic_version <<<"$(sql "select name from sqlite_master")"

echo "=== 1. preflight changes nothing"
before="$(find "$R" -newer "$R/app/.env" | wc -l)"
"${MIGRATE[@]}" bash "$MOVE" --tag "$TAG" --package-dir "$PKG_DIR" > "$WORK/pre.log" 2>&1
rc=$?; sed 's/^/        /' "$WORK/pre.log" | grep -E "FAIL|note|passed|problem"
[ $rc -eq 0 ] && ok "preflight passes" || bad "preflight rc=$rc"
t "nothing changed" test "$(find "$R" -newer "$R/app/.env" | wc -l)" = "$before"

echo "=== 2. --prepare: the release is installed beside the running site"
"${MIGRATE[@]}" bash "$MOVE" --tag "$TAG" --package-dir "$PKG_DIR" --prepare > "$WORK/prepare.log" 2>&1
rc=$?; [ $rc -eq 0 ] && ok "--prepare exits 0" || { bad "--prepare rc=$rc"; tail -30 "$WORK/prepare.log"; exit 1; }
REL="$R/app/releases/$TAG"
t "release installed" test -f "$REL/.installed"
t "its own venv" test -x "$REL/.venv/bin/python"
t "installed from requirements.lock with hashes" grep -q "require-hashes" "$WORK/prepare.log"
t "release is read-only" bash -c "! touch '$REL/src/probe' 2>/dev/null"
t "config/.env = app/.env" cmp -s "$R/app/.env" "$R/config/.env"
t "config/deploy.env written" grep -q '^AISCIENTIST_REPO=RCHENLAB/AiScientist' "$R/config/deploy.env"
t "gene sets copied to data/reference" test -f "$R/data/reference/genesets/MSigDB_Hallmark_2020.gmt"
t "gene sets inside the release, real files" bash -c "test -f '$REL/src/aiscientist/tools/genesets/MSigDB_Hallmark_2020.gmt' -a ! -L '$REL/src/aiscientist/tools/genesets/MSigDB_Hallmark_2020.gmt'"
t "skills/current -> the installed release" test "$(readlink "$R/skills/current")" = "releases/$SKILLS_V"
HEAD_REV="$(ls "$REL/src/aiscientist/gateway/migrations/versions" | grep -E '^[0-9]{4}_' | sort | tail -n 1 | cut -c1-4)"
t "database migrated: from 0001 to the newest revision ($HEAD_REV)" test "$(sql 'select version_num from alembic_version')" = "$HEAD_REV"
t "its rows kept" test "$(sql 'select name from datasets')|$(sql 'select run_id from runs')" = "a.h5ad|run1"
t "the old site still serves, untouched" bash -c "health | grep -q '\"status\":\"ok\"' && ! health | grep -q '\"version\":\"$TAG\"'"

echo "=== 3. --apply waits for a run in progress"
sql "update runs set status='running' where run_id='run1'" >/dev/null
"${MIGRATE[@]}" bash "$MOVE" --tag "$TAG" --package-dir "$PKG_DIR" --apply > "$WORK/busy.log" 2>&1
rc=$?; [ $rc -ne 0 ] && grep -q "run(s) in progress" "$WORK/busy.log" && ok "refused: 1 run in progress" || bad "did not refuse (rc=$rc)"
t "nothing moved" test -f "$R/app/pyproject.toml" -a ! -e "$R/app/current"
sql "update runs set status='done' where run_id='run1'" >/dev/null

echo "=== 4. --apply"
"${MIGRATE[@]}" bash "$MOVE" --tag "$TAG" --package-dir "$PKG_DIR" --apply > "$WORK/apply.log" 2>&1
rc=$?; [ $rc -eq 0 ] && ok "--apply exits 0" || { bad "--apply rc=$rc"; tail -30 "$WORK/apply.log"; }
SET="$(ls -d "$R"/backups/release-layout-* | tail -n 1)"
t "app/ holds only releases and current" test "$(ls -A "$R/app" | tr '\n' ' ')" = "current releases "
t "app/current -> releases/$TAG" test "$(readlink "$R/app/current")" = "releases/$TAG"
t "the rsync tree is in the backup set" test -f "$SET/app-rsync/pyproject.toml" -a -f "$SET/app-rsync/.env" -a -f "$SET/app-rsync/.deployed_sha"
t "the old venv is in the backup set" test -x "$SET/env/bin/python" -a ! -e "$R/env"
t "drop-in: runs app/current's venv on the unit's address" grep -qx "ExecStart=$R/app/current/.venv/bin/python -m aiscientist.gateway --host 127.0.0.1 --port $PORT" "$SYSTEMD_DIR/aiscientist.service.d/release-layout.conf"
t "drop-in: reads config/.env" grep -qx "EnvironmentFile=-$R/config/.env" "$SYSTEMD_DIR/aiscientist.service.d/release-layout.conf"
t "restart rule for the service account" grep -q "NOPASSWD: .*systemctl restart aiscientist.service" "$WORK/sudoers/aiscientist-restart"
t "key service unit points at app/current's venv" grep -qx "ExecStart=$R/app/current/.venv/bin/python -m http.server $KEYS_PORT --bind 127.0.0.1" "$SYSTEMD_DIR/aiscientist-keys.service"
sleep 1
t "key service restarted from the release's Python, answers" bash -c "keys_up && keys_runs '$R/app/current/.venv/bin/python'"
t "restart rule names the key service too" grep -q "restart aiscientist-keys.service" "$WORK/sudoers/aiscientist-restart"
t "deploy timer installed, not enabled" bash -c "test -f '$SYSTEMD_DIR/aiscientist-deploy.timer' -a ! -e '$SYSTEMD_DIR/enabled.aiscientist-deploy.timer'"
body="$(health)"; echo "        /health: $body"
t "/health: the release, accounts on, skills v$SKILLS_V" bash -c "grep -q '\"version\":\"$TAG\"' <<<'$body' && grep -q '\"accounts\":true' <<<'$body' && grep -q '\"skills\":\"v$SKILLS_V\"' <<<'$body'"
t "the console page is served from the read-only release" bash -c "curl -fsS http://127.0.0.1:$PORT/ | grep -qi '<html'"
JAR="$WORK/cookies"
curl -fsS -c "$JAR" -H 'Content-Type: application/json' -d "{\"username\":\"reh\",\"password\":\"$PW\"}" "http://127.0.0.1:$PORT/api/auth/login" >/dev/null
sys="$(curl -fsS -b "$JAR" "http://127.0.0.1:$PORT/api/system")"
"$PYTHON" -c 'import json,sys; r=json.load(sys.stdin)["release"]; print(r["app"]["tag"], r["skills"]["tag"], r["schema"]["database"], r["schema"]["release"])' <<<"$sys" > "$WORK/system.txt" 2>&1
echo "        /api/system release: $(cat "$WORK/system.txt")"
t "System page: app $TAG, skills v$SKILLS_V, schema $HEAD_REV of $HEAD_REV" test "$(cat "$WORK/system.txt")" = "$TAG v$SKILLS_V $HEAD_REV $HEAD_REV"
t "the finished run is still listed (same database)" test "$(sql "select status from runs where run_id='run1'")" = done
t "the deploy log names the switch" grep -q "app installed $TAG" "$R/logs/deploy.log"

echo "=== 5. an update and its rollback through deploy_release.sh"
"${DEPLOY[@]}" bash "$REL/deploy/deploy_release.sh" "$TAG2" > "$WORK/update.log" 2>&1
rc=$?; [ $rc -eq 0 ] && ok "deploy_release.sh $TAG2 exits 0" || { bad "update rc=$rc"; tail -20 "$WORK/update.log"; }
t "current -> $TAG2, previous -> $TAG" test "$(readlink "$R/app/current") $(readlink "$R/app/previous")" = "releases/$TAG2 releases/$TAG"
t "/health names $TAG2" bash -c "health | grep -q '\"version\":\"$TAG2\"'"
t "the update restarted the key service before the app" bash -c "grep -E '^restart aiscientist(-keys)?\.service' '$SYSTEMD_DIR/calls.log' | tail -n 2 | tr '\n' ' ' | grep -qx 'restart aiscientist-keys.service restart aiscientist.service '"
"${DEPLOY[@]}" bash "$R/app/current/deploy/deploy_release.sh" --rollback > "$WORK/rb-app.log" 2>&1
rc=$?; [ $rc -eq 0 ] && ok "--rollback exits 0" || { bad "--rollback rc=$rc"; tail -20 "$WORK/rb-app.log"; }
t "/health names $TAG again" bash -c "health | grep -q '\"version\":\"$TAG\"'"
t "$TAG2 is held" grep -qx "$TAG2" "$R/app/held"
"${DEPLOY[@]}" AISCIENTIST_APP_TAG= bash "$R/app/current/deploy/deploy_release.sh" --auto > "$WORK/auto.log" 2>&1
t "--auto leaves the held release alone" bash -c "grep -q '$TAG2 is held' '$WORK/auto.log' && health | grep -q '\"version\":\"$TAG\"'"

if [ -n "${SKILLS2_DIR:-}" ]; then
  echo "=== 6. a skills switch and its rollback through deploy_skills.sh"
  "${DEPLOY[@]}" AISCIENTIST_SKILLS_PACKAGE_DIR="$SKILLS2_DIR" bash "$R/app/current/deploy/deploy_skills.sh" "v$SKILLS2_V" > "$WORK/skills.log" 2>&1
  rc=$?; [ $rc -eq 0 ] && ok "deploy_skills.sh v$SKILLS2_V exits 0" || { bad "skills rc=$rc"; tail -20 "$WORK/skills.log"; }
  t "the running app loaded v$SKILLS2_V without a restart" bash -c "health | grep -q '\"skills\":\"v$SKILLS2_V\"'"
  "${DEPLOY[@]}" bash "$R/app/current/deploy/deploy_skills.sh" --rollback > "$WORK/skills-rb.log" 2>&1
  t "skills rolled back to v$SKILLS_V" bash -c "health | grep -q '\"skills\":\"v$SKILLS_V\"'"
fi

echo "=== 7. the migration's --rollback"
"${MIGRATE[@]}" bash "$MOVE" --rollback > "$WORK/rollback.log" 2>&1
rc=$?; [ $rc -eq 0 ] && ok "--rollback exits 0" || { bad "--rollback rc=$rc"; tail -30 "$WORK/rollback.log"; }
t "app/ is the rsync tree again" test -f "$R/app/pyproject.toml" -a ! -e "$R/app/current" -a ! -e "$R/app/releases"
t "the old venv is back" test -x "$R/env/bin/python"
t "key service unit back on the old venv, answers" bash -c "grep -qx 'ExecStart=$R/env/bin/python -m http.server $KEYS_PORT --bind 127.0.0.1' '$SYSTEMD_DIR/aiscientist-keys.service' && sleep 1 && keys_up && keys_runs '$R/env/bin/python'"
t "drop-in, restart rule and timer removed" bash -c "! test -e '$SYSTEMD_DIR/aiscientist.service.d/release-layout.conf' && ! test -e '$WORK/sudoers/aiscientist-restart' && ! test -e '$SYSTEMD_DIR/aiscientist-deploy.timer'"
t "the old site serves (no release tag)" bash -c "health | grep -q '\"status\":\"ok\"' && ! health | grep -q '\"version\":\"v'"
t "the releases are kept in the backup set" test -d "$SET/release-layout/releases/$TAG"
t "the database keeps its rows" test "$(sql 'select name from datasets')|$(sql 'select run_id from runs')" = "a.h5ad|run1"

echo "=== 8. --apply again after a rollback"
"${MIGRATE[@]}" bash "$MOVE" --tag "$TAG" --package-dir "$PKG_DIR" --apply --timer > "$WORK/apply2.log" 2>&1
rc=$?; [ $rc -eq 0 ] && ok "second --apply exits 0" || { bad "second --apply rc=$rc"; tail -30 "$WORK/apply2.log"; }
t "/health names $TAG" bash -c "health | grep -q '\"version\":\"$TAG\"'"
t "--timer enabled the deploy timer" test -e "$SYSTEMD_DIR/enabled.aiscientist-deploy.timer"

"$SYSCTL" stop aiscientist.service >/dev/null 2>&1
"$SYSCTL" stop aiscientist-keys.service >/dev/null 2>&1
echo; echo "RESULT: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
