"""Tests de `lbc_alertes.scraper.seller.SellerClient`.

Aucun réseau : le transport est un faux objet muni de `get_json()`, le `Pacer`
est une souche muette et le cache SQLite vit dans `tmp_path`. Les `time.sleep()`
des tentatives suivantes sont interceptées (le backoff réel est de 45 s).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_ad
from lbc_alertes.models import Owner
from lbc_alertes.scraper.leboncoin import BlockedError, FetchError
from lbc_alertes.scraper.seller import USER_CARD_PATH, SellerClient, Pacer
from lbc_alertes.store import Store

USER_ID = "8a44d40a-567d-42eb-b4f4-ecad73392c01"


# --------------------------------------------------------------------------- #
#  Souches                                                                      #
# --------------------------------------------------------------------------- #


class FakeHttp:
    """Bouchon de `HttpxScraper` : `get_json()` ne sort jamais sur le réseau."""

    def __init__(self, results=()):
        # `results` = liste de payloads ou d'exceptions à lever, dans l'ordre.
        self.results = list(results)
        self.calls: list[str] = []

    def get_json(self, url: str):
        self.calls.append(url)
        if not self.results:
            raise AssertionError("appel réseau inattendu")
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class SilentPacer:
    """Pacer sans temporisation (le vrai attend 4 à 9 s entre requêtes)."""

    def __init__(self) -> None:
        self.waits = 0

    def wait(self) -> None:
        self.waits += 1


class FakeTime:
    """Remplace le module `time` de `seller.py` pour neutraliser le backoff."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "sellers.db")
    try:
        yield st
    finally:
        st.close()


def _client(http, *, store=None, max_retries=2, backoff=45.0, ttl_hours=24 * 7):
    pacer = SilentPacer()
    client = SellerClient(
        http,
        pacer,
        max_retries=max_retries,
        backoff_base_min=backoff,
        cache_ttl_hours=ttl_hours,
        store=store,
    )
    return client, pacer


# --------------------------------------------------------------------------- #
#  fetch() : mémoire + cache SQLite                                             #
# --------------------------------------------------------------------------- #


def test_fetch_retourne_le_payload(seller_payload):
    http = FakeHttp([seller_payload])
    client, pacer = _client(http)

    payload = client.fetch(USER_ID)

    assert payload == seller_payload
    assert http.calls == [USER_CARD_PATH.format(user_id=USER_ID)]
    assert pacer.waits == 1


def test_deuxieme_appel_vient_du_cache_memoire(seller_payload):
    http = FakeHttp([seller_payload])
    client, _ = _client(http)

    premier = client.fetch(USER_ID)
    second = client.fetch(USER_ID)

    assert premier is second
    assert len(http.calls) == 1


def test_cache_sqlite_reutilise_par_un_nouveau_client(seller_payload, store):
    http = FakeHttp([seller_payload])
    premier, _ = _client(http, store=store)
    assert premier.fetch(USER_ID) == seller_payload

    # Second client, même base : le payload vient de SQLite, sans requête.
    http2 = FakeHttp()
    second, _ = _client(http2, store=store)

    assert second.fetch(USER_ID) == seller_payload
    assert http2.calls == []
    # ... et le résultat est remonté dans la mémoire du second client.
    assert second.fetch(USER_ID) == seller_payload
    assert http2.calls == []


def test_payload_ecrit_dans_le_store(seller_payload, store):
    client, _ = _client(FakeHttp([seller_payload]), store=store)
    client.fetch(USER_ID)

    assert store.get_seller(USER_ID, ttl_seconds=10_000) == seller_payload


def test_sans_store_le_cache_sqlite_est_desactive(seller_payload):
    client, _ = _client(FakeHttp([seller_payload]), store=None)
    assert client.fetch(USER_ID) == seller_payload


@pytest.mark.parametrize("user_id", [None, ""])
def test_user_id_vide_ne_declenche_aucune_requete(user_id):
    http = FakeHttp()
    client, pacer = _client(http)

    assert client.fetch(user_id) is None
    assert http.calls == []
    assert pacer.waits == 0


# --------------------------------------------------------------------------- #
#  fetch() : erreurs                                                            #
# --------------------------------------------------------------------------- #


def test_404_est_memorise_comme_absence_de_note(monkeypatch):
    http = FakeHttp([FetchError("HTTP 404 : profil introuvable")])
    client, _ = _client(http, max_retries=2)
    monkeypatch.setattr("lbc_alertes.scraper.seller.time", FakeTime())

    assert client.fetch(USER_ID) is None
    # Le 404 n'est pas temporaire : aucune nouvelle tentative.
    assert len(http.calls) == 1
    # L'absence est mémoïsée : un nouvel appel ne rappelle pas.
    assert client.fetch(USER_ID) is None
    assert len(http.calls) == 1


def test_erreur_temporaire_est_essaye_puis_abandonnee(monkeypatch):
    fake_time = FakeTime()
    monkeypatch.setattr("lbc_alertes.scraper.seller.time", fake_time)

    http = FakeHttp([FetchError("timeout"), FetchError("timeout")])
    client, pacer = _client(http, max_retries=1, backoff=0.5)

    assert client.fetch(USER_ID) is None
    assert len(http.calls) == 2
    assert pacer.waits == 2
    # backoff = backoff_base_min * 2**tentative
    assert fake_time.slept == [0.5]


