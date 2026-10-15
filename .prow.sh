#! /bin/bash
set -e

export GOWORK=off
export PULL_BASE_REF=master
# TODO: Replace this with the standard project image registry.
export REGISTRY_NAME=ghcr.io/mauriciopoppe/csi-sidecars-aio-poc

HW_ARCH=$(uname -m)
if [[ "${HW_ARCH}" == "aarch64" || "${HW_ARCH}" == "arm64" ]]; then
  export CSI_PROW_BUILD_PLATFORMS="linux arm64 arm64"
elif [[ "${HW_ARCH}" == "x86_64" ]]; then
  export CSI_PROW_BUILD_PLATFORMS="linux amd64 amd64"
else
  echo "Unsupported hardware arch $HW_ARCH"
  exit 1
fi
# release-tools does not pass BUILD_PLATFORMS to its separate container build.
BUILD_PLATFORMS_MAKEFLAGS=${CSI_PROW_BUILD_PLATFORMS// /\\ }
export MAKEFLAGS="${MAKEFLAGS:+${MAKEFLAGS} }BUILD_PLATFORMS=${BUILD_PLATFORMS_MAKEFLAGS}"

# Keep local Prow builds aligned with the Cloud Build toolchain default.
export CSI_PROW_GO_VERSION_BUILD="1.26.6"
export CSI_PROW_GO_VERSION_E2E="1.26.6"
# This variable controls:
# - The version to use in kind
# - After pulling the k8s codebase, the tag to checkou to.
#
# Legacy regression only, not a supported production Kubernetes matrix.
export CSI_PROW_KUBERNETES_VERSION="1.31.9"
# Select the exact patch image and its matching kind release before inherited defaults.
# Separate assignment from export so a rejected lock stops before sourcing Prow.
CSI_PROW_KIND_IMAGES=$(python3 -B tools/scripts/image_inputs.py test-image --kubernetes "${CSI_PROW_KUBERNETES_VERSION}")
CSI_PROW_KIND_VERSION=$(python3 -B tools/scripts/image_inputs.py kind-version --kubernetes "${CSI_PROW_KUBERNETES_VERSION}")
export CSI_PROW_KIND_IMAGES CSI_PROW_KIND_VERSION
export CSI_PROW_DEPLOYMENT_SUFFIX=""
export CSI_PROW_DRIVER_VERSION="v1.12.1"
# This variable controls the CRDs for snapshotter and the snapshot-controller
# deployment. The AIO image builds its snapshotter from the locked source
# revision, so the CRDs and snapshot-controller must come from that same
# revision instead of a separately pinned release tag.
# Separate assignment from export so a rejected lock stops before sourcing Prow.
CSI_SNAPSHOTTER_VERSION=$(python3 -B tools/scripts/assembly_sources.py commit snapshotter)
export CSI_SNAPSHOTTER_VERSION
export CSI_PROW_TESTS="unit sanity parallel serial"
export CSI_PROW_TESTS_SANITY="sanity"

# Sync already produces module-mode vendor content; use it without rewriting.
. release-tools/prow.sh

main
