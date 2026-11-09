"""Modèle ``Ad`` / ``Owner`` : parsing des vraies réponses Leboncoin."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from lbc_alertes.models import _parse_dt, Ad, Owner


def test_fixture_contains_35_ads(raw_ads: list[dict]) -> None:
    assert len(raw_ads) == 35


def test_from_api_builds_every_ad(raw_ads: list[dict], ads: list[Ad | None]) -> None:
    assert len(ads) == len(raw_ads)
    assert all(isinstance(ad, Ad) for ad in ads)


def test_premiere_annuce_du_fixture(raw_ads: list[dict], ads: list[Ad]) -> None:
    """Champs attendus sur une annonce précise du fixture ``search_cat83.html``."""
    ad = next(a for a in ads if a.list_id == 3296282602)
    assert ad.subject == "Rtx 3090 fe"
    # Le payload expose ``price`` en euros (et un ``price_cents`` ignoré par le code).
    assert ad.price == 1000
    assert ad.url == "https://www.leboncoin.fr/ad/accessoires_informatique/3296282602"
    # ``category_id`` est recopié tel quel depuis le payload : ici une chaine,
    # alors que l'annotation du dataclass annonce ``int | None``.
    assert ad.category_id == "83"
    assert ad.category_name == "Accessoires informatique"
    assert ad.owner.type == "private"


def test_list_ids_are_non_empty(ads: list[Ad]) -> None:
    ids = [ad.list_id for ad in ads]
    assert all(i is not None and str(i) != "" for i in ids)
    assert all(isinstance(i, int) or isinstance(i, str) for i in ids)
    assert len(set(map(str, ids))) == len(ids)


def test_publication_dates_are_timezone_aware(ads: list[Ad]) -> None:
    """Régression : les dates étaient parsées sans fuseau (comparaison naive/aware)."""
    dates = [ad.publication_date for ad in ads]
    assert all(d is not None for d in dates)
    for ad in ads:
        assert ad.first_publication_date is None or ad.first_publication_date.tzinfo is not None
        assert ad.index_date is None or ad.index_date.tzinfo is not None
        assert ad.publication_date is not None and ad.publication_date.tzinfo is not None
    # Une date aware est comparable à une date « maintenant » sans lever d'erreur.
    assert min(dates) <= datetime.now(timezone.utc)


@pytest.mark.parametrize("raw_value", ["2024-01-05 08:30:00", "2024-12-31 23:59:59"])
def test_parse_dt_returns_aware_datetime(raw_value: str) -> None:
    dt = _parse_dt(raw_value)
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None


@pytest.mark.parametrize("raw_value", [None, "", "pas une date", "0"])
def test_parse_dt_returns_none_for_invalid(raw_value) -> None:
    assert _parse_dt(raw_value) is None


def test_owner_types_are_private_or_pro(ads: list[Ad]) -> None:
    types = {ad.owner.type for ad in ads}
    assert types <= {"private", "pro"}
    assert types != set()


def test_at_least_one_seller_rating_is_published(ads: list[Ad]) -> None:
    rated = [ad for ad in ads if ad.owner.rating is not None]
    assert rated, "au moins une annonce devrait avoir une note vendeur"
    for ad in rated:
        assert 0 < ad.owner.rating <= 5


def test_ratings_come_from_rating_score_attribute(raw_ads: list[dict], ads: list[Ad]) -> None:
    """La note est dérivée de l'attribut ``rating_score`` (fraction 0..1 -> note /5)."""

    def score_of(raw: dict) -> str | None:
        for attr in raw.get("attributes") or []:
            if attr.get("key") == "rating_score":
                return str(attr.get("value_label") or attr.get("value"))
        return None

    with_score = [i for i, raw in enumerate(raw_ads) if score_of(raw)]
    assert with_score, "le fixture devrait contenir des annonces notées"
    for i in with_score:
        expected = round(float(score_of(raw_ads[i]).replace(",", ".")) * 5, 2)
        assert ads[i].owner.rating == pytest.approx(expected, abs=0.01)


