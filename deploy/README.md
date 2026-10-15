# Deployment RBAC and hostpath e2e

## One shared RBAC manifest

[`rbac.yaml`](rbac.yaml) is the only maintained RBAC manifest for the AIO
sidecars. Users and the hostpath e2e deployment apply this same file:

```bash
kubectl apply -f deploy/rbac.yaml
```

It contains five resources:

- ServiceAccount `csi-sidecars` in `default`.
- ClusterRole `csi-sidecars-runner` and its ClusterRoleBinding.
- Role `csi-sidecars-cfg` in `default` and its RoleBinding.

Configure the driver workload with `serviceAccountName: csi-sidecars`. The
manifest installs permissions only, not a CSI driver or controller Deployment.
It combines the existing attacher, provisioner, resizer, snapshotter, and
health-monitor permissions. Leadership leases, CSIStorageCapacity publishing,
and ReplicaSet owner lookup remain namespace-scoped. It does not grant Secret
or ReferenceGrant access; add narrowly scoped permissions if required by your
driver or enabled features.

For another namespace, update the ServiceAccount, Role and RoleBinding
`metadata.namespace`, all binding subjects' `namespace`, and the workload
namespace consistently. Passing `kubectl -n` alone does not override explicit
namespaces in this file. Use distinct resource names/bindings when deploying
independent CSI installations into the same cluster.

### Manual maintenance

When updating upstream controller code, enabled features, or the health-monitor
image, review the permission changes and edit **only `deploy/rbac.yaml`**. Do not
retain per-controller upstream RBAC copies or a separate e2e variant. Neither
`sync.sh` nor deployment regenerates, downloads, or selects RBAC by version or
image tag. The previous `CSI_*_RBAC` and `UPDATE_RBAC_RULES` settings are unused.

The hostpath script references this file directly; its workload YAML does not
contain another ServiceAccount or RBAC bindings. Changes therefore take effect
for both user deployments and e2e without synchronization between copies.

### Upgrading an existing deployment

Apply the shared RBAC before rolling the workload to ServiceAccount
`csi-sidecars`. `kubectl apply` does **not** delete resources from older
manifests. After confirming that no workloads use the old per-controller
accounts or `csi-hostpathplugin-sa`, manually remove their obsolete bindings,
roles, and accounts as appropriate. Do not delete roles that other CSI
installations still use, or remove the shared RBAC while workloads reference it.

## Kubernetes 1.31 hostpath test deployment

`kubernetes-1.31/` is the legacy fixture selected by `.prow.sh`.
`release-tools/prow.sh` discovers its `deploy.sh`, loads the commands declared
by the root `Makefile`, and invokes it before running the storage tests.

The script applies `deploy/rbac.yaml`, rewrites configurable upstream image
references, deploys the hostpath manifests, retargets the snapshot-controller to
the locally built image, and waits for each StatefulSet rollout with a
five-minute timeout. An empty StatefulSet selection is an error. Failures retain
workload, shared-RBAC, and pod-log diagnostics. When `CSI_PROW_TEST_DRIVER` is set,
it writes the storage test configuration there and pins `ClientNodeName` to the
node hosting the driver.

| File under `kubernetes-1.31/` | Contents |
| --- | --- |
| `hostpath/csi-hostpath-driverinfo.yaml` | The `CSIDriver` object for `hostpath.csi.k8s.io`. |
| `hostpath/csi-hostpath-plugin.yaml` | Single-replica hostpath StatefulSet using the shared `csi-sidecars` ServiceAccount. |
| `hostpath/csi-hostpath-snapshotclass.yaml` | The `VolumeSnapshotClass` selected by the tests. |
| `hostpath/csi-hostpath-testing.yaml` | Test-only `socat` StatefulSet and NodePort service exposing `csi.sock`. Do not install in production. |
| `test-driver.yaml` | Storage-test capabilities copied to `CSI_PROW_TEST_DRIVER`. |

The plugin StatefulSet runs the hostpath driver, node-driver-registrar,
livenessprobe, health monitor, and one `csi-sidecars` container running all four
AIO controllers. Upstream images can be redirected using the registry/tag
variables documented in `deploy.sh`; these overrides do not change RBAC.

The cluster-wide snapshot-controller is separate from the AIO sidecars and this
shared RBAC. The inherited Prow harness still installs its RBAC, CRDs, and
Deployment from `CSI_SNAPSHOTTER_VERSION`, read from the source lock by
`.prow.sh`. `deploy.sh` retargets that Deployment to `snapshot-controller:csiprow`
when it exists, so e2e tests use the locally built binary. That upstream
installation flow is unchanged.

The versioned hostpath manifests are an e2e fixture, not a production deployment.

### Running the tests locally

```bash
python3 -B -m unittest discover -s tools/scripts -p 'deploy_test.py'
```

The `(block volmode)` and `(allowExpansion)` e2e patterns need loop devices,
because the hostpath driver backs those volumes with `losetup`. Under Podman
that requires a rootful machine whose `loop` module was loaded with `max_loop`
greater than zero; a rootless node container cannot create `/dev/loopN` nodes.
