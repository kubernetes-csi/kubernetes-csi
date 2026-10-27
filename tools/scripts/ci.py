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

"""Provide the locked Go version and prepare Cloud Build inputs."""

import argparse
from pathlib import Path
import subprocess

import build_environment
import image_inputs

ROOT = Path(__file__).resolve().parents[2]
GO_VERSION_FILE = "tmp/ci-go-version"


def go_version(root=ROOT):
    return build_environment.load(root)["versions"]["go"].removeprefix("go")


def prepare_cloud(root=ROOT):
    """Validate locked build inputs and pass the Go version to publishing."""
    build_environment.preflight(build_environment.load(root))
    image_inputs.dockerfiles(root)
    output = root / GO_VERSION_FILE
    if output.parent.is_symlink():
        raise ValueError("refusing symlinked Cloud Build handoff directory")
    output.parent.mkdir(exist_ok=True)
    output.write_text(go_version(root) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("go-version", "prepare-cloud"))
    args = parser.parse_args()
    if args.command == "prepare-cloud":
        prepare_cloud()
    else:
        print(go_version())


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"CI preparation failed: {error}") from error
