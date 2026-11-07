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
	"net/http"
	"testing"
	"time"

	"github.com/kubernetes-csi/csi-lib-utils/standardflags"
	clientleaderelection "k8s.io/client-go/tools/leaderelection"
)

func awaitElectionEvent(t *testing.T, event string, ch <-chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(5 * time.Second):
		t.Fatalf("timed out waiting for %s", event)
	}
}

func TestFollowerStopsOnShutdown(t *testing.T) {
	shutdown := make(chan struct{})
	electing := make(chan struct{})
	done := make(chan struct{})
	runCalled := make(chan struct{})
	lost := make(chan struct{})
	go func() {
		coordinateLeaderElection(context.Background(), shutdown, func(context.Context) {
			close(runCalled)
		}, func(ctx context.Context, callbacks clientleaderelection.LeaderCallbacks) {
			close(electing)
			<-ctx.Done()
			callbacks.OnStoppedLeading()
		}, func() {
			close(lost)
		})
		close(done)
	}()
	awaitElectionEvent(t, "election startup", electing)
	close(shutdown)
	awaitElectionEvent(t, "follower exit", done)
	select {
	case <-runCalled:
		t.Fatal("follower started a controller")
	case <-lost:
		t.Fatal("intentional follower shutdown was treated as lease loss")
	default:
	}
}

func TestLeaderDrainsBeforeStoppingElection(t *testing.T) {
	for _, cancelParent := range []bool{false, true} {
		name := "release-on-exit"
		if cancelParent {
			name = "parent-context-canceled"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			shutdown := make(chan struct{})
			running := make(chan struct{})
			signalObserved := make(chan struct{})
			allowDrain := make(chan struct{})
			electionStopped := make(chan struct{})
			done := make(chan struct{})
			lost := make(chan struct{})
			go func() {
				coordinateLeaderElection(ctx, shutdown, func(runCtx context.Context) {
					close(running)
					if cancelParent {
						<-runCtx.Done()
					} else {
						<-shutdown
					}
					close(signalObserved)
					<-allowDrain
				}, func(electionCtx context.Context, callbacks clientleaderelection.LeaderCallbacks) {
					go callbacks.OnStartedLeading(electionCtx)
					<-electionCtx.Done()
					close(electionStopped)
					callbacks.OnStoppedLeading()
				}, func() {
					close(lost)
				})
				close(done)
			}()
			awaitElectionEvent(t, "leader startup", running)
			close(shutdown)
			if cancelParent {
				cancel()
			}
			awaitElectionEvent(t, "controller shutdown signal", signalObserved)
			select {
			case <-electionStopped:
				t.Fatal("election stopped before the controller drained")
			case <-time.After(50 * time.Millisecond):
			}
			close(allowDrain)
			awaitElectionEvent(t, "leader exit", done)
			awaitElectionEvent(t, "election stop", electionStopped)
			select {
			case <-lost:
				t.Fatal("intentional leader shutdown was treated as lease loss")
			default:
			}
		})
	}
}

func TestShutdownBeforeLeaderCallbackSkipsController(t *testing.T) {
	shutdown := make(chan struct{})
	callbackSkipped := make(chan struct{})
	runCalled := make(chan struct{})
	done := make(chan struct{})
	go func() {
		coordinateLeaderElection(context.Background(), shutdown, func(context.Context) {
			close(runCalled)
		}, func(ctx context.Context, callbacks clientleaderelection.LeaderCallbacks) {
			close(shutdown)
			<-ctx.Done()
			callbacks.OnStartedLeading(ctx)
			close(callbackSkipped)
			callbacks.OnStoppedLeading()
		}, func() {
			t.Error("intentional shutdown was treated as lease loss")
		})
		close(done)
	}()
	awaitElectionEvent(t, "late leader callback", callbackSkipped)
	awaitElectionEvent(t, "follower exit", done)
	select {
	case <-runCalled:
		t.Fatal("controller started after shutdown")
	default:
	}
}

func TestUnexpectedElectionStopReportsLeaseLoss(t *testing.T) {
	lost := make(chan struct{})
	coordinateLeaderElection(context.Background(), make(chan struct{}), func(context.Context) {
		t.Fatal("controller should not start")
	}, func(_ context.Context, callbacks clientleaderelection.LeaderCallbacks) {
		callbacks.OnStoppedLeading()
	}, func() {
		close(lost)
	})
	awaitElectionEvent(t, "lease-loss failure", lost)
}

func TestShutdownWaitsOnlyForStartedControllers(t *testing.T) {
	var lifecycle controllerLifecycle
	shutdown := make(chan struct{})
	if !lifecycle.begin(shutdown) {
		t.Fatal("controller should enter before shutdown")
	}
	blockedStartup := make(chan struct{})
	lateResult := make(chan bool, 1)
	go func() {
		<-blockedStartup
		lateResult <- lifecycle.begin(shutdown)
	}()

	close(shutdown)
	waitDone := make(chan struct{})
	go func() {
		lifecycle.wait()
		close(waitDone)
	}()
	select {
	case <-waitDone:
		t.Fatal("shutdown returned before the running controller drained")
	case <-time.After(50 * time.Millisecond):
	}
	lifecycle.finish()
	awaitElectionEvent(t, "controller drain", waitDone)
	close(blockedStartup)
	select {
	case started := <-lateResult:
		if started {
			t.Fatal("controller started after shutdown")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("blocked startup did not return")
	}
}

func TestControllerWithoutLeaderElectionStopsOnSignal(t *testing.T) {
	previous := processShutdown
	shutdown := make(chan struct{})
	processShutdown = shutdown
	t.Cleanup(func() { processShutdown = previous })

	running := make(chan struct{})
	done := make(chan struct{})
	go func() {
		runWithLeaderElection(context.Background(), nil, standardflags.SidecarConfiguration{}, func(ctx context.Context) {
			close(running)
			<-ctx.Done()
		}, "test", http.NewServeMux(), false)
		close(done)
	}()
	awaitElectionEvent(t, "controller startup", running)
	close(shutdown)
	awaitElectionEvent(t, "controller exit", done)
	controllersInElection.wait()
}
