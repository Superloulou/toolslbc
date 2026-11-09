"""Tests du décodage des pages de recherche et de la construction des requêtes.

Aucun test ne touche le réseau : on travaille sur le fixture HTML et sur un
transport factice injecté dans `LeboncoinClient`.
"""

from __future__ import annotations

import json

import pytest

from lbc_alertes.config import SearchConfig
from lbc_alertes.scraper import CaptchaError, parse_search_page
from lbc_alertes.scraper import leboncoin
from lbc_alertes.scraper.leboncoin import (
    BASE_URL,
    SEARCH_PATH,
    BlockedError,
    FetchError,
    LeboncoinClient,
    Pacer,
    _quote,
    extract_next_data,
    verify_applied,
)

# Filtres tels que Leboncoin les renvoie pour « rtx 3090 » en catégorie 83.
APPLIED = {"keywords": {"text": "rtx 3090"}, "category": {"id": "83"}}


def _page_html(raw_ads, *, total=35, max_pages=7, filters=None) -> str:
    """Reconstruit une page de recherche minimale avec son bloc `__NEXT_DATA__`."""
    payload = {
        "props": {
            "pageProps": {
                "title": "Recherche rtx 3090",
                "search": {"filters": APPLIED if filters is None else filters},
                "searchData": {
                    "ads": raw_ads,
                    "total": total,
                    "max_pages": max_pages,
                },
            }
        }
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


class _FakeTransport:
    """Transport de test : pages en mémoire, journalise les appels reçus."""

    def __init__(self, pages) -> None:
        self._pages = list(pages)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict) -> str:
        self.calls.append((url, dict(params)))
        if len(self.calls) > len(self._pages):
            raise AssertionError("appel réseau inattendu")
        return self._pages[len(self.calls) - 1]


def _client(pages) -> tuple[LeboncoinClient, _FakeTransport]:
    """Client sans délai ni repli navigateur, transport remplacé par un bouchon."""
    transport = _FakeTransport(pages)
    client = LeboncoinClient(
        user_agent="pytest",
        request_delay_min=0.0,
        request_delay_max=0.0,
        max_retries=1,
        playwright_fallback=False,
    )
    client._http = transport
    return client, transport


# --------------------------------------------------------------------------- #
# extract_next_data / parse_search_page
# --------------------------------------------------------------------------- #


def test_extract_next_data_retourne_le_bloc_next(search_html: str) -> None:
    data = extract_next_data(search_html)
    page_props = data["props"]["pageProps"]
    assert page_props["searchData"]["total"] == 241
    assert len(page_props["searchData"]["ads"]) == 35


def test_parse_search_page_fixture(search_html: str) -> None:
    page = parse_search_page(search_html)
    assert len(page.ads) == 35
    assert page.total == 241
    assert page.max_pages == 7
    assert page.applied_filters["keywords"]["text"] == "rtx 3090"
    assert str(page.applied_filters["category"]["id"]) == "83"


def test_parse_search_page_reprend_tous_les_ads_valides(
    search_html: str, raw_ads: list[dict]
) -> None:
    page = parse_search_page(search_html)
    attendus = [r["list_id"] for r in raw_ads if isinstance(r.get("list_id"), int)]
    assert [ad.list_id for ad in page.ads] == attendus
    assert len(attendus) == 35


def test_parse_search_page_ecarte_les_annonces_sans_list_id() -> None:
    html = _page_html([{"list_id": 1}, {"subject": "sans identifiant"}])
    page = parse_search_page(html)
    assert [ad.list_id for ad in page.ads] == [1]


def test_parse_search_page_sans_resultats() -> None:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"title": "aucun résultat"}}})
        + "</script>"
    )
    page = parse_search_page(html)
    assert page.ads == []
    assert page.total == 0
    assert page.max_pages == 0


def test_parse_search_page_inattendue() -> None:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {}}})
        + "</script>"
    )
    with pytest.raises(FetchError, match="page inattendue"):
        parse_search_page(html)


def test_extract_next_data_absent() -> None:
    with pytest.raises(FetchError, match="__NEXT_DATA__ absent"):
        extract_next_data("<html><body>page vide</body></html>")


@pytest.mark.parametrize("marque", ["captcha", "DataDome", "CAPTCHA"])
def test_page_de_challenge_leve_blocked_error(marque: str) -> None:
    """Une page anti-bot doit lever une erreur plutôt qu'être parsée.

    NB : `CaptchaError` est déclarée mais jamais levée par le code source
    (bug signalé) ; c'est `BlockedError` qui est levée ici.
    """
    html = f"<html><body>Erreur {marque} : acces bloque</body></html>" + "x" * 4000
    with pytest.raises(BlockedError, match="challenge anti-bot") as excinfo:
        extract_next_data(html)
    assert not isinstance(excinfo.value, CaptchaError)


def test_captcha_error_herite_de_blocked_error() -> None:
    assert issubclass(CaptchaError, BlockedError)


