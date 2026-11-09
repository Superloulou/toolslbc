"""Orchestration : une recherche = requête -> filtres -> déduplication -> Telegram."""

from __future__ import annotations

import logging
import random
import re
import signal
import time
from dataclasses import dataclass, field

from . import filters
from .ai import Analyzer
from .config import Settings
from .models import Ad
from .notifier import TelegramNotifier
from .scraper.leboncoin import BlockedError, LeboncoinClient
from .scraper.seller import SellerClient
from .store import Store

log = logging.getLogger("lbc")

#: Les annonces vues sont oubliées au bout de ce délai (jours) pour que la base reste petite.
RETENTION_DAYS = 45


@dataclass
class CycleStats:
    fetched: int = 0
    kept: int = 0
    new: int = 0
    searches: int = 0
    blocked: bool = False
    #: Annonces du cycle soumises au tri IA, et celles que l'IA a retenues.
    ai_checked: int = 0
    ai_retained: int = 0
    rejected_samples: list[str] = field(default_factory=list)


_REASON_DETAIL_RE = re.compile(r"\s*\(.*?\)\s*$")


def _reason_breakdown(rejected: list[filters.Rejected]) -> list[tuple[str, int]]:
    """Regroupe les motifs de rejet identiques (détail chiffré retiré)."""
    counter: dict[str, int] = {}
    for item in rejected:
        key = _REASON_DETAIL_RE.sub("", item.reason)
        counter[key] = counter.get(key, 0) + 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def _enrich_ratings(sellers: SellerClient, ads: list, search) -> list:
    """Complète la note des vendeurs non notés, puis applique min_rating.

    La note est déjà présente dans la plupart des résultats de recherche ; on
    n'appelle la fiche vendeur que pour les annonces qui nous intéressent
    vraiment (nouvelles), et une seule fois par vendeur (cache).
    """
    kept: list = []
    for ad in ads:
        if ad.owner.rating is None and ad.owner.user_id:
            try:
                sellers.enrich(ad.owner)
            except BlockedError:
                raise
            except Exception as exc:  # noqa: BLE001 - la note reste facultative
                log.debug("[%s] fiche vendeur indisponible : %s", ad.owner.user_id, exc)
        if (
            search.min_rating is not None
            and ad.owner.rating is not None
            and ad.owner.rating < search.min_rating
        ):
            log.info(
                "[%s] écartée après fiche vendeur : note %.2f < %.2f — %s",
                search.name,
                ad.owner.rating,
                search.min_rating,
                ad.url,
            )
            continue
        kept.append(ad)
    return kept


def _fetch_bodies(client: LeboncoinClient, store: Store, search, ads: list, limit: int) -> dict:
    """Ouvre la fiche de chaque annonce nouvelle pour en lire la description.

    Les pages de recherche ne contiennent jamais le texte de l'annonce : il faut
    ouvrir chaque fiche une par une. C'est lent (la cadenceuse impose 4 à 9 s par
    requête), d'où le plafond ``description_max`` par cycle. Sans ce texte, l'IA
    écarte presque tout faute d'information.

    Returns:
        ``{list_id: description}`` des descriptions effectivement lues.
    """
    bodies: dict[int, str] = {}
    for ad in ads:
        if len(bodies) >= limit:
            log.debug("[%s] plafond de %d fiches atteint", search.name, limit)
            break
        try:
            detail = client.ad_detail(ad.url)
        except BlockedError:
            raise
        except Exception as exc:  # noqa: BLE001 - la description reste facultative
            log.debug("[%s] fiche annonce indisponible : %s", search.name, exc)
            continue
        if detail is None or not detail.body:
            log.debug("[%s] fiche sans description : %s", search.name, ad.url)
            continue
        store.set_body(search.name, ad.list_id, detail.body)
        bodies[ad.list_id] = detail.body
    return bodies


