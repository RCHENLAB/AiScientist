#!/bin/bash
# Build a miniature of the prod host: same account, same tree layout and ACLs, a REAL editable
# venv, the real gateway running under the old unit, and a database with old-root paths.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq python3 python3-venv sudo acl curl iproute2 procps >/dev/null 2>&1
install -m 0755 /in/systemctl /usr/bin/systemctl
groupadd -g 986 aiscientist; useradd -u 995 -g 986 -M -s /usr/sbin/nologin aiscientist
useradd -m -G users yijus12-admin; useradd -m -G users otheruser
O=/data/BioAgent
mkdir -p $O /data/aiscientist
chown root:aiscientist /data/aiscientist; chmod 0750 /data/aiscientist
chown aiscientist:aiscientist $O; chmod 2775 $O
setfacl -m g:users:rwx,d:u::rwx,d:g::rwx,d:g:users:rwx,d:o::r-x $O
S="sudo -u aiscientist"; cd /tmp
$S mkdir -p $O/app $O/state $O/induced_skills/demo_skill $O/env-backups $O/biomni/biomni
$S tar -C $O/app -xf /in/src.tar
chmod 700 $O/state $O/env-backups
$S python3 -m venv $O/env
$S env PIP_CACHE_DIR=/tmp/pipcache $O/env/bin/pip install -q -e "$O/app[gateway,auth]" 2>&1 | grep -v -i "notice" || true
SP="$(ls -d $O/env/lib/python3*/site-packages)"
# the second editable install prod has (biomni): a finder module with absolute paths, loaded by a .pth
$S tee "$SP/__editable___biomni_0_0_8_finder.py" >/dev/null <<PY
MAPPING: dict[str, str] = {'biomni': '$O/biomni/biomni'}
NAMESPACES: dict[str, list[str]] = {'docs': ['$O/biomni/docs']}
def install(): pass
PY
echo "import __editable___biomni_0_0_8_finder; __editable___biomni_0_0_8_finder.install()" | $S tee "$SP/__editable__.biomni-0.0.8.pth" >/dev/null
# Throwaway values, generated here and gone with the container.
PW="reh-$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
SK="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
$S tee $O/app/.env >/dev/null <<ENV
# AiScientist — production environment ($O/app/.env)
AISCIENTIST_DATABASE_URL=sqlite:///$O/state/rehearsal.db
AISCIENTIST_SECRET_KEY=$SK
AISCIENTIST_ADMIN_USER=reh
AISCIENTIST_ADMIN_PASSWORD=$PW
AISCIENTIST_INDUCED_SKILLS_DIR=$O/induced_skills
# moved out of the deploy target ($O/app)
AISCIENTIST_STATE_DIR=$O/state
AISCIENTIST_HPC_SHARED_ROOT=/dfs3b/ruic20_lab/software/AiScientist
ENV
cat > /etc/systemd/system/bioagent.service <<UNIT
[Unit]
Description=BioAgent AiScientist console (FastAPI/uvicorn)
[Service]
Type=simple
User=aiscientist
Group=aiscientist
WorkingDirectory=$O/app
EnvironmentFile=-$O/app/.env
ExecStart=$O/env/bin/python -m bioagent.gateway --host 127.0.0.1 --port 8800
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/sudoers.d/bioagent-deploy <<'SUD'
yijus12-admin ALL=(aiscientist) NOPASSWD: ALL
yijus12-admin ALL=(root) NOPASSWD: /usr/bin/systemctl restart bioagent.service, /usr/bin/systemctl stop bioagent.service, /usr/bin/systemctl start bioagent.service
SUD
# a second rule file, as on prod (another person's diagnostics grant): one line with the full unit
# name, one with the bare name the script must NOT rewrite on its own
cat > /etc/sudoers.d/ziyaom2-diag <<'SUD'
otheruser ALL=(root) NOPASSWD: /usr/bin/journalctl -u bioagent.service -n 200 --no-pager
otheruser ALL=(root) NOPASSWD: /usr/bin/journalctl -u bioagent -f
SUD
chmod 0440 /etc/sudoers.d/bioagent-deploy /etc/sudoers.d/ziyaom2-diag; visudo -c >/dev/null
systemctl enable bioagent.service; systemctl start bioagent.service
for i in $(seq 1 30); do [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/ || true)" = 200 ] && break; sleep 1; done
curl -s -o /dev/null -w 'old service: HTTP %{http_code}\n' http://127.0.0.1:8800/
# data that names the old root: an upload, a run with run_state.json, DB rows
U=$O/app/runs/console/reh
$S mkdir -p $U/uploads $U/abc123/artifacts/process $U/abc123/artifacts/data
echo dummy | $S tee $U/uploads/a.h5ad >/dev/null
$S tee $U/abc123/artifacts/process/run_state.json >/dev/null <<J
{"dataset_path": "$U/uploads/a.h5ad", "datasets": [{"path": "$U/uploads/a.h5ad"}], "hpc_primary": "/dfs3b/ruic20_lab/x.h5ad"}
J
echo '{"path": "/dfs3b/ruic20_lab/result.h5ad", "note": "no local path here"}' | $S tee $U/abc123/artifacts/data/result_dataset.json >/dev/null
$S env PYTHONPATH=$O/app/src $O/env/bin/python - <<PY
import os
from pathlib import Path
from aiscientist.core.config import load_project_env
load_project_env(Path("$O/app"))
from sqlalchemy import create_engine, text
e = create_engine(os.environ["AISCIENTIST_DATABASE_URL"])
with e.begin() as c:
    uid = c.execute(text("select id from users limit 1")).scalar()
    c.execute(text("insert into datasets (user_id, name, path, size_bytes, kind) values (:u,'a.h5ad',:p,6,'h5ad')"), {"u": uid, "p": "$U/uploads/a.h5ad"})
    c.execute(text("insert into datasets (user_id, name, path, size_bytes, kind) values (:u,'remote.h5ad','/dfs3b/ruic20_lab/software/AiScientist/uploads/reh/remote.h5ad',6,'h5ad')"), {"u": uid})
    c.execute(text("insert into runs (user_id, run_id, question, status, plan_mode, artifacts_path) values (:u,'abc123','q','done',0,:p)"), {"u": uid, "p": "$U/abc123/artifacts"})
print("seeded")
PY
find $O/app/runs -exec touch -d "3 hours ago" {} +
echo "setup done: $(ls $O | tr '\n' ' ')"
