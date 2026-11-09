"""État local : annonces déjà vues, dernière exécution, catalogue de catégories.

Tout est dans un seul fichier SQLite (`data/state.db`), ce qui rend le programme
repreunable où il s'est arrêté : une annonce déjà notifiée ne le sera jamais deux
fois, même après un redémarrage.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Ad

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_ads (
    search    TEXT NOT NULL,
    list_id   INTEGER NOT NULL,
    subject   TEXT,
    price     INTEGER,
    url       TEXT,
    published TEXT,
    first_seen TEXT NOT NULL,
    notified  INTEGER NOT NULL DEFAULT 0,
    body      TEXT,
    owner     TEXT,
    location  TEXT,
    rating    REAL,
    rating_count INTEGER,
    verdict   TEXT,
    verdict_reason TEXT,
    verdict_source TEXT,
    analyzed_at TEXT,
    PRIMARY KEY (search, list_id)
);

CREATE INDEX IF NOT EXISTS idx_seen_search ON seen_ads (search, first_seen);

CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    search      TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    fetched     INTEGER NOT NULL,
    kept        INTEGER NOT NULL,
    new_ads     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_search ON runs (search, finished_at);

CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sellers (
    user_id     TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS directives (
    scope  TEXT PRIMARY KEY,
    texte  TEXT NOT NULL
);
"""

# Colonnes ajoutées après la première version du schéma. Une base existante est
# complétée par ALTER TABLE (PRAGMA table_info) : aucune perte de données.
EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("body", "TEXT"),
    ("owner", "TEXT"),
    ("location", "TEXT"),
    ("rating", "REAL"),
    ("rating_count", "INTEGER"),
    ("verdict", "TEXT"),
    ("verdict_reason", "TEXT"),
    ("verdict_source", "TEXT"),
    ("analyzed_at", "TEXT"),
)


