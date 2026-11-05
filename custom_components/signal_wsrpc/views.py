"""Short-lived QR image endpoint for the device-link step of the config flow."""

from __future__ import annotations

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN, QR_VIEW_URL


def qr_store(hass: HomeAssistant) -> dict[str, bytes]:
    """flow_id → PNG bytes (removed when the flow ends)."""
    return hass.data.setdefault(DOMAIN, {}).setdefault("qr", {})


class LinkQrView(HomeAssistantView):
    """Serves the link QR. Requires auth; the flow hands out a signed, 3-minute URL."""

    url = QR_VIEW_URL
    name = f"api:{DOMAIN}:link_qr"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, flow_id: str) -> web.Response:
        png = qr_store(self._hass).get(flow_id)
        if png is None:
            return web.Response(status=404)
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})
