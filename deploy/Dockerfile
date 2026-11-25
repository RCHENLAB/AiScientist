# The AiScientist service image: one app release, as the release workflow publishes it at
# ghcr.io/rchenlab/aiscientist. Running it on Kubernetes: deploy/k8s/README.md.
#
# Built from a release package, never from a working tree, by deploy/build_image.sh:
#
#     deploy/build_package.sh <tag> <commit> dist        # or: gh release download <tag> -D dist
#     deploy/build_image.sh <tag> dist <image>
#
# The build context is the unpacked package (aiscientist-<tag>/), so the image holds exactly the
# release's runtime files, its VERSION and its requirements.lock. Inside, it is laid out like one
# release on the lab server:
#
#   /app                the release, with its venv in /app/.venv: owned by root, read-only to the app
#   /app/skills         the skills release that configs/skills_registry.json pins, installed and
#                       checked at build time by deploy/deploy_skills.sh
#   /data/aiscientist   everything the app writes: state/ (session secret, saved HPC3 and API keys),
#                       data/ (datasets, uploads, run outputs). Mount one persistent volume here.
#
# The gateway holds each session's SSH connection to HPC3 in memory: run exactly one container
# (deploy/k8s/aiscientist.yaml: one replica, strategy Recreate). No credential is built in.

ARG PYTHON_IMAGE=python:3.13-slim-trixie
FROM ${PYTHON_IMAGE}

# What the app runs besides Python:
#   openssh-client   sftp, ssh-agent and ssh-add for fast copies to the HPC3 transfer node;
#                    ssh for the corpus index copy
#   rsync            the corpus index copy (scripts/update_corpus_index.py)
#   pandoc, graphviz the report's DOCX and the schematic figures (`dot`)
#   texlive-xetex    the report's PDF where TeX may run on the gateway (accounts off); with accounts
#                    on, PDFs are typeset on HPC3
#   curl             the skills release download at build time
#   tini             PID 1: forwards SIGTERM to the gateway and reaps the ssh-agent processes
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl tini openssh-client rsync \
        graphviz pandoc texlive-xetex texlive-fonts-recommended lmodern \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The Python packages from the release's lock, exact versions and hashes, then the app itself, as
# deploy/deploy_release.sh installs a release on the server. The app is an editable install: it
# finds its configs/, frontend/ and VERSION next to src/.
COPY requirements.lock /app/requirements.lock
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir -q --require-hashes -r requirements.lock
COPY . /app
RUN test -f VERSION \
    && /app/.venv/bin/pip install --no-cache-dir -q --no-deps --no-build-isolation -e /app \
    && /app/.venv/bin/python -m compileall -q /app/src

# The curated skills: the release this app release pins, downloaded, checked against its SHA256SUMS
# and catalog, and linked as /app/skills/current. No app is running yet, so the script's /health
# check is skipped. The MCP servers pinned in this release match this skills release.
RUN tag="$(/app/.venv/bin/python -c 'import json; print(json.load(open("configs/skills_registry.json"))["release_tag"])')" \
    && AISCIENTIST_ROOT=/app AISCIENTIST_DEPLOY_ENV=/nonexistent \
       AISCIENTIST_SKILLS_PYTHON=/app/.venv/bin/python \
       AISCIENTIST_SKILLS_REGISTRY_CONFIG=/app/configs/skills_registry.json \
       bash deploy/deploy_skills.sh "$tag" \
    && test -d /app/skills/current/ \
    && rm -rf /app/logs /app/.deploy.lock

# Least privilege: the app runs as an unprivileged account that cannot change its own code.
RUN useradd --create-home --uid 10001 --user-group --shell /usr/sbin/nologin aiscientist \
    && install -d -o aiscientist -g aiscientist -m 0750 /data/aiscientist /data/aiscientist/data \
    && install -d -o aiscientist -g aiscientist -m 0700 /data/aiscientist/state

ENV PATH=/app/.venv/bin:$PATH \
    AISCIENTIST_STATE_DIR=/data/aiscientist/state \
    AISCIENTIST_DATA_ROOT=/data/aiscientist/data \
    AISCIENTIST_RESULTS_DIR=/data/aiscientist/data/analysis_results \
    MPLBACKEND=Agg \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
    CMD curl -fsS http://127.0.0.1:8800/health >/dev/null || exit 1

# deploy/container_entrypoint.sh makes the state and data folders on a new volume (state/ 0700).
# 0.0.0.0 inside the container: its network namespace is the boundary, and the port is reached only
# through the Service (or `docker run -p 127.0.0.1:8800:8800`).
ENTRYPOINT ["/usr/bin/tini", "--", "/app/deploy/container_entrypoint.sh"]
CMD ["python", "-m", "aiscientist.gateway", "--host", "0.0.0.0", "--port", "8800"]
