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

"""Select the locked Linux builder and bootstrap only hash-verified wheels."""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import venv
import zipfile

import assembly_sources

ROOT = Path(__file__).resolve().parents[2]
LOCK = "tools/assembly/build-environment.lock.json"
ENVIRONMENT = ".assembly-env"
MAX_WHEEL_BYTES = 10 * 1024 * 1024
IMAGE = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}")
VERSION = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?")
SHA = re.compile(r"[0-9a-f]{64}")


def validate(lock):
    if not isinstance(lock, dict) or set(lock) != {"schema_version", "images", "versions", "python_packages"}:
        raise ValueError("build environment lock has unexpected fields")
    if type(lock["schema_version"]) is not int or lock["schema_version"] != 1:
        raise ValueError("unsupported build environment schema")
    images = lock["images"]
    if not isinstance(images, dict) or set(images) != {"amd64", "arm64"}:
        raise ValueError("builder images must cover exactly amd64 and arm64")
    for image in images.values():
        if not isinstance(image, str) or not IMAGE.fullmatch(image) or ".." in image or "//" in image:
            raise ValueError("builder images require a registry and immutable sha256 digest")
    versions = lock["versions"]
    if not isinstance(versions, dict) or set(versions) != {"go", "python", "gcc", "git"}:
        raise ValueError("expected exact Go, Python, GCC, and Git versions")
    for name, version in versions.items():
        pattern = r"go1\.[0-9]+\.[0-9]+" if name == "go" else r"[0-9]+\.[0-9]+\.[0-9]+"
        if not isinstance(version, str) or not re.fullmatch(pattern, version):
            raise ValueError(f"{name}: exact patch version required")
    packages = lock["python_packages"]
    if not isinstance(packages, dict) or set(packages) != {"pip", "git-filter-repo"}:
        raise ValueError("expected pip and git-filter-repo wheels only")
    for name, item in packages.items():
        if not isinstance(item, dict) or set(item) != {"version", "filename", "url", "sha256"}:
            raise ValueError(f"{name}: invalid wheel fields")
        if not isinstance(item["version"], str) or not VERSION.fullmatch(item["version"]):
            raise ValueError(f"{name}: invalid wheel version")
        filename = f"{name.replace('-', '_')}-{item['version']}-py3-none-any.whl"
        if item["filename"] != filename:
            raise ValueError(f"{name}: expected pure Python wheel filename {filename}")
        if (not isinstance(item["url"], str) or not re.fullmatch(
                r"https://files\.pythonhosted\.org/packages/[0-9a-f/]+/" + re.escape(filename), item["url"])):
            raise ValueError(f"{name}: expected fixed HTTPS wheel URL")
        if not isinstance(item["sha256"], str) or not SHA.fullmatch(item["sha256"]):
            raise ValueError(f"{name}: wheel requires sha256")
    return lock


def load(root=ROOT):
    path = root / LOCK
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("symlink in build environment lock path")
    return validate(json.loads(path.read_text(), object_pairs_hook=assembly_sources.unique_object))


def architecture():
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if arch is None:
        raise ValueError(f"unsupported builder architecture: {platform.machine()}")
    return arch


def select_image(lock, requested=None, arch=None):
    image = validate(lock)["images"][arch or architecture()]
    if requested is not None and requested != image:
        raise ValueError(f"unlocked builder image; preload and use {image}")
    return image


def output(*command):
    return subprocess.check_output(command, text=True, timeout=30, env={
        **os.environ, "GOTOOLCHAIN": "local", "GOENV": "off", "GOFLAGS": "", "GOWORK": "off",
        "LC_ALL": "C",
    }).strip()


def preflight(lock):
    validate(lock)
    if platform.system() != "Linux":
        raise ValueError("assembly requires the locked Linux builder")
    selected = select_image(lock)
    if os.environ.get("CSI_AIO_BUILDER_IMAGE") != selected:
        raise ValueError("missing or mismatched builder identity; use isolated_sync.py")
    go = json.loads(output("go", "env", "-json", "GOVERSION", "GOHOSTOS", "GOHOSTARCH"))
    if go["GOHOSTOS"] != "linux" or go["GOHOSTARCH"] != architecture():
        raise ValueError("Go host platform differs from locked builder")
    actual = {"go": go["GOVERSION"], "python": platform.python_version(),
              "gcc": output("/usr/bin/gcc", "-dumpfullversion"),
              "git": output("git", "--version").removeprefix("git version ")}
    for name, version in actual.items():
        if version != lock["versions"][name]:
            raise ValueError(f"{name}: expected {lock['versions'][name]}, got {version}")
    return actual


