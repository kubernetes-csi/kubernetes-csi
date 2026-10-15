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

"""Generate go.mod from the original sources, aligning the Kubernetes family
on the explicitly selected release."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import assembly_sources

# These repositories have independent release schedules, not Kubernetes staging
# versions, so they are never folded into the aligned Kubernetes family.
INDEPENDENT = frozenset({
    "k8s.io/gengo", "k8s.io/gengo/v2", "k8s.io/klog", "k8s.io/klog/v2",
    "k8s.io/kube-openapi", "k8s.io/utils", "k8s.io/system-validators",
})
ROOT_MODULE = "github.com/kubernetes-csi/csi-sidecars"
LIB_MODULE = "github.com/kubernetes-csi/csi-lib-utils"
SEMVER = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                    r"(?:-([0-9A-Za-z.-]+))?(?:\+incompatible)?")


def is_family(path):
    return path.startswith("k8s.io/") and path not in INDEPENDENT


def go(root, *args):
    env = {**os.environ, "GOTOOLCHAIN": "local", "GOFLAGS": "", "GOENV": "off",
           "GOSUMDB": "sum.golang.org", "GOPRIVATE": "", "GONOSUMDB": "",
           "GOWORK": "off"}
    return subprocess.check_output(["go", *args], cwd=root, env=env, text=True, timeout=300)


def library_module(source):
    """Resolve the locked revision through Go, without a checkout or root module.

    Go fetches the checksum-verified go.mod into its ordinary module cache.
    Require full origin identity, not merely a matching pseudo-version suffix.
    """
    with tempfile.TemporaryDirectory(prefix="csi-module-query-") as directory:
        root = Path(directory)
        module = json.loads(go(root, "list", "-m", "-json", f"{LIB_MODULE}@{source['commit']}"))
        origin = module.get("Origin") or {}
        if (module.get("Path") != LIB_MODULE or origin.get("VCS") != "git"
                or origin.get("URL") != source["repository"]
                or origin.get("Hash") != source["commit"]):
            raise ValueError("csi-lib-utils: resolved module does not match locked source identity")
        version_key(module.get("Version", ""))
        document = json.loads(go(root, "mod", "edit", "-json", module["GoMod"]))
        if document["Module"]["Path"] != LIB_MODULE:
            raise ValueError("csi-lib-utils: unexpected module path in downloaded go.mod")
        return module["Version"], document


def source_documents(root):
    """Read controller manifests and the utility module's cached go.mod."""
    lock = assembly_sources.load(root / "tmp/sources.lock.json")
    documents = {}
    paths = {name: Path(f"tmp/external-{name}/pkg/{name}/go.mod")
             for name in assembly_sources.CONTROLLERS}
    paths["snapshotter/client"] = paths["snapshotter"].parent / "client/go.mod"
    for name, path in paths.items():
        revision = lock["sources"][name.split("/")[0]]["commit"]
        label = f"{name}@{revision}"
        documents[label] = json.loads(go(root, "mod", "edit", "-json", str(root / path)))
    source = lock["sources"]["csi-lib-utils"]
    version, document = library_module(source)
    documents[f"csi-lib-utils@{source['commit']}"] = document
    return documents, version


def version_key(version):
    match = SEMVER.fullmatch(version)
    if not match:
        raise ValueError(f"unsupported module version: {version}")
    major, minor, patch, prerelease = match.groups()
    identifiers = tuple((0, int(item)) if item.isdigit() else (1, item)
                        for item in prerelease.split(".")) if prerelease else ()
    return (int(major), int(minor), int(patch), prerelease is None, identifiers)


def seed(root, kubernetes):
    """Generate go.mod by merging the original source requirements.

    Aligns every Kubernetes staging module to the explicitly selected release,
    keeps independent modules at their highest source minimum, and pins the
    utility library with a version replacement rather than a local path.
    """
    if not re.fullmatch(r"1\.[0-9]+\.[0-9]+", kubernetes):
        raise ValueError("generation requires an explicit Kubernetes release, e.g. 1.36.3")
    documents, library_version = source_documents(root)
    merged, replacements, family = {}, {}, set()
    folded = {doc["Module"]["Path"] for label, doc in documents.items()
              if not label.startswith("csi-lib-utils@")}
    go_versions = []
    for label, document in documents.items():
        go_versions.append(tuple(map(int, document["Go"].split("."))))
        if document.get("Exclude") or document.get("Tool"):
            raise ValueError(f"{label}: unsupported exclude/tool directive; explicit adaptation required")
        for item in document.get("Require") or []:
            path, version = item["Path"], item["Version"]
            if path in folded:
                continue
            if is_family(path):
                family.add(path)
                version = "v" + (kubernetes if path == "k8s.io/kubernetes" else "0." + kubernetes[2:])
            if path not in merged or version_key(version) > version_key(merged[path]):
                merged[path] = version
        for item in document.get("Replace") or []:
            old, new = item["Old"], item["New"]
            path = old["Path"]
            if path == LIB_MODULE:
                raise ValueError(f"{label}: replacement conflicts with the locked csi-lib-utils module")
            if path in folded and new["Path"] == "./client":
                continue
            if is_family(path):
                if new["Path"] != path or not new.get("Version"):
                    raise ValueError(f"{label}: cannot align replacement for {path}")
                family.add(path)
            else:
                if not new.get("Version"):
                    raise ValueError(f"{label}: unsupported local replacement for {path}")
                key = (path, old.get("Version", ""))
                if key in replacements and replacements[key] != new:
                    raise ValueError(f"conflicting source replacements for {path}")
                replacements[key] = new
    merged[LIB_MODULE] = library_version
    go_version = ".".join(map(str, max(go_versions)))
    lines = [f"module {ROOT_MODULE}", "", f"go {go_version}", "", "require ("]
    lines += [f"\t{path} {version}" for path, version in sorted(merged.items())]
    lines += [")", ""]
    for path in sorted(family):
        version = "v" + (kubernetes if path == "k8s.io/kubernetes" else "0." + kubernetes[2:])
        lines.append(f"replace {path} => {path} {version}")
    for (path, version), new in sorted(replacements.items()):
        lines.append(f"replace {path} {version} => {new['Path']} {new['Version']}")
    lines.append(f"replace {LIB_MODULE} => {LIB_MODULE} {library_version}")
    with (root / "go.mod").open("x") as output:
        output.write("\n".join(lines) + "\n")
    go(root, "mod", "edit", "-fmt")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--kubernetes", help="Kubernetes release to align go.mod generation on")
    parser.add_argument("phase", choices=("seed",))
    args = parser.parse_args()
    try:
        seed(args.root.resolve(), args.kubernetes or "")
        print("Generated go.mod from original source requirements")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Dependency compatibility failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
