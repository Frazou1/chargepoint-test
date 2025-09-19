# custom_components/chargepoint/monkeypatch.py
from __future__ import annotations
import logging
_LOGGER = logging.getLogger(__name__)
_scraper = None  # construit une fois

def _build_scraper():
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    # Headers réalistes
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Origin": "https://ca.chargepoint.com",
        "Referer": "https://ca.chargepoint.com/drivers/home",
        "X-Requested-With": "XMLHttpRequest",
    })
    s.headers.pop("Authorization", None)
    return s

def _load_cookies_into(hass, scraper):
    try:
        from .cookies import load_cookies
        jar = load_cookies(hass)
        if jar:
            scraper.cookies.update(jar)
            _LOGGER.warning("ChargePoint: cookies pré-authentifiés chargés.")
        else:
            _LOGGER.debug("ChargePoint: aucun cookie chargé.")
    except Exception as e:
        _LOGGER.debug("ChargePoint: pas de cookies pré-authentifiés (%s)", e)

async def ensure_scraper(hass):
    """Construire le scraper dans l'executor et charger les cookies."""
    global _scraper
    if _scraper is None:
        _LOGGER.warning("ChargePoint: création du scraper en executor…")
        _scraper = await hass.async_add_executor_job(_build_scraper)
    # (Ré)injecte les cookies à chaque appel, au cas où ils viennent d'être importés
    _load_cookies_into(hass, _scraper)

def apply_scoped_patch():
    """
    Patch la classe ChargePoint pour :
      - forcer self._session = _scraper dès __init__
      - skipper login() si cookies présents
    """
    global _scraper
    if _scraper is None:
        raise RuntimeError("ChargePoint: scraper non initialisé (ensure_scraper manquant)")

    import python_chargepoint.client as cpc

    # 1) patch __init__ pour injecter notre session dès la construction
    _orig_init = cpc.ChargePoint.__init__

    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        try:
            self._session = _scraper
            # aucune auth header
            try:
                self._session.headers.pop("Authorization", None)
            except Exception:
                pass
        except Exception:
            pass

    # 2) patch login() pour l’ignorer si cookies présents
    _orig_login = cpc.ChargePoint.login

    def _patched_login(self, username, password):
        try:
            self._session = _scraper
            self._session.headers.pop("Authorization", None)
        except Exception:
            pass
        if _scraper and _scraper.cookies and len(_scraper.cookies) > 0:
            _LOGGER.warning("ChargePoint: cookies présents → skip login().")
            return True
        return _orig_login(self, username, password)

    # Appliquer une seule fois
    if getattr(cpc.ChargePoint.__init__, "__name__", "") != "_patched_init":
        cpc.ChargePoint.__init__ = _patched_init
    if getattr(cpc.ChargePoint.login, "__name__", "") != "_patched_login":
        cpc.ChargePoint.login = _patched_login
