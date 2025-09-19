from __future__ import annotations
import logging
_LOGGER = logging.getLogger(__name__)
_scraper = None

def _build_scraper():
    import cloudscraper
    s = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    })
    # pas d'Authorization en cookies-only
    s.headers.pop("Authorization", None)
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
    global _scraper
    if _scraper is None:
        _LOGGER.warning("ChargePoint: création du scraper en executor…")
        _scraper = await hass.async_add_executor_job(_build_scraper)
        _load_cookies_into(_scraper)

def apply_scoped_patch():
    global _scraper
    if _scraper is None:
        raise RuntimeError("ChargePoint: scraper non initialisé (ensure_scraper manquant)")
    import python_chargepoint.client as cpc
    _orig_login = cpc.ChargePoint.login
    def _patched_login(self, username, password):
        try:
            self._session = _scraper
        except Exception:
            pass
        # cookies présents → on évite toute auth active
        if _scraper and _scraper.cookies:
            _LOGGER.warning("ChargePoint: cookies présents → skip login().")
            # s’assurer d’aucun header Authorization
            try:
                self._session.headers.pop("Authorization", None)
            except Exception:
                pass
            return True
        return _orig_login(self, username, password)
    if getattr(cpc.ChargePoint.login, "__name__", "") != "_patched_login":
        cpc.ChargePoint.login = _patched_login
