#!/usr/bin/env bash

# Copyright 2021 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This script captures the steps required to successfully
# deploy the hostpath plugin driver.  This should be considered
# authoritative and all updates for this process should be
# done here and referenced elsewhere.

# The script assumes that kubectl is available on the OS path
# where it is executed.

set -e
set -o pipefail

BASE_DIR="$( cd "$( dirname "$0" )" && pwd )"

# KUBELET_DATA_DIR can be set to replace the default /var/lib/kubelet.
# All nodes must use the same directory.
default_kubelet_data_dir=/var/lib/kubelet
: ${KUBELET_DATA_DIR:=${default_kubelet_data_dir}}

# If set, the following env variables override image registry and/or tag for each of the
# images that are still pulled from upstream. They are named after the image name, with
# hyphen replaced by underscore and in upper case. The locally built csi-sidecars and
# snapshot-controller images are never rewritten; attacher, provisioner, resizer and
# snapshotter run inside csi-sidecars.
#
# - CSI_EXTERNAL_HEALTH_MONITOR_CONTROLLER_REGISTRY
# - CSI_EXTERNAL_HEALTH_MONITOR_CONTROLLER_TAG
# - CSI_NODE_DRIVER_REGISTRAR_REGISTRY
# - CSI_NODE_DRIVER_REGISTRAR_TAG
# - HOSTPATHPLUGIN_REGISTRY
# - HOSTPATHPLUGIN_TAG
# - LIVENESSPROBE_REGISTRY
# - LIVENESSPROBE_TAG
#
# Alternatively, it is possible to override all registries or tags with:
# - IMAGE_REGISTRY
# - IMAGE_TAG
# These are used as fallback when the more specific variables are unset or empty.
#
# IMAGE_TAG=canary is ignored for images that are blacklisted in the
# deployment's optional canary-blacklist.txt file. This is meant for
# images which have known API breakages and thus cannot work in those
# deployments anymore. That text file must have the name of the blacklisted
# image on a line by itself, other lines are ignored. Example:
#
#     # The following canary images are known to be incompatible with this
#     # deployment:
#     livenessprobe
#
# Beware that the .yaml files do not have "imagePullPolicy: Always". That means that
# also the "canary" images will only be pulled once. This is good for testing
# (starting a pod multiple times will always run with the same canary image), but
# implies that refreshing that image has to be done manually.
#
# As a special case, 'none' as registry removes the registry name.

run () {
    echo "$@" >&2
    "$@"
}

legacy_leases=(
    external-attacher-leader-hostpath-csi-k8s-io
    hostpath-csi-k8s-io
    external-resizer-hostpath-csi-k8s-io
    external-snapshotter-leader-hostpath-csi-k8s-io
)
legacy_records=()
legacy_lease_fields='{.metadata.uid}{"|"}{.spec.leaseTransitions}{"|"}{.spec.holderIdentity}'

get_lease() {
    kubectl -n default get "lease/$1" --ignore-not-found --request-timeout=5s -o "jsonpath=$2"
}

wait_for_lease() {
    local lease="$1" fields="$2" expected="$3" record deadline=$((SECONDS + 180))
    while (( SECONDS < deadline )); do
        record="$(get_lease "$lease" "$fields")" || return 1
        if [[ "$record" == $expected ]]; then
            printf '%s\n' "$record"
            return 0
        fi
        sleep 1
    done
    echo "ERROR: timed out waiting for Lease $lease to match $expected (last state: $record)" >&2
    return 1
}

# Run this only in Prow's disposable cluster. The create fails if another
# hostpath StatefulSet already exists, so we cannot replace a running driver.
run_legacy_sidecars() {
    local manifest="$1" legacy lease record
    legacy="$(kubectl patch --local -f - --type=strategic --patch-file "${BASE_DIR}/legacy-sidecars.yaml" -o yaml <<< "$manifest")"
    run kubectl create --save-config -f - <<< "$legacy"
    run kubectl -n default rollout status statefulset/csi-hostpathplugin --timeout=5m
    for lease in "${legacy_leases[@]}"; do
        record="$(wait_for_lease "$lease" "$legacy_lease_fields" '?*|[0-9]*|?*')"
        legacy_records+=("$record")
    done

    # Stop all old controllers before replacing the Pod template with AIO.
    run kubectl -n default scale statefulset/csi-hostpathplugin --replicas=0
    run kubectl -n default wait --for=delete pod/csi-hostpathplugin-0 --timeout=2m
}

verify_lease_migration() {
    local fields='{.spec.holderIdentity}{"|"}{.metadata.annotations.aio\.csi\.k8s\.io/migration-status}{"|"}{.metadata.annotations.aio\.csi\.k8s\.io/migration-release-pending}'
    wait_for_lease csi-sidecars-hostpath-csi-k8s-io "$fields" '?*|completed|' >/dev/null

    local i lease uid transitions record expected
    for i in "${!legacy_leases[@]}"; do
        lease="${legacy_leases[i]}"
        IFS='|' read -r uid transitions _ <<< "${legacy_records[i]}"
        expected="$uid|$((transitions + 1))|"
        record="$(get_lease "$lease" "$legacy_lease_fields")"
        if [[ "$record" != "$expected" ]]; then
            echo "ERROR: legacy Lease $lease was not taken over and released (before: ${legacy_records[i]}, after: $record)" >&2
            return 1
        fi
    done
    echo "Verified migration of all four hostpath sidecar Leases"
}

