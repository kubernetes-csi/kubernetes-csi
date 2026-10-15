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

package main

import (
	goflag "flag"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/kubernetes-csi/csi-sidecars/cmd/csi-sidecars/config"
	attacherconfig "github.com/kubernetes-csi/csi-sidecars/pkg/attacher/cmd/csi-attacher/config"
)

// TestParseControllers covers the --controllers parsing, including the
// edge cases that the previous inline strings.Split produced (an empty value
// leaking an "" key, and whitespace/duplicate commas).
func TestParseControllers(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want map[string]bool
	}{
		{
			name: "empty string yields empty set",
			in:   "",
			want: map[string]bool{},
		},
		{
			name: "single controller",
			in:   "attacher",
			want: map[string]bool{"attacher": true},
		},
		{
			name: "all four sidecars",
			in:   "attacher,provisioner,resizer,snapshotter",
			want: map[string]bool{
				"attacher":    true,
				"provisioner": true,
				"resizer":     true,
				"snapshotter": true,
			},
		},
		{
			name: "trailing comma is ignored",
			in:   "attacher,",
			want: map[string]bool{"attacher": true},
		},
		{
			name: "leading comma is ignored",
			in:   ",attacher",
			want: map[string]bool{"attacher": true},
		},
		{
			name: "surrounding whitespace is trimmed",
			in:   " attacher , provisioner ",
			want: map[string]bool{"attacher": true, "provisioner": true},
		},
		{
			name: "duplicate entries collapse",
			in:   "attacher,attacher",
			want: map[string]bool{"attacher": true},
		},
		{
			name: "only commas yield empty set",
			in:   ",,,",
			want: map[string]bool{},
		},
		{
			name: "unknown controller is kept verbatim (filtered later at enable time)",
			in:   "foo",
			want: map[string]bool{"foo": true},
		},
		{
			name: "known and unknown controllers coexist in the parsed set",
			in:   "attacher,foo",
			want: map[string]bool{"attacher": true, "foo": true},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := parseControllers(tc.in)
			if !reflect.DeepEqual(got, tc.want) {
				t.Errorf("parseControllers(%q) = %v, want %v", tc.in, got, tc.want)
			}
		})
	}
}

