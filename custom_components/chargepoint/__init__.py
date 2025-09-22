"""
Custom integration to integrate ChargePoint with Home Assistant (token-only).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from python_chargepoint import ChargePoint
from python_chargepoint.exceptions import (
    ChargePointBaseException,
    ChargePointCommunicationException,
    ChargePointInvalidSession,
)
from python_chargepoint.session import ChargingSession
from python_chargepoint.types import (
    ChargePointAccount,
    HomeChargerStatus,
    HomeChargerTechnicalInfo,
    UserChargingStatus,
)

from .const import (
    ACCT_CRG_STATUS,
    ACCT_HOME_CRGS,
    ACCT_INFO,
    ACCT_SESSION,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DOMAIN,
    ISSUE_URL,
    OPTION_POLL_INTERVAL,
    PLATFORMS,
    POLL_INTERVAL_DEFAULT,
    POLL_INTERVAL_OPTIONS,
    VERSION,
)
from . import monkeypatch

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER: logging.Logger = logging.getLogger(__package__)


async def async_setup(hass: HomeAssistant, entry: ConfigEntry):
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Setup with bearer token only."""
    await monkeypatch.ensure_scraper(hass)
    monkeypatch.apply_scoped_patch()

    _LOGGER.info(
        "Version %s starting (token-only). Report issues: %s",
        VERSION,
        ISSUE_URL,
    )

    token: str = (entry.data.get(CONF_ACCESS_TOKEN) or "").strip()
    if not token:
        raise ConfigEntryAuthFailed("empty_token")

    try:
        # Signatures récentes: ChargePoint(username, password, session_token)
        client: ChargePoint = await hass.async_add_executor_job(
            ChargePoint, "", "", token
        )
        # Injecter le header Authorization + marquer comme "auth"
        monkeypatch.mark_authorized(client, token)

    except ChargePointBaseException as exc:
        _LOGGER.error("Failed to initialize ChargePoint client: %s", exc)
        raise ConfigEntryNotReady from exc
    except Exception as exc:
        _LOGGER.exception("Unexpected error creating ChargePoint client")
        raise ConfigEntryNotReady from exc

    hass.data.setdefault(DOMAIN, {})

    async def async_update_data(is_retry: bool = False):
        """Fetch data from ChargePoint API (no relogin; token-only)."""
        data = {
            ACCT_INFO: None,
            ACCT_CRG_STATUS: None,
            ACCT_SESSION: None,
            ACCT_HOME_CRGS: {},
        }
        try:
            account: ChargePointAccount = await hass.async_add_executor_job(
                client.get_account
            )
            data[ACCT_INFO] = account

            crg_status: Optional[UserChargingStatus] = (
                await hass.async_add_executor_job(client.get_user_charging_status)
            )
            data[ACCT_CRG_STATUS] = crg_status

            if crg_status:
                crg_session: ChargingSession = await hass.async_add_executor_job(
                    client.get_charging_session, crg_status.session_id
                )
                data[ACCT_SESSION] = crg_session

            # --- Home chargers (robuste aux réponses partielles) ---
            try:
                home_chargers: list = await hass.async_add_executor_job(
                    client.get_home_chargers
                )
            except Exception as e:
                _LOGGER.warning(
                    "get_home_chargers a échoué (%s) → on continue sans bornes",
                    e,
                )
                home_chargers = []

            for charger in home_chargers or []:
                try:
                    hcrg_status: HomeChargerStatus = await hass.async_add_executor_job(
                        client.get_home_charger_status, charger
                    )
                    hcrg_tech_info: HomeChargerTechnicalInfo = (
                        await hass.async_add_executor_job(
                            client.get_home_charger_technical_info, charger
                        )
                    )
                    data[ACCT_HOME_CRGS][charger] = (hcrg_status, hcrg_tech_info)
                except Exception as e:
                    _LOGGER.warning(
                        "Impossible de récupérer les infos de la borne %s (%s)",
                        charger,
                        e,
                    )

            return data

        except ChargePointInvalidSession as exc:
            # Pas de relogin en token-only : on remonte une réauth
            _LOGGER.error("Invalid/expired token for ChargePoint")
            raise ConfigEntryAuthFailed("invalid_token") from exc

        except ChargePointCommunicationException as err:
            _LOGGER.error("Failed to update ChargePoint state")
            raise UpdateFailed from err

    poll_interval = entry.options.get(OPTION_POLL_INTERVAL, POLL_INTERVAL_DEFAULT)
    if poll_interval not in POLL_INTERVAL_OPTIONS.values():
        _LOGGER.warning(
            "Invalid poll interval %s, using default %s",
            poll_interval,
            POLL_INTERVAL_DEFAULT,
        )
        poll_interval = POLL_INTERVAL_DEFAULT

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update_data,
        update_interval=timedelta(seconds=poll_interval),
    )

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
    }

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


class ChargePointEntity(CoordinatorEntity):
    """Base ChargePoint Entity"""

    def __init__(self, client, coordinator):
        super().__init__(coordinator)
        self.client = client

    @property
    def account(self) -> ChargePointAccount:
        return self.coordinator.data[ACCT_INFO]

    @property
    def charging_status(self) -> UserChargingStatus:
        return self.coordinator.data[ACCT_CRG_STATUS]


class ChargePointChargerEntity(CoordinatorEntity):
    """Base ChargePoint Charger Entity"""

    def __init__(
        self, client: ChargePoint, coordinator: DataUpdateCoordinator, charger_id: int
    ):
        super().__init__(coordinator)
        self.client = client
        self.charger_id = charger_id
        self.manufacturer = (
            "ChargePoint"
            if self.charger_status.brand == "CP"
            else self.charger_status.brand
        )
        self.short_charger_model = self.charger_status.model.split("-")[0]

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(self.charger_id))},
            manufacturer=self.manufacturer,
            model=self.charger_status.model,
            name=(
                f"{self.manufacturer} Home Flex ({self.short_charger_model})"
                if "CPH" in self.short_charger_model
                else f"{self.manufacturer} {self.short_charger_model}"
            ),
            sw_version=self.technical_info.software_version,
        )

    @property
    def charger_status(self) -> HomeChargerStatus:
        return self.coordinator.data[ACCT_HOME_CRGS][self.charger_id][0]

    @property
    def technical_info(self) -> HomeChargerTechnicalInfo:
        return self.coordinator.data[ACCT_HOME_CRGS][self.charger_id][1]

    @property
    def session(self) -> Optional[ChargingSession]:
        session: ChargingSession = self.coordinator.data[ACCT_SESSION]
        if not session:
            return
        if session.device_id == self.charger_id:
            return self.coordinator.data[ACCT_SESSION]

    @session.setter
    def session(self, new_session: Optional[ChargingSession]):
        self.coordinator.data[ACCT_SESSION] = new_session


@dataclass
class ChargePointEntityRequiredKeysMixin:
    name_suffix: str
