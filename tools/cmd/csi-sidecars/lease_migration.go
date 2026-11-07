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
	"fmt"
	"os"
	"sort"
	"strings"
	"sync/atomic"
	"time"

	"github.com/kubernetes-csi/csi-lib-utils/connection"
	csileaderelection "github.com/kubernetes-csi/csi-lib-utils/leaderelection"
	csirpc "github.com/kubernetes-csi/csi-lib-utils/rpc"
	"github.com/kubernetes-csi/csi-lib-utils/standardflags"
	"github.com/kubernetes-csi/csi-sidecars/cmd/csi-sidecars/config"
	"github.com/kubernetes-csi/csi-sidecars/pkg/attacher/pkg/features"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/uuid"
	utilfeature "k8s.io/apiserver/pkg/util/feature"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	clientleaderelection "k8s.io/client-go/tools/leaderelection"
	"k8s.io/client-go/tools/leaderelection/resourcelock"
	"k8s.io/client-go/util/retry"
)

const migrationStatusAnnotation = "aio.csi.k8s.io/migration-status"
const migrationReleasePendingAnnotation = "aio.csi.k8s.io/migration-release-pending"

type consolidatedLeaseContextKey struct{}

type leaseRef struct {
	namespace string
	name      string
}

type heldLegacyLease struct {
	ref    leaseRef
	cancel context.CancelFunc
	done   <-chan struct{}
	lost   <-chan struct{}
}

// runWithConsolidatedLease starts the selected controllers only after the AIO
// leader holds its own Lease and the legacy Lease migration has completed.
func runWithConsolidatedLease(ctx context.Context, kubeConfig *rest.Config, enabled map[string]bool, requestShutdown func(), run func(context.Context) error) error {
	opts := standardflags.Configuration
	client, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return fmt.Errorf("create leader election client: %w", err)
	}

	lookupCtx, cancelLookup := context.WithTimeout(ctx, 30*time.Second)
	defer cancelLookup()
	go func() {
		select {
		case <-processShutdown:
			cancelLookup()
		case <-lookupCtx.Done():
		}
	}()
	conn, err := connection.ConnectWithoutMetrics(lookupCtx, opts.CSIAddress)
	if err != nil {
		if shutdownRequested() {
			return nil
		}
		return fmt.Errorf("connect to CSI driver for Lease migration: %w", err)
	}
	driverName, err := csirpc.GetDriverName(lookupCtx, conn)
	conn.Close()
	if err != nil {
		if shutdownRequested() {
			return nil
		}
		return fmt.Errorf("get CSI driver name for Lease migration: %w", err)
	}
	if shutdownRequested() {
		return nil
	}

	namespace := opts.LeaderElectionNamespace
	if namespace == "" {
		namespace = defaultLeaderElectionNamespace()
	}
	consolidated := leaseRef{namespace: namespace, name: sanitizeLeaderElectionName("csi-sidecars-" + driverName)}
	legacy, err := legacyLeaseRefs(driverName, namespace, enabled, config.Configuration.LegacyLeaseOverrides)
	if err != nil {
		return err
	}
	for _, ref := range legacy {
		if ref == consolidated {
			return fmt.Errorf("legacy Lease %s/%s equals the consolidated Lease", ref.namespace, ref.name)
		}
	}

	hostname, err := os.Hostname()
	if err != nil {
		return fmt.Errorf("get leader election identity: %w", err)
	}
	// Legacy sidecars use only the hostname. A process-unique suffix prevents an
	// AIO container in the same Pod from mistaking their active Lease for its own.
	identity := sanitizeLeaderElectionName(hostname + "-" + string(uuid.NewUUID()))
	lock, err := newLeaseLock(client, consolidated, identity, opts.LeaderElectionLabels)
	if err != nil {
		return err
	}
	healthCheck := clientleaderelection.NewLeaderHealthzAdaptor(csileaderelection.DefaultHealthCheckTimeout)
	electionCtx, cancelElection := context.WithCancel(ctx)
	defer cancelElection()
	var started atomic.Bool
	finished := make(chan error, 1)
	lost := make(chan struct{}, 1)
	go func() {
		select {
		case <-processShutdown:
			if !started.Load() {
				cancelElection()
			}
		case <-electionCtx.Done():
		}
	}()

	clientleaderelection.RunOrDie(electionCtx, consolidatedElectionConfig(lock, opts, healthCheck,
		clientleaderelection.LeaderCallbacks{
			OnStartedLeading: func(leaderCtx context.Context) {
				if leaderCtx.Err() != nil || shutdownRequested() {
					return
				}
				started.Store(true)
				migrationCtx, cancelMigration := context.WithCancel(leaderCtx)
				stopMigrationWatcher := make(chan struct{})
				go func() {
					select {
					case <-processShutdown:
						cancelMigration()
					case <-stopMigrationWatcher:
					}
				}()
				err := migrateLegacyLeases(migrationCtx, client, consolidated, legacy, identity, opts)
				close(stopMigrationWatcher)
				cancelMigration()
				if err == nil && leaderCtx.Err() == nil && !shutdownRequested() {
					err = run(context.WithValue(leaderCtx, consolidatedLeaseContextKey{}, healthCheck))
				}
				if shutdownRequested() && errors.Is(err, context.Canceled) {
					err = nil
				}
				finished <- err
				cancelElection()
			},
			OnStoppedLeading: func() {
				if electionCtx.Err() == nil {
					requestShutdown()
					lost <- struct{}{}
				}
			},
		}))

	select {
	case <-lost:
		// Exit promptly: another replica may take over once this Lease expires.
		return fmt.Errorf("lost consolidated Lease %s/%s", consolidated.namespace, consolidated.name)
	default:
	}
	if started.Load() {
		err := <-finished
		if utilfeature.DefaultFeatureGate.Enabled(features.ReleaseLeaderElectionOnExit) {
			if releaseErr := releaseConsolidatedLease(lock, opts.LeaderElectionRenewDeadline); releaseErr != nil {
				return errors.Join(err, releaseErr)
			}
		}
		return err
	}
	return nil // follower stopped on SIGTERM or SIGINT
}

