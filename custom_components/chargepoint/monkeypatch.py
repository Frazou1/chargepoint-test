from __future__ import annotations
import logging
from typing import Optional

_LOGGER = logging.getLogger(__name__)
_scraper = None  # construit une seule fois


# ---------- construction du scraper ----------

def _build_scraper():
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/116.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Origin": "https://ca.chargepoint.com",
        "Referer": "https://ca.chargepoint.com/dashboard",
    })
    return s


async def ensure_scraper(hass):
    global _scraper
    if _scraper is None:
        _LOGGER.warning("ChargePoint: création du scraper en executor…")
        _scraper = await hass.async_add_executor_job(_build_scraper)


# ---------- marquage "autorisé" ----------

def _set_logged_flags(obj) -> None:
    for attr in ("_logged_in", "logged_in", "_is_logged_in", "_authenticated"):
        try:
            setattr(obj, attr, True)
        except Exception:
            pass


def mark_authorized(client, token: str, region_hint: str | None = None) -> None:
    """
    Prépare la session HTTP pour le mode token-only :
      - Authorization: Bearer <auth-session>
      - Cookies auth-session + coulomb_sess (si dispos dans le token/cookies locaux)
      - Origin/Referer en cohérence avec le sous-domaine (ex: ca.chargepoint.com)
    """
    import re
    from urllib.parse import urlparse

    s = getattr(client, "_session", None) or getattr(client, "session", None)
    if s is None:
        raise RuntimeError("ChargePoint: session HTTP introuvable sur le client")

    # ---------- Détection région ----------
    # Si tu as un cookie 'sub_domain=ca' on force le referer/origin vers ca.chargepoint.com
    origin = "https://www.chargepoint.com"
    referer = "https://www.chargepoint.com/"
    try:
        # si déjà chargés depuis /config/chargepoint_cookies.json
        for c in s.cookies:
            if c.name == "sub_domain" and c.value.lower() == "ca":
                origin = "https://ca.chargepoint.com"
                referer = "https://ca.chargepoint.com/"
                break
    except Exception:
        pass
    if region_hint and region_hint.lower() in ("ca", "canada"):
        origin = "https://ca.chargepoint.com"
        referer = "https://ca.chargepoint.com/"

    # ---------- Header d’autorisation ----------
    bearer = token.strip()
    # Beaucoup de gens collent directement la valeur du cookie auth-session : c’est OK.
    # S’ils collent "Bearer <jwt>", on nettoie pour ne garder que le JWT.
    if bearer.lower().startswith("bearer "):
        bearer = bearer[7:].strip()

    s.headers.update({
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": referer,
        # UA/Accept-Lang déjà posés à la création du scraper
        "X-Requested-With": "XMLHttpRequest",
    })

    # ---------- Cookies ----------
    # 1) auth-session = <JWT> (obligatoire côté web)
    try:
        s.cookies.set("auth-session", bearer, domain=".chargepoint.com", path="/")
    except Exception:
        pass

    # 2) coulomb_sess si on a déjà un jar (chargé depuis cookies.json)
    #    (pas obligatoire partout, mais améliore l’acceptation côté API web)
    has_coulomb = any(c.name == "coulomb_sess" for c in s.cookies)
    # Rien à faire si déjà présent. Si tu veux forcer manuellement :
    # s.cookies.set("coulomb_sess", "<valeur>", domain=".chargepoint.com", path="/")

    # ---------- Indicateurs internes du client ----------
    # Certains points de la lib vérifient ces flags/attributs.
    try:
        client._logged_in = True
    except Exception:
        pass
    # Session token non utilisé en token-only, mais on met une valeur neutre
    try:
        client.session_token = bearer
    except Exception:
        pass



# ---------- patch "token-only" (sans check_login) ----------

def apply_scoped_patch():
    """
    Sur certaines versions de python_chargepoint, il n'y a PAS d'attribut
    ChargePoint.check_login. On wrappe directement les méthodes publiques
    pour injecter le scraper + bypass implicite du login.
    """
    import functools
    import python_chargepoint.client as cpc  # type: ignore

    global _scraper
    if _scraper is None:
        raise RuntimeError("ChargePoint: scraper non initialisé (ensure_scraper manquant)")

    def _inject(self):
        # Injecter la session scraper
        try:
            if getattr(self, "_session", None) is not _scraper:
                self._session = _scraper  # type: ignore[attr-defined]
        except Exception:
            pass
        # Forcer "loggé"
        _set_logged_flags(self)

    targets = [
        "get_account",
        "get_user_charging_status",
        "get_home_chargers",
        "get_home_charger_status",
        "get_home_charger_technical_info",
        "get_charging_session",
        "login",  # on rend no-op
    ]

    for name in targets:
        if not hasattr(cpc.ChargePoint, name):
            continue

        orig = getattr(cpc.ChargePoint, name)
        base = getattr(orig, "__wrapped__", orig)  # si décoré via functools.wraps

        if name == "login":
            @functools.wraps(base)
            def _wrapped_login(self, *args, **kwargs):
                _inject(self)
                return True  # no-op
            setattr(cpc.ChargePoint, name, _wrapped_login)
            continue

        @functools.wraps(base)
        def _make_wrapper(fn):
            def _wrapped(self, *args, **kwargs):
                _inject(self)
                return fn(self, *args, **kwargs)
            return _wrapped

        setattr(cpc.ChargePoint, name, _make_wrapper(base))

    _LOGGER.debug("ChargePoint: méthodes patchées (token-only, sans check_login).")
