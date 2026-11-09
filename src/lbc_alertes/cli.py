"""Point d'entrée en ligne de commande : ``lbc-alertes``.

Commandes utiles :

    lbc-alertes ui            ouvre l'interface web (collecte + annonces + IA)
    lbc-alertes test          vérifie la configuration et l'envoi Telegram
    lbc-alertes once          un seul cycle de vérification, puis quitte
    lbc-alertes run           tourne en continu (démon, sans interface)
    lbc-alertes prune         purge la base locale
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__, daemon, filters
from .config import DEFAULT_CONFIG_NAMES, Settings, default_config_path, load_config
from .notifier import TelegramNotifier
from .scraper.leboncoin import LeboncoinClient
from .store import Store

log = logging.getLogger("lbc")

DEFAULT_TIMEOUT = 15.0


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s : %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _load_env(root: Path) -> None:
    """Charge un éventuel fichier ``.env`` à la racine du projet."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dépendance déclarée
        return
    for candidate in (root / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)


def _make_notifier(settings: Settings) -> TelegramNotifier:
    return TelegramNotifier(
        token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        parse_mode=settings.telegram_parse_mode,
        timeout=DEFAULT_TIMEOUT,
    )


def _account(settings: Settings) -> tuple[Store, LeboncoinClient, TelegramNotifier]:
    store = Store(settings.db_file)
    client = LeboncoinClient(
        user_agent=settings.user_agent,
        impersonate=settings.impersonate,
        request_delay_min=settings.request_delay_min,
        request_delay_max=settings.request_delay_max,
        max_retries=settings.max_retries,
        backoff_base_min=settings.backoff_base_min,
        playwright_fallback=settings.use_playwright_fallback,
    )
    return store, client, _make_notifier(settings)


def cmd_test(settings: Settings) -> int:
    store, client, notifier = _account(settings)
    try:
        print(f"recherches : {', '.join(s.name for s in settings.searches)}")
        print(f"base sqlite : {settings.db_file}")
        print(f"annonces connues : {store.count_seen()}")
        if not notifier.configured:
            print(
                "Telegram : NON CONFIGURÉ. Définis TELEGRAM_BOT_TOKEN et "
                "TELEGRAM_CHAT_ID dans le fichier .env."
            )
            return 1
        ok = notifier.test()
        print(f"Telegram : {'OK' if ok else 'ÉCHEC'}")
        return 0 if ok else 1
    finally:
        client.close()
        store.close()


def cmd_once(settings: Settings) -> int:
    store, client, notifier = _account(settings)
    try:
        stats = daemon.run_cycle(client, store, notifier, settings)
        # Le démon fait ce ménage à chaque cycle ; sans lui, l'usage de
        # `once` (planificateur Windows par exemple) laisserait grossir la base.
        removed = store.prune(filters.stale_cutoff(daemon.RETENTION_DAYS * 24))
    finally:
        client.close()
        store.close()
    if stats.blocked:
        print("Leboncoin a bloqué la requête : réessaie plus tard.")
        return 2
    print(
        f"fait : {stats.fetched} annonce(s) vue(s), {stats.kept} conforme(s), "
        f"{stats.new} nouvelle(s), {removed} purge(s)"
    )
    return 0


def cmd_run(settings: Settings) -> int:
    store, client, notifier = _account(settings)
    try:
        daemon.run_forever(client, store, notifier, settings)
    finally:
        client.close()
        store.close()
    return 0


def cmd_ui(settings: Settings, config_path: Path, args: argparse.Namespace) -> int:
    """Démarre l'interface web ; le collecteur tourne dans ce processus.

    Le magasin SQLite et le client Leboncoin sont créés par ``web.serve`` : il
    ne faut donc pas passer par ``_account()`` ici, sous peine d'ouvrir deux
    connexions sur la même base.
    """
    from .web import serve

    return serve(
        settings,
        _make_notifier(settings),
        host=args.host,
        port=args.port,
        config_path=config_path,
        open_browser=not args.no_browser,
        start_collector=not args.no_collector,
    )


def cmd_prune(settings: Settings) -> int:
    store = Store(settings.db_file)
    try:
        before = store.count_seen()
        removed = store.prune(filters.stale_cutoff(daemon.RETENTION_DAYS * 24))
        print(f"{removed} annonce(s) supprimée(s) (avant : {before})")
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lbc-alertes",
        description="Veille Leboncoin : alertes Telegram sur nouvelles annonces.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("ui", "run", "once", "test", "prune"),
        help=(
            "run (démon, défaut), ui (interface web), once (un cycle), "
            "test (vérif.), prune (nettoie)"
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="chemin du fichier de configuration (défaut : config.yml à la racine)",
    )
    parser.add_argument(
        "-d",
        "--data-dir",
        default=None,
        help="répertoire des données (défaut : à côté de la configuration)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="logs détaillés")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="adresse d'écoute de l'interface web (défaut : 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8760,
        help="port de l'interface web (défaut : 8760)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="ne pas ouvrir le navigateur au démarrage de l'interface",
    )
    parser.add_argument(
        "--no-collector",
        action="store_true",
        help="démarrer l'interface sans lancer la collecte",
    )
    parser.add_argument("--version", action="version", version=f"lbc-alertes {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    root = Path(__file__).resolve().parents[2]
    if args.config:
        config_path = Path(args.config)
    else:
        config_path = default_config_path(Path.cwd()) or default_config_path(root)
    if not config_path:
        searched = ", ".join(
            f"{d / name}" for d in (Path.cwd(), root) for name in DEFAULT_CONFIG_NAMES
        )
        print(
            "Configuration introuvable. Fichiers cherchés :\n  "
            + searched
            + "\nCrée un fichier config.yml (voir config.example.yml) "
            "ou indique-le avec -c CHEMIN.",
            file=sys.stderr,
        )
        return 2

    _load_env(config_path.parent)
    try:
        settings = load_config(config_path, data_dir=args.data_dir)
    except FileNotFoundError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2

    commands = {
        "ui": lambda s: cmd_ui(s, config_path, args),
        "test": cmd_test,
        "once": cmd_once,
        "run": cmd_run,
        "prune": cmd_prune,
    }
    try:
        return commands[args.command](settings)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