def check_wheel(data, item):
    if len(data) > MAX_WHEEL_BYTES or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError(f"wheel checksum mismatch: {item['filename']}")


def fetch_wheel(item):
    # No index resolution, alternate hosts, source archives, or unverified code.
    with urllib.request.urlopen(item["url"], timeout=60) as response:
        if response.geturl() != item["url"]:
            raise ValueError("wheel download redirected away from locked URL")
        data = response.read(MAX_WHEEL_BYTES + 1)
    check_wheel(data, item)
    return data


def bootstrap(root, lock):
    preflight(lock)
    destination = root / ENVIRONMENT
    # Exclusive reservation: neither interrupted installs nor caller venvs are reused.
    destination.mkdir()
    wheels = destination / "wheels"
    wheels.mkdir()
    for item in lock["python_packages"].values():
        data = fetch_wheel(item)
        (wheels / item["filename"]).write_bytes(data)
    venv.EnvBuilder(with_pip=False).create(destination)
    requirements = destination / "requirements.txt"
    requirements.write_text("".join(
        f"{name}=={item['version']} --hash=sha256:{item['sha256']}\n"
        for name, item in sorted(lock["python_packages"].items())))
    python = destination / "bin/python3"
    pip = wheels / lock["python_packages"]["pip"]["filename"]
    # Python executes pip directly from the verified wheel; no system pip or get-pip.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PIP_", "PYTHON"))}
    env["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run([
        str(python), "-I", str(pip) + "/pip", "--isolated", "--disable-pip-version-check",
        "install", "--no-index", "--no-deps", "--no-cache-dir", "--only-binary=:all:",
        "--require-hashes", "--find-links", str(wheels), "-r", str(requirements),
    ], check=True, env=env, timeout=120)


def cleanup(root):
    """Remove only the disposable environment, never follow an external symlink."""
    destination = root / ENVIRONMENT
    if destination.is_symlink():
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)


def verify(root, lock):
    actual = preflight(lock)
    destination = root / ENVIRONMENT
    if Path(sys.prefix) != destination or Path(shutil.which("git-filter-repo") or "") != destination / "bin/git-filter-repo":
        raise ValueError("activate the fresh locked .assembly-env before assembly")
    for name, item in lock["python_packages"].items():
        if importlib.metadata.version(name) != item["version"]:
            raise ValueError(f"installed {name} differs from locked version")
        wheel = destination / "wheels" / item["filename"]
        check_wheel(wheel.read_bytes(), item)
    # Check the generator implementation against the verified wheel, not its version label alone.
    dist = importlib.metadata.distribution("git-filter-repo")
    with zipfile.ZipFile(destination / "wheels" / lock["python_packages"]["git-filter-repo"]["filename"]) as archive:
        if Path(dist.locate_file("git_filter_repo.py")).read_bytes() != archive.read("git_filter_repo.py"):
            raise ValueError("installed git-filter-repo implementation checksum mismatch")
    print("Locked build environment: " + json.dumps(actual, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("image", "preflight", "bootstrap", "verify", "cleanup"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--arch", choices=("amd64", "arm64"))
    args = parser.parse_args()
    root = args.root.resolve()
    if args.phase == "cleanup":
        if args.arch is not None:
            parser.error("--arch applies only to image selection")
        cleanup(root)
        return
    lock = load(root)
    if args.phase == "image":
        print(select_image(lock, arch=args.arch))
    else:
        if args.arch is not None:
            parser.error("--arch applies only to image selection")
        if args.phase == "preflight":
            preflight(lock)
        elif args.phase == "bootstrap":
            bootstrap(root, lock)
        else:
            verify(root, lock)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Build environment failed: {error}") from error
