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
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/kubernetes-csi/csi-lib-utils/standardflags"
	dto "github.com/prometheus/client_model/go"
)

func TestSharedHTTPMetrics(t *testing.T) {
	originalPath := standardflags.Configuration.MetricsPath
	standardflags.Configuration.MetricsPath = "/internal/metrics"
	t.Cleanup(func() { standardflags.Configuration.MetricsPath = originalPath })

	var endpoint aioHTTPEndpoint
	// Use the kernel-assigned port from the first call for the second
	// controller. Another listener on the same address would fail to bind.
	addr := "127.0.0.1:0"
	controllers := []string{"attacher", "provisioner"}
	for i, controller := range controllers {
		manager := newAIOMetricsManager(controller, "")
		manager.SetDriverName("example.csi")
		manager.RecordMetrics("/csi.v1.Identity/Probe", nil, time.Second)
		mux := http.NewServeMux()
		mux.HandleFunc("/"+controller, func(w http.ResponseWriter, _ *http.Request) {
			fmt.Fprint(w, controller)
		})
		if err := endpoint.listen(addr, mux, manager.GetRegistry()); err != nil {
			t.Fatalf("%s: %v", controller, err)
		}
		if i == 0 {
			t.Cleanup(func() { endpoint.listener.Close() })
			addr = endpoint.listener.Addr().String()
		}
	}

	get := func(path string) string {
		t.Helper()
		resp, err := http.Get("http://" + addr + path)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		body, err := io.ReadAll(resp.Body)
		if err != nil || resp.StatusCode != http.StatusOK {
			t.Fatalf("GET %s: %s: %s (%v)", path, resp.Status, body, err)
		}
		return string(body)
	}
	body := get(standardflags.Configuration.MetricsPath)
	for _, controller := range controllers {
		want := fmt.Sprintf("csi_sidecar_operations_seconds_count{controller=%q,driver_name=\"example.csi\",grpc_status_code=\"OK\",method_name=\"/csi.v1.Identity/Probe\"} 1\n", controller)
		if !strings.Contains(body, want) {
			t.Errorf("missing sample: %s", want)
		}
		if got := get("/" + controller); got != controller {
			t.Errorf("route for %s returned %q", controller, got)
		}
	}
	if got := strings.Count(body, "\nprocess_start_time_seconds "); got != 1 {
		t.Errorf("expected one process start time, got %d", got)
	}
}

func TestSharedHTTPMetricsContinueOnError(t *testing.T) {
	// A failing registry must not fail the scrape: the handler is configured
	// with ContinueOnError, matching the standalone sidecars. A working
	// registry alongside the broken one still produces its samples.
	originalPath := standardflags.Configuration.MetricsPath
	standardflags.Configuration.MetricsPath = "/internal/metrics"
	t.Cleanup(func() { standardflags.Configuration.MetricsPath = originalPath })

	var endpoint aioHTTPEndpoint
	manager := newAIOMetricsManager("attacher", "")
	manager.SetDriverName("example.csi")
	manager.RecordMetrics("/csi.v1.Identity/Probe", nil, time.Second)
	if err := endpoint.listen("127.0.0.1:0", http.NewServeMux(), brokenGatherer{}, manager.GetRegistry()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { endpoint.listener.Close() })
	addr := endpoint.listener.Addr().String()

	resp, err := http.Get("http://" + addr + standardflags.Configuration.MetricsPath)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("scrape with one failing gatherer: %s: %s", resp.Status, body)
	}
	if !strings.Contains(string(body), `csi_sidecar_operations_seconds_count{controller="attacher",driver_name="example.csi",grpc_status_code="OK",method_name="/csi.v1.Identity/Probe"} 1`) {
		t.Error("expected samples from the working registry")
	}
}

// brokenGatherer fails on every Gather call.
type brokenGatherer struct{}

func (brokenGatherer) Gather() ([]*dto.MetricFamily, error) {
	return nil, errors.New("gatherer unavailable")
}

func TestSharedHTTPMetricsSubtree(t *testing.T) {
	const path = "/monitoring/"
	originalPath := standardflags.Configuration.MetricsPath
	standardflags.Configuration.MetricsPath = path
	t.Cleanup(func() { standardflags.Configuration.MetricsPath = originalPath })

	var endpoint aioHTTPEndpoint
	controller := http.NewServeMux()
	controller.HandleFunc(path+"health", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	if err := endpoint.listen("127.0.0.1:0", controller); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { endpoint.listener.Close() })

	// Compare with the standalone controller's ServeMux, including Go's
	// redirect status and preservation of the query string.
	standalone := http.NewServeMux()
	standalone.HandleFunc(path, func(http.ResponseWriter, *http.Request) {})
	standalone.Handle(path+"health", controller)
	for _, target := range []string{"/monitoring?x=1", path, path + "sub", path + "health", "/monitoring-other"} {
		t.Run(target, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, target, nil)
			want, got := httptest.NewRecorder(), httptest.NewRecorder()
			standalone.ServeHTTP(want, req)
			endpoint.ServeHTTP(got, req)
			if got.Code != want.Code || got.Header().Get("Location") != want.Header().Get("Location") {
				t.Fatalf("got %d Location %q, want %d Location %q", got.Code, got.Header().Get("Location"), want.Code, want.Header().Get("Location"))
			}
			if want.Code == http.StatusOK && !strings.Contains(got.Body.String(), "# HELP go_goroutines ") {
				t.Error("expected process metrics in the response")
			}
		})
	}
}
