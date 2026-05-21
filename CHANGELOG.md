# Ändringslogg

## [1.1.0] — 2026-05-21

### Rättat
- **B1** Artikel-regex: lade till `^`-ankare och `re.MULTILINE` på tre ställen i
  `_extrahera_artikel`, `hamta_eu_akt` (cache) och `hamta_eu_akt` (ny text) —
  förhindrar recital-träffar som "artikel 5 i fördraget" i stället för normativa artiklar
- **B2** `hitta_nationellt_genomforande`: bytte ut felaktiga SPARQL-predikat mot korrekta
  MEAS_NATION_IMPL-predikat (`measure_national_implementing_implements_resource_legal` och
  `measure_national_implementing_implemented_by_country`) på fyra ställen — regression från commit 51cd153
- **B3** `sok_semantisk` i `db.py`: rättade parameter-ordning i SQL-anropet —
  `[vektor] + filter_värden + [vektor, max_antal]`; semantisk sökning med filter
  gav tidigare alltid tom lista

### Förbättrat
- **Bg1** SPARQL-injection: ny hjälpfunktion `_sparql_escape()` applicerad på alla
  ställen där användarinput interpoleras i SPARQL-frågor
- **Bg2** `_sok_riksdag_propositioner`: max 5 träffar (var 10), filtrerar nu på
  direktivnumret i rubrik för högre precision
- **Bg3** `hitta_nationellt_genomforande`: ny `_NORMALISERA_LAND`-mapping — tvåbokstavs
  ISO 3166-1 (t.ex. "SE", "DE") normaliseras till trebokstavs koder som CELLAR kräver
- **K7** `_CDM_FORMAT_MAP`: lade till PDF/A 2 och 3 (PDFA2A, PDFA2B, PDFA3A, PDFA3B)
- **K8** Batch-embedding i `_indexera_akt`: `modell.encode(chunks, batch_size=8)` i
  stället för per-chunk-anrop

### Dokumentation
- **K1** Terminologi: "SQLite-fallback" → "SQLite-alternativ" i config och changelog
- **K2** `README.md`: `/Users/DITTNAMN/...` → `~/MCP-Servers/cellar-eu/` i config-exempel
- **K3** `config.example.env`: verklighetstroende platshållare ersatta med `<...>`-syntax
- **K4** Intern projektreferens borttagen ur kodkommentar
- **K5** `_chunka_text`: kommentar som förklarar medvetet avsteg från chunkstandarden
- **K6** HTTP-transport: kommentar om connection pool som uppgraderingsspår

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
- SQLite-alternativ för miljöer utan PostgreSQL
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
