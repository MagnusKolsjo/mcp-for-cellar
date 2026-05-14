# Ändringslogg

## [Unreleased]

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
