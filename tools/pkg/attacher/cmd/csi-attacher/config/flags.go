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
	"time"
)

type AttacherConfiguration struct {
	MaxEntries         int
	ReconcileSync      time.Duration
	MaxGRPCLogLength   int
	WorkerThreads      int
	DefaultFSType      string
	Timeout            time.Duration
	RetryIntervalStart time.Duration
	RetryIntervalMax   time.Duration
}

func registerAttacherFlags(flags *flag.FlagSet, configuration *AttacherConfiguration, prefix string) {
	flags.IntVar(&configuration.MaxEntries, prefix+"max-entries", 0, "Max entries per each page in volume lister call, 0 means no limit.")
	flags.DurationVar(&configuration.ReconcileSync, prefix+"reconcile-sync", 1*time.Minute, "Resync interval of the VolumeAttachment reconciler.")
	flags.IntVar(&configuration.MaxGRPCLogLength, prefix+"max-grpc-log-length", -1, "The maximum amount of characters logged for every grpc responses. Defaults to no limit")
	flags.IntVar(&configuration.WorkerThreads, prefix+"worker-threads", 10, "Number of worker threads per sidecar")
	flags.StringVar(&configuration.DefaultFSType, prefix+"default-fstype", "", "The default filesystem type of the volume to use.")
	flags.DurationVar(&configuration.Timeout, prefix+"timeout", 15*time.Second, "Timeout for waiting for attaching or detaching the volume.")
	flags.DurationVar(&configuration.RetryIntervalStart, prefix+"retry-interval-start", time.Second, "Initial retry interval of failed create volume or deletion. It doubles with each failure, up to retry-interval-max.")
	flags.DurationVar(&configuration.RetryIntervalMax, prefix+"retry-interval-max", 5*time.Minute, "Maximum retry interval of failed create volume or deletion.")
}

func RegisterAttacherFlags(flags *flag.FlagSet, configuration *AttacherConfiguration) {
	registerAttacherFlags(flags, configuration, "")
}

func RegisterAttacherFlagsWithPrefix(flags *flag.FlagSet, configuration *AttacherConfiguration) {
	registerAttacherFlags(flags, configuration, "attacher-")
}