func consolidatedElectionConfig(lock resourcelock.Interface, opts standardflags.SidecarConfiguration, healthCheck *clientleaderelection.HealthzAdaptor, callbacks clientleaderelection.LeaderCallbacks) clientleaderelection.LeaderElectionConfig {
	return clientleaderelection.LeaderElectionConfig{
		Lock:          lock,
		LeaseDuration: opts.LeaderElectionLeaseDuration,
		RenewDeadline: opts.LeaderElectionRenewDeadline,
		RetryPeriod:   opts.LeaderElectionRetryPeriod,
		// client-go also releases on renewal failure, before the controller stops.
		ReleaseOnCancel: false,
		WatchDog:        healthCheck,
		Callbacks:       callbacks,
	}
}

func releaseConsolidatedLease(lock resourcelock.Interface, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	record, _, err := lock.Get(ctx)
	if err != nil {
		return fmt.Errorf("read consolidated Lease for release: %w", err)
	}
	if record.HolderIdentity != lock.Identity() {
		return nil
	}
	// Update uses the version read above, so a new holder cannot be overwritten.
	record.HolderIdentity = ""
	record.LeaseDurationSeconds = 1
	record.RenewTime = metav1.Now()
	if err := lock.Update(ctx, *record); err != nil {
		return fmt.Errorf("release consolidated Lease: %w", err)
	}
	return nil
}

func shutdownRequested() bool {
	select {
	case <-processShutdown:
		return true
	default:
		return false
	}
}

func newLeaseLock(client kubernetes.Interface, ref leaseRef, identity string, labels map[string]string) (resourcelock.Interface, error) {
	lock, err := resourcelock.NewWithLabels(resourcelock.LeasesResourceLock, ref.namespace, ref.name,
		client.CoreV1(), client.CoordinationV1(), resourcelock.ResourceLockConfig{Identity: identity}, labels)
	if err != nil {
		return nil, fmt.Errorf("create Lease lock %s/%s: %w", ref.namespace, ref.name, err)
	}
	return lock, nil
}

