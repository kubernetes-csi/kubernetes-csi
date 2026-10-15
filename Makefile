CMDS=csi-sidecars snapshot-controller snapshot-conversion-webhook
# Build the assembled root module, independent of a caller's workspace.
export GOWORK := off
all: build

# The maintained AIO entrypoint is incomplete until sync.sh combines it with
# generated controller entrypoints under cmd/csi-sidecars.
TEST_GO_FILTER_CMD = | grep -v '/tools/cmd/csi-sidecars$$'
TEST_VET_FILTER_CMD = | grep -v '/tools/cmd/csi-sidecars$$'
# Retained upstream checkouts may use a different formatter. Check maintained
# and assembled sources, not immutable originals kept for provenance.
TEST_FMT_FILTER_CMD = | grep -v '^./tmp/'

include release-tools/build.make

# The inherited release-tools build-% compiles each command with a plain
# `go build` (stamping main.version from REV). Builds use the vendor/ directory
# automatically when it is present, so a populated tree needs no sync first.

# Verify dual-entrypoint compatibility without adding individual attacher to
# the release images. Native binaries are temporary and never replace bin/.
.PHONY: verify-entrypoints
verify-entrypoints:
	go test $(GOFLAGS_VENDOR) -timeout=5m ./cmd/csi-sidecars/... ./pkg/attacher/cmd/csi-attacher/...
	@set -eu; output=$$(mktemp -d); trap 'rm -rf "$$output"' EXIT; \
		go build $(GOFLAGS_VENDOR) -ldflags '-X main.version=$(REV)' -o "$$output/csi-sidecars" ./cmd/csi-sidecars; \
		go build $(GOFLAGS_VENDOR) -ldflags '-X main.version=$(REV)' -o "$$output/csi-attacher" ./pkg/attacher/cmd/csi-attacher; \
		python3 -B tools/scripts/verify_artifacts.py entrypoints --bin-dir "$$output" --expected-version '$(REV)'

# Regenerate the assembly area (cmd/, pkg/, go.mod, vendor/)
# from the upstream kubernetes-csi repositories. Requires Linux and the locked
# builder; see tools/scripts/sync.sh.
.PHONY: sync
sync:
	@test -n '$(KUBERNETES)' || { echo 'Usage: make sync KUBERNETES=1.MINOR.PATCH'; exit 1; }
	./tools/scripts/sync.sh --update-dependencies '$(KUBERNETES)'

# Extend release-tools' `clean` (which only removes bin/) so it also removes
# the generated assembly area; cleanup.sh leaves tools/ untouched.
.PHONY: clean-generated
clean: clean-generated
clean-generated:
	./tools/scripts/cleanup.sh
