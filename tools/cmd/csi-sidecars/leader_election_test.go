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
)

func awaitElectionEvent(t *testing.T, event string, ch <-chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(5 * time.Second):
		t.Fatalf("timed out waiting for %s", event)
	}
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
		runAIOController(context.Background(), func(ctx context.Context) {
			close(running)
			<-ctx.Done()
		}, http.NewServeMux(), false)
		close(done)
	}()
	awaitElectionEvent(t, "controller startup", running)
	close(shutdown)
	awaitElectionEvent(t, "controller exit", done)
	controllersInElection.wait()
}