# --------------------------------------------------------------------------- #
# _quote
# --------------------------------------------------------------------------- #


def test_quote_percent_encode_tout() -> None:
    assert _quote("rtx 3090") == "rtx%203090"
    assert _quote("j'achète") == "j%27ach%C3%A8te"
    assert _quote("a/b?c=1&d") == "a%2Fb%3Fc%3D1%26d"


def test_quote_accepte_les_non_chaines() -> None:
    assert _quote(83) == "83"


# --------------------------------------------------------------------------- #
# verify_applied
# --------------------------------------------------------------------------- #


def test_verify_applied_conforme_au_fixture(search_html: str) -> None:
    page = parse_search_page(search_html)
    cfg = SearchConfig(name="rtx 3090", text="rtx 3090", category=83)
    assert verify_applied(cfg, page.applied_filters) == []


def test_verify_applied_accepte_les_fourchettes_de_prix() -> None:
    cfg = SearchConfig(
        name="rtx 3090", text="rtx 3090", category=83, price_min=200, price_max=800
    )
    applied = dict(
        APPLIED, ranges={"price": {"min": 200, "max": 800}}
    )
    assert verify_applied(cfg, applied) == []


def test_verify_applied_signale_les_filts_perdus() -> None:
    cfg = SearchConfig(
        name="rtx 3090", text="rtx 3090", category=83, price_min=200, price_max=800
    )
    problems = verify_applied(cfg, {"keywords": {"text": "ddr4"}, "category": {"id": 12}})
    assert any("texte non appliqué" in p for p in problems)
    assert any("catégorie non appliqué" in p for p in problems)
    assert any("prix min non appliqué" in p for p in problems)
    assert any("prix max non appliqué" in p for p in problems)
    assert len(problems) == 4


def test_verify_applied_vide_sans_filtre_demande() -> None:
    cfg = SearchConfig(name="tout", text="", category=0)
    assert verify_applied(cfg, {}) == []


# --------------------------------------------------------------------------- #
# Construction de la requête par LeboncoinClient.search() (hors réseau)
# --------------------------------------------------------------------------- #


def test_search_construit_la_requête_et_paginer(raw_ads: list[dict]) -> None:
    cfg = SearchConfig(
        name="rtx 3090",
        text="rtx 3090",
        category=83,
        price_min=200,
        price_max=800,
        pages=2,
    )
    applied = dict(APPLIED, ranges={"price": {"min": 200, "max": 800}})
    pages = [
        _page_html(raw_ads[:2], total=241, max_pages=7, filters=applied),
        _page_html(raw_ads[2:4], total=241, max_pages=7, filters=applied),
    ]
    client, transport = _client(pages)

    ads = client.search(cfg)

    assert [url for url, _ in transport.calls] == [BASE_URL + SEARCH_PATH] * 2
    first = transport.calls[0][1]
    assert first["text"] == "rtx 3090"
    assert first["category"] == "83"
    assert first["price"] == "200-800"
    # première page : pas de paramètre `page`
    assert "page" not in first
    assert transport.calls[1][1]["page"] == "2"
    assert [ad.list_id for ad in ads] == [raw["list_id"] for raw in raw_ads[:4]]


def test_search_deduplique_les_annonces_repetees(raw_ads: list[dict]) -> None:
    cfg = SearchConfig(name="rtx 3090", text="rtx 3090", category=83, pages=2)
    page = _page_html(raw_ads[:3], total=241, max_pages=7)
    client, transport = _client([page, page])

    ads = client.search(cfg)
    assert len(transport.calls) == 2
    assert len(ads) == 3


def test_search_respecte_max_pages(raw_ads: list[dict]) -> None:
    cfg = SearchConfig(name="rtx 3090", text="rtx 3090", category=83, pages=5)
    client, transport = _client([_page_html(raw_ads[:1], total=241, max_pages=7)])

    client.search(cfg, max_pages=1)
    assert len(transport.calls) == 1


def test_search_s_arrete_aux_pages_du_site(raw_ads: list[dict]) -> None:
    cfg = SearchConfig(name="rtx 3090", text="rtx 3090", category=83, pages=9)
    pages = [
        _page_html(raw_ads[:1], total=1, max_pages=2),
        _page_html([], total=1, max_pages=2),
    ]
    client, transport = _client(pages)

    client.search(cfg)
    assert len(transport.calls) == 2


def test_search_filtre_non_applique_leve_fetch_error(raw_ads: list[dict]) -> None:
    cfg = SearchConfig(name="rtx 3090", text="rtx 3090", category=83)
    page = _page_html(raw_ads[:1], filters={"keywords": {"text": "autre chose"}})
    client, _ = _client([page])

    with pytest.raises(FetchError, match="filtre texte non appliqué"):
        client.search(cfg)


def test_search_transport_bloque_sans_repli() -> None:
    class _Blocked:
        def get(self, url: str, params: dict) -> str:
            raise BlockedError("403")

    client, _ = _client([])
    client._http = _Blocked()
    cfg = SearchConfig(name="rtx 3090", text="rtx 3090", category=83)

    with pytest.raises(BlockedError, match="403"):
        client.search(cfg)


