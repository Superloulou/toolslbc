"""Tests for ``lbc_alertes.store.Store`` (sqlite dedup + caches)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_ad
from lbc_alertes.store import Store


@pytest.fixture()
def store(tmp_path):
    st = Store(tmp_path / "t.db")
    try:
        yield st
    finally:
        st.close()


def _notified_flag(store: Store, search: str, list_id: int) -> int:
    """Read the flag straight from the DB (no Store API involved)."""
    row = store.con.execute(
        "SELECT notified FROM seen_ads WHERE search=? AND list_id=?", (search, list_id)
    ).fetchone()
    assert row is not None
    return row[0]


# ---------------------------------------------------------------- creation


def test_store_creates_file_and_tables(tmp_path):
    db = tmp_path / "nested" / "state.db"
    st = Store(db)
    try:
        assert db.exists()
        names = {
            r[0]
            for r in st.con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"seen_ads", "runs", "categories", "sellers"} <= names
    finally:
        st.close()


def test_record_is_new_the_first_time_only(store):
    ad = make_ad(list_id=111)
    assert store.record("rtx 3090", ad, notified=False) is True
    assert store.record("rtx 3090", ad, notified=False) is False
    assert store.record("rtx 3090", ad, notified=False) is False


def test_same_ad_under_two_searches_is_two_rows(store):
    ad = make_ad(list_id=222)
    assert store.record("rtx 3090", ad, notified=True) is True
    assert store.record("ddr4 32go", ad, notified=True) is True
    assert store.count_seen() == 2


def test_record_notified_flips_flag_on_existing_row(store):
    ad = make_ad(list_id=333)
    assert store.record("s", ad, notified=False) is True
    assert _notified_flag(store, "s", 333) == 0

    # Already seen: record() returns False but must still mark it notified.
    assert store.record("s", ad, notified=True) is False
    assert _notified_flag(store, "s", 333) == 1


def test_record_notified_false_never_unflags(store):
    ad = make_ad(list_id=444)
    store.record("s", ad, notified=True)
    store.record("s", ad, notified=False)
    assert _notified_flag(store, "s", 444) == 1


def test_record_stores_published_iso_or_none(store):
    published = datetime(2025, 5, 1, 8, 30, tzinfo=timezone.utc)
    ad = make_ad(list_id=555, published=published)
    store.record("s", ad, notified=False)
    (value,) = store.con.execute(
        "SELECT published FROM seen_ads WHERE list_id=555"
    ).fetchone()
    assert value == published.isoformat()

    dateless = make_ad(list_id=556)
    dateless.first_publication_date = None
    store.record("s", dateless, notified=False)
    (value,) = store.con.execute(
        "SELECT published FROM seen_ads WHERE list_id=556"
    ).fetchone()
    assert value is None


def test_is_seen(store):
    ad = make_ad(list_id=666)
    assert store.is_seen("s", 666) is False
    store.record("s", ad, notified=False)
    assert store.is_seen("s", 666) is True
    assert store.is_seen("autre", 666) is False


def test_count_seen_total_and_per_search(store):
    assert store.count_seen() == 0
    store.record("a", make_ad(list_id=1), notified=False)
    store.record("a", make_ad(list_id=2), notified=False)
    store.record("b", make_ad(list_id=3), notified=False)
    assert store.count_seen() == 3
    assert store.count_seen("a") == 2
    assert store.count_seen("b") == 1
    assert store.count_seen("inconnue") == 0


# ---------------------------------------------------------------- pruning


def test_prune_removes_only_older_rows(store):
    for list_id in (1, 2, 3):
        store.record("s", make_ad(list_id=list_id), notified=False)

    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    store.con.execute("UPDATE seen_ads SET first_seen=? WHERE list_id=1", (old,))
    store.con.execute("UPDATE seen_ads SET first_seen=? WHERE list_id=2", (recent,))
    store.con.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(days=45)
    assert store.prune(cutoff) == 1
    assert store.count_seen() == 2
    assert store.is_seen("s", 1) is False
    assert store.is_seen("s", 2) is True
    assert store.is_seen("s", 3) is True


def test_prune_on_empty_db_returns_zero(store):
    assert store.prune(datetime.now(timezone.utc)) == 0


# ---------------------------------------------------------------- runs


def test_log_run_and_last_run(store, monkeypatch):
    stamps = iter(["2025-05-01T10:00:00+00:00", "2025-05-01T11:30:00+00:00"])
    monkeypatch.setattr("lbc_alertes.store._now", lambda: next(stamps))

    assert store.last_run("s") is None
    store.log_run("s", fetched=35, kept=10, new_ads=3)
    first = store.last_run("s")
    assert isinstance(first, str) and first

    store.log_run("s", fetched=40, kept=12, new_ads=1)
    assert store.last_run("s") > first
    assert store.last_run("autre recherche") is None

    row = store.con.execute(
        "SELECT fetched, kept, new_ads FROM runs WHERE search='s' ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    assert row == (40, 12, 1)


def test_log_run_same_timestamp_does_not_collide(store, monkeypatch):
    """Deux exécutions dans la même seconde ne se marchent pas dessus.

    L'ancienne clé primaire ``(search, finished_at)`` levait une
    ``IntegrityError`` et faisait perdre la seconde ligne.
    """
    monkeypatch.setattr("lbc_alertes.store._now",
                        lambda: "2025-05-01T10:00:00+00:00")

    store.log_run("s", fetched=35, kept=10, new_ads=3)
    store.log_run("s", fetched=40, kept=12, new_ads=1)

    rows = store.con.execute(
        "SELECT fetched, kept FROM runs WHERE search='s' ORDER BY run_id"
    ).fetchall()
    assert rows == [(35, 10), (40, 12)]


# ---------------------------------------------------------------- categories


def test_remember_and_known_categories(store, ads):
    store.remember_categories(ads)
    known = store.known_categories()
    assert known, "le fixture contient des catégories"
    assert all(isinstance(cid, int) and name for cid, name in known)
    assert known == sorted(known, key=lambda row: row[1])

    # Idempotent: re-registering must not duplicate rows.
    store.remember_categories(ads)
    assert store.known_categories() == known


def test_remember_categories_skips_incomplete_ads(store):
    ad = make_ad(list_id=7)
    ad.category_name = None
    store.remember_categories([ad])
    assert store.known_categories() == []


# ---------------------------------------------------------------- sellers


def test_seller_round_trip(store, seller_payload):
    assert store.get_seller("u-1", 3600) is None
    store.put_seller("u-1", seller_payload)
    got = store.get_seller("u-1", 7 * 24 * 3600)
    assert got == seller_payload


def test_seller_ttl_zero_is_expired(store, seller_payload):
    store.put_seller("u-2", seller_payload)
    assert store.get_seller("u-2", 0) is None


def test_seller_ttl_negative_is_expired(store, seller_payload):
    store.put_seller("u-3", seller_payload)
    assert store.get_seller("u-3", -1) is None


def test_seller_payload_keeps_accents(store):
    payload = {"seller": {"name": "François é"}, "feedback": {"overall_score": 0.91}}
    store.put_seller("u-4", payload)
    assert store.get_seller("u-4", 3600) == payload


def test_seller_overwritten_by_put_seller(store):
    store.put_seller("u-5", {"v": 1})
    store.put_seller("u-5", {"v": 2})
    assert store.get_seller("u-5", 3600) == {"v": 2}
    (count,) = store.con.execute(
        "SELECT COUNT(*) FROM sellers WHERE user_id='u-5'"
    ).fetchone()
    assert count == 1


def test_get_seller_rejects_corrupt_rows(store):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.con.execute(
        "INSERT INTO sellers (user_id, payload, fetched_at) VALUES ('bad-json',?,?)",
        ("pas du json", now),
    )
    store.con.execute(
        "INSERT INTO sellers (user_id, payload, fetched_at) VALUES ('bad-date',?,'??')",
        ("{}",),
    )
    store.con.commit()
    assert store.get_seller("bad-json", 3600) is None
    assert store.get_seller("bad-date", 3600) is None


def test_get_seller_rejects_non_dict_payload(store):
    store.put_seller("u-6", [1, 2, 3])
    assert store.get_seller("u-6", 3600) is None


def test_store_survives_reopen(tmp_path):
    ad = make_ad(list_id=888)
    first = Store(tmp_path / "state.db")
    first.record("s", ad, notified=True)
    first.close()

    second = Store(tmp_path / "state.db")
    try:
        assert second.is_seen("s", 888) is True
        assert _notified_flag(second, "s", 888) == 1
    finally:
        second.close()


def test_primary_key_is_search_plus_list_id(tmp_path):
    st = Store(tmp_path / "pk.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            st.con.execute(
                "INSERT INTO seen_ads (search, list_id, notified) VALUES ('s',1,0)"
            ).execute(
                "INSERT INTO seen_ads (search, list_id, notified) VALUES ('s',1,0)"
            )
    finally:
        st.close()
