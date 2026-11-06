# syntax=docker/dockerfile:1
# Build the same upstream release used by the integration fixtures. Its former
# public Quay image now requires authentication; production does not use this image.
FROM golang:1.26.10-bookworm@sha256:5de7c99eb78ca5e515a4c340235d0955ec5964a5071c492c15503d7c267da473 AS build
WORKDIR /src
RUN --mount=type=secret,id=proxy_ca \
    set -eu; \
    if [ -f /run/secrets/proxy_ca ]; then \
        export SSL_CERT_FILE=/run/secrets/proxy_ca GIT_SSL_CAINFO=/run/secrets/proxy_ca; \
    fi; \
    git init .; \
    git remote add origin https://github.com/minio/minio.git; \
    git fetch --depth=1 origin 07c3a429bfed433e49018cb0f78a52145d4bedeb; \
    git checkout --detach FETCH_HEAD; \
    test "$(git rev-parse HEAD)" = 07c3a429bfed433e49018cb0f78a52145d4bedeb; \
    CGO_ENABLED=0 GOMAXPROCS=2 go build -mod=readonly -p 2 -trimpath -o /out/minio .

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/minio /usr/local/bin/minio
COPY --from=build /src/LICENSE /usr/share/licenses/minio/LICENSE
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.revision="07c3a429bfed433e49018cb0f78a52145d4bedeb" \
      org.opencontainers.image.licenses="AGPL-3.0"
ENTRYPOINT ["minio"]
