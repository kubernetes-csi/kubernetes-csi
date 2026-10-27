#!/usr/bin/env bash
set -e

# The preceding locked-builder step validates Dockerfiles and records Go.
# Fail before sourcing release-tools (which initializes Go/vendor defaults).
for file in tmp/ci-go-version go.mod go.sum vendor/modules.txt \
  cmd/csi-sidecars/main.go cmd/snapshot-controller/Dockerfile cmd/snapshot-conversion-webhook/Dockerfile; do
  test -s "$file" || { echo "Missing $file in Cloud Build workspace" >&2; exit 1; }
done
IFS= read -r CSI_PROW_GO_VERSION_BUILD < tmp/ci-go-version
[[ "$CSI_PROW_GO_VERSION_BUILD" =~ ^1\.[0-9]+\.[0-9]+$ ]] || { echo "Invalid assembly Go version" >&2; exit 1; }
export CSI_PROW_GO_VERSION_BUILD
# The image lock supports Linux amd64 and arm64, with distinct binary suffixes.
export CSI_PROW_BUILD_PLATFORMS="linux amd64 amd64 -amd64; linux arm64 arm64 -arm64"
export GOWORK=off
. release-tools/prow.sh
# The inherited helper strips through the second "v" when stamping REV. Add
# one for dated tags without a v-prefixed semver release to keep the full tag.
case "$GIT_TAG" in
  v????????-v[0-9]*.[0-9]*.[0-9]*) ;;
  v????????-*) GIT_TAG="v$GIT_TAG" ;;
esac
gcr_cloud_build
