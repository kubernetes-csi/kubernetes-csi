#!/bin/bash

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

set -euxo pipefail

# The generated artifacts live at the repository root; resolve it from the script
# location so the cleanup can be started from any directory.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

if [[ $# -ne 0 ]]; then
  echo "Usage: cleanup.sh" >&2
  exit 1
fi
TRASH="trash"
if ! command -v trash; then
  TRASH="rm -rf"
fi

# removes all the generated files; missing paths are skipped so the script can
# run on any checkout state (fresh clone, partially assembled, fully populated).
# Include staging and workspace files left by older versions of sync.
for path in pkg cmd staging vendor go.mod go.sum go.work go.work.sum tmp bin .assembly-env; do
  if [[ -e "${path}" || -L "${path}" ]]; then
    ${TRASH} "${path}"
  fi
done
