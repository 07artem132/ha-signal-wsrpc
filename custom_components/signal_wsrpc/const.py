"""Constants for the Signal (WsRpcServer) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "signal_wsrpc"

CONF_URL: Final = "url"
CONF_ACCOUNT: Final = "account"
CONF_TOKEN: Final = "token"
CONF_DEVICE_KEY: Final = "device_key"
CONF_INVITE_CODE: Final = "invite_code"
CONF_CA_CERT: Final = "ca_cert"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_RECEIVE: Final = "receive_messages"
CONF_RECIPIENT: Final = "recipient"

SUBENTRY_RECIPIENT: Final = "recipient"

EVENT_MESSAGE_RECEIVED: Final = f"{DOMAIN}_message_received"

SERVICE_SEND_MESSAGE: Final = "send_message"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_RECIPIENTS: Final = "recipients"
ATTR_MESSAGE: Final = "message"

SIGNAL_CONNECTION_STATE: Final = f"{DOMAIN}_connection_state_{{}}"

# Bitmask from SignalCliNet.WsRpcServer `SignalEventTypes` (enum is serialized as a number).
EVENT_TYPES_TEXT_MESSAGES: Final = 1 << 0
