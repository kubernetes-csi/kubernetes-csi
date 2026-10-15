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

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import image_inputs as images


def fixture(root):
    path = root / images.LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((images.ROOT / images.LOCK).read_bytes())
    # .prow.sh also reads the locked snapshotter revision through assembly_sources.py.
    sources = path.parent / "sources.lock.json"
    sources.write_bytes((images.ROOT / "tools/assembly/sources.lock.json").read_bytes())
    lock = images.load(root)
    (root / "Dockerfile").write_text(images.dockerfile("csi-sidecars", lock["runtime"]["image"]))
    return lock


class ImageInputTests(unittest.TestCase):
    def test_build_context_is_a_closed_allowlist(self):
        rules = [line.strip() for line in (images.ROOT / ".dockerignore").read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        # New source/cache/local-config directories must remain excluded by
        # default, while all release Dockerfiles and architecture suffixes work.
        self.assertEqual(rules, ["**", "!Dockerfile", "!bin/**", *(
            f"!cmd/{command}/Dockerfile" for command in images.COMMANDS[1:])])

    def test_strict_schema_and_immutable_platforms(self):
        lock = images.load()
        changes = (
            lambda x: x.update(schema_version=True),
            lambda x: x.update(extra="unlocked"),
            lambda x: x["runtime"].update(image="gcr.io/distroless/static:latest"),
            lambda x: x["runtime"].update(image="gcr.io/distroless/../static@" + "a" * 64),
            lambda x: x["runtime"]["platforms"].pop("linux/arm64"),
            lambda x: x["runtime"]["platforms"].update({"linux/amd64": "bad"}),
            lambda x: x["kubernetes_tests"]["1.31.9"].update(purpose="production"),
            lambda x: x["kubernetes_tests"]["1.31.9"].update(kind_version="latest"),
            lambda x: x["kubernetes_tests"]["1.31.9"]["node"].update(image="kindest/node:v1.31.9"),
            lambda x: x.update(kubernetes_tests={}),
        )
        for change in changes:
            altered = copy.deepcopy(lock)
            change(altered)
            with self.subTest(lock=altered), self.assertRaises(ValueError):
                images.validate(altered)
        with self.assertRaisesRegex(ValueError, "unlocked Kubernetes"):
            images.test_selection(images.ROOT, "1.31.2")

    def test_current_root_and_offline_generation_share_one_template(self):
        images.dockerfiles(images.ROOT, generated=False)
        with tempfile.TemporaryDirectory() as directory, patch.object(images, "request") as request:
            root = Path(directory)
            lock = fixture(root)
            original = (root / "Dockerfile").read_bytes()
            images.dockerfiles(root, generate=True)
            images.dockerfiles(root)
            for command in images.COMMANDS[1:]:
                content = (root / f"cmd/{command}/Dockerfile").read_text()
                self.assertEqual(content, images.dockerfile(command, lock["runtime"]["image"]))
                self.assertIn(f"COPY ${{binary}} /{command}", content)
            self.assertEqual(original, (root / "Dockerfile").read_bytes())
            with self.assertRaisesRegex(ValueError, "existing generated"):
                images.dockerfiles(root, generate=True)
            request.assert_not_called()

    def test_drift_missing_files_and_entrypoint_override_are_rejected(self):
        for name in ("Dockerfile", "cmd/snapshot-controller/Dockerfile", "cmd/snapshot-conversion-webhook/Dockerfile"):
            for change in ("delete", "mutable", "entrypoint"):
                with self.subTest(name=name, change=change), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    fixture(root)
                    images.dockerfiles(root, generate=True)
                    path = root / name
                    if change == "delete":
                        path.unlink()
                    else:
                        path.write_text(path.read_text() + ("FROM mutable:latest\n" if change == "mutable" else 'ENTRYPOINT ["/wrong"]\n'))
                    with self.assertRaises((OSError, ValueError)):
                        images.dockerfiles(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            override = root / "cmd/csi-sidecars/Dockerfile"
            override.parent.mkdir(parents=True)
            override.write_text("FROM unapproved\n")
            with self.assertRaisesRegex(ValueError, "override"):
                images.dockerfiles(root, generated=False)

    def test_duplicate_keys_links_and_all_destinations_checked_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            path = root / images.LOCK
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(ValueError, "duplicate"):
                images.load(root)
            path.unlink()
            path.symlink_to("missing")
            with self.assertRaisesRegex(ValueError, "symlink"):
                images.load(root)
        for existing in ("file", "link", "directory", "parent-link", "parent-file"):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture(root)
                path = root / "cmd/snapshot-conversion-webhook/Dockerfile"
                path.parent.mkdir(parents=True)
                if existing == "file":
                    path.write_text("preserve")
                elif existing == "link":
                    path.symlink_to("missing")
                elif existing == "directory":
                    path.mkdir()
                else:
                    path.parent.rmdir()
                    if existing == "parent-link":
                        path.parent.symlink_to("missing")
                    else:
                        path.parent.write_text("preserve parent file")
                with self.assertRaises(ValueError):
                    images.dockerfiles(root, generate=True)
                self.assertFalse((root / "cmd/snapshot-controller").exists())

    def test_registry_verifies_index_children_and_actual_config_platforms(self):
        for failure in (None, "index", "child", "config", "platform", "missing", "duplicate"):
            with self.subTest(failure=failure):
                responses, descriptors, platforms = {}, [], {}
                base = "https://gcr.io/v2/distroless/static/"

                def add(kind, document):
                    data = json.dumps(document).encode()
                    digest = "sha256:" + hashlib.sha256(data).hexdigest()
                    responses[base + kind + "/" + digest] = data
                    return digest

                for arch in ("amd64", "arm64"):
                    config = add("blobs", {"os": "linux", "architecture": "wrong" if failure == "platform" else arch})
                    child = add("manifests", {"config": {"digest": config}})
                    descriptors.append({"platform": {"os": "linux", "architecture": arch}, "digest": child})
                    platforms["linux/" + arch] = child
                if failure == "missing":
                    descriptors.pop()
                if failure == "duplicate":
                    descriptors.append(descriptors[0])
                index = add("manifests", {"manifests": descriptors})
                for label, kind, digest in (("index", "manifests", index), ("child", "manifests", child), ("config", "blobs", config)):
                    if failure == label:
                        responses[base + kind + "/" + digest] = b"tampered"
                entry = {"image": "gcr.io/distroless/static@" + index, "platforms": platforms}
                with patch.object(images, "request", side_effect=lambda url, _: responses[url]):
                    if failure:
                        with self.assertRaises(ValueError):
                            images.verify_registry(entry)
                    else:
                        images.verify_registry(entry)

    def test_prow_uses_exact_patch_and_kind_before_inherited_defaults(self):
        # Execute the maintained wrapper against a harmless stub, never the real Prow.
        for corrupt in (None, "images", "sources"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lock = fixture(root)
                scripts = root / "tools/scripts"
                scripts.mkdir()
                for name in ("image_inputs.py", "assembly_sources.py"):
                    shutil.copy2(images.ROOT / "tools/scripts" / name, scripts / name)
                shutil.copy2(images.ROOT / ".prow.sh", root / ".prow.sh")
                (root / "go.mod").write_text("module example.org/fixture\ngo 1.26.0\n")
                go = scripts / "go"
                go.write_text('#!/bin/sh\necho "unexpected Go operation in Prow setup" >&2\nexit 86\n')
                go.chmod(0o755)
                (root / "release-tools").mkdir()
                (root / "release-tools/prow.sh").write_text(
                    'printf "stub-sourced %s %s %s %s\\n" "$CSI_PROW_KIND_IMAGES" "$CSI_PROW_KIND_VERSION" "$CSI_SNAPSHOTTER_VERSION" "$GOWORK"\nmain() { :; }\n')
                uname = scripts / "uname"
                uname.write_text("#!/bin/sh\nprintf aarch64\n")
                uname.chmod(0o755)
                if corrupt == "images":
                    (root / images.LOCK).write_text("{}")
                elif corrupt == "sources":
                    (root / "tools/assembly/sources.lock.json").write_text("{}")
                result = subprocess.run(["bash", ".prow.sh"], cwd=root, capture_output=True, text=True, timeout=30,
                                        env={**os.environ, "PATH": str(scripts) + os.pathsep + os.environ["PATH"],
                                             "CSI_PROW_KIND_IMAGES": "mutable", "CSI_PROW_KIND_VERSION": "latest",
                                             "GOWORK": "/unrelated/go.work"})
                if corrupt is None:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    selection = lock["kubernetes_tests"]["1.31.9"]
                    snapshotter = json.loads(
                        (root / "tools/assembly/sources.lock.json").read_text())["sources"]["snapshotter"]["commit"]
                    self.assertEqual(result.stdout.strip(), "stub-sourced " + selection["node"]["image"] + " "
                                     + selection["kind_version"] + " " + snapshotter + " off")
                else:
                    # A rejected lock must stop the wrapper before it sources Prow.
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("stub-sourced", result.stdout)


if __name__ == "__main__":
    unittest.main()
