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

"""Exact upstream source selection; this is not a complete build-input lock."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK = ROOT / "tools/assembly/sources.lock.json"
CONTROLLERS = ("attacher", "provisioner", "resizer", "snapshotter")
REPOSITORIES = {
    **{name: f"https://github.com/kubernetes-csi/external-{name}" for name in CONTROLLERS},
    "csi-lib-utils": "https://github.com/kubernetes-csi/csi-lib-utils",
}
IMPORT_BRANCH = "csi-aio-import"
SHA = re.compile(r"[0-9a-f]{40}")
# Outputs reserved by the assembly; a run never reuses them from a previous one.
GENERATED_OUTPUTS = ("tmp", "pkg", "cmd", "bin", "vendor",
                     "go.mod", "go.sum", "go.work", "go.work.sum")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate lock key: {key}")
        result[key] = value
    return result


def validate(lock):
    if not isinstance(lock, dict) or set(lock) != {"schema_version", "sources"}:
        raise ValueError("source lock must contain only schema_version and sources")
    if type(lock["schema_version"]) is not int or lock["schema_version"] != 1:
        raise ValueError("unsupported source lock schema_version")
    sources = lock["sources"]
    if not isinstance(sources, dict) or set(sources) != set(REPOSITORIES):
        raise ValueError("source lock must retain all four controllers and csi-lib-utils")
    for name, repository in REPOSITORIES.items():
        source = sources[name]
        if not isinstance(source, dict) or set(source) != {"repository", "ref", "commit"}:
            raise ValueError(f"{name}: expected repository, ref, and commit")
        if source["repository"] != repository:
            raise ValueError(f"{name}: unexpected repository identity")
        commit = source["commit"]
        if not isinstance(commit, str) or not SHA.fullmatch(commit) or commit == "0" * 40:
            raise ValueError(f"{name}: commit must be a full nonzero lowercase SHA-1")
        ref = source["ref"]
        if (not isinstance(ref, str) or not re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", ref)
                or any(part in ref for part in ("..", "//", "@{"))
                or any(part.startswith(".") or part.endswith((".", ".lock"))
                       for part in ref.split("/")) or ref.endswith("/")):
            raise ValueError(f"{name}: invalid branch update ref")
    return lock


def load(path):
    return validate(json.loads(Path(path).read_text(), object_pairs_hook=unique_object))


def encoded(lock):
    return (json.dumps(validate(lock), indent=2, sort_keys=True) + "\n").encode()


def git(*args, cwd=None):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True, timeout=300).strip()


def ignored(root, name):
    return subprocess.run(["git", "check-ignore", "-q", name], cwd=root,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def require_fresh(root):
    """Existing generated output must be disposable without losing work.

    A fresh checkout contains none of these paths. A checkout whose
    assembly area is tracked contains them clean, and an ordinary checkout
    contains them git-ignored; both let sync delete and regenerate them safely.
    Anything else — a partial or hand-edited assembly, an interrupted run, an
    untracked experiment — must be removed explicitly with
    tools/scripts/cleanup.sh first.
    """
    root = Path(root)
    present = [name for name in GENERATED_OUTPUTS
               if (path := root / name).exists() or path.is_symlink()]
    # Legacy local library output must still be safe to remove during cleanup.
    staging = root / "staging"
    if staging.is_symlink() or (staging.exists() and (
            not staging.is_dir() or any(p.is_symlink() or p.is_file() for p in staging.rglob("*")))):
        present.append("staging")
    if not present:
        return
    if not (root / ".git").exists():
        raise ValueError(f"existing assembly input/output {present[0]}; "
                         "use a fresh isolated checkout or tools/scripts/cleanup.sh")
    dirty = git("status", "--porcelain", "--untracked-files=all", "--", *present, cwd=root)
    if dirty:
        raise ValueError(f"uncommitted generated output {dirty.splitlines()[0]}; "
                         "commit it or run tools/scripts/cleanup.sh")
    tracked = git("ls-files", "--", *present, cwd=root)
    covered = {line.split("/", 1)[0] for line in tracked.splitlines()}
    foreign = [name for name in present if name not in covered and not ignored(root, name)]
    if foreign:
        raise ValueError(f"existing untracked assembly output {foreign[0]}; "
                         "run tools/scripts/cleanup.sh")


def checkout(lock, name, destination):
    """Fetch exactly one original commit and give filtering a stable local ref."""
    source = validate(lock)["sources"][name]
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"{name}: refusing to reuse checkout {destination}")
    git("init", str(destination))
    git("fetch", "--no-tags", source["repository"], source["commit"], cwd=destination)
    actual = git("rev-parse", "--verify", "FETCH_HEAD^{commit}", cwd=destination)
    if actual != source["commit"]:
        raise ValueError(f"{name}: fetched {actual}, expected {source['commit']}")
    git("checkout", "-b", IMPORT_BRANCH, actual, cwd=destination)
    print(f"{name}: {source['repository']} @ {actual}")


def manifest(path):
    data = Path(path).read_bytes()
    lock = validate(json.loads(data, object_pairs_hook=unique_object))
    return {"schema_version": 1, "source_lock_sha256": hashlib.sha256(data).hexdigest(),
            "sources": lock["sources"]}


def library_inventory(root):
    """Compare retained library bytes/modes; ignore only old sync's removals."""
    ignored = {".git", ".github", "vendor", "release-tools"}
    inventory = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] in ignored:
            continue
        if path.is_symlink():
            inventory[str(relative)] = ("link", os.readlink(path))
        elif path.is_file():
            inventory[str(relative)] = (bool(path.stat().st_mode & 0o111),
                                        hashlib.sha256(path.read_bytes()).hexdigest())
    return inventory


