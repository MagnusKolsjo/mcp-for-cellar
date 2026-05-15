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

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import db

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

load_dotenv()

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


def _hamta_xhtml(celex: str, sprak: str) -> str:
    """Hämtar XHTML-fulltext för en EU-rättsakt via CELLAR REST API.

    Protokoll (dokumenterat via reverse-engineering av CELLAR WEMI-modellen):

    1. GET {CELLAR_REST_BASE}/{CELEX}.{LANG}.xhtml  →  HTTP 303
       Location: http://publications.europa.eu/resource/cellar/{uuid}.{expr}.{manif}/rdf/object/full

    2. Ersätt /rdf/object/full med /DOC_N och hämta varje item.
       Hämta manifestation-RDF för att hitta alla DOC-items,
       eller prova DOC_1 .. DOC_10 tills 404.

    Språkkod ska vara 3-bokstavs ISO 639-2/T (SWE, ENG, DEU, FRA, ...).
    """
    lang = sprak.upper()
    # Mappa 2-bokstavs → 3-bokstavs om användaren skickar ISO 639-1
    _lang_map = {"SV": "SWE", "EN": "ENG", "DE": "DEU", "FR": "FRA",
                 "DA": "DAN", "FI": "FIN", "NL": "NLD", "PL": "POL",
                 "ES": "SPA", "IT": "ITA", "PT": "POR", "CS": "CES",
                 "HU": "HUN", "RO": "RON", "SK": "SLK", "SL": "SLV"}
    if len(lang) == 2:
        lang = _lang_map.get(lang, lang)

    manifest_url = f"{CELLAR_REST_BASE}/{celex}.{lang}.xhtml"
    log.info("Hämtar CELLAR manifestation: %s", manifest_url)

    # Steg 1: 303-redirect ger oss manifestationens UUID
    r1 = requests.get(manifest_url, timeout=REST_TIMEOUT, allow_redirects=False)
    if r1.status_code == 404:
        raise ValueError(
            f"Ingen XHTML-manifestation för {celex} på {lang}. "
            "Akten kanske inte finns i CELLAR eller saknar den begärda språkversionen."
        )
    if r1.status_code != 303:
        raise ValueError(
            f"Oväntat HTTP {r1.status_code} från CELLAR för {celex}.{lang}.xhtml"
        )

    location = r1.headers.get("Location", "")
    if not "/rdf/object/full" in location:
        raise ValueError(f"Oväntat Location-svar från CELLAR: {location}")

    # Bas-URL för DOC-items = location utan /rdf/object/full
    manif_bas = location.replace("/rdf/object/full", "")

    # Steg 2: Hämta manifestation-RDF och lista alla DOC-items
    try:
        rdf_text = requests.get(location, timeout=REST_TIMEOUT).text
        doc_urls = re.findall(
            r'rdf:resource="(' + re.escape(manif_bas) + r'/DOC_\d+)"',
            rdf_text,
        )
    except Exception:
        doc_urls = []

    if not doc_urls:
        # Fallback: prova DOC_1 direkt
        doc_urls = [manif_bas + "/DOC_1"]

    # Steg 3: Hämta alla DOC-items och konkatenera
    texter: list[str] = []
    for doc_url in doc_urls:
        log.info("Hämtar %s", doc_url)
        r = requests.get(doc_url, timeout=REST_TIMEOUT)
        if r.status_code == 200 and r.text.strip():
            texter.append(r.text)
        elif r.status_code == 404:
            break  # Inga fler items

    if not texter:
        raise ValueError(
            f"CELLAR returnerade tomt innehåll för {celex} ({lang}). "
            "Akten kan sakna XHTML-manifestation."
        )

    return "\n".join(texter)


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

    # Fallback: sök i ren text efter "Artikel N\n"
    ren_text = _rensa_html(html)
    monster = re.compile(
        rf'(Artikel\s+{artikel_nr}\b.*?)(?=Artikel\s+\d+\b|\Z)',
        re.DOTALL | re.IGNORECASE,
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
    """Hämtar titel, datum och ELI för ett CELEX via SPARQL."""
    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?titel ?datum ?eli ?typ
WHERE {{
  ?work cdm:resource_legal_id_celex ?celex_val ;
        cdm:work_date_document ?datum ;
        cdm:work_has_resource-type ?typ_uri .
  FILTER(STR(?celex_val) = "{celex}")
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
            # Försök extrahera specifikt artikelnummer ur cachad text
            art_text = re.search(
                rf'(Artikel\s+{artikel}\b.*?)(?=Artikel\s+\d+\b|\Z)',
                fulltext_full, re.DOTALL | re.IGNORECASE,
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
        # Fallback: sök i ren text
        art_match = re.search(
            rf'(Artikel\s+{artikel}\b.*?)(?=Artikel\s+\d+\b|\Z)',
            fulltext_full, re.DOTALL | re.IGNORECASE,
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
        "Kräver att akten finns med svensk titel (titlar på Expression-noden i CDM). "
        "Pre-1995 akter saknar ofta svenska titlar — ange sprak='EN' i sådana fall. "
        "Tillgängliga typer: direktiv, forordning, forordning_delegerad, "
        "forordning_genomforande, direktiv_delegerat, beslut, beslut_genomforande, "
        "beslut_delegerat, rekommendation, yttrande, dom, beslut_domstol, "
        "yttrande_generaladvokat, forslag_direktiv, forslag_forordning, forslag_beslut, "
        "kommunike, gronbok, vitbok, konsoliderad, nationellt_genomforande."
    ),
)
def sok_eu_metadata(
    typ: Optional[str] = None,
    ar_fran: Optional[int] = None,
    ar_till: Optional[int] = None,
    sprak: str = "SV",
    max_antal: int = 20,
) -> dict:
    """Söker EU-rättsakter via SPARQL (metadatasökning, ingen cachning).

    Args:
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

    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>

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
}}
ORDER BY DESC(?datum)
LIMIT {max_antal}"""

    try:
        rader = _kora_sparql(query)
    except requests.RequestException as exc:
        return {"fel": f"SPARQL-anrop misslyckades: {exc}"}

    return {
        "antal":  len(rader),
        "typ":    typ,
        "ar_fran": ar_fran,
        "ar_till": ar_till,
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
    stat_filter = ""
    if medlemsstat:
        stat_kod = medlemsstat.strip().upper()
        stat_uri = STAT_URIS.get(stat_kod)
        if stat_uri:
            stat_filter = (
                f"  ?genomf cdm:member_state_of_publication <{stat_uri}> ."
            )
        else:
            stat_filter = f"""  ?genomf cdm:member_state_of_publication ?stat .
  FILTER(STR(?stat) = "http://publications.europa.eu/resource/authority/country/{stat_kod}")"""

    query = f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>

SELECT DISTINCT ?genomf_celex ?stat ?titel ?datum
WHERE {{
  ?direktiv cdm:resource_legal_id_celex ?dir_celex .
  FILTER(STR(?dir_celex) = "{celex}")

  ?genomf cdm:resource_legal_implements_resource_legal ?direktiv ;
          cdm:resource_legal_id_celex ?genomf_celex ;
          cdm:work_date_document ?datum .
{stat_filter}
  OPTIONAL {{ ?genomf cdm:member_state_of_publication ?stat . }}
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
