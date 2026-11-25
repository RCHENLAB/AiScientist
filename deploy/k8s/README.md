# Running AiScientist on Kubernetes

Every release tag publishes a service image, `ghcr.io/rchenlab/aiscientist:<tag>`. The image is
built from the release package, smoke-tested and then pushed. The release notes give its
`docker pull` command and its digest. This guide runs that image as one pod. The lab server takes
another route: there the app is a systemd unit on the host, and the cluster only routes to it
([`deploy/README.md`](../README.md), [`host-backend.yaml`](host-backend.yaml)).

## What the image is

```bash
docker pull ghcr.io/rchenlab/aiscientist:<tag>
```

| | |
|---|---|
| Platform | `linux/amd64` |
| Command | `python -m aiscientist.gateway --host 0.0.0.0 --port 8800`, as uid 10001, under `tini` |
| Health | `GET /health` returns `{"status":"ok","version":"<tag>","accounts":true,"skills":"<skills tag>"}` |
| `/app` | the release: its code, its venv built from `requirements.lock`, and the skills release it pins. Owned by root, read-only to the app |
| `/data/aiscientist` | everything the app writes. Mount one persistent volume here. `state/` (0700) holds the session secret, every user's saved HPC3 key, the API keys and their master key. `data/` holds datasets, uploads and run outputs |

The image holds no credential. The settings come from the environment: every setting is described
in [`configs/aiscientist.example.env`](../../configs/aiscientist.example.env).

To look at it on one machine (SQLite in the volume, for a look only):

```bash
docker run --rm -p 127.0.0.1:8800:8800 -v aiscientist-data:/data/aiscientist ghcr.io/rchenlab/aiscientist:<tag>
```

## What the cluster needs

- `linux/amd64` nodes, a default StorageClass with `ReadWriteOnce` volumes, and `kubectl`.
- PostgreSQL. [`postgres.yaml`](postgres.yaml) runs one in the namespace for a trial. For a
  deployment people rely on, use a managed PostgreSQL with backups.
- Egress from the pod: DNS; TCP 22 to the HPC3 login node `hpc3.rcic.uci.edu` and the transfer node
  `access-hpc3.rcic.uci.edu`; TCP 443 to the model providers whose API keys users bring; the SMTP
  server, if registration emails are sent. HPC3's SSH port answers only from the UCI campus network
  or the VPN, so the nodes must be on it.
- An HPC3 side: the images, model weights and shared root in
  [`docs/hpc3_assets.md`](../../docs/hpc3_assets.md), named by the settings in sections 1–8 of the
  configuration reference.

## Quickstart

Run the commands from a checkout of the release, so the manifests match the image:

```bash
TAG=<tag>                                   # e.g. the latest release
git clone --depth 1 --branch "$TAG" https://github.com/RCHENLAB/AiScientist && cd AiScientist
IMAGE="ghcr.io/rchenlab/aiscientist@$(docker buildx imagetools inspect ghcr.io/rchenlab/aiscientist:$TAG | awk '/^Digest:/ {print $2; exit}')"
echo "$IMAGE"                               # the digest the release notes name
kubectl create namespace aiscientist
```

**1. Secrets.** The database password goes in two Secrets: one for PostgreSQL and one for the
app's database URL. Generate the password here; it is never written to a file.

```bash
PGPASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
kubectl -n aiscientist create secret generic aiscientist-postgres --from-literal=POSTGRES_PASSWORD="$PGPASS"
kubectl -n aiscientist create secret generic aiscientist-secrets \
  --from-literal=AISCIENTIST_DATABASE_URL="postgresql+psycopg://aiscientist:$PGPASS@aiscientist-postgres:5432/aiscientist"
```

With a managed PostgreSQL, put its URL in `aiscientist-secrets` and skip step 2. Other credentials
the app reads from the environment, such as `AISCIENTIST_SMTP_PASSWORD`, go in this Secret too.
The session secret is not one of them: the app generates it on its first start in
`state/session_secret` on the volume. To keep it in a Kubernetes Secret instead, mount that Secret
as a file and set `AISCIENTIST_SECRET_KEY_FILE` to its path.

**2. PostgreSQL**, for a trial:

```bash
kubectl -n aiscientist apply -f deploy/k8s/postgres.yaml
kubectl -n aiscientist rollout status statefulset/aiscientist-postgres
```

**3. Settings.** Write the settings without credentials to an env file (`KEY=value`; a comment only
on a line of its own, since `kubectl` keeps an inline comment as part of the value) and load it as a
ConfigMap:

```bash
cat > aiscientist.env <<'EOF'
# Served over HTTPS (step 6): the session cookie is sent over HTTPS only.
# Leave it out to sign in through the port-forward of step 5.
AISCIENTIST_PUBLIC_HTTPS=1
# Code the AI writes is off unless AISCIENTIST_MODEL_WRITTEN_CODE=1; then it runs on HPC3
# as the user's own Slurm job, never in the pod
AISCIENTIST_RUN_CODE_ON_HPC=1
AISCIENTIST_HPC_HOST=hpc3.rcic.uci.edu
AISCIENTIST_HPC_TRANSFER_HOST=access-hpc3.rcic.uci.edu
# The rest of sections 1-8 of configs/aiscientist.example.env: the shared root, Slurm, images, models
EOF
kubectl -n aiscientist create configmap aiscientist-config --from-env-file=aiscientist.env
```

