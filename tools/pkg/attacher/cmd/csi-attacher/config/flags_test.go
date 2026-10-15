/*
Copyright 2024 The Kubernetes Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package config

import (
	"flag"
	"io"
	"strings"
	"testing"
	"time"
)

// Both entrypoints must expose the same defaults and field mappings; only the
// flag namespace differs. Repeated registration on fresh caller-owned FlagSets
// also catches flags accidentally registered on the global flag.CommandLine.
func TestAttacherEntrypointFlags(t *testing.T) {
	overrides := []string{
		"max-entries=5", "reconcile-sync=13s", "max-grpc-log-length=99",
		"worker-threads=2", "default-fstype=ext4", "timeout=17s",
		"retry-interval-start=3s", "retry-interval-max=2m",
	}
	cases := []struct {
		name string
		args []string
		want AttacherConfiguration
	}{
		{
			name: "defaults",
			want: AttacherConfiguration{
				ReconcileSync: time.Minute, MaxGRPCLogLength: -1, WorkerThreads: 10,
				Timeout: 15 * time.Second, RetryIntervalStart: time.Second,
				RetryIntervalMax: 5 * time.Minute,
			},
		},
		{
			name: "overrides", args: overrides,
			want: AttacherConfiguration{
				MaxEntries: 5, ReconcileSync: 13 * time.Second, MaxGRPCLogLength: 99,
				WorkerThreads: 2, DefaultFSType: "ext4", Timeout: 17 * time.Second,
				RetryIntervalStart: 3 * time.Second, RetryIntervalMax: 2 * time.Minute,
			},
		},
	}
	for _, entrypoint := range []struct {
		name     string
		prefix   string
		register func(*flag.FlagSet, *AttacherConfiguration)
	}{
		{"individual", "", RegisterAttacherFlags},
		{"aio", "attacher-", RegisterAttacherFlagsWithPrefix},
	} {
		for _, tc := range cases {
			t.Run(entrypoint.name+"/"+tc.name, func(t *testing.T) {
				fs := flag.NewFlagSet(t.Name(), flag.ContinueOnError)
				fs.SetOutput(io.Discard)
				var cfg AttacherConfiguration
				entrypoint.register(fs, &cfg)
				count := 0
				fs.VisitAll(func(*flag.Flag) { count++ })
				if count != len(overrides) {
					t.Fatalf("registered %d flags, want %d", count, len(overrides))
				}
				for _, arg := range overrides {
					name, _, _ := strings.Cut(arg, "=")
					if fs.Lookup(entrypoint.prefix+name) == nil {
						t.Fatalf("missing caller-owned flag %q", entrypoint.prefix+name)
					}
				}
				var args []string
				for _, arg := range tc.args {
					args = append(args, "--"+entrypoint.prefix+arg)
				}
				if err := fs.Parse(args); err != nil {
					t.Fatal(err)
				}
				if cfg != tc.want {
					t.Errorf("configuration = %+v, want %+v", cfg, tc.want)
				}
			})
		}
	}
}
