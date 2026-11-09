"""Transport HTTP : récupère les pages de recherche Leboncoin côté serveur.

Leboncoin protège son API JSON interne (DataDome). En revanche les pages HTML
`/recherche?...` sont servies normalement et contiennent tout le jeu de données
des annonces dans le bloc Next.js `__NEXT_DATA__`.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from ..models import Ad

log = logging.getLogger(__name__)

BASE_URL = "https://www.leboncoin.fr"
SEARCH_PATH = "/recherche"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


class BlockedError(RuntimeError):
    """Leboncoin a renvoyé une page anti-bot (403 / challenge)."""


class CaptchaError(BlockedError):
    """Leboncoin a servi un défi interactif (captcha)."""


class FetchError(RuntimeError):
    """Erreur réseau ou page inattendue."""


class Pacer:
    """Impose un délai aléatoire entre deux requêtes.

    Un seul objet de pacing est partagé par tous les clients (recherche et
    fiche vendeur) pour que le rythme global reste celui choisi dans la
    configuration.
    """

    def __init__(self, min_seconds: float, max_seconds: float) -> None:
        self.min_seconds = float(min_seconds)
        self.max_seconds = max(float(max_seconds), self.min_seconds)
        self._last = 0.0

    def wait(self) -> None:
        delay = random.uniform(self.min_seconds, self.max_seconds)
        remaining = self._last + delay - time.monotonic()
        if remaining > 0:
            log.debug("pause de %.1fs avant la prochaine requête", remaining)
            time.sleep(remaining)
        self._last = time.monotonic()


@dataclass(slots=True)
class SearchPage:
    ads: list[Ad]
    total: int | None
    max_pages: int | None
    applied_filters: dict


class HttpxScraper:
    """Transport principal : curl_cffi avec empreinte TLS Chrome."""

    name = "curl_cffi"

    def __init__(self, user_agent: str, impersonate: str = "chrome") -> None:
        from curl_cffi import requests as curl_requests

        self._session = curl_requests.Session()
        self._impersonate = impersonate
        self._user_agent = user_agent

    def get(self, url: str, params: dict[str, str]) -> str:
        resp = self._session.get(
            url,
            params=params,
            impersonate=self._impersonate,
            timeout=45,
            headers={
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
                "Referer": f"{BASE_URL}/",
            },
        )
        if resp.status_code == 403:
            raise BlockedError("403 (DataDome)")
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code}")
        return resp.text

    def get_json(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """GET d'une API JSON, avec la même empreinte TLS que pour le HTML."""
        resp = self._session.get(
            url,
            params=params or {},
            impersonate=self._impersonate,
            timeout=30,
            headers={
                "Accept": "application/json",
                "Accept-Language": "fr-FR,fr;q=0.9",
                "Referer": f"{BASE_URL}/",
            },
        )
        if resp.status_code == 403:
            raise BlockedError("API Leboncoin: 403 (DataDome)")
        if resp.status_code == 404:
            raise FetchError("API Leboncoin: 404")
        if resp.status_code >= 400:
            raise FetchError(f"API Leboncoin: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise FetchError("réponse JSON inattendue") from exc
        if not isinstance(data, dict):
            raise FetchError("réponse JSON inattendue")
        return data


class PlaywrightScraper:
    """Transport de secours : vrai navigateur, plus lent mais plus crédible."""

    name = "playwright"

    def __init__(self, user_agent: str) -> None:
        self._user_agent = user_agent
        self._browser = None
        self._pw = None

    def _ensure(self):
        if self._browser is not None:
            return self._browser
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        return self._browser

    def get(self, url: str, params: dict[str, str]) -> str:
        browser = self._ensure()
        context = browser.new_context(
            user_agent=self._user_agent,
            locale="fr-FR",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        try:
            query = "&".join(f"{k}={_quote(v)}" for k, v in params.items())
            page.goto(f"{url}?{query}", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("script#__NEXT_DATA__", timeout=20_000)
            return page.content()
        finally:
            context.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


def extract_next_data(html: str) -> dict:
    match = NEXT_DATA_RE.search(html)
    if not match:
        lowered = html[:4000].lower()
        if "captcha" in lowered or "datadome" in lowered:
            raise BlockedError("page de challenge anti-bot")
        raise FetchError("__NEXT_DATA__ absent de la page")
    return json.loads(match.group(1))


def parse_search_page(html: str) -> SearchPage:
    data = extract_next_data(html)
    page_props = (data.get("props") or {}).get("pageProps") or {}
    search = page_props.get("search") or {}
    search_data = page_props.get("searchData")
    if not isinstance(search_data, dict):
        # Page de recherche sans résultat : searchData peut manquer.
        if page_props.get("title") is None:
            raise FetchError("page inattendue (pas de searchData)")
        return SearchPage([], 0, 0, search.get("filters") or {})

    raw_ads = search_data.get("ads") or []
    ads = [ad for ad in (Ad.from_api(a) for a in raw_ads) if ad is not None]
    return SearchPage(
        ads=ads,
        total=search_data.get("total"),
        max_pages=search_data.get("max_pages"),
        applied_filters=search.get("filters") or {},
    )


def parse_ad_page(html: str) -> Ad | None:
    """Extrait l'annonce de la fiche ``/ad/...``.

    Seule cette page contient la description complète (``body``) : la payload de
    recherche ne la fournit jamais. Elle a la même forme qu'une annonce de
    recherche, mais rangée dans ``props.pageProps.ad``.
    """
    page_props = extract_next_data(html).get("props", {}).get("pageProps") or {}
    raw = page_props.get("ad")
    if not isinstance(raw, dict):
        raise FetchError("page d'annonce inattendue (props.pageProps.ad absent)")
    return Ad.from_api(raw)


def verify_applied(search_config, applied: dict) -> list[str]:
    """Contrôle que Leboncoin a bien pris en compte les filtres demandés.

    Les mauvais noms de paramètres sont ignorés silencieusement : sans ce contrôle
    on recevrait des annonces hors sujet.
    """
    problems: list[str] = []
    filters = applied or {}
    keywords = (filters.get("keywords") or {}).get("text")
    if search_config.text and (keywords or "").strip().lower() != search_config.text.strip().lower():
        problems.append(
            f"filtre texte non appliqué (demandé '{search_config.text}', "
            f"obtenu '{keywords}')"
        )
    category = (filters.get("category") or {}).get("id")
    if search_config.category and str(category) != str(search_config.category):
        problems.append(
            f"filtre catégorie non appliqué (demandé {search_config.category}, "
            f"obtenu {category})"
        )
    price = filters.get("ranges") or {}
    if search_config.price_min is not None or search_config.price_max is not None:
        got = price.get("price") or {}
        if search_config.price_min is not None and got.get("min") != search_config.price_min:
            problems.append(f"filtre prix min non appliqué (obtenu {got.get('min')})")
        if search_config.price_max is not None and got.get("max") != search_config.price_max:
            problems.append(f"filtre prix max non appliqué (obtenu {got.get('max')})")
    return problems


class LeboncoinClient:
    """Client de recherche : pagination, politesse, re-essai et repli navigateur."""

    def __init__(
        self,
        *,
        user_agent: str,
        impersonate: str = "chrome",
        request_delay_min: float = 4.0,
        request_delay_max: float = 9.0,
        max_retries: int = 3,
        backoff_base_min: int = 45,
        playwright_fallback: bool = True,
    ) -> None:
        self._http = HttpxScraper(user_agent, impersonate)
        self._browser_scraper: PlaywrightScraper | None = None
        self._playwright_fallback = playwright_fallback
        self._pacer = Pacer(request_delay_min, request_delay_max)
        self._max_retries = max_retries
        self._backoff_base_min = backoff_base_min

    @property
    def http(self) -> HttpxScraper:
        """Transport partagé (utile pour l'API user-card des vendeurs)."""
        return self._http

    @property
    def pacer(self) -> Pacer:
        """Cadenceuse partagée entre recherche et fiches vendeur."""
        return self._pacer

    def _polite_pause(self) -> None:
        self._pacer.wait()

    def _fetch(self, params: dict[str, str]) -> str:
        return self._fetch_url(BASE_URL + SEARCH_PATH, params)

    def _fetch_url(self, url: str, params: dict[str, str]) -> str:
        """Charge une page, en ré-essayant puis en basculant sur le navigateur."""
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            self._polite_pause()
            try:
                return self._http.get(url, params)
            except (BlockedError, FetchError) as exc:
                last_error = exc
                log.warning("échec du transport HTTP (%s), tentative %d/%d", exc, attempt, self._max_retries)
                if attempt < self._max_retries:
                    time.sleep(min(self._backoff_base_min * 60 * attempt, 900))
        if self._playwright_fallback:
            log.info("bascule sur le repli navigateur (Playwright)")
            if self._browser_scraper is None:
                try:
                    self._browser_scraper = PlaywrightScraper(self._http._user_agent)
                except ImportError:
                    raise BlockedError(
                        "Leboncoin bloque le transport HTTP ; installez le repli "
                        "navigateur avec : pip install '.[playwright]' puis "
                        "`playwright install chromium`"
                    ) from last_error
            self._polite_pause()
            return self._browser_scraper.get(url, params)
        assert last_error is not None
        raise last_error

    def search(self, search_config, *, max_pages: int | None = None) -> list[Ad]:
        params = search_config.as_query()
        ads: dict[int, Ad] = {}
        page = 1
        limit = max_pages or search_config.pages or 1
        while page <= limit:
            page_params = dict(params)
            if page > 1:
                page_params["page"] = str(page)
            parsed = parse_search_page(self._fetch(page_params))
            problems = verify_applied(search_config, parsed.applied_filters)
            if problems:
                raise FetchError(
                    f"recherche '{search_config.name}': " + "; ".join(problems)
                )
            for ad in parsed.ads:
                ads.setdefault(ad.list_id, ad)
            log.info(
                "recherche '%s' page %d/%s : %d annonces (total site %s)",
                search_config.name,
                page,
                parsed.max_pages if parsed.max_pages is not None else "?",
                len(parsed.ads),
                parsed.total,
            )
            if parsed.max_pages is not None:
                limit = min(limit, parsed.max_pages)
            if not parsed.ads:
                break
            page += 1
        return list(ads.values())

    def ad_detail(self, ad_url: str) -> Ad | None:
        """Récupère la fiche d'une annonce : seule source de la description.

        Peut lever ``BlockedError`` quand Leboncoin durcit la protection.
        """
        return parse_ad_page(self._fetch_url(ad_url, {}))

    def close(self) -> None:
        if self._browser_scraper is not None:
            self._browser_scraper.close()
            self._browser_scraper = None
