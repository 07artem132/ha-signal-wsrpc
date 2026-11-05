ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

ARG BUILD_ARCH=amd64
ARG SERVER_VERSION=2.3.2
# SHA-256 бандлів релізу v2.3.2 (зафіксовані тут, а не лише з SHA256SUMS того ж релізу).
ARG SHA256_X64=5c721ca4e09b3078c0e2e65a4cbbb64851a9ea3df4b23c296fa4f9708d4c572b
ARG SHA256_ARM64=526b983e8326ff4a15999630765288839333d8729c47f5a7e3204fa30795061a

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libicu72 libssl3 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${BUILD_ARCH}" in \
      amd64) arch=x64; sha="${SHA256_X64}" ;; \
      aarch64) arch=arm64; sha="${SHA256_ARM64}" ;; \
      *) echo "Непідтримувана архітектура: ${BUILD_ARCH}" >&2; exit 1 ;; \
    esac; \
    name="SignalCliNet.WsRpcServer-${SERVER_VERSION}-linux-${arch}-bundle.tar.gz"; \
    curl -fsSL -o /tmp/bundle.tar.gz \
      "https://github.com/07artem132/SignalCliNet.WsRpcServer/releases/download/v${SERVER_VERSION}/${name}"; \
    echo "${sha}  /tmp/bundle.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/wsrpc; \
    tar -xzf /tmp/bundle.tar.gz -C /opt/wsrpc; \
    rm /tmp/bundle.tar.gz; \
    /opt/wsrpc/jre/bin/java -version

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