def test_rating_score_099_devient_495() -> None:
    """Un ``rating_score`` de ``'0.99'`` doit devenir une note ``4.95`` /5."""
    ad = Ad.from_api(
        {
            "list_id": 1,
            "subject": "RTX 3090",
            "attributes": [
                {"key": "rating_score", "value": "0.99"},
                {"key": "rating_count", "value": "7"},
            ],
        }
    )
    assert ad is not None
    assert ad.owner.rating == pytest.approx(4.95)
    assert ad.owner.rating_count == 7


def test_rating_absente_quand_attribut_manquant() -> None:
    ad = Ad.from_api({"list_id": 1, "subject": "RTX 3090", "attributes": []})
    assert ad is not None
    assert ad.owner.rating is None
    assert ad.owner.rating_count is None


def test_attributes_list_is_flattened_to_dict(raw_ads: list[dict], ads: list[Ad]) -> None:
    raw = raw_ads[0]
    assert isinstance(raw.get("attributes"), list)
    assert all(isinstance(a, dict) for a in raw["attributes"])
    ad = ads[0]
    assert isinstance(ad.attributes, dict)
    for key, value in ad.attributes.items():
        assert isinstance(key, str) and isinstance(value, str)
    expected = {
        str(a["key"]): str(a.get("value_label") or a.get("value"))
        for a in raw["attributes"]
        if a.get("key") and (a.get("value_label") or a.get("value")) is not None
    }
    assert ad.attributes == expected


def test_from_api_rejects_ad_without_int_list_id() -> None:
    assert Ad.from_api({"list_id": None, "subject": "x"}) is None
    assert Ad.from_api({"list_id": "1234", "subject": "x"}) is None
    assert Ad.from_api({}) is None


def test_from_api_falls_back_on_missing_url() -> None:
    ad = Ad.from_api({"list_id": 42, "subject": "  Carte graphique  "})
    assert ad is not None
    assert ad.url == "https://www.leboncoin.fr/ad/42"
    assert ad.subject == "Carte graphique"
    assert ad.attributes == {}


def test_from_api_joins_body_when_list() -> None:
    ad = Ad.from_api({"list_id": 7, "body": ["ligne 1", "ligne 2"]})
    assert ad is not None
    assert ad.body == "ligne 1 ligne 2"


def test_apply_user_card_from_real_payload(seller_payload: dict) -> None:
    owner = Owner(user_id="someone", type="private")
    owner.apply_user_card(seller_payload)
    # la fiche vendeur reale : overall_score = 1 -> note maximale, 3 avis, 100 % de réponse
    assert owner.rating == pytest.approx(5.0)
    assert owner.rating_count == 3
    assert owner.reply_rate == 100
    assert owner.reply_time == "1 heure"
    # apply_user_card ne touche ni l'identifiant ni le type deja connus
    assert owner.user_id == "someone"
    assert owner.type == "private"


def test_apply_user_card_without_feedback_keeps_rating_none() -> None:
    owner = Owner(user_id="someone")
    owner.apply_user_card({"account_type": "pro", "total_ads": "12"})
    assert owner.rating is None
    assert owner.rating_count is None
    assert owner.type == "pro"
    assert owner.total_ads == 12


def test_apply_user_card_does_not_override_existing_type() -> None:
    owner = Owner(type="private")
    owner.apply_user_card({"account_type": "pro"})
    assert owner.type == "private"


def test_apply_user_card_badges_and_reply(seller_payload: dict) -> None:
    owner = Owner()
    owner.apply_user_card(seller_payload)
    assert isinstance(owner.badges, tuple)
    assert all(isinstance(b, str) for b in owner.badges)
    assert owner.badges == ("Responsiveness3", "VerifiedPhoneNumber")
    reply = seller_payload.get("reply") or {}
    assert owner.reply_time == (reply.get("reply_time_text") or reply.get("rate_text"))


def test_seller_fixture_is_a_real_user_card_response(fixtures_dir) -> None:
    payload = json.loads((fixtures_dir / "seller.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert payload.get("id") or payload.get("user_id") or payload.get("name")


def test_owner_label() -> None:
    assert Owner(name="Alice", type="private").label.startswith("Alice")
    assert "pro" in Owner(name="Boutique", type="pro").label
