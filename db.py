"""
db.py — Databashantering för CELLAR EU-rätt-servern.

Stödjer PostgreSQL (med pgvector) och SQLite (utan vektorsökning).
Backend väljs automatiskt via DATABASE_URL-prefix:

  postgresql://...  →  PostgreSQL (cellar_eu-schema, pgvector, FTS)
  sqlite:///...     →  SQLite (enkla tabeller, LIKE-sökning, ingen vektorsökning)

Schema (PostgreSQL): cellar_eu
Tabeller:
  akt_cache   — cachad metadata och fulltext per rättsakt (nyckel: celex)
  akt_chunks  — chunkar med pgvector-embeddings för semantisk sökning
                (SQLite: chunkar sparas men embeddings utelämnas)
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).parent.resolve()

DATABASE_URL = os.getenv("DATABASE_URL", "")


# ---------------------------------------------------------------------------
# Backend-hjälpare
# ---------------------------------------------------------------------------

def _ar_postgres() -> bool:
    """Returnerar True om DATABASE_URL pekar på PostgreSQL."""
    return DATABASE_URL.startswith("postgresql")


def _hamta_db():
    """Öppnar databasanslutning — PostgreSQL eller SQLite beroende på DATABASE_URL."""
    if _ar_postgres():
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    else:
        import sqlite3
        db_fil = DATABASE_URL.replace("sqlite:///", "") or "cellar_eu_cache.db"
        if not os.path.isabs(db_fil):
            db_fil = str(_SCRIPT_DIR / db_fil)
        conn = sqlite3.connect(db_fil)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


@contextlib.contextmanager
def _cursor(conn):
    """Kontexthanterare för databascursor (PostgreSQL och SQLite)."""
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


def _prefix(tabell: str) -> str:
    """Returnerar fullt kvalificerat tabellnamn (med schema-prefix för PostgreSQL)."""
    return f"cellar_eu.{tabell}" if _ar_postgres() else tabell


def _ph() -> str:
    """Returnerar rätt platshållarsymbol för parametrar (%s för PG, ? för SQLite)."""
    return "%s" if _ar_postgres() else "?"


# ---------------------------------------------------------------------------
# DDL-scheman
# ---------------------------------------------------------------------------

_DDL_POSTGRES = """
CREATE SCHEMA IF NOT EXISTS cellar_eu;

