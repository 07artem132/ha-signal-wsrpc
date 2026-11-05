# Signal (WsRpcServer) for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/07artem132/ha-signal-wsrpc/actions/workflows/validate.yml/badge.svg)](https://github.com/07artem132/ha-signal-wsrpc/actions/workflows/validate.yml)

Send and receive [Signal](https://signal.org/) messages from Home Assistant through
[**SignalCliNet.WsRpcServer**](https://github.com/07artem132/SignalCliNet.WsRpcServer), a
WebSocket JSON-RPC 2.0 bridge around `signal-cli`.

Unlike the built-in `signal_messenger` integration (which needs `bbernhard/signal-cli-rest-api`),
this integration keeps **one persistent WebSocket**. Sends are instant, and incoming messages arrive
as Home Assistant events, so you don't need to poll.

## Features

- UI setup (config flow), no YAML needed.
- **Notify entities**: add recipients (phone number, UUID or group id) as sub-entries; each one
  becomes a `notify.*` entity that works with `notify.send_message`.
- **Action** `signal_wsrpc.send_message`: send to any list of recipients, and optionally get the
  Signal timestamp back.
- **Event** `signal_wsrpc_message_received` for incoming text messages, which you can use as an
  automation trigger.
- **Connectivity** binary sensor.
- Auto-reconnect with jittered backoff, a keepalive `ping`, and re-subscribe after a reconnect.
- Server auth (`Server:Auth:Enabled=true`): one-time invite → ECDSA P-256 device key
  (generated in HA) → bearer token (WS subprotocol) + proof-of-possession. An expired or revoked
  token starts a re-auth flow (enter a new invite). Invite onboarding needs a WsRpcServer build
  that accepts the `wsrpc-onboarding` subprotocol.
- `wss://` with the server's auto-generated CA. Paste `ca.crt` into the setup form.

## Requirements

- Home Assistant 2025.3 or newer (config sub-entries).
- **A running SignalCliNet.WsRpcServer with a linked Signal account.** The integration is a client
  and does not start the server for you yet (a "managed mode" that downloads and runs it is planned).

### Prerequisites: run WsRpcServer first

The quickest way is on the same host as Home Assistant, on loopback, without auth:

```bash
git clone https://github.com/07artem132/SignalCliNet.WsRpcServer && cd SignalCliNet.WsRpcServer
dotnet run --project src/SignalCliNet.WsRpcServer   # listens on ws://127.0.0.1:9000 (needs .NET 10 + JDK 25)
```

Link your Signal account once (`startLink` → scan the QR in Signal → *Linked devices* →
`finishLink`). See the server's `docs/self-host.md`. For containers and `wss://`, see its
`deploy/DEPLOYMENT.md`.

> If Home Assistant runs in Docker or HAOS, `127.0.0.1` is the Home Assistant container itself.
> Use the address of the host where the server runs. A non-loopback server needs TLS + auth (see
> the server docs).

## Installation

**HACS:** HACS → ⋮ → *Custom repositories* → `https://github.com/07artem132/ha-signal-wsrpc`,
category *Integration* → install **Signal (WsRpcServer)** → restart Home Assistant.

**Manual:** copy `custom_components/signal_wsrpc` into `<config>/custom_components/` and restart.

## Setup

*Settings → Devices & services → Add integration → Signal (WsRpcServer)*

| Field | Example | Notes |
|---|---|---|
| Server URL | `ws://127.0.0.1:9000` / `wss://signal.lan:9443` | |
| Verify TLS certificate | on | Turn it off only for testing |
| Custom CA certificate | contents of `data/secrets/ca.crt` | Needed with the server's auto-generated CA |
| Sending account | `+380501234567` | Must be linked on the server |
| Invite code | `XXXX-XXXX-…` | Only when the server has auth enabled (`createInvite` on the admin port) |

Then open the integration → **Add recipient** for every person or group you want a
`notify` entity for.

> **Security.** Without server auth, WsRpcServer is an open send relay for anyone who can reach
> the port. Keep it on loopback (HA and the server on the same host) or turn on auth + `wss://`
> before you expose it on a network. The token and device key are stored in HA's config entry.

## Usage

```yaml
# Notify entity (one recipient)
action: notify.send_message
target:
  entity_id: notify.artem
data:
  title: "🏠 Home"
  message: "Front door opened"

# Any recipients
action: signal_wsrpc.send_message
data:
  recipients: ["+380501234567", "group.base64id=="]
  message: "Washing machine finished"
```

Automation on an incoming message:

```yaml
triggers:
  - trigger: event
    event_type: signal_wsrpc_message_received
    event_data:
      source: "+380501234567"
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.message | lower == 'світло' }}"
actions:
  - action: light.toggle
    target:
      entity_id: light.living_room
```

Event data: `account`, `source`, `source_uuid`, `source_name`, `message`, `group_id`,
`group_name`, `timestamp`.

> Receiving needs signal-cli running in receive mode on the server. With the default
> `Server:GroupClaim:Enabled=false` the server starts `signal-cli` with `--receive-mode=manual`
> (send-only), so no events arrive. You can still send. You can turn incoming events off in the
> integration options.

## Limitations (v0.1)

- Text only. Attachments wait for an attachment RPC on the server side.
- One sending account per config entry (add the integration again for another account).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

Tests run against an in-process fake of the WsRpcServer wire protocol (`tests/fake_server.py`).

## License

MIT
