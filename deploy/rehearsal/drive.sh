#!/bin/bash
# Run the UNMODIFIED migration script as root and assert the outcome of each mode.
set -uo pipefail
export BIND_HOST=127.0.0.1
M=/in/migrate_prod_root.sh; O=/data/BioAgent; N=/data/aiscientist
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS  $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $*"; }
t()   { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }
nt()  { local d="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$d"; else ok "$d"; fi; }
db()  { (cd /tmp && sudo -u aiscientist env PYTHONPATH=$1/app/src $1/env/bin/python -c "
import os; from pathlib import Path
from aiscientist.core.config import load_project_env; load_project_env(Path('$1/app'))
from sqlalchemy import create_engine, text
e = create_engine(os.environ['AISCIENTIST_DATABASE_URL'])
with e.connect() as c:
    print(sorted(r[0] for r in c.execute(text('select path from datasets'))), c.execute(text('select artifacts_path from runs')).scalar())"); }
http() { curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:8800/; }
SP_OLD="$(ls -d $O/env/lib/python3*/site-packages)"; REL="${SP_OLD#$O/}"

echo "    before: state=$(stat -c %a $O/state) env-backups=$(stat -c %a $O/env-backups)"
echo "=== 1. preflight (changes nothing)"
bash $M > /tmp/pre.log 2>&1; rc=$?
[ $rc -eq 0 ] && grep -q "preflight only" /tmp/pre.log && ok "preflight exits 0 and says nothing changed" || { bad "preflight rc=$rc"; tail -5 /tmp/pre.log; }
t "tree untouched" test -d $O -a ! -L $O
t "service still up" test "$(http)" = 200
grep -E "venv files|run JSON|\.env lines" -A3 /tmp/pre.log | head -12

echo "=== 2. refuses while a run is writing"
sudo -u aiscientist touch $O/app/runs/console/reh/abc123/artifacts/process/loop.log
bash $M --apply > /tmp/busy.log 2>&1; rc=$?
[ $rc -ne 0 ] && grep -q "not idle" /tmp/busy.log && ok "aborts: prod is not idle" || { bad "did not abort (rc=$rc)"; tail -5 /tmp/busy.log; }
t "nothing moved" test -d $O -a ! -L $O
t "old unit still installed" test -f /etc/systemd/system/bioagent.service
t "service still up" test "$(http)" = 200
touch -d "3 hours ago" $O/app/runs/console/reh/abc123/artifacts/process/loop.log $O/app/runs/console/reh/abc123/artifacts/process

check_migrated() {
  t "old root is a symlink to the new one" test "$(readlink $O)" = $N
  t "tree is at the new root" test -f $N/app/pyproject.toml
  t "tree owner unchanged (aiscientist)" test "$(stat -c %U $N)" = aiscientist
  nt "no venv file names the old root" grep -rlI --exclude-dir=__pycache__ -F $O $N/env
  t "editable .pth points at the new root" grep -qx "$N/app/src" $N/$REL/__editable__.aiscientist-0.2.0.pth
  t "biomni finder points at the new root" grep -q "'$N/biomni/biomni'" $N/$REL/__editable___biomni_0_0_8_finder.py
  t "console-script shebang rewritten" test "$(head -1 $N/env/bin/pip)" = "#!$N/env/bin/python3"
  t "console script still executable and runs" sudo -u aiscientist $N/env/bin/pip --version
  t ".env: state dir" grep -qx "AISCIENTIST_STATE_DIR=$N/state" $N/app/.env
  t ".env: induced skills dir" grep -qx "AISCIENTIST_INDUCED_SKILLS_DIR=$N/induced_skills" $N/app/.env
  nt ".env: no old root left" grep -q -F $O $N/app/.env
  t ".env: other keys untouched" grep -qx "AISCIENTIST_HPC_SHARED_ROOT=/dfs3b/ruic20_lab/software/AiScientist" $N/app/.env
  t ".env: owner kept" test "$(stat -c %U:%G $N/app/.env)" = aiscientist:aiscientist
  local rows; rows="$(db $N)"; echo "        db: $rows"
  t "db: local dataset path rewritten" grep -q "'$N/app/runs/console/reh/uploads/a.h5ad'" <<<"$rows"
  t "db: HPC path untouched" grep -q "'/dfs3b/ruic20_lab/software/AiScientist/uploads/reh/remote.h5ad'" <<<"$rows"
  t "db: runs.artifacts_path rewritten" grep -q " $N/app/runs/console/reh/abc123/artifacts" <<<"$rows"
  t "the rewritten dataset path exists on disk" test -f $N/app/runs/console/reh/uploads/a.h5ad
  local rs=$N/app/runs/console/reh/abc123/artifacts/process/run_state.json
  t "run_state.json rewritten" grep -q "\"dataset_path\": \"$N/app/runs" $rs
  nt "run_state.json: no old root left" grep -q -F "$O/" $rs
  t "run_state.json: HPC path untouched" grep -q '"/dfs3b/ruic20_lab/x.h5ad"' $rs
  t "run_state.json still valid JSON, owner kept" bash -c "python3 -m json.tool $rs && test \$(stat -c %U $rs) = aiscientist"
  t "run_state.json keeps its old mtime (a finished run must not look fresh)" bash -c "test \$(( \$(date +%s) - \$(stat -c %Y $rs) )) -gt 3600"
  t "its directory mtime is untouched too" bash -c "test \$(( \$(date +%s) - \$(stat -c %Y \$(dirname $rs)) )) -gt 3600"
  t "new unit installed" test -f /etc/systemd/system/aiscientist.service
  nt "old unit removed" test -e /etc/systemd/system/bioagent.service
  t "unit: new paths and module" grep -qx "ExecStart=$N/env/bin/python -m aiscientist.gateway --host 127.0.0.1 --port 8800" /etc/systemd/system/aiscientist.service
  t "unit enabled" test -f /run/enabled.aiscientist.service
  t "sudoers: new grant, 0440 root" test "$(stat -c '%a %U' /etc/sudoers.d/aiscientist-deploy)" = "440 root"
  nt "sudoers: old grant removed" test -e /etc/sudoers.d/bioagent-deploy
  t "sudoers: names the new unit three times, old one never" bash -c "test \$(grep -o 'aiscientist.service' /etc/sudoers.d/aiscientist-deploy | wc -l) = 3 && ! grep -q bioagent /etc/sudoers.d/aiscientist-deploy"
  t "sudoers: whole config valid" visudo -c
  t "sudoers: a second rule file is rewritten in place, 0440 root" bash -c "grep -q -- '-u aiscientist.service -n 200' /etc/sudoers.d/ziyaom2-diag && test \"\$(stat -c '%a %U' /etc/sudoers.d/ziyaom2-diag)\" = '440 root'"
  t "sudoers: a bare unit name is left alone" grep -q -- "-u bioagent -f" /etc/sudoers.d/ziyaom2-diag
  t "sudoers: no temp files left behind" bash -c "! ls /etc/sudoers.d | grep -q '\.new\.'"
  t "sudoers: the other account's rewritten grant works" bash -c "sudo -l -U otheruser | grep -q 'journalctl -u aiscientist.service'"
  t "sudoers: admin may restart the new unit without a password" bash -c "sudo -l -U yijus12-admin | grep -q 'NOPASSWD: /usr/bin/systemctl restart aiscientist.service'"
  t "service active under the new unit" systemctl is-active --quiet aiscientist.service
  local pid; pid="$(cat /run/aiscientist.service.pid)"
  t "service cwd is the new app dir" test "$(readlink /proc/$pid/cwd)" = $N/app
  t "process runs the new interpreter and module" bash -c "pgrep -af 'aiscientist.gateway' | grep -q '$N/env/bin/python -m aiscientist.gateway'"
  t "service answers HTTP 200" test "$(http)" = 200
  t "API works from the new root (providers list)" bash -c "curl -s http://127.0.0.1:8800/api/llm-providers | grep -q anthropic"
  t "old paths still resolve through the symlink" test -f $O/app/.env
  t "backups written (0700, aiscientist)" bash -c "d=\$(ls -d $N/env-backups/root-move-* | tail -1); test -f \$d/venv-files.tar -a -f \$d/app.env -a -f \$d/bioagent.service -a -f \$d/db-paths.json && stat -c '%a %U' \$d | grep -qE '^2?700 aiscientist\$'"
}

echo "=== 3. --apply"
bash $M --apply > /tmp/apply.log 2>&1; rc=$?
[ $rc -eq 0 ] && ok "--apply exits 0" || { bad "--apply rc=$rc"; tail -25 /tmp/apply.log; }
grep -q "were NOT changed" /tmp/apply.log && grep -q "ziyaom2-diag:2:.*-u bioagent -f" /tmp/apply.log && ok "the untouched bare-name line is reported for a hand review" || bad "leftover sudoers line not reported"
grep -E "^==>|rows|JSON files" /tmp/apply.log | sed 's/^/        /'
check_migrated
bash $M --apply > /tmp/again.log 2>&1; rc=$?
[ $rc -ne 0 ] && grep -q "already moved" /tmp/again.log && ok "a second --apply refuses (already moved)" || bad "second --apply rc=$rc"
t "service unaffected by the refused second run" test "$(http)" = 200

echo "=== 4. --rollback"
bash $M --rollback > /tmp/rb.log 2>&1; rc=$?
[ $rc -eq 0 ] && ok "--rollback exits 0" || { bad "--rollback rc=$rc"; tail -25 /tmp/rb.log; }
t "old root is a real directory again" test -d $O -a ! -L $O
t "new root is the empty root:aiscientist 0750 dir again" bash -c "test -z \"\$(ls -A $N)\" && test \"\$(stat -c '%U:%G %a' $N)\" = 'root:aiscientist 750'"
t "editable .pth back to the old root" grep -qx "$O/app/src" $O/$REL/__editable__.aiscientist-0.2.0.pth
t ".env restored" grep -qx "AISCIENTIST_STATE_DIR=$O/state" $O/app/.env
rows="$(db $O)"; echo "        db: $rows"
t "db rows restored" grep -q "'$O/app/runs/console/reh/uploads/a.h5ad'" <<<"$rows"
t "run_state.json restored" grep -q "\"dataset_path\": \"$O/app/runs" $O/app/runs/console/reh/abc123/artifacts/process/run_state.json
t "old unit back, new unit gone" bash -c "test -f /etc/systemd/system/bioagent.service && ! test -e /etc/systemd/system/aiscientist.service"
t "old sudoers grant back, new one gone" bash -c "test -f /etc/sudoers.d/bioagent-deploy && ! test -e /etc/sudoers.d/aiscientist-deploy && visudo -c"
t "second rule file restored" grep -q -- "-u bioagent.service -n 200" /etc/sudoers.d/ziyaom2-diag
t "service active under the old unit, HTTP 200" bash -c "systemctl is-active --quiet bioagent.service && test \$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/) = 200"
t "service cwd is the old app dir" test "$(readlink /proc/$(cat /run/bioagent.service.pid)/cwd)" = $O/app

echo "=== 5. --apply again after a rollback"
sleep 1; bash $M --apply > /tmp/apply2.log 2>&1; rc=$?
[ $rc -eq 0 ] && ok "second --apply exits 0" || { bad "second --apply rc=$rc"; tail -25 /tmp/apply2.log; }
check_migrated

echo "=== 6. --harden"
t "before: another account can read .env (the problem)" sudo -u otheruser cat $N/app/.env
t "before: another account can write into the venv (the problem)" sudo -u otheruser touch $N/env/planted.py
rm -f $N/env/planted.py
bash $M --harden > /tmp/harden.log 2>&1; rc=$?
[ $rc -eq 0 ] && ok "--harden exits 0" || { bad "--harden rc=$rc"; tail -15 /tmp/harden.log; }
nt "after: another account cannot read .env" sudo -u otheruser cat $N/app/.env
nt "after: another account cannot list the root" sudo -u otheruser ls $N
nt "after: another account cannot write into the venv" sudo -u otheruser touch $N/env/planted.py
nt "after: no group:users ACL left anywhere" bash -c "getfacl -R -p $N 2>/dev/null | grep -q '^group:users\|^default:group:users'"
t "after: the service account still reads its tree" sudo -u aiscientist cat $N/app/.env
t "after: service still serves, not restarted" bash -c "test \$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/) = 200 && test \$(grep -c '^start aiscientist.service' /var/log/systemctl-shim.log) = 2"
t "after: state/ is still closed to its group (private keys live there)" bash -c "stat -c %a $N/state | grep -qE '^2?700\$'"
t "after: env-backups/ is still closed to its group" bash -c "stat -c %a $N/env-backups | grep -qE '^2?700\$'"
t "after: the service can still write its state" sudo -u aiscientist touch $N/state/probe
t "after: a file the service creates is not world-readable" bash -c "sudo -u aiscientist touch $N/app/runs/newfile && test \$(stat -c %a $N/app/runs/newfile | cut -c3) = 0"

echo; echo "RESULT: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
