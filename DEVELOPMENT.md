# Development

## Build on x86_64 Linux

Run commands from the repository root on an x86_64 Linux host. Install Git,
Python 3, Go 1.26.6, and Make. Docker is needed for container images and E2E.

A fresh checkout includes the generated sources and vendored dependencies.
Build the project directly from the checkout:

```bash
make build BUILD_PLATFORMS="linux amd64"
```

For intentional snapshot updates, see [Assembly tools](tools/README.md). Commit
`cmd/`, `pkg/`, `vendor/`, `go.mod`, and `go.sum` in a dedicated generated-source
commit, separate from maintained-code, CI, and documentation changes.

## Use the build outputs

The build produces three Linux amd64 binaries under `bin/`:

| Binary | Purpose |
| --- | --- |
| `csi-sidecars` | Runs selected attacher, provisioner, resizer, and snapshotter controllers in one process. |
| `snapshot-controller` | Runs the cluster-wide snapshot controller separately. |
| `snapshot-conversion-webhook` | Runs the VolumeSnapshot API conversion webhook. |

Inspect their command-line options:

```bash
./bin/csi-sidecars --help
./bin/snapshot-controller --help
./bin/snapshot-conversion-webhook --help
```

For example, run the AIO controllers against an existing CSI driver socket and
Kubernetes cluster, replacing the paths with your configuration:

```bash
./bin/csi-sidecars \
  --controllers=attacher,provisioner,resizer,snapshotter \
  --csi-address=/path/to/csi.sock \
  --kubeconfig=/path/to/kubeconfig
```

The enabled controllers require the corresponding driver capabilities and
Kubernetes permissions; snapshot support also requires the snapshot CRDs and
separate snapshot-controller. Multiple controllers cannot yet share
`--http-endpoint` or `--metrics-address`.

Build and inspect local container images without publishing them:

```bash
make container BUILD_PLATFORMS="linux amd64"
docker run --rm csi-sidecars:latest --help
docker run --rm snapshot-controller:latest --help
docker run --rm snapshot-conversion-webhook:latest --help
```

For an in-cluster AIO workload, use the locally built `csi-sidecars:latest` image,
make it available to the cluster nodes, mount the driver's CSI socket, and set
`--controllers` and `--csi-address`. Apply the shared permissions with
`kubectl apply -f deploy/rbac.yaml` and set `serviceAccountName: csi-sidecars`.
The shared RBAC uses the `default` namespace and does not deploy a driver or the
standalone snapshot components. See [Deployment guide](deploy/README.md) for
namespace changes and the hostpath test fixture, which is not a production
installation.

## Change maintained sources

Edit entrypoints, configuration, assembly scripts, and locks under `tools/`,
not generated `cmd/`, `pkg/`, Go module metadata, or `vendor/`. Build and test
maintained entrypoint changes directly from the checkout. Only regenerate the
snapshot when changing locked upstream sources or assembly output rules. Review
the maintained hostpath `deploy/rbac.yaml` alongside upstream controller updates.
See [Code layout](CODE_LAYOUT.md) for ownership boundaries.

## Validate changes

CI jobs build and test the generated sources already in the checkout. Normal
`make build` and direct `.prow.sh` invocations use those local files.

After building the binaries and local images, check CLI compatibility and image
contents:

```bash
make verify-entrypoints
python3 -B tools/scripts/verify_artifacts.py images --engine docker --tag latest
```

After code changes, run the full Prow suite in a dedicated x86_64 Linux test
environment with Go 1.26.6, Python 3, Git, Bash, Make, a C compiler, `rsync`,
`curl`, `kubectl`, Linux amd64 Kind v0.28.0, and Docker. It needs network access,
access to Kind's container network and the CSI sanity NodePort, and usable loop
devices in the Kind nodes for block-volume and expansion tests.

Use a disposable checkout and a dedicated Linux user account with its own `HOME`
and `GOPATH`. The harness cleans dependency checkouts with `git clean -fdx`,
recreates the `csi-prow` cluster, and resets `KUBECONFIG` to
`${HOME}/.kube/config`; a separate `KUBECONFIG` alone does not isolate it.

```bash
set -o pipefail
./.prow.sh 2>&1 | tee .prow.sh.log
```

Prow builds and tests the code, installs the committed snapshot-controller
manifests with its local image, deploys hostpath, and runs CSI sanity and
parallel/serial storage E2E suites without publishing images.
Inspect the exit status, suite results, and reports under `ARTIFACTS` (by default
under `CSI_PROW_WORK`). Kubernetes 1.31.9 hostpath is a legacy regression target,
not a production support matrix. See
[Validation strategy](tools/README.md#validation-strategy) for the individual gates.

## CI and Cloud Build

The build, hostpath e2e, and image-security jobs select Go from the builder lock
and build or test the checkout directly. They never
run the upstream sync script. Tooling and spelling jobs inspect maintained
inputs. The legacy hostpath cluster version remains a separate test input.
Hostpath runs on every pull request, weekly, or through manual dispatch. It
uploads logs, JUnit reports, and the source lock even when tests fail.

The root `cloudbuild.yaml` first validates the locked Linux amd64 builder and
Dockerfiles, then records the Go version. The next step uses the same checkout
in Cloud Build's shared `/workspace` and invokes `.cloudbuild.sh`. The publishing
wrapper requires the Go-version handoff and passes that version to release-tools,
which builds all three commands for Linux amd64/arm64 and publishes their images.
The builder digest in the YAML must match the build-environment lock; a tooling
test and runtime preflight enforce this.

Cloud Build uses the files supplied by its source trigger without fetching
upstream controller repositories or restoring Git metadata. Configure the trusted
postsubmit/tag job to use the root config and provide `_GIT_TAG`,
`_PULL_BASE_REF`, and `_STAGING_PROJECT` using the existing release-tools
conventions. This config publishes images; ordinary pull-request validation uses
the Actions build/image jobs without publishing. Trigger and service-account
configuration are managed outside this repository.
