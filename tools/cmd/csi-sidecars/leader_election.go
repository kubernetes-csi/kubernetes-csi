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
	clientleaderelection "k8s.io/client-go/tools/leaderelection"
)

var invalidLeaderElectionName = regexp.MustCompile("[^a-zA-Z0-9-]")

// controllerLifecycle tracks controllers that started before shutdown.
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

// runAIOController registers a controller for coordinated process shutdown.
// With leader election enabled, AIO calls this only after the shared Lease is held.
func runAIOController(ctx context.Context, run func(context.Context), mux *http.ServeMux, releaseOnExit bool) {
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
	if healthCheck, ok := ctx.Value(consolidatedLeaseContextKey{}).(*clientleaderelection.HealthzAdaptor); ok && standardflags.Configuration.HttpEndpoint != "" {
		mux.HandleFunc(csileaderelection.HealthCheckerAddress, func(w http.ResponseWriter, r *http.Request) {
			if err := healthCheck.Check(r); err != nil {
				http.Error(w, fmt.Sprintf("internal server error: %v", err), http.StatusInternalServerError)
				return
			}
			fmt.Fprint(w, "ok")
		})
	}
	run(ctx)
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