CREATE TABLE IF NOT EXISTS cellar_eu.akt_cache (
    celex           TEXT PRIMARY KEY,
    sprak           TEXT NOT NULL DEFAULT 'SV',
    titel           TEXT,
    datum           DATE,
    eli             TEXT,
    typ             TEXT,
    fulltext_md     TEXT,
    fulltext_tsv    TSVECTOR
                    GENERATED ALWAYS AS (
                        to_tsvector('swedish', coalesce(fulltext_md, ''))
                    ) STORED,
    hamtad_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    uppdaterad_ts   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS cellar_eu_akt_fts_gin
    ON cellar_eu.akt_cache USING GIN (fulltext_tsv);

CREATE INDEX IF NOT EXISTS cellar_eu_akt_datum_idx
    ON cellar_eu.akt_cache (datum);

CREATE INDEX IF NOT EXISTS cellar_eu_akt_typ_idx
    ON cellar_eu.akt_cache (typ);

CREATE TABLE IF NOT EXISTS cellar_eu.akt_chunks (
    id          BIGSERIAL PRIMARY KEY,
    celex       TEXT NOT NULL
                REFERENCES cellar_eu.akt_cache(celex) ON DELETE CASCADE,
    chunk_nr    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   vector(768),
    UNIQUE (celex, chunk_nr)
);

CREATE INDEX IF NOT EXISTS cellar_eu_chunks_emb_idx
    ON cellar_eu.akt_chunks
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
"""

# SQLite — förenklar bort: GIN/ivfflat-index, GENERATED ALWAYS AS,
# TSVECTOR, BIGSERIAL, TIMESTAMPTZ, DATE, pgvector.
_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS akt_cache (
    celex           TEXT PRIMARY KEY,
    sprak           TEXT NOT NULL DEFAULT 'SV',
    titel           TEXT,
    datum           TEXT,
    eli             TEXT,
    typ             TEXT,
    fulltext_md     TEXT,
    hamtad_ts       TEXT NOT NULL DEFAULT (datetime('now')),
    uppdaterad_ts   TEXT
);

CREATE INDEX IF NOT EXISTS akt_cache_datum_idx ON akt_cache (datum);
CREATE INDEX IF NOT EXISTS akt_cache_typ_idx ON akt_cache (typ);

CREATE TABLE IF NOT EXISTS akt_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    celex       TEXT NOT NULL,
    chunk_nr    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    UNIQUE (celex, chunk_nr),
    FOREIGN KEY (celex) REFERENCES akt_cache(celex) ON DELETE CASCADE
);
"""


# ---------------------------------------------------------------------------
# Schemainitiering
# ---------------------------------------------------------------------------

def initiera_schema():
    """Skapar schema och tabeller om de inte redan finns.

    PostgreSQL kräver att pgvector-tillägget är installerat i databasen.
    SQLite: tabellerna skapas direkt utan schema-prefix.
    Databasfel vid uppstart loggas som varning men stoppar inte servern.
    """
    if not DATABASE_URL:
        log.warning("DATABASE_URL är inte satt — databasen används inte")
        return

    conn = None
    try:
        conn = _hamta_db()
        if _ar_postgres():
            conn.autocommit = True
            with _cursor(conn) as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cur.execute(_DDL_POSTGRES)
        else:
            conn.executescript(_DDL_SQLITE)
        log.info(
            "Databasschemat är klart (%s)",
            "PostgreSQL" if _ar_postgres() else "SQLite",
        )
    except Exception as exc:
        log.warning("Kunde inte initiera databasen: %s", exc)
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Läsning
# ---------------------------------------------------------------------------

def hamta_cachad_akt(celex: str) -> Optional[dict]:
    """Returnerar cachad akt om den finns, annars None."""
    if not DATABASE_URL:
        return None
    conn = None
    try:
        conn = _hamta_db()
        tabell = _prefix("akt_cache")
        ph = _ph()
        with _cursor(conn) as cur:
            if _ar_postgres():
                cur.execute(
                    f"SELECT celex, sprak, titel, datum::text, eli, typ, "
                    f"fulltext_md, hamtad_ts::text FROM {tabell} WHERE celex = {ph}",
                    (celex,),
                )
                rad = cur.fetchone()
                if rad:
                    kolumner = ["celex", "sprak", "titel", "datum", "eli",
                                "typ", "fulltext_md", "hamtad_ts"]
                    return dict(zip(kolumner, rad))
            else:
                cur.execute(
                    f"SELECT celex, sprak, titel, datum, eli, typ, "
                    f"fulltext_md, hamtad_ts FROM {tabell} WHERE celex = {ph}",
                    (celex,),
                )
                rad = cur.fetchone()
                if rad:
                    return dict(rad)
        return None
    except Exception as exc:
        log.warning("Fel vid läsning av akt_cache: %s", exc)
        return None
    finally:
        if conn:
            conn.close()


def sok_fts(fraga: str, typ: Optional[str], ar_fran: Optional[int],
            ar_till: Optional[int], max_antal: int) -> list[dict]:
    """Söker i cachade akters fulltext.

    PostgreSQL: FTS med plainto_tsquery (svenska) + ts_rank för relevansordering.
    SQLite: LIKE-baserad sökning — varje ord matchas med AND-logik.
    Returnerar lista med matchande akter sorterade på relevans/datum.
    """
    if not DATABASE_URL:
        return []
    conn = None
    try:
        conn = _hamta_db()
        tabell = _prefix("akt_cache")
        with _cursor(conn) as cur:
            if _ar_postgres():
                villkor = ["fulltext_tsv @@ plainto_tsquery('swedish', %s)"]
                parametrar: list = [fraga]

                if typ:
                    villkor.append("typ = %s")
                    parametrar.append(typ)
                if ar_fran:
                    villkor.append("EXTRACT(YEAR FROM datum) >= %s")
                    parametrar.append(ar_fran)
                if ar_till:
                    villkor.append("EXTRACT(YEAR FROM datum) <= %s")
                    parametrar.append(ar_till)

                where = " AND ".join(villkor)
                parametrar.append(max_antal)

                cur.execute(
                    f"""SELECT celex, titel, datum::text, typ, eli,
                               ts_rank(fulltext_tsv,
                                       plainto_tsquery('swedish', %s)) AS rang,
                               LEFT(fulltext_md, 500) AS utdrag
                        FROM {tabell}
                        WHERE {where}
                        ORDER BY rang DESC
                        LIMIT %s""",
                    [fraga] + parametrar,
                )
                rader = cur.fetchall()
                kolumner = ["celex", "titel", "datum", "typ", "eli", "rang", "utdrag"]
                return [dict(zip(kolumner, rad)) for rad in rader]

            else:
                # SQLite — LIKE-sökning, AND-logik: varje ord måste finnas
                # i fulltext_md ELLER titel. Splittning på mellanslag undviks
                # i publicera-kb, men här är fragan intern FTS-term, ej
                # kommaseparerad OR-lista — AND-logik per ord är korrekt.
                termer = fraga.strip().split()
                if not termer:
                    return []

                ord_villkor = []
                parametrar_sq: list = []
                for term in termer:
                    ord_villkor.append(
                        "(lower(fulltext_md) LIKE lower(?) "
                        "OR lower(titel) LIKE lower(?))"
                    )
                    parametrar_sq.extend([f"%{term}%", f"%{term}%"])

                extra_villkor: list[str] = []
                if typ:
                    extra_villkor.append("typ = ?")
                    parametrar_sq.append(typ)
                if ar_fran:
                    extra_villkor.append("substr(datum, 1, 4) >= ?")
                    parametrar_sq.append(str(ar_fran))
                if ar_till:
                    extra_villkor.append("substr(datum, 1, 4) <= ?")
                    parametrar_sq.append(str(ar_till))

                alla_villkor = ord_villkor + extra_villkor
                where = " AND ".join(alla_villkor) if alla_villkor else "1=1"
                parametrar_sq.append(max_antal)

                cur.execute(
                    f"""SELECT celex, titel, datum, typ, eli,
                               0.0 AS rang,
                               substr(fulltext_md, 1, 500) AS utdrag
                        FROM {tabell}
                        WHERE {where}
                        ORDER BY datum DESC
                        LIMIT ?""",
                    parametrar_sq,
                )
                rader = cur.fetchall()
                return [dict(rad) for rad in rader]

    except Exception as exc:
        log.warning("FTS-sökning misslyckades: %s", exc)
        return []
    finally:
        if conn:
            conn.close()


def sok_semantisk(vektor: list[float], typ: Optional[str],
                  ar_fran: Optional[int], ar_till: Optional[int],
                  max_antal: int) -> list[dict]:
    """Semantisk sökning med pgvector cosine distance.

    Kräver PostgreSQL med pgvector — returnerar tom lista för SQLite.
    """
    if not DATABASE_URL or not _ar_postgres():
        return []
    conn = None
    try:
        conn = _hamta_db()
        akt_tab = _prefix("akt_cache")
        chunk_tab = _prefix("akt_chunks")

        join_villkor: list[str] = []
        parametrar: list = []

        if typ:
            join_villkor.append("a.typ = %s")
            parametrar.append(typ)
        if ar_fran:
            join_villkor.append("EXTRACT(YEAR FROM a.datum) >= %s")
            parametrar.append(ar_fran)
        if ar_till:
            join_villkor.append("EXTRACT(YEAR FROM a.datum) <= %s")
            parametrar.append(ar_till)

        where = ("AND " + " AND ".join(join_villkor)) if join_villkor else ""
        parametrar += [vektor, max_antal]

        with _cursor(conn) as cur:
            cur.execute(
                f"""SELECT a.celex, a.titel, a.datum::text, a.typ, a.eli,
                           1 - (c.embedding <=> %s::vector) AS likhet,
                           LEFT(c.text, 500) AS utdrag
                    FROM {chunk_tab} c
                    JOIN {akt_tab} a ON a.celex = c.celex
                    WHERE c.embedding IS NOT NULL {where}
                    ORDER BY c.embedding <=> %s::vector
                    LIMIT %s""",
                parametrar[:-1] + [vektor] + [max_antal],
            )
            rader = cur.fetchall()

        kolumner = ["celex", "titel", "datum", "typ", "eli", "likhet", "utdrag"]
        return [dict(zip(kolumner, rad)) for rad in rader]

    except Exception as exc:
        log.warning("Semantisk sökning misslyckades: %s", exc)
        return []
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Skrivning
# ---------------------------------------------------------------------------

def spara_akt(celex: str, sprak: str, titel: Optional[str], datum: Optional[str],
              eli: Optional[str], typ: Optional[str], fulltext: str):
    """Sparar eller uppdaterar en rättsakt i cachen."""
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = _hamta_db()
        tabell = _prefix("akt_cache")
        with _cursor(conn) as cur:
            if _ar_postgres():
                cur.execute(
                    f"""INSERT INTO {tabell}
                            (celex, sprak, titel, datum, eli, typ,
                             fulltext_md, uppdaterad_ts)
                        VALUES (%s, %s, %s, %s::date, %s, %s, %s, NOW())
                        ON CONFLICT (celex) DO UPDATE SET
                            sprak         = EXCLUDED.sprak,
                            titel         = EXCLUDED.titel,
                            datum         = EXCLUDED.datum,
                            eli           = EXCLUDED.eli,
                            typ           = EXCLUDED.typ,
                            fulltext_md   = EXCLUDED.fulltext_md,
                            uppdaterad_ts = NOW()""",
                    (celex, sprak, titel, datum, eli, typ, fulltext),
                )
            else:
                cur.execute(
                    f"""INSERT INTO {tabell}
                            (celex, sprak, titel, datum, eli, typ,
                             fulltext_md, uppdaterad_ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT(celex) DO UPDATE SET
                            sprak         = excluded.sprak,
                            titel         = excluded.titel,
                            datum         = excluded.datum,
                            eli           = excluded.eli,
                            typ           = excluded.typ,
                            fulltext_md   = excluded.fulltext_md,
                            uppdaterad_ts = datetime('now')""",
                    (celex, sprak, titel, datum, eli, typ, fulltext),
                )
        conn.commit()
        log.info("Sparade akt %s i cache", celex)
    except Exception as exc:
        log.warning("Kunde inte spara akt %s: %s", celex, exc)
    finally:
        if conn:
            conn.close()


def spara_chunks(celex: str, chunks: list[str], embeddings: list[list[float]]):
    """Sparar chunkar med embeddings (PostgreSQL) eller bara text (SQLite).

    PostgreSQL: sparar chunkar med pgvector-embeddings för semantisk sökning.
    SQLite: sparar bara texten — embeddings lagras ej (ingen vektorsökning).
    Ersätter alltid befintliga chunkar för samma celex.
    """
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = _hamta_db()
        tabell = _prefix("akt_chunks")
        with _cursor(conn) as cur:
            if _ar_postgres():
                cur.execute(
                    f"DELETE FROM {tabell} WHERE celex = %s", (celex,)
                )
                for i, (text, emb) in enumerate(zip(chunks, embeddings)):
                    cur.execute(
                        f"""INSERT INTO {tabell} (celex, chunk_nr, text, embedding)
                               VALUES (%s, %s, %s, %s::vector)""",
                        (celex, i, text, emb),
                    )
            else:
                cur.execute(
                    f"DELETE FROM {tabell} WHERE celex = ?", (celex,)
                )
                for i, text in enumerate(chunks):
                    cur.execute(
                        f"""INSERT INTO {tabell} (celex, chunk_nr, text)
                               VALUES (?, ?, ?)""",
                        (celex, i, text),
                    )
        conn.commit()
        log.info("Sparade %d chunkar för %s", len(chunks), celex)
    except Exception as exc:
        log.warning("Kunde inte spara chunkar för %s: %s", celex, exc)
    finally:
        if conn:
            conn.close()
