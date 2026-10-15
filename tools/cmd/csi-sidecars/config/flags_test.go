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
	"strings"
	"testing"
	"time"
)

func TestAIOFlags(t *testing.T) {
	original := Configuration
	t.Cleanup(func() { Configuration = original })
	flags := []struct{ name, defaultValue string }{
		{"master", ""},
		{"resync", "10m0s"},
		{"retry-interval-start", "1s"},
		{"retry-interval-max", "5m0s"},
		{"controllers", ""},
		{"resizer-resize-timeout", "10s"},
		{"resizer-modify-timeout", "10s"},
	}
	// Fresh caller-owned FlagSets must work repeatedly without global leaks.
	for attempt := 0; attempt < 2; attempt++ {
		fs := flag.NewFlagSet("aio", flag.ContinueOnError)
		RegisterAIOFlags(fs)
		for _, tc := range flags {
			f := fs.Lookup(tc.name)
			if f == nil {
				t.Fatalf("flag %q missing from caller FlagSet", tc.name)
			}
			if f.DefValue != tc.defaultValue {
				t.Errorf("flag %q default = %q, want %q", tc.name, f.DefValue, tc.defaultValue)
			}
		}
		for _, name := range []string{"attacher", "provisioner", "resizer", "snapshotter"} {
			if !strings.Contains(fs.Lookup("controllers").Usage, name) {
				t.Errorf("controllers help does not mention %q", name)
			}
		}
	}
}

func TestSnapshotterFlags(t *testing.T) {
	overrides := []string{
		"snapshot-name-prefix=snap", "snapshot-name-uuid-length=8",
		"worker-threads=6", "timeout=19s", "extra-create-metadata=true",
		"node-deployment=true", "groupsnapshot-name-prefix=group",
		"groupsnapshot-name-uuid-length=4",
	}
	defaults := SnapshotterConfiguration{
		SnapshotNamePrefix: "snapshot", SnapshotNameUUIDLength: -1,
		Threads: 10, CSITimeout: time.Minute,
		GroupSnapshotNamePrefix: "groupsnapshot", GroupSnapshotNameUUIDLength: -1,
	}
	want := SnapshotterConfiguration{
		SnapshotNamePrefix: "snap", SnapshotNameUUIDLength: 8,
		Threads: 6, CSITimeout: 19 * time.Second,
		ExtraCreateMetadata: true, EnableNodeDeployment: true,
		GroupSnapshotNamePrefix: "group", GroupSnapshotNameUUIDLength: 4,
	}
	for _, registration := range []struct {
		name     string
		prefix   string
		register func(*flag.FlagSet, *SnapshotterConfiguration)
	}{
		{"unprefixed", "", RegisterSnapshotterFlags},
		{"prefixed", "snapshotter-", RegisterSnapshotterFlagsWithPrefix},
	} {
		t.Run(registration.name, func(t *testing.T) {
			for attempt := 0; attempt < 2; attempt++ {
				fs := flag.NewFlagSet(t.Name(), flag.ContinueOnError)
				var cfg SnapshotterConfiguration
				registration.register(fs, &cfg)
				if cfg != defaults {
					t.Errorf("defaults = %+v, want %+v", cfg, defaults)
				}
				count := 0
				fs.VisitAll(func(*flag.Flag) { count++ })
				if count != len(overrides) {
					t.Fatalf("registered %d flags, want %d", count, len(overrides))
				}
				var args []string
				for _, arg := range overrides {
					args = append(args, "--"+registration.prefix+arg)
				}
				if err := fs.Parse(args); err != nil {
					t.Fatal(err)
				}
				if cfg != want {
					t.Errorf("configuration = %+v, want %+v", cfg, want)
				}
			}
		})
	}
}
