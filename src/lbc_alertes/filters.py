"""Filtrage local des annonces.

Leboncoin applique les filtres d'URL (texte, catégorie, prix) mais pas le tri par
date ni les filtres fins. Tout ce qui suit est donc fait côté client, sur le champ
`first_publication_date` renvoyé par la page.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import SearchConfig
from .models import Ad

# Attributs techniques (logistique, URLs, booléens, identifiants, note vendeur)
# qui ne portent aucun sens textuel : ils polluent la recherche de mots-clés.
_NOISE_ATTRS = frozenset(
    {
        "is_import",
        "shippable",
        "shipping_type",
        "shipping_cost",
        "estimated_parcel_weight",
        "estimated_parcel_size",
        "purchase_cta_visible",
        "negotiation_cta_visible",
        "is_bundleable",
        "country_isocode3166",
        "profile_picture_url",
        "store_logo",
        "store_name",
        "online_store_id",
        "external_ad_id",
        "custom_ref",
        "ean",
        "argus_object_id",
        "street_view_url",
        "pro_rates_link",
        "rating_score",
        "rating_count",
        "has_visibility_option",
        "visibility_option_products",
        "district_id",
        "district_visibility",
        "district_type_id",
        "district_resolution_type",
        "old_price",
        "new_item_price",
        "payment_methods",
        "donation",
        "is_eligible_to_warranty",
        "ad_warranty_type",
        "stock_quantity",
        "spare_parts_availability",
        "licence_plate_available",
        "specificities",
        "mandate_type",
        "immo_sell_type",
        "floor_display",
        "charges_included",
        "real_estate_charges_details",
        "price_per_square_meter",
        "price_except_fees",
        "fees_price",
        "fees_at_the_expanse_of",
        "estimated_notary_fees",
        "estimated_total_property_price",
        "annual_energy_budget_min",
        "annual_energy_budget_max",
        "annual_charges",
        "monthly_charges",
        "security_deposit",
        "is_public_rent_control",
        "rent_excluding_charges",
        "lease_type",
        "nb_floors_building",
        "car_price_min",
        "car_price_max",
        "car_price_positioning",
        "recent_used_vehicle",
        "vehicle_available_payment_methods",
        "vehicle_is_eligible_p2p",
        "vehicle_history_report_public_url",
        "vehicle_history_report_status",
        "vehicle_technical_inspection_valid",
        "vehicle_commercial_warranty",
        "vehicle_commercial_warranty_duration",
        "vehicle_damage",
        "vehicle_history",
        "vehicle_interior_specs",
        "furniture_dismountable",
        "furniture_quantity",
        "critair",
    }
)


@dataclass
class Rejected:
    ad: Ad
    reason: str


@dataclass
class FilterResult:
    kept: list[Ad] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def _norm(text: str) -> str:
    """Minuscule sans accents : 'DDR4 32 Go' == 'ddr4 32go', "j'achète" == 'jachete'."""
    return re.sub(r"[^a-z0-9]+", " ", _strip_accents(text.lower())).strip()


def _tokens(needle: str) -> list[str]:
    return [t for t in _norm(needle).split() if t]


def _matches_all(hay: str, needle: str) -> bool:
    """Tous les mots de `needle` sont présents dans `hay` (ordre libre)."""
    return all(tok in hay for tok in _tokens(needle))


def _ad_haystack(ad: Ad) -> str:
    """Texte normalisé dans lequel on cherche les mots-clés.

    ``ad.attributes`` est un dictionnaire ``{clé: libellé}`` (aplati par
    ``Ad.from_api``) : on n'ignore que les attributs purement logistiques pour
    ne pas faire de fausses correspondances.
    """
    parts = [ad.subject, ad.category_name, ad.body[:400]]
    parts += [
        f"{key} {value}"
        for key, value in ad.attributes.items()
        if key not in _NOISE_ATTRS
    ]
    return _norm(" ".join(p for p in parts if p))


def age_hours(ad: Ad, now: datetime) -> float | None:
    dt = ad.publication_date
    if dt is None:
        return None
    if dt.tzinfo is None:  # sécurité : date enregistrée sans fuseau
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def check(ad: Ad, cfg: SearchConfig, now: datetime | None = None) -> str | None:
    """Renvoie la raison du rejet, ou None si l'annonce convient."""
    now = now or datetime.now(timezone.utc)

    max_age = cfg.max_age_hours
    if max_age:
        hours = age_hours(ad, now)
        if hours is None:
            return "date de publication inconnue"
        if hours > max_age:
            return f"trop ancienne ({hours:.0f} h > {max_age} h)"

    hay = _ad_haystack(ad)
    for required in cfg.require:
        if not _matches_all(hay, required):
            return f"mot manquant : « {required} »"
    for excluded in cfg.exclude:
        if _matches_all(hay, excluded):
            return f"mot exclu : « {excluded} »"

    if cfg.seller_types and ad.owner.type not in cfg.seller_types:
        return f"vendeur {ad.owner.type or 'inconnu'} non autorisé"

    if cfg.min_rating is not None and ad.owner.rating is not None:
        if ad.owner.rating < cfg.min_rating:
            return f"note vendeur {ad.owner.rating} < {cfg.min_rating}"

    return None


def apply(ads: list[Ad], cfg: SearchConfig, now: datetime | None = None) -> FilterResult:
    res = FilterResult()
    for ad in ads:
        reason = check(ad, cfg, now)
        if reason is None:
            res.kept.append(ad)
        else:
            res.rejected.append(Rejected(ad, reason))
    res.kept.sort(key=lambda a: a.publication_date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return res


def stale_cutoff(hours: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)