# --------------------------------------------------------------------------- #
# Pacer : délais entre requêtes (horloge, sommeil et tirage simulés)
# --------------------------------------------------------------------------- #


class _FakeTime:
    """Faux module `time` pour `leboncoin` : aucune attente réelle."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _FakeRandom:
    """Faux module `random` : renvoie des délais connus et les enregistre."""

    def __init__(self, delays: list[float]) -> None:
        self._delays = list(delays)
        self.drawn: list[tuple[float, float]] = []

    def uniform(self, low: float, high: float) -> float:
        self.drawn.append((low, high))
        if not self._delays:
            raise AssertionError("tirage de délai inattendu")
        return self._delays.pop(0)


@pytest.fixture
def fake_time(monkeypatch) -> _FakeTime:
    fake = _FakeTime()
    monkeypatch.setattr(leboncoin, "time", fake)
    return fake


@pytest.fixture
def fake_random(monkeypatch):
    def _factory(delays: list[float]) -> _FakeRandom:
        fake = _FakeRandom(delays)
        monkeypatch.setattr(leboncoin, "random", fake)
        return fake

    return _factory


def test_pacer_normalise_et_plafonne_les_bornes() -> None:
    pacer = Pacer(2, 5)
    assert (pacer.min_seconds, pacer.max_seconds) == (2.0, 5.0)
    # Une borne max inférieure à la min est remontée au niveau de la min.
    assert Pacer(5, 1).max_seconds == 5.0
    assert Pacer(3, 3).max_seconds == 3.0


def test_pacer_tire_dans_la_plage_min_max(fake_time, fake_random) -> None:
    rng = fake_random([4.0])
    Pacer(4.0, 9.0).wait()
    assert rng.drawn == [(4.0, 9.0)]


def test_pacer_ne_dort_pas_au_premier_appel(fake_time, fake_random) -> None:
    pacer = Pacer(4.0, 9.0)
    fake_random([7.5])
    pacer.wait()
    assert fake_time.slept == []
    assert pacer._last == fake_time.now


def test_pacer_dort_du_delai_tire_au_second_appel(fake_time, fake_random) -> None:
    pacer = Pacer(4.0, 9.0)
    fake_random([6.0, 4.0])  # le second tirage vaut 4 s
    pacer.wait()  # premier appel : pose la référence, sans dormir
    pacer.wait()  # doit attendre 4 s de plus
    assert fake_time.slept == [4.0]
    assert fake_time.now == 1004.0
    assert pacer._last == 1004.0


def test_pacer_ne_dort_pas_si_le_delai_est_deja_ecoule(fake_time, fake_random) -> None:
    pacer = Pacer(1.0, 2.0)
    fake_random([1.5, 1.5])
    pacer.wait()
    fake_time.now += 10.0  # le temps passé hors boucle suffit déjà
    pacer.wait()
    assert fake_time.slept == []
    assert pacer._last == 1010.0


def test_pacer_delai_zero_ne_dort_jamais(fake_time) -> None:
    pacer = Pacer(0.0, 0.0)
    for _ in range(5):
        pacer.wait()
    assert fake_time.slept == []


def test_pacer_delais_reels_toujours_dans_la_plage(fake_time) -> None:
    """Avec le vrai `random`, le délai tiré reste entre min et max."""
    pacer = Pacer(4.0, 9.0)
    for _ in range(50):
        before = fake_time.now
        pacer.wait()
        assert before <= fake_time.now <= before + 9.0
    assert pacer._last == fake_time.now


def test_client_expose_un_pacer_configure(fake_time, fake_random) -> None:
    client, _ = _client([])
    assert isinstance(client.pacer, Pacer)
    assert (client.pacer.min_seconds, client.pacer.max_seconds) == (0.0, 0.0)

    slow = LeboncoinClient(
        user_agent="pytest",
        request_delay_min=3.0,
        request_delay_max=7.0,
        max_retries=1,
        playwright_fallback=False,
    )
    assert (slow.pacer.min_seconds, slow.pacer.max_seconds) == (3.0, 7.0)

    rng = fake_random([5.0])
    slow._polite_pause()
    assert rng.drawn == [(3.0, 7.0)]
    assert slow.pacer._last == fake_time.now
    slow.close()


def test_backoff_de_retry_plafonne_a_900_secondes(fake_time) -> None:
    """`_fetch` dort `min(backoff_base_min * 60 * tentative, 900)` entre les essais."""

    class _Failing:
        def get(self, url: str, params: dict) -> str:
            raise FetchError("500")

    client, _ = _client([])
    client._http = _Failing()
    client._max_retries = 3
    cfg = SearchConfig(name="rtx 3090", text="rtx 3090", category=83)

    with pytest.raises(FetchError, match="500"):
        client.search(cfg)

    assert fake_time.slept == [900, 900]
