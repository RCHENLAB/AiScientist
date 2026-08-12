# Container image for the AiScientist console (AiScientist) — for the eyeserver Kubernetes cluster.
#
# This is the FULL image: it bundles the analysis stack because the app's CodeAct sandbox runs
# generated analysis code in-process on the same host (scanpy/gseapy/matplotlib), and the report
# renderer shells out to pandoc + xelatex + graphviz. So the image is GB-scale (texlive + scanpy
# are the bulk). If you later move tool execution to HPC3 (RemoteCodeSandbox + Singularity), the
# gateway image can drop the analysis stack and shrink dramatically.
#
# Build + push (run from repo root):
#     docker build -f deploy/Dockerfile -t <registry>/aiscientist:$(git rev-parse --short HEAD) .
#     docker push       <registry>/aiscientist:<tag>
#
# The app holds per-session SSH tunnels to HPC3 + in-memory connection state → it is a STATEFUL
# SINGLETON. Run exactly ONE replica (see deploy/k8s/aiscientist.yaml). No external MODEL API is
# used; the only model is Qwen3.6 on HPC3, reached over SSH (paramiko, in-process).

FROM python:3.13-slim

# System tools the app shells out to (apt-only, not pip):
#   graphviz  -> `dot`, deterministic schematic figures
#   pandoc + texlive-xetex (+ recommended fonts) -> PDF/DOCX report rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
        graphviz \
        pandoc \
        texlive-xetex texlive-fonts-recommended lmodern \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching, then the package.
COPY pyproject.toml README.md ./
COPY src ./src
COPY frontend ./frontend
RUN pip install --no-cache-dir ".[gateway,auth,analysis]"

# Least privilege: run as a non-root user; no baked-in credentials of any kind.
RUN useradd -m -u 10001 bioagent \
    && mkdir -p /data/runs \
    && chown -R bioagent:bioagent /app /data
USER bioagent

ENV BIOAGENT_RESULTS_DIR=/data/runs \
    MPLBACKEND=Agg \
    PYTHONUNBUFFERED=1
EXPOSE 8800

# Bind 0.0.0.0 INSIDE the pod: the container network namespace is isolated, and the app is only
# reachable via the k8s Service/Ingress — never published directly. (On a bare host we'd bind
# 127.0.0.1; in k8s the pod boundary is the network boundary.)
CMD ["python", "-m", "bioagent.gateway", "--host", "0.0.0.0", "--port", "8800"]
