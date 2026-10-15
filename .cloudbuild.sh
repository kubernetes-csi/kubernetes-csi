#!/usr/bin/env bash
export GOWORK=off
. release-tools/prow.sh
gcr_cloud_build
