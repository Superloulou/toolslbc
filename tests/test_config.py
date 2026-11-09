"""Tests for :mod:`lbc_alertes.config` (ported from the t_cfg.py probe)."""

from __future__ import annotations

import textwrap

import pytest

from conftest import PROJECT_ROOT
from lbc_alertes.config import (
    DEFAULT_CONFIG_NAMES,
    SearchConfig,
    Settings,
    default_config_path,
    load_config,
    slugify,
)

CONFIG_YML = PROJECT_ROOT / "config.yml"
CONFIG_EXAMPLE_YML = PROJECT_ROOT / "config.example.yml"


# --------------------------------------------------------------------------- #
# the two real config files at the project root
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def yml_settings() -> Settings:
    """``Settings`` loaded from the real ``config.yml``."""
    return load_config(CONFIG_YML, data_dir=PROJECT_ROOT)


@pytest.mark.parametrize("filename", [CONFIG_YML, CONFIG_EXAMPLE_YML])
def test_load_config_accepts_both_real_files(filename):
    settings = load_config(filename, data_dir=PROJECT_ROOT)
    assert isinstance(settings, Settings)
    assert len(settings.searches) == 2
    assert settings.poll_interval_min >= 30


def test_config_yml_search_names(yml_settings):
    assert [s.name for s in yml_settings.searches] == ["rtx 3090", "ddr4 32go"]


def test_example_and_real_config_expose_same_search_names():
    real = load_config(CONFIG_YML, data_dir=PROJECT_ROOT)
    example = load_config(CONFIG_EXAMPLE_YML, data_dir=PROJECT_ROOT)
    assert [s.name for s in real.searches] == [s.name for s in example.searches]


def test_data_dir_overrides_db_path(tmp_path):
    settings = load_config(CONFIG_YML, data_dir=tmp_path / "out")
    # ``Settings.db_path`` is stored as a str; ``db_file`` is the Path view.
    assert settings.db_path == str(tmp_path / "out" / "state.db")
    assert settings.db_file == tmp_path / "out" / "state.db"


def test_searches_are_fully_populated(yml_settings):
    rtx = yml_settings.searches[0]
    assert rtx.text == "rtx 3090"
    assert rtx.category == 83
    assert rtx.price_min == 200 and rtx.price_max == 800
    assert rtx.max_age_hours == 336.0
    assert "j'achète" in rtx.exclude
    # config.yml ne définit pas ``seller_types`` : la valeur par défaut reste vide.
    assert rtx.seller_types == []
    assert rtx.min_rating == 3.5
    assert rtx.pages == 2


def test_example_config_restricts_seller_types():
    """Contrairement à ``config.yml``, l'exemple limite aux vendeurs particuliers."""

    settings = load_config(CONFIG_EXAMPLE_YML, data_dir=PROJECT_ROOT)
    assert settings.searches[0].seller_types == ["private"]


def test_scraper_and_notify_defaults(yml_settings):
    assert yml_settings.request_delay_min == 4.0
    assert yml_settings.request_delay_max == 9.0
    assert yml_settings.max_retries == 3
    assert yml_settings.impersonate == "chrome"
    assert yml_settings.seller_lookup is True
    assert yml_settings.telegram_parse_mode == "HTML"
    assert yml_settings.notify_on_first_run is False


# --------------------------------------------------------------------------- #
# error paths
# --------------------------------------------------------------------------- #
def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yml")


def test_yaml_scalar_instead_of_mapping_raises(tmp_path):
    p = tmp_path / "bad.yml"
    p.write_text("just a string\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dictionnaire YAML"):
        load_config(p)


def test_config_without_searches_raises(tmp_path):
    p = tmp_path / "empty.yml"
    p.write_text("poll_interval_min: 60\n", encoding="utf-8")
    with pytest.raises(ValueError, match="aucune recherche"):
        load_config(p)


