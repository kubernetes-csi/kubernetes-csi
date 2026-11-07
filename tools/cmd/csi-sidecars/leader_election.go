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
	"fmt"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"

	csileaderelection "github.com/kubernetes-csi/csi-lib-utils/leaderelection"
	"github.com/kubernetes-csi/csi-lib-utils/standardflags"
	v1 "k8s.io/api/core/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/scheme"
	corev1 "k8s.io/client-go/kubernetes/typed/core/v1"
	"k8s.io/client-go/rest"
	clientleaderelection "k8s.io/client-go/tools/leaderelection"
	"k8s.io/client-go/tools/leaderelection/resourcelock"
	"k8s.io/client-go/tools/record"
	"k8s.io/klog/v2"
)

var invalidLeaderElectionName = regexp.MustCompile("[^a-zA-Z0-9-]")

// controllerLifecycle prevents shutdown from missing a controller that is
// entering leader election as the signal arrives.
type controllerLifecycle struct {
	mu      sync.Mutex
	running sync.WaitGroup
}

var controllersInElection controllerLifecycle

func (l *controllerLifecycle) begin(shutdown <-chan struct{}) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	select {
	case <-shutdown:
		return false
	default:
		l.running.Add(1)
		return true
	}
}

func (l *controllerLifecycle) finish() {
	l.running.Done()
}

func (l *controllerLifecycle) wait() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.running.Wait()
}

// runWithLeaderElection preserves the sidecars' leader election settings while
// allowing the combined process to drain each controller before ending its
// election. The upstream helper exits the whole process when any election
// stops, including an intentional shutdown.
func runWithLeaderElection(ctx context.Context, config *rest.Config, opts standardflags.SidecarConfiguration, run func(context.Context), driverName string, mux *http.ServeMux, releaseOnExit bool) {
	if !controllersInElection.begin(processShutdown) {
		return
	}
	defer controllersInElection.finish()

	if !releaseOnExit {
		var cancel context.CancelFunc
		ctx, cancel = context.WithCancel(ctx)
		defer cancel()
		go func() {
			select {
			case <-processShutdown:
				cancel()
			case <-ctx.Done():
			}
		}()
	}
	if !opts.LeaderElection {
		run(ctx)
		return
	}

	clientset, err := kubernetes.NewForConfig(config)
	if err != nil {
		klog.Fatalf("Failed to create leader election client: %v", err)
	}
	identity, err := os.Hostname()
	if err != nil {
		klog.Fatalf("Failed to get leader election identity: %v", err)
	}
	namespace := opts.LeaderElectionNamespace
	if namespace == "" {
		namespace = defaultLeaderElectionNamespace()
	}

	var healthCheck *clientleaderelection.HealthzAdaptor
	if opts.HttpEndpoint != "" {
		healthCheck = clientleaderelection.NewLeaderHealthzAdaptor(csileaderelection.DefaultHealthCheckTimeout)
		mux.HandleFunc(csileaderelection.HealthCheckerAddress, func(w http.ResponseWriter, r *http.Request) {
			if err := healthCheck.Check(r); err != nil {
				http.Error(w, fmt.Sprintf("internal server error: %v", err), http.StatusInternalServerError)
				return
			}
			fmt.Fprint(w, "ok")
		})
	}

	coordinateLeaderElection(ctx, processShutdown, run, func(electionCtx context.Context, callbacks clientleaderelection.LeaderCallbacks) {
		broadcaster := record.NewBroadcaster(record.WithContext(electionCtx))
		broadcaster.StartRecordingToSink(&corev1.EventSinkImpl{Interface: clientset.CoreV1().Events(namespace)})
		recorder := broadcaster.NewRecorder(scheme.Scheme, v1.EventSource{Component: fmt.Sprintf("%s/%s", driverName, identity)})
		lock, err := resourcelock.NewWithLabels(
			resourcelock.LeasesResourceLock,
			namespace,
			sanitizeLeaderElectionName(driverName),
			clientset.CoreV1(),
			clientset.CoordinationV1(),
			resourcelock.ResourceLockConfig{Identity: sanitizeLeaderElectionName(identity), EventRecorder: recorder},
			opts.LeaderElectionLabels,
		)
		if err != nil {
			klog.Fatalf("Failed to create leader election lock: %v", err)
		}
		clientleaderelection.RunOrDie(electionCtx, clientleaderelection.LeaderElectionConfig{
			Lock:            lock,
			LeaseDuration:   opts.LeaderElectionLeaseDuration,
			RenewDeadline:   opts.LeaderElectionRenewDeadline,
			RetryPeriod:     opts.LeaderElectionRetryPeriod,
			ReleaseOnCancel: releaseOnExit,
			Callbacks:       callbacks,
			WatchDog:        healthCheck,
		})
	}, func() {
		klog.Error("Stopped leading")
		klog.FlushAndExit(klog.ExitFlushTimeout, 1)
	})
}

// coordinateLeaderElection handles the race between receiving a shutdown
// signal and becoming leader. A follower stops election immediately; a leader
// keeps its lease until its run callback has returned.
func coordinateLeaderElection(ctx context.Context, shutdown <-chan struct{}, run func(context.Context), elect func(context.Context, clientleaderelection.LeaderCallbacks), onLost func()) {
	electionCtx, cancelElection := context.WithCancel(context.WithoutCancel(ctx))
	defer cancelElection()

	var mu sync.Mutex
	started := false
	runDone := make(chan struct{})
	watcherDone := make(chan struct{})
	go func() {
		defer close(watcherDone)
		select {
		case <-shutdown:
		case <-ctx.Done():
		case <-electionCtx.Done():
			return
		}
		mu.Lock()
		if !started {
			cancelElection()
		}
		mu.Unlock()
	}()

	elect(electionCtx, clientleaderelection.LeaderCallbacks{
		OnStartedLeading: func(context.Context) {
			mu.Lock()
			if electionCtx.Err() != nil {
				mu.Unlock()
				return
			}
			started = true
			mu.Unlock()

			run(ctx)
			close(runDone)
			cancelElection()
		},
		OnStoppedLeading: func() {
			if electionCtx.Err() == nil {
				onLost()
			}
		},
	})

	mu.Lock()
	wasStarted := started
	mu.Unlock()
	if wasStarted {
		<-runDone
	}
	cancelElection()
	<-watcherDone
}

func sanitizeLeaderElectionName(name string) string {
	name = invalidLeaderElectionName.ReplaceAllString(name, "-")
	if strings.HasSuffix(name, "-") {
		name += "X"
	}
	return name
}

func defaultLeaderElectionNamespace() string {
	if namespace := os.Getenv("POD_NAMESPACE"); namespace != "" {
		return namespace
	}
	if data, err := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/namespace"); err == nil {
		if namespace := strings.TrimSpace(string(data)); namespace != "" {
			return namespace
		}
	}
	return "default"
}
