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
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import assembly_sources as sources


def fixture():
    return {"schema_version": 1, "sources": {
        name: {"repository": repository, "ref": "refs/heads/master", "commit": "a" * 40}
        for name, repository in sources.REPOSITORIES.items()}}


class SourceLockTests(unittest.TestCase):
    def test_committed_source_lock(self):
        lock = sources.load(sources.DEFAULT_LOCK)
        self.assertEqual(set(lock["sources"]), set(sources.REPOSITORIES))
        self.assertEqual(sources.encoded(lock), sources.DEFAULT_LOCK.read_bytes())

    def test_rejects_malformed_locks(self):
        invalid = [None, [], {}, {**fixture(), "schema_version": True},
                   {**fixture(), "schema_version": 2}, {**fixture(), "unexpected": 1}]
        for field, values in {
            "commit": [None, 123, "a" * 7, "A" * 40, "0" * 40, "a" * 40 + "\n", "master"],
            "repository": ["https://example.org/attacker", "file:///tmp/source", None],
            "ref": ["master", "refs/tags/v1", "refs/heads/../bad", "refs/heads/x.lock",
                    "refs/heads/.bad", "refs/heads/x/", "refs/heads/x//y", "refs/heads/x y"],
        }.items():
            for value in values:
                lock = fixture()
                lock["sources"]["attacher"][field] = value
                invalid.append(lock)
        for name in sources.REPOSITORIES:
            lock = fixture()
            del lock["sources"][name]
            invalid.append(lock)
        lock = fixture()
        lock["sources"]["attacher"]["extra"] = "not allowed"
        invalid.append(lock)
        for lock in invalid:
            with self.subTest(lock=lock), self.assertRaises(ValueError):
                sources.validate(lock)

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock.json"
            path.write_text('{"schema_version": 1, "schema_version": 1, "sources": {}}')
            with self.assertRaisesRegex(ValueError, "duplicate lock key"):
                sources.load(path)

    def test_preflight_rejects_all_partial_or_stale_outputs(self):
        names = ("tmp", "pkg", "cmd", "bin", "vendor", "go.mod", "go.sum", "go.work",
                 "go.work.sum", "staging/source.go")
        for name in names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("previous assembly")
                with patch.object(sources, "git") as git, self.assertRaises(ValueError):
                    sources.require_fresh(root)
                git.assert_not_called()
                self.assertEqual(target.read_text(), "previous assembly")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "staging/empty").mkdir(parents=True)
            sources.require_fresh(root)
            (root / "staging/dangling").symlink_to("absent")
            with self.assertRaises(ValueError):
                sources.require_fresh(root)

    def test_preflight_accepts_tracked_clean_generated_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            for name in ("pkg", "cmd", "staging", "vendor"):
                (root / name).mkdir()
            (root / "go.mod").write_text("module example.org/assembly\n")
            ls_files = "pkg/a.go\ncmd/b.go\nstaging/c.go\nvendor/d.go\ngo.mod\n"
            with patch.object(sources, "git", side_effect=["", ls_files]) as git:
                sources.require_fresh(root)
            self.assertEqual(git.call_count, 2)
            status = git.call_args_list[0]
            self.assertEqual(status.args[:3], ("status", "--porcelain", "--untracked-files=all"))

    def test_preflight_rejects_dirty_or_untracked_generated_tree(self):
        for status, ls_files, message in (
                (" M pkg/a.go", "pkg/a.go", "uncommitted"),
                ("?? pkg/new.go", "pkg/a.go", "uncommitted"),
                ("", "", "untracked")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".git").mkdir()
                (root / "pkg").mkdir()
                with patch.object(sources, "git", side_effect=[status, ls_files]), \
                        patch.object(sources, "ignored", return_value=False):
                    with self.assertRaisesRegex(ValueError, message):
                        sources.require_fresh(root)

    def test_preflight_accepts_ignored_generated_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            for name in ("pkg", "vendor"):
                (root / name).mkdir()
            (root / "go.mod").write_text("module example.org/assembly\n")
            with patch.object(sources, "git", side_effect=["", ""]) as git, \
                    patch.object(sources, "ignored", return_value=True) as check_ignore:
                sources.require_fresh(root)
            self.assertEqual(git.call_count, 2)
            self.assertEqual({call.args[1] for call in check_ignore.call_args_list},
                             {"pkg", "vendor", "go.mod"})

    def test_checkout_uses_original_sha_and_stable_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source"
            with patch.object(sources, "git", side_effect=["", "", "a" * 40, ""]) as git:
                sources.checkout(fixture(), "attacher", destination)
            calls = [call.args for call in git.call_args_list]
            self.assertEqual(calls[1], ("fetch", "--no-tags", sources.REPOSITORIES["attacher"], "a" * 40))
            self.assertEqual(calls[-1], ("checkout", "-b", "csi-aio-import", "a" * 40))
            with patch.object(sources, "git", side_effect=["", "", "b" * 40]) as git:
                with self.assertRaisesRegex(ValueError, "expected"):
                    sources.checkout(fixture(), "attacher", destination)
                self.assertEqual(git.call_count, 3)
            destination.mkdir()
            with patch.object(sources, "git") as git, self.assertRaisesRegex(ValueError, "reuse"):
                sources.checkout(fixture(), "attacher", destination)
            git.assert_not_called()

    def test_manifest_fingerprint_changes_with_one_source_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock.json"
            lock = fixture()
            path.write_bytes(sources.encoded(lock))
            before = sources.manifest(path)
            lock["sources"]["attacher"]["commit"] = "b" * 40
            path.write_bytes(sources.encoded(lock))
            after = sources.manifest(path)
            self.assertNotEqual(before["source_lock_sha256"], after["source_lock_sha256"])
            for name in sources.REPOSITORIES:
                self.assertEqual(before["sources"][name] == after["sources"][name], name != "attacher")

    def test_bootstrap_rejects_wrong_original_revision_and_modified_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, library = root / "baseline", root / "library"
            retained = baseline / "staging/src/github.com/kubernetes-csi/csi-lib-utils"
            retained.mkdir(parents=True)
            library.mkdir()
            for path in (retained, library):
                (path / "code.go").write_text("pristine library")
            for name in sources.CONTROLLERS:
                mapping = baseline / f"tmp/external-{name}/.git/filter-repo/commit-map"
                mapping.parent.mkdir(parents=True)
                mapping.write_text("a" * 40 + " " + "b" * 40 + "\n")

            def git(*args, cwd=None):
                if args[0] == "status":
                    return ""
                return "a" * 40 if cwd == library else "b" * 40

            with patch.object(sources, "git", side_effect=git):
                sources.verify_baseline(fixture(), baseline, library)
                wrong = fixture()
                wrong["sources"]["snapshotter"]["commit"] = "c" * 40
                with self.assertRaisesRegex(ValueError, "snapshotter: original revision"):
                    sources.verify_baseline(wrong, baseline, library)
                (retained / "code.go").write_text("different bytes")
                with self.assertRaisesRegex(ValueError, "retained source differs"):
                    sources.verify_baseline(fixture(), baseline, library)
            with patch.object(sources, "git", return_value="a" * 40):
                with self.assertRaises(ValueError):
                    sources.verify_baseline(fixture(), baseline, library)

    def test_library_inventory_checks_bytes_modes_and_unexpected_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "code.go").write_text("source")
            before = sources.library_inventory(root)
            (root / ".github").mkdir()
            (root / ".github/ignored").write_text("workflow")
            self.assertEqual(sources.library_inventory(root), before)
            (root / "code.go").chmod(0o755)
            self.assertNotEqual(sources.library_inventory(root), before)
            (root / "code.go").chmod(0o644)
            (root / "extra").symlink_to("code.go")
            self.assertNotEqual(sources.library_inventory(root), before)


