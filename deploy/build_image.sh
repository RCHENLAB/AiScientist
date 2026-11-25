#!/usr/bin/env bash
#
# Build the service image of an app release from its package, as the release workflow does:
#
#   deploy/build_image.sh <tag> <dir> <image>
#
# <dir> holds aiscientist-<tag>.tar.gz and SHA256SUMS: what deploy/build_package.sh writes, or what
# the tag's GitHub Release carries (gh release download <tag> -R RCHENLAB/AiScientist -D <dir>).
# The package is checked against SHA256SUMS and unpacked; the image is built from it with the
# package's own deploy/Dockerfile, for linux/amd64 (the platform requirements.lock is made for), and
# labelled with the tag, the commit and the source repository. Nothing is pushed.
#
# Env: AISCIENTIST_IMAGE_PLATFORM (default linux/amd64), AISCIENTIST_IMAGE_SOURCE (the repository
# the label names, default https://github.com/RCHENLAB/AiScientist).
set -euo pipefail

[ $# -eq 3 ] || { sed -n 2,14p "$0" >&2; exit 2; }
TAG="$1"; DIR="$2"; IMAGE="$3"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?$ ]] \
  || { echo "tag must look like v1.2.3, or v1.2.3a1 / v1.2.3b1 / v1.2.3rc1 for a pre-release (got '$TAG')" >&2; exit 2; }
NAME="aiscientist-$TAG"
PLATFORM="${AISCIENTIST_IMAGE_PLATFORM:-linux/amd64}"
SOURCE="${AISCIENTIST_IMAGE_SOURCE:-https://github.com/RCHENLAB/AiScientist}"
[ -f "$DIR/$NAME.tar.gz" ] && [ -f "$DIR/SHA256SUMS" ] || { echo "$DIR has no $NAME.tar.gz and SHA256SUMS" >&2; exit 1; }

if command -v sha256sum >/dev/null 2>&1; then SUM=(sha256sum); else SUM=(shasum -a 256); fi
(cd "$DIR" && grep " \*\{0,1\}$NAME.tar.gz\$" SHA256SUMS | "${SUM[@]}" -c -) \
  || { echo "$NAME.tar.gz does not match SHA256SUMS" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$DIR/$NAME.tar.gz" -C "$STAGE"
CTX="$STAGE/$NAME"
version_of() { sed -n "s/^$1=//p" "$CTX/VERSION"; }
[ "$(version_of tag)" = "$TAG" ] || { echo "the package's VERSION names $(version_of tag), not $TAG" >&2; exit 1; }
SHA="$(version_of sha)"

docker build --platform "$PLATFORM" -f "$CTX/deploy/Dockerfile" -t "$IMAGE" \
  --label "org.opencontainers.image.title=AiScientist" \
  --label "org.opencontainers.image.description=AiScientist web console and gateway, release $TAG" \
  --label "org.opencontainers.image.source=$SOURCE" \
  --label "org.opencontainers.image.version=$TAG" \
  --label "org.opencontainers.image.revision=$SHA" \
  --label "org.opencontainers.image.created=$(version_of built_at)" \
  --label "org.opencontainers.image.licenses=MIT" \
  "$CTX"
echo "==> built $IMAGE ($TAG, $SHA, $PLATFORM)"
