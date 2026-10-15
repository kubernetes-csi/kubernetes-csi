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

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).with_name("retry-go-dependencies.sh")
SUMDB_ERROR = (
    "go: github.com/miekg/dns@v1.1.68: verifying module: reading "
    "https://sum.golang.org/tile/8/0/x162/379: stream error: "
    "stream ID 571; INTERNAL_ERROR; received from peer"
)


class DependencyRetryTests(unittest.TestCase):
    def run_retry(self, failures, message, command=("mod", "tidy"), tee_error=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            go = root / "go"
            go.write_text('''#!/usr/bin/env bash
set -euo pipefail
printf '%s|%s|%s\\n' "$GOSUMDB" "$GOPROXY" "$*" >> "$CALLS"
attempt=$(wc -l < "$CALLS")
if [[ $attempt -le $FAILURES ]]; then
  echo "$FAIL_MESSAGE" >&2
  exit 23
fi
echo "dependency operation completed"
''')
            go.chmod(0o755)
            calls = root / "calls"
            delays = root / "delays"
            script = '''set -euo pipefail
source "$1"
shift
sleep() { echo "$1" >> "$DELAYS"; }
'''
            if tee_error:
                script += 'tee() { command cat > /dev/null; return 42; }\n'
            script += 'retry_go_dependencies "$@"\necho "caller continued"\n'
            env = dict(os.environ, TMPDIR=str(logs), CALLS=str(calls),
                       DELAYS=str(delays), FAILURES=str(failures), FAIL_MESSAGE=message,
                       GOSUMDB="sum.golang.org", GOPROXY="https://proxy.golang.org,direct")
            result = subprocess.run(
                ["bash", "-c", script, "test", str(HELPER), str(go), *command],
                env=env, capture_output=True, text=True, timeout=10,
            )
            invocations = calls.read_text().splitlines()
            backoffs = delays.read_text().splitlines() if delays.exists() else []
            self.assertEqual(list(logs.iterdir()), [], "retry log must be cleaned up")
            for invocation in invocations:
                self.assertEqual(invocation, "sum.golang.org|https://proxy.golang.org,direct|"
                                 + " ".join(command))
            return result, invocations, backoffs

    def test_success_is_not_retried(self):
        result, calls, delays = self.run_retry(0, "")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(delays, [])
        self.assertIn("caller continued", result.stdout)

    def test_ci_sumdb_transport_error_recovers(self):
        result, calls, delays = self.run_retry(1, SUMDB_ERROR)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertEqual(delays, ["5"])
        self.assertIn(SUMDB_ERROR, result.stdout)
        self.assertIn("dependency operation completed", result.stdout)
        self.assertIn("attempt 1/3", result.stderr)

    def test_vendor_recovers_after_two_failures(self):
        result, calls, delays = self.run_retry(2, "503 Service Unavailable", ("mod", "vendor"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, ["5", "10"])

    def test_persistent_transport_failure_preserves_exit_status(self):
        result, calls, delays = self.run_retry(10, SUMDB_ERROR)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, ["5", "10"])
        self.assertNotIn("caller continued", result.stdout)

    def test_deterministic_error_fails_without_retry(self):
        result, calls, delays = self.run_retry(10, "go: errors parsing go.mod: invalid module path")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(len(calls), 1)
        self.assertEqual(delays, [])

    def test_checksum_mismatch_is_not_retried_or_bypassed(self):
        result, calls, delays = self.run_retry(10, SUMDB_ERROR + "\nchecksum mismatch\nSECURITY ERROR")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(len(calls), 1)
        self.assertEqual(delays, [])

    def test_logging_failure_is_not_hidden(self):
        result, calls, delays = self.run_retry(0, "", tee_error=True)
        self.assertEqual(result.returncode, 42)
        self.assertEqual(len(calls), 1)
        self.assertEqual(delays, [])


if __name__ == "__main__":
    unittest.main()
