# Ändringslogg

## [1.0.0] — 2026-05-15

### Tillagt
- `sok_eu_metadata`: `sokterm`-parameter med kommaseparerad OR-logik och valfri LLM-driven query-expansion (QUERY_EXPANSION_ENABLED i .env)
- `prompts/expansion_prompt.txt`: promptmall för flerspråkig query-expansion mot CELLAR
- Format-fallback vid hämtning: xhtml → html → pdf via CELLAR REST WEMI-protokollet
- PDF-extraktion via pdfplumber (hanterar PDF/A och vanlig PDF)
- EUR-Lex-fallback: TXT/HTML-URL och LexUriServ vid 404 från CELLAR REST — täcker originalförordningar utan WEMI-manifestation i CELLAR
- SPARQL-discovery (`_sok_cellar_manifestationer`): frågar CDM WEMI-hierarkin (Work→Expression→Manifestation) för att fastställa vilka format som faktiskt finns — eliminerar blinda 404-anrop och styr format-loopen direkt till tillgängliga format
- `format`-fält i svar från `hamta_eu_akt` och `hamta_eu_mal`

### Ändrat
- `hamta_eu_akt` och `hamta_eu_mal`: felmeddelande uppdaterat till att redovisa alla provade fallback-steg

## [0.2.0] — 2026-05-14

### Tillagt
- SQLite-fallback för miljöer utan PostgreSQL
- `.gitignore`-fix: exkluderar `*.db` och `__pycache__`

## [0.1.0] — 2026-05-13

### Tillagt
- Fem MCP-verktyg: `hamta_eu_akt`, `hamta_eu_mal`, `sok_i_cachade_akter`, `sok_eu_metadata`, `hitta_nationellt_genomforande`
- Stöd för 21 verifierade dokumenttyper (direktiv, förordning, dom m.fl.)
- Tribunalen (T-xxx) och Personaldomstolen (F-xxx) via CELEX-parsing
- On-demand hämtning och PostgreSQL-caching med pgvector-embeddings
- FTS med `plainto_tsquery('swedish', ...)` och GIN-index
- Semantisk sökning med KBLab/sentence-bert-swedish-cased (768 dimensioner, IVFFlat)
- SPARQL-sökning mot CELLAR för metadata utan lokal cache
- Nationellt genomförande för alla 27 EU-länder via MEAS_NATION_IMPL + riksdag-API för Sverige
- HTTP-transport med Bearer-tokenautentisering (Starlette middleware)
- Korrigerade typ-URI:er: RECO (inte REC), ORDER (inte ORD), PROP_DIR/PROP_REG/PROP_DEC (inte COM_PROP)
