# kubernetes-csi

An experimental assembly of the Kubernetes CSI attacher, provisioner, resizer, and snapshotter sidecars, with standalone snapshot controller and conversion webhook images.

## What this repository builds

The current assembly produces three release binaries and container images:

- `csi-sidecars` runs the selected attacher, provisioner, resizer, and
  snapshotter controllers in one process.
- `snapshot-controller` runs the cluster-wide snapshot controller separately.
- `snapshot-conversion-webhook` runs the VolumeSnapshot API conversion webhook.

## Project status

This project is under active development. The current assembly syncs four CSI
sidecars and validates builds, command-line behavior, image packaging, and a
limited hostpath end-to-end configuration. This coverage does not establish
production readiness for the combined process.

Coordinated lifecycle management, shared diagnostics endpoints, complete
multi-controller in-cluster coverage, and the final release pipeline remain in
progress. Generated assembly sources and vendored dependencies are committed as
a separate snapshot. A fresh checkout and CI build and test that committed
snapshot directly. See the development guide for the current workflow.

## Documentation

- [Development guide](DEVELOPMENT.md) covers building, using the outputs, source
  changes, and local validation.
- [Code layout](CODE_LAYOUT.md) explains the hand-maintained and generated
  source layers.
- [Assembly tools](tools/README.md) documents source locks, synchronization,
  builds, and artifact verification.
- [Deployment guide](deploy/README.md) documents the single
  [RBAC manifest](deploy/rbac.yaml) shared by users and hostpath e2e, plus the
  Kubernetes deployment used by the Prow end-to-end tests.
- [Contributing](CONTRIBUTING.md) covers contribution guidelines and community
  resources.
- [Security policy](SECURITY.md) explains how to report vulnerabilities.

## Community, discussion, contribution, and support

Learn how to engage with the Kubernetes community on the [community page](http://kubernetes.io/community/).

You can reach the maintainers of this project at:

- [Slack channel](https://kubernetes.slack.com/messages/sig-storage)
- [Mailing List](https://groups.google.com/a/kubernetes.io/g/sig-storage)

### Code of conduct

Participation in the Kubernetes community is governed by the [Kubernetes Code of Conduct](code-of-conduct.md).
