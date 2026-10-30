#!/usr/bin/env bash
#
# Build the runtime-only package of an app release from a commit, as the release workflow does:
#
#   deploy/build_package.sh <tag> <commit> <out dir>
#
# writes <out dir>/aiscientist-<tag>.tar.gz and <out dir>/SHA256SUMS. The files come from the commit
# itself (git archive), never from the working tree, so an untracked or edited file cannot ride
# along. Runtime files only: the code, the console, the pipelines, the configuration templates, the
# deploy scripts, the operator scripts in scripts/ (some run on the server, such as
# update_corpus_index.py) and requirements.lock; no tests, docs, experiments or handoffs. VERSION
# names the tag, the commit and the build time. deploy/deploy_release.sh installs the result.
set -euo pipefail

[ $# -eq 3 ] || { sed -n 2,12p "$0" >&2; exit 2; }
TAG="$1"; COMMIT="$2"; OUT="$3"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "tag must look like v1.2.3 (got '$TAG')" >&2; exit 2; }
SHA="$(git rev-parse --verify "$COMMIT^{commit}")"
NAME="aiscientist-$TAG"
RUNTIME=(src frontend preset_pipelines configs deploy scripts pyproject.toml LICENSE README.md
         requirements.txt requirements-gateway.txt requirements.lock start.sh)

# A release without its lock would install whatever versions PyPI has that day.
git cat-file -e "$SHA:requirements.lock" 2>/dev/null || { echo "$SHA has no requirements.lock" >&2; exit 1; }

# Built on a Mac (a rehearsal), tar would add an AppleDouble ._<name> beside every file with extended
# attributes; on Linux those unpack as real files (Alembic then loads versions/._0001_baseline.py).
export COPYFILE_DISABLE=1
mkdir -p "$OUT"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
git archive --format=tar --prefix="$NAME/" "$SHA" -- "${RUNTIME[@]}" | tar -x -C "$STAGE"
{
  echo "tag=$TAG"
  echo "sha=$SHA"
  echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$STAGE/$NAME/VERSION"
tar --no-xattrs -C "$STAGE" -czf "$OUT/$NAME.tar.gz" "$NAME"
(cd "$OUT" && sha256sum "$NAME.tar.gz" > SHA256SUMS)
cat "$OUT/SHA256SUMS"
