# Public-domain TLS setup — AiScientist / MMFatlas

End-to-end runbook for putting **https://<PUBLIC_HOSTNAME>** (and
**https://mmfatlas.<PUBLIC_HOSTNAME>**) online with the InCommon/Sectigo certificates issued
by Pablo Lozano, and for **renewing** them (they expire every ~199 days).

This is the "public-facing domain configuration" document Jin requested. Read
[`deploy/README.md`](README.md) first for the app-level deployment kit and the non-negotiable
app requirements (stateful singleton, egress to HPC3, PostgreSQL, etc.).

> **Scope note — read this first.** Only **AiScientist** is this project (AiScientist). Its backend
> is the AiScientist console on host `:8800`. **MMFatlas is a different service that is NOT ours** —
> it's a **CELLxGENE single-cell atlas** deployed by the **Texera team** (pod
> `mmfatlas-cellxgene` in the `texera` namespace, already running; its backend is that pod, not
> the AiScientist app). The two only share (a) the **same Envoy Gateway** on `<GATEWAY_HOST>:80/:443`
> (`mergeGateways`), and (b) the fact that **Yijun generated/holds both private keys** (both CSRs
> were sent to Pablo in one batch). So the **TLS issuance/verification/renewal flow (steps 1–4, 8)
> applies to both**, but the **backend wiring (step 6) is AiScientist-only** — for MMFatlas the
> backend already exists; it just needs a listener + HTTPRoute + TLS Secret in the `texera`
> namespace, which is Texera's service to own. We hold the MMFatlas key only to hand it to Jin
> (step 9).

---

## Status at a glance (2026-07-01)

| Step | What | Owner | Status |
|---|---|---|---|
| 1 | Generate CSR + private key per domain (private key stays local) | Yijun | ✅ done 2026-06-21 |
| 2 | Pablo signs the CSRs → returns full-chain certificates | Pablo | ✅ done 2026-06-30 |
| 3 | Verify certs (pairing / chain / SAN / expiry) | Yijun | ✅ done 2026-07-01 |
| 4 | Prepare TLS material (`tls.crt` = leaf+intermediate, `tls.key`) | Yijun | ✅ done 2026-07-01 |
| 5 | Install into the Envoy Gateway (TLS Secret + listener) | Yijun (`<admin-ucinetid>`) | ✅ **done 2026-07-01** |
| 6 | Wire the AiScientist gateway backend → host app `:8800` | Yijun | ✅ **done 2026-07-01** |
| 7 | Public verification (real DNS, browser-trusted cert, 200) | Yijun | ✅ **done 2026-07-01** |
| 8 | Migrate the service account `bioagent` → `aiscientist` | Yijun (`sudo`) | ✅ **done 2026-07-01** |
| 9 | Harden `:8800` — bind to internal NIC only (off the public IP) | Yijun (`sudo`) | ✅ **done 2026-07-01** |
| 10 | Kill the leftover `bioagent` worker process | Yijun (`sudo`) | ✅ **done 2026-07-01** |
| 11 | Hand a private-key backup to Jin (for renewals) — **securely** | Yijun | ⏳ pending |

**One-liner:** **AiScientist is LIVE** at https://<PUBLIC_HOSTNAME> — public DNS,
browser-trusted InCommon cert (`ssl_verify=0`), HTTP→HTTPS 301, app serving behind the Envoy
Gateway, and the app port is off the public NIC. Remaining: hand the encrypted key backup to Jin.

### Security posture of the app port (`:8800`)

