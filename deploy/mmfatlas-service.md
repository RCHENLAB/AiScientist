# MMFAtlas (CELLxGENE) — how the service actually runs, and how to fix it

**Status: HEALTHY as of 2026-08-27.** https://mmfatlas.<PUBLIC_HOSTNAME> serves HTTP 200 with the
real CELLxGENE app (the 8,854-cell `Chen_MERFISH_wt2_5` MERFISH dataset). The two causes of
restarts (memory limit, ephemeral-storage eviction) have been addressed and a readiness probe now
makes failures visible — see "Change log". **The listening port is still unpinned**, which remains
the one open fragility; it needs a one-line change in Texera's image.

This note exists because the service has now failed **three times** for the same underlying
reason. Two things to read before debugging it again:

1. **It does not run on the host.** Every investigation so far has started by looking for a Docker
   container or a `mmfatlas` process. Neither exists, and their absence is not the fault.
2. **The app's listening port is not fixed.** It moves between **5005 and 5006** across restarts,
   while the Service's `targetPort` is a hardcoded number. That is the whole bug, and it will keep
   recurring until the port is pinned. See "The actual root cause".

> **Ownership.** MMFAtlas is **not** part of AiScientist/BioAgent, and this document is not an
> operations handover to the AiScientist side. It is a **CELLxGENE** instance owned by the
> **Texera** team. The two services share only the Envoy Gateway (its public `:80`/`:443`) and the
> fact that both TLS keys happened to be generated in one batch. The AiScientist side has only
> ever helped end an outage — three one-line, reversible Service patches — and does not operate
> this service. **The durable fix is in Texera's image and manifest**; see "The permanent fix".
> Run everything below with the Texera/cluster admin account.

---

## Change log — live edits applied to the cluster

**Everything in this section is a live edit to Texera's `mmfatlas` namespace. None of it is in
Texera's own manifest, so a redeploy on their side reverts all of it.** These changes exist to
stop a recurring outage, not to take ownership. Texera should land the same values in their
manifest; the entries below are written so they can be copied across or rolled back verbatim.

Backups of the pre-change objects are on the cluster node at
`~/mmfatlas-backup/{deploy-mmfatlas-cellxgene,svc-mmfatlas}.20260827.yaml`.

### 2026-08-27 — memory, ephemeral-storage and a readiness probe (Deployment `generation` 1 -> 2)

Applied after Jin approved the four recommendations. Covers three of them; **the fourth
(`--port 5005`) was deliberately NOT applied here** — it belongs in Texera's image, and faking it
with a `command:` override would duplicate their entrypoint and silently serve stale data if they
ever change the dataset. That one is written up for Texera to land in the image.

| Field | Before | After | Why |
|---|---|---|---|
| `resources.limits.memory` | `2Gi` | `8Gi` | Confirmed `OOMKilled` on 2026-08-20. It idles at ~950 MiB (47% of the old limit); the node has 258 GB and sits at ~11%, so the tight limit bought nothing |
| `resources.requests.memory` | `512Mi` | `2Gi` | Request was below actual idle usage, which makes the pod a preferred eviction target |
| `resources.requests.ephemeral-storage` | *(unset = 0)* | `2Gi` | The 2026-08-05 eviction message was explicit: *"request is 0"*. Pods that request nothing are evicted first |
| `resources.limits.ephemeral-storage` | *(unset)* | `10Gi` | Bound; actual usage is ~45 MiB |
| `resources.limits.cpu` | `1` | `2` | Differential expression is computed on demand and is CPU-bound |
| `readinessProbe` | *(none)* | `httpGet / :5005`, delay 60 s, period 15 s, timeout 5 s, failureThreshold 4 | **The reason outages were silent.** With no probe the pod reported `Running 1/1 Ready` for 7 days while serving nothing |

Rollback:

```bash
export PATH=$PATH:/data/rke2/bin
kubectl rollout undo deploy/mmfatlas-cellxgene -n mmfatlas      # back to revision 1
# or, from the backup:
kubectl apply -f ~/mmfatlas-backup/deploy-mmfatlas-cellxgene.20260827.yaml
```

**No downtime.** The rollout is gated: `maxUnavailable` rounds to 0 for a single replica, so the
new pod had to pass the readiness probe before the old one was terminated. The old pod kept
serving on 5006 throughout. The new pod got a fresh network namespace and came up on **5005**, as
predicted; `targetPort` was flipped 5006 -> 5005 at the moment it went Ready. Verified after:
HTTPS 200 on four consecutive probes.