# Users and hostpath e2e share one manually maintained RBAC manifest.
# Image overrides do not change permissions; see deploy/README.md.
echo "applying shared RBAC rules"
run kubectl apply -f "${BASE_DIR}/../rbac.yaml"

# deploy hostpath plugin and registrar sidecar
echo "deploying hostpath components"
for i in $(ls ${BASE_DIR}/hostpath/*.yaml | sort); do
    echo "   $i"
    modified="$(cat "$i" | sed -e "s;${default_kubelet_data_dir}/;${KUBELET_DATA_DIR}/;" | while IFS= read -r line; do
        nocomments="$(echo "$line" | sed -e 's/ *#.*$//')"
        # Preserve the locally built AIO image name used by Prow.
        if echo "$nocomments" | grep -q '^[[:space:]]*image:[[:space:]]*' &&
           ! echo "$nocomments" | grep -q '^[[:space:]]*image:[[:space:]]*csi-sidecars'; then
            # Split 'image: quay.io/k8scsi/csi-attacher:v1.0.1'
            # into image (quay.io/k8scsi/csi-attacher:v1.0.1),
            # registry (quay.io/k8scsi),
            # name (csi-attacher),
            # tag (v1.0.1).
            image=$(echo "$nocomments" | sed -e 's;.*image:[[:space:]]*;;')
            registry=$(echo "$image" | sed -e 's;\(.*\)/.*;\1;')
            name=$(echo "$image" | sed -e 's;.*/\([^:]*\).*;\1;')
            tag=$(echo "$image" | sed -e 's;.*:;;')

            # Variables are with underscores and upper case.
            varname=$(echo $name | tr - _ | tr a-z A-Z)

            # Replace registry and/or tag when overrides are set.
            prefix=$(eval echo \${${varname}_REGISTRY:-${IMAGE_REGISTRY:-${registry}}}/ | sed -e 's;none/;;')
            if [ "$IMAGE_TAG" = "canary" ] &&
               [ -f ${BASE_DIR}/canary-blacklist.txt ] &&
               grep -q "^$name\$" ${BASE_DIR}/canary-blacklist.txt; then
                # Ignore IMAGE_TAG=canary for this particular image because its
                # canary image is blacklisted in the deployment blacklist.
                suffix=$(eval echo :\${${varname}_TAG:-${tag}})
            else
                suffix=$(eval echo :\${${varname}_TAG:-${IMAGE_TAG:-${tag}}})
            fi
            line="$(echo "$nocomments" | sed -e "s;$image;${prefix}${name}${suffix};")"
            echo "        using $line" >&2
        fi
        echo "$line"
    done)"
    if [[ -n "${CSI_PROW_TEST_DRIVER}" && "${i##*/}" == csi-hostpath-plugin.yaml ]]; then
        run_legacy_sidecars "$modified"
    fi
    if ! echo "$modified" | kubectl apply -f -; then
        echo "modified version of $i:"
        echo "$modified"
        exit 1
    fi
done

# Prow installs the committed snapshot-controller manifest with the locally
# built image before this script runs. Ensure that image completes its rollout
# before starting e2e tests. Outside Prow there is no such Deployment.
if kubectl -n kube-system get deployment/snapshot-controller >/dev/null 2>&1; then
    echo "using the locally built snapshot-controller"
    run kubectl -n kube-system set image deployment/snapshot-controller '*=snapshot-controller:csiprow'
    run kubectl -n kube-system rollout status deployment/snapshot-controller --timeout=5m
fi

# Wait for each complete rollout, not just one ready replica. An empty selection
# is a deployment error rather than a successful no-op.
statefulsets="$(kubectl get statefulsets -l app.kubernetes.io/instance=hostpath.csi.k8s.io -o name)"
if [ -z "$statefulsets" ]; then
    echo "ERROR: no hostpath StatefulSets found" >&2
    exit 1
fi
for statefulset in $statefulsets; do
    if ! run kubectl rollout status "$statefulset" --timeout=5m; then
        echo "Deployment:"
        kubectl describe all,role,clusterrole,rolebinding,clusterrolebinding,serviceaccount,storageclass,csidriver --all-namespaces -l app.kubernetes.io/instance=hostpath.csi.k8s.io || true
        echo
        echo "Shared RBAC:"
        kubectl describe -f "${BASE_DIR}/../rbac.yaml" || true
        echo
        echo "Pod logs:"
        kubectl get pods -l app.kubernetes.io/instance=hostpath.csi.k8s.io --all-namespaces -o=jsonpath='{range .items[*]}{.metadata.name}{" "}{range .spec.containers[*]}{.name}{" "}{end}{"\n"}{end}' | while read -r pod containers; do
            for c in $containers; do
                echo
                kubectl logs "$pod" "$c" || true
            done
        done || true
        echo
        echo "ERROR: rollout failed for $statefulset" >&2
        exit 1
    fi
done

# Write the test driver configuration expected by Prow.
if [ "${CSI_PROW_TEST_DRIVER}" ]; then
    verify_lease_migration
    cp "${BASE_DIR}/test-driver.yaml" "${CSI_PROW_TEST_DRIVER}"

    # When testing late binding, pods must be forced to run on the
    # same node as the hostpath driver. external-provisioner currently
    # doesn't handle the case when the "wrong" node is chosen and gets
    # stuck permanently with:
    # error generating accessibility requirements: no topology key found on CSINode csi-prow-worker2
    node="$(kubectl get pods/csi-hostpathplugin-0 -o jsonpath='{.spec.nodeName}')"
    echo >>"${CSI_PROW_TEST_DRIVER}" "ClientNodeName: $node"
fi
