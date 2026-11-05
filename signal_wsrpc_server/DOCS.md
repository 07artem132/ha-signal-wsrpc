# Signal WsRpcServer

Runs [SignalCliNet.WsRpcServer](https://github.com/07artem132/SignalCliNet.WsRpcServer) (signal-cli +
WebSocket JSON-RPC) for the **Signal (WsRpcServer)** integration on Home Assistant OS.

## How to use

1. Install the **Signal (WsRpcServer)** integration via HACS and restart Home Assistant.
2. Install and start this add-on.
3. Open *Settings → Devices & services*. Under *Discovered* click **Signal WsRpcServer** → *Configure*.
4. On your phone open Signal → *Settings → Linked devices → Link new device* and scan the QR code.

Home Assistant becomes a linked device of your Signal account, like Signal Desktop.

## Options

| Option | Default | Notes |
|---|---|---|
| `port` | `9876` | Local port, bound to `127.0.0.1` only. Change it if something on the host already uses it; the integration picks up the new port automatically. |
| `log_level` | `Information` | Message bodies and phone numbers are never logged. |

## Security

The server listens on `127.0.0.1` only and has authentication turned off, so only Home Assistant
and other host-network processes on the same machine can reach it. Nothing is exposed to your
network.

Signal keys are stored in the add-on's `/data` and included in Home Assistant backups. Treat those
backups as secrets.
