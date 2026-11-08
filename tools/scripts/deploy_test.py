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

"""Check hostpath deployment and migration ordering with stubbed commands."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

# Record the exact manifest used by deployment; reject any download or source
# lookup. No YAML/Kustomize parser or generated source tree is required.
STUB = r'''
import json
import os
from pathlib import Path
import sys

tool, args = Path(sys.argv[0]).name, sys.argv[1:]
command = args[2:] if args[:2] == ["-n", "default"] else args
event = {"tool": tool, "args": args}
if tool == "kubectl" and command[:2] == ["apply", "-f"]:
    event["manifest"] = sys.stdin.read() if command[2] == "-" else Path(command[2]).read_text()
elif tool == "kubectl" and command[0] in ("patch", "create"):
    event["manifest"] = sys.stdin.read()
events = Path(os.environ["EVENTS"])
with events.open("a") as output:
    output.write(json.dumps(event) + "\n")
if tool != "kubectl":
    sys.exit("unexpected tool: " + tool)
if command[:2] == ["apply", "-f"]:
    if command[2] != "-":
        sys.exit(int(os.environ.get("FAIL_RBAC", "0")))
elif command[:2] == ["patch", "--local"]:
    patch = Path(command[command.index("--patch-file") + 1])
    if not patch.is_file():
        sys.exit("missing legacy sidecar patch")
    print("# rendered legacy StatefulSet")
elif command == ["create", "--save-config", "-f", "-"]:
    if event["manifest"].strip() != "# rendered legacy StatefulSet":
        sys.exit("expected rendered legacy StatefulSet")
elif command == ["scale", "statefulset/csi-hostpathplugin", "--replicas=0"]:
    pass
elif command == ["wait", "--for=delete", "pod/csi-hostpathplugin-0", "--timeout=2m"]:
    sys.exit(int(os.environ.get("FAIL_LEGACY_STOP", "0")))
elif command[0] == "get" and command[1].startswith("lease/"):
    lease = command[1].removeprefix("lease/")
    aio_applied = any(json.loads(line)["args"] == ["apply", "-f", "-"]
                      for line in events.read_text().splitlines())
    if lease == "csi-sidecars-hostpath-csi-k8s-io":
        if not aio_applied:
            sys.exit("AIO lease checked before AIO deployment")
        print("aio-holder|completed|")
    elif aio_applied:
        transitions = 0 if lease == os.environ.get("UNCHANGED_LEASE") else 1
        print(f"uid-{lease}|{transitions}|")
    else:
        print(f"uid-{lease}|0|csi-hostpathplugin-0")
elif args == ["-n", "kube-system", "get", "deployment/snapshot-controller"]:
    sys.exit(1)
elif args[:2] == ["get", "statefulsets"]:
    print(os.environ.get("STATEFULSETS", "statefulset.apps/plugin\nstatefulset.apps/socat"))
elif command[:2] == ["rollout", "status"]:
    if command[2] == os.environ.get("FAIL_ROLLOUT"):
        sys.exit(1)
elif args[0] == "describe":
    sys.exit(int(os.environ.get("FAIL_DESCRIBE", "0")))
elif args[:2] == ["get", "pods"]:
    print("plugin-0 hostpath")
elif args[0] == "logs":
    print("fixture pod logs")
elif args[:2] == ["get", "pods/csi-hostpathplugin-0"]:
    print("fixture-node")
else:
    sys.exit("unexpected kubectl operation: " + repr(args))
'''


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.deploy = self.root / "deploy/kubernetes-1.31"
        (self.deploy / "hostpath").mkdir(parents=True)
        for name in ("deploy.sh", "test-driver.yaml", "legacy-sidecars.yaml"):
            shutil.copy2(ROOT / "deploy/kubernetes-1.31" / name, self.deploy / name)
        self.rbac = self.deploy.parent / "rbac.yaml"
        shutil.copy2(ROOT / "deploy/rbac.yaml", self.rbac)
        shutil.copy2(ROOT / "deploy/kubernetes-1.31/hostpath/csi-hostpath-plugin.yaml",
                     self.deploy / "hostpath/csi-hostpath-plugin.yaml")
        self.bin = self.root / "stub-bin"
        self.bin.mkdir()
        for name in ("kubectl", "curl", "wget", "git", "python3", "sleep"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + STUB)
            path.chmod(0o755)
        self.events = self.root / "events.jsonl"
        self.driver = self.root / "driver.yaml"

    def run_deploy(self, *, prow=True, **overrides):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CSI_", "IMAGE_", "HOSTPATHPLUGIN_", "LIVENESSPROBE_"))
               and key not in ("UPDATE_RBAC_RULES", "KUBELET_DATA_DIR")}
        env.update(PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                   EVENTS=str(self.events), **overrides)
        if prow:
            env["CSI_PROW_TEST_DRIVER"] = str(self.driver)
        result = subprocess.run(["bash", str(self.deploy / "deploy.sh")], cwd=self.bin,
                                env=env, text=True, capture_output=True, timeout=30)
        events = [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []
        return result, events

    def test_committed_rbac_and_rollouts_need_no_source_tree_or_downloads(self):
        self.assertFalse((self.root / "tools").exists())
        result, events = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(e["tool"] == "kubectl" for e in events))
        self.assertEqual(events[0]["args"][:2], ["apply", "-f"])
        self.assertEqual(Path(events[0]["args"][2]).resolve(), self.rbac)
        self.assertEqual(events[0]["manifest"], self.rbac.read_text())
        rollouts = [e["args"] for e in events if e["args"][:2] == ["rollout", "status"]]
        self.assertEqual(rollouts, [["rollout", "status", f"statefulset.apps/{name}", "--timeout=5m"]
                                    for name in ("plugin", "socat")])
        self.assertIn("ClientNodeName: fixture-node", self.driver.read_text())

    def test_users_and_hostpath_share_one_account_and_manifest(self):
        self.assertFalse((ROOT / "deploy/kubernetes-1.31/rbac").exists())
        self.assertCountEqual(re.findall(r"^kind: (.+)$", self.rbac.read_text(), re.M),
                              ["ServiceAccount", "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding"])
        result, events = self.run_deploy(prow=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(e["args"][:2] == ["patch", "--local"] for e in events))
        self.assertFalse(self.driver.exists())
        manifest = next(e["manifest"] for e in events if e["args"] == ["apply", "-f", "-"])
        self.assertEqual(re.findall(r"^kind: (.+)$", manifest, re.M), ["StatefulSet"])
        self.assertEqual(re.findall(r"^\s*serviceAccountName: (.+)$", manifest, re.M), ["csi-sidecars"])
        self.assertIn("kind: ServiceAccount\nmetadata:\n  name: csi-sidecars\n", self.rbac.read_text())

    def test_image_overrides_do_not_change_rbac(self):
        result, events = self.run_deploy(CSI_EXTERNAL_HEALTH_MONITOR_CONTROLLER_TAG="canary")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(e["tool"] == "kubectl" for e in events))
        self.assertEqual(events[0]["manifest"], self.rbac.read_text())
        manifest = next(e["manifest"] for e in events if e["args"] == ["apply", "-f", "-"])
        self.assertIn("csi-external-health-monitor-controller:canary", manifest)
        self.assertIn("image: csi-sidecars:csiprow", manifest)

    def test_prow_stops_legacy_sidecars_before_applying_aio(self):
        result, events = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [e["args"] for e in events]
        create = commands.index(["create", "--save-config", "-f", "-"])
        legacy_rollout = commands.index(["-n", "default", "rollout", "status",
                                         "statefulset/csi-hostpathplugin", "--timeout=5m"])
        scale = commands.index(["-n", "default", "scale", "statefulset/csi-hostpathplugin", "--replicas=0"])
        stopped = commands.index(["-n", "default", "wait", "--for=delete",
                                   "pod/csi-hostpathplugin-0", "--timeout=2m"])
        apply = commands.index(["apply", "-f", "-"])
        self.assertLess(create, legacy_rollout)
        self.assertLess(legacy_rollout, scale)
        self.assertLess(scale, stopped)
        self.assertLess(stopped, apply)
        legacy = {
            "lease/external-attacher-leader-hostpath-csi-k8s-io",
            "lease/hostpath-csi-k8s-io",
            "lease/external-resizer-hostpath-csi-k8s-io",
            "lease/external-snapshotter-leader-hostpath-csi-k8s-io",
        }
        self.assertEqual({c[3] for c in commands[legacy_rollout + 1:scale]
                          if c[:3] == ["-n", "default", "get"]}, legacy)
        self.assertEqual({c[3] for c in commands[apply + 1:]
                          if c[:3] == ["-n", "default", "get"]},
                         legacy | {"lease/csi-sidecars-hostpath-csi-k8s-io"})

    def test_failed_legacy_shutdown_prevents_aio_start(self):
        result, events = self.run_deploy(FAIL_LEGACY_STOP="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(e["args"] == ["apply", "-f", "-"] for e in events))
        self.assertFalse(self.driver.exists())

    def test_completed_marker_without_lease_transition_fails_migration(self):
        lease = "external-attacher-leader-hostpath-csi-k8s-io"
        result, _ = self.run_deploy(UNCHANGED_LEASE=lease)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"legacy Lease {lease} was not taken over and released", result.stderr)
        self.assertFalse(self.driver.exists())

    def test_manual_local_rbac_changes_are_applied(self):
        self.rbac.write_text(self.rbac.read_text() + "\n# manually reviewed update\n")
        result, events = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events[0]["manifest"], self.rbac.read_text())

    def test_missing_rbac_manifest_stops_deployment(self):
        self.rbac.unlink()
        result, events = self.run_deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(e["args"] == ["apply", "-f", "-"] for e in events))
        self.assertFalse(self.driver.exists())

    def test_rbac_apply_failure_stops_deployment(self):
        result, events = self.run_deploy(FAIL_RBAC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["args"][:2], ["apply", "-f"])
        self.assertEqual(Path(events[0]["args"][2]).resolve(), self.rbac)
        self.assertFalse(self.driver.exists())

    def test_rollout_failure_keeps_diagnostics_and_does_not_write_driver_config(self):
        result, events = self.run_deploy(FAIL_ROLLOUT="statefulset.apps/plugin", FAIL_DESCRIBE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollout failed for statefulset.apps/plugin", result.stderr)
        self.assertTrue(any(e["args"][:2] == ["describe", "-f"] and
                            Path(e["args"][2]).resolve() == self.rbac for e in events))
        self.assertIn("fixture pod logs", result.stdout)
        self.assertFalse(self.driver.exists())

    def test_empty_statefulset_selection_is_not_success(self):
        result, events = self.run_deploy(STATEFULSETS="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no hostpath StatefulSets found", result.stderr)
        self.assertFalse(any(e["args"][:2] == ["rollout", "status"] for e in events))
        self.assertFalse(self.driver.exists())


if __name__ == "__main__":
    unittest.main()
