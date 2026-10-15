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

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import assembly_dependencies as dependencies

CORE = ("k8s.io/api", "k8s.io/apimachinery", "k8s.io/client-go")


class SeedTests(unittest.TestCase):
    def test_go_commands_ignore_ambient_workspace_and_module_flags(self):
        with patch.dict(os.environ, {"GOWORK": "/unrelated", "GOFLAGS": "-modfile=/unrelated"}), patch.object(
            dependencies.subprocess, "check_output", return_value="{}"
        ) as run:
            dependencies.go(Path("/isolated"), "list", "-m", "-json", "all")
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["GOWORK"], "off")
            self.assertEqual(env["GOFLAGS"], "")
            self.assertEqual(env["GOTOOLCHAIN"], "local")
            self.assertEqual(env["GOSUMDB"], "sum.golang.org")
            self.assertEqual(env["GONOSUMDB"], "")
            self.assertEqual(env["GOENV"], "off")

    def test_library_resolution_rejects_wrong_or_missing_source_identity(self):
        source = dependencies.assembly_sources.load(
            dependencies.assembly_sources.DEFAULT_LOCK)["sources"]["csi-lib-utils"]
        valid = {"Path": dependencies.LIB_MODULE, "Version": "v0.24.0",
                 "Origin": {"VCS": "git", "URL": source["repository"], "Hash": source["commit"]}}
        for field, value in (("Hash", "f" * 40), ("URL", "https://example.org/fork"),
                             ("VCS", "hg"), ("Hash", None)):
            with self.subTest(field=field, value=value):
                module = {**valid, "Origin": {**valid["Origin"], field: value}}
                with patch.object(dependencies, "go", return_value=json.dumps(module)) as run:
                    with self.assertRaisesRegex(ValueError, "source identity"):
                        dependencies.library_module(source)
                    self.assertEqual(run.call_count, 1)
        with patch.object(dependencies, "go", return_value=json.dumps({**valid, "Origin": {}})):
            with self.assertRaisesRegex(ValueError, "source identity"):
                dependencies.library_module(source)

    def test_seed_rejects_source_replacements_that_override_the_library_pin(self):
        documents = {"attacher@fixture": {
            "Module": {"Path": "example.org/attacher"}, "Go": "1.26.0",
            "Replace": [{"Old": {"Path": dependencies.LIB_MODULE, "Version": "v0.25.0"},
                         "New": {"Path": dependencies.LIB_MODULE, "Version": "v0.26.0"}}],
        }}
        with patch.object(dependencies, "source_documents", return_value=(documents, "v0.24.0")):
            with self.assertRaisesRegex(ValueError, "conflicts with the locked"):
                dependencies.seed(Path("/isolated"), "1.36.3")

    def test_seed_requires_an_explicit_kubernetes_release(self):
        for kubernetes in ("", "1.36", "v1.36.3", "1.36.x"):
            with self.subTest(kubernetes=kubernetes), self.assertRaisesRegex(ValueError, "explicit Kubernetes release"):
                dependencies.seed(Path("/isolated"), kubernetes)

    def test_sync_seeds_manifests_then_builds_without_redundant_checks(self):
        script = Path(__file__).with_name("sync.sh").read_text()
        # The removed post-resolution consistency checks must not come back.
        for removed in ("assembly_dependencies.py sources", "assembly_dependencies.py graph",
                        "assembly_dependencies.py vendor", "build_binaries.py", "build_provenance.py"):
            self.assertNotIn(removed, script)
        self.assertNotIn("checkout csi-lib-utils", script)
        self.assertNotIn("staging/", script)
        checkpoints = ["assembly_dependencies.py seed",
                       "retry_go_dependencies go mod tidy", "retry_go_dependencies go mod vendor",
                       "make build"]
        positions = [script.index(checkpoint) for checkpoint in checkpoints]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(script.count("retry_go_dependencies go mod tidy"), 1)
        self.assertEqual(script.count("retry_go_dependencies go mod vendor"), 1)
        self.assertNotIn("go work", script)
        self.assertNotIn('export GOWORK="${REPO_ROOT}/go.work"', script)
        # The committed generated tree is removed (after preflight) before any
        # assembly input is created.
        self.assertLess(script.index("preflight"), script.index("cleanup.sh"))
        self.assertLess(script.index("cleanup.sh"), script.index("mkdir tmp"))