class ExactHistoryTests(unittest.TestCase):
    def test_filtered_ref_imports_selected_commit_not_moving_branch(self):
        if not shutil.which("git-filter-repo"):
            self.skipTest("git-filter-repo required for real history rewrite fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "upstream"
            sources.git("init", str(upstream))
            env = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
                       GIT_AUTHOR_EMAIL="fixture@localhost", GIT_COMMITTER_EMAIL="fixture@localhost")

            def run(*args, input=None):
                return subprocess.check_output(["git", *args], cwd=upstream, input=input,
                                               text=True, env=env, timeout=30).strip()

            blob = run("hash-object", "-w", "--stdin", input="selected\n")
            tree = run("mktree", input=f"100644 blob {blob}\tfixture.txt\n")
            selected = run("commit-tree", tree, input="selected revision\n")
            newer = run("commit-tree", tree, "-p", selected, input="newer branch head\n")
            run("update-ref", "refs/heads/moving", newer)
            lock = fixture()
            lock["sources"]["attacher"].update(repository=str(upstream), commit=selected)
            destination = root / "selected"
            with patch.dict(sources.REPOSITORIES, {"attacher": str(upstream)}):
                sources.checkout(lock, "attacher", destination)
                with self.assertRaises(subprocess.CalledProcessError):
                    missing = copy.deepcopy(lock)
                    missing["sources"]["attacher"]["commit"] = "9" * 40
                    sources.checkout(missing, "attacher", root / "missing")
            self.assertEqual(sources.git("rev-parse", "HEAD", cwd=destination), selected)
            sources.git("filter-repo", "--force", "--to-subdirectory-filter", "pkg/attacher", cwd=destination)
            filtered = sources.git("rev-parse", "refs/heads/csi-aio-import", cwd=destination)
            self.assertNotEqual(filtered, selected)
            imported = root / "imported"
            sources.git("init", str(imported))
            sources.git("fetch", str(destination),
                        "refs/heads/csi-aio-import:refs/remotes/external-attacher/csi-aio-import", cwd=imported)
            sources.git("merge", "--ff-only", "refs/remotes/external-attacher/csi-aio-import", cwd=imported)
            self.assertEqual((imported / "pkg/attacher/fixture.txt").read_text(), "selected\n")
            self.assertEqual(sources.git("rev-parse", "HEAD", cwd=imported), filtered)
            self.assertEqual(sources.git("rev-list", "--count", "HEAD", cwd=imported), "1")


if __name__ == "__main__":
    unittest.main()