The node's INPUT policy is `ACCEPT` with no host firewall on `:8800`, and it's a Calico/kube-proxy
node (manual iptables rules risk being flushed / fighting the CNI). So instead of an iptables rule,
the app is **bound to the internal node IP `<GATEWAY_BIND_IP>` only** (not `0.0.0.0`): Envoy still
reaches it (that's the Endpoints target), but `<GATEWAY_HOST>:8800` (the public/DNS IP) has **no
listener** — verified `Connection refused` from the host. The public face is exclusively `:443`
via Envoy. (Loopback `127.0.0.1:8800` SSH-tunnel access is therefore gone; use the domain.) If
campus-internal access to `.197:8800` ever needs locking too, that's a Calico HostEndpoint /
GlobalNetworkPolicy — not needed given the app is login-gated and off the public IP.

### How it was actually deployed (2026-07-01, via `<admin-ucinetid>` cluster-admin)

The gateways were pre-created 10 days earlier (2026-06-21) with a `cert-manager.io/cluster-issuer:
letsencrypt-prod` annotation trying to auto-issue via ACME — that challenge was **stuck 10 days**
(HTTP-01 never completed), which is why we pivoted to Pablo's manual cert. Go-live steps:

1. **Removed the stuck ACME path** — `kubectl -n aiscientist annotate gateway aiscientist-gateway
   cert-manager.io/cluster-issuer-` then deleted the `Certificate/aiscientist-cert` (it stopped
   respawning once the annotation was gone).
2. **Installed Pablo's cert** as Secret `aiscientist-cert` (`kubernetes.io/tls`) — the name the
   HTTPS listener already referenced → listener `ResolvedRefs=True`, serves the InCommon cert.
3. **Backend wiring** — the cluster is a **single node** `texera.<PUBLIC_HOSTNAME>`, internal IP
   **<GATEWAY_BIND_IP>**; pods reach the host app there. Created selectorless Service +
   Endpoints `aiscientist-app → <GATEWAY_BIND_IP>:8800`, an HTTPRoute (`:443` → backend) and a
   redirect route (`:80` → 301 https). Manifest: `scratchpad/aiscientist-backend.yaml` (also
   inline below).
4. **App bind** — changed systemd `--host 127.0.0.1` → `--host <GATEWAY_BIND_IP>` (loopback isn't
   reachable from the pod network; the internal node IP is, and keeps `:8800` off the public NIC)
   and migrated the unit to `User=aiscientist`.

Verified: `curl https://<PUBLIC_HOSTNAME>/` → 200 with `ssl_verify=0` from off-server;
`<GATEWAY_HOST>:8800` → `Connection refused` (app not on the public NIC).

---

## Actors & domains

| | |
|---|---|
| **Domains** | `<PUBLIC_HOSTNAME>`, `mmfatlas.<PUBLIC_HOSTNAME>` |
| **Server** | eyeserver `<GATEWAY_HOST>` (SOM-managed; RKE2 k8s + host services) |
| **Cert issuer** | **Pablo Lozano** `<plozano@hs.uci.edu>` — generates the certs from our CSRs (InCommon RSA OV SSL CA 3 / Sectigo) |
| **Owner / renewals** | **Jin Li** `<<ucinetid>@hs.uci.edu>` — keeps a private-key copy for future renewals |
| **Operator** | **Yijun Sun** `<<ucinetid>@uci.edu>` — generates CSRs, verifies, deploys |
| **DNS / firewall** | OIT ticket **INC0907754** — `*.<PUBLIC_HOSTNAME>` → `<GATEWAY_HOST>`, open 80/443 (DNS confirmed live) |

---

## Where TLS fits (deployment architecture)

```
browser ──HTTPS :443──▶ Envoy Gateway (RKE2 k8s, shared <GATEWAY_HOST>, mergeGateways)
                          │  per-host listener + TLS Secret (this doc = step 5)
                          │  HTTPRoute: <PUBLIC_HOSTNAME> ─┐
                          ▼                                          │
              selectorless Service + Endpoints ◀────────────────────┘  (step 6)
                          │
                          ▼
              host app  127.0.0.1:8800  (bioagent service account, uvicorn)
                          │  holds per-session SSH tunnels
                          ▼
                    HPC3 (Qwen3.6 vLLM, per-user UCInetID+Duo)
```

The app is a **stateful singleton on the host** (not in a pod). The Envoy Gateway terminates
TLS and proxies into the host port `8800` — hence the "hybrid" backend wiring in step 6.

---

## Step 1 — CSR generation (2026-06-21) ✅

Done on Yijun's laptop. **Private keys never leave the local machine**; only the `.csr` files
go to Pablo. Recorded here so the renewal path is reproducible.

```bash
mkdir -p ~/aiscientist-certs && cd ~/aiscientist-certs
for fq in <PUBLIC_HOSTNAME> mmfatlas.<PUBLIC_HOSTNAME>; do
  openssl req -new -newkey rsa:2048 -nodes \
    -keyout "$fq.key" -out "$fq.csr" \
    -subj "/C=US/ST=California/L=Irvine/O=University of California, Irvine/OU=School of Medicine/CN=$fq" \
    -addext "subjectAltName=DNS:$fq"
  chmod 600 "$fq.key"
done
```

Only the CSRs were zipped and sent (`~/Downloads/aiscientist-csr-for-pablo.zip`, `.csr` only,
**no private key**).

---

## Step 2 — Certificate issuance by Pablo (2026-06-30) ✅

Pablo returned `certs.zip` containing one full-chain PEM per domain:

```
aiscientist_eye_som_uci_edu.pem   (leaf → InCommon RSA OV SSL CA 3 → Sectigo Root R46)
mmfatlas_eye_som_uci_edu.pem      (same chain)
```

Pablo's note: **keep the private keys** so the cert can be renewed on expiry (currently ~199-day
validity, expected to shorten in future). *If the private keys are lost, the CSR for each domain
must be regenerated (step 1).*

