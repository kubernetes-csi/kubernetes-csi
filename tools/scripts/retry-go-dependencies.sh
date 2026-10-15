#!/usr/bin/env bash

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

# Source this helper; retry only the idempotent Go dependency operations, not
# the whole sync (which rewrites repositories and is not safe to restart).
# Keep the normal Go proxy/checksum verification settings on every attempt.
retry_go_dependencies() (
  set -o pipefail
  local attempt status log
  local -a statuses
  log=$(mktemp)
  trap 'rm -f "$log"' EXIT

  for attempt in 1 2 3; do
    if "$@" 2>&1 | tee "$log"; then
      return 0
    else
      statuses=("${PIPESTATUS[@]}")
      status=${statuses[0]}
    fi
    # A logging failure is not a dependency download failure.
    if [[ ${statuses[1]} -ne 0 ]]; then
      return "${statuses[1]}"
    fi
    # Never retry integrity failures, even if earlier lines mention a
    # transient transport error. In particular, do not disable GOSUMDB.
    if grep -Eiq 'checksum mismatch|SECURITY ERROR' "$log"; then
      return "$status"
    fi
    if [[ $attempt -eq 3 ]] || ! grep -Eiq \
      'stream error:.*INTERNAL_ERROR|connection reset by peer|unexpected EOF|TLS handshake timeout|i/o timeout|temporary failure in name resolution|429 Too Many Requests|502 Bad Gateway|503 Service Unavailable|504 Gateway Timeout' "$log"; then
      return "$status"
    fi
    echo "Transient Go dependency download failure (attempt $attempt/3); retrying in $((attempt * 5))s: $*" >&2
    sleep "$((attempt * 5))"
  done
)
