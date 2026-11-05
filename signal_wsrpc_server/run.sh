#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

PORT="$(bashio::config 'port')"
LOG_LEVEL="$(bashio::config 'log_level')"

mkdir -p /data/signal-cli
chmod 700 /data /data/signal-cli

# Лише loopback і без auth: HA Core на HAOS у host network, тож бачить 127.0.0.1 add-on'а.
export Server__Host=127.0.0.1
export Server__Port="${PORT}"
export Server__Admin__Enabled=false
export Server__Auth__Enabled=false
export Persistence__DataDirectory=/data
export SignalCli__LibDirectory=/opt/wsrpc/signal-cli/lib
export SignalCli__StoragePathCli=/data/signal-cli
export SignalCli__JavaExecutable=/opt/wsrpc/jre/bin/java
export Logging__LogLevel__Default="${LOG_LEVEL}"
export DOTNET_CLI_TELEMETRY_OPTOUT=1

# Коли порт відкрився, повідомляємо HA (Settings → Devices & services → Discovered).
(
    for _ in $(seq 1 90); do
        if (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
            if bashio::discovery "signal_wsrpc" "$(bashio::var.json host 127.0.0.1 port "^${PORT}")" > /dev/null; then
                bashio::log.info "Discovery надіслано в Home Assistant (порт ${PORT})"
            else
                bashio::log.warning "Не вдалося надіслати discovery в Home Assistant"
            fi
            exit 0
        fi
        sleep 2
    done
    bashio::log.warning "Сервер не відкрив порт ${PORT} за 3 хв, discovery не надіслано"
) &

bashio::log.info "Запуск SignalCliNet.WsRpcServer на 127.0.0.1:${PORT}"
cd /opt/wsrpc
exec ./SignalCliNet.WsRpcServer
