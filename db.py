"""
db.py — Databashantering för CELLAR EU-rätt-servern.

Schema: cellar_eu
Tabeller:
  akt_cache   — cachad metadata och fulltext per rättsakt (nyckel: celex)
  akt_chunks  — chunkar med pgvector-embeddings för semantisk sökning
"""

from __future__ import annotations

import os
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anslutning
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "")


def hamta_anslutning():
    """Returnerar en psycopg2-anslutning till PostgreSQL."""
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


# ---------------------------------------------------------------------------
# Schemainitiering
# ---------------------------------------------------------------------------

_DDL = """
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


def initiera_schema():
    """Skapar schema och tabeller om de inte redan finns.

    Kräver att pgvector-tillägget är installerat i PostgreSQL-databasen.
    Databasfel vid uppstart loggas som varning men stoppar inte servern.
    """
    if not DATABASE_URL:
        log.warning("DATABASE_URL är inte satt — databasen används inte")
        return

    try:
        conn = hamta_anslutning()
        conn.autocommit = True
        cur = conn.cursor()

        # pgvector-tillägget krävs för embedding-kolumnen
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(_DDL)
        cur.close()
        conn.close()
        log.info("Databasschemat cellar_eu är klart")
    except Exception as exc:
        log.warning("Kunde inte initiera databasen: %s", exc)


# ---------------------------------------------------------------------------
# Läsning
# ---------------------------------------------------------------------------

def hamta_cachad_akt(celex: str) -> Optional[dict]:
    """Returnerar cachad akt om den finns, annars None."""
    if not DATABASE_URL:
        return None
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()
        cur.execute(
            """SELECT celex, sprak, titel, datum::text, eli, typ,
                      fulltext_md, hamtad_ts::text
               FROM cellar_eu.akt_cache
               WHERE celex = %s""",
            (celex,),
        )
        rad = cur.fetchone()
        cur.close()
        conn.close()
        if rad:
            kolumner = ["celex", "sprak", "titel", "datum", "eli",
                        "typ", "fulltext_md", "hamtad_ts"]
            return dict(zip(kolumner, rad))
        return None
    except Exception as exc:
        log.warning("Fel vid läsning av akt_cache: %s", exc)
        return None


def sok_fts(fraga: str, typ: Optional[str], ar_fran: Optional[int],
            ar_till: Optional[int], max_antal: int) -> list[dict]:
    """Söker med PostgreSQL FTS i cachade akters fulltext.

    Returnerar lista med matchande akter sorterade på relevans.
    """
    if not DATABASE_URL:
        return []
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()

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
                FROM cellar_eu.akt_cache
                WHERE {where}
                ORDER BY rang DESC
                LIMIT %s""",
            [fraga] + parametrar,
        )
        rader = cur.fetchall()
        cur.close()
        conn.close()

        kolumner = ["celex", "titel", "datum", "typ", "eli", "rang", "utdrag"]
        return [dict(zip(kolumner, rad)) for rad in rader]
    except Exception as exc:
        log.warning("FTS-sökning misslyckades: %s", exc)
        return []


def sok_semantisk(vektor: list[float], typ: Optional[str],
                  ar_fran: Optional[int], ar_till: Optional[int],
                  max_antal: int) -> list[dict]:
    """Semantisk sökning med pgvector cosine distance."""
    if not DATABASE_URL:
        return []
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()

        join_villkor = []
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

        cur.execute(
            f"""SELECT a.celex, a.titel, a.datum::text, a.typ, a.eli,
                       1 - (c.embedding <=> %s::vector) AS likhet,
                       LEFT(c.text, 500) AS utdrag
                FROM cellar_eu.akt_chunks c
                JOIN cellar_eu.akt_cache a ON a.celex = c.celex
                WHERE c.embedding IS NOT NULL {where}
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s""",
            parametrar[:-1] + [vektor] + [max_antal],
        )
        rader = cur.fetchall()
        cur.close()
        conn.close()

        kolumner = ["celex", "titel", "datum", "typ", "eli", "likhet", "utdrag"]
        return [dict(zip(kolumner, rad)) for rad in rader]
    except Exception as exc:
        log.warning("Semantisk sökning misslyckades: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Skrivning
# ---------------------------------------------------------------------------

def spara_akt(celex: str, sprak: str, titel: Optional[str], datum: Optional[str],
              eli: Optional[str], typ: Optional[str], fulltext: str):
    """Sparar eller uppdaterar en rättsakt i cachen."""
    if not DATABASE_URL:
        return
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO cellar_eu.akt_cache
                   (celex, sprak, titel, datum, eli, typ, fulltext_md, uppdaterad_ts)
               VALUES (%s, %s, %s, %s::date, %s, %s, %s, NOW())
               ON CONFLICT (celex) DO UPDATE SET
                   sprak = EXCLUDED.sprak,
                   titel = EXCLUDED.titel,
                   datum = EXCLUDED.datum,
                   eli   = EXCLUDED.eli,
                   typ   = EXCLUDED.typ,
                   fulltext_md   = EXCLUDED.fulltext_md,
                   uppdaterad_ts = NOW()""",
            (celex, sprak, titel, datum, eli, typ, fulltext),
        )
        conn.commit()
        cur.close()
        conn.close()
        log.info("Sparade akt %s i cache", celex)
    except Exception as exc:
        log.warning("Kunde inte spara akt %s: %s", celex, exc)


def spara_chunks(celex: str, chunks: list[str], embeddings: list[list[float]]):
    """Sparar chunkar med embeddings. Ersätter befintliga chunkar."""
    if not DATABASE_URL:
        return
    try:
        conn = hamta_anslutning()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM cellar_eu.akt_chunks WHERE celex = %s", (celex,)
        )
        for i, (text, emb) in enumerate(zip(chunks, embeddings)):
            cur.execute(
                """INSERT INTO cellar_eu.akt_chunks (celex, chunk_nr, text, embedding)
                   VALUES (%s, %s, %s, %s::vector)""",
                (celex, i, text, emb),
            )
        conn.commit()
        cur.close()
        conn.close()
        log.info("Sparade %d chunkar för %s", len(chunks), celex)
    except Exception as exc:
        log.warning("Kunde inte spara chunkar för %s: %s", celex, exc)
