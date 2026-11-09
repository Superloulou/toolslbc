"""Tests du cycle du démon : ``CycleStats``, ``_reason_breakdown``, ``run_cycle``,
``_sleep`` et ``run_forever``."""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from lbc_alertes import daemon, filters
from lbc_alertes import store as store_mod
from lbc_alertes.config import SearchConfig, Settings
from lbc_alertes.daemon import (
    RETENTION_DAYS,
    CycleStats,
    _enrich_ratings,
    _reason_breakdown,
    _sleep,
    run_cycle,
    run_forever,
)
from lbc_alertes.models import Owner
from lbc_alertes.scraper import BlockedError
from lbc_alertes.store import Store

from conftest import make_ad

# --------------------------------------------------------------------------- #
# CycleStats
# --------------------------------------------------------------------------- #


def test_cycle_stats_defaults() -> None:
    stats = CycleStats()
    assert (stats.fetched, stats.kept, stats.new, stats.searches) == (0, 0, 0, 0)
    assert stats.blocked is False
    assert stats.rejected_samples == []


def test_cycle_stats_rejected_samples_not_shared() -> None:
    """La liste par défaut n'est pas partagée entre instances (default_factory)."""
    a, b = CycleStats(), CycleStats()
    a.rejected_samples.append("x")
    assert b.rejected_samples == []


def test_retention_days_is_a_positive_int() -> None:
    assert isinstance(RETENTION_DAYS, int)
    assert RETENTION_DAYS > 0


# --------------------------------------------------------------------------- #
# _reason_breakdown
# --------------------------------------------------------------------------- #


def test_reason_breakdown_collapses_parenthesised_detail() -> None:
    """Le cas demandé par la spec : deux motifs « trop ancienne (…) » → un seul bucket."""
    rejected = [
        filters.Rejected(ad=make_ad(list_id=1), reason="trop ancienne (96 h)"),
        filters.Rejected(ad=make_ad(list_id=2), reason="mot manquant : « x »"),
        filters.Rejected(ad=make_ad(list_id=3), reason="trop ancienne (120 h)"),
    ]
    assert _reason_breakdown(rejected) == [
        ("trop ancienne", 2),
        ("mot manquant : « x »", 1),
    ]


def test_reason_breakdown_empty() -> None:
    assert _reason_breakdown([]) == []


def test_reason_breakdown_single() -> None:
    assert _reason_breakdown([filters.Rejected(ad=make_ad(), reason="date de publication inconnue")]) == [
        ("date de publication inconnue", 1)
    ]


def test_reason_breakdown_strips_only_the_trailing_group() -> None:
    out = dict(
        _reason_breakdown(
            [
                filters.Rejected(ad=make_ad(), reason="note vendeur 3.2 < 4.0"),
                filters.Rejected(ad=make_ad(), reason="vendeur pro non autorisé"),
            ]
        )
    )
    assert out == {"note vendeur 3.2 < 4.0": 1, "vendeur pro non autorisé": 1}


def test_reason_breakdown_sorts_by_count_then_label() -> None:
    reasons = ["b (1 h)", "b (2 h)", "a (3 h)", "a (4 h)", "a (5 h)", "c"]
    out = _reason_breakdown([filters.Rejected(ad=make_ad(), reason=r) for r in reasons])
    assert out == [("a", 3), ("b", 2), ("c", 1)]


def test_reason_breakdown_handles_trailing_whitespace() -> None:
    out = _reason_breakdown(
        [
            filters.Rejected(ad=make_ad(), reason="trop ancienne (96 h)  "),
            filters.Rejected(ad=make_ad(), reason="trop ancienne (120 h)"),
        ]
    )
    assert out == [("trop ancienne", 2)]