def test_poll_interval_below_30_is_refused(tmp_path):
    p = tmp_path / "fast.yml"
    p.write_text(
        textwrap.dedent(
            """
            poll_interval_min: 10
            searches:
              - name: rtx
                text: rtx
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="poll_interval_min"):
        load_config(p)


def test_relative_db_path_resolved_against_config_file(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text(
        textwrap.dedent(
            """
            db_path: nested/state.db
            searches:
              - name: rtx
                text: rtx
            """
        ),
        encoding="utf-8",
    )
    settings = load_config(p)
    assert settings.db_file == tmp_path / "nested" / "state.db"


def test_search_without_name_raises(tmp_path):
    p = tmp_path / "noname.yml"
    p.write_text(
        textwrap.dedent(
            """
            searches:
              - text: rtx 3090
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="name"):
        load_config(p)


# --------------------------------------------------------------------------- #
# SearchConfig.validate()
# --------------------------------------------------------------------------- #
def test_valid_search_config():
    sc = SearchConfig(name="rtx", text="rtx 3090", category=83)
    sc.validate()


@pytest.mark.parametrize(
    "kwargs, match",
    [
        # neither text nor category
        ({"name": "vide", "text": "", "category": None}, "text"),
        # inverted price range
        ({"name": "prix", "text": "rtx", "price_min": 500, "price_max": 100}, "price"),
        # non-positive age window
        ({"name": "age", "text": "rtx", "max_age_hours": 0}, "max_age"),
        # require must be a list
        ({"name": "require", "text": "rtx", "require": "rtx"}, "require"),
        # exclude must be a list
        ({"name": "exclude", "text": "rtx", "exclude": "3060"}, "exclude"),
        # seller_types must be a list (le message nomme la recherche, pas le champ)
        ({"name": "types", "text": "rtx", "seller_types": "private"}, "liste attendue"),
        # locations must be a list
        ({"name": "lieux", "text": "rtx", "locations": "75"}, "liste attendue"),
    ],
)
def test_invalid_search_config_raises(kwargs, match):
    with pytest.raises(ValueError) as excinfo:
        SearchConfig(**kwargs).validate()
    assert match in str(excinfo.value)


def test_pages_zero_is_not_rejected_by_validate():
    """Documented behaviour: ``pages`` is not validated (only clamped later)."""
    SearchConfig(name="p", text="rtx", pages=0).validate()


def test_as_query_shape():
    sc = SearchConfig(
        name="rtx",
        text="rtx 3090",
        category=83,
        price_min=200,
        price_max=800,
        enums={"soustype": ["offer", "used"]},
        locations=["paris", "75"],
    )
    q = sc.as_query()
    assert q["text"] == "rtx 3090"
    assert q["category"] == "83"
    assert q["price"] == "200-800"
    assert q["soustype"] == "offer,used"
    assert q["locations"] == "paris,75"


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"name": "rtx", "text": "rtx"}, {"text": "rtx"}),
        ({"name": "cat", "category": 83}, {"category": "83"}),
        # only one price bound known -> the other side stays empty
        ({"name": "lo", "text": "rtx", "price_min": 200}, {"text": "rtx", "price": "200-"}),
        ({"name": "hi", "text": "rtx", "price_max": 800}, {"text": "rtx", "price": "-800"}),
    ],
)
def test_as_query_omits_empty_values(kwargs, expected):
    assert SearchConfig(**kwargs).as_query() == expected


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value, expected",
    [
        ("RTX 3090 !", "rtx-3090"),
        ("ddr4 32go", "ddr4-32go"),
        ("  à l'est, 75  ", "l-est-75"),
        ("___", ""),
    ],
)
def test_slugify(value, expected):
    assert slugify(value) == expected


def test_default_config_path_finds_config_yml():
    found = default_config_path(PROJECT_ROOT)
    assert found is not None
    assert found.name in DEFAULT_CONFIG_NAMES
    assert found == CONFIG_YML


def test_default_config_path_returns_none_when_absent(tmp_path):
    assert default_config_path(tmp_path) is None
