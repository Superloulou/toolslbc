"""Tests for ``lbc_alertes.filters`` (ported from the ``t_norm.py`` probe).

Covers accent folding, the searchable haystack, ``check()`` reasons, the
naive-datetime regression in ``age_hours()`` and the ordering of ``apply()``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lbc_alertes import filters
from lbc_alertes.config import SearchConfig
from lbc_alertes.models import Owner

from conftest import make_ad

NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(**overrides) -> SearchConfig:
    """Base config: nothing is filtered unless a test opts in."""
    kwargs = dict(name="rtx 3090", text="rtx 3090", category=83, max_age_hours=336.0)
    kwargs.update(overrides)
    return SearchConfig(**kwargs)


# --------------------------------------------------------------------------
# _norm / _strip_accents
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("J'achète RTX 3090 (bon état)", "j achete rtx 3090 bon etat"),
        ("Déstockage", "destockage"),
        ("État neuf", "etat neuf"),
        ("DDR4 32 Go", "ddr4 32 go"),
        ("  Multiple   Spaces  ", "multiple spaces"),
        ("!!!***", ""),
    ],
)
def test_norm_folds_accents_and_punctuation(raw: str, expected: str) -> None:
    assert filters._norm(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("Déstockage", "Destockage"), ("État", "Etat"), ("çà", "ca")],
)
def test_strip_accents_removes_combining_marks(raw: str, expected: str) -> None:
    assert filters._strip_accents(raw) == expected


def test_norm_is_idempotent() -> None:
    once = filters._norm("J'achète RTX 3090 (bon état)")
    assert filters._norm(once) == once


# --------------------------------------------------------------------------
# _ad_haystack
# --------------------------------------------------------------------------
def test_ad_haystack_includes_subject_body_and_attributes() -> None:
    ad = make_ad(
        list_id=1,
        subject="RTX 3090 Gaming",
        body="carte graphique en bon état",
        attributes={"condition": "Bon état", "capacity": "24 Go"},
    )
    hay = filters._ad_haystack(ad)
    assert "rtx 3090 gaming" in hay
    assert "carte graphique en bon etat" in hay
    assert "24 go" in hay
    assert "bon etat" in hay
    # le nom de catégorie fait partie du texte cherché
    assert "informatique" in hay


def test_ad_haystack_skips_noise_attributes() -> None:
    ad = make_ad(
        list_id=1,
        subject="RTX 3090",
        attributes={
            "rating_score": "4.5",
            "rating_count": "12",
            "shippable": "true",
            "store_name": "Boutique RTX 3090 Pro",
        },
    )
    hay = filters._ad_haystack(ad)
    assert "4.5" not in hay
    assert "rating_score" not in hay
    assert "rating_count" not in hay
    # les attributs logistiques/techniques ne doivent pas servir de mot-clé
    assert "shippable" not in hay
    assert "store_name" not in hay
    assert "boutique" not in hay


def test_ad_haystack_truncates_body_at_400_chars() -> None:
    ad = make_ad(list_id=1, subject="RTX 3090", body="motus" + "x" * 500 + " intrus")
    hay = filters._ad_haystack(ad)
    assert "intrus" not in hay


# --------------------------------------------------------------------------
# check()
# --------------------------------------------------------------------------
WANT_TO_BUY = "J'achète RTX 3090 (bon état)"


def test_check_rejects_want_to_buy_ad_with_exclude() -> None:
    """Le cas réel : une annonce « j'achète » doit être écartée."""
    ad = make_ad(list_id=1, subject=WANT_TO_BUY, published=NOW - timedelta(hours=2))
    reason = filters.check(
        ad,
        _cfg(require=["rtx", "3090"], exclude=["j'achète"]),
        now=NOW,
    )
    assert isinstance(reason, str)
    assert "j'achète" in reason
    assert reason == "mot exclu : « j'achète »"


def test_check_accepts_same_ad_with_unrelated_exclude() -> None:
    ad = make_ad(list_id=1, subject=WANT_TO_BUY, published=NOW - timedelta(hours=2))
    assert filters.check(ad, _cfg(exclude=["3060"]), now=NOW) is None


def test_check_missing_required_word() -> None:
    ad = make_ad(list_id=1, subject="RTX 3060 Ti", published=NOW)
    reason = filters.check(ad, _cfg(require=["rtx 3090"]), now=NOW)
    assert reason == "mot manquant : « rtx 3090 »"


def test_check_too_old() -> None:
    ad = make_ad(list_id=1, subject="RTX 3090", published=NOW - timedelta(hours=96))
    reason = filters.check(ad, _cfg(max_age_hours=72.0), now=NOW)
    assert reason is not None
    assert reason.startswith("trop ancienne (96 h")


def test_check_unknown_date() -> None:
    ad = make_ad(list_id=1, subject="RTX 3090", published=None)
    assert filters.check(ad, _cfg(max_age_hours=72.0), now=NOW) == "date de publication inconnue"


