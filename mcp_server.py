"""
mcp_server.py — MCP-server för EU-rätt via CELLAR/EUR-Lex.

Fem verktyg:
  hamta_eu_akt              — Hämtar en EU-rättsakt via CELEX-nummer (on-demand, cachar i DB)
  hamta_eu_mal              — Hämtar EU-domstolens eller Tribunalens avgöranden (C-xxx eller T-xxx)
  sok_i_cachade_akter       — FTS + semantisk sökning bland lokalt cachade akter
  sok_eu_metadata           — SPARQL-sökning för att hitta akter utan att cacha
  hitta_nationellt_genomforande — Nationella genomförandeåtgärder per medlemsstat via CELLAR

Datakällor:
  SPARQL: http://publications.europa.eu/webapi/rdf/sparql
  REST:   https://publications.europa.eu/resource/celex/{CELEX}

Transport styrs via MCP_TRANSPORT i .env: stdio (standard) eller http.
"""

from __future__ import annotations

import os
import re
import logging
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()          # MÅSTE köras innan "import db" — db.DATABASE_URL läses vid importtid

import requests
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

import db

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent.resolve()

MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST      = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT      = int(os.getenv("MCP_PORT", "8010"))
MCP_API_KEY   = os.getenv("MCP_API_KEY", "")

