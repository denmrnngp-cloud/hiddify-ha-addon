ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

ARG BUILD_ARCH=amd64
ARG HIDDIFY_VERSION=4.1.0

# Install runtime dependencies
RUN apk add --no-cache \
    bash \
    curl \
    jq \
    python3 \
    py3-requests \
    iproute2 \
    ca-certificates \
    iptables \
    ip6tables \
    tzdata

# Download hiddify-core binary (musl build for Alpine)
RUN case "${BUILD_ARCH}" in \
      amd64)  ARCH="amd64" ;; \
      aarch64) ARCH="arm64" ;; \
      *) echo "Unsupported arch: ${BUILD_ARCH}" && exit 1 ;; \
    esac && \
    curl -fsSL \
      "https://github.com/hiddify/hiddify-core/releases/download/v${HIDDIFY_VERSION}/hiddify-core-linux-${ARCH}-musl.tar.gz" \
      -o /tmp/hiddify.tar.gz && \
    tar -xzf /tmp/hiddify.tar.gz -C /tmp/ && \
    mv /tmp/hiddify-core-linux-${ARCH}-musl/hiddify-core /usr/local/bin/hiddify-core && \
    chmod +x /usr/local/bin/hiddify-core && \
    rm -rf /tmp/hiddify*

# Data directory for configs and state
RUN mkdir -p /data/hiddify /var/lib/hiddify

COPY run.sh /run.sh
COPY parse_sub.py /parse_sub.py
COPY grpc_ctl.py /grpc_ctl.py

RUN chmod +x /run.sh

CMD ["/run.sh"]