**Deliberately not done:**

- **No `livenessProbe`.** While the port can still drift, a liveness probe would turn a
  wrong-port start into a restart loop. Add one *after* `--port` is pinned in the image.
- **The port is still unpinned**, so a restart is still a coin flip between 5005 and 5006. What
  changed is that both known triggers are gone and the readiness probe now makes a drift visible
  in seconds instead of days. Until Texera pins `--port`, the stopgap patch below is still the
  recovery step.
- **The evicted pod `mmfatlas-cellxgene-79fff58dd9-mvfc6` (`Error`, from 2026-08-05) was left in
  place.** It serves nothing; deleting it is safe cleanup whenever Texera wants to.

### 2026-08-27 — `targetPort` 5006 -> 5005 (Service)

Followed the rollout above; see "The stopgap".

### 2026-08-27 (earlier) — `targetPort` 5005 -> 5006 (Service)

Emergency fix ending the 7-day outage that started 2026-08-20.

### 2026-07-31 — `targetPort` 5006 -> 5005 (Service)

Ended the ~22.5 h outage that started 2026-07-30.

### 2026-07-02 — cert install + `targetPort` 5005 -> 5006 (Service, Secret)

Installed the InCommon certificate as Secret `mmfatlas-tls` and removed the stuck cert-manager
ACME annotation. First of the port patches.

---

## Where it runs (this is the part that keeps causing confusion)

| Question | Answer |
|---|---|
| Is it a host process? | **No.** Nothing runs under the `mmfatlas` service account — `ps -u mmfatlas` is empty, and that is *expected*, not a fault. |
| Is it a Docker container? | **No.** `sudo docker ps` / `docker images` will **never** show it. The only Docker daemon on that host runs the Texera `buildx` builder. |
| So what is it? | A **Kubernetes Deployment**, in its **own `mmfatlas` namespace** — not `texera`. The image `alirisheh876/eye-cellxgene` is pulled by **RKE2/containerd**, which Docker cannot see. |
| Is `/data/mmfatlas` used? | **No.** The Deployment declares no `volumes` and no `volumeMounts`; the app's data lives at `/data/cellxgene` *inside* the container. See "Open items". |

`kubectl` is **not on the default PATH** — it lives at `/data/rke2/bin/kubectl`, and it needs an
account with a working kubeconfig for the RKE2 cluster. Use the Texera/cluster admin account.

```bash
export PATH=$PATH:/data/rke2/bin
kubectl get all -n mmfatlas
```

Resources involved:

| Kind | Name | Namespace |
|---|---|---|
| Deployment / Pod | `mmfatlas-cellxgene` | `mmfatlas` |
| Service | `mmfatlas-svc` (port 5005 → targetPort **moves**, see below) | `mmfatlas` |
| HTTPRoute | `mmfatlas-route` | `mmfatlas` |
| Gateway | `mmfatlas-gateway` (merged into the shared Envoy) | `mmfatlas` |
| TLS Secret | `mmfatlas-tls` (InCommon, valid to **2027-01-14**) | `mmfatlas` |

---

## The actual root cause (confirmed 2026-08-27)

The container's entrypoint, read straight off the running process:

```
cellxgene launch --host 0.0.0.0 --disable-annotations /data/cellxgene/Chen_MERFISH_wt2_5_cellxgene.h5ad
```

**There is no `--port` flag.** That single omission is the entire bug. In
`server/common/config/server_config.py` the behaviour splits on whether a port was given:

```python
if self.app__port:                       # --port was passed
    if not is_port_available(...):       # -> refuse to start, with a clear error
        raise ConfigurationError(f"The port selected {self.app__port} is in use, ...")
else:                                    # no --port (our case)
    default_server_port = int(os.environ.get("CXG_SERVER_PORT", DEFAULT_SERVER_PORT))
    self.app__port = find_available_port(self.app__host, default_server_port)
```

and `find_available_port` "tries 5000 ports incremented from the specified port", returning the
first one a plain `bind()` accepts — starting at **5005**. So with no `--port`, the app **silently
takes 5006 whenever 5005 is momentarily unbindable**, and the Service's hardcoded `targetPort`
is then wrong. Observed, in order: **5006** (Apr-Jul) -> **5005** (Jul 30) -> **5005** (Aug 5
pod replacement) -> **5006** (Aug 20 -> now).

