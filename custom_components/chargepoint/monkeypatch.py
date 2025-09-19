from __future__ import annotations
import logging

_LOGGER = logging.getLogger(__name__)
_scraper = None  # construit une seule fois

def _build_scraper():
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    })
    return s

def _load_cookies_into(scraper):
    try:
        from .cookies import load_cookies
        jar = load_cookies()
        if jar:
            scraper.cookies.update(jar)
            _LOGGER.warning("ChargePoint: cookies pré-authentifiés chargés.")
    except Exception as e:
        _LOGGER.debug("ChargePoint: pas de cookies pré-authentifiés (%s)", e)

async def ensure_scraper(hass):
    """À appeler au setup: construit le scraper dans l'executor."""
    global _scraper
    if _scraper is None:
        _LOGGER.warning("ChargePoint: création du scraper en executor…")
        _scraper = await hass.async_add_executor_job(_build_scraper)
        _load_cookies_into(_scraper)

def apply_scoped_patch():
    """
    Patch uniquement la classe ChargePoint pour :
      - injecter notre scraper dans self._session
      - skipper login() si des cookies pré-authentifiés existent
    """
    global _scraper
    if _scraper is None:
        raise RuntimeError("ChargePoint: scraper non initialisé (ensure_scraper manquant)")

    import python_chargepoint.client as cpc
    from .cookies import load_cookies

    # Sauvegarde du login original
    _orig_login = cpc.ChargePoint.login

    def _patched_login(self, username, password):
        # 1) Injecter notre scraper dans l'instance AVANT toute requête
        try:
            self._session = _scraper  # la lib utilise self._session pour ses calls
        except Exception:
            pass

        # 2) Si cookies présents → on évite l'endpoint de login (anti-bot)
        jar = load_cookies()
        if jar:
            _LOGGER.warning("ChargePoint: cookies présents → skip login().")
            # s'assurer que le scraper a aussi ces cookies
            try:
                _scraper.cookies.update(jar)
            except Exception:
                pass
            return True

        # 3) Sinon on appelle le login original (qui utilisera now self._session = _scraper)
        return _orig_login(self, username, password)

    # Appliquer notre login patché
    cpc.ChargePoint.login = _patched_login
