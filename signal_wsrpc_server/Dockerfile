# Мульти-арх base image HA (amd64 + arm64); build.yaml більше не потрібен.
FROM ghcr.io/home-assistant/base-debian:bookworm-2026.10.3

ARG SERVER_VERSION=2.3.4
# SHA-256 бандлів релізу v2.3.2 (зафіксовані тут, а не лише з SHA256SUMS того ж релізу).
ARG SHA256_X64=dddbc695573ad765bb90412816cd967101781105ccd25293a8547f08db60f974
ARG SHA256_ARM64=a818dd355198607d2b2d3b101469005d44d13a526f3bc1e85f4523a1909036f8

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libicu72 libssl3 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    machine="$(uname -m)"; \
    case "${machine}" in \
      x86_64) arch=x64; sha="${SHA256_X64}" ;; \
      aarch64) arch=arm64; sha="${SHA256_ARM64}" ;; \
      *) echo "Непідтримувана архітектура: ${machine}" >&2; exit 1 ;; \
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
