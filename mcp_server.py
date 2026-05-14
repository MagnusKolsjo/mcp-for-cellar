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

# VIKTIGT: load_dotenv() MÅSTE köras FÖRE "import db"
# db.py läser DATABASE_URL på modulnivå — kör den innan environ är populerat
# och db.DATABASE_URL blir alltid "" (tom sträng), cachen används aldrig.
load_dotenv()

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
# Konstanter: verifierade typ-URI:er (session 19)
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


_LANG_MAP_2TO3 = {
    "SV": "SWE", "EN": "ENG", "DE": "DEU", "FR": "FRA",
    "DA": "DAN", "FI": "FIN", "NL": "NLD", "PL": "POL",
    "ES": "SPA", "IT": "ITA", "PT": "POR", "CS": "CES",
    "HU": "HUN", "RO": "RON", "SK": "SLK", "SL": "SLV",
}


def _normalisera_sprak(sprak: str) -> str:
    """Normaliserar språkkod till 3-bokstavs ISO 639-2/T (SWE, ENG, ...)."""
    lang = sprak.strip().upper()
    if len(lang) == 2:
        lang = _LANG_MAP_2TO3.get(lang, lang)
    return lang


def _hamta_xhtml_fran_manifestation(manif_iri: str) -> str:
    """Hämtar XHTML-innehåll från en känd CELLAR-manifestations-IRI.

    Listar DOC-items via RDF och hämtar dem i ordning.
    """
    rdf_url = f"{manif_iri}/rdf/object/full"
    try:
        rdf_text = requests.get(rdf_url, timeout=REST_TIMEOUT).text
        doc_urls = list(dict.fromkeys(re.findall(
            r'rdf:resource="(' + re.escape(manif_iri) + r'/DOC_\d+)"',
            rdf_text,
        )))
    except Exception:
        doc_urls = []

    if not doc_urls:
        doc_urls = [f"{manif_iri}/DOC_1"]

    texter: list[str] = []
    for doc_url in doc_urls:
        log.info("Hämtar %s", doc_url)
        r = requests.get(doc_url, timeout=REST_TIMEOUT)
        if r.status_code == 200 and r.text.strip():
            texter.append(r.text)
        elif r.status_code == 404:
            break

    return "\n".join(texter)


