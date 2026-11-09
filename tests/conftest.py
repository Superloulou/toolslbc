"""Fixtures partagées par la suite de tests.

Aucun test ne touche le réseau : on repart des réponses réelles capturées dans
``tests/fixtures/``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lbc_alertes.models import Ad, Owner

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
PROJECT_ROOT = TESTS_DIR.parent

NEXT_DATA_RE = re.compile(
    r'id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def search_html_path() -> Path:
    return FIXTURES_DIR / "search_cat83.html"


@pytest.fixture(scope="session")
def search_html(search_html_path: Path) -> str:
    return search_html_path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="session")
def seller_payload() -> dict:
    return json.loads((FIXTURES_DIR / "seller.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def raw_ads(search_html: str) -> list[dict]:
    """Les 35 annonces brutes du blob ``__NEXT_DATA__`` d'une vraie page."""
    match = NEXT_DATA_RE.search(search_html)
    assert match, "le fixture HTML ne contient pas de blob __NEXT_DATA__"
    data = json.loads(match.group(1))
    ads = data["props"]["pageProps"]["searchData"]["ads"]
    assert isinstance(ads, list) and ads
    return ads


@pytest.fixture(scope="session")
def ads(raw_ads: list[dict]) -> list[Ad]:
    return [Ad.from_api(raw) for raw in raw_ads]


_UNSET = object()


def make_ad(
    *,
    list_id: int = 1234567890,
    subject: str = "RTX 3090",
    price: int | None = 1000,
    body: str = "",
    attributes: dict[str, str] | None = None,
    published: datetime | None = _UNSET,
    owner: Owner | None = None,
    **kwargs,
) -> Ad:
    """Construit une ``Ad`` avec tous les champs obligatoires (dont ``index_date``).

    ``published`` omis = maintenant ; ``published=None`` = annonce sans date
    (le champ ``publication_date`` de ``Ad`` retombe sur ``index_date``).
    """
    when = datetime.now(timezone.utc) if published is _UNSET else published
    return Ad(
        list_id=list_id,
        subject=subject,
        url=f"https://www.leboncoin.fr/ad/{list_id}",
        price=price,
        category_id=kwargs.pop("category_id", 83),
        category_name=kwargs.pop("category_name", "Informatique"),
        first_publication_date=when,
        index_date=when,
        owner=owner or Owner(user_id="u-1", type="private", name="Alice"),
        location=kwargs.pop("location", "Paris, 75"),
        thumbnail=kwargs.pop("thumbnail", None),
        ad_type=kwargs.pop("ad_type", "offer"),
        body=body,
        attributes=attributes if attributes is not None else {"condition": "Bon état"},
    )