### So what is occupying 5005? (nothing else on the machine)

A reasonable first guess is that some other service holds 5005. **It cannot.** The pod is not on
the host network (`hostNetwork` unset; pod IP `10.42.0.x` from the CNI, distinct from the node IP),
so **port 5005 inside that pod is a private namespace** — invisible to, and unblockable by, every
other pod and every host process. Verified directly: no other pod in the cluster declares
5005/5006, and no host process listens on either.

The only thing that can occupy 5005 in that namespace is **CELLxGENE's own previous instance**.
When a container is restarted *in place* (OOMKill, SIGKILL), the pod's sandbox and its **network
namespace survive** — only the app container is recreated. Sockets left by the previous instance
in `TIME_WAIT` therefore persist for ~60 s, and `is_port_available` does a plain `bind()` with **no
`SO_REUSEADDR`**, so during that window 5005 is unbindable and the new instance takes 5006. This is
the classic "Address already in use" that any restarted server hits.

That explains the whole history, including why it looks random: it tracks **whether the previous
instance had recently served traffic when it died**.

| Event | Restart type | Namespace | Recent connections? | Port taken |
|---|---|---|---|---|
| Jul 30 | in place (SIGKILL) | preserved | no (idle) | **5005** |
| Aug 5 | pod **replaced** (evicted) | brand new | n/a | **5005** |
| Aug 20 | in place (**OOMKilled** mid-use) | preserved | yes -> `TIME_WAIT` | **5006** |

> **Correction to the previous version of this document.** It claimed the port was "baked into the
> image, same image -> same port, it cannot drift to 5006 or 5007." **That was wrong**, and the
> 2026-08-27 outage disproved it. The port is chosen at runtime by a port scan. The
> unpinned-`:latest`-image theory offered there for the 5006<->5005 shift was also wrong — the
> image digest never changed. Nothing about the fix depends on the image tag; it depends on
> `--port`.

### The permanent fix (Texera — this ends the recurrence)

**Pin the port in the launch command:**

```
cellxgene launch --host 0.0.0.0 --port 5005 --disable-annotations /data/cellxgene/<file>.h5ad
```

This has to be done **in Texera's image / entrypoint script**, not by overriding `command:` in the
Deployment — the entrypoint also downloads the `.h5ad` before launching, and replacing `command:`
would skip that step and start the app with no data. Once `--port 5005` is fixed, `targetPort:
5005` and `containerPort: 5005` are correct permanently and no one has to patch anything again.

Two things to know about that change, both from the code above:

- **`CXG_SERVER_PORT` is not a substitute.** Setting the env var only changes where the scan
  *starts*; it still calls `find_available_port` and still drifts. Only the `--port` flag takes the
  branch that pins the port.
- **With `--port` set, a busy port becomes a clean crash instead of a silent drift.** CELLxGENE
  raises `ConfigurationError("The port selected 5005 is in use...")` and exits. That is the
  outcome we want: the pod goes `CrashLoopBackOff` — loud and obvious in `kubectl get pods` —
  instead of quietly serving on the wrong port behind a 503. It also **self-heals**, because the
  `TIME_WAIT` entries expire in ~60 s and Kubernetes' restart backoff simply retries.

**Add a `readinessProbe` on the same port.** This is why the outages are *silent*: with no probe,
Kubernetes never checks whether anything is listening, so the pod reports `Running 1/1 Ready` for
hours while serving nothing, and the only symptom is a 503 at the edge. With a probe, a mismatch
shows up immediately as `NotReady` in `kubectl get pods`.

### The image's entrypoint (why `--port` does not strictly require a rebuild)

The whole entrypoint is 14 lines, readable from the running container at `/entrypoint.sh`:

```bash
#!/bin/bash
set -e
datadir=/data/cellxgene
name=Chen_MERFISH_wt2_5_cellxgene
url="https://ndownloader.figshare.com/files/35290042?private_link=..."

mkdir -p "$datadir"
if [ ! -f "$datadir/${name}.h5ad" ]; then      # conditional -- a volume makes this a one-off
  echo "Downloading ${name}.h5ad..."
  python -c "import urllib.request; urllib.request.urlretrieve('$url', '$datadir/${name}.h5ad')"
fi
exec cellxgene launch --host 0.0.0.0 --disable-annotations "$datadir/${name}.h5ad"
```

