/*
Copyright 2026 The Kubernetes Authors.

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
	"context"
	"errors"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/kubernetes-csi/csi-lib-utils/standardflags"
	coordinationv1 "k8s.io/api/coordination/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	clienttesting "k8s.io/client-go/testing"
	clientleaderelection "k8s.io/client-go/tools/leaderelection"
)

func TestLegacyLeaseRefs(t *testing.T) {
	enabled := map[string]bool{"attacher": true, "provisioner": true, "resizer": true, "snapshotter": true}
	want := []leaseRef{
		{namespace: "csi", name: "external-attacher-leader-example-csi-io"},
		{namespace: "csi", name: "example-csi-io"},
		{namespace: "csi", name: "external-resizer-example-csi-io"},
		{namespace: "csi", name: "external-snapshotter-leader-example-csi-io"},
	}
	got, err := legacyLeaseRefs("example.csi.io", "csi", enabled, "")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("default legacy Leases = %#v, want %#v", got, want)
	}

	got, err = legacyLeaseRefs("example.csi.io", "csi", enabled, "attacher=old-namespace/custom-attacher,resizer=another-namespace/custom-resizer")
	if err != nil {
		t.Fatal(err)
	}
	want[0] = leaseRef{namespace: "old-namespace", name: "custom-attacher"}
	want[2] = leaseRef{namespace: "another-namespace", name: "custom-resizer"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("overridden legacy Leases = %#v, want %#v", got, want)
	}

	for _, overrides := range []string{
		"attacher=old-namespace/custom,attacher=old-namespace/other",
		"attacher=missing-name",
		"other=old-namespace/custom",
		"attacher=old-namespace/shared,resizer=old-namespace/shared",
	} {
		if _, err := legacyLeaseRefs("example.csi.io", "csi", enabled, overrides); err == nil {
			t.Errorf("accepted invalid legacy Lease overrides %q", overrides)
		}
	}
}

func migrationTestOptions() standardflags.SidecarConfiguration {
	return standardflags.SidecarConfiguration{
		LeaderElectionLeaseDuration: 2 * time.Second,
		LeaderElectionRenewDeadline: time.Second,
		LeaderElectionRetryPeriod:   20 * time.Millisecond,
	}
}

func migrationTestLease(ref leaseRef, holder, marker string) *coordinationv1.Lease {
	duration := int32(30)
	lease := &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{Namespace: ref.namespace, Name: ref.name},
		Spec: coordinationv1.LeaseSpec{
			HolderIdentity:       &holder,
			LeaseDurationSeconds: &duration,
			RenewTime:            &metav1.MicroTime{Time: time.Now()},
		},
	}
	if marker != "" {
		lease.Annotations = map[string]string{migrationStatusAnnotation: marker}
	}
	return lease
}

func TestConsolidatedLeaseIsNotReleasedOnRenewalFailure(t *testing.T) {
	ref := leaseRef{namespace: "csi", name: "aio"}
	client := fake.NewClientset()
	lock, err := newLeaseLock(client, ref, "leader", nil)
	if err != nil {
		t.Fatal(err)
	}
	var failRenew atomic.Bool
	client.PrependReactor("update", "leases", func(action clienttesting.Action) (bool, runtime.Object, error) {
		lease := action.(clienttesting.UpdateAction).GetObject().(*coordinationv1.Lease)
		if failRenew.Load() && lease.Spec.HolderIdentity != nil && *lease.Spec.HolderIdentity != "" {
			return true, nil, errors.New("renewal unavailable")
		}
		return false, nil, nil
	})
	opts := migrationTestOptions()
	opts.LeaderElectionRenewDeadline = 300 * time.Millisecond
	started := make(chan struct{})
	stopped := make(chan struct{})
	drained := make(chan struct{})
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		clientleaderelection.RunOrDie(context.Background(), consolidatedElectionConfig(lock, opts, nil,
			clientleaderelection.LeaderCallbacks{
				OnStartedLeading: func(ctx context.Context) {
					close(started)
					<-ctx.Done()
					<-drained
				},
				OnStoppedLeading: func() { close(stopped) },
			}))
	}()
	awaitElectionEvent(t, "consolidated leader", started)
	failRenew.Store(true)
	awaitElectionEvent(t, "renewal failure", stopped)
	lease, err := client.CoordinationV1().Leases(ref.namespace).Get(context.Background(), ref.name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != "leader" {
		t.Fatalf("Lease released before controller drain: %#v", lease.Spec.HolderIdentity)
	}
	close(drained)
	awaitElectionEvent(t, "election exit", finished)
}

func TestReleaseConsolidatedLease(t *testing.T) {
	ref := leaseRef{namespace: "csi", name: "aio"}
	for _, holder := range []string{"leader", "other"} {
		t.Run(holder, func(t *testing.T) {
			client := fake.NewClientset(migrationTestLease(ref, holder, "completed"))
			lock, err := newLeaseLock(client, ref, "leader", nil)
			if err != nil {
				t.Fatal(err)
			}
			if err := releaseConsolidatedLease(lock, time.Second); err != nil {
				t.Fatal(err)
			}
			lease, err := client.CoordinationV1().Leases(ref.namespace).Get(context.Background(), ref.name, metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			want := holder
			if holder == "leader" {
				want = ""
			}
			if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != want || lease.Annotations[migrationStatusAnnotation] != "completed" {
				t.Fatalf("unexpected released Lease: %#v", lease)
			}
		})
	}
}

func TestMigrationSkipsLegacyLeasesWithCompletedMarker(t *testing.T) {
	consolidated := leaseRef{namespace: "csi", name: "csi-sidecars-example"}
	legacy := leaseRef{namespace: "csi", name: "external-attacher-leader-example"}
	client := fake.NewClientset(migrationTestLease(consolidated, "aio", "completed"), migrationTestLease(legacy, "legacy", ""))
	if err := migrateLegacyLeases(context.Background(), client, consolidated, []leaseRef{legacy}, "aio", migrationTestOptions()); err != nil {
		t.Fatal(err)
	}
	for _, action := range client.Actions() {
		if action.GetVerb() != "get" || action.GetResource().Resource != "leases" || action.GetNamespace() != consolidated.namespace {
			t.Fatalf("completed marker caused unexpected API action: %s", action)
		}
	}
	lease, err := client.CoordinationV1().Leases(legacy.namespace).Get(context.Background(), legacy.name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != "legacy" {
		t.Fatalf("migration changed legacy holder with completed marker: %#v", lease.Spec.HolderIdentity)
	}
}

func TestMigrationAcquiresAndReleasesLegacyLeasesBeforeReturning(t *testing.T) {
	consolidated := leaseRef{namespace: "csi", name: "csi-sidecars-example"}
	legacy := []leaseRef{{namespace: "csi", name: "attacher"}, {namespace: "other", name: "resizer"}}
	client := fake.NewClientset(migrationTestLease(consolidated, "aio", ""))
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := migrateLegacyLeases(ctx, client, consolidated, legacy, "aio", migrationTestOptions()); err != nil {
		t.Fatal(err)
	}
	assertMigrationResult(t, client, consolidated, legacy, "completed")
}

func TestMigrationCancelsPartialAcquisitionWithActiveLegacyHolder(t *testing.T) {
	consolidated := leaseRef{namespace: "csi", name: "csi-sidecars-example"}
	first := leaseRef{namespace: "csi", name: "attacher"}
	active := leaseRef{namespace: "csi", name: "provisioner"}
	client := fake.NewClientset(migrationTestLease(consolidated, "aio", ""), migrationTestLease(active, "legacy", ""))
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- migrateLegacyLeases(ctx, client, consolidated, []leaseRef{first, active}, "aio", migrationTestOptions())
	}()
	deadline := time.NewTimer(3 * time.Second)
	defer deadline.Stop()
	for {
		lease, err := client.CoordinationV1().Leases(first.namespace).Get(ctx, first.name, metav1.GetOptions{})
		if err == nil && lease.Spec.HolderIdentity != nil && *lease.Spec.HolderIdentity == "aio" {
			break
		}
		select {
		case <-deadline.C:
			t.Fatal("first legacy Lease was not acquired")
		case <-time.After(10 * time.Millisecond):
		}
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("partial acquisition error = %v, want context cancellation", err)
	}
	assertMigrationResult(t, client, consolidated, []leaseRef{first}, "")
	lease, err := client.CoordinationV1().Leases(active.namespace).Get(context.Background(), active.name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != "legacy" {
		t.Fatalf("active legacy holder changed: %#v", lease.Spec.HolderIdentity)
	}
}

func TestMigrationTimesOutBehindActiveLegacyHolder(t *testing.T) {
	consolidated := leaseRef{namespace: "csi", name: "csi-sidecars-example"}
	legacy := leaseRef{namespace: "csi", name: "attacher"}
	active := migrationTestLease(legacy, "legacy", "")
	duration := int32(1)
	active.Spec.LeaseDurationSeconds = &duration
	client := fake.NewClientset(migrationTestLease(consolidated, "aio", ""), active)
	client.PrependReactor("get", "leases", func(action clienttesting.Action) (bool, runtime.Object, error) {
		if action.(clienttesting.GetAction).GetName() == legacy.name {
			lease := active.DeepCopy()
			lease.Spec.RenewTime = &metav1.MicroTime{Time: time.Now()}
			return true, lease, nil
		}
		return false, nil, nil
	})
	opts := standardflags.SidecarConfiguration{
		LeaderElectionLeaseDuration: 500 * time.Millisecond,
		LeaderElectionRenewDeadline: 300 * time.Millisecond,
		LeaderElectionRetryPeriod:   20 * time.Millisecond,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	err := migrateLegacyLeases(ctx, client, consolidated, []leaseRef{legacy}, "aio", opts)
	if err == nil || !strings.Contains(err.Error(), "timed out acquiring legacy Lease") {
		t.Fatalf("active legacy holder error = %v, want acquisition timeout", err)
	}
	lease, err := client.CoordinationV1().Leases(legacy.namespace).Get(context.Background(), legacy.name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != "legacy" {
		t.Fatalf("active legacy holder changed: %#v", lease.Spec.HolderIdentity)
	}
	assertMigrationResult(t, client, consolidated, nil, "")
}

func TestMigrationWaitsForLongerLegacyLease(t *testing.T) {
	consolidated := leaseRef{namespace: "csi", name: "csi-sidecars-example"}
	legacy := leaseRef{namespace: "csi", name: "attacher"}
	oldLease := migrationTestLease(legacy, "old-holder", "")
	duration := int32(3)
	oldLease.Spec.LeaseDurationSeconds = &duration
	oldLease.Spec.RenewTime = &metav1.MicroTime{Time: time.Now().Add(-time.Hour)}
	client := fake.NewClientset(migrationTestLease(consolidated, "aio", ""), oldLease)
	opts := migrationTestOptions()
	opts.LeaderElectionLeaseDuration = time.Second
	opts.LeaderElectionRenewDeadline = 500 * time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	if err := migrateLegacyLeases(ctx, client, consolidated, []leaseRef{legacy}, "aio", opts); err != nil {
		t.Fatalf("migration did not wait for the legacy Lease: %v", err)
	}
	assertMigrationResult(t, client, consolidated, []leaseRef{legacy}, "completed")
}

func TestMigrationFailsWhenLegacyLeaseReadFails(t *testing.T) {
	consolidated := leaseRef{namespace: "csi", name: "csi-sidecars-example"}
	legacy := leaseRef{namespace: "csi", name: "attacher"}
	client := fake.NewClientset(migrationTestLease(consolidated, "aio", ""))
	readErr := errors.New("legacy Lease read failed")
	client.PrependReactor("get", "leases", func(action clienttesting.Action) (bool, runtime.Object, error) {
		if action.(clienttesting.GetAction).GetName() == legacy.name {
			return true, nil, readErr
		}
		return false, nil, nil
	})
	err := migrateLegacyLeases(context.Background(), client, consolidated, []leaseRef{legacy}, "aio", migrationTestOptions())
	if !errors.Is(err, readErr) {
		t.Fatalf("migration error = %v, want %v", err, readErr)
	}
}

func TestMigrationReleasesLegacyLeaseWhenMarkerUpdateFails(t *testing.T) {
	consolidated := leaseRef{namespace: "csi", name: "csi-sidecars-example"}
	legacy := leaseRef{namespace: "csi", name: "attacher"}
	client := fake.NewClientset(migrationTestLease(consolidated, "aio", ""))
	markerErr := errors.New("marker update blocked")
	client.PrependReactor("update", "leases", func(action clienttesting.Action) (bool, runtime.Object, error) {
		lease := action.(clienttesting.UpdateAction).GetObject().(*coordinationv1.Lease)
		if lease.Name == consolidated.name && lease.Annotations[migrationStatusAnnotation] == "completed" {
			return true, nil, markerErr
		}
		return false, nil, nil
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err := migrateLegacyLeases(ctx, client, consolidated, []leaseRef{legacy}, "aio", migrationTestOptions())
	if !errors.Is(err, markerErr) {
		t.Fatalf("marker update error = %v, want %v", err, markerErr)
	}
	assertMigrationResult(t, client, consolidated, []leaseRef{legacy}, "")
}

func TestMigrationFailsIfLegacyLeaseChangesHolderDuringRelease(t *testing.T) {
	consolidated := leaseRef{namespace: "csi", name: "csi-sidecars-example"}
	legacy := leaseRef{namespace: "csi", name: "attacher"}
	client := fake.NewClientset(migrationTestLease(consolidated, "aio", ""))
	var released atomic.Bool
	client.PrependReactor("update", "leases", func(action clienttesting.Action) (bool, runtime.Object, error) {
		lease := action.(clienttesting.UpdateAction).GetObject().(*coordinationv1.Lease)
		if lease.Name == legacy.name && (lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity == "") {
			released.Store(true)
		}
		return false, nil, nil
	})
	client.PrependReactor("get", "leases", func(action clienttesting.Action) (bool, runtime.Object, error) {
		if released.Load() && action.(clienttesting.GetAction).GetName() == legacy.name {
			lease := migrationTestLease(legacy, "old-sidecar", "")
			duration := int32(1)
			lease.Spec.LeaseDurationSeconds = &duration
			return true, lease, nil
		}
		return false, nil, nil
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err := migrateLegacyLeases(ctx, client, consolidated, []leaseRef{legacy}, "aio", migrationTestOptions())
	if err == nil || !strings.Contains(err.Error(), `has holder "old-sidecar" after release`) {
		t.Fatalf("migration returned %v after another holder took the legacy Lease", err)
	}
	lease, err := client.CoordinationV1().Leases(consolidated.namespace).Get(ctx, consolidated.name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[migrationStatusAnnotation] != "completed" || lease.Annotations[migrationReleasePendingAnnotation] != "true" {
		t.Fatalf("release failure did not retain pending state: %v", lease.Annotations)
	}
	shortOpts := migrationTestOptions()
	shortOpts.LeaderElectionLeaseDuration = 500 * time.Millisecond
	shortOpts.LeaderElectionRenewDeadline = 300 * time.Millisecond
	if err := migrateLegacyLeases(ctx, client, consolidated, []leaseRef{legacy}, "aio", shortOpts); err == nil || !strings.Contains(err.Error(), "timed out acquiring legacy Lease") {
		t.Fatalf("retry with an active legacy holder = %v, want acquisition timeout", err)
	}
}

func TestMigrationRetriesWhenReleaseFinalizationFails(t *testing.T) {
	consolidated := leaseRef{namespace: "csi", name: "csi-sidecars-example"}
	legacy := leaseRef{namespace: "csi", name: "attacher"}
	client := fake.NewClientset(migrationTestLease(consolidated, "aio", ""))
	finalizeErr := errors.New("finalization blocked")
	var failed atomic.Bool
	client.PrependReactor("update", "leases", func(action clienttesting.Action) (bool, runtime.Object, error) {
		lease := action.(clienttesting.UpdateAction).GetObject().(*coordinationv1.Lease)
		if lease.Name == consolidated.name && lease.Annotations[migrationStatusAnnotation] == "completed" &&
			lease.Annotations[migrationReleasePendingAnnotation] == "" && !failed.Swap(true) {
			return true, nil, finalizeErr
		}
		return false, nil, nil
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := migrateLegacyLeases(ctx, client, consolidated, []leaseRef{legacy}, "aio", migrationTestOptions()); !errors.Is(err, finalizeErr) {
		t.Fatalf("finalization error = %v, want %v", err, finalizeErr)
	}
	lease, err := client.CoordinationV1().Leases(consolidated.namespace).Get(ctx, consolidated.name, metav1.GetOptions{})
	if err != nil || lease.Annotations[migrationReleasePendingAnnotation] != "true" {
		t.Fatalf("finalization failure did not retain pending state: %v, %v", lease, err)
	}
	if err := migrateLegacyLeases(ctx, client, consolidated, []leaseRef{legacy}, "aio", migrationTestOptions()); err != nil {
		t.Fatalf("retry failed: %v", err)
	}
	assertMigrationResult(t, client, consolidated, []leaseRef{legacy}, "completed")
}

func assertMigrationResult(t *testing.T, client *fake.Clientset, consolidated leaseRef, legacy []leaseRef, marker string) {
	t.Helper()
	lease, err := client.CoordinationV1().Leases(consolidated.namespace).Get(context.Background(), consolidated.name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if got := lease.Annotations[migrationStatusAnnotation]; got != marker {
		t.Errorf("migration marker = %q, want %q", got, marker)
	}
	if got := lease.Annotations[migrationReleasePendingAnnotation]; got != "" {
		t.Errorf("unexpected pending release marker %q", got)
	}
	for _, ref := range legacy {
		lease, err := client.CoordinationV1().Leases(ref.namespace).Get(context.Background(), ref.name, metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		if lease.Spec.HolderIdentity != nil && strings.TrimSpace(*lease.Spec.HolderIdentity) != "" {
			t.Errorf("legacy Lease %s/%s was not released: holder = %q", ref.namespace, ref.name, *lease.Spec.HolderIdentity)
		}
	}
}
