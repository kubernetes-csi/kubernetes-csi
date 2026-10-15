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

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import verify_artifacts as verify
from image_inputs_test import fixture as image_fixture


class EntrypointTests(unittest.TestCase):
    version = "test-revision"
    bin_dir = Path("/native-binaries")

    def result(self, argv):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, f"{argv[0]} {self.version}\n", "")
        # Both Go's flag package (stderr) and pflag (stdout) are supported.
        output = "\n".join(arg.split("=", 1)[0] + " value" for arg in argv[1:-1])
        return subprocess.CompletedProcess(argv, 0, "", output)

    def test_both_entrypoints_parse_equivalent_flags(self):
        with patch.object(verify, "run", side_effect=self.result) as run:
            verify.verify_entrypoints(self.bin_dir, self.version)
        expected = []
        for command, prefix in (("csi-sidecars", "attacher-"), ("csi-attacher", "")):
            binary = str(self.bin_dir / command)
            args = ["--" + prefix + arg for arg in verify.ATTACHER_ARGS]
            selection = ["--controllers=attacher"] if prefix else []
            expected.append([binary, *selection, *args, "--help"])
            if selection:
                expected.append([binary, *selection, *args, "--version"])
            expected.append([binary, *args, "--version"])
        self.assertEqual([call.args[0] for call in run.call_args_list], expected)

    def test_missing_attacher_flag_is_rejected(self):
        with patch.object(verify, "run", return_value=subprocess.CompletedProcess([], 0, "--controllers string", "")):
            with self.assertRaisesRegex(ValueError, "expected flag --attacher-max-entries"):
                verify.verify_entrypoints(self.bin_dir, self.version)

    def test_wrong_version_is_rejected(self):
        with patch.object(verify, "run", side_effect=self.result):
            with self.assertRaisesRegex(ValueError, "unexpected version output"):
                verify.verify_entrypoints(self.bin_dir, "different-revision")

    def test_help_and_version_failures_are_not_swallowed(self):
        for failing_operation in ("--help", "--version"):
            def result(argv):
                if argv[-1] == failing_operation:
                    raise subprocess.CalledProcessError(1, argv)
                return self.result(argv)

            with self.subTest(operation=failing_operation), patch.object(verify, "run", side_effect=result):
                with self.assertRaises(subprocess.CalledProcessError):
                    verify.verify_entrypoints(self.bin_dir, self.version)

    def test_missing_binary_is_not_swallowed(self):
        with patch.object(verify, "run", side_effect=FileNotFoundError("missing binary")):
            with self.assertRaises(FileNotFoundError):
                verify.verify_entrypoints(self.bin_dir, self.version)


class ImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        image_fixture(self.root)
        verify.image_inputs.dockerfiles(self.root, generate=True)
        (self.root / "bin").mkdir()
        self.command = "snapshot-controller"
        self.contents = b"expected snapshot-controller executable"
        (self.root / "bin" / self.command).write_bytes(self.contents)
        self.entrypoint = ["/snapshot-controller"]
        self.cmd = None
        self.packaged = self.contents
        self.help = "  -retry-crd-interval-max duration\n"
        self.exit_code = "0"
        self.start_error = None
        self.calls = []
        self.mock = patch.object(verify, "run", side_effect=self.engine)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def engine(self, argv):
        self.calls.append(argv)
        operation = argv[1]
        output = ""
        if operation == "image":
            output = json.dumps(self.entrypoint) + "\n" + json.dumps(self.cmd)
        elif operation == "create":
            self.assertIn("--network=none", argv)
            self.assertEqual(argv[-1], "--help")
            output = "smoke-container\n"
        elif operation == "cp":
            Path(argv[-1]).write_bytes(self.packaged)
        elif operation == "start":
            if self.start_error:
                raise self.start_error
            output = self.help
        elif operation == "inspect":
            output = self.exit_code
        elif operation != "rm":
            self.fail(f"Unexpected container operation: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")

    def verify_image(self):
        verify.verify_image(self.root, ["docker"], self.command, "latest")

    def assert_cleaned_up(self):
        self.assertEqual(self.calls[-1], ["docker", "rm", "--force", "smoke-container"])

    def test_correct_image(self):
        self.verify_image()
        self.assert_cleaned_up()

    def test_unlocked_dockerfile_fails_before_container_operations(self):
        (self.root / "cmd/snapshot-controller/Dockerfile").write_text("FROM mutable:latest\n")
        with self.assertRaisesRegex(ValueError, "locked runtime template"):
            self.verify_image()
        self.assertEqual(self.calls, [])

    def test_rejects_aio_entrypoint_under_snapshot_tag(self):
        self.entrypoint = ["/csi-sidecars"]
        with self.assertRaisesRegex(ValueError, "expected entrypoint"):
            self.verify_image()
        self.assertEqual(len(self.calls), 1)

    def test_rejects_default_command_overriding_the_entrypoint(self):
        self.cmd = ["--leaked-default-arg"]
        with self.assertRaisesRegex(ValueError, "unexpected default command"):
            self.verify_image()
        self.assertEqual(len(self.calls), 1)

    def test_rejects_wrong_binary_even_with_correct_entrypoint(self):
        self.packaged = b"an unrelated executable"
        with self.assertRaisesRegex(ValueError, "packaged executable differs"):
            self.verify_image()
        self.assert_cleaned_up()

    def test_rejects_wrong_component_help(self):
        self.help = "  --controllers string\n"
        with self.assertRaisesRegex(ValueError, "expected flag"):
            self.verify_image()
        self.assert_cleaned_up()

    def test_rejects_nonzero_exit_even_if_help_looks_correct(self):
        self.exit_code = "2"
        with self.assertRaisesRegex(ValueError, "exited with status 2"):
            self.verify_image()
        self.assert_cleaned_up()

    def test_removes_container_after_timeout(self):
        self.start_error = subprocess.TimeoutExpired("docker start", 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.verify_image()
        self.assert_cleaned_up()

    def test_removes_container_after_start_failure(self):
        self.start_error = subprocess.CalledProcessError(1, "docker start")
        with self.assertRaises(subprocess.CalledProcessError):
            self.verify_image()
        self.assert_cleaned_up()

    def test_component_flags_accept_go_and_pflag_help(self):
        for command, flag in verify.COMMAND_FLAGS.items():
            for prefix in ("-", "--"):
                with self.subTest(command=command, prefix=prefix):
                    verify.check_help(command, f"  {prefix}{flag} string\n")
            with self.assertRaises(ValueError):
                verify.check_help(command, f"Description mentioning --{flag}")


if __name__ == "__main__":
    unittest.main()
