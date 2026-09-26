"""
mcp_server.py — MCP-server för EU-rätt via CELLAR.

Fem verktyg:
  hamta_eu_akt              — Hämtar en EU-rättsakt via CELEX-nummer (on-demand, cachar i DB)
  hamta_eu_mal              — Hämtar EU-domstolens eller Tribunalens avgöranden (C-xxx eller T-xxx)
  sok_i_cachade_akter       — FTS + semantisk sökning bland lokalt cachade akter
  sok_eu_metadata           — SPARQL-sökning för att hitta akter utan att cacha
  hitta_nationellt_genomforande — Nationella genomförandeåtgärder per medlemsstat via CELLAR

Datakällor:
  SPARQL: http://publications.europa.eu/webapi/rdf/sparql
  Text:   http://publications.europa.eu/resource/celex/{CELEX}
          (innehållsförhandling med Accept och Accept-Language)
  All data hämtas från CELLAR, Publikationsbyråns auktoritativa arkiv som
  EUR-Lex bygger på. EUR-Lex anropas aldrig.

Transport styrs via MCP_TRANSPORT i .env: stdio eller http (se mcp_transport.py).
"""

from __future__ import annotations

import os
import re
import logging
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# typing_extensions.TypedDict krävs av pydantic före Python 3.12.
from typing_extensions import NotRequired, TypedDict

from dotenv import load_dotenv
load_dotenv()          # MÅSTE köras innan "import db" — db.DATABASE_URL läses vid importtid

import requests
from bs4 import BeautifulSoup
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import db
from mcp_annotationer import CACHE_HINTAR, LASNING_DB, LASNING_EXTERN
from mcp_transport import starta

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent.resolve()

SPARQL_ENDPOINT = os.getenv(
    "CELLAR_SPARQL_ENDPOINT",
    "http://publications.europa.eu/webapi/rdf/sparql",
)
CELLAR_REST_BASE = os.getenv(
    "CELLAR_REST_BASE",
    "http://publications.europa.eu/resource/celex",
)
# Senaste släppta version enligt CHANGELOG.md. Rapporteras till klienten
# och ingår i User-Agent, så att källorna kan se vilken version som anropar.
VERSION = "2.0.0"