def test_backoff_double_a_chaque_tentative(monkeypatch):
    fake_time = FakeTime()
    monkeypatch.setattr("lbc_alertes.scraper.seller.time", fake_time)

    http = FakeHttp([FetchError("500")] * 3)
    client, _ = _client(http, max_retries=2, backoff=1.0)

    assert client.fetch(USER_ID) is None
    assert len(http.calls) == 3
    assert fake_time.slept == [1.0, 2.0]


def test_succes_apres_une_erreur_temporaire(seller_payload, monkeypatch):
    monkeypatch.setattr("lbc_alertes.scraper.seller.time", FakeTime())

    http = FakeHttp([FetchError("timeout"), seller_payload])
    client, _ = _client(http, max_retries=2, backoff=0.1)

    assert client.fetch(USER_ID) == seller_payload
    assert len(http.calls) == 2


def test_blocked_error_se_propage_sans_essai_supplementaire():
    http = FakeHttp([BlockedError("403")])
    client, _ = _client(http, max_retries=3)

    with pytest.raises(BlockedError, match="403"):
        client.fetch(USER_ID)
    assert len(http.calls) == 1


def test_echec_enregistre_en_cache_sqlite(monkeypatch, store):
    monkeypatch.setattr("lbc_alertes.scraper.seller.time", FakeTime())
    client, _ = _client(FakeHttp([FetchError("HTTP 404")]), store=store, max_retries=1)

    assert client.fetch(USER_ID) is None
    # Rien n'est écrit en base pour un profil introuvable.
    assert store.get_seller(USER_ID, ttl_seconds=10_000) is None


# --------------------------------------------------------------------------- #
#  enrich()                                                                     #
# --------------------------------------------------------------------------- #


def test_enrich_applique_la_carte_vendeur(seller_payload):
    client, _ = _client(FakeHttp([seller_payload]))
    owner = Owner(user_id=USER_ID, type="private")

    returned = client.enrich(owner)

    assert returned is owner
    assert owner.rating == 5.0
    assert owner.rating_count == 3
    assert owner.reply_rate == 100
    assert owner.reply_time == "1 heure"
    assert owner.badges == ("Responsiveness3", "VerifiedPhoneNumber")
    assert owner.registered_at == "2016-10-01"
    assert owner.total_ads == 4


def test_enrich_ne_ricre_de_note_quavec_un_payload_non_vide():
    owner = Owner(user_id=USER_ID, type="private", rating=4.0)
    client, _ = _client(FakeHttp([{}]))

    client.enrich(owner)

    # `{}` est faux : `enrich()` ne fusionne rien et la note est conservée.
    assert owner.rating == 4.0


def test_enrich_sur_un_profil_introuvable_laisse_le_vendeur_intact():
    owner = Owner(user_id=USER_ID, type="private")
    client, _ = _client(FakeHttp([FetchError("HTTP 404")]), max_retries=0)

    returned = client.enrich(owner)

    assert returned is owner
    assert owner.rating is None
    assert owner.rating_count is None
    assert owner.badges == ()


def test_enrich_complete_lannonce_dune_recherche(seller_payload):
    ad = make_ad(owner=Owner(user_id=USER_ID, type="private"))
    client, _ = _client(FakeHttp([seller_payload]))

    client.enrich(ad.owner)

    assert ad.owner.rating == 5.0


# --------------------------------------------------------------------------- #
#  Pacer et réglages du client                                                  #
# --------------------------------------------------------------------------- #


def test_pacer_reel_est_utilisable_sans_attente():
    """Le vrai `Pacer(0, 0)` ne dort jamais : pratique pour les tests."""
    pacer = Pacer(0, 0)
    pacer.wait()
    pacer.wait()


def test_ttl_minimum_d_une_heure():
    http = FakeHttp()
    client, _ = _client(http, ttl_hours=0)
    assert client._cache_ttl == 3600.0


def test_ttl_en_secondes():
    http = FakeHttp()
    client, _ = _client(http, ttl_hours=48)
    assert client._cache_ttl == 48 * 3600.0


def test_cache_sqlite_explose_apres_le_ttl(seller_payload, store):
    http = FakeHttp([seller_payload, seller_payload])
    client, _ = _client(http, store=store)
    client.fetch(USER_ID)

    # On rajeunit artificiellement la ligne pour la rendre périmée.
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(
        timespec="seconds"
    )
    store.con.execute("UPDATE sellers SET fetched_at = ?", (old,))
    store.con.commit()

    assert store.get_seller(USER_ID, ttl_seconds=3600) is None
    # `fetch()` consulte d'abord le cache mémoire : on le vide pour vérifier
    # que la ligne SQLite périmée déclenche bien un nouvel appel.
    client._mem.clear()
    assert client.fetch(USER_ID) == seller_payload
    assert len(http.calls) == 2