Cleanest fix is still to add `--port 5005` to that last line **in the image**. But because the
script is this simple, the same result can be had **without touching the image**, by overriding
`command:` in the Deployment with a wrapper that reproduces these steps and adds `--port 5005`.
That is a legitimate stopgap when the image cannot be rebuilt quickly — with one caveat: it
**duplicates** the entrypoint, so if Texera ever changes the real one (new dataset, new URL), the
override silently keeps serving the old thing. Prefer the image change; use the override only as a
bridge, and document it where Texera will see it.

Note also `--disable-annotations`: the annotation/gene-set feature is **switched off**, so there is
no user-created state in the container to preserve. Persistence matters here only for the 32 MB
`.h5ad`, not for user data.

### Three separate problems — fix them in this order

It helps to separate what *triggers* a restart from what turns a restart into an outage:

| Layer | What it is | Consequence if unfixed | Fix |
|---|---|---|---|
| **Trigger** | The 2Gi limit. The pod idles at **~950 MiB (47%)** with no users, and CELLxGENE computes differential expression on demand — so a user browsing can push it over | Restarts every week or two | Raise the limit |
| **Fragility** | The unpinned port. Any restart is a coin flip between 5005 and 5006 | A restart becomes a **total outage** | `--port 5005` |
| **Blindness** | No `readinessProbe`. The pod reports `Running 1/1 Ready` while serving nothing | The outage lasts until a human notices — **7 days**, last time | Add a probe |

**Memory is the trigger, but it is not the fix.** Raising it only makes restarts rarer; it does not
make them safe, and restarts will still happen for reasons that have nothing to do with memory —
the 2026-08-05 restart was a **disk** eviction, and node maintenance or a redeploy would do the
same. Conversely, pinning `--port` alone leaves the OOM kills in place but makes each one
**harmless**: a sub-minute blip instead of a 503. That is why `--port` is the highest-leverage
single change, and why all three are worth doing.

### Can it just wait for 5005 to be released instead?

Yes — and once `--port 5005` is set, **Kubernetes already does this for you**. CELLxGENE exits with
`ConfigurationError` if the port is busy, and `restartPolicy: Always` (already in the manifest)
retries with exponential backoff — 10 s, 20 s, 40 s. The `TIME_WAIT` entries expire in ~60 s, so
the next attempt binds 5005 and the pod goes Ready on its own. The waiting is done by the
orchestrator rather than by a script.

If the `CrashLoopBackOff` status in between is unwanted, the entrypoint can wait explicitly before
launching:

```sh
# wait (max 120s) until 5005 is bindable, then start pinned to it
python3 -c 'import socket,time
for _ in range(120):
    s=socket.socket()
    try: s.bind(("0.0.0.0",5005)); s.close(); break
    except OSError: s.close(); time.sleep(1)'
exec cellxgene launch --host 0.0.0.0 --port 5005 --disable-annotations /data/cellxgene/<file>.h5ad
```

What will **not** help: `terminationGracePeriodSeconds` or a `preStop` hook. An OOM kill is a
`SIGKILL` — there is no grace period to hook into.

### The stopgap (what has been done three times)

Point the Service at whatever port the app actually chose. Reversible, and safe to repeat:

```bash
export PATH=$PATH:/data/rke2/bin
# read the port the app announced, then:
kubectl patch svc mmfatlas-svc -n mmfatlas --type=json \
  -p '[{"op":"replace","path":"/spec/ports/0/targetPort","value":5006}]'   # or 5005
```

---

## Failure history

| Date | Symptom | Cause | Fix |
|---|---|---|---|
| 2026-07-02 | Down (no cert, then 503) | (a) cert-manager ACME stuck → no certificate; (b) app on 5006, Service targeted 5005 | Installed the InCommon cert as Secret `mmfatlas-tls`; patched `targetPort` → 5006 |
| 2026-07-30 → 07-31 | 503 for ~22.5 h | Container SIGKILLed (137); restarted on **5005** while the Service still pointed at 5006 | Patched `targetPort` → 5005 |
| 2026-08-05 | Pod replaced (no outage) | **Evicted — node low on ephemeral-storage.** The pod re-downloads the `.h5ad` to ephemeral storage on every restart | New pod scheduled automatically; fresh namespace, so it took 5005 and kept working |
| 2026-08-20 → 08-27 | 503 for ~7 days | Container **`OOMKilled`** (confirmed reason, 2Gi limit); restarted on **5006** while the Service pointed at 5005 | Patched `targetPort` → 5006 |