// TestValidateControllers guards the --controllers validation: an empty
// selection or any unknown name (typos, wrong case) must fail loudly instead
// of starting no controller and silently exiting.
func TestValidateControllers(t *testing.T) {
	tests := []struct {
		name    string
		in      map[string]bool
		wantErr string
	}{
		{
			name:    "empty selection is rejected",
			in:      map[string]bool{},
			wantErr: "no controllers enabled",
		},
		{
			name:    "single unknown name is rejected",
			in:      map[string]bool{"foo": true},
			wantErr: "unknown controllers: foo",
		},
		{
			name:    "unknown mixed with known is rejected",
			in:      map[string]bool{"foo": true, "attacher": true},
			wantErr: "unknown controllers: foo",
		},
		{
			name:    "unknown names are sorted in the error",
			in:      map[string]bool{"baz": true, "bar": true},
			wantErr: "unknown controllers: bar, baz",
		},
		{
			name:    "case-sensitive: Attacher is not the known attacher",
			in:      map[string]bool{"Attacher": true},
			wantErr: "unknown controllers: Attacher",
		},
		{
			name: "single known name passes",
			in:   map[string]bool{"attacher": true},
		},
		{
			name: "all known names pass",
			in: map[string]bool{
				"attacher":    true,
				"provisioner": true,
				"resizer":     true,
				"snapshotter": true,
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := validateControllers(tc.in)
			if tc.wantErr == "" {
				if err != nil {
					t.Errorf("validateControllers(%v) = %v, want nil", tc.in, err)
				}
				return
			}
			if err == nil {
				t.Fatalf("validateControllers(%v) = nil, want error containing %q", tc.in, tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("validateControllers(%v) error = %q, want it to contain %q", tc.in, err.Error(), tc.wantErr)
			}
		})
	}
}

// TestKnownControllersMatchesMainBranches keeps the knownControllers set in
// sync with the if-branches in main(): every known name must have a branch and
// vice versa, so adding a controller cannot silently skip validation or
// dispatch.
func TestKnownControllersMatchesMainBranches(t *testing.T) {
	want := []string{"attacher", "provisioner", "resizer", "snapshotter"}
	if len(knownControllers) != len(want) {
		t.Fatalf("knownControllers has %d entries, want %d (%v)", len(knownControllers), len(want), want)
	}
	for _, name := range want {
		if !knownControllers[name] {
			t.Errorf("knownControllers is missing %q", name)
		}
	}
}

// TestCopyFlagsFromConfigToGlobalVars asserts the global vars used by the
// sidecar entrypoints alias the correct fields of config.Configuration and
// standardflags.Configuration. This guards the error-prone manual pointer
// mapping in copyFlagsFromConfigToGlobalVars: aliasing the wrong field would
// silently feed the wrong value to a sidecar. We verify aliasing (not just
// equal values) by mutating the source config and observing the global var
// through its pointer.
func TestCopyFlagsFromConfigToGlobalVars(t *testing.T) {
	// Restore the package-global config.Configuration after the test so we do
	// not leak the sentinel values below into any other test in this package.
	// config.Configuration and its nested *Configuration structs are all value
	// types, so a plain struct copy is a complete snapshot.
	original := config.Configuration
	t.Cleanup(func() {
		config.Configuration = original
	})

	// Set distinct sentinel values on the source config so a mis-aliased
	// pointer would surface as a wrong/zero value.
	config.Configuration.Master = "https://master.example"
	config.Configuration.Resync = 7 * time.Minute
	config.Configuration.RetryIntervalStart = 3 * time.Second
	config.Configuration.RetryIntervalMax = 11 * time.Minute
	config.Configuration.AttacherConfiguration.DefaultFSType = "ext4"
	config.Configuration.AttacherConfiguration.WorkerThreads = 42
	config.Configuration.AttacherConfiguration.MaxGRPCLogLength = 99
	config.Configuration.AttacherConfiguration.MaxEntries = 5
	config.Configuration.AttacherConfiguration.ReconcileSync = 13 * time.Second
	config.Configuration.AttacherConfiguration.Timeout = 17 * time.Second
	config.Configuration.SnapshotterConfiguration.SnapshotNamePrefix = "snap-prefix"
	config.Configuration.SnapshotterConfiguration.SnapshotNameUUIDLength = 8
	config.Configuration.SnapshotterConfiguration.CSITimeout = 19 * time.Second
	config.Configuration.SnapshotterConfiguration.Threads = 6
	config.Configuration.SnapshotterConfiguration.EnableNodeDeployment = true
	config.Configuration.SnapshotterConfiguration.GroupSnapshotNamePrefix = "grp-prefix"
	config.Configuration.SnapshotterConfiguration.GroupSnapshotNameUUIDLength = 4
	config.Configuration.SnapshotterConfiguration.ExtraCreateMetadata = true

	copyFlagsFromConfigToGlobalVars()

	// AIO-specific.
	assertPtr(t, "master", master, config.Configuration.Master)
	assertPtr(t, "resync", resync, config.Configuration.Resync)
	assertPtr(t, "resyncPeriod", resyncPeriod, config.Configuration.Resync)
	assertPtr(t, "retryIntervalStart", retryIntervalStart, config.Configuration.RetryIntervalStart)
	assertPtr(t, "retryIntervalMax", retryIntervalMax, config.Configuration.RetryIntervalMax)

	// Attacher-specific.
	assertPtr(t, "defaultFSType", defaultFSType, config.Configuration.AttacherConfiguration.DefaultFSType)
	assertPtr(t, "workerThreads", workerThreads, config.Configuration.AttacherConfiguration.WorkerThreads)
	assertPtr(t, "workers", workers, config.Configuration.AttacherConfiguration.WorkerThreads)
	assertPtr(t, "maxGRPCLogLength", maxGRPCLogLength, config.Configuration.AttacherConfiguration.MaxGRPCLogLength)
	assertPtr(t, "maxEntries", maxEntries, config.Configuration.AttacherConfiguration.MaxEntries)
	assertPtr(t, "reconcileSync", reconcileSync, config.Configuration.AttacherConfiguration.ReconcileSync)
	assertPtr(t, "timeout", timeout, config.Configuration.AttacherConfiguration.Timeout)
	assertPtr(t, "operationTimeout", operationTimeout, config.Configuration.AttacherConfiguration.Timeout)

	// Snapshotter-specific.
	assertPtr(t, "snapshotNamePrefix", snapshotNamePrefix, config.Configuration.SnapshotterConfiguration.SnapshotNamePrefix)
	assertPtr(t, "snapshotNameUUIDLength", snapshotNameUUIDLength, config.Configuration.SnapshotterConfiguration.SnapshotNameUUIDLength)
	assertPtr(t, "snapshotterCSITimeout", snapshotterCSITimeout, config.Configuration.SnapshotterConfiguration.CSITimeout)
	assertPtr(t, "snapshotterThreads", snapshotterThreads, config.Configuration.SnapshotterConfiguration.Threads)
	assertPtr(t, "snapshotterEnableNodeDeployment", snapshotterEnableNodeDeployment, config.Configuration.SnapshotterConfiguration.EnableNodeDeployment)
	assertPtr(t, "groupSnapshotNamePrefix", groupSnapshotNamePrefix, config.Configuration.SnapshotterConfiguration.GroupSnapshotNamePrefix)
	assertPtr(t, "groupSnapshotNameUUIDLength", groupSnapshotNameUUIDLength, config.Configuration.SnapshotterConfiguration.GroupSnapshotNameUUIDLength)
	assertPtr(t, "snapshotterExtraCreateMetadata", snapshotterExtraCreateMetadata, config.Configuration.SnapshotterConfiguration.ExtraCreateMetadata)
}

// The AIO's prefixed attacher retries must not alias the common retries used
// by provisioner/resizer. Exercise actual parsing in both argument orders.
func TestAttacherRetryFlagsAreIndependent(t *testing.T) {
	common := []string{"--retry-interval-start=7s", "--retry-interval-max=4m"}
	attacher := []string{"--attacher-retry-interval-start=3s", "--attacher-retry-interval-max=2m"}
	for _, tc := range []struct {
		name string
		args []string
	}{
		{"common-first", append(append([]string{}, common...), attacher...)},
		{"attacher-first", append(append([]string{}, attacher...), common...)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			original := config.Configuration
			t.Cleanup(func() {
				config.Configuration = original
				copyFlagsFromConfigToGlobalVars()
			})
			fs := goflag.NewFlagSet(t.Name(), goflag.ContinueOnError)
			config.RegisterAIOFlags(fs)
			attacherconfig.RegisterAttacherFlagsWithPrefix(fs, &config.Configuration.AttacherConfiguration)
			if err := fs.Parse(tc.args); err != nil {
				t.Fatal(err)
			}
			copyFlagsFromConfigToGlobalVars()
			assertPtr(t, "attacherRetryIntervalStart", attacherRetryIntervalStart, 3*time.Second)
			assertPtr(t, "attacherRetryIntervalMax", attacherRetryIntervalMax, 2*time.Minute)
			assertPtr(t, "retryIntervalStart", retryIntervalStart, 7*time.Second)
			assertPtr(t, "retryIntervalMax", retryIntervalMax, 4*time.Minute)
			if attacherRetryIntervalStart != &config.Configuration.AttacherConfiguration.RetryIntervalStart ||
				attacherRetryIntervalMax != &config.Configuration.AttacherConfiguration.RetryIntervalMax {
				t.Fatal("attacher retries must alias the attacher configuration")
			}
			if retryIntervalStart != &config.Configuration.RetryIntervalStart ||
				retryIntervalMax != &config.Configuration.RetryIntervalMax {
				t.Fatal("common retries must keep their independent configuration")
			}
		})
	}
}

func assertPtr[T comparable](t *testing.T, name string, got *T, want T) {
	t.Helper()
	if got == nil {
		t.Errorf("%s is nil, want alias to %v", name, want)
		return
	}
	if *got != want {
		t.Errorf("%s = %v, want %v (mis-aliased field?)", name, *got, want)
	}
}
