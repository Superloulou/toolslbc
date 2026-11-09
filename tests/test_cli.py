"""Tests de la ligne de commande : ``test``, ``once``, ``prune``, ``run`` et ``main``.

Aucun accès réseau : le client Leboncoin, le client vendeur et le notifieur
Telegram sont remplacés par des doubles.
"""

from __future__ import annotations

import logging

import pytest

from conftest import make_ad
from lbc_alertes import cli, daemon
from lbc_alertes.config import SearchConfig, Settings
from lbc_alertes.daemon import CycleStats
from lbc_alertes.scraper import BlockedError
from lbc_alertes.store import Store


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------
class FakeClient:
    """Client Leboncoin factice : renvoie des annonces fabriquées."""

    def __init__(self, ads=None, error: Exception | None = None):
        self.ads = list(ads) if ads is not None else [make_ad(list_id=1)]
        self.error = error
        self.queries: list[tuple[str, int | None]] = []
        self.closed = 0

    def search(self, search_config, *, max_pages=None):
        self.queries.append((search_config.name, max_pages))
        if self.error is not None:
            raise self.error
        return list(self.ads)

    def close(self) -> None:
        self.closed += 1


class FakeSellers:
    def __init__(self):
        self.calls = 0

    def enrich(self, ad):
        self.calls += 1
        return ad


class FakeNotifier:
    def __init__(self, *, configured: bool = True, test_ok: bool = True):
        self.configured = configured
        self.test_ok = test_ok
        self.calls: list[tuple[str, list]] = []
        self.tests = 0

    def notify(self, name, ads):
        self.calls.append((name, list(ads)))
        return len(ads)

    def test(self) -> bool:
        self.tests += 1
        return self.test_ok


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        searches=[SearchConfig(name="rtx 3090", text="rtx 3090", category=83)],
        db_path=str(tmp_path / "state.db"),
        seller_lookup=False,
        **overrides,
    )


def _patch_account(monkeypatch, settings, client, notifier):
    def fake(settings_):
        store = Store(settings_.db_file)
        return store, client, notifier

    monkeypatch.setattr(cli, "_account", fake)
    return settings


