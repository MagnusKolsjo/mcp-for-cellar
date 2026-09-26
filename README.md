# cellar-eu — MCP-server för EU-rätt

MCP-server som ger AI-assistenter åtkomst till EU-rätt ur CELLAR, Publikationsbyråns arkiv och den auktoritativa källan bakom EUR-Lex. All data hämtas från CELLAR; EUR-Lex är bara en läsvy för människor och anropas inte av servern. Servern hanterar hela EU:s normhierarki — förordningar, direktiv, beslut, domar och nationallt genomförande.

## Tillgängliga verktyg

| Verktyg | Beskrivning |
|---|---|
| `hamta_eu_akt` | Hämtar en EU-rättsakt på CELEX-nummer. Returnerar fulltext och metadata, cachar i PostgreSQL. |
| `hamta_eu_mal` | Hämtar EU-domstolens eller Tribunalens avgöranden på målnummer (C-441/17, T-325/15 m.fl.). |
| `sok_i_cachade_akter` | FTS- och semantisk sökning bland lokalt cachade akter via PostgreSQL/pgvector. |
| `sok_eu_metadata` | SPARQL-sökning mot CELLAR för att hitta akter utan lokal cache. Stöder filtrering på typ, år och nyckelord. |
| `hitta_nationellt_genomforande` | Visar alla EU-länders nationella genomförandeåtgärder för ett direktiv, valfritt filtrerat per land. |

## Dokumenttyper som stöds

Förordning, delegerad förordning, genomförandeförordning, direktiv, delegerat direktiv, beslut, delegerat beslut, genomförandebeslut, rekommendation, yttrande, generaladvokatens yttrande, dom, processbeslut, förslag till direktiv/förordning/beslut, kommuniké, grönbok, vitbok, konsoliderade texter och nationella genomförandeåtgärder (MEAS_NATION_IMPL).

## Domstolstäckning

- **EU-domstolen** (C-xxx), **Tribunalen** (T-xxx) och **Personaldomstolen** (F-xxx):
  fulltext och metadata från CELLAR. Saknas avgörandet på önskat språk provas
  franska och sedan engelska.

## Texthämtning

Texten hämtas från CELLAR genom innehållsförhandling:
`GET http://publications.europa.eu/resource/celex/{CELEX}` med `Accept`
(`application/xhtml+xml`, `text/html` eller `application/pdf;type=...`) och
`Accept-Language` (trebokstavskod, t.ex. `swe`). CELLAR svarar 303 till
dokumentet. Äldre akter (t.ex. 31958R0001) finns bara som html och hämtas
på samma sätt. Räcker inte det provas den äldre vägen `{CELEX}.{SPRÅK}.{format}`.

Finns akten inte på det begärda språket säger felbeskedet vilka språk och
format CELLAR har, och saknar CELLAR text helt säger felbeskedet det.
Servern identifierar sig med en egen User-Agent
(`CELLAR_USER_AGENT`) och ser aldrig ut som en webbläsare.

## Förutsättningar

- Python 3.11+
- MCP Python SDK 2.x (`mcp>=2.0,<3`, installeras via `requirements.txt`)
- PostgreSQL 15+ med tillägget `pgvector`, eller SQLite (en lokal fil;
  LIKE-sökning, ingen semantisk sökning)
- Internetåtkomst mot `publications.europa.eu` (CELLAR)

## Installation

```
cp config.example.env .env
# Redigera .env — sätt DATABASE_URL
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Starta servern

```
# stdio (lokal MCP-klient)
python3 mcp_server.py

# HTTP (Streamable HTTP på http://MCP_HOST:MCP_PORT/mcp, standardport 8010)
MCP_TRANSPORT=http MCP_API_KEY=<NYCKEL> python3 mcp_server.py
```

http-läget kräver `MCP_API_KEY`. Utan nyckel avbryts uppstarten med
exitkod 2. Klienten skickar nyckeln som `Authorization: Bearer <NYCKEL>`;
anrop utan header får 401 och med fel nyckel 403. Generera en nyckel med
`python3 -c "import secrets; print(secrets.token_hex(32))"`.

## Konfiguration i MCP-klienten

Lägg till i MCP-klientens konfiguration (`mcpServers`):

```json
{
  "mcpServers": {
    "cellar-eu": {
      "command": "~/MCP-Servers/cellar-eu/.venv/bin/python3",
      "args": ["~/MCP-Servers/cellar-eu/mcp_server.py"]
    }
  }
}
```

## Arkitektur

Servern hämtar dokument on-demand från CELLAR och cachar dem i PostgreSQL (`cellar_eu`-schemat). Sökning sker mot den lokala cachen via PostgreSQL FTS (`plainto_tsquery`) och pgvector IVFFlat (semantisk cosine-sökning med KBLabs svenska BERT-modell). Embeddings skapas vid inläggning i cachen. Cachen håller en språkversion per akt, den senast hämtade; begärs ett annat språk hämtas akten på nytt.

SPARQL-sökning mot `publications.europa.eu/webapi/rdf/sparql` används för metadata-discovery utan att kräva lokal cache.

## Omindexering av chunkar

Chunkar och embeddings skapas när en akt hämtas första gången. Ändras
chunkningen får redan cachade akter inte de nya chunkarna av sig själva.
Kör då, med samma `.env` som servern:

```
python3 omindexera_chunkar.py              # alla cachade akter
python3 omindexera_chunkar.py 32016R0679   # bara vissa CELEX-nummer
```

Skriptet läser fulltexten ur cachen och ersätter chunkarna; inget hämtas
från CELLAR. Med PostgreSQL räknas embeddings om, vilket tar några sekunder
per akt. Ta gärna en säkerhetskopia av databasen först.
