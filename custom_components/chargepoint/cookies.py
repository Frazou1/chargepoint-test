import json
import logging
from typing import Optional
from requests.cookies import RequestsCookieJar

_LOGGER = logging.getLogger(__name__)

COOKIES_PATH = "/config/chargepoint_cookies.json"


def _read_cookies_file() -> list[dict]:
    with open(COOKIES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_cookiejar(objs: list[dict]) -> RequestsCookieJar:
    jar = RequestsCookieJar()
    for c in objs:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain") or ".chargepoint.com"
        path = c.get("path") or "/"
        if name is None or value is None:
            continue
        jar.set(name, value, domain=domain, path=path)
    return jar


def load_cookies_sync() -> Optional[RequestsCookieJar]:
    """Lecture **synchrone** (à appeler uniquement depuis un executor)."""
    try:
        objs = _read_cookies_file()
        if not isinstance(objs, list):
            _LOGGER.warning("cookies.json: format inattendu (pas une liste)")
            return None
        return _build_cookiejar(objs)
    except FileNotFoundError:
        return None
    except Exception as e:
        _LOGGER.warning("Impossible de charger les cookies: %s", e)
        return None


async def load_cookies(hass) -> Optional[RequestsCookieJar]:
    """Lecture **asynchrone** (non bloquante) via executor."""
    return await hass.async_add_executor_job(load_cookies_sync)
