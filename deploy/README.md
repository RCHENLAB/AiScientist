# Deploying AiScientist

How the lab's server runs AiScientist at <https://AiScientist.eye.som.uci.edu>, and how to update
it. For a first look at the system, read the [project README](../README.md).

## What runs

```
Browser ──HTTPS :443──▶ Envoy Gateway on the lab server's Kubernetes cluster (TLS ends here)
                          │  HTTPRoute aiscientist ─▶ Service aiscientist-app (no selector)
                          │                           ─▶ Endpoints: <node IP>:8800
                          ▼
        aiscientist.service: a systemd unit on the host, not a pod
        python -m aiscientist.gateway --host <node IP> --port 8800
        user aiscientist, root /data/aiscientist, PostgreSQL on 127.0.0.1:5432
                          │  one SSH session per signed-in user
                          ▼
        UCI HPC3: the model server and every tool job (Slurm + Apptainer)
```

- **The gateway is one process on the host.** It holds each session's SSH connection and tunnel
  to HPC3 in memory, so there is exactly one instance and no load balancing. A restart ends the
  live sessions: users stay signed in (the sign-in cookie is signed, not stored) but connect to
  HPC3 again, and a run that was interrupted can be continued.
- **The cluster only routes.** There is no pod and no image. The objects are in
  [`k8s/host-backend.yaml`](k8s/host-backend.yaml).
- **The unit binds the node's internal IP**, the address the Endpoints object names. Loopback
  cannot be reached from the cluster, and the public address must not carry port 8800.
- **Code the model writes never runs on this host.** With accounts on, `run_code` runs on HPC3 as
  the user's own Slurm job, and so do the analysis, variant, phenotype and literature tools.

## On the server

| Path | What |
|---|---|
| `/data/aiscientist/app/` | the code (the rsync target), `.env`, `.deployed_sha`, and under `runs/console/<UCInetID>/` each HPC3 user's run outputs and unfinished browser uploads (`uploads/.parts/`, removed 24 hours after their last write, `AISCIENTIST_UPLOAD_TTL_SECONDS`) |
| `/data/aiscientist/env/` | the Python virtual environment; the app is an editable install |
| `/data/aiscientist/state/` | `AISCIENTIST_STATE_DIR`, mode 0700: the session secret (`session_secret`), the users' saved SSH keys (`ssh_creds/`) and LLM API keys (`llm_creds/`) with the master key that encrypts them (`llm_key.master`), and the model list once an admin has saved it |
| `/data/aiscientist/induced_skills/` | `AISCIENTIST_INDUCED_SKILLS_DIR`: skills learned from runs, with their review file |
| `/data/aiscientist/env-backups/` | copies of `.env` taken before an edit |
| `/data/BioAgent` | a symlink to `/data/aiscientist`. Stored run paths may name it, so it stays. |
| `/etc/systemd/system/aiscientist.service` | the unit, installed from [`systemd/aiscientist.service`](systemd/aiscientist.service) |

Everything under `/data/aiscientist` belongs to the service account `aiscientist`, which has no
login shell. An operator needs an account that may run `sudo -u aiscientist` and
`sudo systemctl start|stop|restart aiscientist.service`. The sudo rule matches the command as
written, so always name the unit with `.service`.

The accounts database is PostgreSQL on the host, reachable only on loopback. The role and the
database are both named `bioagent`.

## Updating the server

The server cannot fetch the repository. Code is copied with rsync from a checkout on your machine
by [`scripts/sync_deploy.sh`](../scripts/sync_deploy.sh), which then installs the package,
restarts the unit through systemd and checks that it answers.

```bash
# Settings, in the environment or in a gitignored .deploy.env:
#   DEPLOY_SSH   ssh alias of your own account on the server (the bulk copy; no sudo)
#   ADMIN_SSH    ssh alias of the account that may sudo (the copy into the app, the restart)
#   BIND_HOST    the node IP the unit binds
scripts/sync_deploy.sh --dry-run     # list what would be copied; changes nothing
scripts/sync_deploy.sh               # copy, pip install -e, restart, health check
```

| Option | Use it when |
|---|---|
| `--no-install` | `pyproject.toml` and the requirements did not change |
| `--no-restart` | the files should land now and the restart come later; `.deployed_sha` then says "NOT yet restarted" |
| `--delete` | files removed from the repository should be removed on the server too. `.env`, the databases, the virtual environment, `runs/`, the credential stores and the gene-set libraries are never deleted. |

Before a deploy:

1. **Deploy a clean checkout of the commit you mean.** The script copies the working tree, so an
   untracked file in it is copied too.
2. **Read what is running:** `cat /data/aiscientist/app/.deployed_sha` on the server. If that
   commit is not an ancestor of yours, deploying removes someone's change.
3. **Pick a quiet moment.** `ss -Htn state established '( sport = :8800 )' | wc -l` on the server
   counts the open connections.
4. **Gene sets.** `src/aiscientist/tools/genesets/*.gmt` are not in git
   (`scripts/fetch_genesets.py` downloads them). A checkout without them leaves the server's
   copies as they are.

