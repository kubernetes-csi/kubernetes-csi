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
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import build_environment as build


class BuildEnvironmentTests(unittest.TestCase):
    def test_strict_lock_and_immutable_images(self):
        lock = build.load()
        changes = (
            lambda b: b.update(schema_version=True),
            lambda b: b.update(extra="unlocked"),
            lambda b: b["images"].update(amd64="golang:latest"),
            lambda b: b["images"].pop("arm64"),
            lambda b: b["versions"].update(go="go1.26"),
            lambda b: b["versions"].update(python="3.13"),
            lambda b: b["versions"].update(gcc=14),
            lambda b: b["python_packages"].pop("pip"),
            lambda b: b["python_packages"]["pip"].update(sha256="bad"),
            lambda b: b["python_packages"]["pip"].update(filename="../escape.whl"),
            lambda b: b["python_packages"]["pip"].update(url="http://files.pythonhosted.org/untrusted"),
            lambda b: b["python_packages"]["pip"].update(version="latest"),
        )
        for change in changes:
            altered = copy.deepcopy(lock)
            change(altered)
            with self.subTest(lock=altered), self.assertRaises(ValueError):
                build.validate(altered)
        for arch in ("amd64", "arm64"):
            self.assertEqual(build.select_image(lock, arch=arch), lock["images"][arch])
            with self.assertRaisesRegex(ValueError, "unlocked builder"):
                build.select_image(lock, "golang:1.26.6", arch=arch)

    def test_duplicate_keys_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / build.LOCK
            path.parent.mkdir(parents=True)
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(ValueError, "duplicate"):
                build.load(root)
            path.unlink()
            path.symlink_to("missing")
            with self.assertRaisesRegex(ValueError, "symlink"):
                build.load(root)

    def test_actual_tool_version_and_image_mismatches_fail_before_install(self):
        lock = build.load()
        go = json.dumps({"GOVERSION": lock["versions"]["go"], "GOHOSTOS": "linux", "GOHOSTARCH": "arm64"})
        for name in (None, "go", "python", "gcc", "git", "image"):
            expected = copy.deepcopy(lock)
            if name in expected["versions"]:
                expected["versions"][name] = "go1.26.99" if name == "go" else "99.0.0"
            with tempfile.TemporaryDirectory() as directory, patch.object(build.platform, "system", return_value="Linux"), patch.object(
                build, "architecture", return_value="arm64"
            ), patch.object(build.platform, "python_version", return_value=lock["versions"]["python"]), patch.object(
                build, "output", side_effect=[go, lock["versions"]["gcc"], "git version " + lock["versions"]["git"]]
            ), patch.dict(os.environ, {"CSI_AIO_BUILDER_IMAGE": "wrong" if name == "image" else lock["images"]["arm64"]}), patch.object(
                build, "fetch_wheel"
            ) as fetch:
                if name is None:
                    self.assertEqual(build.preflight(expected), lock["versions"])
                else:
                    with self.assertRaises(ValueError):
                        build.bootstrap(Path(directory), expected)
                    self.assertFalse((Path(directory) / build.ENVIRONMENT).exists())
                fetch.assert_not_called()

    def test_wheel_tampering_oversize_and_redirect_fail_closed(self):
        data = b"verified wheel bytes"
        item = {"filename": "fixture.whl", "sha256": hashlib.sha256(data).hexdigest(),
                "url": "https://files.pythonhosted.org/fixture.whl"}
        build.check_wheel(data, item)
        with self.assertRaisesRegex(ValueError, "checksum"):
            build.check_wheel(data + b"corruption", item)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.geturl.return_value = item["url"]
        response.read.return_value = data
        with patch.object(build.urllib.request, "urlopen", return_value=response) as fetch:
            self.assertEqual(build.fetch_wheel(item), data)
            fetch.assert_called_once_with(item["url"], timeout=60)
            response.read.assert_called_once_with(build.MAX_WHEEL_BYTES + 1)
            response.geturl.return_value = "https://elsewhere.example/wheel"
            with self.assertRaisesRegex(ValueError, "redirected"):
                build.fetch_wheel(item)
        with patch.object(build, "MAX_WHEEL_BYTES", 1), self.assertRaisesRegex(ValueError, "checksum"):
            build.check_wheel(data, item)

    def test_failed_download_never_runs_installer_or_reuses_partial_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(build, "preflight"), patch.object(
                build, "fetch_wheel", side_effect=ValueError("wheel checksum mismatch")
            ), patch.object(build.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "checksum"):
                    build.bootstrap(root, build.load())
                run.assert_not_called()
                with self.assertRaises(FileExistsError):
                    build.bootstrap(root, build.load())
                self.assertFalse((root / build.ENVIRONMENT / "bin").exists())

    def test_bootstrap_uses_verified_pip_and_no_index_or_dependencies(self):
        lock = build.load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(build, "preflight"), patch.object(build, "fetch_wheel", return_value=b"fixture"), patch.object(
                build.venv, "EnvBuilder"
            ) as venv, patch.object(build.subprocess, "run") as run, patch.dict(
                os.environ, {"PIP_INDEX_URL": "http://untrusted", "PYTHONPATH": "/untrusted"}
            ):
                build.bootstrap(root, lock)
            venv.assert_called_once_with(with_pip=False)
            command = run.call_args.args[0]
            self.assertTrue(command[2].endswith(lock["python_packages"]["pip"]["filename"] + "/pip"))
            for flag in ("-I", "--isolated", "--no-index", "--no-deps", "--require-hashes", "--only-binary=:all:"):
                self.assertIn(flag, command)
            env = run.call_args.kwargs["env"]
            self.assertNotIn("PIP_INDEX_URL", env)
            self.assertNotIn("PYTHONPATH", env)
            self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
            requirements = (root / build.ENVIRONMENT / "requirements.txt").read_text()
            for name, item in lock["python_packages"].items():
                self.assertIn(f"{name}=={item['version']} --hash=sha256:{item['sha256']}", requirements)

    def test_generator_bytes_checked_not_just_version_metadata(self):
        lock = build.load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = root / build.ENVIRONMENT
            wheels = environment / "wheels"
            wheels.mkdir(parents=True)
            for item in lock["python_packages"].values():
                data = io.BytesIO()
                with zipfile.ZipFile(data, "w") as archive:
                    archive.writestr("git_filter_repo.py", "original")
                item["sha256"] = hashlib.sha256(data.getvalue()).hexdigest()
                (wheels / item["filename"]).write_bytes(data.getvalue())
            installed = environment / "git_filter_repo.py"
            installed.write_text("original")
            dist = Mock()
            dist.locate_file.return_value = installed
            with patch.object(build, "preflight", return_value=lock["versions"]), patch.object(
                build.sys, "prefix", str(environment)
            ), patch.object(build.shutil, "which", return_value=str(environment / "bin/git-filter-repo")), patch.object(
                build.importlib.metadata, "version", side_effect=lambda name: lock["python_packages"][name]["version"]
            ), patch.object(build.importlib.metadata, "distribution", return_value=dist):
                build.verify(root, lock)
                installed.write_text("modified implementation, same version")
                with self.assertRaisesRegex(ValueError, "implementation checksum"):
                    build.verify(root, lock)


if __name__ == "__main__":
    unittest.main()