# Projektets egen User-Agent, med kontaktväg. Den ska aldrig se ut som en
# webbläsare: en ärlig identifiering är det källan kan agera på.
CELLAR_USER_AGENT = os.getenv(
    "CELLAR_USER_AGENT",
    f"mcp-for-cellar/{VERSION} (+https://github.com/MagnusKolsjo/mcp-for-cellar)",
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
_modell_las = threading.Lock()


def _hamta_modell():
    """Laddar embeddingmodellen första gången den behövs.

    Verktygen körs på arbetstrådar. Dubbelkontrollerad låsning hindrar att
    två samtidiga anrop laddar modellen var för sig, utan att senare anrop
    behöver ta låset.
    """
    global _modell
    if _modell is None:
        with _modell_las:
            if _modell is None:
                from sentence_transformers import SentenceTransformer
                log.info("Laddar embeddingmodell: %s", EMBEDDING_MODEL)
                _modell = SentenceTransformer(EMBEDDING_MODEL)
    return _modell


def _forvarm_modell() -> None:
    """Laddar modellen före första anropet i http-läget, om den används.

    Embeddings lagras och söks bara i PostgreSQL; med SQLite behövs modellen inte.
    """
    if db.DATABASE_URL and db._ar_postgres():
        _hamta_modell()

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

# EU:s 24 officiella språk. Verktygen använder tvåbokstavskoder
# (ISO 639-1); CELLAR använder trebokstavskoder (ISO 639-2/T) både i
# språk-URI:erna och i Accept-Language vid innehållsförhandling.
_SPRAK_3: dict[str, str] = {
    "BG": "BUL", "CS": "CES", "DA": "DAN", "DE": "DEU", "EL": "ELL",
    "EN": "ENG", "ES": "SPA", "ET": "EST", "FI": "FIN", "FR": "FRA",
    "GA": "GLE", "HR": "HRV", "HU": "HUN", "IT": "ITA", "LT": "LIT",
    "LV": "LAV", "MT": "MLT", "NL": "NLD", "PL": "POL", "PT": "POR",
    "RO": "RON", "SK": "SLK", "SL": "SLV", "SV": "SWE",
}
_SPRAK_2: dict[str, str] = {tre: tva for tva, tre in _SPRAK_3.items()}

# Format som går att göra text av, i den ordning de provas. XHTML bär
# ELI-strukturen (id="art_N") och ger de säkraste artikelutdragen. PDF
# kräver textextraktion och tappar strukturen. Formex (fmx4) och DOC tas
# inte med: de kräver egna tolkar och finns i praktiken bara där XHTML
# eller PDF också finns.
_ACCEPT_TYP: dict[str, str] = {
    "xhtml": "application/xhtml+xml",
    "html":  "text/html",
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

# En session för alla anrop mot CELLAR, SPARQL-tjänsten och riksdagen: återanvända anslutningar och projektets egen User-Agent.
# Headers sätts bara här, vid modulinläsning, så att sessionen kan delas
# mellan verktygsanrop som körs på olika trådar.
_HTTP = requests.Session()
_HTTP.headers["User-Agent"] = CELLAR_USER_AGENT

# En manifestation kan bestå av flera dokument (DOC_1, DOC_2, ...). Taket
# skyddar mot en felaktig lista i ett 300-svar; ingen känd akt har fler.
_MAX_DELAR = 50


class HamtningsFel(Exception):
    """Förväntat fel vid hämtning från CELLAR.

    Meddelandet är skrivet för den som anropar verktyget och förs vidare
    oförändrat till verktygets felsvar.
    """


class CelexOkant(HamtningsFel):
    """CELLAR känner inte till CELEX-numret."""


class KallaSvararInte(HamtningsFel):
    """CELLAR eller SPARQL-tjänsten svarade inte, eller svarade med serverfel."""


class _TextSaknas(Exception):
    """Ingen text gick att hämta på ett visst språk. Bär försöksloggen."""

    def __init__(self, forsok: list[str]):
        super().__init__("; ".join(forsok))
        self.forsok = forsok


def _kora_sparql(query: str) -> list[dict]:
    """Kör en SPARQL-fråga mot CELLAR och returnerar rader som ordböcker.

    Kastar KallaSvararInte vid timeout, nätverksfel, HTTP-fel eller svar
    som inte är JSON. Anslutningen får tio sekunder; själva frågan får
    SPARQL_TIMEOUT, eftersom tunga titelsökningar tar tid hos tjänsten.
    """
    log.info("Kör SPARQL (%d tecken)", len(query))
    try:
        svar = _HTTP.post(
            SPARQL_ENDPOINT,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=(10, SPARQL_TIMEOUT),
        )
        svar.raise_for_status()
    except requests.Timeout as exc:
        raise KallaSvararInte(
            f"CELLAR:s SPARQL-tjänst svarade inte inom {SPARQL_TIMEOUT} sekunder."
        ) from exc
    except requests.HTTPError as exc:
        raise KallaSvararInte(
            f"CELLAR:s SPARQL-tjänst svarade med HTTP {exc.response.status_code}."
        ) from exc
    except requests.RequestException as exc:
        raise KallaSvararInte(
            f"Kunde inte nå CELLAR:s SPARQL-tjänst ({type(exc).__name__})."
        ) from exc
    try:
        data = svar.json()
    except ValueError as exc:
        raise KallaSvararInte(
            "CELLAR:s SPARQL-tjänst gav ett svar som inte gick att tolka."
        ) from exc
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


def _celex_monster(variabel: str, celex: str) -> str:
    """SPARQL-mönster som binder ?variabel till verket med ett visst CELEX-nummer.

    CELLAR lagrar CELEX-numret som xsd:string. Ett VALUES-block med den
    typade och den otypade literalen slår direkt i tjänstens index, medan
    FILTER(STR(?x) = "...") får tjänsten att gå igenom alla CELEX-nummer
    och tar tiotals sekunder per fråga.
    """
    c = _sparql_escape(celex)
    return (
        f'VALUES ?{variabel}_celex {{ "{c}"^^<http://www.w3.org/2001/XMLSchema#string> "{c}" }}\n'
        f"  ?{variabel} cdm:resource_legal_id_celex ?{variabel}_celex ."
    )


def _normalisera_sprak(sprak: str) -> tuple[str, str]:
    """Returnerar (tvåbokstavskod, trebokstavskod) för ett EU-språk.

    Tar emot både 'SV' och 'SWE', i valfri skiftläge. Kastar HamtningsFel
    för koder som inte är något av EU:s officiella språk.
    """
    s = sprak.strip().upper()
    if s in _SPRAK_3:
        return s, _SPRAK_3[s]
    if s in _SPRAK_2:
        return _SPRAK_2[s], s
    raise HamtningsFel(
        f"Okänd språkkod {sprak!r}. Ange en tvåbokstavskod för ett av EU:s "
        f"officiella språk: {', '.join(sorted(_SPRAK_3))}."
    )


def _lista_manifestationer(celex: str) -> Optional[dict[str, set[str]]]:
    """Frågar CELLAR vilka språk och format som finns för en akt.

    Returnerar {trebokstavskod: {format, ...}}, t.ex.
    {"SWE": {"xhtml", "pdfa1a", "fmx4"}}. En tom ordbok betyder att CELLAR
    inte har några manifestationer för verket. None betyder att frågan
    misslyckades och att inget är känt om formaten.
    """
    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?sprak ?typ
WHERE {{
  {_celex_monster("work", celex)}
  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language ?sprak .
  ?manif cdm:manifestation_manifests_expression ?expr ;
         cdm:manifestation_type ?typ .
}}"""
    try:
        rader = _kora_sparql(query)
    except HamtningsFel as exc:
        log.warning("Kunde inte lista manifestationer för %s: %s", celex, exc)
        return None

    resultat: dict[str, set[str]] = {}
    for r in rader:
        sprak = (r.get("sprak") or "").rsplit("/", 1)[-1].upper()
        typ = (r.get("typ") or "").rsplit("/", 1)[-1].lower()
        if sprak and typ:
            resultat.setdefault(sprak, set()).add(typ)
    log.info("Manifestationer för %s: %d språk", celex, len(resultat))
    return resultat


class _Manifestlista:
    """Manifestlistan för en akt, hämtad lat och högst en gång.

    Skapas per verktygsanrop och delas mellan språkförsöken, så att
    SPARQL-frågan bara körs när innehållsförhandlingen inte räckte.
    """

    def __init__(self, celex: str):
        self.celex = celex
        self._hamtad = False
        self._varde: Optional[dict[str, set[str]]] = None

    def hamta(self) -> Optional[dict[str, set[str]]]:
        if not self._hamtad:
            self._varde = _lista_manifestationer(self.celex)
            self._hamtad = True
        return self._varde


def _cellar_get(url: str, **kwargs) -> requests.Response:
    """GET mot CELLAR. Nätverksfel blir KallaSvararInte."""
    try:
        return _HTTP.get(url, timeout=REST_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise KallaSvararInte(
            f"CELLAR svarade inte ({type(exc).__name__}). Försök igen om en stund."
        ) from exc


def _hamta_del(url: str) -> Optional[bytes]:
    """Hämtar ett dokument (DOC_n). Returnerar None om CELLAR saknar det (404).

    Tillfälliga fel (429, 5xx, nätverksfel) prövas en gång till efter en
    kort paus; kvarstår felet kastas KallaSvararInte.
    """
    for forsok in range(2):
        try:
            r = _cellar_get(url)
        except KallaSvararInte:
            if forsok == 0:
                time.sleep(2)
                continue
            raise
        if r.status_code == 200 and r.content:
            return r.content
        if r.status_code == 404:
            return None
        if forsok == 0 and (r.status_code == 429 or r.status_code >= 500):
            log.info("CELLAR svarade HTTP %d för %s — försöker igen", r.status_code, url)
            time.sleep(2)
            continue
        raise KallaSvararInte(
            f"CELLAR svarade HTTP {r.status_code} för {url.rsplit('/', 1)[-1]}. "
            "Försök igen om en stund."
        )
    raise KallaSvararInte("CELLAR svarade inte. Försök igen om en stund.")


def _hamta_delar(urls: list[str]) -> list[bytes]:
    """Hämtar alla dokument i en manifestation, eller inget.

    En rättsakt vars mittersta del saknas ser komplett ut för läsaren och
    skulle dessutom sparas så i cachen. Därför returneras antingen alla
    delar eller, när ingen del finns, en tom lista (formatet saknas). Finns
    vissa delar men inte andra kastas KallaSvararInte med de saknade delarna.
    """
    urls = urls[:_MAX_DELAR]
    innehall: list[bytes] = []
    saknade: list[str] = []
    for url in urls:
        log.info("Hämtar %s", url)
        del_ = _hamta_del(url)
        if del_ is None:
            saknade.append(url.rsplit("/", 1)[-1])
        else:
            innehall.append(del_)
    if innehall and saknade:
        raise KallaSvararInte(
            f"CELLAR levererade bara {len(innehall)} av {len(urls)} delar av "
            f"dokumentet (saknas: {', '.join(saknade)}). Texten returneras inte "
            "ofullständig. Försök igen om en stund."
        )
    return innehall


def _forhandla(celex: str, sprak3: str, accept: str) -> tuple[list[bytes], str]:
    """Hämtar en akt genom innehållsförhandling mot CELLAR:s CELEX-resurs.

    GET {CELLAR_REST_BASE}/{CELEX} med Accept (format) och Accept-Language
    (språk) svarar 303 till manifestationens dokument när det finns ett, och
    300 med en lista när manifestationen består av flera. 404 betyder att
    formatet eller språket saknas, 400 och 406 att kombinationen inte går
    att leverera.

    Returnerar (innehåll, notering). Tomt innehåll betyder att inget fanns;
    noteringen säger varför och används i felmeddelandet. Kastar CelexOkant
    om CELLAR inte känner till CELEX-numret.
    """
    url = f"{CELLAR_REST_BASE}/{quote(celex, safe='()')}"
    r = _cellar_get(
        url,
        headers={"Accept": accept, "Accept-Language": sprak3.lower()},
        allow_redirects=False,
    )
    if r.status_code == 303:
        location = r.headers.get("Location", "")
        if not location:
            return [], "303 utan Location"
        innehall = _hamta_delar([location])
        return innehall, "hämtad" if innehall else "tomt dokument"
    if r.status_code == 300:
        urls = list(dict.fromkeys(re.findall(r'href="([^"]+/DOC_\d+)"', r.text)))
        innehall = _hamta_delar(urls)
        return innehall, f"hämtad ({len(innehall)} delar)" if innehall else "tomma dokument"
    if r.status_code == 404 and re.search(r"Resource \[system 'celex'.*not found", r.text):
        raise CelexOkant(
            f"CELEX-numret {celex} finns inte i CELLAR. Kontrollera numret, "
            "eller sök fram rätt nummer med sok_eu_metadata."
        )
    return [], f"HTTP {r.status_code}"


def _hamta_via_rest(celex: str, sprak3: str, fmt: str) -> tuple[list[bytes], str]:
    """Hämtar en akt via CELLAR:s äldre REST-väg {CELEX}.{SPRÅK}.{format}.

    Vägen svarar 303 till manifestationens RDF, som listar dokumenten. Den
    fungerar för en del akter men ger 404 för andra, även där manifestationen
    finns; därför provas den först när innehållsförhandlingen inte räckt.
    """
    r1 = _cellar_get(f"{CELLAR_REST_BASE}/{celex}.{sprak3}.{fmt}", allow_redirects=False)
    if r1.status_code != 303:
        return [], f"HTTP {r1.status_code}"
    location = r1.headers.get("Location", "")
    if "/rdf/object/full" not in location:
        return [], "oväntad omdirigering"
    manif_bas = location.replace("/rdf/object/full", "")
    rdf = _cellar_get(location)
    doc_urls = re.findall(
        r'rdf:resource="(' + re.escape(manif_bas) + r'/DOC_\d+)"', rdf.text,
    ) if rdf.status_code == 200 else []
    innehall = _hamta_delar(list(dict.fromkeys(doc_urls)) or [manif_bas + "/DOC_1"])
    return innehall, "hämtad" if innehall else "tomt dokument"


def _pdf_till_text(innehall: list[bytes]) -> tuple[Optional[str], str]:
    """Extraherar text ur PDF-dokument. Returnerar (text eller None, notering)."""
    try:
        import pdfplumber
        from io import BytesIO
    except ImportError:
        log.warning("pdfplumber saknas — PDF-texten kan inte extraheras")
        return None, "pdfplumber är inte installerat"
    delar: list[str] = []
    try:
        for pdf_bytes in innehall:
            with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
                for sida in pdf.pages:
                    text = sida.extract_text()
                    if text:
                        delar.append(text)
    except Exception as exc:
        log.warning("PDF-extraktion misslyckades: %s", exc)
        return None, "PDF gick inte att läsa"
    if not delar:
        return None, "PDF utan extraherbar text"
    return "\n".join(delar), "hämtad"


def _hamta_cellar_text(celex: str, sprak: str, manifest: _Manifestlista) -> tuple[str, str]:
    """Hämtar text för en akt på ett språk. Returnerar (rå_text, format).

    Formatet är 'xhtml', 'html' eller 'pdf'. För xhtml och html är texten
    rå markup som anroparen rensar; för pdf är den redan extraherad.

    Ordning:
      1. Innehållsförhandling mot {CELLAR_REST_BASE}/{CELEX} för xhtml och html.
      2. Manifestlistan via SPARQL (bara om steg 1 inte räckte): saknas
         språket helt avbryts försöket här.
      3. Innehållsförhandling för PDF, med den PDF-typ CELLAR anger
         (application/pdf;type=pdfa1a o.s.v.).
      4. Den äldre REST-vägen {CELEX}.{SPRÅK}.{format}.

    Äldre akter (t.ex. 31958R0001) finns bara som html och hämtas i steg 1.

    Kastar CelexOkant, KallaSvararInte, HamtningsFel (okänd språkkod) eller
    _TextSaknas med försöksloggen.
    """
    sprak2, sprak3 = _normalisera_sprak(sprak)
    forsok: list[str] = []

    for fmt, accept in _ACCEPT_TYP.items():
        innehall, notering = _forhandla(celex, sprak3, accept)
        forsok.append(f"{fmt}: {notering}")
        if innehall:
            return "\n".join(c.decode("utf-8", errors="replace") for c in innehall), fmt

    lista = manifest.hamta()
    typer = lista.get(sprak3, set()) if lista is not None else None
    if lista and not typer:
        forsok.append(f"CELLAR har ingen version på {sprak2}")
        raise _TextSaknas(forsok)

    # PDF-typen måste anges exakt; "application/pdf" räcker bara för typen pdf.
    pdf_typer = (
        sorted((t for t in typer if t.startswith("pdf")), key=lambda t: (t != "pdf", t))
        if typer is not None else ["pdf"]
    )
    for typ in pdf_typer:
        accept = "application/pdf" if typ == "pdf" else f"application/pdf;type={typ}"
        innehall, notering = _forhandla(celex, sprak3, accept)
        if innehall:
            text, notering = _pdf_till_text(innehall)
            if text:
                return text, "pdf"
        forsok.append(f"{typ}: {notering}")

    for fmt in ("xhtml", "html", "pdf"):
        if typer is not None and fmt not in typer:
            continue
        innehall, notering = _hamta_via_rest(celex, sprak3, fmt)
        if innehall and fmt == "pdf":
            text, notering = _pdf_till_text(innehall)
            if text:
                return text, "pdf"
        elif innehall:
            return "\n".join(c.decode("utf-8", errors="replace") for c in innehall), fmt
        forsok.append(f"REST {fmt}: {notering}")

    raise _TextSaknas(forsok)


def _beskriv_tillgangligt(lista: Optional[dict[str, set[str]]]) -> str:
    """Beskriver vilka språk och format CELLAR har, för felmeddelanden."""
    if lista is None:
        return (
            "Listan över tillgängliga språk och format kunde inte hämtas, "
            "eftersom CELLAR:s SPARQL-tjänst inte svarade."
        )
    if not lista:
        return (
            "CELLAR har ingen digital text för akten på något språk, bara "
            "metadata. Servern hämtar all text från CELLAR och har därför "
            "ingen text att ge."
        )
    grupper: dict[tuple[str, ...], list[str]] = {}
    for sprak3, typer in lista.items():
        grupper.setdefault(tuple(sorted(typer)), []).append(_SPRAK_2.get(sprak3, sprak3))
    delar = [
        f"{', '.join(sorted(sprak))} ({', '.join(typer)})"
        for typer, sprak in sorted(grupper.items(), key=lambda g: -len(g[1]))
    ]
    return (
        "Akten finns i CELLAR på: " + "; ".join(delar) + ". "
        "Servern läser formaten xhtml, html och pdf."
    )


def _hamta_text_med_sprakordning(
    celex: str, sprakordning: list[str],
) -> tuple[str, str, str]:
    """Provar språken i tur och ordning. Returnerar (rå_text, format, språk).

    Kastar HamtningsFel med ett meddelande som säger vilka språk och format
    som finns, när inget av de provade språken gav text.
    """
    manifest = _Manifestlista(celex)
    logg: list[str] = []
    provade: list[str] = []
    for sprak in dict.fromkeys(s.strip().upper() for s in sprakordning):
        sprak2, _ = _normalisera_sprak(sprak)
        if sprak2 in provade:
            continue
        provade.append(sprak2)
        try:
            raw, fmt = _hamta_cellar_text(celex, sprak2, manifest)
            return raw, fmt, sprak2
        except _TextSaknas as exc:
            logg.append(f"{sprak2}: {'; '.join(exc.forsok)}")

    raise HamtningsFel(
        f"Ingen text för {celex} på {' eller '.join(provade)}. "
        f"{_beskriv_tillgangligt(manifest.hamta())} "
        "Ange ett tillgängligt språk med parametern sprak. "
        f"Provat: {' | '.join(logg)}."
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

    # Fallback: sök i ren text efter artikelrubriken i radinledning.
    return _artikel_ur_klartext(_rensa_html(html), artikel_nr)


def _artikel_ur_klartext(text: str, artikel_nr: int) -> Optional[str]:
    """Hittar en artikel i klartext: från rubriken "Artikel N" till nästa artikel.

    ^-ankaret med re.MULTILINE kräver att rubriken står först på raden, så
    att hänvisningar som "artikel 5 i fördraget" i skälen inte träffas.
    "Article" täcker engelska och franska, som språkordningen kan landa i.
    """
    m = re.search(
        rf'^((?:Artikel|Article)\s+{artikel_nr}\b.*?)(?=^(?:Artikel|Article)\s+\d+\b|\Z)',
        text, re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )
    if m and len(m.group(1).strip()) > 20:
        return m.group(1).strip()
    return None


def _samma_sprak(cachat: Optional[str], sprak2: str) -> bool:
    """Om en cachad rads språk motsvarar det begärda (tvåbokstavskod)."""
    if not cachat:
        return False
    try:
        return _normalisera_sprak(cachat)[0] == sprak2
    except HamtningsFel:
        return False


# Embeddingmodellen (KBLab/sentence-bert-swedish-cased) läser högst 384
# tokens; resten av en längre chunk kommer aldrig in i vektorn. 250 ord
# svensk lagtext ryms med marginal. Överlappet gör att en mening som
# hamnar vid en chunkgräns finns hel i minst en chunk.
CHUNK_MAX_ORD = 250
CHUNK_OVERLAPP_ORD = 40

_ARTIKELRUBRIK = re.compile(r"^(?:Artikel|Article)\s+\d+\b", re.IGNORECASE)


def _chunka_text(
    text: str,
    max_ord: int = CHUNK_MAX_ORD,
    overlapp: int = CHUNK_OVERLAPP_ORD,
) -> list[str]:
    """Delar en akts klartext i chunks om högst max_ord ord.

    Den rensade texten har ett stycke per rad. Stycken samlas tills gränsen
    nås, och en artikelrubrik börjar en ny chunk när den pågående redan har
    innehåll, så att artiklar i möjligaste mån hålls samman. Varje ny chunk
    inleds med de sista `overlapp` orden ur den förra. Stycken som ensamma
    är längre än gränsen delas på ordnivå.
    """
    ord_per_stycke: list[list[str]] = []
    for rad in text.splitlines():
        ord_ = rad.split()
        if not ord_:
            continue
        # Långa stycken delas i bitar som får plats med överlappet.
        steg = max_ord - overlapp
        while len(ord_) > steg:
            ord_per_stycke.append(ord_[:steg])
            ord_ = ord_[steg:]
        ord_per_stycke.append(ord_)

    chunks: list[str] = []
    aktuell: list[str] = []
    ny_i_aktuell = 0  # ord i aktuell chunk utöver överlappet
    for ord_ in ord_per_stycke:
        artikelstart = bool(_ARTIKELRUBRIK.match(" ".join(ord_[:3])))
        if ny_i_aktuell and (len(aktuell) + len(ord_) > max_ord or artikelstart):
            chunks.append(" ".join(aktuell))
            aktuell = aktuell[-overlapp:] if overlapp and not artikelstart else []
            ny_i_aktuell = 0
        aktuell.extend(ord_)
        ny_i_aktuell += len(ord_)
    if ny_i_aktuell:
        chunks.append(" ".join(aktuell))
    return [c for c in chunks if len(c) > 50]


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
SELECT ?titel ?datum ?eli ?typ_uri
WHERE {{
  {_celex_monster("work", celex)}
  ?work cdm:work_date_document ?datum ;
        cdm:work_has_resource-type ?typ_uri .
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
                "typ":   typ_kod or None,
            }
    except HamtningsFel as exc:
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
    chunks = _chunka_text(fulltext)
    if not chunks or not db.DATABASE_URL:
        return
    if not db._ar_postgres():
        # SQLite lagrar bara chunktexten; att räkna embeddings vore bortkastat.
        db.spara_chunks(celex, chunks, [])
        return
    try:
        modell = _hamta_modell()
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
    svar = _HTTP.get(
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

# ---------------------------------------------------------------------------
# Svarstyper
#
# Fält som kan saknas i CELLAR:s metadata (titel, datum, ELI) eller i äldre
# cacherader är typade som X | None, eftersom ett None i ett strikt typat
# fält får hela anropet att misslyckas.
# ---------------------------------------------------------------------------

class AktSvar(TypedDict):
    celex: str
    sprak: str
    format: NotRequired[str]
    titel: str | None
    datum: str | None
    eli: str | None
    fran_cache: bool
    artikel: NotRequired[int]
    tecken: NotRequired[int]
    fulltext: str


class MalSvar(TypedDict):
    malnum: str
    celex: str
    domstol: str
    sprak: str
    format: NotRequired[str]
    titel: str | None
    datum: str | None
    eli: NotRequired[str | None]
    fran_cache: bool
    tecken: int
    fulltext: str


class CacheTraff(TypedDict):
    celex: str
    titel: str | None
    datum: str | None
    typ: str | None
    eli: str | None
    rang: NotRequired[float | None]
    likhet: NotRequired[float | None]
    utdrag: str | None


class CacheSokSvar(TypedDict):
    fraga: str
    antal: int
    treffar: list[CacheTraff]


class MetadataTraff(TypedDict):
    celex: str | None
    titel: str | None
    datum: str | None
    eli: str | None


class MetadataSvar(TypedDict):
    antal: int
    sokterm: str | None
    typ: str | None
    ar_fran: int | None
    ar_till: int | None
    expansion: list[str] | None
    treffar: list[MetadataTraff]
    tips: str


class Genomforande(TypedDict):
    celex: str
    datum: str | None
    titel: str | None
    medlemsstat: str
    kalla: str


class RiksdagProposition(TypedDict):
    beteckning: str | None
    rubrik: str | None
    datum: str | None
    url: str
    kalla: str


# Fältet "not" är ett reserverat ord i Python och kräver funktionsformen.
GenomforandeSvar = TypedDict("GenomforandeSvar", {
    "celex": str,
    "eu_nummer": "str | None",
    "filtrar_pa_stat": "str | None",
    "cellar_genomforanden": list[Genomforande],
    "riksdag_propositioner": list[RiksdagProposition],
    "riksdag_fel": NotRequired[str],
    "not": str,
})


mcp = MCPServer(
    "cellar-eu-ratt",
    version=VERSION,
    cache_hints=CACHE_HINTAR,
    instructions=(
        "MCP-server för EU-rätt via CELLAR. Verktyg: hamta_eu_akt (rättsakter via CELEX), "
        "hamta_eu_mal (domar C-xxx/T-xxx), sok_i_cachade_akter (FTS/semantisk sökning), "
        "sok_eu_metadata (SPARQL-sökning utan cache), "
        "hitta_nationellt_genomforande (nationellt genomförande per medlemsstat)."
    ),
)


@mcp.tool(
    name="hamta_eu_akt",
    title="Hämta EU-rättsakt",
    annotations=LASNING_EXTERN,
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
                 artikel: Optional[int] = None) -> AktSvar:
    """Hämtar och cachar en EU-rättsakt.

    Args:
        celex:   CELEX-nummer, t.ex. '32006L0054'.
        sprak:   Önskat språk — 'SV' (standard), 'EN', 'DE', 'FR'.
        artikel: Om satt hämtas bara detta artikelnummer (t.ex. artikel=3).
                 Användbart för långa direktiv där hela texten trunkeras.
    """
    celex = celex.strip().upper()
    log.info("hamta_eu_akt: celex=%s sprak=%s artikel=%s", celex, sprak, artikel)
    try:
        sprak2, _ = _normalisera_sprak(sprak)
    except HamtningsFel as exc:
        raise ToolError(str(exc)) from exc

    # Cachen används bara när akten ligger där på det begärda språket;
    # annars hämtas den på nytt, så att svaret blir detsamma som vid en
    # direkthämtning. Cachen har en rad per akt, med senast hämtade språk.
    cachad = db.hamta_cachad_akt(celex)
    raw: Optional[str] = None
    anvant_format: Optional[str] = None
    if cachad and cachad.get("fulltext_md") and _samma_sprak(cachad.get("sprak"), sprak2):
        log.info("Serverar %s (%s) från cache", celex, sprak2)
        fulltext_full = cachad["fulltext_md"]
        anvant_sprak = sprak2
        meta = {"titel": cachad["titel"], "datum": cachad["datum"], "eli": cachad["eli"]}
        fran_cache = True
    else:
        # Texten hämtas före metadatan: ett okänt CELEX-nummer avslöjas då
        # redan av första anropet mot CELLAR.
        try:
            raw, anvant_format, anvant_sprak = _hamta_text_med_sprakordning(
                celex, [sprak2, "EN"],
            )
        except HamtningsFel as exc:
            raise ToolError(str(exc)) from exc
        meta = _hamta_sparql_metadata(celex) or {}
        # Full otrunkerad text sparas; trunkering sker bara i svaret.
        fulltext_full = _rensa_html(raw) if anvant_format in ("xhtml", "html") else raw
        _indexera_akt(
            celex, anvant_sprak, fulltext_full,
            meta.get("titel"), meta.get("datum"), meta.get("eli"), meta.get("typ"),
        )
        fran_cache = False

    svar: AktSvar = {
        "celex":      celex,
        "sprak":      anvant_sprak,
        "titel":      meta.get("titel"),
        "datum":      meta.get("datum"),
        "eli":        meta.get("eli"),
        "fran_cache": fran_cache,
        "fulltext":   "",
    }
    if anvant_format:
        svar["format"] = anvant_format

    if artikel is not None:
        # XHTML/HTML ger strukturerad extraktion via id-attribut; klartexten
        # (som är det cachen har) söks med samma mönster i båda vägarna.
        art_text = None
        if raw is not None and anvant_format in ("xhtml", "html"):
            art_text = _extrahera_artikel(raw, artikel)
        if not art_text:
            art_text = _artikel_ur_klartext(fulltext_full, artikel)
        if not art_text:
            raise ToolError(
                f"Artikel {artikel} hittades inte i {celex} ({anvant_sprak}). "
                "Kontrollera att artikelnumret stämmer, eller hämta hela akten "
                "utan parametern artikel."
            )
        svar["artikel"] = artikel
        svar["fulltext"] = art_text
        return svar

    svar["tecken"] = len(fulltext_full)
    svar["fulltext"] = _trunkera(fulltext_full)
    return svar


@mcp.tool(
    name="hamta_eu_mal",
    title="Hämta EU-domstolens avgörande",
    annotations=LASNING_EXTERN,
    description=(
        "Hämtar fulltext för ett EU-domstolsavgörande. "
        "Accepterar EU-domstolens målnummer (C-441/17, C-30/19 PPU) "
        "och Tribunalens målnummer (T-325/15). "
        "Saknas avgörandet på önskat språk provas franska och sedan engelska; "
        "fältet sprak i svaret visar vilket språk texten har."
    ),
)
def hamta_eu_mal(malnum: str, sprak: str = "SV") -> MalSvar:
    """Hämtar ett EU-domstolsavgörande.

    Args:
        malnum: Målnummer, t.ex. 'C-441/17', 'C-30/19 PPU', 'T-325/15',
                eller direkt CELEX-format som '62017CJ0441'.
        sprak:  Önskat språk — 'SV' (standard), 'EN', 'FR'.
    """
    malnum_rensat = malnum.strip()
    celex, domstol = _parsera_malnum(malnum_rensat)
    log.info("hamta_eu_mal: malnum=%s → celex=%s domstol=%s", malnum_rensat, celex, domstol)

    try:
        sprak2, _ = _normalisera_sprak(sprak)
    except HamtningsFel as exc:
        raise ToolError(str(exc)) from exc

    # Cachen används bara på det begärda språket, som i hamta_eu_akt.
    cachad = db.hamta_cachad_akt(celex)
    if cachad and cachad.get("fulltext_md") and _samma_sprak(cachad.get("sprak"), sprak2):
        fulltext_full = cachad["fulltext_md"]
        return {
            "malnum":    malnum_rensat,
            "celex":     celex,
            "domstol":   domstol,
            "sprak":     sprak2,
            "titel":     cachad["titel"],
            "datum":     cachad["datum"],
            "fran_cache": True,
            "tecken":    len(fulltext_full),
            "fulltext":  _trunkera(fulltext_full),
        }

    # Franska är domstolens arbetsspråk och finns för alla avgöranden;
    # äldre mål saknar ofta svensk version.
    try:
        raw, anvant_format, anvant_sprak = _hamta_text_med_sprakordning(
            celex, [sprak2, "FR", "EN"],
        )
    except HamtningsFel as exc:
        raise ToolError(f"Avgörande {malnum_rensat} (CELEX {celex}): {exc}") from exc

    meta = _hamta_sparql_metadata(celex) or {}

    fulltext_full = _rensa_html(raw) if anvant_format in ("xhtml", "html") else raw
    # Typkoden ur CELLAR (JUDG, ORDER, OPIN_AG ...) är den som typfiltret i
    # sok_i_cachade_akter jämför mot.
    _indexera_akt(celex, anvant_sprak, fulltext_full,
                  meta.get("titel"), meta.get("datum"), meta.get("eli"),
                  meta.get("typ") or None)

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
    title="Sök bland cachade EU-akter",
    annotations=LASNING_DB,
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
) -> CacheSokSvar:
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
        raise ToolError(
            f"Okänd typ: {typ!r}. Tillgängliga typer: {', '.join(TYP_BESKRIVING)}."
        )

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


# Titelsökningen är en CONTAINS över alla titlar på språket. Utan typ- eller
# årsfilter, och särskilt när inget matchar, går tjänsten igenom allt och kan
# slå i tidsgränsen.
_SOK_TIDSGRANS_TIPS = (
    "Titelsökning utan träff och utan avgränsning kan ta längre tid än "
    "tidsgränsen, eftersom tjänsten då går igenom alla titlar. Avgränsa med "
    "typ och ar_fran/ar_till, prova en annan sökterm eller försök igen senare."
)


@mcp.tool(
    name="sok_eu_metadata",
    title="Sök EU-rättsakter i CELLAR",
    annotations=LASNING_EXTERN,
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
) -> MetadataSvar:
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
        raise ToolError(
            f"Okänd typ: {typ!r}. Tillgängliga typer: {', '.join(TYP_BESKRIVING)}."
        )

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
    except HamtningsFel as exc:
        raise ToolError(f"{exc} {_SOK_TIDSGRANS_TIPS}") from exc

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
    title="Hitta nationellt genomförande",
    annotations=LASNING_EXTERN,
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
) -> GenomforandeSvar:
    """Hittar nationella genomförandeåtgärder för ett EU-direktiv.

    Args:
        celex:      CELEX-nummer för direktivet, t.ex. '32006L0054'.
        medlemsstat: ISO-3-kod, t.ex. 'SWE', 'DEU', 'FRA'. Tom = alla länder.
    """
    celex = celex.strip().upper()
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
  {_celex_monster("direktiv", celex)}

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

    # CELLAR är huvudkällan här. En tom lista efter ett misslyckat anrop
    # skulle se ut som "inga genomföranden", så felet förs vidare.
    cellar_treffar: list[Genomforande] = []
    try:
        rader = _kora_sparql(query)
    except HamtningsFel as exc:
        raise ToolError(
            f"Genomförandeåtgärderna för {celex} kunde inte hämtas: {exc} "
            "Försök igen om en stund."
        ) from exc
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

    # Riksdag-sökning som komplement för Sverige.
    # Normalisera eventuell 2-bokstavs-kod så att "SE" också matchar.
    riksdag_treffar: list[RiksdagProposition] = []
    riksdag_fel: Optional[str] = None
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
            except (requests.RequestException, ValueError) as exc:
                log.warning("Riksdag-sökning misslyckades: %s", exc)
                riksdag_fel = (
                    "Sökningen i riksdagens öppna data misslyckades "
                    f"({type(exc).__name__}); riksdag_propositioner är därför tom."
                )

    svar: GenomforandeSvar = {
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
    if riksdag_fel:
        svar["riksdag_fel"] = riksdag_fel
    return svar


# ---------------------------------------------------------------------------
# Startpunkt och transport
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _konfigurera_logging()
    # db.py öppnar en anslutning per anrop, vilket är trådsäkert även när
    # http-läget kör flera verktygsanrop samtidigt.
    starta(
        mcp,
        standardport=8010,
        initiera=db.initiera_schema,
        forvarm_http=_forvarm_modell,
    )
