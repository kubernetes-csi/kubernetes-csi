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
	"errors"
	"net"
	"net/http"
	"slices"
	"sync"

	"github.com/kubernetes-csi/csi-lib-utils/metrics"
	"github.com/kubernetes-csi/csi-lib-utils/standardflags"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	dto "github.com/prometheus/client_model/go"
	"k8s.io/component-base/metrics/legacyregistry"
	"k8s.io/klog/v2"
)

func newAIOMetricsManager(controller, driver string, options ...metrics.MetricsManagerOption) metrics.CSIMetricsManager {
	return metrics.NewCSIMetricsManagerWithOptions(driver, append(options,
		metrics.WithLabels(map[string]string{"controller": controller}),
		metrics.WithProcessStartTime(false), // Already collected by legacyregistry.
	)...)
}

type aioHTTPEndpoint struct {
	mu         sync.Mutex
	listener   net.Listener
	muxes      []*http.ServeMux
	registries prometheus.Gatherers
	metrics    http.ServeMux
}

var sharedHTTP aioHTTPEndpoint

// Each controller contributes its own routes and registries. The address is a
// process-wide flag, so only the first controller needs to bind it.
func (e *aioHTTPEndpoint) listen(addr string, mux *http.ServeMux, registries ...prometheus.Gatherer) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.listener == nil {
		listener, err := net.Listen("tcp", addr)
		if err != nil {
			return err
		}
		instrumentation := prometheus.NewRegistry()
		e.registries = prometheus.Gatherers{legacyregistry.DefaultGatherer, instrumentation}
		e.metrics.Handle(standardflags.Configuration.MetricsPath,
			promhttp.InstrumentMetricHandler(instrumentation, promhttp.HandlerFor(e, promhttp.HandlerOpts{
				// Match the standalone sidecars (ContinueOnError in
				// csi-lib-utils/metrics RegisterToServer): skip a failing
				// registry and still expose the rest. Gatherers.Gather
				// returns a non-nil error only when all registries fail, so
				// a completely broken scrape is still reported.
				ErrorHandling: promhttp.ContinueOnError,
			})))
		e.listener = listener
		klog.InfoS("Serving shared CSI HTTP endpoint", "address", listener.Addr())
		go func() {
			if err := http.Serve(listener, e); err != nil && !errors.Is(err, net.ErrClosed) {
				klog.Fatalf("Serving CSI HTTP endpoint: %v", err)
			}
		}()
	}
	e.muxes = append(e.muxes, mux)
	e.registries = append(e.registries, registries...)
	return nil
}

func (e *aioHTTPEndpoint) Gather() ([]*dto.MetricFamily, error) {
	e.mu.Lock()
	registries := slices.Clone(e.registries)
	e.mu.Unlock()
	return registries.Gather()
}

func (e *aioHTTPEndpoint) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	e.mu.Lock()
	muxes := slices.Clone(e.muxes)
	e.mu.Unlock()
	_, metricsPattern := e.metrics.Handler(r)
	for _, mux := range muxes {
		// Controller routes are literal paths. A more specific health or
		// profiling route still takes precedence over a metrics subtree.
		if _, pattern := mux.Handler(r); len(pattern) > len(metricsPattern) {
			mux.ServeHTTP(w, r)
			return
		}
	}
	e.metrics.ServeHTTP(w, r)
}
