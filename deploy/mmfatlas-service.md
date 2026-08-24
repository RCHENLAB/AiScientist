# MMFAtlas (CELLxGENE) — how the service actually runs, and how to fix it

**Status: RESTORED 2026-07-31 19:40 UTC.** https://mmfatlas.<PUBLIC_HOSTNAME> serves HTTP 200 with
the real CELLxGENE app (the 8,854-cell `Chen_MERFISH_wt2_5` MERFISH dataset renders, UMAP +
`majortype`/`subtype` metadata).

This note exists because the service has now failed **twice** for a port-related reason, and both
times the first instinct was to look for it on the host — where it does not exist. Read the
"Where it runs" section before debugging it a third time.

> **Ownership.** MMFAtlas is **not** part of AiScientist/BioAgent. It is a **CELLxGENE** instance
> owned by the **Texera** team. The two services share only the shared Envoy Gateway (its public
> `:80`/`:443`) and the fact that Yijun generated both TLS keys in one batch. Yijun has helped
> twice with one-line, reversible gateway/service wiring — not as the owner. Anything in the
> "Open items" section below is Texera's to decide.

---

## Where it runs (this is the part that keeps causing confusion)

| Question | Answer |
|---|---|
| Is it a host process? | **No.** Nothing runs under the `mmfatlas` service account — `ps -u mmfatlas` is empty, and that is *expected*, not a fault. |
| Is it a Docker container? | **No.** `sudo docker ps` / `docker images` will **never** show it. The only Docker daemon on that host runs the Texera `buildx` builder. |
| So what is it? | A **Kubernetes Deployment**, in its **own `mmfatlas` namespace** — not `texera`. The image `alirisheh876/eye-cellxgene` is pulled by **RKE2/containerd**, which Docker cannot see. |
| Is `/data/mmfatlas` used? | **No** — see "Open items". The Deployment declares no `volumes` and no `volumeMounts`. |

`kubectl` is **not on the default PATH** — it lives at `/data/rke2/bin/kubectl`, and you need the
`<ucinetid>-admin` account (which has the working kubeconfig), not the plain `<ucinetid>`.

```bash
export PATH=$PATH:/data/rke2/bin
kubectl get all -n mmfatlas
```

Resources involved:

| Kind | Name | Namespace |
|---|---|---|
| Deployment / Pod | `mmfatlas-cellxgene` | `mmfatlas` |
| Service | `mmfatlas-svc` (port 5005 → targetPort 5005) | `mmfatlas` |
| HTTPRoute | `mmfatlas-route` | `mmfatlas` |
| Gateway | `mmfatlas-gateway` (merged into the shared Envoy) | `mmfatlas` |
| TLS Secret | `mmfatlas-tls` (InCommon, valid to **2027-01-14**) | `mmfatlas` |

---

## What went wrong on 2026-07-30 → 07-31

**Symptom:** `https://mmfatlas.<PUBLIC_HOSTNAME>` returned **503** for ~22.5 hours
(2026-07-30 21:16 UTC → 2026-07-31 19:40 UTC).

**Cause — a stale fix of ours, not a Texera regression.** On 2026-07-02 the container that had been
running since 2026-04-08 was serving on port **5006** while `mmfatlas-svc` targeted **5005**, so the
Service was patched `targetPort` 5005→**5006** to restore it. Then:

1. On **2026-07-30 21:16:44 UTC** that long-lived container was **SIGKILLed (exit code 137)** —
   consistent with its 2Gi memory limit, though the events had already rotated so OOM is
   unconfirmed. Kubernetes restarted it one second later.
2. The **fresh** container started CELLxGENE on its **default port 5005**
   (`[cellxgene] Launching! Please go to http://0.0.0.0:5005`), which is also what
   `containerPort: 5005` in the manifest says.
3. The Service was still pointing at **5006**. Nothing listened there, the endpoint refused
   connections, and Envoy correctly returned **503**.

**TLS and routing were never the problem.** `mmfatlas-route` was `Accepted` + `ResolvedRefs`, the
InCommon certificate is valid through 2027-01-14, and the fact that the failure was a 503 rather
than a certificate error was the giveaway.

**Fix applied** — verified first (`pod:5005` → 200, `pod:5006` → connection refused), then:

```bash
export PATH=$PATH:/data/rke2/bin
kubectl patch svc mmfatlas-svc -n mmfatlas --type=json \
  -p '[{"op":"replace","path":"/spec/ports/0/targetPort","value":5005}]'
```

**5005 is the durable value.** It matches Texera's own manifest and `containerPort`, so — unlike
the July patch — it survives redeploys and Texera has nothing to reconcile. Rollback, if ever
needed, is the same command with `5006`.

---

## Failure history

