"""Notify entities: one per configured recipient (config subentry)."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SignalConfigEntry, async_send
from .const import CONF_RECIPIENT, DOMAIN, SIGNAL_CONNECTION_STATE, SUBENTRY_RECIPIENT

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SignalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a notify entity for every recipient subentry."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_RECIPIENT:
            continue
        async_add_entities(
            [SignalRecipientNotifyEntity(entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class SignalRecipientNotifyEntity(NotifyEntity):
    """Send a Signal message to one fixed recipient (phone number, UUID or group id)."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: SignalConfigEntry, subentry: ConfigSubentry) -> None:
        self._entry = entry
        self._recipient: str = subentry.data[CONF_RECIPIENT]
        self._attr_unique_id = f"{entry.entry_id}_{subentry.subentry_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{subentry.subentry_id}")},
            name=subentry.title,
            manufacturer="Signal",
            model="Recipient",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Available while the WS connection is up."""
        return self._entry.runtime_data.client.connected

    async def async_added_to_hass(self) -> None:
        """Refresh availability when the connection state changes."""

        @callback
        def _update(_: bool) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CONNECTION_STATE.format(self._entry.entry_id), _update
            )
        )

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Send `title\\nmessage` (title optional)."""
        text = f"{title}\n{message}" if title else message
        await async_send(self._entry, [self._recipient], text)
