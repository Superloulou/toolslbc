"""Contrôleur de collecte : la boucle du démon pilotée depuis l'IHM.

`daemon.run_forever()` est pensé pour un terminal : il dort, s'arrête avec
Ctrl-C et n'expose rien. La classe `Collector` reprend la même logique
(backoff après blocage, purge, pause avec aléa) dans un thread que l'on peut
démarrer, arrêter, relancer immédiatement et reprogrammer à chaud.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from . import ai as ai_mod
from . import daemon, filters
from .config import Settings
from .notifier import TelegramNotifier
from .scraper.leboncoin import LeboncoinClient
from .store import Store

log = logging.getLogger("lbc")

MIN_INTERVAL_MIN = 30
"""Plancher repris de `config.load_config` : en dessous, risque de blocage."""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


@dataclass(slots=True)
class CollectorStatus:
    """Instantané sérialisable de l'état du collecteur."""

    running: bool = False
    busy: bool = False
    next_run_at: str | None = None
    last_run_at: str | None = None
    started_at: str | None = None
    poll_interval_min: int = 0
    last_result: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class Collector:
    """Un thread de collecte, partagé entre l'IHM et le démon."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        notifier: TelegramNotifier,
        *,
        analyzer: ai_mod.Analyzer | None = None,
        client: LeboncoinClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.notifier = notifier
        self.analyzer = analyzer if analyzer is not None else ai_mod.Analyzer()
        self._client = client
        self._owns_client = client is None

        self._thread: threading.Thread | None = None
        self._stop = {"stop": False}
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._run_now = False
        self._status = CollectorStatus(poll_interval_min=int(settings.poll_interval_min))
        self._next_run: datetime | None = None
        self._started: datetime | None = None

    # ------------------------------------------------------------------ état

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive()) and not self._stop["stop"]

    @property
    def busy(self) -> bool:
        return self._status.busy

    def status(self) -> CollectorStatus:
        with self._lock:
            st = CollectorStatus(**asdict(self._status))
            st.running = self.running
            st.next_run_at = _iso(self._next_run)
            st.started_at = _iso(self._started)
            return st

    # ------------------------------------------------------------- commandes

    def start(self) -> bool:
        """Démarre la boucle (sans effet si elle tourne déjà)."""
        if self.running:
            return False
        self._stop["stop"] = False
        self._started = datetime.now()
        self._thread = threading.Thread(target=self._loop, name="collecte", daemon=True)
        self._thread.start()
        log.info("collecte démarrée : toutes les %d min", self._status.poll_interval_min)
        return True

    def stop(self, *, timeout: float = 5.0) -> bool:
        """Demande l'arrêt de la boucle (le cycle en cours va à son terme)."""
        if not self.running:
            return False
        self._stop["stop"] = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        log.info("collecte arrêtée")
        return True

    def run_now(self) -> bool:
        """Interrompt la pause pour lancer un cycle tout de suite."""
        self._run_now = True
        self._wake.set()
        if not self.running:
            return self.start()
        log.info("exécution immédiate demandée")
        return True

    def set_interval(self, minutes: int) -> int:
        """Change la fréquence à chaud, et reprogramme la pause en cours."""
        minutes = int(minutes)
        if minutes < MIN_INTERVAL_MIN:
            raise ValueError(f"intervalle minimum : {MIN_INTERVAL_MIN} minutes")
        with self._lock:
            self._status.poll_interval_min = minutes
        self.settings.poll_interval_min = minutes
        self._wake.set()
        log.info("fréquence changée : toutes les %d min", minutes)
        return minutes

    def close(self) -> None:
        self.stop()
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    # ---------------------------------------------------------------- boucle

    def _get_client(self) -> LeboncoinClient:
        if self._client is None:
            self._client = LeboncoinClient(
                self.settings.user_agent,
                impersonate=self.settings.impersonate,
                delay_min=self.settings.request_delay_min,
                delay_max=self.settings.request_delay_max,
                max_retries=self.settings.max_retries,
            )
        return self._client

    def _wait(self, seconds: float) -> None:
        """Pause interrompable par `run_now()`, `stop()` ou un changement de fréquence."""
        deadline = time.monotonic() + seconds
        while not self._stop["stop"] and time.monotonic() < deadline:
            if self._wake.wait(timeout=min(1.0, deadline - time.monotonic())):
                self._wake.clear()
                if self._stop["stop"] or self._run_now:
                    return

    def _loop(self) -> None:
        blocked_streak = 0
        started = time.monotonic()
        while not self._stop["stop"]:
            with self._lock:
                self._status.busy = True
                self._next_run = None
            self._run_now = False
            stats: daemon.CycleStats | None = None
            try:
                stats = daemon.run_cycle(
                    self._get_client(),
                    self.store,
                    self.notifier,
                    self.settings,
                    analyzer=self.analyzer,
                )
            except Exception as exc:  # le thread ne doit jamais mourir en silence
                log.exception("cycle interrompu : %s", exc)
            finally:
                with self._lock:
                    self._status.busy = False
                    self._status.last_run_at = _iso(datetime.now())
                    if stats is not None:
                        self._status.last_result = {
                            "fetched": stats.fetched,
                            "kept": stats.kept,
                            "new": stats.new,
                            "blocked": stats.blocked,
                            "searches": stats.searches,
                            "ai_checked": stats.ai_checked,
                            "ai_retained": stats.ai_retained,
                        }

            if self._stop["stop"]:
                break

            if stats is not None and stats.blocked:
                blocked_streak += 1
                wait = min(
                    self.settings.backoff_base_min * 60 * blocked_streak,
                    4 * 3600,
                )
                log.warning(
                    "Leboncoin a bloqué la requête : pause de %d min avant nouvelle "
                    "tentative (tentative %d)",
                    wait // 60,
                    blocked_streak,
                )
            else:
                blocked_streak = 0
                if stats is not None:
                    log.info(
                        "cycle terminé : %d annonce(s) vue(s), %d conforme(s), %d nouvelle(s)",
                        stats.fetched,
                        stats.kept,
                        stats.new,
                    )
                    removed = self.store.prune(
                        filters.stale_cutoff(daemon.RETENTION_DAYS * 24)
                    )
                    if removed:
                        log.info("base allégée : %d annonce(s) oubliée(s)", removed)
                pause = self._status.poll_interval_min * 60
                pause += random.uniform(
                    -self.settings.poll_jitter_min, self.settings.poll_jitter_min
                ) * 60
                wait = max(pause, 60.0)

            if self._stop["stop"] or self._run_now:
                continue

            self._next_run = datetime.now() + timedelta(seconds=wait)
            log.info("prochaine vérification dans %.0f min", wait / 60)
            self._wait(wait)

        self._next_run = None
        log.info("arrêté proprement après %.0f min", (time.monotonic() - started) / 60)
