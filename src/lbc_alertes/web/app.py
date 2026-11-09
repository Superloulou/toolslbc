"""Serveur web de l'IHM : collecte, annonces, tamis IA.

On reste sur la bibliothèque standard (``http.server``) pour ne ajouter aucune
dépendance. Le collecteur tourne dans un thread de ce processus : l'IHM pilote
donc la *seule* boucle de collecte, ce qui évite deux démons sur la même base.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import socket
import threading
import time
import urllib.error
import urllib.parse
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .. import ai as ai_mod
from .. import filters
from ..config import Settings
from ..daemon import RETENTION_DAYS, log, run_cycle
from ..notifier import TelegramNotifier
from ..scraper.leboncoin import LeboncoinClient
from ..store import Store

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8760
MIN_INTERVAL_MIN = 30
DEFAULT_TIMEOUT = 15.0
_REACH_TTL = 60.0


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class RingBuffer(logging.Handler):
    """Mémoire circulaire des derniers logs, exposée par ``GET /api/logs``."""

    def __init__(self, capacity: int = 1500) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            self.lines.append(f"{ts} {record.levelname} {record.getMessage()}")
        except Exception:  # pragma: no cover - le log ne doit jamais casser le serveur
            self.handleError(record)

    def tail(self, limit: int = 300) -> list[str]:
        limit = max(1, min(int(limit), 2000))
        return list(self.lines)[-limit:]


# --------------------------------------------------------------------------- #
#  Collecte pilotée                                                           #
# --------------------------------------------------------------------------- #


class Collector:
    """Le cycle de collecte, pilotable à distance (démarrer/arrêter/fréquence)."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        notifier: TelegramNotifier,
        *,
        analyzer: ai_mod.Analyzer | None = None,
        client_factory: Callable[[], Any] | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.notifier = notifier
        self.analyzer = analyzer or ai_mod.Analyzer()
        self._client_factory = client_factory or (lambda: self._default_client())
        self.config_path = config_path
        self._thread: threading.Thread | None = None
        self._stop = {"stop": False}
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.running = False
        self.busy = False
        self.started_at: datetime | None = None
        self.next_run_at: datetime | None = None
        self.last_run_at: datetime | None = None
        self.last_result: dict[str, Any] | None = None
        self._blocks = 0

    # -- cycle ------------------------------------------------------------- #

    def _default_client(self) -> LeboncoinClient:
        return LeboncoinClient(
            user_agent=self.settings.user_agent,
            impersonate=self.settings.impersonate,
            request_delay_min=self.settings.request_delay_min,
            request_delay_max=self.settings.request_delay_max,
            max_retries=self.settings.max_retries,
            backoff_base_min=self.settings.backoff_base_min,
            playwright_fallback=self.settings.use_playwright_fallback,
        )

    def cycle(self) -> dict[str, Any]:
        """Un aller-retour complet sur toutes les recherches.

        On réutilise ``daemon.run_cycle`` : l'IHM et le démon doivent faire
        exactement la même chose (mêmes filtres, même tamis IA, mêmes règles
        de notification), sinon les compteurs affichés divergent.
        """

        client = self._client_factory()
        try:
            stats = run_cycle(client, self.store, self.notifier, self.settings, self.analyzer)
        finally:
            client.close()
        return {
            "fetched": stats.fetched,
            "kept": stats.kept,
            "new": stats.new,
            "blocked": stats.blocked,
            "searches": stats.searches,
            "ai_checked": stats.ai_checked,
            "ai_retained": stats.ai_retained,
        }

    # -- boucle ------------------------------------------------------------ #

    def _pause_seconds(self) -> float:
        import random

        pause = self.settings.poll_interval_min * 60.0
        pause += random.uniform(-self.settings.poll_jitter_min, self.settings.poll_jitter_min) * 60.0
        return max(pause, 60.0)

    def _loop(self) -> None:
        removed = self.store.prune(filters.stale_cutoff(RETENTION_DAYS * 24))
        if removed:
            log.info("base allégée : %d annonce(s) oubliée(s)", removed)
        while not self._stop["stop"]:
            self._wake.clear()
            self.busy = True
            self.next_run_at = None
            try:
                result = self.cycle()
            except Exception as exc:  # noqa: BLE001 - le fil ne doit jamais mourir
                log.exception("cycle interrompu : %s", exc)
                result = {"fetched": 0, "kept": 0, "new": 0, "blocked": False, "searches": 0}
                result["error"] = str(exc)
            self.busy = False
            self.last_run_at = datetime.now()
            self.last_result = result
            log.info(
                "cycle terminé : %d vue(s), %d conforme(s), %d nouvelle(s), "
                "%d analysée(s) IA, %d retenue(s)",
                result["fetched"],
                result["kept"],
                result["new"],
                result.get("ai_checked", 0),
                result.get("ai_retained", 0),
            )
            if result.get("blocked"):
                self._blocks += 1
                wait = min(self.settings.backoff_base_min * 60 * self._blocks, 4 * 3600)
                log.warning(
                    "Leboncoin a bloqué la requête : pause de %d min avant nouvelle tentative "
                    "(tentative %d)",
                    wait // 60,
                    self._blocks,
                )
            else:
                self._blocks = 0
                pause = self._pause_seconds()
                self.next_run_at = datetime.now() + timedelta(seconds=pause)
                log.info("prochaine vérification dans %.0f min", pause / 60)
                wait = pause
            if self._stop["stop"]:
                break
            if self._sleep(wait):
                break
        self.running = False
        self.next_run_at = None
        log.info("collecteur arrêté")

    def _sleep(self, seconds: float) -> bool:
        """Dort jusqu'à ``seconds`` en se réveillant tôt si nécessaire.

        Vrai si le collecteur doit s'arrêter (``stop()`` ou déclenchement
        manuel via ``run_now()``).
        """

        deadline = time.monotonic() + max(seconds, 0.0)
        while True:
            if self._stop["stop"]:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._wake.wait(timeout=min(remaining, 1.0)):
                self._wake.clear()
                return self._stop["stop"]

    # -- commandes --------------------------------------------------------- #

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop["stop"] = False
            self._wake.clear()
            self.running = True
            self.started_at = datetime.now()
            self._thread = threading.Thread(target=self._loop, name="collecteur", daemon=True)
            self._thread.start()
        log.info("collecteur démarré (toutes les %d min)", self.settings.poll_interval_min)

    def stop(self) -> None:
        with self._lock:
            if not self.running:
                return
            self._stop["stop"] = True
            self._wake.set()
        log.info("arrêt du collecteur demandé")

    def run_now(self) -> None:
        self._wake.set()

    def set_interval(self, minutes: int) -> int:
        minutes = int(minutes)
        if minutes < MIN_INTERVAL_MIN:
            raise ValueError(f"poll_interval_min < {MIN_INTERVAL_MIN} : risque de blocage trop élevé")
        self.settings.poll_interval_min = minutes
        if self.next_run_at is not None:
            self.next_run_at = datetime.now() + timedelta(minutes=minutes)
        if self.config_path:
            _write_interval(self.config_path, minutes)
        log.info("fréquence réglée sur %d min", minutes)
        return minutes

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "busy": self.busy,
            "next_run_at": _iso(self.next_run_at),
            "last_run_at": _iso(self.last_run_at),
            "poll_interval_min": int(self.settings.poll_interval_min),
            "started_at": _iso(self.started_at),
            "last_result": self.last_result,
        }