func legacyLeaseRefs(driverName, namespace string, enabled map[string]bool, overrides string) ([]leaseRef, error) {
	configured := map[string]leaseRef{}
	if overrides != "" {
		for _, entry := range strings.Split(overrides, ",") {
			controller, location, ok := strings.Cut(entry, "=")
			ns, name, hasSlash := strings.Cut(location, "/")
			if !ok || !hasSlash || ns == "" || name == "" || strings.Contains(name, "/") || !enabled[controller] {
				return nil, fmt.Errorf("invalid legacy Lease override %q: expected enabled-controller=namespace/name", entry)
			}
			if _, exists := configured[controller]; exists {
				return nil, fmt.Errorf("duplicate legacy Lease override for %s", controller)
			}
			configured[controller] = leaseRef{namespace: ns, name: name}
		}
	}

	defaults := map[string]string{
		"attacher":    "external-attacher-leader-" + driverName,
		"provisioner": strings.ReplaceAll(driverName, "/", "-"),
		"resizer":     "external-resizer-" + sanitizeLeaderElectionName(driverName),
		"snapshotter": "external-snapshotter-leader-" + strings.ReplaceAll(driverName, "/", "-"),
	}
	var names []string
	for name := range enabled {
		names = append(names, name)
	}
	sort.Strings(names)
	refs := make([]leaseRef, 0, len(names))
	seen := map[leaseRef]bool{}
	for _, controller := range names {
		ref, ok := configured[controller]
		if !ok {
			ref = leaseRef{namespace: namespace, name: sanitizeLeaderElectionName(defaults[controller])}
		}
		if seen[ref] {
			return nil, fmt.Errorf("multiple controllers use legacy Lease %s/%s", ref.namespace, ref.name)
		}
		seen[ref] = true
		refs = append(refs, ref)
	}
	return refs, nil
}

func migrateLegacyLeases(ctx context.Context, client kubernetes.Interface, consolidated leaseRef, legacy []leaseRef, identity string, opts standardflags.SidecarConfiguration) (err error) {
	lease, err := client.CoordinationV1().Leases(consolidated.namespace).Get(ctx, consolidated.name, metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("read consolidated Lease: %w", err)
	}
	if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != identity {
		return fmt.Errorf("consolidated Lease %s/%s is no longer held by this process", consolidated.namespace, consolidated.name)
	}
	// A completed marker alone is valid for older migrations. A pending release
	// means a previous leader could not confirm that the legacy locks were free.
	if lease.Annotations[migrationStatusAnnotation] == "completed" && lease.Annotations[migrationReleasePendingAnnotation] != "true" {
		return nil
	}

	acquireCtx, cancelAcquisition := context.WithCancelCause(ctx)
	defer cancelAcquisition(nil)
	var held []*heldLegacyLease
	defer func() {
		releaseErr := releaseLegacyLeases(client, held, identity, opts.LeaderElectionRenewDeadline)
		err = errors.Join(err, context.Cause(acquireCtx), releaseErr)
	}()
	for _, ref := range legacy {
		lock, lockErr := acquireLegacyLease(acquireCtx, client, ref, identity, opts)
		if lockErr != nil {
			return lockErr
		}
		held = append(held, lock)
		go func(lock *heldLegacyLease) {
			select {
			case <-lock.lost:
				cancelAcquisition(fmt.Errorf("lost legacy Lease %s/%s", lock.ref.namespace, lock.ref.name))
			case <-acquireCtx.Done():
			}
		}(lock)
	}
	if err := context.Cause(acquireCtx); err != nil {
		return err
	}
	if err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		if err := acquireCtx.Err(); err != nil {
			return context.Cause(acquireCtx)
		}
		lease, err := client.CoordinationV1().Leases(consolidated.namespace).Get(acquireCtx, consolidated.name, metav1.GetOptions{})
		if err != nil {
			return err
		}
		if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != identity {
			return fmt.Errorf("consolidated Lease %s/%s was lost before migration completed", consolidated.namespace, consolidated.name)
		}
		if lease.Annotations == nil {
			lease.Annotations = make(map[string]string)
		}
		lease.Annotations[migrationStatusAnnotation] = "completed"
		lease.Annotations[migrationReleasePendingAnnotation] = "true"
		_, err = client.CoordinationV1().Leases(consolidated.namespace).Update(acquireCtx, lease, metav1.UpdateOptions{})
		return err
	}); err != nil {
		return fmt.Errorf("mark consolidated Lease migration complete: %w", err)
	}
	if err := releaseLegacyLeases(client, held, identity, opts.LeaderElectionRenewDeadline); err != nil {
		held = nil
		return err
	}
	held = nil
	if err := context.Cause(acquireCtx); err != nil {
		return err
	}
	if err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		lease, err := client.CoordinationV1().Leases(consolidated.namespace).Get(acquireCtx, consolidated.name, metav1.GetOptions{})
		if err != nil {
			return err
		}
		if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != identity {
			return fmt.Errorf("consolidated Lease %s/%s was lost before migration completed", consolidated.namespace, consolidated.name)
		}
		delete(lease.Annotations, migrationReleasePendingAnnotation)
		_, err = client.CoordinationV1().Leases(consolidated.namespace).Update(acquireCtx, lease, metav1.UpdateOptions{})
		return err
	}); err != nil {
		return fmt.Errorf("finalize consolidated Lease migration: %w", err)
	}
	return context.Cause(acquireCtx)
}

