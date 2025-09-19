"""
Custom integration to integrate ChargePoint with Home Assistant.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_PASSWORD, CONF_USERNAME
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
    ChargePointLoginError,
)
from python_chargepoint.session import ChargingSession
from python_chargepoint.types import (
    ChargePointAccount,
    HomeChargerStatus,
    HomeChargerTechnicalInfo,
    UserChargingStatus,
)

from . import monkeypatch
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
    TOKEN_FILE_NAME,
    VERSION,
)

# Optionnel : flag posé par le config_flow quand on choisit l’auth par cookies
try:
    from .const import CONF_COOKIE_AUTH  # ajouté dans const.py
except Exception:  # retro-compat si pas présent
    CONF_COOKIE_AUTH = "cookie_auth"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER: logging.Logger = logging.getLogger(__package__)

# Regex très permissive “JWT-like” (3 segments base64url)
_JWT_RE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$")


def remove_session_token_from_disk(hass: HomeAssistant) -> None:
    config_dir = hass.config.config_dir
    file = os.path.join(config_dir, TOKEN_FILE_NAME)
    if os.path.isfile(file):
        os.remove(file)


async def async_setup(hass: HomeAssistant, entry: ConfigEntry):
    """Disallow configuration via YAML"""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Load the saved entities."""
    # Préparer le scraper/patch AVANT de créer le client
    await monkeypatch.ensure_scraper(hass)
    monkeypatch.apply_scoped_patch()

    _LOGGER.info(
        "Version %s is starting, if you have any issues please report them here: %s",
        VERSION,
        ISSUE_URL,
    )

    username = entry.data.get(CONF_USERNAME, "")
    password = entry.data.get(CONF_PASSWORD, "")
    stored_token = entry.data.get(CONF_ACCESS_TOKEN, "") or ""
    use_cookie_auth = bool(entry.data.get(CONF_COOKIE_AUTH, False))

    # Nettoyage ancien fichier token
    await hass.async_add_executor_job(remove_session_token_from_disk, hass)

    # Déterminer quel token passer au constructeur:
    # - en mode cookies, on prend le JWT réel du cookie `auth-session` si présent;
    #   sinon on ne passe **aucun** token (None) pour éviter l'erreur de format.
    # - hors cookies, on ne passe le token que s'il “ressemble” à un JWT.
    token_to_pass: Optional[str] = None

    def _jwt_from_cookie() -> Optional[str]:
        try:
            if monkeypatch._scraper is None:
                return None
            for c in monkeypatch._scraper.cookies:
                if c.name == "auth-session" and c.value:
                    return c.value
        except Exception:
            pass
        return None

    if use_cookie_auth:
        cookie_jwt = _jwt_from_cookie()
        if cookie_jwt and _JWT_RE.match(cookie_jwt):
            token_to_pass = cookie_jwt
        else:
            token_to_pass = None  # pas de token → le client utilisera les cookies
    else:
        if stored_token and _JWT_RE.match(stored_token):
            token_to_pass = stored_token
        else:
            # Ancien chemin sans cookies → si format invalide, on échouera plus bas proprement
            token_to_pass = None

    try:
        # Important: ne passe PAS de token invalide → sinon la lib lève "Invalid session token format"
        client: ChargePoint = await hass.async_add_executor_job(
            ChargePoint, username, password, token_to_pass
        )

        # En mode cookies, si on a un JWT côté cookie mais que le client n'a rien,
        # on le renseigne (la lib n'en a pas besoin quand on a les cookies, mais ça rassure le reste du code).
        if use_cookie_auth:
            jwt_now = _jwt_from_cookie()
            if jwt_now and _JWT_RE.match(jwt_now):
                try:
                    client.session_token = jwt_now
                except Exception:
                    pass

        # Si la lib a rafraîchi le token et qu'il est bien “JWT-like”, on le persiste.
        if getattr(client, "session_token", None) and _JWT_RE.match(client.session_token):
            if client.session_token != stored_token:
                _LOGGER.debug("Session token refreshed by client, updating config entry")
                hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_ACCESS_TOKEN: client.session_token,
                    },
                )

    except ChargePointLoginError as exc:
        # En mode cookies-only, on ne devrait jamais tenter un vrai login; s'il arrive,
        # c'est que les cookies ne passent plus (datadome/expiration). On demande une reauth (nouveaux cookies).
        _LOGGER.error("Failed to authenticate to ChargePoint")
        raise ConfigEntryAuthFailed(exc) from exc

    except ChargePointBaseException as exc:
        _LOGGER.error("Unknown ChargePoint Error!")
        raise ConfigEntryNotReady from exc

    hass.data.setdefault(DOMAIN, {})

    async def async_update_data(is_retry: bool = False):
        """Fetch data from ChargePoint API."""
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
            _LOGGER.debug("Account information: %s", account)
            data[ACCT_INFO] = account

            crg_status: Optional[UserChargingStatus] = (
                await hass.async_add_executor_job(client.get_user_charging_status)
            )
            _LOGGER.debug("User charging status: %s", crg_status)
            data[ACCT_CRG_STATUS] = crg_status

            if crg_status:
                crg_session: ChargingSession = await hass.async_add_executor_job(
                    client.get_charging_session, crg_status.session_id
                )
                _LOGGER.debug("Charging session: %s", crg_session)
                data[ACCT_SESSION] = crg_session

            home_chargers: list = await hass.async_add_executor_job(
                client.get_home_chargers
            )
            for charger in home_chargers:
                hcrg_status: HomeChargerStatus = await hass.async_add_executor_job(
                    client.get_home_charger_status, charger
                )
                hcrg_tech_info: HomeChargerTechnicalInfo = (
                    await hass.async_add_executor_job(
                        client.get_home_charger_technical_info, charger
                    )
                )
                data[ACCT_HOME_CRGS][charger] = (hcrg_status, hcrg_tech_info)

            return data

        except ChargePointInvalidSession:
            # Si le token est invalide:
            if use_cookie_auth:
                # En mode cookies, on NE tente PAS de relogin (bloqué par l'anti-bot).
                # On force une reauth côté HA (l’utilisateur devra recoller des cookies frais).
                _LOGGER.error(
                    "ChargePoint cookie session expired/invalid. Please re-import cookies."
                )
                raise ConfigEntryAuthFailed("cookie_expired")
            else:
                if not is_retry:
                    _LOGGER.warning(
                        "ChargePoint Session Token is invalid, attempting to re-login"
                    )
                    try:
                        await hass.async_add_executor_job(client.login, username, password)
                        # Si la lib a remis un token, persiste-le
                        if getattr(client, "session_token", None) and _JWT_RE.match(
                            client.session_token
                        ):
                            hass.config_entries.async_update_entry(
                                entry,
                                data={
                                    **entry.data,
                                    CONF_ACCESS_TOKEN: client.session_token,
                                },
                            )
                        return await async_update_data(is_retry=True)
                    except ChargePointLoginError as exc:
                        _LOGGER.error("Failed to authenticate to ChargePoint")
                        raise ConfigEntryAuthFailed(exc) from exc
                # Si on était déjà en retry, on laisse tomber
                raise UpdateFailed("invalid session after retry")

        except ChargePointCommunicationException as err:
            _LOGGER.error("Failed to update ChargePoint State")
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

    # Fetch initial data so we have data when entities subscribe
    await coordinator.async_config_entry_first_refresh()

    # Setup components
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


