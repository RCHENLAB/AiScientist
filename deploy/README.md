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

The server runs the release layout: each app release is installed from a Git tag into its own
folder, with its own virtual environment, and `app/current` names the one that runs.

| Path | What |
|---|---|
| `/data/aiscientist/app/releases/<tag>/` | one app release: the package's runtime files (the operator scripts in `scripts/` included), its own venv in `.venv/`, links to `config/.env` and `skills/`, and the gene sets copied from `data/reference/genesets/`. Read-only once installed. The newest 5 are kept |
| `/data/aiscientist/app/current`, `app/previous` | links to the running release and to the one before it. `app/previous` exists once a second release has been switched to |
| `/data/aiscientist/app/held` | tags the deploy timer never installs: one rolled back from, or one whose health check failed |
| `/data/aiscientist/config/` | mode 0700: `.env`, the app's settings and secrets ([Configuration](#configuration)); `deploy.env`, the deploy's own settings; the encrypted backups of the credential stores |
| `/data/aiscientist/data/` | `AISCIENTIST_DATA_ROOT`, the users' data ([`docs/dataset_storage.md`](../docs/dataset_storage.md)): `users/<account>/datasets/<uuid>/`, uploads in progress under `temp/<account>/` (an unfinished browser upload is removed 24 hours after its last write, `AISCIENTIST_UPLOAD_TTL_SECONDS`), each run's outputs under `analysis_results/<UCInetID>/<run_id>/` (`AISCIENTIST_RESULTS_DIR`), `corpus_scans/`, `exports/`, `reference/genesets/` (the gene-set libraries, not in git, that each release copies), and `reference/retigene/`: the copy of the RetiGene index that literature searches read, written by `scripts/update_corpus_index.py` (see the `search_corpus` tool's documentation) |
| `/data/aiscientist/state/` | `AISCIENTIST_STATE_DIR`, mode 0700: the session secret (`session_secret`), the users' saved SSH keys (`ssh_creds/`) and LLM API keys (`llm_creds/`) with the master key that encrypts them (`llm_key.master`), the model list once an admin has saved it, `induced_skills/` (`AISCIENTIST_INDUCED_SKILLS_DIR`: skills learned from runs, with their review file), and `skill_requests/` (one 0600 JSON file per run that needed code the AI wrote, read in Admin → Skill requests) |
| `/data/aiscientist/skills/releases/<version>/` | the curated skills: releases of aiscientist-skills, installed and checked by [`deploy_skills.sh`](deploy_skills.sh), read-only. `skills/current` names the one the app reads; without it, the tag `configs/skills_registry.json` pins (found through `AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT`). An app release never ships or deletes them. Without one there are no curated skills |
| `/data/aiscientist/logs/` | `deploy.log`: one line per install, switch, rollback, refusal and prune. The units write to the journal |
| `/data/aiscientist/backups/` | `env/` (mode 0700): copies of `config/.env` taken before an edit; `release-layout-<stamp>/`: the code and venv of the rsync layout, which `migrate_release_layout.sh --rollback` puts back ([below](#moving-a-server-to-releases)); copies taken before a migration; `_archive_<date>/`: items set aside for 30 days before deletion, each with a `MANIFEST.txt` |
| `/etc/systemd/system/aiscientist.service` | the unit, installed from [`systemd/aiscientist.service`](systemd/aiscientist.service), with the drop-in `aiscientist.service.d/release-layout.conf` ([source](systemd/aiscientist.service.d/release-layout.conf)): it runs `app/current/.venv/bin/python` in `app/current` with `config/.env`, and a start fails when `/health` does not answer |
| `/etc/systemd/system/aiscientist-deploy.timer` | starts `aiscientist-deploy.service`, [`deploy_release.sh --auto`](deploy_release.sh), every 10 minutes ([Updating the server](#updating-the-server)) |
| `/etc/systemd/system/aiscientist-keys.service` | the key service ([below](#the-key-service-api-keys-under-their-own-account)), run from `app/current/.venv` |
| `/etc/sudoers.d/aiscientist-restart` | lets the service account restart `aiscientist.service` and `aiscientist-keys.service`, as a release switch does |

Everything under `/data/aiscientist` belongs to the service account `aiscientist`, which has no
login shell. An operator needs an account that may run `sudo -u aiscientist` and
`sudo systemctl start|stop|restart aiscientist.service`. The sudo rule matches the command as
written, so always name the unit with `.service`.

The accounts database is PostgreSQL on the host, reachable only on loopback. The role and the
database are both named `bioagent`.

## Updating the server

A deploy is a release: publish a tag `vX.Y.Z` on RCHENLAB/AiScientist, and the server installs it.

1. **The package.** Pushing the tag runs
   [`.github/workflows/release.yml`](../.github/workflows/release.yml). It builds the tagged
   commit's runtime files with [`build_package.sh`](build_package.sh) (no tests, docs or
   experiments) and attaches `aiscientist-<tag>.tar.gz` and `SHA256SUMS` to the tag's GitHub
   Release. The package's `VERSION` names the tag and the commit.
2. **The install.** GitHub cannot reach the server inside the university network, but the server
   can reach GitHub. Every 10 minutes `aiscientist-deploy.timer` runs
   [`deploy_release.sh --auto`](deploy_release.sh) as the service account. When the latest release
   has a package, is newer than the running one and is not held, it installs it. It switches once
   no run is in progress: a restart interrupts runs, so it tries again at its next start. Then it
   brings the skills to the pinned release ([Skills releases](#skills-releases)).

So a release is deployed by publishing it, and stopped by not publishing it. Nothing is copied to
the server by hand. `AISCIENTIST_APP_TAG` in `config/deploy.env` holds the server at one tag
instead of the latest.

By hand, as the service account, with the scripts of the running release:

```bash
D=/data/aiscientist/app/current/deploy
sudo -u aiscientist $D/deploy_release.sh v0.4.0            # install that tag now: download, verify, install, migrate, switch, restart, check
sudo -u aiscientist $D/deploy_release.sh --prepare v0.4.0  # everything up to the switch; the site keeps running
sudo -u aiscientist $D/deploy_release.sh --rollback        # back to app/previous; the release rolled back from is held
sudo -u aiscientist $D/deploy_release.sh --list            # installed releases, current, previous, held
```

A deploy by name stops while a run is in progress, and takes the tag off the held list.

**What a deploy checks.** Each step before the next: the package against `SHA256SUMS`; the Python
packages installed from the release's `requirements.lock` with `pip install --require-hashes`
(exact versions and hashes, made for the server's Python by
[`scripts/lock_requirements.sh`](../scripts/lock_requirements.sh)); the database migrated; then the
switch, a restart, and `/health`, which must name the new tag with accounts on. Otherwise it
switches back and holds the tag. With the key service installed
([below](#the-key-service-api-keys-under-their-own-account)), every switch restarts
`aiscientist-keys.service` first: it runs the release's code from `app/current`, and the app makes its
API-key calls through it. Keys a user gave for one session only are dropped by that restart.

After a deploy:

```bash
curl -s https://AiScientist.eye.som.uci.edu/health   # {"status":"ok","version":"<tag>","accounts":true,...}
sudo -u aiscientist tail -n 20 /data/aiscientist/logs/deploy.log
systemctl show aiscientist.service -p ActiveState -p NRestarts -p ActiveEnterTimestamp
sudo journalctl -u aiscientist.service -n 50          # the service log
sudo journalctl -u aiscientist-deploy.service -n 50   # what the timer did
```

**Database schema.** It is versioned by Alembic: one revision per change in
[`src/aiscientist/gateway/migrations/versions/`](../src/aiscientist/gateway/migrations/versions/).
`aiscientist-admin migrate` applies the missing ones; a deploy runs it before the switch, and the
app runs it at start. A database from before migrations is recorded as revision `0001` the first
time. A change to a model in `gateway/models.py` needs a revision
(`tests/test_db_migrations.py` fails until it has one), written to only add: a new nullable column, a
new table. The release before it then still works on the migrated database, so a rollback needs no
downgrade.

**A new Python dependency** is a change to `pyproject.toml` and to `requirements.lock`
(`scripts/lock_requirements.sh`, after `pip install uv`), then a release. A large one weighs on every
release's venv: `sentence-transformers` for a local embedding model brings PyTorch, about 2 GB more
per release.

### Skills releases

A skills release is a tag of [`lijinbio/aiscientist-skills`](https://github.com/lijinbio/aiscientist-skills)
whose GitHub Release holds `catalog.json`, the skills zip and `SHA256SUMS`.
[`deploy_skills.sh`](deploy_skills.sh) installs one, as the service account:

```bash
deploy_skills.sh v0.2.0       # download, check, install into skills/releases/0.2.0/, switch skills/current
deploy_skills.sh --rollback   # back to skills/previous; the release rolled back from is held
deploy_skills.sh --list
```

A release is installed only when every file `SHA256SUMS` lists matches and `catalog.json` is the
catalog of that tag, the same check the app makes before it reads a skill. The app reads the
library again when `skills/current` changes, without a restart, and the script waits until
`/health` reports the new release, or switches back. The deploy timer installs the release
`config/deploy.env` pins (`AISCIENTIST_SKILLS_TAG`), else the one the running app release pins in
`configs/skills_registry.json`. A skills release needs no app release.

## Configuration

The app's settings are in `/data/aiscientist/config/.env`. Each release links it as its `.env`,
which the app reads at start, and so do the admin commands
(`/data/aiscientist/app/current/.venv/bin/aiscientist-admin`, run as the service account). Every
setting is described in [`configs/aiscientist.example.env`](../configs/aiscientist.example.env). A
public server needs at least:

| Setting | Why |
|---|---|
| `AISCIENTIST_STATE_DIR` | the server's private files: the session secret and the credential stores. Mode 0700, owned by the service account, outside `app/`, where releases are installed and pruned. |
| `AISCIENTIST_RESULTS_DIR` | the runs' outputs, outside `app/`. A deploy refuses a `config/.env` without it. |
| `AISCIENTIST_PUBLIC_HTTPS=1` | marks the session cookie `Secure` |
| `AISCIENTIST_DATABASE_URL` | PostgreSQL. Without it the app falls back to a local SQLite file, which is for development only. |
| `AISCIENTIST_SMTP_*`, `AISCIENTIST_ALLOWED_EMAIL_DOMAINS` | registration email. Without `AISCIENTIST_SMTP_HOST` the verification code is shown in the browser instead of being emailed. |
| `AISCIENTIST_HPC_*`, `AISCIENTIST_*_ON_HPC`, `AISCIENTIST_*_IMAGE` | the HPC3 side: host, shared root, which lines run as Slurm jobs, and their images ([`docs/hpc3_assets.md`](../docs/hpc3_assets.md)) |

`config/deploy.env` holds the settings of `deploy_release.sh` and `deploy_skills.sh`, described at
the top of [`deploy_release.sh`](deploy_release.sh): `AISCIENTIST_APP_TAG` and
`AISCIENTIST_SKILLS_TAG` pin a release, and `AISCIENTIST_EXTRAS` names the pip extras of each
release's venv. The Eye Server sets the default extras plus `corpus`, which adds CPU PyTorch and
`sentence-transformers` on top of the lock for its copy of the RetiGene index.

**Session secret.** It signs every sign-in cookie, so whoever can read it can sign in as any
account, an admin's included. It is not a setting in `config/.env`: with `AISCIENTIST_STATE_DIR`
set, the app generates it on first start in `state/session_secret` (mode 0600).
`AISCIENTIST_SECRET_KEY_FILE` names another file, such as a mounted Kubernetes Secret.
`AISCIENTIST_SECRET_KEY` in the environment still wins over the file, and the app warns at every
start until it is removed. To rotate the secret, delete the file and restart: every user signs in
again. Without a state directory the built-in development secret is used, which the app reports
loudly at start when `AISCIENTIST_PUBLIC_HTTPS=1`.

`config/.env` holds the database and SMTP passwords, so only the service account may read it
(`chown aiscientist:aiscientist config/.env && chmod 640 config/.env`, and `getfacl config/.env`
shows no entry for another group). To change a setting: copy the file to `backups/env/` first
(`sudo -u aiscientist cp -p /data/aiscientist/config/.env /data/aiscientist/backups/env/.env.bak-<date>-<why>`),
edit it as the service account, restart the unit. `/api/auth/config` shows the registration
settings the running process uses.

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
small. The key service below takes the API keys out of that account's reach.

### The key service (API keys under their own account)

With the key service, the API keys are held by a second process, `aiscientist-keys.service`, which
runs as its own system account `aiscientist-keys`. The web server's account cannot read them,
whatever code runs in it (the agents' tools included).
- **Saving a key.** The web server hands a new or rotated key to the service once and keeps only
  its label, hint and fingerprint.
- **Model calls.** Every model call on a user's key goes through the service's proxy on
  `127.0.0.1:8899`. The service puts the key in the request and sends it only to the address the
  key was saved for.
- **The token.** The web server authenticates with a token (`/etc/aiscientist-keys/token`, 0640
  `aiscientist-keys:aiscientist`). The token lets the web server spend keys, never read them.

Set it up once, as root:

```
sudo bash deploy/keys/install_key_service.sh
```

The script:
- creates the account;
- copies the saved keys and the master key into `/var/lib/aiscientist-keys` (0700);
- installs and starts the unit;
- checks that the service can read as many keys as the web server lists.

It changes nothing the web server uses. Then switch the web server over:

1. Add to `config/.env`:
   ```
   AISCIENTIST_KEY_SERVICE=http://127.0.0.1:8899
   AISCIENTIST_KEY_SERVICE_TOKEN_FILE=/etc/aiscientist-keys/token
   ```
2. Restart it when no run is running.
3. Check that the service holds and can read every saved key:
   ```
   sudo -u aiscientist /data/aiscientist/app/current/.venv/bin/aiscientist-admin key-service-check
   ```
4. Delete the web server's own copies, `state/llm_creds/*/*.key` and `state/llm_key.master`.

From then on:
- **Deploys.** The service runs `app/current/.venv/bin/python`, so every release switch restarts
  it first. By hand: `sudo systemctl restart aiscientist-keys.service` (the script grants the
  admin account this, as it has for `aiscientist.service`).
- **Back-ups.** Back up `/var/lib/aiscientist-keys/llm_key.master`. It is the only copy, and
  without it every saved key has to be entered again.
- **Revoking every key.** `aiscientist-admin purge-llm-keys` asks the service to delete every key.
- **Undo.** `sudo bash deploy/keys/install_key_service.sh --copy-back` puts the keys back into
  `state/`. Then remove the two lines from `config/.env` and restart.

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

A new server starts on the rsync layout ([below](#a-server-on-the-rsync-layout)), then moves to
releases.

1. PostgreSQL: a role and a database for the app (section 13 of the configuration reference).
2. The service account, `/data/aiscientist` owned by it, then
   `AISCIENTIST_EXTRAS=gateway,auth,anthropic ./deploy.sh` as that account to build the virtual
   environment. `auth` is accounts and PostgreSQL; `anthropic` is for users who bring their own
   Claude key, which is refused when it is verified if the SDK is missing.
3. `state/`, mode 0700, and `.env` in the app directory, readable by the service account only,
   with the settings above.
4. The skills: `deploy/deploy_skills.sh <tag>` as the service account.
5. The unit: copy [`systemd/aiscientist.service`](systemd/aiscientist.service) to
   `/etc/systemd/system/`, set `--host` to the address the ingress reaches,
   `sudo systemctl enable --now aiscientist.service`.
6. The ingress: apply [`k8s/host-backend.yaml`](k8s/host-backend.yaml) with the host name and the
   node IP of that cluster, and create the TLS Secret.
7. HPC3: the images, model weights and databases in
   [`docs/hpc3_assets.md`](../docs/hpc3_assets.md).
8. Releases: `migrate_release_layout.sh --tag <tag> --apply --timer`
   ([Moving a server to releases](#moving-a-server-to-releases)).

[`Dockerfile`](Dockerfile), [`k8s/aiscientist.yaml`](k8s/aiscientist.yaml) and
[`nginx/`](nginx/aiscientist.example.conf) describe two other shapes, the app in a pod and the app
behind nginx on a bare host. The lab server uses neither, and no test exercises them.

## Check before opening a server to the internet

- [ ] The session secret is the app's own file:
      `sudo -u aiscientist stat -c '%a %U %n' /data/aiscientist/state/session_secret` shows
      `600 aiscientist`, and no `AISCIENTIST_SECRET_KEY` is set.
- [ ] `config/.env` is readable by the service account only.
- [ ] `AISCIENTIST_PUBLIC_HTTPS=1`: the session cookie shows `Secure` and `HttpOnly` in the browser.
- [ ] Port 8800 answers only on the address the ingress uses, not on the public one.
- [ ] HTTP redirects to HTTPS, and the certificate is valid for the host name.
- [ ] `AISCIENTIST_STATE_DIR` is outside the app directory and closed to other accounts.
- [ ] `AISCIENTIST_ALLOWED_EMAIL_DOMAINS` lists only the domains that may register, and
      `/api/auth/config` reports `"email_mode": "smtp"`.
- [ ] The admin password is strong, and no account was created with a default password.
- [ ] `run_code` runs on HPC3 (`AISCIENTIST_RUN_CODE_ON_HPC=1`).

## A server on the rsync layout

On the rsync layout the code and its `.env` are in `app/`, and the virtual environment, with the
app as an editable install, is in `env/`. A new server is on it until its first release.
[`scripts/sync_deploy.sh`](../scripts/sync_deploy.sh) updates such a server from a checkout on your
machine: it copies the code with rsync, installs the package, restarts the unit through systemd and
checks that it answers. It refuses a server on the release layout.

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
| `--no-restart` | the files should land now and the restart come later; `app/.deployed_sha` then says "NOT yet restarted" |
| `--delete` | files removed from the repository should be removed on the server too. `.env`, the databases, the virtual environment, `runs/`, the credential stores, the gene-set libraries and the skills release are never deleted. |

The script copies the working tree (runtime files only), so deploy a clean checkout of the commit
you mean. `app/.deployed_sha` names the commit that was copied. The gene sets
(`src/aiscientist/tools/genesets/*.gmt`) are not in git (`scripts/fetch_genesets.py` downloads
them); a checkout without them leaves the server's copies as they are.

[`scripts/deploy_interactive.sh`](../scripts/deploy_interactive.sh) does the same with password
prompts, for an operator without an SSH key on the server. It refuses a server on the release
layout too, as the service account before it copies anything into `app/`. `./deploy.sh` builds the virtual environment and `./start.sh`
runs the app in the foreground, for a first installation and for development.

### Moving a server to releases

[`migrate_release_layout.sh`](migrate_release_layout.sh) moves a server on the rsync layout (a new
one, or one rolled back from releases) to releases, once. Run it as root from the package of the
release it moves to:

```bash
cd /tmp
curl -fsSLO https://github.com/RCHENLAB/AiScientist/releases/download/<tag>/aiscientist-<tag>.tar.gz
curl -fsSLO https://github.com/RCHENLAB/AiScientist/releases/download/<tag>/SHA256SUMS
sha256sum -c SHA256SUMS && tar xzf aiscientist-<tag>.tar.gz
sudo bash aiscientist-<tag>/deploy/migrate_release_layout.sh --tag <tag>             # checks; changes nothing
sudo bash aiscientist-<tag>/deploy/migrate_release_layout.sh --tag <tag> --prepare   # the site keeps running
sudo bash aiscientist-<tag>/deploy/migrate_release_layout.sh --tag <tag> --apply     # about a minute down
sudo bash aiscientist-<tag>/deploy/migrate_release_layout.sh --tag <tag> --apply --timer   # the same, and deploy from tags from then on
sudo bash aiscientist-<tag>/deploy/migrate_release_layout.sh --rollback              # back to the rsync layout
```

- The check: the rsync layout, `.env` with the results and state directories outside `app/`, the
  unit, the gene sets, the installed skills release, the server's Python against the lock's,
  access to GitHub and PyPI, free disk, and how many runs are in progress. It changes nothing.
- `--prepare`: `config/.env` (a copy of `app/.env`), `config/deploy.env`, the gene sets in
  `data/reference/genesets/`, `skills/current`, and the release installed in `app/releases/<tag>/` with
  its venv. The database is migrated: recorded as revision `0001`, then the newer revisions; they only
  add, so the running site does not mind. From then on `scripts/sync_deploy.sh` and
  `scripts/deploy_interactive.sh` refuse the server.
- `--apply`: waits for no run in progress, stops the unit, moves the rsync tree (all of `app/` but
  `releases/`) and the venv `env/` into `backups/release-layout-<stamp>/`, links `app/current`,
  installs the unit drop-in [`release-layout.conf`](systemd/aiscientist.service.d/release-layout.conf)
  (the bind address taken from the installed unit), the rule that lets the service account run
  `sudo systemctl restart aiscientist.service` without a password
  (`/etc/sudoers.d/aiscientist-restart`, which also covers `aiscientist-keys.service`) and the timer
  units, starts the unit and checks `/health`. If the check fails it rolls back on its own. A key
  service that runs the venv in `env/` is pointed at `app/current/.venv` and restarted first.
- `--rollback`: the reverse, from the newest backup set. `config/`, `data/reference/genesets/`,
  `skills/current` and the database's revision table stay; the rsync layout does not read them.

[`rehearsal/release_layout.sh`](rehearsal/release_layout.sh) runs every mode against a scratch copy
of a server on the rsync layout (its code, its own venv, a database with rows, the skills release),
with a stand-in for systemd and no root, then deploys an update, rolls it back, switches the skills,
and checks each step.
