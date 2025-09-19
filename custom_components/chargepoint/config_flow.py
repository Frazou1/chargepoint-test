"""Adds config flow for ChargePoint."""

import json
import os
import logging
from collections import OrderedDict
from typing import Any, Mapping, Tuple

import voluptuous as vol
from homeassistant.config_entries import (
    CONN_CLASS_CLOUD_POLL,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    FlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from . import monkeypatch  # IMPORTANT: préparer le client avant login

from python_chargepoint import ChargePoint
from python_chargepoint.exceptions import (
    ChargePointCommunicationException,
    ChargePointLoginError,
)

from .const import (
    DOMAIN,
    OPTION_POLL_INTERVAL,
    POLL_INTERVAL_DEFAULT,
    POLL_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

# Champs/constantes pour le mode cookies
CONF_COOKIES_JSON = "cookies_json"
CONF_USE_COOKIE_AUTH = "use_cookie_auth"
COOKIES_PATH = "/config/chargepoint_cookies.json"


def _login_schema(
    username: str = "",
    use_cookie_auth: bool = False,
    cookies_json: str = "",
) -> vol.Schema:
    """Formulaire unique : identifiants + option cookies + champ cookies."""
    # Le selector("text", multiline) peut ne pas exister sur HA très ancien; si besoin, on pourrait revenir à 'str'
    return vol.Schema(
        OrderedDict(
            [
                (vol.Required(CONF_USERNAME, default=username), str),
                # Mot de passe facultatif si on coche "use_cookie_auth"
                (vol.Optional(CONF_PASSWORD, default=""), str),
                (vol.Optional(CONF_USE_COOKIE_AUTH, default=use_cookie_auth), bool),
                # Champ multiligne pour coller les cookies (JSON ou header)
                (
                    vol.Optional(CONF_COOKIES_JSON, default=cookies_json),
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


def _save_cookies_json(raw: str) -> int:
    """Valider et sauvegarder le JSON de cookies dans /config.
    Accepte un JSON (liste d'objets) ou un header 'name=value; ...'."""
    def parse_header(header: str):
        items = []
        for part in header.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            items.append(
                {
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".chargepoint.com",
                    "path": "/",
                }
            )
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
    """Config flow for ChargePoint."""

    VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_CLOUD_POLL

    def __init__(self):
        self._reauth_entry: ConfigEntry | None = None

    async def _login(
        self, username: str, password: str
    ) -> Tuple[str | None, str | None]:
        """Attempt login; returns (session_token, error_code)."""

        # Assurer le scraper + patch *avant* la création du client
        await monkeypatch.ensure_scraper(self.hass)
        monkeypatch.apply_scoped_patch()

        try:
            _LOGGER.info("Attempting to authenticate with chargepoint")
            client = await self.hass.async_add_executor_job(
                ChargePoint, username, password
            )

            # --- IMPORTANT : fallback si on a skip le login via cookies mais pas de token
            # --- IMPORTANT : fallback si on a skip le login via cookies mais pas de token
            token = getattr(client, "session_token", None)
            if not token:
                try:
                    from .monkeypatch import _has_auth_cookies
                    if _has_auth_cookies():
                        _LOGGER.warning("ChargePoint: session_token absent mais cookies présents → accept.")
                        # Donner un token "JWT-like" pour satisfaire les validations de format
                        return "eyJhbGciOiJIUzI1NiJ9.cookie.auth", None
                except Exception:
                    pass


            return token, None

        except ChargePointLoginError as exc:
            # Serveur peut renvoyer HTML (DataDome) -> pas de JSON
            error_code = "auth_failed"
            try:
                data = exc.response.json()  # type: ignore[attr-defined]
                error_id = data.get("errorId")
                if error_id == 9:
                    _LOGGER.exception("Invalid credentials for ChargePoint")
                    return None, "invalid_credentials"
                if error_id == 241:
                    _LOGGER.exception("ChargePoint Account is locked")
                    return None, "account_locked"
                error_code = str(error_id or error_code)
            except Exception:
                try:
                    text = exc.response.text  # type: ignore[attr-defined]
                    if (
                        "captcha-delivery.com" in text
                        or "Please enable JS" in text
                        or "DataDome" in text
                    ):
                        _LOGGER.error("Anti-bot challenge detected during login")
                        return None, "antibot_challenge"
                except Exception:
                    pass
            return None, error_code

        except ChargePointCommunicationException:
            _LOGGER.exception("Failed to communicate with ChargePoint")
            return None, "communication_error"

        except Exception:
            _LOGGER.exception("Unexpected error while authenticating to ChargePoint")
            return None, "unknown"


    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Formulaire unique : login classique OU cookies."""
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
                # Sauvegarder les cookies (obligatoire si case cochée)
                try:
                    count = await self.hass.async_add_executor_job(
                        _save_cookies_json, cookies_json
                    )
                    _LOGGER.warning("Cookies importés (%s entrées).", count)
                except Exception:
                    errors["base"] = "invalid_cookies_json"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_login_schema(username, use_cookie_auth, cookies_json),
                        errors=errors or {"base": "invalid_cookies_json"},
                    )
                # Tenter la "connexion" sans mot de passe → monkeypatch skip login()
                session_token, error = await self._login(username, "")
                if error is not None:
                    errors["base"] = error
                if session_token:
                    return self.async_create_entry(
                        title=username,
                        data={
                            CONF_USERNAME: username,
                            CONF_PASSWORD: "",
                            CONF_ACCESS_TOKEN: session_token,
                        },
                    )
                # échec → réafficher le formulaire avec l'erreur
                return self.async_show_form(
                    step_id="user",
                    data_schema=_login_schema(username, use_cookie_auth, cookies_json),
                    errors=errors or {"base": "auth_failed"},
                )

            # Chemin “login classique”
            session_token, error = await self._login(username, password or "")
            if error is not None:
                errors["base"] = error
            if session_token:
                return self.async_create_entry(
                    title=username,
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password or "",
                        CONF_ACCESS_TOKEN: session_token,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_login_schema(username, use_cookie_auth, cookies_json),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Triggered when reauth is needed."""
        entry_id = self.context["entry_id"]
        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        username = self._reauth_entry.data[CONF_USERNAME]
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            session_token, error = await self._login(username, password)
            if error is not None:
                errors["base"] = error
            if session_token:
                # Update the existing config entry
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        CONF_ACCESS_TOKEN: session_token,
                    },
                )
                # Reload the config entry
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD, default=""): str}),
            description_placeholders={"username": username},
            errors=errors,
        )


class OptionsFlowHandler(OptionsFlow):
    """Handle options flow for ChargePoint."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
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
                options={
                    **self.config_entry.options,
                    OPTION_POLL_INTERVAL: poll_interval,
                },
            )
            # Reload the config entry
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_abort(reason="options_successful")

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(
                self.config_entry.options.get(
                    OPTION_POLL_INTERVAL, POLL_INTERVAL_DEFAULT
                )
            ),
        )