class ChargePointEntity(CoordinatorEntity):
    """Base ChargePoint Entity"""

    def __init__(self, client, coordinator):
        """Initialize the ChargePoint entity."""
        super().__init__(coordinator)
        self.client = client

    @property
    def account(self) -> ChargePointAccount:
        """Shortcut to access account info for the entity."""
        return self.coordinator.data[ACCT_INFO]

    @property
    def charging_status(self) -> UserChargingStatus:
        """Shortcut to access charging status for the entity."""
        return self.coordinator.data[ACCT_CRG_STATUS]


class ChargePointChargerEntity(CoordinatorEntity):
    """Base ChargePoint Entity"""

    def __init__(
        self, client: ChargePoint, coordinator: DataUpdateCoordinator, charger_id: int
    ):
        """Initialize the ChargePoint entity."""
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
        _LOGGER.debug(
            "Session in progress, checking if home charger (%s): %s",
            self.charger_id,
            session,
        )
        if session.device_id == self.charger_id:
            return self.coordinator.data[ACCT_SESSION]

    @session.setter
    def session(self, new_session: Optional[ChargingSession]):
        self.coordinator.data[ACCT_SESSION] = new_session


@dataclass
class ChargePointEntityRequiredKeysMixin:
    """Mixin for required keys on all entities."""

    # Suffix to be appended to the entity name
    name_suffix: str
