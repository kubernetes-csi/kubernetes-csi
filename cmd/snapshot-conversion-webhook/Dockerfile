FROM gcr.io/distroless/static@sha256:58133991db06659feaabe0f4e97a35cebf15ef4ea08f8a4c6d2ee5f75e4aa6a0
LABEL maintainers="Kubernetes Authors"
LABEL description="snapshot-conversion-webhook"
ARG binary=./bin/snapshot-conversion-webhook
COPY ${binary} /snapshot-conversion-webhook
ENTRYPOINT ["/snapshot-conversion-webhook"]