def test_reason_breakdown_on_real_filter_output() -> None:
    """Intégration : le breakdown consomme bien la sortie réelle de ``filters.apply``."""
    cfg = SearchConfig(name="rtx", text="rtx 3090", max_age_hours=24.0, require=["gpu"])
    ads = [
        make_ad(list_id=1, published=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        make_ad(list_id=2, published=datetime(2026, 1, 2, tzinfo=timezone.utc)),
        make_ad(list_id=3, subject="carte graphique inconnue"),
    ]
    result = filters.apply(ads, cfg)
    assert _reason_breakdown(result.rejected) == [
        ("trop ancienne", 2),
        ("mot manquant : « gpu »", 1),
    ]


# --------------------------------------------------------------------------- #
# Doubles de test pour run_cycle
# --------------------------------------------------------------------------- #


class _FakeClient:
    """``LeboncoinClient`` factice : renvoie des annonces issues du fixture."""

    def __init__(
        self,
        error: Exception | None = None,
        detail: object = None,
        **by_name: list,
    ) -> None:
        self._by_name = by_name
        self._error = error
        self._detail = detail
        self.calls: list[str] = []
        self.details: list[str] = []
        self.closed = 0
        # ``run_cycle`` lit ``client.http`` et ``client.pacer`` pour construire SellerClient.
        self.http = object()
        self.pacer = object()

    def search(self, search: SearchConfig) -> list:
        self.calls.append(search.name)
        if self._error is not None:
            raise self._error
        return list(self._by_name.get(search.name, []))

    def ad_detail(self, ad_url: str):
        """Fiche annonce : la ``detail`` fourni est resservi pour chaque annonce."""
        self.details.append(ad_url)
        return self._detail

    def close(self) -> None:
        self.closed += 1


class _FakeNotifier:
    def __init__(self, configured: bool = True, sent: int | None = None) -> None:
        self.configured = configured
        self._sent = sent
        self.calls: list[tuple[str, list]] = []

    def notify(self, name: str, ads: list) -> int:
        self.calls.append((name, list(ads)))
        return len(ads) if self._sent is None else self._sent


class _FakeSellers:
    """Se substitue à ``SellerClient`` (monkeypatch) pour rester hors réseau."""

    def __init__(self, ratings: dict[str, float | None] | None = None, error: Exception | None = None) -> None:
        self.ratings = ratings or {}
        self.error = error
        self.seen: list[str] = []

    def enrich(self, owner: Owner) -> Owner:
        self.seen.append(owner.user_id)
        if self.error is not None:
            raise self.error
        if owner.user_id in self.ratings:
            owner.rating = self.ratings[owner.user_id]
            owner.rating_count = 3
        return owner


def _settings(*names: str, **overrides) -> Settings:
    """``Settings`` minimal mais valide (les champs réels du dataclass, rien d'autre)."""
    kwargs: dict = {
        "searches": [SearchConfig(name=n, text=n, pages=1) for n in names],
        "poll_interval_min": 30,
        "seller_lookup": False,
        # Sans ça, le tout premier cycle du store ne notifie jamais.
        "notify_on_first_run": True,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _notified(store: Store, search: str) -> list[int]:
    rows = store.con.execute(
        "SELECT notified FROM seen_ads WHERE search = ? ORDER BY list_id", (search,)
    ).fetchall()
    return [int(r[0]) for r in rows]


def _sent(notifier: "_FakeNotifier") -> list[tuple[str, list[int]]]:
    """Notifications émises, sous forme ``(recherche, [list_id...])``."""
    return [(name, [ad.list_id for ad in ads]) for name, ads in notifier.calls]


def _steady_clock(monkeypatch) -> None:
    """Horloge ``store`` qui avance d'une seconde à chaque appel.

    Depuis que ``runs`` porte une clé ``run_id`` auto-incrémentée et que
    ``store._now()`` est à la microseconde, deux cycles du même essai ne peuvent
    plus entrer en collision. Cette horloge reste utile parce qu'elle rend les
    comparaisons de ``last_run()`` indépendantes de l'heure murale de la machine.
    """
    base = datetime(2025, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    ticks = iter(range(0, 100_000))
    monkeypatch.setattr(
        store_mod,
        "_now",
        lambda: (base + timedelta(seconds=next(ticks))).isoformat(timespec="seconds"),
    )


# --------------------------------------------------------------------------- #
# run_cycle : compteurs et journalisation
# --------------------------------------------------------------------------- #


def test_run_cycle_counts_and_logs_a_run(tmp_path) -> None:
    st = Store(tmp_path / "state.db")
    old = make_ad(list_id=3, published=datetime(2020, 1, 1, tzinfo=timezone.utc))
    ads = [make_ad(list_id=1), make_ad(list_id=2), old]
    stats = run_cycle(_FakeClient(a=ads), st, _FakeNotifier(), _settings("a"))

    assert stats.searches == 1
    assert stats.fetched == 3
    assert stats.kept == 2
    assert stats.new == 2
    assert stats.blocked is False

    row = st.con.execute(
        "SELECT fetched, kept, new_ads FROM runs WHERE search = ?", ("a",)
    ).fetchone()
    assert row == (3, 2, 2)
    # ``last_run`` renvoie la chaîne ISO de fin de cycle, pas un enregistrement.
    assert st.last_run("a") is not None
    assert st.last_run("inconnue") is None


def test_run_cycle_accumulates_across_searches(tmp_path) -> None:
    st = Store(tmp_path / "state.db")
    settings = _settings("a", "b")
    client = _FakeClient(a=[make_ad(list_id=1)], b=[make_ad(list_id=2), make_ad(list_id=3)])
    stats = run_cycle(client, st, _FakeNotifier(), settings)

    assert client.calls == ["a", "b"]
    assert (stats.searches, stats.fetched, stats.kept, stats.new) == (2, 3, 3, 3)
    assert st.count_seen() == 3
    assert st.count_seen("a") == 1


def test_run_cycle_remembers_categories(tmp_path) -> None:
    st = Store(tmp_path / "state.db")
    ads = [make_ad(list_id=1, category_id=128, category_name="Automobile")]
    run_cycle(_FakeClient(a=ads), st, _FakeNotifier(), _settings("a"))
    assert st.known_categories() == [(128, "Automobile")]


def test_run_cycle_dedups_across_cycles(tmp_path, monkeypatch) -> None:
    _steady_clock(monkeypatch)
    st = Store(tmp_path / "state.db")
    ads = [make_ad(list_id=1)]
    settings = _settings("a")
    first = run_cycle(_FakeClient(a=ads), st, _FakeNotifier(), settings)
    second = run_cycle(_FakeClient(a=ads), st, _FakeNotifier(), settings)

    assert first.new == 1
    assert second.new == 0
    assert st.count_seen("a") == 1


def test_run_cycle_rejects_stale_ads_and_samples_the_reason(tmp_path, caplog) -> None:
    st = Store(tmp_path / "state.db")
    ads = [
        make_ad(list_id=1, published=datetime(2020, 1, 1, tzinfo=timezone.utc)),
        make_ad(list_id=2, published=datetime(2020, 1, 2, tzinfo=timezone.utc)),
    ]
    with caplog.at_level(logging.DEBUG, logger="lbc"):
        stats = run_cycle(_FakeClient(a=ads), st, _FakeNotifier(), _settings("a"))

    assert (stats.fetched, stats.kept, stats.new) == (2, 0, 0)
    assert stats.rejected_samples == ["a : trop ancienne"]
    assert "rejet 2× : trop ancienne" in caplog.text
    assert "0 annonce conforme sur" in caplog.text


def test_run_cycle_keeps_at_most_three_rejected_samples(tmp_path) -> None:
    st = Store(tmp_path / "state.db")
    ads = [
        make_ad(list_id=n, published=datetime(2020, 1, n, tzinfo=timezone.utc))
        for n in (1, 2, 3)
    ]
    settings = _settings("a", "b", "c")
    client = _FakeClient(a=ads, b=ads, c=ads)
    stats = run_cycle(client, st, _FakeNotifier(), settings)
    assert len(stats.rejected_samples) == 3


# --------------------------------------------------------------------------- #
# run_cycle : notifications Telegram
# --------------------------------------------------------------------------- #


def test_run_cycle_notifies_and_marks_ads_notified(tmp_path) -> None:
    st = Store(tmp_path / "state.db")
    ads = [make_ad(list_id=1), make_ad(list_id=2)]
    notifier = _FakeNotifier()
    run_cycle(_FakeClient(a=ads), st, notifier, _settings("a"))

    assert [name for name, _ in notifier.calls] == ["a"]
    assert sorted(ad.list_id for ad in notifier.calls[0][1]) == [1, 2]
    assert _notified(st, "a") == [1, 1]


def test_run_cycle_marks_only_the_ads_actually_sent(tmp_path, caplog) -> None:
    st = Store(tmp_path / "state.db")
    ads = [make_ad(list_id=1), make_ad(list_id=2)]
    notifier = _FakeNotifier(sent=1)
    with caplog.at_level(logging.WARNING, logger="lbc"):
        run_cycle(_FakeClient(a=ads), st, notifier, _settings("a"))

    assert _notified(st, "a") == [1, 0]
    assert "annonce non notifiée" in caplog.text


def test_run_cycle_silent_when_notifier_unconfigured(tmp_path, caplog) -> None:
    st = Store(tmp_path / "state.db")
    ads = [make_ad(list_id=1)]
    notifier = _FakeNotifier(configured=False)
    with caplog.at_level(logging.WARNING, logger="lbc"):
        stats = run_cycle(_FakeClient(a=ads), st, notifier, _settings("a"))

    assert notifier.calls == []
    assert stats.new == 1
    assert _notified(st, "a") == [0]
    assert "Telegram non configuré" in caplog.text


def test_run_cycle_suppresses_notifications_on_first_run(tmp_path, caplog) -> None:
    """Store vierge + ``notify_on_first_run=False`` : on enregistre sans notifier."""
    st = Store(tmp_path / "state.db")
    ads = [make_ad(list_id=1)]
    notifier = _FakeNotifier()
    with caplog.at_level(logging.INFO, logger="lbc"):
        stats = run_cycle(
            _FakeClient(a=ads), st, notifier, _settings("a", notify_on_first_run=False)
        )

    assert notifier.calls == []
    assert stats.new == 1
    assert _notified(st, "a") == [0]
    assert "première exécution" in caplog.text


def test_run_cycle_notifies_once_a_previous_run_is_recorded(tmp_path, monkeypatch) -> None:
    """``last_run`` non vide ⇒ ce n'est plus la première exécution."""
    _steady_clock(monkeypatch)
    st = Store(tmp_path / "state.db")
    st.log_run("a", fetched=0, kept=0, new_ads=0)
    notifier = _FakeNotifier()
    run_cycle(
        _FakeClient(a=[make_ad(list_id=1)]),
        st,
        notifier,
        _settings("a", notify_on_first_run=False),
    )
    assert len(notifier.calls) == 1
    assert _notified(st, "a") == [1]


def test_run_cycle_does_not_notify_already_seen_ads(tmp_path, monkeypatch) -> None:
    _steady_clock(monkeypatch)
    st = Store(tmp_path / "state.db")
    run_cycle(_FakeClient(a=[make_ad(list_id=1)]), st, _FakeNotifier(), _settings("a"))
    notifier = _FakeNotifier()
    run_cycle(_FakeClient(a=[make_ad(list_id=1)]), st, notifier, _settings("a"))
    assert notifier.calls == []


# --------------------------------------------------------------------------- #
# run_cycle : erreurs
# --------------------------------------------------------------------------- #


def test_run_cycle_stops_on_blocked_error(tmp_path, caplog) -> None:
    st = Store(tmp_path / "state.db")
    client = _FakeClient(a=[make_ad(list_id=1)], error=BlockedError("429 trop de requêtes"))
    with caplog.at_level(logging.ERROR, logger="lbc"):
        stats = run_cycle(client, st, _FakeNotifier(), _settings("a", "b"))

    assert stats.blocked is True
    assert client.calls == ["a"]
    assert (stats.searches, stats.fetched, stats.new) == (0, 0, 0)
    assert "blocage" in caplog.text
    # Le cycle bloqué n'est pas journalisé dans ``runs``.
    assert st.last_run("a") is None


def test_run_cycle_survives_a_generic_search_error(tmp_path, caplog) -> None:
    st = Store(tmp_path / "state.db")

    class _Client(_FakeClient):
        def search(self, search: SearchConfig) -> list:
            self.calls.append(search.name)
            if search.name == "a":
                raise RuntimeError("panne réseau")
            return [make_ad(list_id=9)]

    with caplog.at_level(logging.ERROR, logger="lbc"):
        stats = run_cycle(_Client(), st, _FakeNotifier(), _settings("a", "b"))

    assert stats.blocked is False
    assert (stats.searches == 1 and stats.new == 1)
    assert "panne réseau" in caplog.text


# --------------------------------------------------------------------------- #
# _enrich_ratings
# --------------------------------------------------------------------------- #


def test_enrich_ratings_drops_ads_below_min_rating() -> None:
    ad = make_ad(list_id=1, owner=Owner(user_id="u-bad", type="private", name="Bob"))
    sellers = _FakeSellers(ratings={"u-bad": 3.0})
    cfg = SearchConfig(name="a", text="a", min_rating=4.0)
    assert _enrich_ratings(sellers, [ad], cfg) == []
    assert sellers.seen == ["u-bad"]


def test_enrich_ratings_keeps_ads_at_or_above_min_rating() -> None:
    ad = make_ad(list_id=1, owner=Owner(user_id="u-good", type="private", name="Bob"))
    sellers = _FakeSellers(ratings={"u-good": 4.5})
    cfg = SearchConfig(name="a", text="a", min_rating=4.0)
    kept = _enrich_ratings(sellers, [ad], cfg)
    assert [a.list_id for a in kept] == [1]
    assert kept[0].owner.rating == 4.5


def test_enrich_ratings_keeps_ad_when_rating_stays_unknown() -> None:
    ad = make_ad(list_id=1, owner=Owner(user_id="u-x", type="private", name="Bob"))
    cfg = SearchConfig(name="a", text="a", min_rating=4.0)
    kept = _enrich_ratings(_FakeSellers(), [ad], cfg)
    assert [a.list_id for a in kept] == [1]
    assert kept[0].owner.rating is None


def test_enrich_ratings_skips_ads_that_already_have_a_rating() -> None:
    ad = make_ad(
        list_id=1,
        owner=Owner(user_id="u-y", type="pro", name="P", rating=1.0, rating_count=2),
    )
    sellers = _FakeSellers(ratings={"u-y": 5.0})
    cfg = SearchConfig(name="a", text="a", min_rating=4.0)
    kept = _enrich_ratings(sellers, [ad], cfg)
    assert sellers.seen == []
    assert kept == []  # note déjà présente (1.0) et sous le seuil ⇒ écartée


def test_enrich_ratings_without_min_rating_keeps_everything() -> None:
    ad = make_ad(list_id=1, owner=Owner(user_id="u-z", type="private", name="Bob"))
    sellers = _FakeSellers(ratings={"u-z": 1.0})
    assert [a.list_id for a in _enrich_ratings(sellers, [ad], SearchConfig(name="a", text="a"))] == [1]


def test_enrich_ratings_propagates_blocked_error() -> None:
    ad = make_ad(list_id=1, owner=Owner(user_id="u-b", type="private", name="Bob"))
    sellers = _FakeSellers(error=BlockedError("challenge"))
    with pytest.raises(BlockedError):
        _enrich_ratings(sellers, [ad], SearchConfig(name="a", text="a"))


def test_enrich_ratings_swallows_other_errors(tmp_path) -> None:
    ad = make_ad(list_id=1, owner=Owner(user_id="u-e", type="private", name="Bob"))
    sellers = _FakeSellers(error=RuntimeError("boom"))
    kept = _enrich_ratings(sellers, [ad], SearchConfig(name="a", text="a"))
    assert [a.list_id for a in kept] == [1]
    assert kept[0].owner.rating is None


def test_run_cycle_applies_min_rating_after_seller_lookup(tmp_path, monkeypatch) -> None:
    st = Store(tmp_path / "state.db")
    sellers = _FakeSellers(ratings={"u-1": 2.0})
    monkeypatch.setattr(daemon, "SellerClient", lambda *a, **k: sellers)

    ads = [make_ad(list_id=1)]
    notifier = _FakeNotifier()
    settings = _settings("a", seller_lookup=True)
    settings.searches[0].min_rating = 4.0
    stats = run_cycle(_FakeClient(a=ads), st, notifier, settings)

    assert sellers.seen == ["u-1"]
    assert stats.new == 0
    assert notifier.calls == []
    # L'annonce reste enregistrée comme vue, mais non notifiée.
    assert _notified(st, "a") == [0]


def test_run_cycle_never_enriches_when_seller_lookup_disabled(tmp_path, monkeypatch) -> None:
    """``seller_lookup=False`` ⇒ aucune fiche vendeur demandée.

    ``SellerClient`` n'est même plus construit dans ce cas (daemon.py:86) ; ce qui
    ne doit pas arriver, c'est l'appel à ``enrich()``.
    """
    st = Store(tmp_path / "state.db")
    sellers = _FakeSellers(ratings={"u-1": 5.0})
    monkeypatch.setattr(daemon, "SellerClient", lambda *a, **k: sellers)

    stats = run_cycle(
        _FakeClient(a=[make_ad(list_id=1)]), st, _FakeNotifier(), _settings("a")
    )
    assert sellers.seen == []
    assert stats.new == 1


# --------------------------------------------------------------------------- #
# _sleep
# --------------------------------------------------------------------------- #


def test_sleep_returns_none_immediately_when_stopped() -> None:
    started = time.monotonic()
    assert _sleep(3600, {"stop": True}) is None
    assert time.monotonic() - started < 1.0


def test_sleep_slices_the_wait_and_honours_stop_midway(monkeypatch) -> None:
    """La pause est découpée en tranches ≤ 1 s pour pouvoir réagir à Ctrl-C."""
    clock = {"t": 0.0}
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock["t"])

    stop = {"stop": False}
    slices: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        slices.append(seconds)
        clock["t"] += seconds
        if len(slices) == 2:
            stop["stop"] = True

    monkeypatch.setattr(daemon.time, "sleep", _fake_sleep)

    assert _sleep(10.0, stop) is None
    assert slices == [1.0, 1.0]


def test_sleep_runs_the_full_duration_when_not_stopped(monkeypatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock["t"])
    slices: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        slices.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(daemon.time, "sleep", _fake_sleep)
    _sleep(2.5, {"stop": False})
    assert slices == [1.0, 1.0, 0.5]
    assert all(s <= 1.0 for s in slices)


# --------------------------------------------------------------------------- #
# run_forever
# --------------------------------------------------------------------------- #


def _fake_sleeps(monkeypatch, stop_after: int | None = None) -> list[float]:
    """Neutralise les attentes de ``run_forever`` et les enregistre.

    ``stop_after`` demande l'arrêt (le drapeau que SIGINT lève) dès la
    ``stop_after``-ième attente.
    """
    sleeps: list[float] = []

    def _sleep(seconds: float, stop: dict) -> None:
        sleeps.append(seconds)
        if stop_after is not None and len(sleeps) >= stop_after:
            stop["stop"] = True

    monkeypatch.setattr(daemon, "_sleep", _sleep)
    return sleeps


def test_run_forever_un_cycle_s_arrete_sans_attendre(tmp_path, monkeypatch):
    """``cycles=1`` : un seul cycle, puis sortie immédiate (pas de pause)."""
    st = Store(tmp_path / "state.db")
    client = _FakeClient(a=[make_ad(list_id=1)])
    notifier = _FakeNotifier()
    sleeps = _fake_sleeps(monkeypatch)

    run_forever(client, st, notifier, _settings("a"), cycles=1, install_signals=False)

    assert client.calls == ["a"]
    assert _sent(notifier) == [("a", [1])]
    assert sleeps == []
    assert client.closed == 1


def test_run_forever_pauses_between_cycles(tmp_path, monkeypatch):
    """Entre deux cycles : une pause ``poll_interval_min`` ± ``poll_jitter_min``."""
    st = Store(tmp_path / "state.db")
    client = _FakeClient(a=[make_ad(list_id=1)])
    notifier = _FakeNotifier()
    sleeps = _fake_sleeps(monkeypatch)

    run_forever(client, st, notifier, _settings("a"), cycles=2, install_signals=False)

    assert client.calls == ["a", "a"]
    # La même annonce n'est notifiée qu'une fois (déduplication).
    assert _sent(notifier) == [("a", [1])]
    assert len(sleeps) == 1
    assert 30 * 60 - 15 * 60 <= sleeps[0] <= 30 * 60 + 15 * 60


def test_run_forever_backoff_apres_un_cycle_bloque(tmp_path, monkeypatch):
    """Un cycle bloqué attend ``backoff_base_min`` x N, sans notification."""
    st = Store(tmp_path / "state.db")
    client = _FakeClient(a=[], error=BlockedError("403"))
    notifier = _FakeNotifier()
    sleeps = _fake_sleeps(monkeypatch)

    run_forever(client, st, notifier, _settings("a"), cycles=2, install_signals=False)

    assert sleeps == [45 * 60, 45 * 60 * 2]
    assert notifier.calls == []
    assert st.count_seen() == 0
    assert client.closed == 1


def test_run_forever_backoff_plafonne_a_quatre_heures(tmp_path, monkeypatch):
    """Le backoff croissant ne dépasse jamais 4 h, même après 6 blocages."""
    st = Store(tmp_path / "state.db")
    client = _FakeClient(a=[], error=BlockedError("403"))
    sleeps = _fake_sleeps(monkeypatch)

    run_forever(client, st, _FakeNotifier(), _settings("a"), cycles=6, install_signals=False)

    assert sleeps == [45 * 60 * n for n in range(1, 6)] + [4 * 3600]


def test_run_forever_backoff_se_reinitialise_apres_un_succes(tmp_path, monkeypatch):
    """Un cycle réussi remet le backoff à zéro."""
    st = Store(tmp_path / "state.db")
    client = _FakeClient(a=[])
    calls = {"n": 0}

    def _alternating(*_a, **_k):
        calls["n"] += 1
        # Blocé, bloqué, réussi, bloqué.
        return CycleStats(searches=1, blocked=calls["n"] != 3)

    monkeypatch.setattr(daemon, "run_cycle", _alternating)
    sleeps = _fake_sleeps(monkeypatch)

    run_forever(client, st, _FakeNotifier(), _settings("a"), cycles=4, install_signals=False)

    assert sleeps[0] == 45 * 60
    assert sleeps[1] == 45 * 60 * 2
    # Le troisième cycle réussit : c'est la pause normale qui suit.
    assert 15 * 60 <= sleeps[2] <= 45 * 60
    # Le blocage suivant repart au premier palier, pas au quatrième.
    assert sleeps[3] == 45 * 60


def test_run_forever_s_arrete_quand_le_drapeau_stop_est_leve(tmp_path, monkeypatch):
    """L'arrêt demandé pendant la pause coupe la boucle (chemin de SIGINT)."""
    st = Store(tmp_path / "state.db")
    client = _FakeClient(a=[make_ad(list_id=1)])
    sleeps = _fake_sleeps(monkeypatch, stop_after=1)

    run_forever(client, st, _FakeNotifier(), _settings("a"), install_signals=False)

    assert client.calls == ["a"]
    assert len(sleeps) == 1
    assert client.closed == 1


def test_run_forever_allege_la_base_a_chaque_cycle(tmp_path, monkeypatch):
    """Les annonces vues il y a plus de 2 x ``RETENTION_DAYS`` sont supprimées."""
    st = Store(tmp_path / "state.db")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=RETENTION_DAYS * 24 + 10)).isoformat(
        timespec="microseconds"
    )
    recent = now.isoformat(timespec="microseconds")
    monkeypatch.setattr(store_mod, "_now", iter([old, *[recent] * 10]).__next__)
    st.record("a", make_ad(list_id=1), notified=True)
    st.record("a", make_ad(list_id=2), notified=True)

    _fake_sleeps(monkeypatch)
    run_forever(
        _FakeClient(a=[]), st, _FakeNotifier(), _settings("a"), cycles=1, install_signals=False
    )

    assert st.is_seen("a", 1) is False
    assert st.is_seen("a", 2) is True