def _now() -> str:
    # Précision à la microseconde : deux cycles rapprochés (ou deux tests) ne
    # peuvent pas produire le même horodatage.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _rating_of(ad: Ad) -> tuple[float | None, int | None]:
    """Note du vendeur (0-5) et nombre d'avis, si la fiche vendeur est connue."""
    if not ad.owner:
        return None, None
    return ad.owner.rating, ad.owner.rating_count


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path), check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL")
        # L'IHM et le collecteur partagent la base : on attend au lieu d'échouer.
        self.con.execute("PRAGMA busy_timeout=5000")
        self.con.executescript(SCHEMA)
        self._migrate()
        self.con.commit()

    def _migrate(self) -> None:
        """Ajoute les colonnes manquantes sur une base déjà ancienne."""
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(seen_ads)")}
        for name, sql_type in EXTRA_COLUMNS:
            if name not in cols:
                self.con.execute(f"ALTER TABLE seen_ads ADD COLUMN {name} {sql_type}")
        # Créé après migration : l'index référence une colonne ajoutée.
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_verdict ON seen_ads (search, verdict)"
        )

    def close(self) -> None:
        self.con.close()

    # --- annonces -----------------------------------------------------------
    def is_seen(self, search: str, list_id: int) -> bool:
        row = self.con.execute(
            "SELECT 1 FROM seen_ads WHERE search=? AND list_id=?", (search, list_id)
        ).fetchone()
        return row is not None

    def record(self, search: str, ad: Ad, notified: bool) -> bool:
        """Enregistre l'annonce. Renvoie True si c'était une nouvelle annonce."""
        rating, rating_count = _rating_of(ad)
        cur = self.con.execute(
            """INSERT OR IGNORE INTO seen_ads
               (search, list_id, subject, price, url, published, first_seen, notified,
                body, owner, location, rating, rating_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                search,
                ad.list_id,
                ad.subject,
                ad.price,
                ad.url,
                (
                    ad.first_publication_date.isoformat()
                    if ad.first_publication_date
                    else None
                ),
                _now(),
                int(notified),
                ad.body or None,
                ad.owner.label if ad.owner else None,
                ad.location or None,
                rating,
                rating_count,
            ),
        )
        if cur.rowcount == 0 and notified:
            self.con.execute(
                "UPDATE seen_ads SET notified=1 WHERE search=? AND list_id=?",
                (search, ad.list_id),
            )
        self.con.commit()
        return cur.rowcount > 0

    def count_seen(self, search: str | None = None) -> int:
        if search:
            return self.con.execute(
                "SELECT COUNT(*) FROM seen_ads WHERE search=?", (search,)
            ).fetchone()[0]
        return self.con.execute("SELECT COUNT(*) FROM seen_ads").fetchone()[0]

    def counts(self, search: str | None = None) -> dict[str, int]:
        """total / retenu / rejete / inconnu, pour les badges de l'interface."""
        where, params = (("", ()) if not search
                         else ("WHERE search=?", (search,)))
        rows = self.con.execute(
            f"SELECT verdict, COUNT(*) FROM seen_ads {where} GROUP BY verdict",
            params,
        ).fetchall()
        out = {"total": 0, "retenu": 0, "rejete": 0, "inconnu": 0}
        for verdict, n in rows:
            out["total"] += n
            out[verdict if verdict in ("retenu", "rejete") else "inconnu"] += n
        return out

    def ads(
        self,
        search: str | None = None,
        verdict: str | None = None,
        q: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Annonces triées de la plus récente à la plus ancienne.

        ``verdict`` : ``retenu``, ``rejete``, ``inconnu`` ou ``all``.
        ``q`` : recherche texte dans le titre et la description.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if search:
            clauses.append("search=?")
            params.append(search)
        if verdict == "retenu":
            clauses.append("verdict='retenu'")
        elif verdict == "rejete":
            clauses.append("verdict='rejete'")
        elif verdict == "inconnu":
            clauses.append("verdict IS NULL")
        if q:
            clauses.append("(subject LIKE ? OR body LIKE ?)")
            like = f"%{q}%"
            params += [like, like]
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.con.execute(
            f"""SELECT search, list_id, subject, url, price, published, first_seen,
                       body, owner, location, rating, rating_count, verdict,
                       verdict_reason, verdict_source, analyzed_at, notified
                FROM seen_ads {where}
                ORDER BY COALESCE(published, first_seen) DESC, list_id DESC
                LIMIT ?""",
            (*params, int(limit)),
        ).fetchall()
        keys = ("search", "list_id", "subject", "url", "price", "published",
                "first_seen", "body", "owner", "location", "rating",
                "rating_count", "verdict", "verdict_reason", "verdict_source",
                "analyzed_at", "notified")
        return [dict(zip(keys, row)) for row in rows]

    def get_ad(self, search: str, list_id: int) -> dict[str, Any] | None:
        row = self.con.execute(
            """SELECT search, list_id, subject, url, price, published, first_seen,
                      body, owner, location, rating, rating_count,
                      verdict, verdict_reason, verdict_source, analyzed_at
               FROM seen_ads WHERE search=? AND list_id=?""",
            (search, list_id),
        ).fetchone()
        if not row:
            return None
        keys = ("search", "list_id", "subject", "url", "price", "published",
                "first_seen", "body", "owner", "location", "rating", "rating_count",
                "verdict", "verdict_reason", "verdict_source", "analyzed_at")
        return dict(zip(keys, row))

    def pending(self, search: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Annonces jamais analysées par l'IA (verdict vide)."""
        params: list[Any] = []
        where = "WHERE verdict IS NULL"
        if search:
            where += " AND search=?"
            params.append(search)
        rows = self.con.execute(
            f"SELECT search, list_id FROM seen_ads {where} "
            f"ORDER BY first_seen DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [{"search": s, "list_id": i} for s, i in rows]

    def set_verdict(
        self,
        search: str,
        list_id: int,
        verdict: str | None,
        reason: str | None = None,
        source: str = "ia",
    ) -> None:
        """Verdict manuel ou IA. ``verdict=None`` efface l'analyse."""
        self.con.execute(
            """UPDATE seen_ads
               SET verdict=?, verdict_reason=?, verdict_source=?, analyzed_at=?
               WHERE search=? AND list_id=?""",
            (verdict, reason, source if verdict else None,
             _now() if verdict else None, search, list_id),
        )
        self.con.commit()

    def set_body(self, search: str, list_id: int, body: str) -> None:
        """Range la description, absente des résultats de recherche."""
        self.con.execute(
            """
            UPDATE seen_ads SET body=? WHERE search=? AND list_id=?
            """,
            (body or None, search, list_id),
        )
        self.con.commit()

    # --- directives IA ------------------------------------------------------
    def get_directives(self) -> dict[str, str]:
        return {
            row[0]: row[1]
            for row in self.con.execute("SELECT scope, texte FROM directives")
        }

    def set_directive(self, scope: str, texte: str) -> None:
        if texte:
            self.con.execute(
                "INSERT OR REPLACE INTO directives (scope, texte) VALUES (?,?)",
                (scope, texte),
            )
        else:
            self.con.execute("DELETE FROM directives WHERE scope=?", (scope,))
        self.con.commit()

    def prune(self, before: datetime) -> int:
        """Oublie les vieilles annonces : la table ne grossit pas indéfiniment."""
        cur = self.con.execute(
            "DELETE FROM seen_ads WHERE first_seen < ?", (before.isoformat(),)
        )
        self.con.commit()
        return cur.rowcount

    # --- exécutions ---------------------------------------------------------
    def log_run(self, search: str, fetched: int, kept: int, new_ads: int) -> None:
        self.con.execute(
            "INSERT INTO runs (search, finished_at, fetched, kept, new_ads) VALUES (?,?,?,?,?)",
            (search, _now(), fetched, kept, new_ads),
        )
        self.con.commit()

    def last_run(self, search: str) -> str | None:
        row = self.con.execute(
            "SELECT finished_at FROM runs WHERE search=? "
            "ORDER BY finished_at DESC, run_id DESC LIMIT 1",
            (search,),
        ).fetchone()
        return row[0] if row else None

    # --- catégories ---------------------------------------------------------
    def remember_categories(self, ads: list[Ad]) -> None:
        rows = {(a.category_id, a.category_name) for a in ads if a.category_id and a.category_name}
        self.con.executemany(
            "INSERT OR REPLACE INTO categories (id, name) VALUES (?,?)", sorted(rows)
        )
        self.con.commit()

    def known_categories(self) -> list[tuple[int, str]]:
        return self.con.execute("SELECT id, name FROM categories ORDER BY name").fetchall()

    # --- fiches vendeur -----------------------------------------------------
    def get_seller(self, user_id: str, ttl_seconds: float) -> dict[str, Any] | None:
        """Fiche vendeur en cache, ou None si absente/périmée."""
        row = self.con.execute(
            "SELECT payload, fetched_at FROM sellers WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return None
        try:
            fetched = datetime.fromisoformat(row[1])
        except (TypeError, ValueError):
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - fetched).total_seconds()
        # Un TTL nul ou négatif demande un rafraîchissement systématique : une
        # fiche écrite à l'instant présent doit être considérée comme périmée.
        if ttl_seconds <= 0 or age > ttl_seconds:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def put_seller(self, user_id: str, payload: dict[str, Any]) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO sellers (user_id, payload, fetched_at) VALUES (?,?,?)",
            (user_id, json.dumps(payload, ensure_ascii=False), _now()),
        )
        self.con.commit()