SPARQL_ENDPOINT = os.getenv(
    "CELLAR_SPARQL_ENDPOINT",
    "http://publications.europa.eu/webapi/rdf/sparql",
)
CELLAR_REST_BASE = os.getenv(
    "CELLAR_REST_BASE",
    "http://publications.europa.eu/resource/celex",
)
SPARQL_TIMEOUT = int(os.getenv("SPARQL_TIMEOUT", "60"))
REST_TIMEOUT   = int(os.getenv("REST_TIMEOUT", "30"))
MAX_TECKEN     = int(os.getenv("CELLAR_MAX_TECKEN", "60000"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "KBLab/sentence-bert-swedish-cased")

# Query-expansion (valfritt)
QUERY_EXPANSION_ENABLED     = os.getenv("QUERY_EXPANSION_ENABLED", "false").lower() == "true"
QUERY_EXPANSION_BASE_URL    = os.getenv("QUERY_EXPANSION_BASE_URL",  "")
QUERY_EXPANSION_API_KEY     = os.getenv("QUERY_EXPANSION_API_KEY",   "")
QUERY_EXPANSION_MODEL       = os.getenv("QUERY_EXPANSION_MODEL",     "")
QUERY_EXPANSION_PROMPT_FILE = os.getenv(
    "QUERY_EXPANSION_PROMPT_FILE",
    str(_SCRIPT_DIR / "prompts" / "expansion_prompt.txt"),
)

# ---------------------------------------------------------------------------
# Loggning
# ---------------------------------------------------------------------------

def _konfigurera_logging() -> Path:
    log_mapp = _SCRIPT_DIR / "logs"
    log_mapp.mkdir(parents=True, exist_ok=True)
    log_fil = log_mapp / "mcp_server.log"
    logging.basicConfig(
        filename=str(log_fil),
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        encoding="utf-8",
    )
    return log_fil

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Query-expansion
# ---------------------------------------------------------------------------

def expandera_fraga(query: str) -> list[str]:
    """Expanderar söktermen med flerspråkiga juridiska ekvivalenter via LLM.

    Returnerar kompletterande söktermer på svenska, engelska, franska,
    tyska m.fl. EU-språk — eller tom lista om expansion är inaktiverat
    eller misslyckas.

    Aktiveras via QUERY_EXPANSION_ENABLED=true i .env. Stöder alla
    OpenAI-kompatibla endpoints (Claude, OpenAI, Ollama, LM Studio).
    Promptfilen (prompts/expansion_prompt.txt) kan redigeras fritt.
    """
    if not QUERY_EXPANSION_ENABLED:
        return []

    prompt_path = Path(QUERY_EXPANSION_PROMPT_FILE)
    if not prompt_path.exists():
        log.warning("Promptfil för query-expansion saknas: %s", prompt_path)
        return []

    try:
        from openai import OpenAI

        prompt_template = prompt_path.read_text(encoding="utf-8")
        prompt = prompt_template.format(query=query)

        client = OpenAI(
            base_url=QUERY_EXPANSION_BASE_URL or None,
            api_key=QUERY_EXPANSION_API_KEY or "placeholder",
        )
        response = client.chat.completions.create(
            model=QUERY_EXPANSION_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.1,
        )
        raw   = response.choices[0].message.content.strip()
        terms = [t.strip() for t in raw.split(",") if t.strip()]
        log.info("Query-expansion: %r → %s", query, terms)
        return terms[:10]

    except Exception as exc:
        log.warning("Query-expansion misslyckades (fortsätter utan): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Lazy-laddad embeddingmodell
# ---------------------------------------------------------------------------

_modell = None

def _hamta_modell():
    global _modell
    if _modell is None:
        from sentence_transformers import SentenceTransformer
        log.info("Laddar embeddingmodell: %s", EMBEDDING_MODEL)
        _modell = SentenceTransformer(EMBEDDING_MODEL)
    return _modell

# ---------------------------------------------------------------------------
# Konstanter: typ-URI:er verifierade mot live CELLAR
# ---------------------------------------------------------------------------

BASE_TYP = "http://publications.europa.eu/resource/authority/resource-type/"

TYP_URIS: dict[str, str] = {
    # Sekundärrätt — lagstiftningsakter
    "direktiv":                  BASE_TYP + "DIR",
    "forordning":                BASE_TYP + "REG",
    "forordning_delegerad":      BASE_TYP + "REG_DEL",
    "forordning_genomforande":   BASE_TYP + "REG_IMPL",
    "direktiv_delegerat":        BASE_TYP + "DIR_DEL",
    "beslut":                    BASE_TYP + "DEC",
    "beslut_genomforande":       BASE_TYP + "DEC_IMPL",
    "beslut_delegerat":          BASE_TYP + "DEC_DEL",
    "rekommendation":            BASE_TYP + "RECO",      # OBS: RECO, inte REC
    "yttrande":                  BASE_TYP + "OPIN",
    # Domstolsakter
    "dom":                       BASE_TYP + "JUDG",
    "beslut_domstol":            BASE_TYP + "ORDER",     # OBS: ORDER, inte ORD
    "yttrande_generaladvokat":   BASE_TYP + "OPIN_AG",
    # Förberedande akter
    "forslag_direktiv":          BASE_TYP + "PROP_DIR",
    "forslag_forordning":        BASE_TYP + "PROP_REG",
    "forslag_beslut":            BASE_TYP + "PROP_DEC",
    "kommunike":                 BASE_TYP + "COMMUNIC",
    "gronbok":                   BASE_TYP + "PAPER_GREEN",
    "vitbok":                    BASE_TYP + "PAPER_WHITE",
    # Specialtyper
    "konsoliderad":              BASE_TYP + "CONS_TEXT",
    "nationellt_genomforande":   BASE_TYP + "MEAS_NATION_IMPL",
}

TYP_BESKRIVING: dict[str, str] = {
    "direktiv":                "Direktiv — måste genomföras i nationell rätt",
    "forordning":              "Förordning — direkt tillämplig",
    "forordning_delegerad":    "Delegerad förordning (kommissionen)",
    "forordning_genomforande": "Genomförandeförordning (kommissionen)",
    "direktiv_delegerat":      "Delegerat direktiv (kommissionen)",
    "beslut":                  "Beslut — riktas till specifika mottagare",
    "beslut_genomforande":     "Genomförandebeslut",
    "beslut_delegerat":        "Delegerat beslut",
    "rekommendation":          "Rekommendation — ej rättsligt bindande",
    "yttrande":                "Yttrande — ej rättsligt bindande",
    "dom":                     "EU-domstolens eller Tribunalens dom",
    "beslut_domstol":          "Processuellt beslut från EU-domstolen eller Tribunalen",
    "yttrande_generaladvokat": "Generaladvokatens förslag till avgörande",
    "forslag_direktiv":        "Kommissionens förslag till direktiv",
    "forslag_forordning":      "Kommissionens förslag till förordning",
    "forslag_beslut":          "Kommissionens förslag till beslut",
    "kommunike":               "Kommissionskommuniké",
    "gronbok":                 "Grönbok — inledande konsultation",
    "vitbok":                  "Vitbok — politisk färdplan",
    "konsoliderad":            "Konsoliderad text — aktuell lydelse med alla ändringar",
    "nationellt_genomforande": "Nationell genomförandeåtgärd (anmäld till kommissionen)",
}

SPRAK_URIS: dict[str, str] = {
    "SV": "http://publications.europa.eu/resource/authority/language/SWE",
    "EN": "http://publications.europa.eu/resource/authority/language/ENG",
    "DE": "http://publications.europa.eu/resource/authority/language/DEU",
    "FR": "http://publications.europa.eu/resource/authority/language/FRA",
}

CELLAR_FORMAT_ORDNING    = ["xhtml", "html", "pdf"]
EURLEX_CONTENT_BASE      = "https://eur-lex.europa.eu/legal-content"
EURLEX_LEXURISERV_BASE   = "https://eur-lex.europa.eu/LexUriServ/LexUriServ.do"

# SPARQL-discovery: CDM representation-type URI-suffix → format-kod
# PDF/A-varianter mappas till "pdf" — pdfplumber hanterar dem.
# DOC och FMX4 skippas — kräver specialbibliotek och ger sällan bättre täckning.
_CDM_FORMAT_MAP: dict[str, str] = {
    "XHTML":  "xhtml",
    "HTML":   "html",
    "PDF":    "pdf",
    "PDFA1A": "pdf",
    "PDFA1B": "pdf",
    "PDFA2A": "pdf",
    "PDFA2B": "pdf",
    "PDFA3A": "pdf",
    "PDFA3B": "pdf",
}

STAT_URIS: dict[str, str] = {
    "SWE": "http://publications.europa.eu/resource/authority/country/SWE",
    "DEU": "http://publications.europa.eu/resource/authority/country/DEU",
    "FRA": "http://publications.europa.eu/resource/authority/country/FRA",
    "DNK": "http://publications.europa.eu/resource/authority/country/DNK",
    "NOR": "http://publications.europa.eu/resource/authority/country/NOR",
    "FIN": "http://publications.europa.eu/resource/authority/country/FIN",
    "NLD": "http://publications.europa.eu/resource/authority/country/NLD",
    "BEL": "http://publications.europa.eu/resource/authority/country/BEL",
    "AUT": "http://publications.europa.eu/resource/authority/country/AUT",
    "ITA": "http://publications.europa.eu/resource/authority/country/ITA",
    "ESP": "http://publications.europa.eu/resource/authority/country/ESP",
    "POL": "http://publications.europa.eu/resource/authority/country/POL",
    "HUN": "http://publications.europa.eu/resource/authority/country/HUN",
}

# Normalisering av tvåbokstavs ISO 3166-1 alpha-2 → trebokstavs ISO 639-2/T
# som CELLAR använder. Användare kan ange t.ex. "SE" och "SVE" utöver "SWE".
_NORMALISERA_LAND: dict[str, str] = {
    "SE":  "SWE", "SVE": "SWE",
    "DE":  "DEU",
    "FR":  "FRA",
    "DK":  "DNK",
    "NO":  "NOR",
    "FI":  "FIN",
    "NL":  "NLD",
    "BE":  "BEL",
    "AT":  "AUT",
    "IT":  "ITA",
    "ES":  "ESP",
    "PL":  "POL",
    "HU":  "HUN",
    "PT":  "PRT",
    "CZ":  "CZE",
    "SK":  "SVK",
    "RO":  "ROU",
    "BG":  "BGR",
    "HR":  "HRV",
    "SI":  "SVN",
    "EE":  "EST",
    "LV":  "LVA",
    "LT":  "LTU",
    "LU":  "LUX",
    "CY":  "CYP",
    "MT":  "MLT",
    "IE":  "IRL",
    "EL":  "GRC", "GR": "GRC",
}

# ---------------------------------------------------------------------------
# Regex för parsning
# ---------------------------------------------------------------------------

# C-441/17, C-30/19 PPU, C-284/16 RX
_CJ_MONSTER = re.compile(
    r'^C-(\d+)/(\d{2,4})(?:\s+(?:PPU|RX|RAP|OP))?$', re.IGNORECASE
)
# T-325/15, T-74/00
_TJ_MONSTER = re.compile(
    r'^T-(\d+)/(\d{2,4})$', re.IGNORECASE
)
# F-1/05 (Personaldomstolen, avvecklad 2016)
_FJ_MONSTER = re.compile(
    r'^F-(\d+)/(\d{2,4})$', re.IGNORECASE
)

# 32006L0054, 32016R0679
_CELEX_SEKUNDAR = re.compile(r'^3(\d{4})([LRD])0*(\d+)$', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

def _kora_sparql(query: str) -> list[dict]:
    """Kör en SPARQL-fråga mot CELLAR och returnerar rader som ordböcker."""
    log.info("Kör SPARQL (%d tecken)", len(query))
    svar = requests.post(
        SPARQL_ENDPOINT,
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=SPARQL_TIMEOUT,
    )
    svar.raise_for_status()
    data = svar.json()
    variabler = data.get("head", {}).get("vars", [])
    rader = []
    for bindning in data.get("results", {}).get("bindings", []):
        rad: dict[str, Optional[str]] = {}
        for var in variabler:
            rad[var] = bindning[var]["value"] if var in bindning else None
        rader.append(rad)
    log.info("SPARQL returnerade %d rader", len(rader))
    return rader


def _sparql_escape(s: str) -> str:
    """Escapar en sträng för inbäddning i SPARQL-literaler.

    Ersätter bakåtsnedstreck och citattecken för att förhindra
    att användarinput bryter ut ur SPARQL-strängliteraler.
    Appliceras på alla interpolerade värden i SPARQL-frågor.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _sok_cellar_manifestationer(celex: str, sprak: str) -> list[str]:
    """Frågar CELLAR via SPARQL om vilka manifestationstyper som finns.

    Returnerar en prioriterad lista med format-koder (t.ex. ['pdf']) i ordning
    xhtml > html > pdf. Tom lista = okänt eller SPARQL-fel — fallback till
    CELLAR_FORMAT_ORDNING-blind probing.

    Exempel: 32014R1143 (SV) → ['pdf'] om bara PDF-manifestation finns i CELLAR.
    """
    _lang_map = {"SV": "SWE", "EN": "ENG", "DE": "DEU", "FR": "FRA",
                 "DA": "DAN", "FI": "FIN", "NL": "NLD", "PL": "POL",
                 "ES": "SPA", "IT": "ITA", "PT": "POR", "CS": "CES",
                 "HU": "HUN", "RO": "RON", "SK": "SLK", "SL": "SLV"}
    lang = _lang_map.get(sprak.upper(), sprak.upper())
    sprak_uri = f"http://publications.europa.eu/resource/authority/language/{lang}"

    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?manif_type
WHERE {{
  ?work cdm:resource_legal_id_celex ?celex_val .
  FILTER(STR(?celex_val) = "{_sparql_escape(celex)}")

  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language <{sprak_uri}> .

  ?manif cdm:manifestation_manifests_expression ?expr ;
         cdm:manifestation_type ?manif_type .
}}"""

    try:
        rader = _kora_sparql(query)
    except Exception as exc:
        log.warning("SPARQL-discovery misslyckades för %s (%s): %s", celex, sprak, exc)
        return []

    format_koder: set[str] = set()
    for r in rader:
        typ_uri = r.get("manif_type") or ""
        typ_kod = typ_uri.split("/")[-1].upper()  # t.ex. "pdf"→"PDF", "xhtml"→"XHTML"
        fmt = _CDM_FORMAT_MAP.get(typ_kod)
        if fmt:
            format_koder.add(fmt)

    if not format_koder:
        log.info(
            "SPARQL-discovery: inga kända manifestationer för %s (%s) — "
            "råa typer: %s",
            celex, sprak,
            [r.get("manif_type", "").split("/")[-1] for r in rader] or "[]",
        )
        return []

    # Returnera i prioritetsordning: xhtml > html > pdf
    result = [f for f in CELLAR_FORMAT_ORDNING if f in format_koder]
    log.info("SPARQL-discovery: %s (%s) → %s", celex, sprak, result)
    return result


def _hamta_cellar_text(celex: str, sprak: str) -> tuple[str, str]:
    """Hämtar text för en EU-rättsakt via CELLAR REST API med format-fallback.

    Provar formaten i ordning: xhtml → html → pdf (se CELLAR_FORMAT_ORDNING).
    Returnerar (rå_text, format) där format är 'xhtml', 'html' eller 'pdf'.
    Kastar ValueError om inget format ger innehåll.

    Protokoll (CELLAR WEMI-modellen):
      1. GET {CELLAR_REST_BASE}/{CELEX}.{LANG}.{FORMAT}  →  HTTP 303
         Location: …/cellar/{uuid}.{expr}.{manif}/rdf/object/full
      2. Hämta RDF för att lista DOC-items, eller fall tillbaka på DOC_1.
      3. Hämta varje DOC-item och konkatenera innehållet.
    """
    lang2 = sprak.upper()                   # 2-bokstavs ISO 639-1 — används för EUR-Lex
    _lang_map = {"SV": "SWE", "EN": "ENG", "DE": "DEU", "FR": "FRA",
                 "DA": "DAN", "FI": "FIN", "NL": "NLD", "PL": "POL",
                 "ES": "SPA", "IT": "ITA", "PT": "POR", "CS": "CES",
                 "HU": "HUN", "RO": "RON", "SK": "SLK", "SL": "SLV"}
    lang = _lang_map.get(lang2, lang2)      # 3-bokstavs ISO 639-2/T — används för CELLAR

    sista_fel: Optional[str] = None

    # SPARQL-discovery: ta reda på vilka format som faktiskt finns i CELLAR
    # för att undvika blinda 404-anrop för varje format.
    tillgangliga_format = _sok_cellar_manifestationer(celex, sprak)
    format_att_prova = tillgangliga_format if tillgangliga_format else CELLAR_FORMAT_ORDNING
    if tillgangliga_format:
        log.info("SPARQL-discovery styr format-loop: %s", format_att_prova)
    else:
        log.info("SPARQL-discovery gav inga träffar — provar standardordning: %s", format_att_prova)

    for fmt in format_att_prova:
        manifest_url = f"{CELLAR_REST_BASE}/{celex}.{lang}.{fmt}"
        log.info("Provar CELLAR-format %s: %s", fmt, manifest_url)

        try:
            r1 = requests.get(manifest_url, timeout=REST_TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            sista_fel = str(exc)
            continue

        if r1.status_code == 404:
            log.info("Format %s saknas för %s (%s)", fmt, celex, lang)
            continue
        if r1.status_code != 303:
            sista_fel = f"HTTP {r1.status_code} för {fmt}"
            continue

        location = r1.headers.get("Location", "")
        if "/rdf/object/full" not in location:
            sista_fel = f"Oväntat Location-svar: {location}"
            continue

        manif_bas = location.replace("/rdf/object/full", "")

        # Hämta RDF och lista DOC-items
        try:
            rdf_text = requests.get(location, timeout=REST_TIMEOUT).text
            doc_urls = re.findall(
                r'rdf:resource="(' + re.escape(manif_bas) + r'/DOC_\d+)"',
                rdf_text,
            )
        except Exception:
            doc_urls = []
        if not doc_urls:
            doc_urls = [manif_bas + "/DOC_1"]

        # Hämta DOC-items
        innehall: list[bytes] = []
        for doc_url in doc_urls:
            log.info("Hämtar %s", doc_url)
            r = requests.get(doc_url, timeout=REST_TIMEOUT)
            if r.status_code == 200 and r.content:
                innehall.append(r.content)
            elif r.status_code == 404:
                break

        if not innehall:
            sista_fel = f"Tomt svar för format {fmt}"
            continue

        # Konvertera till text beroende på format
        if fmt in ("xhtml", "html"):
            return "\n".join(c.decode("utf-8", errors="replace") for c in innehall), fmt

        elif fmt == "pdf":
            try:
                import pdfplumber
                from io import BytesIO
                delar: list[str] = []
                for pdf_bytes in innehall:
                    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
                        for sida in pdf.pages:
                            text = sida.extract_text()
                            if text:
                                delar.append(text)
                if delar:
                    return "\n".join(delar), "pdf"
                sista_fel = "PDF utan extraherbar text"
            except ImportError:
                log.warning("pdfplumber saknas — PDF-fallback ej tillgänglig")
                sista_fel = "pdfplumber inte installerat"
            except Exception as exc:
                log.warning("PDF-extraktion misslyckades för %s: %s", celex, exc)
                sista_fel = str(exc)

    # Fallback 4: EUR-Lex direktlänk — täcker originaltexter som saknar CELLAR-manifestation
    _eurlex_headers = {
        # EUR-Lex har historiskt krävt Mozilla-prefixad UA — den gamla strängen
        # `Mozilla/5.0 (compatible; CELLAR-EU-MCP/1.0; ...)` verifierades fungera
        # 2026-05-18. compatible-syntax behålls med nya repo-namnet så vi följer
        # projektets UA-konvention så långt EUR-Lex tillåter.
        "User-Agent": "Mozilla/5.0 (compatible; mcp-for-cellar/1.0; +https://github.com/MagnusKolsjo/mcp-for-cellar)"
    }

    def _hamta_eurlex_html(url: str) -> Optional[str]:
        """Hämtar HTML från EUR-Lex. Hanterar 202 med ett omförsök efter 3 s."""
        import time
        for forsok in range(2):
            try:
                r = requests.get(
                    url, timeout=REST_TIMEOUT,
                    allow_redirects=True, headers=_eurlex_headers,
                )
                log.info(
                    "EUR-Lex svarade HTTP %d (%d tecken) för %s (försök %d)",
                    r.status_code, len(r.text), celex, forsok + 1,
                )
                if r.status_code == 200 and r.text.strip():
                    return r.text
                if r.status_code == 202 and forsok == 0:
                    log.info("EUR-Lex 202 — väntar 3 s och försöker igen")
                    time.sleep(3)
                    continue
                break
            except requests.RequestException as exc:
                log.warning("EUR-Lex nätverksfel (%s): %s", url, exc)
                break
        return None

    # 4a: Huvud-URL (TXT/HTML)
    eurlex_url = f"{EURLEX_CONTENT_BASE}/{lang2}/TXT/HTML/?uri=CELEX:{celex}"
    log.info("Provar EUR-Lex huvud-URL: %s", eurlex_url)
    text = _hamta_eurlex_html(eurlex_url)
    if text:
        return text, "html"

    # 4b: LexUriServ (äldre API, levererar direkt utan async-rendering)
    lexuriserv_url = f"{EURLEX_LEXURISERV_BASE}?uri=CELEX:{celex}:{lang2}:HTML"
    log.info("Provar EUR-Lex LexUriServ: %s", lexuriserv_url)
    text = _hamta_eurlex_html(lexuriserv_url)
    if text:
        return text, "html"

    sista_fel = f"Både EUR-Lex huvud-URL och LexUriServ misslyckades för {celex} ({lang2})"

    raise ValueError(
        f"Ingen textmanifestation hittades för {celex} ({lang2}). "
        f"Provade: {', '.join(CELLAR_FORMAT_ORDNING)} via CELLAR + HTML via EUR-Lex. "
        f"Senaste fel: {sista_fel}"
    )


def _rensa_html(html: str) -> str:
    """Extraherar ren text ur XHTML/HTML från CELLAR (utan trunkering).

    Returnerar alltid hela texten — trunkering sker separat vid behov.
    """
    soppa = BeautifulSoup(html, "html.parser")
    for tag in soppa(["script", "style", "nav", "header", "footer", "aside", "noscript"]):
        tag.decompose()
    text = soppa.get_text(separator="\n")
    rader = [r.strip() for r in text.splitlines() if r.strip()]
    return "\n".join(rader)


def _trunkera(text: str, max_tecken: int = MAX_TECKEN) -> str:
    """Trunkerar text med tydlig markering om den är längre än max_tecken."""
    if len(text) <= max_tecken:
        return text
    return (
        text[:max_tecken]
        + f"\n\n[Trunkerad vid {max_tecken:,} tecken. "
          f"Originaldokumentet är {len(text):,} tecken. "
          f"Använd parametern 'artikel' för att hämta ett specifikt artikelnummer.]"
    )


def _extrahera_artikel(html: str, artikel_nr: int) -> Optional[str]:
    """Extraherar ett specifikt artikelnummer ur CELLAR XHTML.

    CELLAR XHTML använder ELI-strukturen med id="art_N" på article-divs.
    Returnerar artikelns rena text, eller None om artikeln inte hittas.
    """
    soppa = BeautifulSoup(html, "html.parser")

    # ELI-standard: <div id="art_3"> eller <div id="art_3."> eller liknande
    kandidater = [
        soppa.find(id=f"art_{artikel_nr}"),
        soppa.find(id=f"art_{artikel_nr}."),
        soppa.find(id=f"d1e{artikel_nr}"),  # äldre mönster
    ]
    # Regex-baserad sökning om ovan misslyckas
    for tag in soppa.find_all(True, id=True):
        if re.search(rf'\bart_?{artikel_nr}\b', tag.get('id', ''), re.IGNORECASE):
            kandidater.append(tag)

    # Prova också att söka i klasser
    for klass in [f"eli-subdivision", "NormBody"]:
        for tag in soppa.find_all(class_=klass, id=True):
            if str(artikel_nr) in tag.get('id', ''):
                kandidater.append(tag)

    for div in filter(None, kandidater):
        text = div.get_text(separator="\n")
        rader = [r.strip() for r in text.splitlines() if r.strip()]
        result = "\n".join(rader)
        if len(result) > 20:  # Sanity check — ej tomt
            return result

    # Fallback: sök i ren text efter "Artikel N" i rad-inledning.
    # re.MULTILINE krävs för att ^-ankaret ska matcha radstarter,
    # inte bara dokumentets början — annars träffar recitaler som
    # "...artikel 5 i fördraget..." istället för normativa artiklar.
    ren_text = _rensa_html(html)
    monster = re.compile(
        rf'^(Artikel\s+{artikel_nr}\b.*?)(?=^Artikel\s+\d+\b|\Z)',
        re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )
    m = monster.search(ren_text)
    if m:
        utdrag = m.group(1).strip()
        if len(utdrag) > 20:
            return utdrag

    return None


def _chunka_text(text: str, max_ord: int = 400) -> list[str]:
    """Delar upp text i semantiska chunks om max max_ord ord.

    EU-rättsakter är ovanligt långa och innehåller tät normativ text där
    ett stycke ofta refererar till nästa. 400 ord (~2 400–3 200 tecken) per
    chunk ger bättre semantisk kontext än projektstandarden 800 tecken —
    ett medvetet avsteg motiverat av domänens dokumentstruktur.
    """
    stycken = [s.strip() for s in text.split("\n\n") if s.strip()]
    chunks, aktuell, raknare = [], [], 0
    for stycke in stycken:
        ord_antal = len(stycke.split())
        if raknare + ord_antal > max_ord and aktuell:
            chunks.append("\n\n".join(aktuell))
            aktuell, raknare = [], 0
        aktuell.append(stycke)
        raknare += ord_antal
    if aktuell:
        chunks.append("\n\n".join(aktuell))
    return [c for c in chunks if len(c.strip()) > 50]


def _parsera_malnum(malnum: str) -> tuple[str, str]:
    """Konverterar EU-domstolsmålnummer till CELEX-format.

    Returnerar (celex, domstol) där domstol är 'CJ', 'TJ' eller 'FJ'.

    C-441/17   → ('62017CJ0441', 'CJ')   EU-domstolen
    T-325/15   → ('62015TJ0325', 'TJ')   Tribunalen
    F-1/05     → ('62005FJ0001', 'FJ')   Personaldomstolen (avvecklad)
    """
    for monster, prefix, domstol in [
        (_CJ_MONSTER, "CJ", "CJ"),
        (_TJ_MONSTER, "TJ", "TJ"),
        (_FJ_MONSTER, "FJ", "FJ"),
    ]:
        m = monster.match(malnum.strip())
        if m:
            nr = m.group(1).zfill(4)
            ar = m.group(2)
            if len(ar) == 2:
                # År >= 30 antas vara 1930–1999, år < 30 antas vara 2000–2029
                ar = ("19" if int(ar) >= 30 else "20") + ar
            return f"6{ar}{prefix}{nr}", domstol
    return malnum, "okand"


def _parsera_celex_till_eu_nummer(celex: str) -> Optional[str]:
    """Extraherar EU-dokumentnumret ur ett sekundärrättsligt CELEX-nummer.

    32006L0054 → '2006/54'
    32016R0679 → '2016/679'
    """
    m = _CELEX_SEKUNDAR.match(celex.strip())
    if not m:
        return None
    ar  = m.group(1)
    nr  = str(int(m.group(3)))
    return f"{ar}/{nr}"


def _hamta_sparql_metadata(celex: str) -> Optional[dict]:
    """Hämtar titel, datum och ELI för ett CELEX via SPARQL."""
    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?titel ?datum ?eli ?typ
WHERE {{
  ?work cdm:resource_legal_id_celex ?celex_val ;
        cdm:work_date_document ?datum ;
        cdm:work_has_resource-type ?typ_uri .
  FILTER(STR(?celex_val) = "{_sparql_escape(celex)}")
  OPTIONAL {{ ?work cdm:resource_legal_eli ?eli . }}
  OPTIONAL {{
    ?expr cdm:expression_belongs_to_work ?work ;
          cdm:expression_uses_language
            <{SPRAK_URIS["SV"]}> ;
          cdm:expression_title ?titel .
  }}
}} LIMIT 1"""
    try:
        rader = _kora_sparql(query)
        if rader:
            r = rader[0]
            typ_kod = (r.get("typ_uri") or "").split("/")[-1]
            return {
                "titel": r.get("titel"),
                "datum": r.get("datum"),
                "eli":   r.get("eli"),
                "typ":   typ_kod,
            }
    except Exception as exc:
        log.warning("Kunde inte hämta SPARQL-metadata för %s: %s", celex, exc)
    return None


def _indexera_akt(celex: str, sprak: str, fulltext: str,
                  titel: Optional[str], datum: Optional[str],
                  eli: Optional[str], typ: Optional[str]):
    """Sparar FULL otrunkerad fulltext i DB + indexerar chunks med embeddings.

    fulltext ska vara den kompletta texten — trunkering sker först vid
    returnering till MCP-anroparen, inte vid lagring.
    """
    db.spara_akt(celex, sprak, titel, datum, eli, typ, fulltext)
    try:
        modell = _hamta_modell()
        chunks = _chunka_text(fulltext)
        if chunks:
            embeddings = modell.encode(
                chunks, batch_size=8, convert_to_numpy=True,
            ).tolist()
            db.spara_chunks(celex, chunks, embeddings)
    except Exception as exc:
        log.warning("Embedding misslyckades för %s: %s", celex, exc)


def _sok_riksdag_propositioner(sok_term: str, max_antal: int = 5) -> list[dict]:
    """Söker riksdagens öppna data efter propositioner som nämner söktermen.

    Filtrerar resultaten så att bara propositioner vars rubrik innehåller
    söktermen (direktivnumret, t.ex. "2006/54") inkluderas — förhindrar
    irrelevanta träffar där söktermen förekommer i brödtext men inte gäller
    just detta direktiv.
    """
    svar = requests.get(
        "https://data.riksdagen.se/dokumentlista/",
        params={"doktyp": "prop", "sok": sok_term, "format": "json",
                "utformat": "json", "a": "s", "p": 1},
        timeout=30,
    )
    svar.raise_for_status()
    data = svar.json()
    dok_lista = data.get("dokumentlista", {}).get("dokument", [])
    if isinstance(dok_lista, dict):
        dok_lista = [dok_lista]
    resultat = []
    for dok in dok_lista:
        rubrik = dok.get("titel", "")
        # Inkludera bara propositioner vars rubrik innehåller direktivnumret
        if sok_term not in rubrik:
            continue
        beteckning = dok.get("beteckning", "")
        resultat.append({
            "beteckning": beteckning,
            "rubrik": rubrik,
            "datum":  dok.get("datum", ""),
            "url": f"https://www.riksdagen.se/sv/dokument-lagar/dokument/{dok.get('typ','prop')}/{beteckning}",
            "kalla": "Riksdagen",
        })
        if len(resultat) >= max_antal:
            break
    return resultat


# ---------------------------------------------------------------------------
# MCP-servern
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "cellar-eu-ratt",
    instructions=(
        "MCP-server för EU-rätt via CELLAR. Verktyg: hamta_eu_akt (rättsakter via CELEX), "
        "hamta_eu_mal (domar C-xxx/T-xxx), sok_i_cachade_akter (FTS/semantisk sökning), "
        "sok_eu_metadata (SPARQL-sökning utan cache), "
        "hitta_nationellt_genomforande (nationellt genomförande per medlemsstat)."
    ),
)


@mcp.tool(
    name="hamta_eu_akt",
    description=(
        "Hämtar fulltext för en EU-rättsakt via CELEX-nummer. Cachar lokalt i databasen "
        "för framtida sökning. Giltiga CELEX-format: 32006L0054 (direktiv), "
        "32016R0679 (förordning), 32015D1602 (delegerat beslut). "
        "Returnerar titel, datum, ELI och fulltext. "
        "Använd parametern 'artikel' (heltal) för att hämta ett specifikt artikelnummer — "
        "viktigt för långa direktiv där hela texten trunkeras. "
        "Prova sprak='EN' om svensk version saknas (pre-1995 akter)."
    ),
)
def hamta_eu_akt(celex: str, sprak: str = "SV",
                 artikel: Optional[int] = None) -> dict:
    """Hämtar och cachar en EU-rättsakt.

    Args:
        celex:   CELEX-nummer, t.ex. '32006L0054'.
        sprak:   Önskat språk — 'SV' (standard), 'EN', 'DE', 'FR'.
        artikel: Om satt hämtas bara detta artikelnummer (t.ex. artikel=3).
                 Användbart för långa direktiv där hela texten trunkeras.
    """
    celex = celex.strip().upper()
    log.info("hamta_eu_akt: celex=%s sprak=%s artikel=%s", celex, sprak, artikel)

    # Kontrollera cache
    cachad = db.hamta_cachad_akt(celex)
    if cachad and cachad.get("fulltext_md"):
        log.info("Serverar %s från cache", celex)
        fulltext_full = cachad["fulltext_md"]
        if artikel is not None:
            # Försök extrahera specifikt artikelnummer ur cachad text.
            # ^-ankare + re.MULTILINE förhindrar recital-träffar.
            art_text = re.search(
                rf'^(Artikel\s+{artikel}\b.*?)(?=^Artikel\s+\d+\b|\Z)',
                fulltext_full, re.DOTALL | re.IGNORECASE | re.MULTILINE,
            )
            if art_text:
                return {
                    "celex":      cachad["celex"],
                    "sprak":      cachad["sprak"],
                    "titel":      cachad["titel"],
                    "datum":      cachad["datum"],
                    "eli":        cachad["eli"],
                    "fran_cache": True,
                    "artikel":    artikel,
                    "fulltext":   art_text.group(1).strip(),
                }
        return {
            "celex":      cachad["celex"],
            "sprak":      cachad["sprak"],
            "titel":      cachad["titel"],
            "datum":      cachad["datum"],
            "eli":        cachad["eli"],
            "fran_cache": True,
            "tecken":     len(fulltext_full),
            "fulltext":   _trunkera(fulltext_full),
        }

    # Hämta SPARQL-metadata
    meta = _hamta_sparql_metadata(celex) or {}

    # Hämta fulltext via CELLAR REST (WEMI-protokollet, format-fallback)
    anvant_sprak = sprak
    anvant_format: Optional[str] = None
    raw: Optional[str] = None

    for forsok_sprak in ([sprak, "EN"] if sprak != "EN" else [sprak]):
        try:
            raw, anvant_format = _hamta_cellar_text(celex, forsok_sprak)
            anvant_sprak = forsok_sprak
            break
        except ValueError:
            continue
        except requests.RequestException as exc:
            return {"fel": f"Nätverksfel vid hämtning av {celex!r}: {exc}"}

    if raw is None:
        return {
            "fel": (
                f"Dokumentet {celex!r} hittades inte i något tillgängligt format. "
                f"Provade: CELLAR ({', '.join(CELLAR_FORMAT_ORDNING)}) + EUR-Lex (TXT/HTML + LexUriServ). "
                "Kontrollera CELEX-numret."
            )
        }

    # Konvertera till klartext — full otrunkerad text för cachelagring
    fulltext_full = _rensa_html(raw) if anvant_format in ("xhtml", "html") else raw
    _indexera_akt(
        celex, anvant_sprak,
        fulltext_full,          # ← full text sparas i DB
        meta.get("titel"),
        meta.get("datum"),
        meta.get("eli"),
        meta.get("typ"),
    )

    # Artikel-extraktion om begärd
    if artikel is not None:
        # XHTML/HTML: strukturerad extraktion via id-attribut
        if anvant_format in ("xhtml", "html"):
            art_text = _extrahera_artikel(raw, artikel)
            if art_text:
                return {
                    "celex":      celex,
                    "sprak":      anvant_sprak,
                    "format":     anvant_format,
                    "titel":      meta.get("titel"),
                    "datum":      meta.get("datum"),
                    "eli":        meta.get("eli"),
                    "fran_cache": False,
                    "artikel":    artikel,
                    "fulltext":   art_text,
                }
        # Fallback för alla format: regex i klartext.
        # ^-ankare + re.MULTILINE förhindrar recital-träffar.
        art_match = re.search(
            rf'^(Artikel\s+{artikel}\b.*?)(?=^Artikel\s+\d+\b|\Z)',
            fulltext_full, re.DOTALL | re.IGNORECASE | re.MULTILINE,
        )
        if art_match:
            return {
                "celex":      celex,
                "sprak":      anvant_sprak,
                "format":     anvant_format,
                "titel":      meta.get("titel"),
                "datum":      meta.get("datum"),
                "eli":        meta.get("eli"),
                "fran_cache": False,
                "artikel":    artikel,
                "fulltext":   art_match.group(1).strip(),
            }
        return {
            "fel": (
                f"Artikel {artikel} hittades inte i {celex}. "
                "Kontrollera att artikelnumret stämmer."
            )
        }

    return {
        "celex":      celex,
        "sprak":      anvant_sprak,
        "format":     anvant_format,
        "titel":      meta.get("titel"),
        "datum":      meta.get("datum"),
        "eli":        meta.get("eli"),
        "fran_cache": False,
        "tecken":     len(fulltext_full),
        "fulltext":   _trunkera(fulltext_full),   # ← trunkeras bara vid visning
    }


@mcp.tool(
    name="hamta_eu_mal",
    description=(
        "Hämtar fulltext för ett EU-domstolsavgörande. "
        "Accepterar EU-domstolens målnummer (C-441/17, C-30/19 PPU) "
        "och Tribunalens målnummer (T-325/15). "
        "OBS: Tribunalens domar (T-xxx) saknar HTML via CELLAR REST — "
        "metadata returneras men fulltext kan saknas. "
        "Äldre mål saknar ofta svensk version — prova sprak='FR' eller 'EN'."
    ),
)
def hamta_eu_mal(malnum: str, sprak: str = "SV") -> dict:
    """Hämtar ett EU-domstolsavgörande.

    Args:
        malnum: Målnummer, t.ex. 'C-441/17', 'C-30/19 PPU', 'T-325/15',
                eller direkt CELEX-format som '62017CJ0441'.
        sprak:  Önskat språk — 'SV' (standard), 'EN', 'FR'.
    """
    malnum_rensat = malnum.strip()
    celex, domstol = _parsera_malnum(malnum_rensat)
    log.info("hamta_eu_mal: malnum=%s → celex=%s domstol=%s", malnum_rensat, celex, domstol)

    # Kontrollera cache
    cachad = db.hamta_cachad_akt(celex)
    if cachad and cachad.get("fulltext_md"):
        fulltext_full = cachad["fulltext_md"]
        return {
            "malnum":    malnum_rensat,
            "celex":     celex,
            "domstol":   domstol,
            "sprak":     cachad["sprak"],
            "titel":     cachad["titel"],
            "datum":     cachad["datum"],
            "fran_cache": True,
            "tecken":    len(fulltext_full),
            "fulltext":  _trunkera(fulltext_full),
        }

    # Tribunalen: REST HTML fungerar inte — returnera metadata
    if domstol in ("TJ", "TO"):
        meta = _hamta_sparql_metadata(celex) or {}
        return {
            "malnum":   malnum_rensat,
            "celex":    celex,
            "domstol":  domstol,
            "titel":    meta.get("titel"),
            "datum":    meta.get("datum"),
            "eli":      meta.get("eli"),
            "fulltext": None,
            "not": (
                "Tribunalens domar saknar HTML-fulltext via CELLAR REST API. "
                "Metadata ovan hämtat via SPARQL. "
                "Fulltext tillgänglig på EUR-Lex: "
                f"https://eur-lex.europa.eu/legal-content/SV/TXT/?uri=CELEX:{celex}"
            ),
        }

    meta = _hamta_sparql_metadata(celex) or {}
    raw: Optional[str] = None
    anvant_sprak = sprak
    anvant_format: Optional[str] = None

    # EU-domstolen: prova SV → FR → EN, med format-fallback per språk
    for forsok_sprak in ([sprak, "FR", "EN"] if sprak not in ("FR", "EN") else [sprak, "EN"]):
        try:
            raw, anvant_format = _hamta_cellar_text(celex, forsok_sprak)
            anvant_sprak = forsok_sprak
            break
        except ValueError:
            continue
        except requests.RequestException as exc:
            return {"fel": f"Nätverksfel: {exc}"}

    if raw is None:
        return {
            "fel": (
                f"Avgörande {malnum_rensat!r} (CELEX: {celex}) hittades inte i något "
                f"tillgängligt format. Provade: CELLAR "
                f"({', '.join(CELLAR_FORMAT_ORDNING)}) + EUR-Lex (TXT/HTML + LexUriServ). "
                "Kontrollera målnumret."
            )
        }

    fulltext_full = _rensa_html(raw) if anvant_format in ("xhtml", "html") else raw
    _indexera_akt(celex, anvant_sprak, fulltext_full,
                  meta.get("titel"), meta.get("datum"), meta.get("eli"), "dom")

    return {
        "malnum":     malnum_rensat,
        "celex":      celex,
        "domstol":    domstol,
        "sprak":      anvant_sprak,
        "format":     anvant_format,
        "titel":      meta.get("titel"),
        "datum":      meta.get("datum"),
        "fran_cache": False,
        "tecken":     len(fulltext_full),
        "fulltext":   _trunkera(fulltext_full),
    }


@mcp.tool(
    name="sok_i_cachade_akter",
    description=(
        "Söker i fulltext bland lokalt cachade EU-rättsakter. "
        "PostgreSQL: FTS och semantisk sökning (pgvector). SQLite: LIKE-sökning. "
        "Returnerar bara akter som tidigare hämtats via hamta_eu_akt eller hamta_eu_mal. "
        "Tillgängliga typer: direktiv, forordning, forordning_delegerad, "
        "forordning_genomforande, beslut, dom m.fl. "
        "Använd sok_eu_metadata för att hitta och ladda hem akter utan lokal cache."
    ),
)
def sok_i_cachade_akter(
    fraga: str,
    typ: Optional[str] = None,
    ar_fran: Optional[int] = None,
    ar_till: Optional[int] = None,
    max_antal: int = 10,
) -> dict:
    """Söker i cachade EU-rättsakter.

    Args:
        fraga:     Sökfras, t.ex. 'lönediskriminering' eller 'personuppgifter samtycke'.
        typ:       Valfritt typfilter (se lista i verktygets beskrivning).
        ar_fran:   Lägsta år (inklusivt).
        ar_till:   Högsta år (inklusivt).
        max_antal: Max antal träffar (standard 10, max 20).
    """
    max_antal = min(int(max_antal), 20)

    if typ and typ.lower() not in TYP_URIS:
        return {
            "fel": f"Okänd typ: {typ!r}",
            "tillgangliga_typer": list(TYP_BESKRIVING.keys()),
        }

    typ_kod = None
    if typ:
        typ_kod = TYP_URIS[typ.lower()].split("/")[-1]

    # FTS-sökning
    fts_treffar = db.sok_fts(fraga, typ_kod, ar_fran, ar_till, max_antal)

    # Semantisk sökning — kräver PostgreSQL med pgvector (ej SQLite)
    semantiska_treffar: list[dict] = []
    if db.DATABASE_URL and db._ar_postgres():
        try:
            modell = _hamta_modell()
            vektor = modell.encode(fraga).tolist()
            semantiska_treffar = db.sok_semantisk(vektor, typ_kod, ar_fran, ar_till, max_antal)
        except Exception as exc:
            log.warning("Semantisk sökning misslyckades: %s", exc)

    # Slå ihop och deduplicera — FTS-träffar prioriteras
    sett = set()
    samlade = []
    for t in fts_treffar + semantiska_treffar:
        if t["celex"] not in sett:
            sett.add(t["celex"])
            samlade.append(t)

    return {
        "fraga":   fraga,
        "antal":   len(samlade),
        "treffar": samlade[:max_antal],
    }


@mcp.tool(
    name="sok_eu_metadata",
    description=(
        "Söker EU-rättsakter via SPARQL mot CELLAR — hittar akter utan att cacha dem lokalt. "
        "Returnerar lista med CELEX-nummer, titlar och datum att använda med hamta_eu_akt. "
        "sokterm: fritext mot titlar. Skicka kommaseparerade termer för OR-logik, t.ex. "
        "'dataskydd,data protection,protection des données'. Utan kommatecken behandlas "
        "hela strängen som en enda fras — undvik mellanslag inom fraser som 'rule of law'. "
        "Pre-1995 akter saknar ofta svenska titlar — ange sprak='EN' i sådana fall. "
        "Tillgängliga typer: direktiv, forordning, forordning_delegerad, "
        "forordning_genomforande, direktiv_delegerat, beslut, beslut_genomforande, "
        "beslut_delegerat, rekommendation, yttrande, dom, beslut_domstol, "
        "yttrande_generaladvokat, forslag_direktiv, forslag_forordning, forslag_beslut, "
        "kommunike, gronbok, vitbok, konsoliderad, nationellt_genomforande."
    ),
)
def sok_eu_metadata(
    sokterm: Optional[str] = None,
    typ: Optional[str] = None,
    ar_fran: Optional[int] = None,
    ar_till: Optional[int] = None,
    sprak: str = "SV",
    max_antal: int = 20,
) -> dict:
    """Söker EU-rättsakter via SPARQL (metadatasökning, ingen cachning).

    Args:
        sokterm:   Fritext mot titlar. Kommaseparerade termer ger OR-logik.
                   Utan kommatecken = exakt fras. Lämna tom för bläddring.
        typ:       Dokumenttyp (se lista ovan). Utelämnas för alla typer.
        ar_fran:   Lägsta publiceringsår.
        ar_till:   Högsta publiceringsår.
        sprak:     Titelspråk — 'SV' (standard), 'EN', 'FR'.
        max_antal: Max antal träffar (standard 20, max 50).
    """
    max_antal = min(int(max_antal), 50)

    if typ and typ.lower() not in TYP_URIS:
        return {
            "fel": f"Okänd typ: {typ!r}",
            "tillgangliga_typer": list(TYP_BESKRIVING.items()),
        }

    # Query-expansion: flerspråkiga ekvivalenter via valfritt LLM-anrop
    extra_termer = expandera_fraga(sokterm) if sokterm else []

    # Bygg lista med alla söktermer (OR-logik)
    if sokterm:
        if "," in sokterm:
            sokterm_delar = [t.strip() for t in sokterm.split(",") if t.strip()]
        else:
            sokterm_delar = [sokterm.strip()]
    else:
        sokterm_delar = []
    alla_termer = sokterm_delar + extra_termer

    sprak_uri = SPRAK_URIS.get(sprak.upper(), SPRAK_URIS["SV"])
    typ_filter = ""
    if typ:
        typ_filter = f'?work cdm:work_has_resource-type <{TYP_URIS[typ.lower()]}> .'

    ar_delar = []
    if ar_fran:
        ar_delar.append(f"YEAR(?datum) >= {ar_fran}")
    if ar_till:
        ar_delar.append(f"YEAR(?datum) <= {ar_till}")
    ar_filter = f"  FILTER({' && '.join(ar_delar)})" if ar_delar else ""

    # Bygg FILTER-villkor för titelsökning (OR per term)
    if alla_termer:
        or_villkor = " || ".join(
            f'CONTAINS(LCASE(STR(?titel)), LCASE("{_sparql_escape(t)}"))'
            for t in alla_termer
        )
        titel_filter = f"  FILTER({or_villkor})"
    else:
        titel_filter = ""

    sparql_query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>

SELECT DISTINCT ?celex ?titel ?datum ?eli
WHERE {{
  ?work cdm:resource_legal_id_celex ?celex ;
        cdm:work_date_document ?datum .
  {typ_filter}

  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language <{sprak_uri}> ;
        cdm:expression_title ?titel .

  OPTIONAL {{ ?work cdm:resource_legal_eli ?eli . }}
{ar_filter}
{titel_filter}
}}
ORDER BY DESC(?datum)
LIMIT {max_antal}"""

    try:
        rader = _kora_sparql(sparql_query)
    except requests.RequestException as exc:
        return {"fel": f"SPARQL-anrop misslyckades: {exc}"}

    return {
        "antal":       len(rader),
        "sokterm":     sokterm,
        "typ":         typ,
        "ar_fran":     ar_fran,
        "ar_till":     ar_till,
        "expansion":   extra_termer if extra_termer else None,
        "treffar": [
            {"celex": r["celex"], "titel": r["titel"],
             "datum": r["datum"], "eli": r.get("eli")}
            for r in rader
        ],
        "tips": (
            "Använd hamta_eu_akt(celex) för att hämta fulltext "
            "och indexera akten för framtida sökning."
        ),
    }


@mcp.tool(
    name="hitta_nationellt_genomforande",
    description=(
        "Hittar nationella genomförandeåtgärder för ett EU-direktiv. "
        "Söker i CELLAR (typ MEAS_NATION_IMPL) för alla officiellt anmälda genomförandelagar. "
        "Komplement för Sverige: söker också i riksdagens öppna data efter propositioner. "
        "Ange CELEX-nummer för direktivet, t.ex. '32006L0054' (jämlikhetsdirektivet). "
        "Lämna medlemsstat tom för alla 27 EU-länder, ange ISO-kod (t.ex. 'SWE', 'DEU') "
        "för ett specifikt land."
    ),
)
def hitta_nationellt_genomforande(
    celex: str,
    medlemsstat: Optional[str] = None,
) -> dict:
    """Hittar nationella genomförandeåtgärder för ett EU-direktiv.

    Args:
        celex:      CELEX-nummer för direktivet, t.ex. '32006L0054'.
        medlemsstat: ISO-3-kod, t.ex. 'SWE', 'DEU', 'FRA'. Tom = alla länder.
    """
    celex = celex.strip().upper()
    # ISO-normalisering tillämpas av anroparen (Bg3) — celex är redan rensat.
    stat_filter = ""
    if medlemsstat:
        stat_kod = _NORMALISERA_LAND.get(
            medlemsstat.strip().upper(), medlemsstat.strip().upper()
        )
        stat_uri = STAT_URIS.get(stat_kod)
        # Korrekta MEAS_NATION_IMPL-predikat verifierade mot live CELLAR.
        if stat_uri:
            stat_filter = (
                f"  ?genomf cdm:measure_national_implementing_implemented_by_country"
                f" <{stat_uri}> ."
            )
        else:
            stat_filter = (
                f"  ?genomf cdm:measure_national_implementing_implemented_by_country"
                f" ?stat .\n"
                f'  FILTER(STR(?stat) = "http://publications.europa.eu/resource/'
                f'authority/country/{_sparql_escape(stat_kod)}")'
            )

    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>

SELECT DISTINCT ?genomf_celex ?stat ?titel ?datum
WHERE {{
  ?direktiv cdm:resource_legal_id_celex ?dir_celex .
  FILTER(STR(?dir_celex) = "{_sparql_escape(celex)}")

  ?genomf cdm:measure_national_implementing_implements_resource_legal ?direktiv ;
          cdm:resource_legal_id_celex ?genomf_celex ;
          cdm:work_date_document ?datum .
{stat_filter}
  OPTIONAL {{ ?genomf cdm:measure_national_implementing_implemented_by_country ?stat . }}
  OPTIONAL {{
    ?genomf_expr cdm:expression_belongs_to_work ?genomf ;
                 cdm:expression_title ?titel .
  }}
}}
ORDER BY ?stat ?datum
LIMIT 100"""

    cellar_treffar: list[dict] = []
    try:
        rader = _kora_sparql(query)
        for r in rader:
            if r.get("genomf_celex"):
                stat_kod_resultat = (r.get("stat") or "").split("/")[-1]
                cellar_treffar.append({
                    "celex":      r["genomf_celex"],
                    "datum":      r.get("datum"),
                    "titel":      r.get("titel"),
                    "medlemsstat": stat_kod_resultat,
                    "kalla":      "CELLAR",
                })
    except requests.RequestException as exc:
        log.warning("SPARQL misslyckades för genomförande av %s: %s", celex, exc)

    # Riksdag-sökning som komplement för Sverige.
    # Normalisera eventuell 2-bokstavs-kod så att "SE" också matchar.
    riksdag_treffar: list[dict] = []
    normaliserad_stat = (
        _NORMALISERA_LAND.get(medlemsstat.strip().upper(), medlemsstat.strip().upper())
        if medlemsstat else None
    )
    ska_soka_riksdag = not normaliserad_stat or normaliserad_stat == "SWE"
    if ska_soka_riksdag:
        eu_nummer = _parsera_celex_till_eu_nummer(celex)
        if eu_nummer:
            try:
                riksdag_treffar = _sok_riksdag_propositioner(eu_nummer)
            except requests.RequestException as exc:
                log.warning("Riksdag-sökning misslyckades: %s", exc)

    return {
        "celex":            celex,
        "eu_nummer":        _parsera_celex_till_eu_nummer(celex),
        "filtrar_pa_stat":  medlemsstat,
        "cellar_genomforanden": cellar_treffar,
        "riksdag_propositioner": riksdag_treffar,
        "not": (
            "CELLAR listar formellt anmälda genomförandeåtgärder (MEAS_NATION_IMPL). "
            "Riksdag-resultaten inkluderar propositioner som nämner direktivnumret "
            "och kan täcka fall som inte anmälts till CELLAR."
        ),
    }


# ---------------------------------------------------------------------------
# Startpunkt och transport
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _konfigurera_logging()
    db.initiera_schema()

    if MCP_TRANSPORT == "http":
        import uvicorn
        from starlette.applications import Starlette
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import Response

        class BearerTokenMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if MCP_API_KEY:
                    auth = request.headers.get("Authorization", "")
                    if auth != f"Bearer {MCP_API_KEY}":
                        return Response("Obehörig", status_code=401)
                return await call_next(request)

        # Förladda embeddingmodell i HTTP-läge
        _hamta_modell()

        # OBS: db.py använder per-anrops-anslutningar som är korrekta för
        # stdio-transport. Vid HTTP-deployment med flera samtidiga klienter
        # bör psycopg2.pool.ThreadedConnectionPool läggas till i db.py
        # för att undvika att varje anrop öppnar en ny PG-anslutning.
        app = Starlette()
        app.add_middleware(BearerTokenMiddleware)
        app.mount("/", mcp.get_asgi_app())

        log.info("Startar HTTP-server på %s:%s", MCP_HOST, MCP_PORT)
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
    else:
        log.info("Startar i stdio-läge")
        mcp.run()
