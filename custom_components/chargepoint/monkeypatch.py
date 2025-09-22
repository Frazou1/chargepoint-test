def apply_scoped_patch():
    """
    Patch "token-only" compatible avec les versions où il n'y a PAS
    d'attribut ChargePoint.check_login.
    On remplace directement certaines méthodes publiques par des wrappers
    qui injectent le scraper + flags, puis appellent la version dédecorée
    (.__wrapped__) si disponible pour bypass le check de login.
    """
    import functools
    import python_chargepoint.client as cpc  # type: ignore

    global _scraper
    if _scraper is None:
        raise RuntimeError("ChargePoint: scraper non initialisé (ensure_scraper manquant)")

    # Méthodes que l’intégration appelle et qui sont typiquement décorées par "check_login"
    targets = [
        "get_account",
        "get_user_charging_status",
        "get_home_chargers",
        "get_home_charger_status",
        "get_home_charger_technical_info",
        "get_charging_session",
        # Optionnel: si jamais l’intégration essaie de "login", on rend no-op mais
        # on garde l’injection pour éviter tout appel réseau inutile.
        "login",
    ]

    def _inject(self):
        # Injecter la session scraper
        try:
            if getattr(self, "_session", None) is not _scraper:
                self._session = _scraper  # type: ignore[attr-defined]
        except Exception:
            pass
        # Forcer les flags "logged"
        for attr in ("_logged_in", "logged_in", "_is_logged_in", "_authenticated"):
            try:
                setattr(self, attr, True)
            except Exception:
                pass

    for name in targets:
        if not hasattr(cpc.ChargePoint, name):
            continue

        orig = getattr(cpc.ChargePoint, name)

        # Si le décorateur a utilisé functools.wraps, on récupère la vraie fonction
        base = getattr(orig, "__wrapped__", orig)

        # login → no-op (mais injection quand même)
        if name == "login":
            @functools.wraps(base)
            def _wrapped_login(self, *args, **kwargs):
                _inject(self)
                # no-op: on prétend que c'est ok
                return True
            setattr(cpc.ChargePoint, name, _wrapped_login)
            continue

        # Wrappers qui contournent le check et appellent la base
        @functools.wraps(base)
        def _make_wrapper(fn):
            def _wrapped(self, *args, **kwargs):
                _inject(self)
                return fn(self, *args, **kwargs)
            return _wrapped

        setattr(cpc.ChargePoint, name, _make_wrapper(base))

    _LOGGER.debug("ChargePoint: méthodes patchées (token-only, sans check_login).")
