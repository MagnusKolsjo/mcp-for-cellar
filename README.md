# cellar-eu — MCP-server för EU-rätt

MCP-server som ger AI-assistenter åtkomst till EU:s rättsliga informationssystem CELLAR/EUR-Lex. Servern hanterar hela EU:s normhierarki — förordningar, direktiv, beslut, domar och nationallt genomförande.

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

- **EU-domstolen** (C-xxx): fulltext + metadata via REST
- **Tribunalen** (T-xxx): metadata + länk till EUR-Lex (HTML ej tillgängligt via CELLAR REST API)
- **Personaldomstolen** (F-xxx): metadata + länk till EUR-Lex

## Förutsättningar

- Python 3.11+
- PostgreSQL 15+ med tillägget `pgvector`
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
# stdio (Claude Desktop)
python3 mcp_server.py

# HTTP
MCP_TRANSPORT=http python3 mcp_server.py
```

## Claude Desktop-konfiguration

Lägg till i `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cellar-eu": {
      "command": "/Users/DITTNAMN/MCP-Servers/cellar-eu/.venv/bin/python3",
      "args": ["/Users/DITTNAMN/MCP-Servers/cellar-eu/mcp_server.py"]
    }
  }
}
```

## Arkitektur

Servern hämtar dokument on-demand från CELLAR och cachar dem i PostgreSQL (`cellar_eu`-schemat). Sökning sker mot den lokala cachen via PostgreSQL FTS (`plainto_tsquery`) och pgvector IVFFlat (semantisk cosine-sökning med KBLabs svenska BERT-modell). Embeddings skapas vid inläggning i cachen.

SPARQL-sökning mot `publications.europa.eu/webapi/rdf/sparql` används för metadata-discovery utan att kräva lokal cache.
