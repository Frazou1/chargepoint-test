"""Config flow for ChargePoint (token-only)."""

from __future__ import annotations
import logging
from collections import OrderedDict
from typing import Any, Mapping

import voluptuous as vol
from homeassistant.config_entries import (
    CONN_CLASS_CLOUD_POLL,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    FlowResult,
)
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from .const import (
    DOMAIN,
    OPTION_POLL_INTERVAL,
    POLL_INTERVAL_DEFAULT,
    POLL_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)


def _token_schema(token: str = "") -> vol.Schema:
    # Un simple champ texte (multiligne pour faciliter le collage)
    return vol.Schema(
        OrderedDict(
            [
                (
                    vol.Required(CONF_ACCESS_TOKEN, default=token),
                    selector({"text": {"multiline": True}}),
                ),
            ]
        )
    )


def _options_schema(poll_interval: int | str = POLL_INTERVAL_DEFAULT) -> vol.Schema:
    return vol.Schema(
        OrderedDict(
            [
                (
                    vol.Required(OPTION_POLL_INTERVAL, default=str(poll_interval)),
                    selector(
                        {
                            "select": {
                                "mode": "dropdown",
                                "options": [
                                    {"label": k, "value": str(v)}
                                    for k, v in POLL_INTERVAL_OPTIONS.items()
                                ],
                            }
                        }
                    ),
                ),
            ]
        )
    )


class ChargePointFlowHandler(ConfigFlow, domain=DOMAIN):
    """Token-only config flow."""

    VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        token = ""

        if user_input is not None:
            token = (user_input.get(CONF_ACCESS_TOKEN) or "").strip()

            # Validation minimale: un JWT a 3 segments séparés par des points.
            if token.count(".") < 2:
                errors["base"] = "invalid_token_format"

            if not errors:
                # On utilise l’empreinte du token comme unique_id pour éviter les doublons
                unique_id = f"cp:{hash(token)}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title="ChargePoint (token)",
                    data={CONF_ACCESS_TOKEN: token},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_token_schema(token),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Réauth = resaisir token uniquement."""
        return await self.async_step_user()


class OptionsFlowHandler(OptionsFlow):
    """Options: uniquement l’intervalle de poll."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            poll_interval = int(user_input[OPTION_POLL_INTERVAL])
            if poll_interval not in POLL_INTERVAL_OPTIONS.values():
                return self.async_show_form(
                    step_id="init",
                    data_schema=_options_schema(poll_interval),
                    errors={"base": "invalid_poll_interval"},
                )
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                options={**self.config_entry.options, OPTION_POLL_INTERVAL: poll_interval},
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_abort(reason="options_successful")

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(
                self.config_entry.options.get(OPTION_POLL_INTERVAL, POLL_INTERVAL_DEFAULT)
            ),
        )
