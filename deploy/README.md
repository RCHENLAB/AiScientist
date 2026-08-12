# Public deployment — <PUBLIC_HOSTNAME>

Bringing the AiScientist console live on the public domain, on **eyeserver (<GATEWAY_HOST>)**,
ports **80 + 443** (IT ticket **INC0907754**).

> **Deployment target: the eyeserver Kubernetes cluster.** The cluster already terminates TLS on
> the shared **:443** and routes multiple subdomains to different Services via its ingress
> controller; AiScientist is added as one more ingress rule. Use **`deploy/Dockerfile` +
> `deploy/k8s/aiscientist.yaml`** (below). The standalone **nginx + systemd** files
> (`deploy/nginx/`, `deploy/systemd/`) are a **bare-host fallback / reference only** — their proxy
> settings are the source of the ingress annotations; don't run them alongside the cluster ingress
> (they'd fight over :443).

## For the k8s team — non-negotiable app requirements

This is **not** a stateless web app. Slotting it in like the other subdomains will break it unless:

1. **Single replica.** `replicas: 1`, `strategy: Recreate`. The app holds each session's SSH tunnel
   to HPC3 + connection state in ONE process's memory; a 2nd pod can't serve a session the 1st pod
   owns. Do **not** autoscale it.
2. **Egress to HPC3.** The pod must reach `hpc3.rcic.uci.edu:22` (SSH, via paramiko) + DNS. If a
   NetworkPolicy restricts egress, allow it — without this the whole app is dead.
3. **PostgreSQL is REQUIRED — not SQLite.** This serves the UCI bioinformatics lab now and is
   expected to open to **all of UCI**, possibly shared by **other project groups** → it must handle
   real **concurrency** and multi-tenant durable storage. SQLite is single-writer and hits
   `database is locked` under concurrent access — it is **not acceptable here** (dev/CI only). Run a
   dedicated PostgreSQL (in-cluster StatefulSet with its OWN PVC, or a managed instance) and point
   `BIOAGENT_DATABASE_URL=postgresql+psycopg://...` at it. (eyeserver has a host Postgres 17 on
   127.0.0.1:5432, but it binds loopback only, so a pod can't reach it without config changes — use
   an in-cluster PG instead.)
4. **Persistent volume** for run artifacts (`/data/runs` PVC) — figures/tables/reports per run.
5. **Secrets, no defaults** (OIT's caution): `BIOAGENT_SECRET_KEY` strong & unique (it signs session
   cookies — a known key = forgeable admin sessions; the app refuses quietly: it prints a loud
   startup warning if public + dev secret). Admin seeded from a **bcrypt hash**, never a default
   password. The app ships **no default credentials** — `bioagent-admin` requires an explicit
   password, and no admin exists unless `BIOAGENT_ADMIN_*` is provided.
6. **Ingress annotations** (in the manifest): long proxy timeouts (streaming runs + ~10-min vLLM
   load), 8g bodies (dataset uploads), WebSocket upgrade for `/ws/` (automatic on nginx-ingress).

### Build + deploy (k8s path)
```
docker build -f deploy/Dockerfile -t <registry>/aiscientist:<tag> .
docker push <registry>/aiscientist:<tag>
# fill in <placeholders> + the Secret in deploy/k8s/aiscientist.yaml (don't commit real secrets), then:
kubectl apply -f deploy/k8s/aiscientist.yaml
kubectl -n <ns> rollout status deploy/aiscientist
```
Update workflow: rebuild+push a new image tag, `kubectl set image deploy/aiscientist app=<registry>/aiscientist:<newtag>` (or re-apply). A restart drops live sessions; users reconnect.

> **Image size note:** the image bundles the analysis stack (scanpy/gseapy) + pandoc/texlive
> because the CodeAct sandbox + report renderer run in-pod → GB-scale (texlive is the bulk). It can
> shrink a lot later if tool execution moves to HPC3 (the deferred RemoteCodeSandbox work).

---

## Bare-host fallback (nginx + systemd) — reference only

The steps below run the app directly on a host with its own nginx (not the k8s cluster). Kept for
reference and as the origin of the ingress annotations above.

## Architecture (read this first)

```
browser ──HTTPS──▶ nginx (eyeserver :443, TLS)  ──proxy──▶ uvicorn app (127.0.0.1:8800)
                                                              │ holds per-session SSH tunnels
                                                              ▼
                                                       HPC3 (Qwen3.6 vLLM)
```

- The **app stays bound to `127.0.0.1:8800`** — nginx is the *only* public face. Never bind the
  app to `0.0.0.0`.
- The app is a **stateful singleton** (each session opens its own SSH tunnel to HPC3 + holds
  in-memory connection state). So: **one process, no replicas, no load balancing.** A restart
  drops live sessions; users reconnect. There is no zero-downtime backend update — that is
  inherent to the SSH-tunnel design, not a deployment choice. (RCIC offers no web/k8s/VM
  hosting; this is a self-managed SOM host, so systemd + nginx is the right fit.)

## Prerequisites (not done by these files)

1. **DNS + firewall** — the IT ticket: `<PUBLIC_HOSTNAME>` → `<GATEWAY_HOST>`, open
   inbound `80` + `443`. Confirm with `dig <PUBLIC_HOSTNAME>` and a port check.
2. **TLS certificate** for the domain. Two paths:
   - **SOM/OIT-issued (recommended here):** request the cert via SOM IT (likely bundled with
     the same ticket). Install the fullchain + key at `/etc/ssl/AiScientist/fullchain.pem` and
     `/etc/ssl/AiScientist/privkey.pem` (the paths the nginx conf points at).
   - **Let's Encrypt** (only if the domain is publicly reachable on :80):
     `sudo certbot certonly --webroot -w /var/www/certbot -d <PUBLIC_HOSTNAME>`,
     then switch the `ssl_certificate*` lines in the nginx conf to the `/etc/letsencrypt/...` paths.

## Steps (on eyeserver)

### 1. Production environment (`/data/BioAgent/app/.env`)
Set at minimum:
```
BIOAGENT_SECRET_KEY=<long random string>     # signs session cookies — MUST be strong (see below)
BIOAGENT_PUBLIC_HTTPS=1                       # marks session cookies Secure (HTTPS-only)
BIOAGENT_DATABASE_URL=postgresql+psycopg://bioagent:<pw>@127.0.0.1/bioagent   # accounts
# (HPC3/vLLM settings as already configured)

# Outbound email for registration verification codes — UCI SER (Proofpoint).
# Without BIOAGENT_SMTP_HOST the app runs in DEV mode (codes logged, not emailed), so
# public self-registration will NOT work. Values from the "AiScientist SER" PDF:
BIOAGENT_SMTP_HOST=smtp-us.ser.proofpoint.com
BIOAGENT_SMTP_PORT=587                        # STARTTLS; 25 also permitted by SER
BIOAGENT_SMTP_TLS=starttls
BIOAGENT_SMTP_USER=<Relay User ID>            # GUID starting c5c4dca0... (from the PDF)
BIOAGENT_SMTP_PASSWORD=<relay password>       # from the PDF — keep out of git
BIOAGENT_SMTP_FROM=AiScientist <no-reply@<PUBLIC_HOSTNAME>>   # any uci.edu address
BIOAGENT_ALLOWED_EMAIL_DOMAINS=uci.edu        # who may self-register (subdomains included)
```
Generate a secret: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
> SER DKIM-signs outbound mail so it passes DMARC (delivers to gmail.com etc.). After
> editing `.env`, `sudo systemctl restart bioagent` for the app to pick up the new SMTP
> config. Verify with `curl -s https://<PUBLIC_HOSTNAME>/api/auth/config`
> (`"email_mode":"smtp"` = live; `"dev"` = still logging codes) or the journal line
> `[email] send to … failed:` if the relay rejects a message.
> The app prints a loud `*** SECURITY ... FORGEABLE ***` warning at startup if
> `BIOAGENT_PUBLIC_HTTPS=1` but `BIOAGENT_SECRET_KEY` is still the built-in dev secret — fix
> that before going public (a known key lets anyone forge an admin session).

Create the admin account: `bioagent-admin create-admin <username>` (prompts for the password).

### 2. Run the app under systemd
```
sudo cp deploy/systemd/bioagent.service /etc/systemd/system/bioagent.service
# adjust User=/paths in the unit if needed
sudo systemctl daemon-reload
sudo systemctl enable --now bioagent
systemctl status bioagent           # active?  journalctl -u bioagent -f  for the fingerprint line
```

### 3. nginx in front
```
sudo apt install nginx                                  # if not present
sudo cp deploy/nginx/aiscientist.example.conf /etc/nginx/sites-available/
sudo ln -s ../sites-available/aiscientist.example.conf /etc/nginx/sites-enabled/
# install the TLS cert at the paths in the conf (see Prerequisites)
sudo nginx -t && sudo systemctl reload nginx
```

### 4. Verify
```
curl -I https://<PUBLIC_HOSTNAME>/         # 200, valid cert
# log in through the UI; confirm the session cookie shows Secure + HttpOnly in devtools.
```

## Frequent-update workflow

- **Frontend only** (HTML/JS/CSS): `./scripts/push.sh -y` then refresh the browser — no restart.
- **Backend (.py):** `./deploy/redeploy.sh` — rsyncs code and `systemctl restart bioagent`
  (drops only currently-connected sessions). Confirm the new build via the fingerprint line in
  `journalctl -u bioagent`.

## Public-exposure security checklist

- [ ] `BIOAGENT_SECRET_KEY` is strong and unique (not the dev secret).
- [ ] `BIOAGENT_PUBLIC_HTTPS=1` (session cookies `Secure`).
- [ ] App bound to `127.0.0.1` only; firewall exposes just 80/443 (the app port 8800 is NOT public).
- [ ] Accounts enabled (Postgres + `.[auth]`); admin-created accounts only; admin password is strong.
- [ ] HPC3 credentials are never stored (two-layer identity — only the bcrypt app-password hash).
- [ ] HTTP→HTTPS redirect working; cert valid and auto-renewing (if Let's Encrypt).