def _screen_new_ads(store: Store, analyzer, search, fresh: list[Ad]) -> list[Ad]:
    """Fait analyser les annonces nouvelles par l'IA et renvoie celles à notifier.

    La décision est enregistrée en base (verdict) que l'annonce soit retenue ou
    non : l'IHM affiche les deux. Seules les annonces « retenu » déclenchent
    Telegram. Si l'analyse échoue, l'annonce reste sans verdict et n'est pas
    notifiée : elle sera réessayée plus tard depuis l'IHM.
    """
    to_notify: list[Ad] = []
    for ad in fresh:
        verdict = analyzer.analyze(store.get_ad(search.name, ad.list_id), store.get_directives())
        if verdict.verdict:
            store.set_verdict(search.name, ad.list_id, verdict.verdict, verdict.reason, "ia")
        else:
            log.warning(
                "[%s] analyse IA inexploitable (%s) : %s", search.name, verdict.error, ad.url
            )
        if verdict.retained:
            to_notify.append(ad)
        else:
            log.info(
                "[%s] annonce écartée par l'IA : %s", search.name, verdict.reason or "sans raison"
            )
    return to_notify


def run_cycle(
    client: LeboncoinClient,
    store: Store,
    notifier: TelegramNotifier,
    settings: Settings,
    analyzer=None,
) -> CycleStats:
    """Un tour complet sur toutes les recherches. Ne lève jamais BlockedError."""
    stats = CycleStats()
    # La fiche vendeur n'est sollicitée que si l'option est active.
    sellers = (
        SellerClient(client.http, client.pacer, max_retries=1, store=store)
        if settings.seller_lookup
        else None
    )
    for search in settings.searches:
        name = search.name
        try:
            ads = client.search(search)
        except BlockedError as exc:
            log.error("[%s] blocage : %s", name, exc)
            stats.blocked = True
            break
        except Exception as exc:  # noqa: BLE001 - une recherche ne doit pas tuer le daemon
            log.exception("[%s] erreur inattendue : %s", name, exc)
            continue

        result = filters.apply(ads, search)
        first_run = store.last_run(name) is None
        # reserve l'annonce : record() ne renvoie True que si elle est vraiment nouvelle
        fresh = [ad for ad in result.kept if store.record(name, ad, notified=False)]

        # Fiche vendeur uniquement pour les nouveautés (coût réseau minimal).
        if fresh and settings.seller_lookup:
            fresh = _enrich_ratings(sellers, fresh, search)

        # La description n'est jamais dans les résultats de recherche : on ouvre
        # la fiche des nouveautés, sinon l'IA écarte presque tout faute de texte.
        if fresh and settings.description_lookup:
            bodies = _fetch_bodies(
                client, store, search, fresh, settings.description_max
            )
            if bodies:
                log.info(
                    "[%s] %d description(s) récupérée(s)", name, len(bodies)
                )

        stats.searches += 1
        stats.fetched += len(ads)
        stats.kept += len(result.kept)
        # `new` compte les annonces réellement nouvelles (déduplication), que la
        # notification ait été envoyée ou non (première exécution, Telegram absent).
        stats.new += len(fresh)
        if result.rejected:
            reasons = _reason_breakdown(result.rejected)
            for reason, count in reasons:
                log.debug("[%s] rejet %d× : %s", name, count, reason)
            if not result.kept:
                log.info(
                    "[%s] 0 annonce conforme sur %d : %s",
                    name,
                    len(ads),
                    ", ".join(f"{reason} ({count})" for reason, count in reasons[:3]),
                )
            if len(stats.rejected_samples) < 3:
                stats.rejected_samples.append(f"{name} : {reasons[0][0]}")

        # Première exécution : on ne prévient pas sur des annonces déjà en ligne.
        if first_run and not settings.notify_on_first_run and fresh:
            log.info(
                "[%s] première exécution : %d annonces enregistrées sans notification",
                name,
                len(fresh),
            )
            fresh = []

        # Tri IA : seules les annonces retenues déclenchent une notification.
        to_notify = fresh
        if fresh and analyzer is not None and analyzer.config.usable:
            to_notify = _screen_new_ads(store, analyzer, search, fresh)
            stats.ai_checked += len(fresh)
            stats.ai_retained += len(to_notify)

        if to_notify:
            if notifier.configured:
                sent = notifier.notify(name, to_notify)
                log.info(
                    "[%s] %d nouvelle(s) annonce(s), %d notifiée(s)",
                    name,
                    len(to_notify),
                    sent,
                )
                for ad in to_notify[:sent]:
                    store.record(name, ad, notified=True)
                if sent < len(to_notify):
                    # On garde la trace de ce qui n'a pas pu être envoyé.
                    for ad in to_notify[sent:]:
                        log.warning("annonce non notifiée : %s", ad.url)
            else:
                log.warning(
                    "[%s] %d nouvelle(s) annonce(s) mais Telegram non configuré : %s",
                    name,
                    len(to_notify),
                    "; ".join(ad.url for ad in to_notify[:5]),
                )
        elif fresh:
            log.info(
                "[%s] %d nouvelle(s) annonce(s), aucune retenue",
                name,
                len(fresh),
            )
        else:
            log.info(
                "[%s] %d annonce(s) récupérée(s), %d conforme(s), aucune nouveauté",
                name,
                len(ads),
                len(result.kept),
            )

        store.log_run(name, len(ads), len(result.kept), len(fresh))
        store.remember_categories(ads)

    return stats


