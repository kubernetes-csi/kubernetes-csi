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

"""Run tooling checks or the full assembly inside the locked Linux builder.

The container mounts the checkout itself, so the assembly regenerates the
tree in place; sync.sh verifies the existing outputs are disposable (via
preflight) and removes them before assembling.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess

import build_environment

ROOT = Path(__file__).resolve().parents[2]


def workspace_path(value):
    # Keep mount destinations out of system/tool directories; no shell metacharacters.
    if value != "/workspace" and not re.fullmatch(r"/build/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", value):
        raise argparse.ArgumentTypeError("workspace must be /workspace or a safe path under /build/")
    return value


def container_command(engine, image, checkout, script, workspace="/workspace"):
    workspace_path(workspace)
    command = [engine, "run", "--rm", "--pull=never", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--env", "GOTOOLCHAIN=local",
               "--env", f"CSI_AIO_BUILDER_IMAGE={image}",
               "--volume", f"{checkout}:{workspace}", "--workdir", workspace]
    if engine == "docker" and os.uname().sysname == "Linux":
        # Keep assembled files owned by the checkout user without weakening Git trust.
        command += ["--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp",
                    "--env", "GOPATH=/tmp/go", "--env", "GOCACHE=/tmp/go-build"]
    return command + [image, "bash", "-c", script]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="podman", choices=("podman", "docker"))
    parser.add_argument("--workspace", default="/workspace", type=workspace_path,
                        help="Linux mount path for independent path-variation checks")
    parser.add_argument("--image", help="Optional digest matching the locked builder")
    parser.add_argument("--tooling-only", action="store_true",
                        help="Validate tools/ in the locked builder without assembling")
    parser.add_argument("--update-dependencies", metavar="1.MINOR.PATCH",
                        help="Kubernetes release to align go.mod generation on")
    args = parser.parse_args()
    if args.tooling_only == bool(args.update_dependencies):
        parser.error("provide either --tooling-only or --update-dependencies 1.MINOR.PATCH")
    if args.update_dependencies and not re.fullmatch(r"1\.[0-9]+\.[0-9]+", args.update_dependencies):
        parser.error("--update-dependencies must look like 1.MINOR.PATCH")
    args.image = build_environment.select_image(build_environment.load(ROOT), args.image)
    if args.tooling_only:
        script = '''set -e
python3 -B tools/scripts/build_environment.py preflight
python3 -B tools/scripts/build_environment.py cleanup
bootstrap_python=$(command -v python3)
cleanup_environment() {
    status=$?
    trap - EXIT
    "$bootstrap_python" -B tools/scripts/build_environment.py cleanup || {
        if [ "$status" -eq 0 ]; then status=1; fi
    }
    exit "$status"
}
trap cleanup_environment EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3 -B tools/scripts/build_environment.py bootstrap
source .assembly-env/bin/activate
python3 -B tools/scripts/build_environment.py verify
python3 -B -m unittest discover -s tools/scripts -p '*_test.py'
test -z "$(gofmt -l tools/)"
./release-tools/verify-boilerplate.sh "$PWD/tools"
'''
    else:
        script = f"./tools/scripts/sync.sh --update-dependencies {args.update_dependencies}"
    command = container_command(args.engine, args.image, ROOT, script,
                                workspace=args.workspace)
    return subprocess.run(command).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Isolated assembly failed: {error}") from error
