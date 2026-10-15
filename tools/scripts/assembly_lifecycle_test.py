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

"""Single-run cleanup/session scripts with offline bootstrap and tool fixtures."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import build_environment
import isolated_sync

ROOT = Path(__file__).resolve().parents[2]

# Keep the real cleanup implementation. Replace only the
# platform/download/install checks so these fixtures never bootstrap or sync
# the developer's tree, contact the network, or need a container engine.
BUILD_STUB = r'''
import os
from pathlib import Path
import sys
import time
sys.path.insert(0, os.environ["SOURCE_SCRIPTS"])
import build_environment as build
build.ROOT = Path(__file__).resolve().parents[2]
def preflight(lock):
    if os.environ.get("FAIL_PREFLIGHT"):
        raise SystemExit(19)
    return lock["versions"]
def bootstrap(root, lock):
    destination = root / build.ENVIRONMENT
    destination.mkdir()
    (destination / "partial").write_text("owned by this run")
    if os.environ.get("HOLD_BOOTSTRAP"):
        (root / "bootstrap-ready").touch()
        deadline = time.monotonic() + 20
        while not (root / "release-bootstrap").exists():
            if time.monotonic() > deadline:
                raise SystemExit(99)
            time.sleep(0.01)
    if os.environ.get("FAIL_BOOTSTRAP"):
        raise SystemExit(17)
    (destination / "bin").mkdir()
    (destination / "bin/activate").write_text(":\n")
def verify(root, lock):
    if os.environ.get("FAIL_VERIFY"):
        raise SystemExit(23)
cleanup = build.cleanup
def checked_cleanup(root):
    existed = (root / build.ENVIRONMENT).exists()
    cleanup(root)
    if existed and os.environ.get("FAIL_CLEANUP"):
        raise SystemExit(31)
build.preflight = preflight
build.bootstrap = bootstrap
build.verify = verify
build.cleanup = checked_cleanup
build.main()
'''


class AssemblyLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ("sync.sh", "cleanup.sh", "assembly_sources.py",
                     "image_inputs.py", "retry-go-dependencies.sh"):
            target = self.root / "tools/scripts" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "tools/scripts" / name, target)
        shutil.copytree(ROOT / "tools/assembly", self.root / "tools/assembly")
        shutil.copy2(ROOT / "Dockerfile", self.root / "Dockerfile")
        (self.root / "tools/scripts/build_environment.py").write_text(BUILD_STUB)
        # Python 3.13+ fails an empty discovered suite; give the nested tooling
        # session one harmless test rather than weakening its failure handling.
        (self.root / "tools/scripts/fixture_test.py").write_text(
            "import unittest\nclass Fixture(unittest.TestCase):\n"
            "    def test_fixture(self):\n        pass\n")
        (self.root / ".gitignore").write_text("pkg/\ntmp/\n.assembly-env/\n")
        self.env = dict(os.environ)
        self.env.update(SOURCE_SCRIPTS=str(ROOT / "tools/scripts"),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        subprocess.run(["git", "init", "-q", str(self.root)], env=self.env, check=True)
        self.bin = self.root / "stub-bin"
        self.bin.mkdir()
        for name, content in {
            "uname": '#!/bin/sh\nprintf "Linux\\n"\n',
            "trash": '#!/bin/sh\nexec /bin/rm -rf -- "$@"\n',
            "gofmt": '#!/bin/sh\nexit 0\n',
        }.items():
            path = self.bin / name
            path.write_text(content)
            path.chmod(0o755)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
        license_check = self.root / "release-tools/verify-boilerplate.sh"
        license_check.parent.mkdir()
        license_check.write_text('#!/bin/sh\nexit 0\n')
        license_check.chmod(0o755)
        # Generate the exact tooling session command used by isolated_sync,
        # but do not launch a container.
        with patch.object(isolated_sync, "ROOT", self.root), patch.object(
                isolated_sync.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, patch(
                "sys.argv", ["isolated_sync", "--tooling-only"]):
            self.assertEqual(isolated_sync.main(), 0)
            self.tooling = run.call_args.args[0][-1]

    def command(self, operation):
        if operation == "tooling":
            return ["bash", "-c", self.tooling]
        args = ["--update-dependencies", "1.36.3"] if operation == "sync" else []
        return ["bash", str(self.root / "tools/scripts" / (operation + ".sh")), *args]

    def run_operation(self, operation, **env):
        return subprocess.run(self.command(operation), cwd=self.root,
                              env={**self.env, **env}, text=True, capture_output=True, timeout=30)

    def marker(self, directory):
        path = self.root / directory / "keep"
        path.parent.mkdir(exist_ok=True)
        path.write_text("preserve until validation succeeds")
        return path

    def test_invalid_sync_and_cleanup_arguments_preserve_outputs(self):
        output = self.marker("pkg")
        for name in ("sync", "cleanup"):
            result = subprocess.run(["bash", str(self.root / "tools/scripts" / (name + ".sh")), "--invalid"],
                                    cwd=self.bin, env=self.env, text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Usage:", result.stderr)
            self.assertTrue(output.exists())

    def test_preflight_failure_preserves_outputs_and_old_environment(self):
        output, environment = self.marker("pkg"), self.marker(".assembly-env")
        for operation in ("sync", "tooling"):
            result = self.run_operation(operation, FAIL_PREFLIGHT="1")
            self.assertEqual(result.returncode, 19, result.stderr)
            self.assertTrue(output.exists())
            self.assertTrue(environment.exists())

    def test_invalid_image_input_preserves_outputs(self):
        output = self.marker("pkg")
        (self.root / "Dockerfile").write_text("FROM unapproved\n")
        result = self.run_operation("sync")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root Dockerfile differs", result.stderr)
        self.assertTrue(output.exists())

    def test_cleanup_removes_environment_and_can_run_again(self):
        output, environment = self.marker("pkg"), self.marker(".assembly-env")
        staging = self.marker("staging")
        for name in ("go.work", "go.work.sum"):
            (self.root / name).write_text("legacy workspace")
        result = self.run_operation("cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(environment.exists())
        self.assertFalse(staging.exists())
        for name in ("go.work", "go.work.sum"):
            self.assertFalse((self.root / name).exists())
        self.assertEqual(self.run_operation("cleanup").returncode, 0)

    def test_tooling_can_run_twice_without_touching_generated_output(self):
        output = self.marker("pkg")
        self.marker(".assembly-env")  # Simulate a previous interrupted session.
        for _ in range(2):
            result = self.run_operation("tooling")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.root / ".assembly-env").exists())
            self.assertTrue(output.exists())

    def test_partial_bootstrap_and_verification_failure_allow_retry(self):
        for operation in ("sync", "tooling"):
            for failure, status in (("FAIL_BOOTSTRAP", 17), ("FAIL_VERIFY", 23)):
                with self.subTest(operation=operation, failure=failure):
                    result = self.run_operation(operation, **{failure: "1"})
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertFalse((self.root / ".assembly-env").exists())
                    # A succeeding tooling run must be possible after each failure.
                    retry = self.run_operation("tooling")
                    self.assertEqual(retry.returncode, 0, retry.stderr)

    def test_cleanup_failure_does_not_hide_original_failure(self):
        result = self.run_operation("tooling", FAIL_BOOTSTRAP="1", FAIL_CLEANUP="1")
        self.assertEqual(result.returncode, 17, result.stderr)
        result = self.run_operation("tooling", FAIL_CLEANUP="1")
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_terminated_tooling_cleans_environment(self):
        process = subprocess.Popen(self.command("tooling"), cwd=self.root,
                                   env={**self.env, "HOLD_BOOTSTRAP": "1"},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "bootstrap-ready").exists():
                self.assertIsNone(process.poll(), "session exited before bootstrap")
                self.assertLess(time.monotonic(), deadline, "bootstrap did not start")
                time.sleep(0.01)
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=15)
            self.assertNotEqual(process.returncode, 0)
            self.assertFalse((self.root / ".assembly-env").exists())
            result = self.run_operation("cleanup")
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)

    def test_environment_cleanup_does_not_follow_symlinks(self):
        outside = self.root / "unrelated"
        outside.mkdir()
        marker = outside / "keep"
        marker.touch()
        (self.root / ".assembly-env").symlink_to(outside, target_is_directory=True)
        build_environment.cleanup(self.root)
        self.assertTrue(marker.exists())
        self.assertFalse((self.root / ".assembly-env").is_symlink())


if __name__ == "__main__":
    unittest.main()
