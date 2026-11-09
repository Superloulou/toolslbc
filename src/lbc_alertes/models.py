"""Domain model for a Leboncoin ad."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

DATE_FMT = "%Y-%m-%d %H:%M:%S"

try:  # les dates Leboncoin sont exprimées en heure de France métropolitaine
    from zoneinfo import ZoneInfo

    LBC_TZ = ZoneInfo("Europe/Paris")
except Exception:  # tzdata indisponible sur la machine
    LBC_TZ = timezone.utc


def _parse_dt(raw: str | None) -> datetime | None:
    """Convertit une date Leboncoin ("2026-11-06 18:03:11") en datetime *aware*.

    Le site n'indique pas le fuseau ; on suppose Europe/Paris. Rendre la date
    aware évite les erreurs du type « can't subtract offset-naive and
    offset-aware datetimes » quand on la compare à datetime.now(timezone.utc).
    """
    if not raw:
        return None
    try:
        return datetime.strptime(raw, DATE_FMT).replace(tzinfo=LBC_TZ)
    except ValueError:
        return None


def _first_price(price: object) -> int | None:
    """`price` is a list in the API payload (e.g. [140000]); price_cents is an int."""
    if isinstance(price, list) and price and isinstance(price[0], int):
        return price[0]
    if isinstance(price, int):
        return price
    return None


def _to_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class Owner:
    store_id: int | None = None
    user_id: str | None = None
    type: str | None = None  # "private" | "pro"
    name: str | None = None
    siren: str | None = None
    pro_rates_link: str | None = None
    activity_sector: str | None = None
    rating: float | None = None       # note sur 5, renseignée par l'API user-card
    rating_count: int | None = None   # nombre d'avis reçus
    reply_rate: int | None = None     # taux de réponse en %
    reply_time: str | None = None     # "Répond en moyenne en 1 heure"
    badges: tuple[str, ...] = ()
    registered_at: str | None = None
    total_ads: int | None = None

    @property
    def label(self) -> str:
        parts = [self.name or "vendeur inconnu"]
        if self.type:
            parts.append("pro" if self.type == "pro" else "particulier")
        return " · ".join(parts)

    def apply_user_card(self, payload: dict) -> None:
        """Merge la réponse de `api/user-card/v2/<user_id>/infos` dans le vendeur.

        `feedback.overall_score` est une fraction 0..1 (0.99 = 4,95/5). Beaucoup de
        vendeurs n'ont aucun bloc `feedback` : la note reste alors `None`.
        """
        feedback = payload.get("feedback") or {}
        score = feedback.get("overall_score")
        if score is not None:
            try:
                value = float(score)
            except (TypeError, ValueError):
                value = None
            if value is not None:
                self.rating = round(value * 5 if value <= 1.0 else value, 2)
        self.rating_count = _to_int(feedback.get("received_count"))

        reply = payload.get("reply") or {}
        self.reply_rate = _to_int(reply.get("rate"))
        self.reply_time = reply.get("reply_time_text") or reply.get("rate_text")

        self.badges = tuple(
            str(b.get("name"))
            for b in (payload.get("badges") or [])
            if isinstance(b, dict) and b.get("name")
        )
        self.registered_at = payload.get("registered_at")
        self.total_ads = _to_int(payload.get("total_ads"))
        if not self.type and payload.get("account_type"):
            self.type = str(payload["account_type"])


@dataclass(slots=True)
class Ad:
    list_id: int
    subject: str
    url: str
    price: int | None
    category_id: int | None
    category_name: str | None
    first_publication_date: datetime | None
    index_date: datetime | None
    owner: Owner
    location: str | None = None
    thumbnail: str | None = None
    ad_type: str | None = None
    body: str = ""
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def publication_date(self) -> datetime | None:
        """Date de première publication, la plus fiable pour juger de la fraîcheur."""
        return self.first_publication_date or self.index_date

    @classmethod
    def from_api(cls, raw: dict) -> "Ad | None":
        list_id = raw.get("list_id")
        if not isinstance(list_id, int):
            return None
        owner_raw = raw.get("owner") or {}
        owner = Owner(
            store_id=owner_raw.get("store_id"),
            user_id=owner_raw.get("user_id"),
            type=owner_raw.get("type"),
            name=owner_raw.get("name"),
            siren=owner_raw.get("siren"),
            pro_rates_link=owner_raw.get("pro_rates_link"),
            activity_sector=owner_raw.get("activity_sector"),
        )
        loc = raw.get("location") or {}
        location = ", ".join(
            str(v) for v in (loc.get("city_label") or loc.get("city"), loc.get("department_name")) if v
        ) or None
        images = raw.get("images") or {}
        attrs: dict[str, str] = {}
        for attr in raw.get("attributes") or []:
            key = attr.get("key")
            value = attr.get("value_label") or attr.get("value")
            if key and value is not None:
                attrs[str(key)] = str(value)
        # La note du vendeur est déjà présente dans les attributs de la fiche
        # (rating_score sur 0..1, rating_count = nombre d'avis) : pas de requête
        # supplémentaire dans le cas général.
        score = attrs.get("rating_score")
        if score:
            try:
                value = float(str(score).replace(",", "."))
            except ValueError:
                value = None
            if value is not None:
                owner.rating = round(value * 5 if value <= 1.0 else value, 2)
        owner.rating_count = owner.rating_count or _to_int(attrs.get("rating_count"))
        owner.pro_rates_link = owner.pro_rates_link or attrs.get("pro_rates_link")

        raw_body = raw.get("body")
        if isinstance(raw_body, list):
            body = " ".join(str(part) for part in raw_body)
        else:
            body = str(raw_body or "")
        return cls(
            list_id=list_id,
            subject=str(raw.get("subject") or "").strip(),
            url=str(raw.get("url") or f"https://www.leboncoin.fr/ad/{list_id}"),
            price=_first_price(raw.get("price")),
            category_id=raw.get("category_id"),
            category_name=raw.get("category_name"),
            first_publication_date=_parse_dt(raw.get("first_publication_date")),
            index_date=_parse_dt(raw.get("index_date")),
            owner=owner,
            location=location,
            thumbnail=images.get("thumb_url") or images.get("small_url"),
            ad_type=raw.get("ad_type"),
            body=body,
            attributes=attrs,
        )