---

## Step 3 — Verification (2026-07-01) ✅

Run every check before installing. All passed.

```bash
cd ~/aiscientist-certs
# unpack Pablo's chain
unzip -o ~/Downloads/certs.zip -d /tmp/certs_inspect

for host in aiscientist mmfatlas; do
  pem="/tmp/certs_inspect/${host}_eye_som_uci_edu.pem"
  key="${host}.<PUBLIC_HOSTNAME>.key"

  # (a) chain contents + expiry
  openssl crl2pkcs7 -nocrl -certfile "$pem" | openssl pkcs7 -print_certs -noout \
    | grep -E 'subject|issuer'
  # leaf validity
  openssl x509 -in "$pem" -noout -dates -subject

  # (b) SAN matches the hostname
  openssl x509 -in "$pem" -noout -ext subjectAltName

  # (c) THE critical check — leaf public key must match our private key
  c=$(openssl x509 -in "$pem" -noout -pubkey | openssl md5)
  k=$(openssl pkey -in "$key" -pubout | openssl md5)
  [ "$c" = "$k" ] && echo "$host: KEY↔CERT MATCH ✅" || echo "$host: MISMATCH ❌"
done
```

Verified results:

| Check | aiscientist | mmfatlas |
|---|---|---|
| Chain complete (leaf → InCommon CA 3 → Sectigo Root R46) | ✅ 3 certs | ✅ 3 certs |
| SAN | `DNS:<PUBLIC_HOSTNAME>` | `DNS:mmfatlas.<PUBLIC_HOSTNAME>` |
| **Leaf ↔ local private key** | ✅ pubkey md5 match | ✅ pubkey md5 match |
| Validity | 2026-06-30 → **2027-01-14** | 2026-06-30 → **2027-01-14** |
| Issuer | InCommon RSA OV SSL CA 3 (Sectigo) | same |

> `openssl verify` reports `error 2 at depth 2 ... unable to get issuer certificate` only because
> the USERTrust root above Sectigo R46 isn't in the local trust store — it is **not** a problem;
> browsers ship the Sectigo/USERTrust roots and trust the chain. Each leaf still verifies `OK`.

---

## Step 4 — TLS material preparation (2026-07-01) ✅

Organized alongside the private keys in `~/aiscientist-certs/`:

```bash
cd ~/aiscientist-certs
for host in aiscientist mmfatlas; do
  fq="${host}.<PUBLIC_HOSTNAME>"
  # full chain as delivered (leaf+intermediate+root, correct order)
  cp "/tmp/certs_inspect/${host}_eye_som_uci_edu.pem" "$fq.fullchain.pem"
  # tls.crt = leaf + intermediate (what a server should present; root optional)
  awk 'BEGIN{n=0} /BEGIN CERT/{n++} n<=2{print}' "$fq.fullchain.pem" > "$fq.tls.crt"
done
```

Resulting files (private keys `600`, certs `644`):

```
<PUBLIC_HOSTNAME>.key            ← private key (SECRET, local only)
<PUBLIC_HOSTNAME>.csr            ← CSR (for re-issue/renewal)
<PUBLIC_HOSTNAME>.fullchain.pem  ← leaf+intermediate+root
<PUBLIC_HOSTNAME>.tls.crt        ← leaf+intermediate  → k8s Secret tls.crt
   (same four for mmfatlas.<PUBLIC_HOSTNAME>)
```

---

## Step 5 — Install into the Envoy Gateway (⏳ pending)

