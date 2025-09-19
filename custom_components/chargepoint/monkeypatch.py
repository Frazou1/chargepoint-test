from __future__ import annotations
import logging
from typing import Optional

_LOGGER = logging.getLogger(__name__)
_scraper = None  # construit une seule fois


def _build_scraper():
    """Création synchrone du cloudscraper (à appeler dans un executor)."""
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/116 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        }
    )
    return s


def _has_auth_cookies() -> bool:
    """Détecte la présence de cookies utiles pour éviter le login."""
    global _scraper
    if _scraper is None:
        return False
    jar = _scraper.cookies
    wanted = {"auth-session", "coulomb_sess", "ci_ui_session", "datadome"}
    try:
        for c in jar:
            if c.name in wanted:
                return True
    except Exception:
        pass
    return False


async def ensure_scraper(hass):
    """À appeler avant toute utilisation: construit/charge le scraper SANS I/O bloquant."""
    global _scraper
    if _scraper is None:
        _LOGGER.warning("ChargePoint: création du scraper en executor…")
        _scraper = await hass.async_add_executor_job(_build_scraper)

        # Charger les cookies depuis /config via un executor (fonction async dans cookies.py)
        try:
            from .cookies import load_cookies  # version ASYNC
            jar = await load_cookies(hass)
            if jar:
                _scraper.cookies.update(jar)
                _LOGGER.warning("ChargePoint: cookies pré-authentifiés chargés.")
        except Exception:
            _LOGGER.debug(
                "ChargePoint: pas de cookies pré-authentifiés ou erreur de lecture",
                exc_info=True,
            )


def apply_scoped_patch():
    """
    Patch la classe ChargePoint pour :
      - injecter notre scraper dans self._session
      - skipper login() si des cookies pré-authentifiés sont déjà en mémoire
    NOTE: AUCUNE lecture de fichier ici (pas d'I/O bloquant).
    """
    global _scraper
    if _scraper is None:
        raise RuntimeError(
            "ChargePoint: scraper non initialisé (ensure_scraper manquant)"
        )

    import python_chargepoint.client as cpc

    # Sauvegarde du login original
    _orig_login = cpc.ChargePoint.login

    def _patched_login(self, username, password):
        # 1) Injecter notre scraper dans l'instance AVANT toute requête
        try:
            self._session = _scraper  # la lib utilise self._session pour ses calls
        except Exception:
            pass

        # 2) Si cookies d’auth présents → éviter l'endpoint de login (anti-bot)
        if _has_auth_cookies():
            _LOGGER.warning("ChargePoint: cookies présents → skip login().")
            return True

        # 3) Sinon on appelle le login original (qui utilisera self._session = _scraper)
        return _orig_login(self, username, password)

    # Appliquer notre login patché (une seule fois)
    if getattr(cpc.ChargePoint.login, "__name__", "") != "_patched_login":
        cpc.ChargePoint.login = _patched_login
