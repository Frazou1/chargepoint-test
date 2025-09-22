from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)
_scraper = None  # construit une seule fois


def _build_scraper():
    import cloudscraper

    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    # En-têtes "desktop" réalistes + préférence régionale (CA)
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/116 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
            "Origin": "https://ca.chargepoint.com",
            "Referer": "https://ca.chargepoint.com/",
        }
    )
    return s


async def ensure_scraper(hass):
    """À appeler au setup: construit le scraper dans l'executor (pas d'I/O sync)."""
    global _scraper
    if _scraper is None:
        _LOGGER.warning("ChargePoint: création du scraper en executor…")
        _scraper = await hass.async_add_executor_job(_build_scraper)


def mark_authorized(client, token: str):
    """
    Injecte le scraper + le bearer token dans l'instance client.
    Marque aussi l'objet comme "authentifié" (flags courants).
    """
    global _scraper
    if _scraper is None:
        raise RuntimeError("ChargePoint: scraper non initialisé (ensure_scraper manquant)")

    # Force l'utilisation de notre session
    try:
        client._session = _scraper  # la lib utilise self._session pour ses calls
    except Exception:
        pass

    # Bearer token + variantes de flags utilisés par la lib
    try:
        client.session_token = token
    except Exception:
        pass
    for attr in ("_logged_in", "logged_in", "is_logged_in"):
        try:
            setattr(client, attr, True)
        except Exception:
            pass

    # Ajout du header Authorization
    try:
        _scraper.headers["Authorization"] = f"Bearer {token}"
    except Exception:
        pass


def _wrap_home_chargers(cpc):
    """Rend get_home_chargers tolérant aux variations de JSON."""
    orig = cpc.ChargePoint.get_home_chargers

    def _safe(self, *args, **kwargs):
        try:
            return orig(self, *args, **kwargs)
        except KeyError:
            # La lib attend response.json()["get_pandas"]["device_ids"]
            # Certains envs renvoient get_pandas sans 'device_ids' ou autre schéma.
            _LOGGER.warning(
                "Schéma get_home_chargers sans 'device_ids' → retourne []."
            )
            return []
        except Exception as e:
            _LOGGER.warning("get_home_chargers a levé %s → retourne []", e)
            return []

    cpc.ChargePoint.get_home_chargers = _safe


def _wrap_method(cpc, method_name: str):
    """
    Wrapper léger pour log/robustesse; NE contourne PAS le décorateur check_login.
    Utile pour ajouter des logs si nécessaire.
    """
    orig = getattr(cpc.ChargePoint, method_name, None)
    if orig is None:
        return

    def _wrapped(self, *args, **kwargs):
        try:
            return orig(self, *args, **kwargs)
        except Exception as e:
            _LOGGER.debug("%s a levé %s", method_name, e)
            raise

    setattr(cpc.ChargePoint, method_name, _wrapped)


def apply_scoped_patch():
    """
    Patchs "token-only":
      - assure que notre cloudscraper est utilisé par le client
      - ajoute le wrapper tolérant sur get_home_chargers (évite KeyError 'device_ids')
      - wrappers légers pour debug (facultatif)
    """
    import python_chargepoint.client as cpc

    # Wrappers debug simples (facultatifs)
    for name in (
        "get_account",
        "get_user_charging_status",
        "get_charging_session",
        "get_home_charger_status",
        "get_home_charger_technical_info",
    ):
        _wrap_method(cpc, name)

    # Patch robuste pour la liste des bornes
    _wrap_home_chargers(cpc)

    _LOGGER.debug("ChargePoint: méthodes patchées (token-only, sans check_login).")