The shared `:80/:443` is owned by the cluster's Envoy Gateway (`mergeGateways`), so TLS install
and host routing happen **in Kubernetes**, not with a host nginx. This needs cluster access
(`kubectl` / kubeconfig). Two accounts exist on eyeserver: **`<ucinetid>`** (normal, no kubeconfig,
does NOT run the app) and **`<admin-ucinetid>`** (has `sudo` + a working kubeconfig — used for the
texera/mmfatlas diagnosis). So **Yijun can run these via `<admin-ucinetid>`** — confirm that
kubeconfig has write/`apply` RBAC (only `get` was exercised so far), otherwise ask the cluster
admin to grant it or apply the manifests.

### 5a — TLS Secret

```bash
# per domain; NS = the namespace the app's gateway/route live in
kubectl -n <ns> create secret tls aiscientist-tls \
  --cert=<PUBLIC_HOSTNAME>.tls.crt \
  --key=<PUBLIC_HOSTNAME>.key
```

### 5b — Gateway listener (HTTPS, host-scoped, references the Secret)

```yaml
# add to the shared Gateway (or the aiscientist child gateway under mergeGateways)
- name: aiscientist-https
  protocol: HTTPS
  port: 443
  hostname: <PUBLIC_HOSTNAME>
  tls:
    mode: Terminate
    certificateRefs:
      - kind: Secret
        name: aiscientist-tls
  allowedRoutes:
    namespaces: { from: Same }
```

### 5c — HTTPRoute → backend (see step 6 for the backend object)

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: { name: aiscientist, namespace: <ns> }
spec:
  parentRefs: [{ name: <gateway-name>, sectionName: aiscientist-https }]
  hostnames: ["<PUBLIC_HOSTNAME>"]
  rules:
    - backendRefs: [{ name: aiscientist-host, port: 8800 }]
      # long timeouts (streaming runs + ~10-min vLLM load), WebSocket /ws/ upgrade,
      # large bodies (8g uploads) — set via BackendTrafficPolicy / ClientTrafficPolicy.
```

---

## Step 6 — Wire the gateway backend to the host app (⏳ pending) — **AiScientist only**

*(MMFatlas does not need this — its backend is the existing `mmfatlas-cellxgene` pod in the
`texera` namespace. This step is specific to the AiScientist console.)*

The app runs on the **host** at `127.0.0.1:8800`, not in a pod. Expose it to the cluster as a
**selectorless Service + manual Endpoints** pointing at the host IP:

```yaml
apiVersion: v1
kind: Service
metadata: { name: aiscientist-host, namespace: <ns> }
spec:
  ports: [{ port: 8800, targetPort: 8800 }]
  # no selector — endpoints are set manually
---
apiVersion: v1
kind: Endpoints
metadata: { name: aiscientist-host, namespace: <ns> }
subsets:
  - addresses: [{ ip: <GATEWAY_HOST> }]   # host IP reachable from the pod network
    ports: [{ port: 8800 }]
