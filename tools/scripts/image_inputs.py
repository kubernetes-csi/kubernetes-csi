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

"""Lock runtime Dockerfiles and explicitly selected legacy Kubernetes test images."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import urllib.request

from assembly_sources import unique_object

ROOT = Path(__file__).resolve().parents[2]
LOCK = "tools/assembly/images.lock.json"
COMMANDS = ("csi-sidecars", "snapshot-controller", "snapshot-conversion-webhook")
DIGEST = r"sha256:[0-9a-f]{64}"
VERSION = r"1\.[0-9]+\.[0-9]+"


def safe_path(root, name):
    path = root / name
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"symlink in image input: {name}")
        if parent != path and parent.exists() and not parent.is_dir():
            raise ValueError(f"not a directory in image input: {name}")
    if path.exists() and not path.is_file():
        raise ValueError(f"not a regular image input: {name}")
    return path


def image_entry(value, prefix):
    if (not isinstance(value, dict) or set(value) != {"image", "platforms"} or
            not isinstance(value["image"], str) or
            not re.fullmatch(re.escape(prefix) + "@" + DIGEST, value["image"])):
        raise ValueError(f"expected immutable image from {prefix}")
    platforms = value["platforms"]
    if (not isinstance(platforms, dict) or set(platforms) != {"linux/amd64", "linux/arm64"} or
            any(not isinstance(v, str) or not re.fullmatch(DIGEST, v) for v in platforms.values())):
        raise ValueError("image requires exact Linux amd64 and arm64 manifest digests")


def validate(lock):
    if (not isinstance(lock, dict) or set(lock) != {"schema_version", "runtime", "kubernetes_tests"} or
            type(lock["schema_version"]) is not int or lock["schema_version"] != 1):
        raise ValueError("invalid image lock schema")
    image_entry(lock["runtime"], "gcr.io/distroless/static")
    tests = lock["kubernetes_tests"]
    if not isinstance(tests, dict) or not tests:
        raise ValueError("missing selected Kubernetes test images")
    for version, entry in tests.items():
        if (not isinstance(version, str) or not re.fullmatch(VERSION, version) or
                not isinstance(entry, dict) or set(entry) != {"purpose", "kind_version", "node"} or
                entry["purpose"] != "legacy-regression" or
                not isinstance(entry["kind_version"], str) or
                not re.fullmatch(r"v0\.[0-9]+\.[0-9]+", entry["kind_version"])):
            raise ValueError("invalid legacy Kubernetes test selection; production matrix is not established")
        image_entry(entry["node"], "docker.io/kindest/node:v" + version)
    return lock


def load(root=ROOT):
    return validate(json.loads(safe_path(root, LOCK).read_text(), object_pairs_hook=unique_object))


def dockerfile(command, image):
    description = "CSI Sidecars" if command == "csi-sidecars" else command
    return (f"FROM {image}\n"
            'LABEL maintainers="Kubernetes Authors"\n'
            f'LABEL description="{description}"\n'
            f"ARG binary=./bin/{command}\n"
            f"COPY ${{binary}} /{command}\n"
            f'ENTRYPOINT ["/{command}"]\n')


def dockerfiles(root, generate=False, generated=True):
    """Check the maintained root; create only fresh standalone Dockerfiles."""
    lock = load(root)
    image = lock["runtime"]["image"]
    if safe_path(root, "Dockerfile").read_text() != dockerfile("csi-sidecars", image):
        raise ValueError("root Dockerfile differs from locked runtime template")
    override = safe_path(root, "cmd/csi-sidecars/Dockerfile")
    if override.exists():
        raise ValueError("unexpected csi-sidecars Dockerfile override")
    if generated:
        paths = [(command, safe_path(root, f"cmd/{command}/Dockerfile")) for command in COMMANDS[1:]]
        # Validate every destination before writing anything, including broken links.
        for command, path in paths:
            if generate and path.exists():
                raise ValueError(f"refusing existing generated Dockerfile: {command}")
            if not generate and path.read_text() != dockerfile(command, image):
                raise ValueError(f"generated Dockerfile differs from locked runtime template: {command}")
        if generate:
            for command, path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("x") as output:
                    output.write(dockerfile(command, image))
    return lock


def test_selection(root, version):
    lock = load(root)
    if version not in lock["kubernetes_tests"]:
        raise ValueError(f"unlocked Kubernetes test version: {version}")
    return lock["kubernetes_tests"][version]


def request(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        return response.read()


def checked_json(data, digest):
    if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"registry content checksum mismatch: {digest}")
    return json.loads(data, object_pairs_hook=unique_object)


def verify_registry(entry):
    """Explicit network check; normal assembly never resolves tags or queries registries."""
    reference, digest = entry["image"].split("@")
    registry, repository = reference.split("/", 1)
    repository = repository.split(":", 1)[0]
    headers = {"Accept": ", ".join(("application/vnd.oci.image.index.v1+json",
                                   "application/vnd.oci.image.manifest.v1+json",
                                   "application/vnd.docker.distribution.manifest.list.v2+json",
                                   "application/vnd.docker.distribution.manifest.v2+json"))}
    if registry == "docker.io":
        token = json.loads(request("https://auth.docker.io/token?service=registry.docker.io&scope=repository:"
                                   + repository + ":pull", {}))["token"]
        headers["Authorization"] = "Bearer " + token
        registry = "registry-1.docker.io"
    base = f"https://{registry}/v2/{repository}/"
    index = checked_json(request(base + "manifests/" + digest, headers), digest)
    manifests = index.get("manifests", [])
    for platform, expected in entry["platforms"].items():
        os_name, architecture = platform.split("/")
        matching = [item for item in manifests if item.get("platform", {}).get("os") == os_name
                    and item["platform"].get("architecture") == architecture]
        if len(matching) != 1 or matching[0]["digest"] != expected:
            raise ValueError(f"registry platform mismatch: {entry['image']} {platform}")
        manifest = checked_json(request(base + "manifests/" + expected, headers), expected)
        config_digest = manifest["config"]["digest"]
        if not re.fullmatch(DIGEST, config_digest):
            raise ValueError("invalid registry config digest")
        config = checked_json(request(base + "blobs/" + config_digest, headers), config_digest)
        if config.get("os") != os_name or config.get("architecture") != architecture:
            raise ValueError(f"registry config platform mismatch: {platform}")
    print(f"Registry index, platform manifests, and configs verified: {entry['image']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "generate", "verify", "registry", "test-image", "kind-version"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--kubernetes")
    args = parser.parse_args()
    if (args.phase in ("test-image", "kind-version")) != (args.kubernetes is not None):
        parser.error("test-image and kind-version require --kubernetes; other phases do not accept it")
    root = args.root.resolve()
    if args.phase in ("test-image", "kind-version"):
        selection = test_selection(root, args.kubernetes)
        print(selection["node"]["image"] if args.phase == "test-image" else selection["kind_version"])
    elif args.phase == "registry":
        lock = load(root)
        for entry in [lock["runtime"], *(v["node"] for v in lock["kubernetes_tests"].values())]:
            verify_registry(entry)
    else:
        dockerfiles(root, generate=args.phase == "generate", generated=args.phase != "preflight")
        print(f"Locked image inputs: {args.phase} passed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"Image inputs failed: {error}") from error
