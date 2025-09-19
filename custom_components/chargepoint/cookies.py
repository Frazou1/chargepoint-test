import json, os
from requests.cookies import RequestsCookieJar

COOKIES_PATH = "/config/chargepoint_cookies.json"

def load_cookies() -> RequestsCookieJar | None:
    if not os.path.exists(COOKIES_PATH):
        return None
    try:
        with open(COOKIES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        jar = RequestsCookieJar()
        for c in data:
            jar.set(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )
        return jar
    except Exception:
        return None
