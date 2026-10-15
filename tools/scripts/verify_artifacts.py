#!/usr/bin/env python3

# Copyright 2026 The Kubernetes Authors.
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

"""Smoke-test assembled binaries and images without a cluster or CSI socket."""

import argparse
import filecmp
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

import image_inputs


ROOT = Path(__file__).resolve().parents[2]
# A distinct flag from each command, not just the shared logging flags.
COMMAND_FLAGS = {
    "csi-sidecars": "controllers",
    "snapshot-controller": "retry-crd-interval-max",
    "snapshot-conversion-webhook": "tls-private-key-file",
}


def run(command):
    return subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=30
    )


def check_flags(command, output, flags):
    for flag in flags:
        if not re.search(r"^\s*--?" + re.escape(flag) + r"(?:\s|=|$)", output, re.M):
            raise ValueError(f"{command}: help is missing its expected flag --{flag}")


def check_help(command, output):
    check_flags(command, output, [COMMAND_FLAGS[command]])


def check_image_config(engine, command, image):
    """Assert the whole end-state image config, not just the entrypoint.

    The runtime image is the locked distroless base plus a single copied
    binary, so the built image must expose exactly the command entrypoint and
    carry no default CMD that could silently override it. Checking the full
    config here keeps a tampered Dockerfile from passing the smoke test.
    """
    fmt = "{{json .Config.Entrypoint}}\n{{json .Config.Cmd}}"
    result = run([*engine, "image", "inspect", "--format", fmt, image])
    entrypoint, cmd = (json.loads(line) for line in result.stdout.splitlines())
    if entrypoint != [f"/{command}"]:
        raise ValueError(f"{image}: expected entrypoint /{command}, got {entrypoint!r}")
    if cmd not in (None, []):
        raise ValueError(f"{image}: unexpected default command {cmd!r}")


# Exercise the same attacher configuration through both real entrypoints.
# Keep help last so both flag parsers must validate every preceding argument.
ATTACHER_ARGS = (
    "max-entries=5", "reconcile-sync=13s", "max-grpc-log-length=99",
    "worker-threads=2", "default-fstype=ext4", "timeout=17s",
    "retry-interval-start=3s", "retry-interval-max=2m",
)


def verify_entrypoints(bin_dir, expected_version):
    for command, prefix in (("csi-sidecars", "attacher-"), ("csi-attacher", "")):
        binary = str(bin_dir / command)
        args = ["--" + prefix + arg for arg in ATTACHER_ARGS]
        selection = ["--controllers=attacher"] if prefix else []
        result = run([binary, *selection, *args, "--help"])
        flags = [prefix + arg.split("=", 1)[0] for arg in ATTACHER_ARGS]
        if selection:
            flags.append("controllers")
        check_flags(command, result.stdout + result.stderr, flags)
        # Version must not start controllers or turn their normal return into
        # an errgroup failure. AIO supports it with or without a selection.
        selections = [selection, []] if selection else [[]]
        for selected in selections:
            result = run([binary, *selected, *args, "--version"])
            if result.stdout.strip() != f"{binary} {expected_version}":
                raise ValueError(f"{command}: unexpected version output {result.stdout!r}")
        print(f"{command}: attacher arguments, help, and version verified")


def verify_image(root, engine, command, tag):
    image_inputs.dockerfiles(root)
    image = f"{command}:{tag}"
    binary = root / "bin" / command
    if not binary.is_file():
        raise ValueError(f"Missing reference binary: {binary}; run make build first")
    check_image_config(engine, command, image)

    # Inspect the file without requiring a shell in the distroless image. Use
    # the same container for help, with networking disabled. Always remove it,
    # including when the executable fails or the help command times out.
    container = run([*engine, "create", "--network=none", image, "--help"]).stdout.strip()
    try:
        with tempfile.TemporaryDirectory(prefix="csi-image-smoke-") as directory:
            extracted = Path(directory) / command
            run([*engine, "cp", f"{container}:/{command}", str(extracted)])
            if not filecmp.cmp(binary, extracted, shallow=False):
                raise ValueError(f"{image}: packaged executable differs from {binary}")
        result = run([*engine, "start", "--attach", container])
        # docker start --attach is not sufficient to assert the process exit
        # status across container engines; inspect the stopped container too.
        status = run([*engine, "inspect", "--format", "{{.State.ExitCode}}", container])
        if status.stdout.strip() != "0":
            raise ValueError(f"{image}: --help exited with status {status.stdout.strip()}")
        check_help(command, result.stdout + result.stderr)
        print(f"{image}: entrypoint, executable contents, and help verified")
    finally:
        run([*engine, "rm", "--force", container])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("entrypoints", "images"))
    parser.add_argument("--engine", default="docker", help="Container CLI, e.g. podman")
    parser.add_argument("--tag", default="latest", help="Local image tag to check")
    parser.add_argument("--bin-dir", type=Path, help="Native binaries built for entrypoint checks")
    parser.add_argument("--expected-version", help="Version stamped into the entrypoint binaries")
    args = parser.parse_args()
    if args.mode == "entrypoints" and (args.bin_dir is None or not args.expected_version):
        parser.error("entrypoints requires --bin-dir and --expected-version")
    if args.mode == "images" and (args.bin_dir is not None or args.expected_version is not None):
        parser.error("--bin-dir and --expected-version apply only to entrypoints")
    try:
        if args.mode == "entrypoints":
            verify_entrypoints(args.bin_dir.resolve(), args.expected_version)
        else:
            engine = shlex.split(args.engine)
            if not engine:
                raise ValueError("Container engine must not be empty")
            for command in COMMAND_FLAGS:
                verify_image(ROOT, engine, command, args.tag)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Artifact verification failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout + error.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