All four were plumbing or resource problems. **The CELLxGENE app and its data have never been the
fault.**

---

## How to diagnose it next time (5 commands)

```bash
export PATH=$PATH:/data/rke2/bin
kubectl get pods -n mmfatlas                       # 1. is the pod Running/Ready?
kubectl logs -n mmfatlas deploy/mmfatlas-cellxgene --tail=20   # 2. which port did it announce?
kubectl get svc mmfatlas-svc -n mmfatlas -o jsonpath='{.spec.ports[0].targetPort}{"\n"}'  # 3. does the Service agree?
kubectl get endpoints -n mmfatlas                  # 4. is there a live endpoint?
curl -sS -o /dev/null -w '%{http_code}\n' https://mmfatlas.<PUBLIC_HOSTNAME>/   # 5. public check
```

Reading the result:

- **503 + pod `Running`** → the port mismatch (compare steps 2 and 3). This is the common case.
- **Certificate error** → TLS Secret / listener problem; see [`public-domain-tls.md`](public-domain-tls.md).
- **Pod not Running / CrashLoop / restart count climbing** → resource kill; check
  `kubectl get pod -n mmfatlas -o jsonpath='{.items[*].status.containerStatuses[*].lastState}'`
  for `OOMKilled` vs `Evicted`.

---

## Open items (Texera's call — deliberately not changed)

1. **Pin `--port` and add a readiness probe** — see "The permanent fix". Everything else on this
   list is comfort; this one stops the recurrence.
2. **Set an `ephemeral-storage` request (and give it a volume).** On 2026-08-05 the pod was
   evicted with: *"The node was low on resource: ephemeral-storage ... Container cellxgene was
   using 46428Ki, request is 0."* Two things follow from that message. First, **MMFAtlas was the
   victim, not the cause** — it was using only ~45 MiB. Second, it was picked **because its
   `ephemeral-storage` request is 0**: pods that request nothing are evicted first. Setting a
   modest request (say 1Gi) takes it out of the firing line. The node root filesystem is at
   **87% used (13 GB free, eviction threshold ~5 GB)**, so whatever is actually filling that disk
   is a separate problem worth chasing on the Texera side.

   A volume for `/data/cellxgene` is still worth adding — the entrypoint's download is already
   conditional (`if [ ! -f ... ]`), so a persistent volume means the 32 MB figshare fetch happens
   once instead of on every fresh pod, and startup stops depending on figshare being reachable.
3. **Raise the 2Gi memory limit.** The 2026-08-20 kill is confirmed `OOMKilled`. The node is not
   the constraint — 258 GB RAM, ~11% used, no memory pressure since January — this is the
   container's own cgroup limit. It idles around **950 MiB (47% of the limit) with no users at
   all**, leaving barely 1 GiB of headroom, and CELLxGENE computes differential expression on
   demand — so **a user browsing the atlas can kill it**. `requests` is only 512Mi against a 2Gi
   limit; on a node this empty there is no reason to keep it that tight.
4. **Plain `http://mmfatlas.<PUBLIC_HOSTNAME>` returns 200 instead of redirecting to HTTPS**
   (AiScientist issues a 301). Worth adding a redirect route.
5. **Certificate renewal is due before 2027-01-14.** The private key is in the bundle handed to
   Jin; the renewal procedure is in [`public-domain-tls.md`](public-domain-tls.md).

---

## Did the Texera team's deployment cause this?

**No — not the port failures, and no other service is involved at all.** The mmfatlas Deployment
is still on **revision 1** and its ReplicaSet is 190 days old, so the manifest has never been
redeployed or changed by anyone. And nothing outside this pod can take its port: the pod has its
own network namespace, no other pod in the cluster declares 5005/5006, and no host process listens
on either. The port is taken by **CELLxGENE's own previous instance** — see "So what is occupying
5005?" above.

**The 2026-08-05 eviction is a shared-node effect**, though not a Texera misconfiguration: MMFAtlas
and the Texera workloads share one node's disk, and the kubelet evicted MMFAtlas when free
ephemeral storage crossed the threshold. Whoever fills that disk — including MMFAtlas's own
repeated `.h5ad` downloads — can trigger it. Item 2 above is the fix that takes MMFAtlas out of
that competition.