def _sleep(seconds: float, stop: dict[str, bool]) -> None:
    """Dort par tranches courtes pour réagir vite à Ctrl-C."""
    end = time.monotonic() + seconds
    while not stop.get("stop") and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def run_forever(client: LeboncoinClient, store: Store, notifier: TelegramNotifier,
                settings: Settings, *, cycles: int | None = None,
                            install_signals: bool = True, analyzer=None) -> None:
    """Enchaîne les cycles jusqu'à Ctrl-C.

    ``cycles`` borne le nombre de cycles (utile pour les tests et un lancement
    planifié) ; ``install_signals`` désactive le gestionnaire SIGINT quand on
    n'est pas dans le thread principal.
    """
    stop = {"stop": False}

    def _handler(signum, frame):  # noqa: ANN001, ARG001
        if stop["stop"]:
            return
        stop["stop"] = True
        log.info("arrêt demandé (Ctrl-C) : fin de la pause en cours")

    if install_signals:
        try:
            signal.signal(signal.SIGINT, _handler)
        except ValueError:  # thread secondaire : le signal est interdit ici
            log.debug("SIGINT non interceptable dans ce thread")

    consecutive_blocks = 0
    started = time.monotonic()
    done = 0
    while not stop["stop"] and (cycles is None or done < cycles):
        stats = run_cycle(client, store, notifier, settings, analyzer=analyzer)
        done += 1

        if stats.blocked:
            consecutive_blocks += 1
            wait = min(settings.backoff_base_min * 60 * consecutive_blocks, 4 * 3600)
            log.warning(
                "Leboncoin a bloqué la requête : pause de %d min avant nouvelle tentative "
                "(tentative %d)",
                wait // 60,
                consecutive_blocks,
            )
            _sleep(wait, stop)
            continue

        consecutive_blocks = 0
        log.info(
            "cycle terminé : %d annonce(s) vue(s), %d conforme(s), %d nouvelle(s)",
            stats.fetched,
            stats.kept,
            stats.new,
        )
        if stats.rejected_samples:
            log.debug("rejets types : %s", " | ".join(stats.rejected_samples))
        # On oublie les annonces trop anciennes pour que la base reste petite.
        removed = store.prune(filters.stale_cutoff(RETENTION_DAYS * 24))
        if removed:
            log.info("base allégée : %d annonce(s) oubliée(s)", removed)

        # Inutile de dormir après le tout dernier cycle.
        if cycles is not None and done >= cycles:
            break

        pause = settings.poll_interval_min * 60
        if settings.poll_jitter_min:
            pause += random.uniform(-settings.poll_jitter_min, settings.poll_jitter_min) * 60
        pause = max(pause, 60.0)
        log.info("prochaine vérification dans %.0f min", pause / 60)
        _sleep(pause, stop)

    client.close()
    log.info("arrêté proprement après %.0f min", (time.monotonic() - started) / 60)