**4. The app.** The manifest's image is a placeholder; set it to the digest:

```bash
sed "s#ghcr.io/rchenlab/aiscientist@sha256:<digest>#$IMAGE#" deploy/k8s/aiscientist.yaml \
  | kubectl -n aiscientist apply -f -
kubectl -n aiscientist rollout status deployment/aiscientist --timeout=10m
kubectl -n aiscientist get pods -l app.kubernetes.io/name=aiscientist     # one pod, READY 1/1
```

[`aiscientist.yaml`](aiscientist.yaml) holds the volume claim (50 GiB), the Deployment and the
Service. The first start migrates the empty database; the startup probe allows five minutes for it.

**5. Check that it answers**, and make the first admin:

```bash
kubectl -n aiscientist port-forward svc/aiscientist 8800:80 &
curl -s http://127.0.0.1:8800/health      # "status":"ok", "version":"<tag>", "accounts":true
kubectl -n aiscientist exec -it deploy/aiscientist -- aiscientist-admin create-admin <login>
```

Sign in at the public address (step 6), or at <http://127.0.0.1:8800/> when
`AISCIENTIST_PUBLIC_HTTPS` is not set. Users connect their own HPC3 account from the console.

**6. A public address.** The gateway streams runs for minutes and takes large uploads, so the route
needs long timeouts and a large body limit. With ingress-nginx:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: aiscientist
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-body-size: "8g"
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
spec:
  ingressClassName: nginx
  tls:
    - hosts: [<host name>]
      secretName: <TLS Secret>
  rules:
    - host: <host name>
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: aiscientist, port: { number: 80 } } }
```

With Envoy Gateway, [`host-backend.yaml`](host-backend.yaml) shows the Gateway, the routes and the
upload window the lab uses. Point its HTTPRoute at the Service `aiscientist`, port 80, and leave
out its selector-less Service and Endpoints. Before opening the site, go through
[Check before opening a server to the internet](../README.md#check-before-opening-a-server-to-the-internet).

## The pod is a singleton

The gateway holds each signed-in user's SSH connection to HPC3, and the tunnels through it, in the
memory of one process. So the Deployment runs one replica with the `Recreate` strategy: the old pod
stops before the new one starts, and a second pod never answers beside the first. Replacing the pod
for an upgrade, a rollback, a node drain or a crash ends every live session:

- users stay signed in: the cookie is signed with the session secret on the volume;
- each user connects to HPC3 again; their Slurm jobs keep running and are found again by name;
- a run that was in progress stops, and can be continued from its chat.

The volume holds the saved keys of every user. Keep `kubectl exec` into the namespace, and access
to the volume, to the operators who may read them.

## Upgrades and rollbacks

Each release names its image digest in its release notes. Before replacing the pod, check that no
run is in progress (a restart interrupts runs):

```bash
kubectl -n aiscientist exec deploy/aiscientist -- aiscientist-admin active-runs     # 0
```

**Upgrade.** Set the new digest, then check that `/health` names the new tag:

```bash
kubectl -n aiscientist set image deployment/aiscientist app=ghcr.io/rchenlab/aiscientist@sha256:<new digest>
kubectl -n aiscientist rollout status deployment/aiscientist --timeout=10m
curl -s http://127.0.0.1:8800/health
```

If `deploy/k8s/aiscientist.yaml` changed between the two tags, apply the new tag's file instead,
with the new digest, as in step 4. The new release migrates the database when it starts.
Migrations only add (a new nullable column, a new table), so the release before it still runs on
the migrated database.

**Rollback.** Go back to the previous image. The database needs no downgrade:

```bash
kubectl -n aiscientist rollout undo deployment/aiscientist
kubectl -n aiscientist rollout status deployment/aiscientist
```

An upgrade and a rollback interrupt the live sessions in the same way.

**Skills.** The image carries the skills release its app release pins, and the MCP servers that
release pins are the ones that match it. In a pod the skills change with the image: a new skills
release comes with a new app release.

**Pre-releases.** A pre-release tag (`vX.Y.ZrcN`) gets an image too. It is for trying a version
before its release, as the lab's staging site does.

## Building the image yourself

From a checkout, for any tag and commit. The image is built from the package, as the release
workflow builds it, and tested the same way:

```bash
deploy/build_package.sh <tag> <commit> dist
deploy/build_image.sh <tag> dist aiscientist:<tag>
deploy/smoke_test_image.sh aiscientist:<tag> <tag>
```

[`build_image.sh`](../build_image.sh) checks the package against `SHA256SUMS` and builds it with the
package's own [`Dockerfile`](../Dockerfile). [`smoke_test_image.sh`](../smoke_test_image.sh) runs
the image with a PostgreSQL of its own and an empty volume, then checks that `/health` names the
tag, the console answers and the database is migrated. It also checks the account and folder modes,
and that the state survives a restart.
