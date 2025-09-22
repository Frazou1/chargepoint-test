from __future__ import annotations

import logging
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# Scraper global, construit une seule fois par instance HA
_scraper = None  # type: ignore[assignment]


# ---------- helpers: construction du scraper ----------

def _build_scraper():
    """
    Construit un cloudscraper prêt pour des endpoints protégés par Cloudflare/DataDome.
    Fait en executor par ensure_scraper() (pas d'I/O bloquante dans l'event loop).
    """
    import cloudscraper

    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

    # En-têtes "browser-like"
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/116.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            # Certains backends vérifient Origin/Referer pour les API privées
            "Origin": "https://ca.chargepoint.com",
            "Referer": "https://ca.chargepoint.com/dashboard",
        }
    )
    return s


async def ensure_scraper(hass):
    """
    Construit le scraper dans un thread executor (safe pour HA).
    À appeler au début (setup de l’intégration).
    """
    global _scraper
    if _scraper is None:
        _LOGGER.warning("ChargePoint: création du scraper en executor…")
        _scraper = await hass.async_add_executor_job(_build_scraper)


# ---------- marquage "autorisé" d'une instance ChargePoint ----------

def _set_logged_flags(obj) -> None:
    """
    Certaines versions de python_chargepoint utilisent des flags différents.
    On les force tous.
    """
    for attr in ("_logged_in", "logged_in", "_is_logged_in", "_authenticated"):
        try:
            setattr(obj, attr, True)
        except Exception:
            pass


def mark_authorized(client, token: Optional[str]) -> None:
    """
    Injecte le scraper global dans le client, pousse Authorization si fourni,
    et marque l'instance comme "loggée" pour contourner check_login().
    """
    global _scraper
    if _scraper is None:
        raise RuntimeError("ChargePoint: scraper non initialisé (ensure_scraper manquant)")

    # 1) Session HTTP
    try:
        client._session = _scraper  # type: ignore[attr-defined]
    except Exception:
        pass

    # 2) Headers Authorization
    try:
        # nettoie un éventuel header existant
        _scraper.headers.pop("Authorization", None)
    except Exception:
        pass

    if token:
        try:
            _scraper.headers["Authorization"] = f"Bearer {token}"
        except Exception:
            pass
        # certaines versions lisent encore `session_token`
        try:
            client.session_token = token  # type: ignore[attr-defined]
        except Exception:
            pass

    # 3) Flags login
    _set_logged_flags(client)


# ---------- patchage "soft" de la lib pour éviter Must login ----------

def apply_scoped_patch():
    """
    Patch minimal : on remplace ChargePoint.check_login par une version qui
    s’assure que la session & les flags sont OK et NE JETTE PAS d’exception.
    On ne touche ni requests.Session, ni le login() (pas besoin en token-only).
    """
    global _scraper
    if _scraper is None:
        raise RuntimeError("ChargePoint: scraper non initialisé (ensure_scraper manquant)")

    import python_chargepoint.client as cpc  # type: ignore

    # Sauvegarde du check_login original si besoin de debug
    _orig_check_login = getattr(cpc.ChargePoint, "check_login", None)

    def _patched_check_login(self, *args, **kwargs):
        # S'assurer que l'instance a bien notre scraper
        try:
            if getattr(self, "_session", None) is None:
                self._session = _scraper  # type: ignore[attr-defined]
        except Exception:
            pass

        # Si Authorization est présent ou session_token non vide → on considère "loggé"
        try:
            has_auth_header = "Authorization" in getattr(self, "_session", {}).headers  # type: ignore[union-attr]
        except Exception:
            has_auth_header = False

        has_token = False
        try:
            tok = getattr(self, "session_token", None)
            has_token = bool(tok)
        except Exception:
            pass

        if has_auth_header or has_token:
            _set_logged_flags(self)
            return True

        # Fallback: si l’intégration n’a pas encore injecté le token,
        # on ne lève pas d’exception pour ne pas casser la config page.
        _set_logged_flags(self)
        return True

    # Appliquer le patch une seule fois
    if getattr(cpc.ChargePoint.check_login, "__name__", "") != "_patched_check_login":
        cpc.ChargePoint.check_login = _patched_check_login  # type: ignore[assignment]
        _LOGGER.debug("ChargePoint: check_login() patché (token-only).")