After a deploy:

```bash
cat /data/aiscientist/app/.deployed_sha                      # <commit> <branch> <UTC time>
systemctl show aiscientist.service -p ActiveState -p NRestarts -p ActiveEnterTimestamp
curl -s https://AiScientist.eye.som.uci.edu/api/auth/config  # answers through the ingress
sudo journalctl -u aiscientist.service -n 50                 # the service log
```

The app creates the schema at start, and adds a new nullable column to an existing table
(`_ensure_columns` in `gateway/db.py`). Any other change to an existing table is a manual migration.

Other scripts:

- [`scripts/deploy_interactive.sh`](../scripts/deploy_interactive.sh) does the same with password
  prompts, for an operator without an SSH key on the server.
- `./deploy.sh` builds the virtual environment and `./start.sh` runs the app in the foreground,
  for a first installation and for development.

## Configuration

The app reads `/data/aiscientist/app/.env` at start; every setting is described in
[`configs/aiscientist.example.env`](../configs/aiscientist.example.env). A public server needs at
least:

| Setting | Why |
|---|---|
| `AISCIENTIST_STATE_DIR` | the server's private files: the session secret and the credential stores. Mode 0700, owned by the service account, outside the app directory, which every deploy writes into. |
| `AISCIENTIST_PUBLIC_HTTPS=1` | marks the session cookie `Secure` |
| `AISCIENTIST_DATABASE_URL` | PostgreSQL. Without it the app falls back to a local SQLite file, which is for development only. |
| `AISCIENTIST_SMTP_*`, `AISCIENTIST_ALLOWED_EMAIL_DOMAINS` | registration email. Without `AISCIENTIST_SMTP_HOST` the verification code is shown in the browser instead of being emailed. |
| `AISCIENTIST_HPC_*`, `AISCIENTIST_*_ON_HPC`, `AISCIENTIST_*_IMAGE` | the HPC3 side: host, shared root, which lines run as Slurm jobs, and their images ([`docs/hpc3_assets.md`](../docs/hpc3_assets.md)) |

**Session secret.** It signs every sign-in cookie, so whoever can read it can sign in as any
account, an admin's included. It is not a setting in `.env`: with `AISCIENTIST_STATE_DIR` set, the
app generates it on first start in `state/session_secret` (mode 0600). `AISCIENTIST_SECRET_KEY_FILE`
names another file, such as a mounted Kubernetes Secret. `AISCIENTIST_SECRET_KEY` in the
environment still wins over the file, and the app warns at every start until it is removed. To
rotate the secret, delete the file and restart: every user signs in again. Without a state
directory the built-in development secret is used, which the app reports loudly at start when
`AISCIENTIST_PUBLIC_HTTPS=1`.

`.env` holds the database and SMTP passwords, so only the service account may read it
(`chown aiscientist:aiscientist .env && chmod 640 .env`, and `getfacl .env` shows no entry for
another group). To change a setting: copy the file to `env-backups/` first
(`sudo -u aiscientist cp -p .env ../env-backups/.env.bak-<date>-<why>`), edit it as the service
account, restart the unit. `/api/auth/config` shows the registration settings the running process
uses.

Never commit a credential. Documents and examples use placeholders such as `<password>`.

## Accounts and what the server stores

- **Sign-in accounts** are rows in PostgreSQL, with the password as a bcrypt hash. A person
  registers with an email address in an allowed domain (`uci.edu`) and a six-digit code sent to
  it, or an admin creates the account. `AISCIENTIST_ALLOW_SELF_REGISTER=0` turns registration off.
  The first admin is created on the server: `aiscientist-admin create-admin <login>`.
- **HPC3 access** is each user's own HPC3 account. They connect with their password and Duo,
  which are not stored, or with a saved SSH key: the server generates an Ed25519 key pair, adds
  the public key to the user's `authorized_keys` on HPC3, and keeps the private key in
  `state/ssh_creds/<account>/` (folder 0700, files 0600, with a passphrase if the user sets one).
- **LLM API keys** that users bring are kept in `state/llm_creds/<account>/` (folder 0700, files
  0600), encrypted with a master key kept outside that folder: `state/llm_key.master` (0600,
  created on first use; `AISCIENTIST_LLM_KEY_MASTER_FILE` moves it). Back it up with the state
  directory: without it every saved key has to be entered again. `AISCIENTIST_LLM_KEY_ENCRYPTION=0`
  stores keys unencrypted.
- **Usage.** Every model call is a row in the `llm_calls` table (tokens, model, chat, run and step,
  failed calls included), priced when it is recorded from
  [`configs/llm_pricing.yaml`](../configs/llm_pricing.yaml). `AISCIENTIST_LLM_PRICING_FILE` points
  at a copy outside the app directory, so prices can change without a deploy. Users see their own
  under Account › API usage; the cost is AiScientist's estimate, not the provider's bill.
  Calls on the cluster GPU are recorded at $0 and shown apart as free.

