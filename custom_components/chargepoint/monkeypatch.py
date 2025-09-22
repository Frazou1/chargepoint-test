# custom_components/chargepoint/monkeypatch.py
from __future__ import annotations
import json
import logging
from typing import Iterable
from requests.cookies import RequestsCookieJar

_LOGGER = logging.getLogger(__name__)

COOKIES_PATH = "/config/chargepoint_cookies.json"
_scraper = None  # construit une seule fois

# ---------- util cookies ----------

def _parse_cookie_header(header: str) -> list[dict]:
    items: list[dict] = []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        items.append({"name": name.strip(), "value": value.strip()})
    return items

def _to_cookiejar(raw: Iterable[dict] | str) -> RequestsCookieJar:
    """Accepte une liste d'objets {name,value[,domain,path]} OU un header 'a=b; c=d'."""
    if isinstance(raw, str):
        data = _parse_cookie_header(raw)
    else:
        data = list(raw or [])
    jar = RequestsCookieJar()
    if not data:
        return jar

    # Domaines probables utilisés par ChargePoint
    domains = [
        ".chargepoint.com",
        "chargepoint.com",
        "www.chargepoint.com",
        "account.chargepoint.com",
        "ca.chargepoint.com",
    ]
    for item in data:
        name = item.get("name")
        value = item.get("value")
        if not name or value is None:
            continue
        # si le JSON inclut domain/path on les respecte, sinon on duplique
        dom = item.get("domain")
        path = item.get("path", "/")
        if dom:
            jar.set(name, value, domain=dom, path=path)
        else:
            for d in domains:
                jar.set(name, value, domain=d, path=path)
    return jar

def _read_cookie_file() -> RequestsCookieJar | None:
    try:
        with open(COOKIES_PATH, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        if not txt:
            return None
        raw = json.loads(txt) if txt.startswith("[") else txt  # liste JSON OU header
        return _to_cookiejar(raw)
    except FileNotFoundError:
        _LOGGER.debug("ChargePoint: fichier cookies introuvable: %s", COOKIES_PATH)
        return None
    except Exception as e:
        _LOGGER.debug("ChargePoint: lecture cookies a échoué: %s", e)
        return None

# ---------- scraper / patch ----------

def _build_scraper():
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    # Headers réalistes
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/116 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Origin": "https://ca.chargepoint.com",
        "Referer": "https://ca.chargepoint.com/drivers/home",
        "X-Requested-With": "XMLHttpRequest",
    })
    s.headers.pop("Authorization", None)
    return s

async def ensure_scraper(hass):
    """Construit le scraper et (ré)injecte les cookies depuis /config/… dans l'executor."""
    global _scraper
    if _scraper is None:
        _LOGGER.warning("ChargePoint: création du scraper en executor…")
        _scraper = await hass.async_add_executor_job(_build_scraper)

    def load_jar_sync():
        return _read_cookie_file()

    jar = await hass.async_add_executor_job(load_jar_sync)
    if jar and len(jar) > 0:
        before = len(_scraper.cookies)
        _scraper.cookies.update(jar)
        after = len(_scraper.cookies)
        # pour debug : affiche quelques noms importants si présents
        has_dd = any(c.name.lower() == "datadome" for c in _scraper.cookies)
        has_auth = any(c.name.lower() in ("auth-session", "coulomb_sess") for c in _scraper.cookies)
        _LOGGER.warning(
            "ChargePoint: cookies chargés (%s→%s). datadome=%s auth-session/coulomb_sess=%s",
            before, after, has_dd, has_auth
        )
    else:
        _LOGGER.debug("ChargePoint: aucun cookie chargé depuis %s", COOKIES_PATH)

def apply_scoped_patch():
    """
    Force la lib à utiliser notre session dès __init__ et
    skip login() si des cookies sont présents.
    """
    global _scraper
    if _scraper is None:
        raise RuntimeError("ChargePoint: scraper non initialisé (ensure_scraper manquant)")

    import python_chargepoint.client as cpc

    _orig_init = cpc.ChargePoint.__init__
    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        try:
            self._session = _scraper
            self._session.headers.pop("Authorization", None)
        except Exception:
            pass

    _orig_login = cpc.ChargePoint.login
    def _patched_login(self, username, password):
        try:
            self._session = _scraper
            self._session.headers.pop("Authorization", None)
        except Exception:
            pass
        # Si on a des cookies, on évite l'endpoint de login (DataDome)
        if _scraper and _scraper.cookies and len(_scraper.cookies) > 0:
            _LOGGER.warning("ChargePoint: cookies présents → skip login().")
            return True
        return _orig_login(self, username, password)

    if getattr(cpc.ChargePoint.__init__, "__name__", "") != "_patched_init":
        cpc.ChargePoint.__init__ = _patched_init
    if getattr(cpc.ChargePoint.login, "__name__", "") != "_patched_login":
        cpc.ChargePoint.login = _patched_login
