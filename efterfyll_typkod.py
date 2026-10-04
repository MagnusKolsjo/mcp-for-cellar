"""
efterfyll_typkod.py — Fyller i CELLAR:s typkod för cachade akter som saknar den.

Äldre versioner sparade aldrig aktens typkod (REG, DIR, JUDG ...), och
avgöranden sparades med typen 'dom'. Typfiltret i sok_i_cachade_akter
jämför mot typkoden, så sådana rader träffas inte. Skriptet hämtar typkoden
ur CELLAR:s SPARQL-tjänst för varje berörd rad och skriver in den. Fulltext
och chunkar lämnas orörda.

Databasen väljs med DATABASE_URL i .env, som för servern.

Användning:
    python3 efterfyll_typkod.py               # alla rader utan typkod
    python3 efterfyll_typkod.py --torrkorning # visa vad som skulle ändras
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()  # före importen av db, som läser DATABASE_URL vid importtid

import db  # noqa: E402
import mcp_server  # noqa: E402


def main(torrkorning: bool) -> int:
    if not db.DATABASE_URL:
        print("DATABASE_URL är inte satt.", file=sys.stderr)
        return 1
    tabell, ph = db._prefix("akt_cache"), db._ph()
    conn = db._hamta_db()
    try:
        with db._cursor(conn) as cur:
            cur.execute(
                f"SELECT celex, typ FROM {tabell} "
                "WHERE typ IS NULL OR typ = '' OR typ = 'dom' ORDER BY celex"
            )
            rader = [(r[0], r[1]) for r in cur.fetchall()]
        print(f"{len(rader)} akter saknar typkod"
              f"{' (torrkörning)' if torrkorning else ''}.")
        andrade = 0
        for celex, gammal in rader:
            typ = (mcp_server._hamta_sparql_metadata(celex) or {}).get("typ")
            if not typ:
                print(f"  {celex}: ingen typkod i CELLAR, lämnas som {gammal!r}")
                continue
            print(f"  {celex}: {gammal!r} -> {typ}")
            if not torrkorning:
                with db._cursor(conn) as cur:
                    cur.execute(f"UPDATE {tabell} SET typ = {ph} WHERE celex = {ph}",
                                (typ, celex))
                andrade += 1
        if not torrkorning:
            conn.commit()
            print(f"{andrade} akter uppdaterade.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main("--torrkorning" in sys.argv[1:]))
