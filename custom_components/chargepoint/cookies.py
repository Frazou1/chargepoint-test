# custom_components/chargepoint/cookies.py
from __future__ import annotations
import json
from http.cookiejar import Cookie
from requests.cookies import RequestsCookieJar

COOKIES_PATH = "/config/chargepoint_cookies.json"

def _load_cookies_from_disk() -> list[dict]:
    with open(COOKIES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def _add_cookie(jar: RequestsCookieJar, name: str, value: str, domain: str, path: str = "/"):
    jar.set(name, value, domain=domain, path=path)

def load_cookies(hass) -> RequestsCookieJar | None:
    """Lire /config/chargepoint_cookies.json et créer un jar pour plusieurs domaines CP."""
    try:
        data = hass.loop.run_in_executor(None, _load_cookies_from_disk).result()  # évite I/O bloquant
    except Exception:
        # fallback synchrone si l’executor n’est pas dispo à ce moment (rare au setup)
        try:
            data = _load_cookies_from_disk()
        except Exception:
            return None

    if not isinstance(data, list) or not data:
        return None

    jar = RequestsCookieJar()
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
        # Ajoute le cookie sur tous les domaines utiles
        for d in domains:
            _add_cookie(jar, name, value, d)
    return jar