| Date | Symptom | Cause | Fix |
|---|---|---|---|
| 2026-07-02 | Down (no cert, then 503) | (a) cert-manager ACME stuck → no certificate; (b) Service targeted 5005 while the running container served 5006 | Installed the InCommon cert as Secret `mmfatlas-tls`; patched `targetPort` 5005→5006 |
| 2026-07-30 → 07-31 | 503 for ~22.5 h | Container SIGKILLed (137); the fresh one came up on the default 5005 while the Service still pointed at 5006 | Patched `targetPort` back to **5005** (matches the manifest — stable) |

So: **yes, it has gone down before** — this is the second outage in a month, and both were
port/plumbing issues rather than anything wrong with the CELLxGENE app or its data.

---

## How to diagnose it next time (5 commands)

```bash
export PATH=$PATH:/data/rke2/bin
kubectl get pods -n mmfatlas                       # 1. is the pod Running/Ready?
kubectl logs -n mmfatlas deploy/mmfatlas-cellxgene --tail=20   # 2. which port does it announce?
kubectl get svc mmfatlas-svc -n mmfatlas -o jsonpath='{.spec.ports[0].targetPort}{"\n"}'  # 3. does the Service agree?
kubectl get endpoints -n mmfatlas                  # 4. is there a live endpoint?
curl -sS -o /dev/null -w '%{http_code}\n' https://mmfatlas.<PUBLIC_HOSTNAME>/   # 5. public check
```

Reading the result:

- **503 + pod Running** → almost certainly the port mismatch above (steps 2 vs 3).
- **Certificate error** → TLS Secret / listener problem; see [`public-domain-tls.md`](public-domain-tls.md).
- **Pod not Running / CrashLoop** → app or resource-limit problem; check `kubectl describe pod`.

---

## Will this happen again? (why the port will not become 5007)

**A normal restart is now self-healing, and no longer needs a manual patch.** The Deployment sets
**no `command`, no `args`, and no `env`** — the listening port comes entirely from the port baked
into the image's own startup command. Same image → same port, every single restart. It is not
allocated dynamically, so it cannot drift to 5006 or 5007 on its own. With
`targetPort` = `containerPort` = image default = **5005**, all three now agree; the July config was
fragile precisely because it pointed at 5006, a port the manifest never declared.

Two residual risks remain, both Texera's to close:

1. **The image is unpinned.** `alirisheh876/eye-cellxgene` carries **no tag** (so, `:latest`) with
   `imagePullPolicy: IfNotPresent`. It currently resolves to
   `sha256:d8c6208958bcd3f09f1cefaba83b8288cbca716b6e917d514ba21a2393602f9a`. If a new `:latest`
   is pushed **and** the node pulls it, the app can change behaviour — including its port —
   without any manifest change. This is also the **most likely explanation for the 5006 mystery**:
   the container running from 2026-04-08 was almost certainly an older `:latest` that listened on
   5006, and the 07-30 restart picked up the newer cached image, which listens on 5005.
   *Fix: pin a real tag or digest.*
2. **There is no `readinessProbe` and no `livenessProbe`** — this is why the outage was silent.
   Kubernetes never checked whether anything was actually listening, so the pod cheerfully
   reported `Running 1/1 Ready` for 22 hours while serving nothing, and the only symptom was a 503
   at the edge. *Fix: a readiness probe on the HTTP port.* A mismatch would then show up
   immediately as `NotReady` in `kubectl get pods` instead of having to be traced back from the
   public URL.

## Open items (Texera's call — deliberately not changed)

1. **`/data/mmfatlas` and the `mmfatlas` service account are currently unused.** The Deployment
   has no `volumes` and no `volumeMounts`, so nothing is wired to the host. The pod
   **re-downloads `Chen_MERFISH_wt2_5_cellxgene.h5ad` into ephemeral container storage on every
   restart**, which means annotations and gene sets created through the UI are **lost when the
   container restarts**. If the intent behind creating that directory was persistent storage, it
   needs a `hostPath` or PVC in the Texera manifest.
2. **Add a readiness probe** and **pin the image** — see the section above. These two together
   would have turned this outage into an obvious `NotReady` pod instead of a silent 503.
3. **Revisit the 2Gi memory limit.** The container was SIGKILLed (137). The node is not the
   problem — it has 258 GB RAM, sits at ~11% used, and has reported `MemoryPressure: False`
   since January — so this was almost certainly the container's **own cgroup limit**. It idles at
   ~915 MiB (~45% of 2Gi), and CELLxGENE computes differential expression and gene expression on
   demand, which can spike well past that: i.e. **a user browsing the atlas can kill it**. That
   also fits the cadence (23 restarts in 163 days). Confirm with `dmesg -T | grep -i oom` on the
   host, which needs root.
4. **Plain `http://mmfatlas.<PUBLIC_HOSTNAME>` returns 200 instead of redirecting to HTTPS**
   (AiScientist issues a 301). Worth adding a redirect route.
5. **Certificate renewal is due before 2027-01-14.** The private key is in the bundle handed to
   Jin; the renewal procedure is in [`public-domain-tls.md`](public-domain-tls.md).
