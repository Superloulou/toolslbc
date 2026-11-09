"""Fiche vendeur : note, nombre d'avis, taux de réponse.

Endpoint découvert (utilisable avec l'empreinte TLS Chrome de curl_cffi, contrairement
à l'API de recherche qui elle est derrière DataDome) :

    GET https://api.leboncoin.fr/api/user-card/v2/{user_id}/infos

Attention : la note n'existe pas pour tous les vendeurs. Sur un échantillon de 8
vendeurs, 6 ne publiaient aucun champ ``feedback``. Une note absente vaut
« inconnue », jamais « 0 ».
"""

from __future__ import annotations

import logging
import time

from ..models import Owner
from .leboncoin import BlockedError, FetchError, HttpxScraper, Pacer

log = logging.getLogger("lbc.seller")

USER_CARD_PATH = "https://api.leboncoin.fr/api/user-card/v2/{user_id}/infos"


class SellerClient:
    """Récupère la fiche vendeur, avec cache et même cadence que la recherche."""

    def __init__(
        self,
        http: HttpxScraper,
        pacer: Pacer,
        max_retries: int = 2,
        backoff_base_min: float = 45.0,
        cache_ttl_hours: float = 24 * 7,
        store=None,
    ) -> None:
        self._http = http
        self._pacer = pacer
        self._max_retries = max_retries
        self._backoff_base_min = backoff_base_min
        self._cache_ttl = max(cache_ttl_hours, 1.0) * 3600
        self._store = store
        self._mem: dict[str, dict | None] = {}

    def fetch(self, user_id):
        """Retourne la fiche brute, ou ``None`` si indisponible (jamais bloquant)."""
        if not user_id:
            return None
        if user_id in self._mem:
            return self._mem[user_id]

        cached = self._from_store(user_id)
        if cached is not None:
            self._mem[user_id] = cached
            return cached

        url = USER_CARD_PATH.format(user_id=user_id)
        for attempt in range(self._max_retries + 1):
            self._pacer.wait()
            try:
                payload = self._http.get_json(url)
            except BlockedError:
                # Une 403 sur l'API doit remonter : le démon sait qu'il faut lever le pied.
                raise
            except FetchError as exc:
                if "404" in str(exc) or attempt >= self._max_retries:
                    log.debug("fiche vendeur %s indisponible : %s", user_id, exc)
                    self._mem[user_id] = None
                    return None
                delay = self._backoff_base_min * (2**attempt)
                log.warning("fiche vendeur %s : %s (nouvel essai dans %.0fs)", user_id, exc, delay)
                time.sleep(delay)
                continue
            self._mem[user_id] = payload
            self._to_store(user_id, payload)
            return payload
        self._mem[user_id] = None
        return None

    def enrich(self, owner: Owner) -> Owner:
        """Applique la fiche vendeur à un ``Owner`` (no-op si fiche indisponible)."""
        payload = self.fetch(owner.user_id)
        if payload:
            owner.apply_user_card(payload)
        return owner

    def _from_store(self, user_id):
        if self._store is None:
            return None
        return self._store.get_seller(user_id, self._cache_ttl)

    def _to_store(self, user_id, payload):
        if self._store is None:
            return
        self._store.put_seller(user_id, payload)
