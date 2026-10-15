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

"""Check the formatting gate's maintained and generated source coverage."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("make") and shutil.which("gofmt"),
                     "make and gofmt are required")
class MakefileTests(unittest.TestCase):
    def test_formatting_checks_assembly_but_not_retained_upstream(self):
        good = "package example\n\nvar value = 1\n"
        bad = "package example\n\nvar value=1\n"
        checked = ("tools/example.go", "cmd/example/main.go",
                   "pkg/example/example.go", "staging/example/example.go")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "release-tools").mkdir()
            for name in ("Makefile", "release-tools/build.make"):
                shutil.copy2(ROOT / name, root / name)
            for name in (*checked, "tmp/upstream/example.go", "vendor/example.go"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(good if name in checked else bad)

            def run_gate():
                return subprocess.run(
                    ["make", "test-fmt"], cwd=root, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=30,
                )

            result = run_gate()
            self.assertEqual(result.returncode, 0, result.stdout)
            for name in checked:
                with self.subTest(path=name):
                    (root / name).write_text(bad)
                    result = run_gate()
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(name, result.stdout)
                    (root / name).write_text(good)


if __name__ == "__main__":
    unittest.main()