```

> **⚠️ THE real blocker.** The app binds `127.0.0.1:8800` (host loopback) by default
> (`BIOAGENT_HOST`, systemd `--host 127.0.0.1`). **The pod network cannot reach loopback**, so
> the Endpoints above won't connect until the app binds a CNI-reachable interface. Fix: set
> `BIOAGENT_HOST` to the host's cluster-facing IP (or `0.0.0.0`) **and** add a host firewall rule
> that allows only the pod/CNI CIDR to reach `:8800` while blocking the public internet — the
> public face must stay `:443` via Envoy, never a bare `:8800`. (start.sh's own comment warns
> against bare `0.0.0.0` for exactly this reason; the firewall is what makes it safe.)

---

## Step 7 — Public verification (after 5+6)

```bash
# valid cert + 200 from the public side (test off-VPN, e.g. phone on cellular)
curl -Iv https://<PUBLIC_HOSTNAME>/ 2>&1 | grep -E 'HTTP/|subject:|issuer:'
# then log in through the UI; confirm the session cookie is Secure + HttpOnly,
# a streaming run works (WebSocket /ws/), and a dataset upload (~large body) succeeds.
```

---

## Step 8 — Renewal procedure (every ~199 days)

Certs expire **2027-01-14**. To renew:

1. **Reuse the existing key + CSR** (`~/aiscientist-certs/<fq>.{key,csr}`) — send the same `.csr`
   to Pablo (`<plozano@hs.uci.edu>`), cc Jin. *If the key was lost, regenerate CSR via step 1.*
2. Verify the returned cert (step 3) and rebuild `tls.crt` (step 4).
3. Update the k8s Secret in place (no gateway/route change needed):
   ```bash
   kubectl -n <ns> create secret tls aiscientist-tls \
     --cert=<PUBLIC_HOSTNAME>.tls.crt \
     --key=<PUBLIC_HOSTNAME>.key \
     --dry-run=client -o yaml | kubectl apply -f -
   ```
   Envoy picks up the new Secret with no downtime.

> **Set a reminder for ~2026-12-15** (a few weeks before expiry) to start renewal.

---

## Step 9 — Private-key custody / backup to Jin (⏳ pending)

Jin asked for a copy of the private keys so renewals aren't blocked if Yijun is unavailable.
Reasonable — but **never send a private key over plain email/Slack**. Use one of:

- an **encrypted archive** (`zip -e` / age / gpg), with the passphrase shared over a *different*
  channel; or
- UCI OIT secure file transfer; or
- a shared secrets manager the lab already trusts.

Files to hand over (both domains): `*.key`, `*.csr`, and the issued `*.fullchain.pem`.
**Do not** commit any `.key` to git.

> **MMFatlas note.** The `mmfatlas.*` key is for the Texera team's CELLxGENE service, not
> AiScientist. Yijun is only holding it because both CSRs were generated together. Handing it to
> Jin puts long-term custody with the owner — Jin can pass it on to whoever operates MMFatlas.
> Yijun has no reason to keep operating the MMFatlas cert beyond this handoff.

---

## Service account — run under `aiscientist` (migrate from `bioagent`)

Jin created a dedicated **`aiscientist`** service account (uid 995, `/home/aiscientist`,
`nologin`). The app currently runs under `bioagent` (uid 994) via the systemd unit
`bioagent.service` (enabled). Migrate it — privileged steps need `sudo` (run from
`<admin-ucinetid>`; sudo over a non-TTY SSH won't work, run these in an interactive session):

```bash
sudo systemctl stop bioagent
sudo chown -R aiscientist:aiscientist /data/BioAgent          # app, env, runs, logs, datasets
sudo sed -i 's/^User=.*/User=aiscientist/; s/^Group=.*/Group=aiscientist/' \
     /etc/systemd/system/bioagent.service
# carry the team deploy keys over to the service account:
sudo mkdir -p /home/aiscientist/.ssh && sudo chmod 700 /home/aiscientist/.ssh
sudo cp -n /home/bioagent/.ssh/authorized_keys /home/aiscientist/.ssh/authorized_keys 2>/dev/null || true
sudo chown -R aiscientist:aiscientist /home/aiscientist/.ssh
sudo systemctl daemon-reload && sudo systemctl restart bioagent
# verify: process owner is now aiscientist, and the app answers
ps -eo user,cmd | grep "[b]ioagent.gateway"
curl -sI http://127.0.0.1:8800/ | head -1
```

Repo configs already point at `aiscientist`: [`deploy/systemd/bioagent.service`](systemd/bioagent.service)
(`User=`), [`scripts/sync_deploy.sh`](../scripts/sync_deploy.sh) (`SVC_USER` default + team-key
path). Do the server migration **before** the next `sync_deploy.sh` run so ownership matches.

> The systemd unit file keeps the name `bioagent.service` (only `User=` changed) to avoid
> re-registering the unit; rename to `aiscientist.service` later if desired.

## File inventory

| Location | Contents | In git? |
|---|---|---|
| `~/aiscientist-certs/*.key` | private keys | ❌ never |
| `~/aiscientist-certs/*.csr` | CSRs (renewal) | ❌ (local) |
| `~/aiscientist-certs/*.fullchain.pem`, `*.tls.crt` | issued certs / chains | ❌ (local; public certs, but kept local per lab preference) |
| `~/Downloads/certs.zip` | Pablo's original delivery | ❌ |
| `deploy/public-domain-tls.md` (this file) | the runbook | ✅ |

---

## Open items / ownership

- **Cluster access** — `<ucinetid>` has no `kubectl`/kubeconfig. Steps 5–6 need the cluster admin
  (the team that runs the RKE2 / Envoy Gateway, alongside texera/minio) to apply the manifests
  or grant Yijun a kubeconfig. Four cluster facts still needed to finalize the manifests:
  gateway/GatewayClass name + namespace, how existing TLS is modeled, storageClass, image
  registry (see `deploy/README.md`).
- **Backend reachability** — confirm the pod network can reach the host `:8800` (step 6 caveat).
- **Renewal reminder** — ~2026-12-15.
- **Key backup to Jin** — pending a secure channel (step 9).