def test_run_forever_laisse_SIGINT_intact_sans_install_signals(tmp_path, monkeypatch):
    """``install_signals=False`` ne touche pas au gestionnaire du processus."""
    original = signal.getsignal(signal.SIGINT)
    st = Store(tmp_path / "state.db")
    client = _FakeClient(a=[])
    _fake_sleeps(monkeypatch, stop_after=1)

    run_forever(client, st, _FakeNotifier(), _settings("a"), install_signals=False)

    try:
        assert signal.getsignal(signal.SIGINT) is original
    finally:
        signal.signal(signal.SIGINT, original)


def test_run_forever_installe_le_gestionnaire_SIGINT(tmp_path, monkeypatch):
    """En mode service, SIGINT est intercepté pour la fermeture propre."""
    original = signal.getsignal(signal.SIGINT)
    st = Store(tmp_path / "state.db")
    client = _FakeClient(a=[])
    _fake_sleeps(monkeypatch, stop_after=1)

    try:
        run_forever(client, st, _FakeNotifier(), _settings("a"), install_signals=True)
        handler = signal.getsignal(signal.SIGINT)
        assert handler is not original
        assert callable(handler)
        # Le handler est idempotent : un second SIGINT ne lève rien.
        assert handler(signal.SIGINT, None) is None
        assert handler(signal.SIGINT, None) is None
    finally:
        signal.signal(signal.SIGINT, original)


def test_run_forever_hors_thread_principal_ne_leve_pas_erreur(tmp_path, monkeypatch):
    """``signal.signal`` est interdit en thread secondaire : le démon l'ignore."""
    client = _FakeClient(a=[])
    _fake_sleeps(monkeypatch, stop_after=1)
    errors: list[BaseException] = []

    def _target() -> None:
        # La connexion SQLite est affine au thread qui l'a créée : le Store est
        # construit (et refermé) dans le thread ouvrier.
        st = Store(tmp_path / "state.db")
        try:
            run_forever(client, st, _FakeNotifier(), _settings("a"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            st.close()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(10)

    assert not thread.is_alive()
    assert errors == []
    assert client.closed == 1
