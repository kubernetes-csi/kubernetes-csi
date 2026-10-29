#! /bin/bash
set -e

export GOWORK=off
export PULL_BASE_REF=${PULL_BASE_REF:-main}

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

# Use the same toolchain as the locked source assembly.
CSI_PROW_GO_VERSION_BUILD=$(python3 -B tools/scripts/ci.py go-version)
export CSI_PROW_GO_VERSION_BUILD
export CSI_PROW_GO_VERSION_E2E="$CSI_PROW_GO_VERSION_BUILD"
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
# Read the locked snapshotter revision before sourcing Prow. The install
# functions below use the committed manifests instead of fetching them in CI.
# Separate assignment from export so a rejected lock stops before sourcing Prow.
CSI_SNAPSHOTTER_VERSION=$(python3 -B tools/scripts/assembly_sources.py commit snapshotter)
export CSI_SNAPSHOTTER_VERSION
export CSI_PROW_TESTS="unit sanity parallel serial"
export CSI_PROW_TESTS_SANITY="sanity"

# Sync already produces module-mode vendor content; use it without rewriting.
. release-tools/prow.sh

install_snapshot_crds() {
  local crds="${REPO_DIR}/pkg/snapshotter/client/config/crd"
  for name in volumesnapshotclasses volumesnapshots volumesnapshotcontents; do
    kubectl apply --validate=false -f "${crds}/snapshot.storage.k8s.io_${name}.yaml"
  done
  kubectl wait --for=condition=Established --timeout=2m \
    crd/volumesnapshotclasses.snapshot.storage.k8s.io \
    crd/volumesnapshots.snapshot.storage.k8s.io \
    crd/volumesnapshotcontents.snapshot.storage.k8s.io
}

install_snapshot_controller() {
  local manifests="${REPO_DIR}/pkg/snapshotter/deploy/kubernetes/snapshot-controller"
  local deployment
  kubectl apply -f "${manifests}/rbac-snapshot-controller.yaml"
  kind load docker-image --name csi-prow snapshot-controller:csiprow
  deployment="$(kubectl set image -f "${manifests}/setup-snapshot-controller.yaml" \
    snapshot-controller=snapshot-controller:csiprow --local -o yaml)"
  kubectl apply -f - <<< "$deployment"
  kubectl -n kube-system rollout status deployment/snapshot-controller --timeout=5m
}

main