def test_check_seller_type() -> None:
    ad = make_ad(list_id=1, subject="RTX 3090", published=NOW, owner=Owner(user_id="s", type="pro", name="Boutik"))
    reason = filters.check(ad, _cfg(seller_types=["private"]), now=NOW)
    assert reason == "vendeur pro non autorisé"
    assert filters.check(ad, _cfg(seller_types=["pro"]), now=NOW) is None


def test_check_min_rating() -> None:
    ad = make_ad(
        list_id=1,
        subject="RTX 3090",
        published=NOW,
        owner=Owner(user_id="u", type="private", name="Bob", rating=3.9),
    )
    assert filters.check(ad, _cfg(min_rating=4.0), now=NOW) == "note vendeur 3.9 < 4.0"
    assert filters.check(ad, _cfg(min_rating=3.5), now=NOW) is None


def test_check_min_rating_ignored_when_rating_unknown() -> None:
    ad = make_ad(
        list_id=1,
        subject="RTX 3090",
        published=NOW,
        owner=Owner(user_id="u", type="private", name="Bob", rating=None),
    )
    assert filters.check(ad, _cfg(min_rating=4.5), now=NOW) is None


def test_check_max_age_zero_disables_age_filter() -> None:
    ad = make_ad(list_id=1, subject="RTX 3090", published=NOW - timedelta(days=400))
    assert filters.check(ad, _cfg(max_age_hours=0), now=NOW) is None


def test_le_prix_n_est_pas_filtre_par_check() -> None:
    """Le filtrage du prix se fait cote requete (config.as_query), pas dans check().

    ``SearchConfig`` accepte ``price_min``/``price_max`` mais ``check()`` ne les
    compare jamais au prix de l'annonce : la plage est uniquement transmise a
    Leboncoin via le parametre ``price`` de l'URL.
    """
    ad = make_ad(list_id=1, subject="RTX 3090", price=5000, published=NOW)
    assert filters.check(ad, _cfg(price_min=200, price_max=800), now=NOW) is None
    assert _cfg(price_min=200, price_max=800).as_query()["price"] == "200-800"


# --------------------------------------------------------------------------
# age_hours()
# --------------------------------------------------------------------------
def test_age_hours_tz_aware() -> None:
    ad = make_ad(list_id=1, subject="x", published=NOW - timedelta(hours=3, minutes=30))
    assert filters.age_hours(ad, NOW) == pytest.approx(3.5)


def test_age_hours_naive_date_does_not_raise() -> None:
    """Régression : une date naïve en base ne doit pas faire planter le filtre."""
    naive = (NOW - timedelta(hours=5)).replace(tzinfo=None)
    ad = make_ad(list_id=1, subject="x", published=naive)
    assert ad.publication_date is not None
    assert ad.publication_date.tzinfo is None
    hours = filters.age_hours(ad, NOW)
    assert hours is not None
    assert hours == pytest.approx(5.0)


def test_age_hours_none_date() -> None:
    ad = make_ad(list_id=1, subject="x", published=None)
    assert filters.age_hours(ad, NOW) is None


# --------------------------------------------------------------------------
# apply()
# --------------------------------------------------------------------------
def test_apply_keeps_newest_first_and_records_reasons() -> None:
    old = make_ad(list_id=1, subject="RTX 3090", published=NOW - timedelta(hours=10))
    recent = make_ad(list_id=2, subject="RTX 3090 FE", published=NOW - timedelta(hours=1))
    middle = make_ad(list_id=3, subject="RTX 3090 Ti", published=NOW - timedelta(hours=5))
    bad = make_ad(list_id=4, subject="GTX 1050", published=NOW - timedelta(hours=2))

    res = filters.apply([old, recent, bad, middle], _cfg(require=["rtx 3090"]), now=NOW)

    assert isinstance(res, filters.FilterResult)
    assert [ad.list_id for ad in res.kept] == [2, 3, 1]
    assert len(res.rejected) == 1
    assert isinstance(res.rejected[0], filters.Rejected)
    assert res.rejected[0].ad.list_id == 4
    assert res.rejected[0].reason == "mot manquant : « rtx 3090 »"


def test_apply_with_no_date_keeps_ad_last() -> None:
    dated = make_ad(list_id=1, subject="RTX 3090", published=NOW - timedelta(hours=1))
    res = filters.apply([dated], _cfg(max_age_hours=0), now=NOW)
    assert [ad.list_id for ad in res.kept] == [1]


def test_apply_empty_input() -> None:
    res = filters.apply([], _cfg(), now=NOW)
    assert res.kept == []
    assert res.rejected == []


# --------------------------------------------------------------------------
# stale_cutoff()
# --------------------------------------------------------------------------
def test_stale_cutoff_is_tz_aware_and_in_the_past() -> None:
    cutoff = filters.stale_cutoff(48)
    assert cutoff.tzinfo is not None
    assert cutoff < datetime.now(timezone.utc)
    assert (datetime.now(timezone.utc) - cutoff).total_seconds() == pytest.approx(48 * 3600, abs=30)
