"""Connectivity sensor for the WsRpcServer connection."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SignalConfigEntry
from .const import CONF_ACCOUNT, DOMAIN, SIGNAL_CONNECTION_STATE

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SignalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the connectivity sensor."""
    async_add_entities([SignalConnectionSensor(entry)])


class SignalConnectionSensor(BinarySensorEntity):
    """On while the WebSocket JSON-RPC session to WsRpcServer is alive."""

    _attr_has_entity_name = True
    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: SignalConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_connection"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Signal {entry.data[CONF_ACCOUNT]}",
            manufacturer="SignalCliNet.WsRpcServer",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        """Connection state."""
        return self._entry.runtime_data.client.connected

    async def async_added_to_hass(self) -> None:
        """Follow connection state changes."""

        @callback
        def _update(_: bool) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CONNECTION_STATE.format(self._entry.entry_id), _update
            )
        )
