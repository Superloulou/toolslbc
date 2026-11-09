"""Configuration : recherches nommées + réglages globaux."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_NAMES = ("config.yml", "config.yaml", "config.json")

#: User-Agent envoyé à Leboncoin. Doit rester cohérent avec l'empreinte TLS
#: simulée par ``curl_cffi`` (``impersonate``), sinon DataDome bloque.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class SearchConfig:
    """Une recherche = une ligne de recherche Leboncoin + ses filtres locaux."""

    name: str
    text: str | None = None
    category: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    enums: dict[str, list[str]] = field(default_factory=dict)
    locations: list[str] = field(default_factory=list)
    max_age_hours: float | None = 72.0
    require: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    seller_types: list[str] = field(default_factory=list)
    min_rating: float | None = None
    pages: int = 1

    def validate(self) -> None:
        if not self.text and not self.category:
            raise ValueError(
                f"recherche '{self.name}': il faut au moins 'text' ou 'category'"
            )
        if self.price_min is not None and self.price_max is not None:
            if self.price_min > self.price_max:
                raise ValueError(f"recherche '{self.name}': price_min > price_max")
        if self.max_age_hours is not None and self.max_age_hours <= 0:
            raise ValueError(f"recherche '{self.name}': max_age_hours doit être > 0")
        for field_name, value in (
            ("require", self.require),
            ("exclude", self.exclude),
            ("seller_types", self.seller_types),
            ("locations", self.locations),
        ):
            if not isinstance(value, list):
                raise ValueError(
                    f"recherche '{self.name}': liste attendue pour '{field_name}'"
                )

    def as_query(self) -> dict[str, str]:
        """Paramètres d'URL réellement compris par leboncoin.fr."""
        q: dict[str, str] = {}
        if self.text:
            q["text"] = self.text
        if self.category:
            q["category"] = str(self.category)
        if self.price_min is not None or self.price_max is not None:
            lo = self.price_min if self.price_min is not None else ""
            hi = self.price_max if self.price_max is not None else ""
            q["price"] = f"{lo}-{hi}"
        for key, values in self.enums.items():
            q[key] = ",".join(values)
        if self.locations:
            q["locations"] = ",".join(self.locations)
        return q


@dataclass(slots=True)
class Settings:
    poll_interval_min: int = 60
    poll_jitter_min: int = 15
    request_delay_min: float = 4.0
    request_delay_max: float = 9.0
    max_retries: int = 3
    backoff_base_min: int = 45
    db_path: str = "data/state.db"
    user_agent: str = DEFAULT_USER_AGENT
    impersonate: str = "chrome"
    use_playwright_fallback: bool = True
    #: Récupère la fiche vendeur (note, taux de réponse, badges) pour les
    #: annonces nouvelles dont la note n'est pas déjà dans les résultats.
    seller_lookup: bool = True
    #: Récupère la description de chaque annonce nouvelle en ouvrant sa fiche
    #: (les résultats de recherche ne contiennent jamais le texte de l'annonce).
    description_lookup: bool = True
    #: Plafond de fiches ouvertes par cycle : chaque requête coûte 4 à 9 s.
    description_max: int = 25
    telegram_parse_mode: str = "HTML"
    notify_on_first_run: bool = False
    searches: list[SearchConfig] = field(default_factory=list)

    @property
    def db_file(self) -> Path:
        return Path(self.db_path)


def _load_search(raw: dict) -> SearchConfig:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("une recherche sans 'name' dans la configuration")
    sc = SearchConfig(
        name=name,
        text=raw.get("text"),
        category=raw.get("category"),
        price_min=raw.get("price_min"),
        price_max=raw.get("price_max"),
        enums=dict(raw.get("enums") or {}),
        locations=list(raw.get("locations") or []),
        max_age_hours=raw.get("max_age_hours", 72.0),
        require=[str(x).lower() for x in (raw.get("require") or [])],
        exclude=[str(x).lower() for x in (raw.get("exclude") or [])],
        seller_types=[str(x).lower() for x in (raw.get("seller_types") or [])],
        min_rating=raw.get("min_rating"),
        pages=int(raw.get("pages", 1)),
    )
    sc.validate()
    return sc


def load_config(
    path: str | os.PathLike[str],
    data_dir: str | os.PathLike[str] | None = None,
) -> Settings:
    """Charge et valide la configuration YAML.

    ``data_dir`` (optionnel) déplace la base SQLite. Une ``db_path`` relative
    est résolue à côté du fichier de configuration, ce qui permet de lancer le
    démon depuis n'importe quel répertoire courant.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"configuration introuvable: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("la configuration doit être un dictionnaire YAML")

    db_path = Path(str(data.get("db_path", "data/state.db")))
    if data_dir is not None:
        db_path = Path(data_dir) / "state.db"
    elif not db_path.is_absolute():
        db_path = p.parent / db_path

    scraper = data.get("scraper") or {}
    notify = data.get("notify") or {}
    settings = Settings(
        poll_interval_min=int(data.get("poll_interval_min", 60)),
        poll_jitter_min=int(data.get("poll_jitter_min", 15)),
        request_delay_min=float(scraper.get("request_delay_min", 4.0)),
        request_delay_max=float(scraper.get("request_delay_max", 9.0)),
        max_retries=int(scraper.get("max_retries", 3)),
        backoff_base_min=int(scraper.get("backoff_base_min", 45)),
        db_path=str(db_path),
        user_agent=str(scraper.get("user_agent", DEFAULT_USER_AGENT)),
        impersonate=str(scraper.get("impersonate", "chrome")),
        use_playwright_fallback=bool(scraper.get("playwright_fallback", True)),
        seller_lookup=bool(scraper.get("seller_lookup", True)),
        description_lookup=bool(scraper.get("description_lookup", True)),
        description_max=int(scraper.get("description_max", 25)),
        telegram_parse_mode=str(notify.get("parse_mode", "HTML")),
        notify_on_first_run=bool(notify.get("notify_on_first_run", False)),
        searches=[_load_search(s) for s in (data.get("searches") or [])],
    )
    if not settings.searches:
        raise ValueError("aucune recherche définie dans la configuration")
    if settings.poll_interval_min < 30:
        raise ValueError("poll_interval_min < 30 : risque de blocage trop élevé")
    return settings


def default_config_path(root: Path) -> Path | None:
    for name in DEFAULT_CONFIG_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


_SAFE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    return _SAFE.sub("-", value.lower()).strip("-")
