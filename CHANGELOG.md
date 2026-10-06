# Ändringslogg

## [Unreleased]

## [2.1.0] — 2026-10-06

### Tillagt

- `efterfyll_typkod.py` fyller i CELLAR:s typkod för cachade akter som sparades utan den
  eller med typen `dom`, så att typfiltret i `sok_i_cachade_akter` träffar dem. `--torrkorning`
  visar ändringarna utan att skriva.

### Fixat

- Samtidiga sökanrop kunde krascha servern med SIGSEGV när embeddingmodellen kördes på
  Apple-GPU:n (MPS). PyTorchs MPS-backend fyller sina kärncacher utan lås första gången de
  används, och verktygen körs på parallella arbetstrådar. Alla `encode()`-anrop i processen
  går nu genom ett gemensamt lås.

## [2.0.0] — 2026-09-26

### Ändrat
- Frågeexpansion på serversidan har inget förvalt modellnamn. `QUERY_EXPANSION_MODEL` anges alltid i `.env` (platshållare `<modellnamn>` i `config.example.env`).
- Texterna är produktneutrala: README, konfigurationsexempel, kommentarer och äldre CHANGELOG-poster nämner MCP-klienten i stället för en viss klient.
- User-Agent-strängen följer huvudversionen (härleds ur `VERSION`).
- **Brytande:** kräver MCP Python SDK 2.x (`mcp>=2.0,<3`). Servern bygger på
  `MCPServer`, rapporterar sin version och har cachningshintar.
- **Brytande:** http-läget kräver `MCP_API_KEY` och startar inte utan den
  (exitkod 2). Transporten är Streamable HTTP på `/mcp`, standardport 8010.
- **Brytande (returstruktur):** förväntade fel returneras inte längre som
  `{"fel": ...}` utan som felsvar (`isError`) med ett begripligt meddelande:
  okänt CELEX-nummer, språk som saknas, okänd språkkod, okänd typ, artikel
  som inte finns, SPARQL-tidsgräns och fel mot CELLAR. `hitta_nationellt_genomforande`
  ger felsvar när CELLAR-frågan misslyckas i stället för en tom lista.
- Texten hämtas i första hand genom innehållsförhandling mot
  `publications.europa.eu/resource/celex/{CELEX}` (`Accept`, `Accept-Language`).
  Den äldre vägen `{CELEX}.{SPRÅK}.{format}` provas därefter.
- Tribunalens och Personaldomstolens avgöranden hämtas med fulltext som
  EU-domstolens, i stället för enbart metadata.
- Alla verktyg har titel, annotationer och utdataschema (`outputSchema`).
- Projektets egen User-Agent används mot alla källor; ny variabel
  `CELLAR_USER_AGENT`.
- SPARQL-frågor som gäller en viss akt binder CELEX-numret direkt och tar
  under en sekund i stället för tiotals sekunder.
- Embeddings räknas bara när de lagras (PostgreSQL).

### Tillagt
- `omindexera_chunkar.py`: delar om cachade akter i chunkar och räknar om embeddings, utan nya anrop mot CELLAR.
- Felbeskedet när en akt saknas på det begärda språket listar de språk och
  format CELLAR har.
- PDF hämtas med den PDF-typ CELLAR anger (`application/pdf;type=pdfa1a` m.fl.).
- `hitta_nationellt_genomforande`: fältet `riksdag_fel` när sökningen i
  riksdagens öppna data misslyckas.

### Rättat
- Chunkningen delade på tomrader, som den rensade texten saknar, så varje
  akt blev en enda chunk och semantisk sökning såg bara aktens början. Nu
  delas texten i chunks om högst 250 ord (inom embeddingmodellens 384
  tokens) med 40 ords överlapp, och en artikelrubrik börjar en ny chunk.
  **Redan cachade akter behåller sina gamla chunkar** tills de omindexeras
  med `python3 omindexera_chunkar.py` (se README).
- Sökningen i riksdagens öppna data skickade inte projektets User-Agent.
- Cachen och direkthämtningen svarar likadant: `artikel` som inte finns ger
  felsvar även från cachen (i stället för hela akten), och artikelrubriker
  på engelska och franska (`Article N`) känns igen. Cachen används bara när
  akten ligger där på det begärda språket; annars hämtas den på nytt.
  Cachen håller fortfarande en språkversion per akt, den senast hämtade.
- Ett dokument i flera delar (DOC_1, DOC_2 ...) där en del inte kunde hämtas
  returnerades tyst utan den delen. Tillfälliga fel prövas nu en gång till;
  saknas en del ändå ges ett felsvar som namnger den, i stället för en
  ofullständig text som ser komplett ut och sparas i cachen.
- http-läget startade inte (anropade `mcp.get_asgi_app()`, som inte finns).
- Texthämtningen gav 404 för bl.a. 32016R0679 och 32024R1689 trots att
  CELLAR har texten.
- Aktens typkod sparades aldrig i cachen, och avgöranden sparades med typen
  `dom`, så typfiltret i `sok_i_cachade_akter` träffade inte nyhämtade akter.
  Redan cachade rader behåller sitt tidigare värde tills akten hämtas om.
- Lat inläsning av embeddingmodellen är skyddad med lås, eftersom verktygen
  körs på arbetstrådar.
- `pdfplumber` saknades i `requirements.txt`.

### Borttaget
- `SPARQLWrapper` ur `requirements.txt`; SPARQL-anropen görs med `requests`.
- EUR-Lex som hämtväg (TXT/HTML och LexUriServ). All data hämtas från CELLAR,
  den auktoritativa källan som EUR-Lex bygger på. Akter som reserven var till
  för, äldre originalakter utan xhtml, levererar CELLAR som html. Servern
  anropar därmed inte längre en tjänst bakom botskydd (AWS WAF).
- `[cli]`-extrat i `mcp`-beroendet, som inte används.
- Egen Starlette-app och Bearer-middleware; transporten sköts av `mcp_transport.py`.

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
