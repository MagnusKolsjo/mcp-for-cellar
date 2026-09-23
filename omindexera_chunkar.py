"""
omindexera_chunkar.py — Delar om cachade akter i chunkar och räknar om embeddings.

Behövs efter en ändring av chunkningen: chunkarna skapas när en akt hämtas,
och en akt som redan ligger i cachen hämtas inte igen. Skriptet läser varje
cachad akts fulltext ur databasen och ersätter dess chunkar. Inget hämtas
från CELLAR.

Databasen väljs med DATABASE_URL i .env, som för servern. PostgreSQL får nya
embeddings (modellen laddas en gång); SQLite får bara chunktexten.

Användning:
    python3 omindexera_chunkar.py            # alla cachade akter
    python3 omindexera_chunkar.py 32016R0679 # bara angivna CELEX-nummer
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()  # före importen av db, som läser DATABASE_URL vid importtid

import db  # noqa: E402
import mcp_server  # noqa: E402


def main(urval: list[str]) -> int:
    if not db.DATABASE_URL:
        print("DATABASE_URL är inte satt.", file=sys.stderr)
        return 1
    akter = db.lista_cachade_akter()
    if urval:
        onskade = {c.strip().upper() for c in urval}
        akter = [(c, t) for c, t in akter if c in onskade]
    print(f"Omindexerar {len(akter)} akter "
          f"({'PostgreSQL' if db._ar_postgres() else 'SQLite'}).")
    for nr, (celex, fulltext) in enumerate(akter, 1):
        chunks = mcp_server._chunka_text(fulltext)
        if db._ar_postgres():
            embeddings = mcp_server._hamta_modell().encode(
                chunks, batch_size=8, convert_to_numpy=True,
            ).tolist()
        else:
            embeddings = []
        db.spara_chunks(celex, chunks, embeddings)
        print(f"  {nr}/{len(akter)} {celex}: {len(chunks)} chunkar")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
