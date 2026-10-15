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

"""Run local-RBAC deployment with stubbed commands, never a cluster or download."""

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
event = {"tool": tool, "args": args}
if tool == "kubectl" and args[:2] == ["apply", "-f"]:
    event["manifest"] = sys.stdin.read() if args[2] == "-" else Path(args[2]).read_text()
with open(os.environ["EVENTS"], "a") as output:
    output.write(json.dumps(event) + "\n")
if tool != "kubectl":
    sys.exit("unexpected tool: " + tool)
if args[:2] == ["apply", "-f"]:
    if args[2] != "-":
        sys.exit(int(os.environ.get("FAIL_RBAC", "0")))
elif args == ["-n", "kube-system", "get", "deployment/snapshot-controller"]:
    sys.exit(1)
elif args[:2] == ["get", "statefulsets"]:
    print(os.environ.get("STATEFULSETS", "statefulset.apps/plugin\nstatefulset.apps/socat"))
elif args[:2] == ["rollout", "status"]:
    if args[2] == os.environ.get("FAIL_ROLLOUT"):
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
        for name in ("deploy.sh", "test-driver.yaml"):
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

    def run_deploy(self, **overrides):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CSI_", "IMAGE_", "HOSTPATHPLUGIN_", "LIVENESSPROBE_"))
               and key not in ("UPDATE_RBAC_RULES", "KUBELET_DATA_DIR")}
        env.update(PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                   EVENTS=str(self.events), CSI_PROW_TEST_DRIVER=str(self.driver), **overrides)
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
        result, events = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
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
