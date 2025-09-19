"""Adds config flow for ChargePoint."""
from __future__ import annotations

import json
import os
import logging
from collections import OrderedDict
from typing import Any, Mapping

import voluptuous as vol
from homeassistant.config_entries import (
    CONN_CLASS_CLOUD_POLL,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    FlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from . import monkeypatch
from .const import (
    DOMAIN,
    OPTION_POLL_INTERVAL,
    POLL_INTERVAL_DEFAULT,
    POLL_INTERVAL_OPTIONS,
    CONF_COOKIE_AUTH,
)

_LOGGER = logging.getLogger(__name__)

CONF_COOKIES_JSON = "cookies_json"
CONF_USE_COOKIE_AUTH = "use_cookie_auth"
COOKIES_PATH = "/config/chargepoint_cookies.json"


def _login_schema(username: str = "", use_cookie_auth: bool = False, cookies_json: str = "") -> vol.Schema:
    return vol.Schema(
        OrderedDict(
            [
                (vol.Required(CONF_USERNAME, default=username), str),
                (vol.Optional(CONF_PASSWORD, default=""), str),  # ignoré si cookies
                (vol.Optional(CONF_USE_COOKIE_AUTH, default=use_cookie_auth), bool),
                (vol.Optional(CONF_COOKIES_JSON, default=cookies_json), selector({"text": {"multiline": True}})),
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
                        {"select": {"mode": "dropdown",
                                    "options": [{"label": k, "value": str(v)} for k, v in POLL_INTERVAL_OPTIONS.items()]}}
                    ),
                ),
            ]
        )
    )


def _save_cookies_json(raw: str) -> int:
    def parse_header(header: str):
        items = []
        for part in (header or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            items.append({"name": name.strip(), "value": value.strip(), "domain": ".chargepoint.com", "path": "/"})
        return items

    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Vide")

    if raw.startswith("["):
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("Le JSON doit être une liste d'objets cookie.")
    else:
        data = parse_header(raw)
        if not data:
            raise ValueError("Impossible de parser le header de cookies.")

    os.makedirs(os.path.dirname(COOKIES_PATH), exist_ok=True)
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return len(data)


class ChargePointFlowHandler(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_CLOUD_POLL

    def __init__(self):
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        username = ""
        errors: dict[str, str] = {}
        use_cookie_auth = False
        cookies_json = ""

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "")
            password = user_input.get(CONF_PASSWORD, "")
            use_cookie_auth = bool(user_input.get(CONF_USE_COOKIE_AUTH, False))
            cookies_json = user_input.get(CONF_COOKIES_JSON, "")

            await self.async_set_unique_id(username)
            self._abort_if_unique_id_configured()

            if use_cookie_auth:
                # 1) enregistrer cookies
                try:
                    count = await self.hass.async_add_executor_job(_save_cookies_json, cookies_json)
                    _LOGGER.warning("Cookies importés (%s entrées).", count)
                except Exception:
                    errors["base"] = "invalid_cookies_json"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_login_schema(username, use_cookie_auth, cookies_json),
                        errors=errors or {"base": "invalid_cookies_json"},
                    )

                # 2) préparer scraper/patch (chargera les cookies, pas de login)
                await monkeypatch.ensure_scraper(self.hass)
                monkeypatch.apply_scoped_patch()

                # 3) créer l’entrée cookies-only (pas de token sauvegardé)
                data = {
                    CONF_USERNAME: username,
                    CONF_PASSWORD: "",       # inutile en mode cookies
                    CONF_COOKIE_AUTH: True,  # flag pour __init__.py
                }
                return self.async_create_entry(title=username, data=data)

            # Si l’utilisateur n’a pas coché cookies, on refuse (intégration est cookies-only)
            errors["base"] = "antibot_challenge"
            return self.async_show_form(
                step_id="user",
                data_schema=_login_schema(username, True, cookies_json),
                errors=errors,
            )

        return self.async_show_form(step_id="user", data_schema=_login_schema(username, use_cookie_auth, cookies_json), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        entry_id = self.context["entry_id"]
        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # En mode cookies-only, la reauth consiste à recoller des cookies via le flow user
        return await self.async_step_user()


class OptionsFlowHandler(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            poll_interval = int(user_input[OPTION_POLL_INTERVAL])
            if poll_interval not in POLL_INTERVAL_OPTIONS.values():
                return self.async_show_form(step_id="init", data_schema=_options_schema(poll_interval), errors={"base": "invalid_poll_interval"})
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                options={**self.config_entry.options, OPTION_POLL_INTERVAL: poll_interval},
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_abort(reason="options_successful")

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(self.config_entry.options.get(OPTION_POLL_INTERVAL, POLL_INTERVAL_DEFAULT)),
        )