func acquireLegacyLease(ctx context.Context, client kubernetes.Interface, ref leaseRef, identity string, opts standardflags.SidecarConfiguration) (*heldLegacyLease, error) {
	waitDuration := 2 * opts.LeaderElectionLeaseDuration
	lease, err := client.CoordinationV1().Leases(ref.namespace).Get(ctx, ref.name, metav1.GetOptions{})
	if err != nil && !apierrors.IsNotFound(err) {
		return nil, fmt.Errorf("read legacy Lease %s/%s: %w", ref.namespace, ref.name, err)
	}
	if err == nil && lease.Spec.LeaseDurationSeconds != nil {
		// A new elector must observe the old record for its entire lease duration,
		// even if its last renewal was long ago. Allow for the retry interval too.
		oldLeaseWait := time.Duration(*lease.Spec.LeaseDurationSeconds)*time.Second + opts.LeaderElectionRenewDeadline + 2*opts.LeaderElectionRetryPeriod
		if oldLeaseWait > waitDuration {
			waitDuration = oldLeaseWait
		}
	}
	lock, err := newLeaseLock(client, ref, identity, nil)
	if err != nil {
		return nil, err
	}
	runCtx, cancel := context.WithCancel(ctx)
	acquired := make(chan struct{})
	done := make(chan struct{})
	lost := make(chan struct{})
	var started atomic.Bool
	go func() {
		defer close(done)
		clientleaderelection.RunOrDie(runCtx, clientleaderelection.LeaderElectionConfig{
			Lock:            lock,
			LeaseDuration:   opts.LeaderElectionLeaseDuration,
			RenewDeadline:   opts.LeaderElectionRenewDeadline,
			RetryPeriod:     opts.LeaderElectionRetryPeriod,
			ReleaseOnCancel: true,
			Callbacks: clientleaderelection.LeaderCallbacks{
				OnStartedLeading: func(leaderCtx context.Context) {
					if leaderCtx.Err() == nil {
						started.Store(true)
						close(acquired)
						<-leaderCtx.Done()
					}
				},
				OnStoppedLeading: func() {
					if runCtx.Err() == nil {
						close(lost)
					}
				},
			},
		})
	}()
	timer := time.NewTimer(waitDuration)
	defer timer.Stop()
	select {
	case <-acquired:
		if ctx.Err() == nil {
			return &heldLegacyLease{ref: ref, cancel: cancel, done: done, lost: lost}, nil
		}
	case <-ctx.Done():
	case <-done:
	case <-timer.C:
	}
	cancel()
	<-done
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if started.Load() {
		return nil, fmt.Errorf("lost legacy Lease %s/%s during acquisition", ref.namespace, ref.name)
	}
	return nil, fmt.Errorf("timed out acquiring legacy Lease %s/%s; stop all legacy sidecars and check Lease RBAC", ref.namespace, ref.name)
}

func releaseLegacyLeases(client kubernetes.Interface, held []*heldLegacyLease, identity string, timeout time.Duration) error {
	for _, lock := range held {
		lock.cancel()
	}
	var errs []error
	for _, lock := range held {
		select {
		case <-lock.done:
		case <-time.After(timeout + time.Second):
			errs = append(errs, fmt.Errorf("timed out releasing legacy Lease %s/%s", lock.ref.namespace, lock.ref.name))
			continue
		}
		select {
		case <-lock.lost:
			errs = append(errs, fmt.Errorf("lost legacy Lease %s/%s before release", lock.ref.namespace, lock.ref.name))
		default:
		}
		checkCtx, cancel := context.WithTimeout(context.Background(), timeout)
		lease, err := client.CoordinationV1().Leases(lock.ref.namespace).Get(checkCtx, lock.ref.name, metav1.GetOptions{})
		cancel()
		if err != nil {
			errs = append(errs, fmt.Errorf("verify legacy Lease %s/%s release: %w", lock.ref.namespace, lock.ref.name, err))
		} else if lease.Spec.HolderIdentity != nil && *lease.Spec.HolderIdentity != "" {
			errs = append(errs, fmt.Errorf("legacy Lease %s/%s has holder %q after release", lock.ref.namespace, lock.ref.name, *lease.Spec.HolderIdentity))
		}
	}
	return errors.Join(errs...)
}