def verify_baseline(lock, baseline, library):
    """Bootstrap provenance from an old assembly that retained filter maps."""
    validate(lock)
    for name in CONTROLLERS:
        repo = baseline / "tmp" / f"external-{name}"
        filtered = git("rev-parse", "HEAD", cwd=repo)
        pairs = {tuple(line.split()) for line in
                 (repo / ".git/filter-repo/commit-map").read_text().splitlines()}
        original = lock["sources"][name]["commit"]
        if (original, filtered) not in pairs:
            raise ValueError(f"{name}: original revision does not match retained filter map")
    if git("rev-parse", "HEAD", cwd=library) != lock["sources"]["csi-lib-utils"]["commit"]:
        raise ValueError("csi-lib-utils: comparison checkout has the wrong revision")
    if git("status", "--porcelain", "--untracked-files=all", cwd=library):
        raise ValueError("csi-lib-utils: comparison checkout is not pristine")
    retained = baseline / "staging/src/github.com/kubernetes-csi/csi-lib-utils"
    expected, actual = library_inventory(library), library_inventory(retained)
    differences = sorted(key for key in expected.keys() | actual.keys()
                         if expected.get(key) != actual.get(key))
    if not expected or differences:
        raise ValueError(f"csi-lib-utils: retained source differs: {differences[:10]}")
    print("All five original source identities match the retained assembly")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--root", type=Path, required=True)
    commands.add_parser("controllers")
    commands.add_parser("manifest")
    clone = commands.add_parser("checkout")
    clone.add_argument("source", choices=tuple(REPOSITORIES))
    clone.add_argument("destination", type=Path)
    locked_commit = commands.add_parser("commit")
    locked_commit.add_argument("source", choices=tuple(REPOSITORIES))
    baseline = commands.add_parser("verify-baseline")
    baseline.add_argument("--baseline", type=Path, required=True)
    baseline.add_argument("--library", type=Path, required=True)
    args = parser.parse_args()
    try:
        lock = load(args.lock)
        if args.command == "preflight":
            require_fresh(args.root)
        elif args.command == "controllers":
            print("\n".join(CONTROLLERS))
        elif args.command == "manifest":
            print(json.dumps(manifest(args.lock), indent=2, sort_keys=True))
        elif args.command == "checkout":
            checkout(lock, args.source, args.destination)
        elif args.command == "commit":
            print(lock["sources"][args.source]["commit"])
        else:
            verify_baseline(lock, args.baseline, args.library)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Source selection failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