def _hitta_xhtml_manifestation_via_sparql(celex: str, lang3: str) -> Optional[str]:
    """Hittar XHTML-manifestations-IRI via SPARQL (fallback när REST 404:ar).

    Verifierat 2026-05-14: .{CELEX}.{LANG}.xhtml REST-URL ger 404 för
    nyare/äldre dokument (t.ex. AI-förordningen 32024R1689, GDPR 32016R0679)
    trots att xhtml-manifestation finns i CELLAR. SPARQL hittar rätt IRI.
    Manifestationerna indexeras som .0024.NN — siffran varierar per dokument.
    """
    lang_uri = f"http://publications.europa.eu/resource/authority/language/{lang3}"
    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?manif WHERE {{
  ?work cdm:resource_legal_id_celex ?c .
  FILTER(STR(?c) = "{celex}")
  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language <{lang_uri}> .
  ?manif cdm:manifestation_manifests_expression ?expr ;
         cdm:manifestation_type ?type .
  FILTER(CONTAINS(LCASE(STR(?type)), "xhtml"))
}} LIMIT 1"""
    try:
        rader = _kora_sparql(query)
        if rader:
            # _kora_sparql extraherar redan .value — rader[0]["manif"] är en sträng
            return rader[0]["manif"]
    except Exception as exc:
        log.warning("SPARQL-manifestationssökning misslyckades för %s/%s: %s",
                    celex, lang3, exc)
    return None


def _hamta_xhtml(celex: str, sprak: str) -> str:
    """Hämtar XHTML-fulltext för en EU-rättsakt via CELLAR.

    Protokoll (verifierat 2026-05-14):

    Primär väg — REST {CELEX}.{LANG}.xhtml → HTTP 303 → manifestation UUID:
      Fungerar för en delmängd dokument (t.ex. NIS2 32022L2555).

    Fallback — SPARQL manifestationssökning:
      För nyare/äldre dokument (t.ex. AI-akten 32024R1689, GDPR 32016R0679)
      ger REST-URL:en 404 trots att xhtml-manifestation finns. SPARQL hittar
      rätt manifestations-IRI direkt (cdm:manifestation_type = "xhtml").

    Språkfallback-kedja: begärt språk → ENG → FRA → SWE.
    """
    lang = _normalisera_sprak(sprak)

    # Bygg fallback-lista: begärt språk + ENG + FRA + SWE (om inte redan med)
    sprak_kedja = [lang]
    for fb in ("ENG", "FRA", "SWE"):
        if fb not in sprak_kedja:
            sprak_kedja.append(fb)

    for forsok_lang in sprak_kedja:
        # --- Primär väg: REST .xhtml ---
        manifest_url = f"{CELLAR_REST_BASE}/{celex}.{forsok_lang}.xhtml"
        log.info("Hämtar CELLAR manifestation (REST): %s", manifest_url)
        r1 = requests.get(manifest_url, timeout=REST_TIMEOUT, allow_redirects=False)

        if r1.status_code == 303:
            location = r1.headers.get("Location", "")
            if "/rdf/object/full" in location:
                manif_bas = location.replace("/rdf/object/full", "")
                texter = _hamta_xhtml_fran_manifestation(manif_bas)
                if texter:
                    log.info("Hämtade %s via REST (lang=%s)", celex, forsok_lang)
                    return texter

        # --- Fallback: SPARQL-baserad manifestationssökning ---
        log.info("REST 404/tom för %s.%s — provar SPARQL-manifestation",
                 celex, forsok_lang)
        manif_iri = _hitta_xhtml_manifestation_via_sparql(celex, forsok_lang)
        if manif_iri:
            texter = _hamta_xhtml_fran_manifestation(manif_iri)
            if texter:
                log.info("Hämtade %s via SPARQL-manifestation (lang=%s)",
                         celex, forsok_lang)
                return texter

    raise ValueError(
        f"Ingen XHTML-manifestation hittades för {celex} på {sprak} "
        f"(provade: {', '.join(sprak_kedja)}). "
        "Kontrollera CELEX-numret eller att akten har XHTML i CELLAR."
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
        + f"\n\n[VARNING: Texten är trunkerad vid {max_tecken:,} tecken. "
          f"Originaldokumentet är {len(text):,} tecken — innehållet är ofullständigt. "
          f"Anropa hamta_eu_akt igen med artikel=N för att hämta ett specifikt "
          f"artikelnummer ur hela dokumentet. Exempel: hamta_eu_akt(celex=..., artikel=4). "
          f"Ange fraga med användarens fråga för automatisk artikeldetektering.]"
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

    # Fallback: sök i ren text efter "Artikel N" i début av rad
    # OBS: re.MULTILINE + ^ krävs för att inte matcha "artikel N i fördraget"
    # som förekommer mitt i meningar i skälen.
    ren_text = _rensa_html(html)
    monster = re.compile(
        rf'^(Artikel\s+{artikel_nr}\b.*?)(?=^Artikel\s+\d+\b|\Z)',
        re.DOTALL | re.MULTILINE,
    )
    m = monster.search(ren_text)
    if m:
        utdrag = m.group(1).strip()
        if len(utdrag) > 20:
            return utdrag

    return None


def _chunka_text(text: str, max_ord: int = 400) -> list[str]:
    """Delar upp text i semantiska chunks om max max_ord ord."""
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
    """Hämtar titel, datum och ELI för ett CELEX via SPARQL.

    Titeln hämtas med språkfallback SV → EN → FR → (ingen titel).
    OBS: variabeln måste heta ?typ_uri i BÅDE SELECT och WHERE — annars
    binder WHERE ?typ_uri men SELECT exponerar den ej och r.get("typ_uri")
    returnerar alltid None → typ sparas som '' i DB (bugg 2026-05-14).
    """
    # Hämta grundmetadata + alla tillgängliga titlar
    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?datum ?eli ?typ_uri ?sprak ?titel
WHERE {{
  ?work cdm:resource_legal_id_celex ?celex_val ;
        cdm:work_date_document ?datum ;
        cdm:work_has_resource-type ?typ_uri .
  FILTER(STR(?celex_val) = "{celex}")
  OPTIONAL {{ ?work cdm:resource_legal_eli ?eli . }}
  OPTIONAL {{
    ?expr cdm:expression_belongs_to_work ?work ;
          cdm:expression_uses_language ?sprak ;
          cdm:expression_title ?titel .
    FILTER(?sprak IN (
      <{SPRAK_URIS["SV"]}>,
      <{SPRAK_URIS["EN"]}>,
      <http://publications.europa.eu/resource/authority/language/FRA>
    ))
  }}
}}
ORDER BY (IF(?sprak = <{SPRAK_URIS["SV"]}>, 0,
             IF(?sprak = <{SPRAK_URIS["EN"]}>, 1, 2)))
LIMIT 5"""
    try:
        rader = _kora_sparql(query)
        if rader:
            r = rader[0]
            typ_kod = (r.get("typ_uri") or "").split("/")[-1]
            # Välj bästa tillgängliga titel (första raden är prioriterad)
            titel = r.get("titel")
            return {
                "titel": titel,
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
            embeddings = [modell.encode(c).tolist() for c in chunks]
            db.spara_chunks(celex, chunks, embeddings)
    except Exception as exc:
        log.warning("Embedding misslyckades för %s: %s", celex, exc)


def _sok_riksdag_propositioner(sok_term: str, max_antal: int = 10) -> list[dict]:
    """Söker riksdagens öppna data efter propositioner som nämner söktermen."""
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
    for dok in dok_lista[:max_antal]:
        beteckning = dok.get("beteckning", "")
        resultat.append({
            "beteckning": beteckning,
            "rubrik": dok.get("titel", ""),
            "datum":  dok.get("datum", ""),
            "url": f"https://www.riksdagen.se/sv/dokument-lagar/dokument/{dok.get('typ','prop')}/{beteckning}",
            "kalla": "Riksdagen",
        })
    return resultat


# ---------------------------------------------------------------------------
# MCP-servern
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "cellar-eu-v2",
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
        "Hämtar fulltext för en EU-rättsakt via CELEX-nummer. Cachar lokalt i PostgreSQL "
        "för framtida sökning. Giltiga CELEX-format: 32006L0054 (direktiv), "
        "32016R0679 (förordning), 32015D1602 (delegerat beslut). "
        "Returnerar titel, datum, ELI och fulltext. "
        "ARTIKELEXTRAKTION: Frågar användaren om ett specifikt artikelnummer — "
        "t.ex. 'hur lyder artikel 4', 'vad säger artikel 17 om' — "
        "ANGE ALLTID artikel=N (t.ex. artikel=4) DIREKT i anropet. "
        "Stora direktiv (NIS2, GDPR m.fl.) är hundratusentals tecken och trunkeras "
        "om artikel-parametern utelämnas. Ange också fraga med användarens originalfråga "
        "så att artikelnumret kan detekteras automatiskt som fallback. "
        "Prova sprak='EN' om svensk version saknas (pre-1995 akter)."
    ),
)
def hamta_eu_akt(celex: str, sprak: str = "SV",
                 artikel: Optional[int] = None,
                 fraga: Optional[str] = None) -> dict:
    """Hämtar och cachar en EU-rättsakt.

    Args:
        celex:   CELEX-nummer, t.ex. '32006L0054'.
        sprak:   Önskat språk — 'SV' (standard), 'EN', 'DE', 'FR'.
        artikel: Om satt hämtas bara detta artikelnummer (t.ex. artikel=3).
                 Användbart för långa direktiv där hela texten trunkeras.
        fraga:   Användarens originalfråga (fritext). Används för att auto-detektera
                 artikelnummer om artikel-parametern utelämnats.
    """
    celex = celex.strip().upper()

    # Auto-detektera artikelnummer ur fritext om artikel inte angetts
    if artikel is None and fraga:
        _art_m = re.search(r'\bartikel\s+(\d+)\b', fraga, re.IGNORECASE)
        if _art_m:
            artikel = int(_art_m.group(1))
            log.info("Auto-detekterade artikel %d ur fråga: %r", artikel, fraga)

    log.info("hamta_eu_akt: celex=%s sprak=%s artikel=%s", celex, sprak, artikel)

    # Kontrollera cache
    cachad = db.hamta_cachad_akt(celex)
    if cachad and cachad.get("fulltext_md"):
        log.info("Serverar %s från cache", celex)
        fulltext_full = cachad["fulltext_md"]
        if artikel is not None:
            # Försök extrahera specifikt artikelnummer ur cachad text.
            # re.MULTILINE + ^ krävs — annars matchar frasen "artikel N i fördraget"
            # som förekommer mitt i meningar i ingressen/skälen.
            art_text = re.search(
                rf'^(Artikel\s+{artikel}\b.*?)(?=^Artikel\s+\d+\b|\Z)',
                fulltext_full, re.DOTALL | re.MULTILINE,
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

    # Hämta fulltext via CELLAR REST (WEMI-protokollet)
    anvant_sprak = sprak
    html = None
    for forsok_sprak in [sprak, "EN"] if sprak != "EN" else [sprak]:
        try:
            html = _hamta_xhtml(celex, forsok_sprak)
            anvant_sprak = forsok_sprak
            break
        except requests.HTTPError as exc:
            kod = exc.response.status_code if exc.response is not None else 0
            if kod in (400, 404, 406):
                continue
            return {"fel": f"HTTP {kod} vid hämtning av {celex!r}: {exc}"}
        except requests.RequestException as exc:
            return {"fel": f"Nätverksfel vid hämtning av {celex!r}: {exc}"}

    if html is None:
        return {
            "fel": (
                f"Dokumentet {celex!r} hittades inte i CELLAR. "
                "Kontrollera CELEX-numret eller prova sprak='EN'."
            )
        }

    # Rensa HTML — full otrunkerad text för cachelagring
    fulltext_full = _rensa_html(html)
    _indexera_akt(
        celex, anvant_sprak,
        fulltext_full,          # ← full text sparas i DB
        meta.get("titel"),
        meta.get("datum"),
        meta.get("eli"),
        meta.get("typ"),
    )

    # Artikel-extraktion direkt ur XHTML om begärd
    if artikel is not None:
        art_text = _extrahera_artikel(html, artikel)
        if art_text:
            return {
                "celex":      celex,
                "sprak":      anvant_sprak,
                "titel":      meta.get("titel"),
                "datum":      meta.get("datum"),
                "eli":        meta.get("eli"),
                "fran_cache": False,
                "artikel":    artikel,
                "fulltext":   art_text,
            }
        # Fallback: sök i ren text — re.MULTILINE + ^ för att undvika skälen
        art_match = re.search(
            rf'^(Artikel\s+{artikel}\b.*?)(?=^Artikel\s+\d+\b|\Z)',
            fulltext_full, re.DOTALL | re.MULTILINE,
        )
        if art_match:
            return {
                "celex":      celex,
                "sprak":      anvant_sprak,
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
    html = None
    anvant_sprak = sprak

    # EU-domstolen: prova SV → FR → EN
    for forsok_sprak in ([sprak, "FR", "EN"] if sprak not in ("FR", "EN") else [sprak, "EN"]):
        if forsok_sprak == anvant_sprak or html is not None:
            pass
        try:
            html = _hamta_xhtml(celex, forsok_sprak)
            anvant_sprak = forsok_sprak
            break
        except requests.HTTPError as exc:
            kod = exc.response.status_code if exc.response is not None else 0
            if kod in (400, 404, 406):
                continue
            return {"fel": f"HTTP {kod} vid hämtning av {malnum_rensat!r}: {exc}"}
        except requests.RequestException as exc:
            return {"fel": f"Nätverksfel: {exc}"}

    if html is None:
        return {
            "fel": (
                f"Avgörande {malnum_rensat!r} (CELEX: {celex}) hittades inte. "
                "Kontrollera målnumret."
            )
        }

    fulltext_full = _rensa_html(html)
    _indexera_akt(celex, anvant_sprak, fulltext_full,
                  meta.get("titel"), meta.get("datum"), meta.get("eli"), "dom")

    return {
        "malnum":     malnum_rensat,
        "celex":      celex,
        "domstol":    domstol,
        "sprak":      anvant_sprak,
        "titel":      meta.get("titel"),
        "datum":      meta.get("datum"),
        "fran_cache": False,
        "tecken":     len(fulltext_full),
        "fulltext":   _trunkera(fulltext_full),
    }


@mcp.tool(
    name="sok_i_cachade_akter",
    description=(
        "Söker i fulltext bland lokalt cachade EU-rättsakter med PostgreSQL FTS "
        "och semantisk sökning (pgvector). "
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

    # Semantisk sökning om modell är tillgänglig och databas ansluten
    semantiska_treffar: list[dict] = []
    if db.DATABASE_URL:
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
        "VIKTIGT: Ange sokterm för att söka på nyckelord i titeln (t.ex. sokterm='artificiell intelligens'). "
        "Utan sokterm returneras de senaste max_antal akterna av vald typ — nyttigt som listning men "
        "ej som fritextsökning. Titlar hämtas med språkfallback SV→EN→FR automatiskt. "
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
        sokterm:   Nyckelord att söka i titeln, t.ex. 'artificiell intelligens'
                   eller 'halvledare'. Lämna tomt för att lista senaste akter.
        typ:       Dokumenttyp (se lista ovan). Utelämnas för alla typer.
        ar_fran:   Lägsta publiceringsår.
        ar_till:   Högsta publiceringsår.
        sprak:     Föredraget titelspråk — 'SV' (standard), 'EN', 'FR'.
                   Fallback till EN och FR om SV saknas.
        max_antal: Max antal träffar (standard 20, max 100).
    """
    max_antal = min(int(max_antal), 100)

    if typ and typ.lower() not in TYP_URIS:
        return {
            "fel": f"Okänd typ: {typ!r}",
            "tillgangliga_typer": list(TYP_BESKRIVING.items()),
        }

    sprak_uri = SPRAK_URIS.get(sprak.upper(), SPRAK_URIS["SV"])
    eng_uri   = SPRAK_URIS["EN"]

    typ_filter = ""
    if typ:
        typ_filter = f'?work cdm:work_has_resource-type <{TYP_URIS[typ.lower()]}> .'

    ar_delar = []
    if ar_fran:
        ar_delar.append(f"YEAR(?datum) >= {ar_fran}")
    if ar_till:
        ar_delar.append(f"YEAR(?datum) <= {ar_till}")
    ar_filter = f"  FILTER({' && '.join(ar_delar)})" if ar_delar else ""

    def _bygg_query(lang_uri: str, term: Optional[str], limit: int) -> str:
        """Enkel SPARQL-sökning i ett språk (undviker timeout från 3-OPTIONAL-join)."""
        term_filter = ""
        if term:
            escaped = term.replace('"', '\\"')
            term_filter = f'  FILTER(CONTAINS(LCASE(?titel), LCASE("{escaped}")))'
        return f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex ?titel ?datum ?eli
WHERE {{
  ?work cdm:resource_legal_id_celex ?celex ;
        cdm:work_date_document ?datum .
  {typ_filter}
  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language <{lang_uri}> ;
        cdm:expression_title ?titel .
  OPTIONAL {{ ?work cdm:resource_legal_eli ?eli . }}
{ar_filter}
{term_filter}
}}
ORDER BY DESC(?datum)
LIMIT {limit}"""

    try:
        # Sök i primärspråk
        rader = _kora_sparql(_bygg_query(sprak_uri, sokterm, max_antal))

        # Om sokterm angavs och primärspråket inte är EN — komplettera med EN-sökning
        # (OBS: CELLAR sparar ofta titlar bara på EN för äldre/icke-publicerade akter)
        if sokterm and sprak_uri != eng_uri:
            sett = {r["celex"] for r in rader}
            komplettering = _kora_sparql(
                _bygg_query(eng_uri, sokterm, max_antal - len(rader))
            )
            for r in komplettering:
                if r["celex"] not in sett:
                    rader.append(r)
                    sett.add(r["celex"])

        # Sortera sammanslagen lista på datum (DESC)
        rader.sort(key=lambda r: r.get("datum", ""), reverse=True)

    except requests.RequestException as exc:
        return {"fel": f"SPARQL-anrop misslyckades: {exc}"}

    return {
        "antal":   len(rader),
        "sokterm": sokterm,
        "typ":     typ,
        "ar_fran": ar_fran,
        "ar_till": ar_till,
        "treffar": [
            {"celex": r["celex"], "titel": r.get("titel"),
             "datum": r["datum"], "eli": r.get("eli")}
            for r in rader[:max_antal]
        ],
        "tips": (
            "Utan sokterm returneras de senaste akterna av vald typ. "
            "Med sokterm söks i titlar på begärt språk + engelska. "
            "Använd hamta_eu_akt(celex) för att hämta fulltext och indexera."
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

    # Bygg landfilter — korrekt predicat: measure_national_implementing_implemented_by_country
    # Lands-URI-format: http://publications.europa.eu/resource/authority/country/{ISO3}
    stat_filter = ""
    if medlemsstat:
        stat_kod = medlemsstat.strip().upper()
        stat_uri = (
            STAT_URIS.get(stat_kod)
            or f"http://publications.europa.eu/resource/authority/country/{stat_kod}"
        )
        stat_filter = (
            f"  ?genomf cdm:measure_national_implementing_implemented_by_country "
            f"<{stat_uri}> ."
        )

    # OBS: Korrekta CDM-predicat för genomförandeåtgärder (verifierade 2026-05-14):
    #   - measure_national_implementing_implements_resource_legal  (länk direktiv → genomförande)
    #   - measure_national_implementing_implemented_by_country     (land)
    #   - measure_national_implementing_date_notification          (notifieringsdatum)
    #   - measure_national_implementing_date_official_journal      (OJ-datum)
    #   - measure_national_implementing_number_official_journal    (OJ-nummer/SFS-nummer)
    #   - measure_national_implementing_name_official_journal      (OJ-namn, t.ex. "Retsinformation")
    # Landet extraheras ur ?stat_uri, INTE ur cdm:member_state_of_publication.
    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>

SELECT DISTINCT ?genomf_celex ?stat_uri ?titel ?datum_not ?datum_oj ?oj_nummer ?oj_namn
WHERE {{
  ?direktiv cdm:resource_legal_id_celex ?dir_celex .
  FILTER(STR(?dir_celex) = "{celex}")

  ?genomf cdm:measure_national_implementing_implements_resource_legal ?direktiv ;
          cdm:resource_legal_id_celex ?genomf_celex .
{stat_filter}
  OPTIONAL {{ ?genomf cdm:measure_national_implementing_implemented_by_country ?stat_uri . }}
  OPTIONAL {{ ?genomf cdm:measure_national_implementing_date_notification ?datum_not . }}
  OPTIONAL {{ ?genomf cdm:measure_national_implementing_date_official_journal ?datum_oj . }}
  OPTIONAL {{ ?genomf cdm:measure_national_implementing_number_official_journal ?oj_nummer . }}
  OPTIONAL {{ ?genomf cdm:measure_national_implementing_name_official_journal ?oj_namn . }}
  OPTIONAL {{
    ?genomf_expr cdm:expression_belongs_to_work ?genomf ;
                 cdm:expression_title ?titel .
  }}
}}
ORDER BY ?stat_uri ?datum_not
LIMIT 200"""

    cellar_treffar: list[dict] = []
    try:
        rader = _kora_sparql(query)
        for r in rader:
            if r.get("genomf_celex"):
                stat_kod_resultat = (r.get("stat_uri") or "").split("/")[-1]
                # Filtrera bort ogiltiga OJ-datum (1001-01-01 = saknas)
                datum_oj = r.get("datum_oj")
                if datum_oj and datum_oj.startswith("1001"):
                    datum_oj = None
                cellar_treffar.append({
                    "celex":            r["genomf_celex"],
                    "datum_notifierat": r.get("datum_not"),
                    "datum_oj":         datum_oj,
                    "oj_nummer":        r.get("oj_nummer"),
                    "oj_namn":          r.get("oj_namn"),
                    "titel":            r.get("titel"),
                    "medlemsstat":      stat_kod_resultat,
                    "kalla":            "CELLAR",
                })
    except requests.RequestException as exc:
        log.warning("SPARQL misslyckades för genomförande av %s: %s", celex, exc)

    # Riksdag-sökning som komplement för Sverige
    riksdag_treffar: list[dict] = []
    ska_soka_riksdag = (
        not medlemsstat
        or medlemsstat.strip().upper() in ("SWE", "SVE", "SE")
    )
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

        app = Starlette()
        app.add_middleware(BearerTokenMiddleware)
        app.mount("/", mcp.get_asgi_app())

        log.info("Startar HTTP-server på %s:%s", MCP_HOST, MCP_PORT)
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
    else:
        log.info("Startar i stdio-läge")
        mcp.run()