@unittest.skipUnless(shutil.which("go"), "Go required for the real source-manifest fixture")
class SourceDocumentTests(unittest.TestCase):
    def test_seed_generates_aligned_manifests_from_the_six_original_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = root / "tmp/sources.lock.json"
            frozen.parent.mkdir()
            frozen.write_bytes(dependencies.assembly_sources.DEFAULT_LOCK.read_bytes())
            paths = [root / f"tmp/external-{name}/pkg/{name}/go.mod"
                     for name in dependencies.assembly_sources.CONTROLLERS]
            paths += [paths[-1].parent / "client/go.mod"]
            for index, path in enumerate(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("module example.org/source\ngo 1.26.0\n" + "\n".join(
                    f"require {module} v0.36.{index}\n" for module in CORE))
            library = {"Module": {"Path": dependencies.LIB_MODULE}, "Go": "1.26.0"}
            with patch.object(dependencies, "library_module", return_value=("v0.24.0", library)):
                documents, version = dependencies.source_documents(root)
            self.assertEqual(version, "v0.24.0")
            self.assertEqual(len(documents), 6)
            self.assertTrue(any(label.startswith("snapshotter/client@") for label in documents))
            self.assertTrue(any(label.startswith("csi-lib-utils@") for label in documents))

            # seed forces every Kubernetes family module onto the selected release
            # regardless of the (deliberately mismatched) source patch levels.
            with patch.object(dependencies, "library_module", return_value=(version, library)):
                dependencies.seed(root, "1.36.3")
            generated = (root / "go.mod").read_text()
            self.assertIn(f"module {dependencies.ROOT_MODULE}", generated)
            for module in CORE:
                self.assertIn(f"replace {module} => {module} v0.36.3", generated)
            self.assertIn(
                f"replace {dependencies.LIB_MODULE} => {dependencies.LIB_MODULE} {version}", generated)
            self.assertFalse((root / "go.work").exists())
            self.assertFalse((root / "go.work.sum").exists())

            paths[-1].unlink()
            with self.assertRaises(subprocess.CalledProcessError):
                dependencies.source_documents(root)

    def test_module_vendor_pins_library_even_when_transitively_upgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "checkout"
            root.mkdir()
            proxy = parent / "proxy"
            lock = dependencies.assembly_sources.load(dependencies.assembly_sources.DEFAULT_LOCK)
            source_lock = lock["sources"]["csi-lib-utils"]
            version = "v0.24.1-0.20260827084826-" + source_lock["commit"][:12]
            source = 'package utils\nconst Origin = "locked module source"\n'

            def publish(module, selected, files, origin=None):
                target = proxy / module / "@v"
                target.mkdir(parents=True, exist_ok=True)
                info = {"Version": selected, "Time": "2026-08-27T08:48:26Z"}
                if origin:
                    info["Origin"] = origin
                (target / (selected + ".info")).write_text(json.dumps(info))
                (target / (selected + ".mod")).write_text(files["go.mod"])
                with zipfile.ZipFile(target / (selected + ".zip"), "w") as archive:
                    for name, content in files.items():
                        archive.writestr(f"{module}@{selected}/{name}", content)
                return target

            libmod = f"module {dependencies.LIB_MODULE}\ngo 1.26.0\n"
            origin = {"VCS": "git", "URL": source_lock["repository"], "Hash": source_lock["commit"]}
            target = publish(dependencies.LIB_MODULE, version, {"go.mod": libmod, "utils.go": source}, origin)
            shutil.copy2(target / (version + ".info"), target / (source_lock["commit"] + ".info"))
            publish(dependencies.LIB_MODULE, "v0.25.0",
                    {"go.mod": libmod, "utils.go": 'package utils\nconst Origin = "wrong newer source"\n'})
            consumer = "example.org/consumer"
            publish(consumer, "v1.0.0", {
                "go.mod": f"module {consumer}\ngo 1.26.0\nrequire {dependencies.LIB_MODULE} v0.25.0\n",
                "consumer.go": f'package consumer\nimport _ "{dependencies.LIB_MODULE}"\n',
            })
            (root / "main.go").write_text(
                f'package assembly\nimport _ "{consumer}"\nimport utils "{dependencies.LIB_MODULE}"\nvar Origin = utils.Origin\n')
            frozen = root / "tmp/sources.lock.json"
            frozen.parent.mkdir()
            frozen.write_bytes(dependencies.assembly_sources.encoded(lock))
            for name in dependencies.assembly_sources.CONTROLLERS:
                path = root / f"tmp/external-{name}/pkg/{name}/go.mod"
                path.parent.mkdir(parents=True)
                path.write_text(f"module example.org/{name}\ngo 1.26.0\nrequire {consumer} v1.0.0\n")
            client = root / "tmp/external-snapshotter/pkg/snapshotter/client/go.mod"
            client.parent.mkdir()
            client.write_text("module example.org/snapshotclient\ngo 1.26.0\n")
            unrelated = parent / "go.work"
            unrelated.write_text("invalid unrelated workspace\n")

            def fixture_go(cwd, *args):
                # Synthetic file-proxy modules are not published in the public
                # checksum database. Production Go environment is tested above.
                return subprocess.check_output(["go", *args], cwd=cwd, text=True, timeout=60,
                                               env={**os.environ, "GOWORK": "off", "GOENV": "off",
                                                    "GOFLAGS": "", "GOTOOLCHAIN": "local",
                                                    "GOPROXY": proxy.as_uri(), "GOSUMDB": "off",
                                                    "GOPRIVATE": "", "GONOPROXY": "",
                                                    "GOMODCACHE": str(parent / "module-cache")})

            with patch.dict(os.environ, {"GOWORK": str(unrelated)}), patch.object(
                    dependencies, "go", side_effect=fixture_go):
                dependencies.seed(root, "1.36.3")
                fixture_go(root, "mod", "tidy")
                selected = json.loads(fixture_go(root, "list", "-m", "-json", dependencies.LIB_MODULE))
                self.assertEqual(selected["Version"], "v0.25.0")
                self.assertEqual(selected["Replace"]["Path"], dependencies.LIB_MODULE)
                self.assertEqual(selected["Replace"]["Version"], version)
                fixture_go(root, "mod", "vendor")
                location = fixture_go(root, "list", "-mod=vendor", "-f", "{{.Dir}}", dependencies.LIB_MODULE).strip()
                expected = root / "vendor" / dependencies.LIB_MODULE
                self.assertEqual(Path(location), expected)
                self.assertEqual((expected / "utils.go").read_text(), source)
                fixture_go(root, "test", "-mod=vendor", "./...")
                manifest = (root / "vendor/modules.txt").read_bytes()
                fixture_go(root, "mod", "vendor")
                self.assertEqual((root / "vendor/modules.txt").read_bytes(), manifest)
            for name in ("staging", "go.work", "go.work.sum"):
                self.assertFalse((root / name).exists())
            self.assertEqual(unrelated.read_text(), "invalid unrelated workspace\n")

    @unittest.skipUnless(shutil.which("make"), "make required for the environment fixture")
    def test_make_ignores_an_ambient_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = Path(__file__).resolve().parents[2]
            shutil.copy2(source_root / "Makefile", root / "Makefile")
            (root / "release-tools").mkdir()
            (root / "release-tools/build.make").write_text("")
            (root / "probe.mk").write_text('probe:\n\t@printf "%s" "$$GOWORK"\n')
            result = subprocess.run(["make", "-s", "-f", "Makefile", "-f", "probe.mk", "probe"],
                                    cwd=root, env={**os.environ, "GOWORK": "/unrelated/go.work"},
                                    text=True, capture_output=True, check=True, timeout=30)
            self.assertEqual(result.stdout, "off")


if __name__ == "__main__":
    unittest.main()