# --------------------------------------------------------------------------
# cmd_test
# --------------------------------------------------------------------------
def test_cmd_test_indique_les_recherches(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    client = FakeClient()
    notifier = FakeNotifier()
    _patch_account(monkeypatch, settings, client, notifier)

    assert cli.cmd_test(settings) == 0
    out = capsys.readouterr().out
    assert "rtx 3090" in out
    assert "state.db" in out
    assert notifier.tests == 1


def test_cmd_test_notifieur_non_configure(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    _patch_account(monkeypatch, settings, FakeClient(), FakeNotifier(configured=False))

    assert cli.cmd_test(settings) == 1
    assert "TELEGRAM" in capsys.readouterr().out


def test_cmd_test_echec_telegram(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _patch_account(monkeypatch, settings, FakeClient(), FakeNotifier(test_ok=False))
    assert cli.cmd_test(settings) == 1


# --------------------------------------------------------------------------
# cmd_once
# --------------------------------------------------------------------------
def test_cmd_once_affiche_le_bilan(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, notify_on_first_run=True)
    client = FakeClient([make_ad(list_id=1), make_ad(list_id=2)])
    notifier = FakeNotifier()
    _patch_account(monkeypatch, settings, client, notifier)

    assert cli.cmd_once(settings) == 0
    out = capsys.readouterr().out
    assert "2 annonce(s) vue(s)" in out
    assert "0 purge(s)" in out
    assert notifier.calls and notifier.calls[0][0] == "rtx 3090"


def test_cmd_once_purge_les_vieilles_anonces(tmp_path, monkeypatch, capsys):
    """``once`` doit purger comme le fait le démon à chaque cycle."""
    settings = _settings(tmp_path)
    store = Store(settings.db_file)
    # Une annonce vue il y a 60 jours : au-delà de RETENTION_DAYS * 24 heures.
    store.con.execute(
        "INSERT INTO seen_ads (search, list_id, subject, first_seen) VALUES (?,?,?,?)",
        ("rtx 3090", 99, "vieille annonce", "2000-01-01T00:00:00+00:00"),
    )
    store.con.commit()
    store.close()

    client = FakeClient([make_ad(list_id=1)])
    _patch_account(monkeypatch, settings, client, FakeNotifier())

    assert cli.cmd_once(settings) == 0
    assert "1 purge(s)" in capsys.readouterr().out

    check = Store(settings.db_file)
    assert check.count_seen() == 1
    check.close()


def test_cmd_once_bloque_renvoye_2(tmp_path, monkeypatch, caplog, capsys):
    settings = _settings(tmp_path)
    client = FakeClient(error=BlockedError("DataDome"))
    _patch_account(monkeypatch, settings, client, FakeNotifier())

    with caplog.at_level(logging.WARNING):
        assert cli.cmd_once(settings) == 2
    assert "blocage" in caplog.text
    assert "fait :" not in capsys.readouterr().out


def test_cmd_once_ferme_le_client_meme_en_erreur(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = FakeClient(error=RuntimeError("inattendu"))
    _patch_account(monkeypatch, settings, client, FakeNotifier())

    # run_cycle ne jamais levue generic exceptions (log.exception) -> cmd_once
    # renvoye 0 et ne fermé le client que once, dans its finally.
    assert cli.cmd_once(settings) == 0
    assert client.closed == 1


# --------------------------------------------------------------------------
# cmd_prune
# --------------------------------------------------------------------------
def test_cmd_prune_supprime_et_anonce(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    store = Store(settings.db_file)
    store.record("rtx 3090", make_ad(list_id=1), notified=True)
    store.con.execute(
        "UPDATE seen_ads SET first_seen=? WHERE list_id=1", ("2000-01-01T00:00:00+00:00",)
    )
    store.con.commit()
    store.close()

    assert cli.cmd_prune(settings) == 0
    out = capsys.readouterr().out
    assert "1 annonce(s) supprimée(s)" in out
    assert "avant : 1" in out


# --------------------------------------------------------------------------
# cmd_run
# --------------------------------------------------------------------------
def test_cmd_run_delegue_a_run_forever(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = FakeClient()
    _patch_account(monkeypatch, settings, client, FakeNotifier())

    seen: list[tuple] = []

    def fake_run_forever(client_, store, notifier, settings_):
        seen.append((client_, store, notifier, settings_))

    monkeypatch.setattr(daemon, "run_forever", fake_run_forever)

    assert cli.cmd_run(settings) == 0
    assert len(seen) == 1
    assert client.closed == 1


# --------------------------------------------------------------------------
# cmd_ui
# --------------------------------------------------------------------------
def test_cmd_ui_delegue_a_web_serve(tmp_path, monkeypatch):
    import lbc_alertes.web as web

    settings = _settings(tmp_path)
    seen: dict = {}

    def fake_serve(settings_, notifier, **kwargs):
        seen["settings"] = settings_
        seen["notifier"] = notifier
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(web, "serve", fake_serve)
    monkeypatch.setattr(
        cli, "_make_notifier", lambda settings_: FakeNotifier(configured=False)
    )
    args = cli.build_parser().parse_args(
        ["ui", "--port", "8791", "--no-browser", "--no-collector"]
    )

    assert cli.cmd_ui(settings, tmp_path / "config.yml", args) == 0
    assert seen["settings"] is settings
    assert seen["config_path"] == tmp_path / "config.yml"
    assert seen["port"] == 8791
    assert seen["open_browser"] is False
    assert seen["start_collector"] is False
    # Le notifieur est construit par le CLI, pas par l'interface.
    assert seen["notifier"].configured is False


def test_cmd_ui_ne_construit_pas_de_compte(tmp_path, monkeypatch):
    """``web.serve`` ouvre son propre Store : le CLI ne doit pas en créer un second."""
    import lbc_alertes.web as web

    settings = _settings(tmp_path)
    monkeypatch.setattr(web, "serve", lambda *a, **kw: 0)

    def boom(*_args, **_kwargs):  # pragma: no cover - doit être interdit
        raise AssertionError("_account() ne doit pas être appelé pour ui")

    monkeypatch.setattr(cli, "_account", boom)
    args = cli.build_parser().parse_args(["ui"])
    assert cli.cmd_ui(settings, tmp_path / "config.yml", args) == 0


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------
def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yml"
    path.write_text(body, encoding="utf-8")
    return path


CONFIG_OK = """
poll_interval_min: 60
searches:
  - name: rtx 3090
    text: rtx 3090
    category: 83
"""


def test_main_sans_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_env", lambda root: None)
    # Ne le repo ni la cwd muster containner a un configuration.
    monkeypatch.setattr(cli, "default_config_path", lambda root: None)
    assert cli.main(["once"]) == 2
    assert "introuvable" in capsys.readouterr().err


def test_main_configuration_invalide(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "config.yml"
    bad.write_text("poll_interval_min: 5\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_env", lambda root: None)

    assert cli.main(["once", "-c", str(bad)]) == 2
    assert "Configuration invalide" in capsys.readouterr().err


def test_main_config_absente_via_option(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_env", lambda root: None)
    assert cli.main(["once", "-c", str(tmp_path / "absent.yml")]) == 2


def test_main_prune_depuis_la_ligne_de_commande(tmp_path, monkeypatch, capsys):
    config = _write_config(tmp_path, CONFIG_OK)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_env", lambda root: None)

    settings = cli.load_config(config)
    store = Store(settings.db_file)
    store.record("rtx 3090", make_ad(list_id=1), notified=False)
    store.close()

    monkeypatch.setattr(cli, "_account", lambda: (Store(settings.db_file), FakeClient(), FakeNotifier(), FakeSellers()))
    assert cli.main(["prune", "-c", str(config)]) == 0
    assert "0 annonce(s) supprimée(s)" in capsys.readouterr().out


def test_main_keyboard_interrupt_renvoye_130(tmp_path, monkeypatch):
    config = _write_config(tmp_path, CONFIG_OK)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_env", lambda root: None)
    monkeypatch.setattr(
        daemon, "run_forever", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    _patch_account(monkeypatch, settings_db(tmp_path), FakeClient(), FakeNotifier())

    assert cli.main(["run", "-c", str(config)]) == 130


def settings_db(tmp_path) -> str:
    return str(tmp_path / "state.db")


def test_main_version(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert cli.__version__ in capsys.readouterr().out


def test_main_commande_implicite_run(tmp_path, monkeypatch):
    """Sans argument, la commande par défaut est ``run``."""
    config = _write_config(tmp_path, CONFIG_OK)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_env", lambda root: None)

    called: list[str] = []
    monkeypatch.setattr(cli, "cmd_run", lambda settings: called.append("run") or 0)
    monkeypatch.setattr(cli, "_account", lambda: (Store(settings_db(tmp_path)), FakeClient(), FakeNotifier(), FakeSellers()))

    assert cli.main(["-c", str(config)]) == 0
    assert called == ["run"]


def test_main_ui_transmet_le_port(tmp_path, monkeypatch):
    """``ui`` passe la configuration, le chemin du fichier et le port choisi."""
    config = _write_config(tmp_path, CONFIG_OK)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_env", lambda root: None)

    seen: dict[str, object] = {}

    def fake_cmd_ui(settings_, config_path_, args_):
        seen["settings"] = settings_
        seen["config_path"] = config_path_
        seen["port"] = args_.port
        seen["host"] = args_.host
        seen["no_browser"] = args_.no_browser
        return 0

    monkeypatch.setattr(cli, "cmd_ui", fake_cmd_ui)

    assert cli.main(["ui", "-c", str(config), "--port", "8899", "--no-browser"]) == 0
    assert seen["config_path"] == config
    assert seen["port"] == 8899
    assert seen["host"] == "127.0.0.1"
    assert seen["no_browser"] is True
    assert seen["settings"].searches[0].name == "rtx 3090"