def _write_interval(path: Path, minutes: int) -> None:
    """Met à jour ``poll_interval_min`` dans config.yml sans toucher aux commentaires."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("config %s illisible : fréquence non mémorisée", path)
        return
    import re

    new = f"poll_interval_min: {minutes}"
    if re.search(r"^poll_interval_min:.*$", text, re.MULTILINE):
        updated = re.sub(r"^poll_interval_min:.*$", new, text, count=1, flags=re.MULTILINE)
    else:
        updated = new + "\n" + text
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError:
        log.warning("config %s en écriture impossible : fréquence non mémorisée", path)


# --------------------------------------------------------------------------- #
#  HTTP                                                                       #
# --------------------------------------------------------------------------- #


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lbc-alertes"

    # injectées par la fabrique de serveur
    collector: Collector
    store: Store
    notifier: TelegramNotifier
    analyzer: ai_mod.Analyzer
    logs: RingBuffer
    settings: Settings

    # -- utilitaires ------------------------------------------------------- #

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        log.debug("http %s", fmt % args)

    def _send(self, code: int, payload: Any, ctype: str = "application/json; charset=utf-8") -> None:
        if isinstance(payload, (bytes, bytearray)):
            body = bytes(payload)
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, payload)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("corps JSON attendu : un objet")
        return data

    # -- routes GET -------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/state":
                return self._json(self._state())
            if path == "/api/logs":
                qs = urllib.parse.parse_qs(parsed.query)
                limit = int((qs.get("limit") or ["300"])[0])
                return self._json({"lines": self.logs.tail(limit)})
            if path == "/api/ads":
                return self._json(self._ads(urllib.parse.parse_qs(parsed.query)))
            if path == "/api/directives":
                return self._json(self._directives())
            if path.startswith("/api/"):
                return self._json({"error": f"route inconnue: {path}"}, 404)
            return self._static(path)
        except Exception as exc:  # noqa: BLE001
            log.exception("GET %s : %s", self.path, exc)
            return self._json({"error": str(exc)}, 500)

    # -- routes POST ------------------------------------------------------- #

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            body = self._body()
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": False, "error": f"corps illisible : {exc}"}, 400)
        try:
            if path == "/api/collector/run-now":
                self.collector.run_now()
                return self._json({"ok": True, "message": "collecte demandée"})
            if path == "/api/collector/start":
                self.collector.start()
                return self._json({"ok": True})
            if path == "/api/collector/stop":
                self.collector.stop()
                return self._json({"ok": True})
            if path == "/api/collector/interval":
                try:
                    minutes = self.collector.set_interval(body.get("minutes"))
                except (TypeError, ValueError) as exc:
                    return self._json({"ok": False, "error": str(exc)})
                return self._json({"ok": True, "minutes": minutes})
            if path == "/api/ads/verdict":
                return self._verdict(body)
            if path == "/api/ads/analyze":
                return self._analyze(body)
            if path == "/api/ads/analyze_pending":
                return self._analyze_pending(body)
            if path == "/api/directives":
                return self._save_directives(body)
            if path == "/api/test/ai":
                return self._test_ai()
            if path == "/api/test/telegram":
                return self._test_telegram()
            return self._json({"error": f"route inconnue: {path}"}, 404)
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s : %s", self.path, exc)
            return self._json({"ok": False, "error": str(exc)}, 500)

    # -- implémentations --------------------------------------------------- #

    _reach_cache: tuple[float, bool] = (0.0, False)
    _reach_lock = threading.Lock()

    def _ai_reachable(self) -> bool:
        """Valide la jointure de l'IA au plus une fois par minute.

        L'IHM interroge /api/state en continu : sans cache, chaque rafraîchissement
        dépenserait un aller-retour réseau vers l'endpoint de l'IA.
        """
        now = time.monotonic()
        with ApiHandler._reach_lock:
            stamp, value = ApiHandler._reach_cache
            if now - stamp < _REACH_TTL:
                return value
        try:
            value = bool(self.analyzer.reachable())
        except Exception:
            value = False
        with ApiHandler._reach_lock:
            ApiHandler._reach_cache = (now, value)
        return value

    def _state(self) -> dict[str, Any]:
        cfg = self.analyzer.config
        searches = []
        for sc in self.settings.searches:
            counts = self.store.counts(sc.name)
            searches.append(
                {
                    "name": sc.name,
                    "text": sc.text,
                    "category": sc.category,
                    "retenu": counts["retenu"],
                    "total": counts["total"],
                    "pending": counts["inconnu"],
                    "last_run": self.store.last_run(sc.name),
                }
            )
        return {
            "collector": self.collector.status(),
            "ai": {
                "enabled": bool(cfg.enabled),
                "model": self.analyzer.resolve_model() or "",
                "base_url": cfg.base_url,
                "auto_analyze": bool(cfg.auto),
                "reachable": self._ai_reachable(),
            },
            "telegram": {"configured": bool(self.notifier.configured)},
            "searches": searches,
        }

    def _ads(self, qs: dict[str, list[str]]) -> dict[str, Any]:
        def get(name: str, default: str = "") -> str:
            return (qs.get(name) or [default])[0]

        rows = self.store.ads(
            search=get("search") or None,
            verdict=get("verdict") or "all",
            q=get("q") or None,
            limit=int(get("limit") or 200),
        )
        for row in rows:
            row["notified"] = bool(row["notified"])
        return {"ads": rows}

    def _verdict(self, body: dict[str, Any]) -> None:
        verdict = (body.get("verdict") or "").strip()
        if verdict not in ("retenu", "rejete", "none"):
            return self._json({"ok": False, "error": "verdict invalide"}, 400)
        reason = (body.get("reason") or "").strip() or None
        self.store.set_verdict(
            body.get("search") or "",
            int(body.get("list_id") or 0),
            None if verdict == "none" else verdict,
            reason,
            "manuel",
        )
        return self._json({"ok": True})

    def _analyze(self, body: dict[str, Any]) -> None:
        ad = self.store.get_ad(body.get("search") or "", int(body.get("list_id") or 0))
        if ad is None:
            return self._json({"ok": False, "error": "annonce inconnue"}, 404)
        res = self.analyzer.analyze(ad, self.store.get_directives())
        if res.verdict is None:
            return self._json({"ok": False, "error": res.error or "verdict inexploitable"})
        self.store.set_verdict(ad["search"], ad["list_id"], res.verdict, res.reason, "ia")
        return self._json({"ok": True, "verdict": res.verdict, "reason": res.reason})

    def _analyze_pending(self, body: dict[str, Any]) -> None:
        search = body.get("search") or None
        analyzed, retained = ai_mod.screen_ads(self.store, self.analyzer, search)
        return self._json({"ok": True, "analyzed": analyzed, "retained": retained})

    def _directives(self) -> dict[str, Any]:
        stored = self.store.get_directives()
        return {
            "global": stored.get("global", ""),
            "per_search": {sc.name: stored.get(sc.name, "") for sc in self.settings.searches},
        }

    def _save_directives(self, body: dict[str, Any]) -> None:
        self.store.set_directive("global", body.get("global") or "")
        per_search = body.get("per_search") or {}
        if isinstance(per_search, dict):
            for name, texte in per_search.items():
                self.store.set_directive(str(name), str(texte or ""))
        return self._json({"ok": True})

    def _test_ai(self) -> None:
        try:
            reply = self.analyzer.ping()
        except Exception as exc:  # noqa: BLE001 - l'erreur est affichée telle quelle dans l'IHM
            return self._json({"ok": False, "error": str(exc)})
        if not reply:
            return self._json({"ok": False, "error": "l'IA a répondu vide"})
        return self._json({"ok": True, "reply": reply})

    def _test_telegram(self) -> None:
        if not self.notifier.configured:
            return self._json({"ok": False, "error": "Telegram non configuré"})
        ok = self.notifier.test()
        if ok:
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "Telegram a refusé le message"})

    # -- fichiers ---------------------------------------------------------- #

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        candidate = (STATIC_DIR / name).resolve()
        try:
            candidate.relative_to(STATIC_DIR)
        except ValueError:
            return self._json({"error": "chemin refusé"}, 403)
        if not candidate.is_file():
            return self._send(404, b"introuvable", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, candidate.read_bytes(), ctype)


def make_server(
    settings: Settings,
    store: Store,
    notifier: TelegramNotifier,
    *,
    collector: Collector | None = None,
    analyzer: ai_mod.Analyzer | None = None,
    logs: RingBuffer | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    config_path: Path | None = None,
) -> tuple[ThreadingHTTPServer, Collector, RingBuffer]:
    analyzer = analyzer or ai_mod.Analyzer()
    collector = collector or Collector(
        settings, store, notifier, analyzer=analyzer, config_path=config_path
    )
    logs = logs or RingBuffer()

    handler = type("BoundHandler", (ApiHandler,), {})
    handler.collector = collector
    handler.store = store
    handler.notifier = notifier
    handler.analyzer = analyzer
    handler.logs = logs
    handler.settings = settings

    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd, collector, logs


def serve(
    settings: Settings,
    notifier: TelegramNotifier,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    config_path: Path | None = None,
    open_browser: bool = True,
    start_collector: bool = True,
) -> int:
    if not _port_free(host, port):
        log.error("le port %d est déjà occupé : l'IHM tourne peut-être déjà", port)
        return 3

    store = Store(settings.db_file)
    logs = RingBuffer()
    logging.getLogger().addHandler(logs)
    analyzer = ai_mod.Analyzer()
    try:
        httpd, collector, _ = make_server(
            settings,
            store,
            notifier,
            analyzer=analyzer,
            logs=logs,
            host=host,
            port=port,
            config_path=config_path,
        )
    except OSError as exc:
        store.close()
        log.error("impossible d'écouter sur %s:%d : %s", host, port, exc)
        return 3

    url = f"http://{host}:{port}/"
    log.info("IHM disponible sur %s", url)
    if open_browser:
        _open_later(url)
    # Le collecteur ne démarre qu'une fois le serveur à l'écoute : l'IHM répond
    # immédiatement, même si la première collecte est longue.
    if start_collector:
        collector.start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print()
        log.info("arrêt de l'IHM demandé")
    finally:
        collector.stop()
        httpd.shutdown()
        httpd.server_close()
        store.close()
    return 0


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((host, port)) != 0


def _open_later(url: str) -> None:
    import webbrowser

    def go() -> None:
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            log.warning("ouvre %s dans ton navigateur", url)

    threading.Thread(target=go, name="ouvre-navigateur", daemon=True).start()
