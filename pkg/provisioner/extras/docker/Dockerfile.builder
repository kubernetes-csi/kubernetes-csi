FROM golang:alpine
LABEL maintainers="Kubernetes Authors"
LABEL description="CSI Provisioner"

RUN apk add --no-cache git
RUN go get github.com/kubernetes-csi/csi-sidecars/pkg/provisioner/cmd/csi-provisioner
