"""Rendu des messages Telegram (porté de la sonde ``t_msg.py``).

Aucun appel réseau : ``TelegramNotifier`` n'est exercé que dans son chemin
« non configuré », qui retourne ``False`` sans ouvrir de connexion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_ad
from lbc_alertes.models import Ad, Owner
from lbc_alertes.notifier import (
    TelegramNotifier,
    build_message,
    esc,
    format_price,
    human_age,
)

NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _owner(**overrides) -> Owner:
    kwargs = dict(user_id="u-1", type="private", name="Alice")
    kwargs.update(overrides)
    return Owner(**kwargs)


# --------------------------------------------------------------------------
# esc()
# --------------------------------------------------------------------------


def test_esc_escapes_html_special_chars() -> None:
    assert esc("A & B <b> c") == "A &amp; B &lt;b&gt; c"


def test_esc_escapes_ampersand_first() -> None:
    """Le `&` est échappé en premier : les entités ne sont pas doublées."""

    assert esc("<") == "&lt;"
    assert esc("&lt;") == "&amp;lt;"


def test_esc_leaves_plain_text_untouched() -> None:
    assert esc("RTX 3090 - 1 000 €") == "RTX 3090 - 1 000 €"


# --------------------------------------------------------------------------
# format_price()
# --------------------------------------------------------------------------


def test_format_price_groups_thousands() -> None:
    """`format_price` prend un `Ad` (pas un int) : 1000 -> « 1 000 € »."""

    assert format_price(make_ad(price=1000)) == "1 000 €"


def test_format_price_groups_six_digits() -> None:
    """Comportement réel : 100000 -> « 100 000 € » (pas « 1 000 € »)."""

    assert format_price(make_ad(price=100000)) == "100 000 €"


def test_format_price_none() -> None:
    assert format_price(make_ad(price=None)) == "prix non précisé"


def test_format_price_zero_is_free() -> None:
    assert format_price(make_ad(price=0)) == "gratuit"


def test_format_price_small_value_has_no_separator() -> None:
    assert format_price(make_ad(price=250)) == "250 €"


# --------------------------------------------------------------------------
# human_age()
# --------------------------------------------------------------------------


def test_human_age_uses_aware_datetime() -> None:
    assert human_age(NOW - timedelta(minutes=5), now=NOW) == "il y a 5 min"


def test_human_age_hours() -> None:
    assert human_age(NOW - timedelta(hours=5), now=NOW) == "il y a 5 h"


def test_human_age_days() -> None:
    assert human_age(NOW - timedelta(days=3), now=NOW) == "il y a 3 j"


def test_human_age_boundary_48h_is_in_days() -> None:
    assert human_age(NOW - timedelta(hours=48), now=NOW) == "il y a 2 j"


def test_human_age_none_is_unknown() -> None:
    assert human_age(None, now=NOW) == "date inconnue"


def test_human_age_naive_datetime_does_not_raise() -> None:
    """Une date naïve est coercée en UTC : pas de TypeError à la soustraction."""

    naive = datetime(2025, 6, 1, 11, 30, 0)
    assert human_age(naive, now=NOW) == "il y a 30 min"
    # Le seuil « minutes » est strictement inférieur à 60 min : à 60 min pile
    # on bascule déjà sur le format heures.
    assert human_age(datetime(2025, 6, 1, 11, 0, 0), now=NOW) == "il y a 1 h"


# --------------------------------------------------------------------------
# build_message()
# --------------------------------------------------------------------------


def test_build_message_renders_every_section() -> None:
    ad = make_ad(
        list_id=3296282602,
        subject="Rtx 3090 FE",
        price=1000,
        published=NOW - timedelta(hours=5),
        owner=_owner(),
        location="Paris, 75",
    )
    msg = build_message("rtx 3090", ad, now=NOW)
    assert "🆕 <b>Rtx 3090 FE</b>" in msg
    assert "💰 1 000 €" in msg
    # ``build_message`` accepte une horloge injectable (``now``) : l'âge relatif
    # est déterministe, comme le reste du message.
    assert "🕐 publiée il y a 5 h (01/06/2025 07:00)" in msg
    assert "👤 Alice · particulier" in msg
    assert "📍 Paris, 75" in msg
    assert "🔎 recherche : rtx 3090" in msg
    assert "<a href=\"https://www.leboncoin.fr/ad/3296282602\">Voir l'annonce</a>" in msg


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(minutes=5), "🕐 publiée il y a 5 min"),
        (timedelta(hours=5), "🕐 publiée il y a 5 h"),
        (timedelta(days=3), "🕐 publiée il y a 3 j"),
    ],
)
def test_build_message_age_uses_injected_clock(delta: timedelta,
                                               expected: str) -> None:
    """L'âge affiché dépend de l'horloge passée, pas de la date du jour."""
    ad = make_ad(subject="Rtx 3090", published=NOW - delta, owner=_owner())
    assert expected in build_message("rtx 3090", ad, now=NOW)


def test_notify_forwards_clock_to_message() -> None:
    """``notify`` propage l'horloge jusqu'au message rendu."""
    seen: list[str] = []

    class FakeNotifier(TelegramNotifier):
        def send(self, text: str, photo_url: str | None = None) -> bool:
            seen.append(text)
            return True

    notifier = FakeNotifier("x", "y")
    ad = make_ad(subject="Rtx 3090", published=NOW - timedelta(minutes=7),
                 owner=_owner())
    assert notifier.notify("rtx 3090", [ad], now=NOW) == 1
    assert "🕐 publiée il y a 7 min" in seen[0]


def test_build_message_rating_absent() -> None:
    ad = make_ad(subject="Rtx 3090", published=NOW, owner=_owner())
    assert "⭐ note vendeur : non publiée" in build_message("rtx 3090", ad)


def test_build_message_rating_published() -> None:
    ad = make_ad(
        subject="Rtx 3090",
        published=NOW,
        owner=_owner(rating=5.0, rating_count=3),
    )
    msg = build_message("rtx 3090", ad)
    assert "⭐ 5.0/5 (3 avis)" in msg
    assert "note vendeur : non publiée" not in msg


def test_build_message_rating_without_count() -> None:
    ad = make_ad(subject="x", published=NOW, owner=_owner(rating=4.5))
    assert "⭐ 4.5/5" in build_message("s", ad)


def test_build_message_pro_link_without_rating() -> None:
    ad = make_ad(
        subject="x",
        published=NOW,
        owner=_owner(type="pro", pro_rates_link="https://x/avis"),
    )
    msg = build_message("s", ad)
    assert "<a href=\"https://x/avis\">avis du vendeur pro</a>" in msg
    assert "note vendeur : non publiée" not in msg


def test_build_message_unknown_date() -> None:
    ad = make_ad(subject="x", published=NOW, owner=_owner())
    ad.first_publication_date = None
    ad.index_date = None
    msg = build_message("s", ad)
    assert "🕐 date de publication inconnue" in msg


def test_build_message_escapes_subject() -> None:
    ad = make_ad(subject="<b>RTX</b> & co", published=NOW, owner=_owner())
    msg = build_message("s", ad)
    assert "&lt;b&gt;RTX&lt;/b&gt; &amp; co" in msg
    assert "<b><b>" not in msg


def test_build_message_has_no_blank_lines() -> None:
    ad = make_ad(subject="x", published=NOW, owner=_owner())
    msg = build_message("s", ad)
    assert "\n\n" not in msg
    assert msg.strip() == msg


def test_build_message_on_real_fixture_ads(ads: list[Ad]) -> None:
    """Les 35 annonces réelles se rendent toutes sans erreur."""

    rated = [a for a in ads if a.owner.rating is not None]
    unrated = [a for a in ads if a.owner.rating is None]
    assert rated and unrated
    assert "⭐" in build_message("rtx 3090", rated[0])
    assert "note vendeur : non publiée" in build_message("rtx 3090", unrated[0])


def test_build_message_after_user_card(seller_payload: dict) -> None:
    """Reproduit `t_msg.py` : la carte vendeur fait apparaître la note."""

    owner = Owner(store_id=23159180, user_id="x", type="private", name="vendeur test")
    owner.apply_user_card(seller_payload)
    ad = make_ad(subject="Rtx 3090", published=NOW, owner=owner)
    msg = build_message("rtx 3090", ad)
    assert "⭐ 5.0/5 (3 avis)" in msg
    assert "👤 vendeur test · particulier" in msg


# --------------------------------------------------------------------------
# TelegramNotifier (chemin hors réseau uniquement)
# --------------------------------------------------------------------------


def test_notifier_not_configured_without_credentials() -> None:
    assert TelegramNotifier("", "").configured is False


def test_notifier_configured_with_credentials() -> None:
    assert TelegramNotifier("token", "42").configured is True


def test_notifier_send_returns_false_when_unconfigured() -> None:
    """Aucun réseau : `send()` court-circuite quand les identifiants manquent."""

    notifier = TelegramNotifier("", "")
    ad = make_ad(subject="x", published=NOW, owner=_owner())
    assert notifier.send("rtx 3090", ad) is False


def test_notifier_notify_returns_zero_when_unconfigured() -> None:
    notifier = TelegramNotifier("", "")
    ad = make_ad(subject="x", published=NOW, owner=_owner())
    assert notifier.notify("rtx 3090", [ad]) == 0


@pytest.mark.parametrize("token,chat_id", [("t", ""), ("", "c")])
def test_notifier_requires_both_token_and_chat(token: str, chat_id: str) -> None:
    assert TelegramNotifier(token, chat_id).configured is False
