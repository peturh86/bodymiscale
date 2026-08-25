"""Button entities for BodyMiScale."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, HANDLERS
from .entity import BodyScaleBaseEntity
from .history import async_import_bluetooth_scale_history
from .metrics import BodyScaleMetricsHandler

_LOGGER = logging.getLogger(__name__)

_IMPORT_HISTORY_DESCRIPTION = ButtonEntityDescription(
    key="import_bluetooth_scale_history",
    name="Import Bluetooth Scale history",
    icon="mdi:database-import",
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BodyMiScale buttons."""
    handler: BodyScaleMetricsHandler = hass.data[DOMAIN][HANDLERS][
        config_entry.entry_id
    ]
    async_add_entities([BodyScaleHistoryImportButton(handler)])


class BodyScaleHistoryImportButton(BodyScaleBaseEntity, ButtonEntity):
    """Import the Bluetooth Scale ledger into HA long-term statistics."""

    entity_description = _IMPORT_HISTORY_DESCRIPTION

    def __init__(self, handler: BodyScaleMetricsHandler) -> None:
        super().__init__(handler, _IMPORT_HISTORY_DESCRIPTION)

    async def async_press(self) -> None:
        """Import history across all currently loaded BodyMiScale profiles."""
        result = await async_import_bluetooth_scale_history(self.hass)
        _LOGGER.info("BodyMiScale historical import completed: %s", result)