So the service account can read the session secret and every user's HPC3 key and API key. Keep
`state/` closed to other accounts, and keep the set of people who may run `sudo -u aiscientist`
small.

## The Kubernetes side

Namespace `aiscientist`, applied from [`k8s/host-backend.yaml`](k8s/host-backend.yaml):

| Object | What it does |
|---|---|
| Gateway `aiscientist-gateway` (class `eg`, Envoy Gateway) | listens on 80 and 443 for the host name and ends TLS with the Secret `aiscientist-cert` |
| HTTPRoute `aiscientist` | HTTPS, every path, to the Service |
| HTTPRoute `aiscientist-redirect` | HTTP to HTTPS, 301 |
| Service `aiscientist-app`, without a selector | port 8800 |
| Endpoints `aiscientist-app` | the node's internal IP, port 8800: the host process |
| ClientTrafficPolicy `aiscientist-upload-window` | the HTTP/2 flow-control window of the HTTPS listener: 8 MiB per stream, 32 MiB per connection |

- **Request limit.** No timeout is set on the route, so Envoy Gateway's default applies: a
  request must be answered within 15 seconds of its body arriving. Uploads are chunked and copied
  to HPC3 in the background for this reason. A rule with `timeouts: { request: 0s }` lifts the
  limit for the paths it matches.
- **Upload speed.** Browsers speak HTTP/2 to the proxy. With Envoy Gateway's default window of
  64 KiB per stream, a stream carries at most one window per round trip: under 1 MiB/s from a home
  connection, however fast the line. The ClientTrafficPolicy raises the window for this host's
  HTTPS listener only; the proxy is shared with other sites on the cluster, whose listeners keep
  the default. Check it with `nghttp -nv https://AiScientist.eye.som.uci.edu/`: the server's
  SETTINGS frame must show `SETTINGS_INITIAL_WINDOW_SIZE` 8388608.
- **Certificate.** The certificate for the host name is issued by the university's certificate
  service (InCommon) and is renewed by hand before it expires. Check the date, then replace the Secret; the
  gateway picks the new one up without a restart. The private key never goes into git.

  ```bash
  echo | openssl s_client -connect AiScientist.eye.som.uci.edu:443 \
      -servername aiscientist.eye.som.uci.edu 2>/dev/null | openssl x509 -noout -enddate
  kubectl -n aiscientist create secret tls aiscientist-cert --cert=<fullchain.pem> --key=<key.pem> \
      --dry-run=client -o yaml | kubectl apply -f -
  ```

## A new server

1. PostgreSQL: a role and a database for the app (section 13 of the configuration reference).
2. The service account, `/data/aiscientist` owned by it, then
   `AISCIENTIST_EXTRAS=gateway,auth,anthropic ./deploy.sh` as that account to build the virtual
   environment. `auth` is accounts and PostgreSQL; `anthropic` is for users who bring their own
   Claude key, which is refused when it is verified if the SDK is missing.
3. `state/`, mode 0700, and `.env` in the app directory, readable by the service account only,
   with the settings above.
4. The unit: copy [`systemd/aiscientist.service`](systemd/aiscientist.service) to
   `/etc/systemd/system/`, set `--host` to the address the ingress reaches,
   `sudo systemctl enable --now aiscientist.service`.
5. The ingress: apply [`k8s/host-backend.yaml`](k8s/host-backend.yaml) with the host name and the
   node IP of that cluster, and create the TLS Secret.
6. HPC3: the images, model weights and databases in
   [`docs/hpc3_assets.md`](../docs/hpc3_assets.md).

[`Dockerfile`](Dockerfile), [`k8s/aiscientist.yaml`](k8s/aiscientist.yaml) and
[`nginx/`](nginx/aiscientist.example.conf) describe two other shapes, the app in a pod and the app
behind nginx on a bare host. The lab server uses neither, and no test exercises them.

## Check before opening a server to the internet

- [ ] The session secret is the app's own file:
      `sudo -u aiscientist stat -c '%a %U %n' /data/aiscientist/state/session_secret` shows
      `600 aiscientist`, and no `AISCIENTIST_SECRET_KEY` is set.
- [ ] `.env` is readable by the service account only.
- [ ] `AISCIENTIST_PUBLIC_HTTPS=1`: the session cookie shows `Secure` and `HttpOnly` in the browser.
- [ ] Port 8800 answers only on the address the ingress uses, not on the public one.
- [ ] HTTP redirects to HTTPS, and the certificate is valid for the host name.
- [ ] `AISCIENTIST_STATE_DIR` is outside the app directory and closed to other accounts.
- [ ] `AISCIENTIST_ALLOWED_EMAIL_DOMAINS` lists only the domains that may register, and
      `/api/auth/config` reports `"email_mode": "smtp"`.
- [ ] The admin password is strong, and no account was created with a default password.
- [ ] `run_code` runs on HPC3 (`AISCIENTIST_RUN_CODE_ON_HPC=1`).
