"""
Route Editor Agent — Stadio 1 di 4: Intent Parser.

Modifica guidata di un GPX esistente tramite istruzioni testuali dell'utente
(es. "da qui in poi invece di andare a X, passa per Y e torna a raccordarti
al percorso originale in Z"). Concettualmente diverso dal Planner esistente
(planner_agent.py): quello genera un percorso da zero, questo ne modifica uno
già esistente — modulo separato, anche se riusa componenti comuni (geocoding
via geocoding_agent, stesso client AI via ai_client).

Pipeline completa prevista (SOLO lo Stadio 1 è implementato qui):
  1. Intent Parser (questo modulo)      — GPX + testo → decisione, vincoli, waypoint
  2. Candidate Generator + Evaluator    — BRouter tra gli ANCHOR, scarta INVALID
  3. Presentazione e conferma utente    — nessuno score sceglie da solo il vincitore
  4. GPX Splicer + validazione finale   — fonde, rimuove corni, valida

Questo modulo NON chiama BRouter, NON produce un GPX, NON si integra con
Planner/Builder — è testabile in isolamento passando un GPX + un testo.

── Geocodifica: nucleo ad alto impatto + motore fuzzy (validati separatamente,
ora ricollegati) ──────────────────────────────────────────────────────────────
Diagnosi di partenza: le query fallivano non solo per forme testuali imprecise,
ma perché l'Intent Parser trattava proprie INFERENZE (es. "Chiaravalle" per
"Il Grottino", mai scritto dall'utente) come fatti certi dentro la query — un
qualificatore geografico sbagliato fa fallire Nominatim per intero invece di
essere ignorato. Il nucleo:
  1. explicit_context (solo termini scritti dall'utente) vs inferred_context
     (aggiunte del modello) — riportati entrambi nell'output per trasparenza,
     ma la geocodifica usa solo "name" (vedi punto 3): un comune inferito e
     sbagliato non deve mai vincolare la ricerca.
  2. Viewbox del GPX (bounding box + margine) come contesto geografico
     primario, al posto del comune/frazione indovinato dal modello — SEMPRE
     presente, mai una ricerca globale poi filtrata.
  3. Motore di geocodifica fuzzy (geocoding_agent.fuzzy_geocode_in_area,
     validato in isolamento su questo stesso caso prima di essere ricollegato
     qui): genera piccole varianti testuali meccaniche (articolo, prefisso
     stradale, desinenza) di "name" e raccoglie TUTTI i candidati trovati nel
     viewbox da qualunque variante — nessuna scelta automatica del "migliore"
     tra più candidati, quella disambiguazione resta un passo successivo
     deliberatamente rimandato.
  4. Mappatura ruolo→marcatore: ANCHOR/MANDATORY → "!" (vincolante, coordinata
     grezza mandatory per le regole esistenti dell'app), SHAPING → "~"
     (indicativo, non vincolante — senza questo marcatore esplicito una
     coordinata grezza diventerebbe mandatory per sbaglio, vanificando SHAPING).
Deliberatamente NON implementati in questo giro: disambiguazione automatica
tra candidati multipli (fuzzy matching Overpass, scoring pesato per
somiglianza/vicinanza/tipo OSM/coerenza sequenza), normalizzazione
linguistica estesa oltre le regole meccaniche già in geocoding_agent.py
(S./San/Santa, Contrada/C.da...).

── ANCHOR: map-matching invece della geocodifica singola (validato su un
caso reale) ────────────────────────────────────────────────────────────────
Un ANCHOR non è "dove si trova questo luogo nel mondo" (quello è geocodifica,
sopra) — è "dove nel GPX ORIGINALE SPECIFICO il tracciato lascia una via per
andarne su un'altra". Quando l'Intent Parser riconosce questo pattern
("leaving_road" valorizzato sul waypoint), la risoluzione usa map_matcher.py
(HMM via leuvenmapmatching, area OSM scaricata via Overpass — riusa
area_resolver.query_overpass, nessuna query duplicata) invece della
geocodifica fuzzy: il risultato è un punto preciso sul tracciato originale
(nodo OSM reale se la transizione coincide con un'intersezione, altrimenti
punto medio interpolato tra i due trackpoint a cavallo della transizione),
non una coordinata isolata. Se `toward_place` è anche valorizzato, il
tracciato dopo l'anchor viene verificato per direzione (bearing) contro
quel punto geocodificato — scarta implicitamente un anchor la cui
prosecuzione tornerebbe indietro invece di procedere verso il luogo
descritto dall'utente.

Il centro approssimativo attorno a cui cercare la finestra locale viene
dalla geocodifica fuzzy di wp.name — quando questa restituisce più
candidati (es. più segmenti di "Via Brecciata" in punti diversi del
tracciato, il caso "multi-segmento" ancora da disambiguare), si prova ogni
candidato in ordine finché uno produce una transizione valida: non è
disambiguazione (nessuno scoring tra i candidati), solo un tentativo
sequenziale sugli stessi candidati già raccolti — la disambiguazione vera
resta il prossimo passo naturale, non costruita qui.

Affidabilità di leaving_road/toward_place (osservato su run ripetute: il
primo passaggio, un solo prompt che produce insieme decision+constraints+
waypoints, non applica il pattern in modo affidabile su ogni ANCHOR
pertinente): per ogni ANCHOR rimasto con leaving_road=None dopo il primo
passaggio, una rete di sicurezza secondaria (_followup_leaving_road) lo
richiede con un prompt molto più piccolo, mirato su UN SOLO waypoint, prima
di ricadere sulla geocodifica standard. `leaving_road_source` sul waypoint
distingue "first_pass" da "followup" da None (mai risolto, incluso il caso
legittimo di un ANCHOR di solo raccordo che il testo non descrive come
abbandono di una strada).
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Literal

import gpxpy
from pydantic import BaseModel, ConfigDict, Field

import ai_client
from gpx_analyzer import smooth_elevations, sum_uphill_downhill
from geocoding_agent import fuzzy_geocode_in_area
from area_resolver import OverpassUnavailable
import map_matcher

log = logging.getLogger(__name__)


# ── Marcatore ruolo waypoint ───────────────────────────────────────────────────
# Convenzione già esistente in main.py (_parse_pl_waypoint_line): "!" finale su
# una riga di testo marca un waypoint come mandatory; una coordinata lat,lon
# grezza è SEMPRE mandatory anche senza "!", perché scrivere una coordinata
# precisa è già di per sé un'indicazione di vincolo esatto. Questo però non
# lascia modo di esprimere una coordinata grezza "indicativa, non vincolante"
# (ruolo SHAPING) — da qui il marcatore "~". Mappatura concordata: ANCHOR e
# MANDATORY sono entrambi vincolanti (raccordo col GPX originale o tappa
# obbligata: in entrambi i casi il percorso finale DEVE passarci) → "!";
# SHAPING è indicativo → "~". Non ancora collegato al parsing di main.py
# (Stadio 1 è isolato dal resto dell'app; il collegamento è un'integrazione
# da fare in uno stadio successivo).
MANDATORY_MARKER = "!"
SHAPING_MARKER = "~"

WaypointRole = Literal["ANCHOR", "MANDATORY", "SHAPING"]


def role_marker_suffix(role: WaypointRole) -> str:
    """Marcatore applicato a un waypoint secondo la mappatura ruolo→marcatore
    concordata: "!" per ANCHOR/MANDATORY (vincolanti), "~" per SHAPING
    (indicativo, non vincolante)."""
    if role == "SHAPING":
        return SHAPING_MARKER
    return MANDATORY_MARKER


# ── Modello dati ───────────────────────────────────────────────────────────────
# extra="allow" ovunque: questo è uno Stadio 1 esplorativo, il test deve
# mostrare l'output COMPLETO e GREZZO del modello — un campo che il modello
# aggiunge di sua iniziativa (oltre lo schema minimo richiesto) non va perso
# silenziosamente da una validazione troppo rigida.

class PhysicalConstraints(BaseModel):
    model_config = ConfigDict(extra="allow")

    maximum_200m_grade_pct: float | None = None
    prefer_low_traffic: bool | None = None
    avoid_historic_centres: bool | None = None
    remove_spurs: bool | None = None


class EditConstraints(BaseModel):
    model_config = ConfigDict(extra="allow")

    target_distance_km: float | None = None
    distance_tolerance_km: float | None = None
    preserve_before: str | None = None
    preserve_after: str | None = None
    mandatory_roads: list[str] = Field(default_factory=list)
    mandatory_places: list[str] = Field(default_factory=list)
    optional_places: list[str] = Field(default_factory=list)
    avoid_roads: list[str] = Field(default_factory=list)
    constraints: PhysicalConstraints = Field(default_factory=PhysicalConstraints)


class AlternativeDecision(BaseModel):
    """
    Decisione motivata su una scelta esplicita tra alternative presente nel
    testo utente (es. "Via X" vs "Via Y" per raggiungere lo stesso punto).
    has_alternative=False (con gli altri campi a None) quando il testo non
    presenta nessuna alternativa esplicita da decidere.
    """
    model_config = ConfigDict(extra="allow")

    has_alternative: bool = False
    option_a_id: str | None = None
    option_a_description: str | None = None
    option_b_id: str | None = None
    option_b_description: str | None = None
    criteria: list[str] = Field(default_factory=list)
    choice: str | None = None
    rationale: str | None = None


class GeocodeCandidate(BaseModel):
    """Un risultato Nominatim grezzo trovato dal motore fuzzy — nessuna scelta
    del "migliore" a questo livello, solo l'insieme completo per ispezione."""
    model_config = ConfigDict(extra="allow")

    display_name: str
    lat: float
    lon: float
    place_rank: int | None = None
    class_: str | None = None
    type_: str | None = None
    matched_variant: str | None = None


class EditWaypoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    # ── Prodotti dall'Intent Parser (AI) ──
    role: WaypointRole
    name: str
    explicit_context: list[str] = Field(default_factory=list)
    inferred_context: list[str] = Field(default_factory=list)
    notes: str | None = None
    # Solo per ANCHOR che descrivono una transizione tra vie (es. "lascia Via
    # Brecciata verso Montemarciano"): leaving_road = il nome della via che
    # il tracciato ORIGINALE abbandona in quel punto; toward_place = il
    # riferimento di direzione che l'utente ha dato (usato per la verifica
    # di direzione via map-matching, non per la geocodifica del punto
    # stesso). None per ANCHOR che non descrivono questo pattern, e per
    # MANDATORY/SHAPING (mai popolati).
    leaving_road: str | None = None
    toward_place: str | None = None
    # Da dove viene leaving_road, per trasparenza/misurazione — "first_pass"
    # (l'Intent Parser lo ha popolato al primo tentativo), "followup" (rete
    # di sicurezza: un secondo prompt mirato lo ha estratto dopo che il primo
    # passaggio lo aveva lasciato null), None (mai popolato — o non
    # applicabile, o non individuato nemmeno dalla rete di sicurezza).
    leaving_road_source: str | None = None

    # Pattern OPPOSTO (raccordo/arrivo, non distacco): per ANCHOR che
    # descrivono il tracciato NUOVO che si ricongiunge al percorso originale
    # su una via nominata (es. "raccordarsi sulla SP13"). rejoin_road = il
    # nome/riferimento di percorrenza della via di raccordo ESATTAMENTE come
    # scritto nel testo; rejoin_reference_place = il riferimento geografico
    # dato dall'utente per situare il punto (es. "il Grottino" — NON il nome
    # della via stessa, usato come centro di ricerca approssimativo per la
    # finestra locale). Mutuamente esclusivo con leaving_road in pratica (un
    # ANCHOR descrive o un distacco o un raccordo, raramente entrambi).
    rejoin_road: str | None = None
    rejoin_reference_place: str | None = None
    rejoin_road_source: str | None = None  # stessa semantica di leaving_road_source

    # ── Popolati dopo, dalla pipeline di risoluzione di questo modulo (mai
    # dal modello). Per un ANCHOR con leaving_road risolto via map-matching
    # (map_matcher.py — un punto preciso sul GPX originale, non una
    # geocodifica isolata): anchor_match popolato, candidates lasciato
    # vuoto (non è geocodifica). Per tutti gli altri waypoint (o un ANCHOR
    # il cui map-matching non risolve): candidates/variants_* popolati dal
    # motore fuzzy (_resolve_waypoint_candidates), nessuna scelta
    # automatica del "migliore" tra candidati multipli.
    marker: str | None = None
    variants_tried: list[str] = Field(default_factory=list)
    variants_with_hits: list[str] = Field(default_factory=list)
    candidates: list[GeocodeCandidate] = Field(default_factory=list)
    anchor_match: dict | None = None


class IntentParseResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    decision: AlternativeDecision
    constraints: EditConstraints
    waypoints: list[EditWaypoint] = Field(default_factory=list)


# ── Fatti oggettivi sul GPX originale (contesto per il prompt + viewbox) ──────

_VIEWBOX_MARGIN_KM = 12.0  # tarabile — margine attorno al bounding box del GPX


def _gpx_viewbox(lats: list[float], lons: list[float], margin_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) — bounding box del GPX allargato di margin_km
    su ogni lato. 1° di latitudine ≈ 111 km ovunque; 1° di longitudine si
    restringe con cos(latitudine media), da qui la conversione separata."""
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    mean_lat = (min_lat + max_lat) / 2
    dlat = margin_km / 111.0
    dlon = margin_km / (111.0 * max(0.1, math.cos(math.radians(mean_lat))))
    return (min_lat - dlat, min_lon - dlon, max_lat + dlat, max_lon + dlon)


def _gpx_quick_facts(gpx_path: str, viewbox_margin_km: float = _VIEWBOX_MARGIN_KM) -> dict:
    """
    Estrae dal GPX originale solo i fatti utili a dare contesto al modello
    (distanza/dislivello attuali, punti di partenza/arrivo), il viewbox per
    ancorare geograficamente la geocodifica dei waypoint (margine largo,
    12km di default) e i dati per il map-matching degli ANCHOR (i punti
    grezzi del tracciato + il bbox STRETTO, nessun margine largo — quello
    serve solo per la ricerca testuale, il map-matching lavora già dentro
    l'area vera del GPX + un margine di poche centinaia di metri gestito da
    map_matcher.build_area_graph). Non l'analisi completa di
    gpx_analyzer.analyze_gpx (climbs/out-and-back), non necessaria per
    interpretare l'intento.
    """
    with open(gpx_path, "r", encoding="utf-8") as f:
        gpx = gpxpy.parse(f)

    points = [p for t in gpx.tracks for s in t.segments for p in s.points]
    if not points:
        raise ValueError("Il file GPX non contiene punti traccia")

    distance_km = round(gpx.length_2d() / 1000, 2)

    cum_m = [0.0] * len(points)
    for i in range(1, len(points)):
        cum_m[i] = cum_m[i - 1] + (points[i - 1].distance_2d(points[i]) or 0.0)

    _, smoothed = smooth_elevations(cum_m, [p.elevation for p in points])
    elevation_gain_m = round(sum_uphill_downhill(smoothed)[0], 1) if smoothed else None

    lats = [p.latitude for p in points]
    lons = [p.longitude for p in points]

    return {
        "distance_km": distance_km,
        "elevation_gain_m": elevation_gain_m,
        "num_points": len(points),
        "start": (points[0].latitude, points[0].longitude),
        "end": (points[-1].latitude, points[-1].longitude),
        "viewbox": _gpx_viewbox(lats, lons, viewbox_margin_km),
        "viewbox_margin_km": viewbox_margin_km,
        "tight_bbox": (min(lats), min(lons), max(lats), max(lons)),
        "points": [(p.latitude, p.longitude) for p in points],
    }


# ── Prompt ─────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """Sei un assistente esperto di cicloturismo e toponomastica stradale italiana.

Questo è lo STADIO 1 di 4 di una funzionalità di modifica guidata di un percorso GPX
esistente. Il tuo compito è SOLO interpretare l'intento dell'utente — non generare un
percorso reale, non chiamare servizi di routing, non stimare con precisione km/dislivello
del risultato finale: quello accade negli stadi successivi (Stadio 2, via BRouter, con
dati di pendenza/traffico reali che potrebbero confermare o ribaltare qualunque tua stima
qualitativa di questo stadio).

Ricevi: (a) alcuni fatti oggettivi sul GPX originale (distanza, dislivello, punto di
partenza), (b) istruzioni testuali scritte in linguaggio naturale dall'utente su come
modificarlo. Il tuo output ha SEMPRE tre parti:

1. "decision" — DECISIONE SU EVENTUALI ALTERNATIVE ESPLICITE
   Se il testo presenta una scelta esplicita tra due (o più) alternative per raggiungere
   lo stesso punto (es. opzioni etichettate "3a"/"3b" o simili), analizzale USANDO SOLO i
   criteri che l'utente stesso ha indicato nel testo — mai criteri che ti inventi tu. Se
   l'utente non fornisce dati oggettivi di pendenza/traffico/distanza per ciascuna opzione,
   usa la tua conoscenza generale della zona/toponomastica come GIUDIZIO INFORMATO, ma
   dichiaralo esplicitamente in "rationale" come stima qualitativa preliminare, non un
   fatto certo — non hai ancora dati di routing reali.
   Se il testo NON presenta nessuna alternativa esplicita da decidere, imposta
   has_alternative=false e lascia gli altri campi della decisione a null.

2. "constraints" — STRUTTURA DEI VINCOLI
   - target_distance_km / distance_tolerance_km: se l'utente indica un chilometraggio
     desiderato (anche approssimativo, "circa X km") e/o una tolleranza.
   - preserve_before / preserve_after: descrizioni testuali (NON coordinate) dei due punti
     del percorso ORIGINALE dove inizia e finisce la porzione da sostituire — abbastanza
     specifiche da poter essere ritrovate su una mappa (es. "l'incrocio tra via Brecciate
     e la deviazione verso Montemarciano, a Montemarciano Marina", non solo "inizio
     modifica").
   - mandatory_roads / mandatory_places: strade e luoghi che il testo indica come tappe
     obbligate del nuovo tragitto.
   - optional_places: riferimenti geografici menzionati solo come contesto/orientamento,
     non tappe obbligate.
   - avoid_roads: strade che il testo indica esplicitamente di evitare.
   - constraints (nidificato): vincoli fisici/qualitativi impliciti o espliciti nel testo
     (es. maximum_200m_grade_pct se l'utente parla di pendenze massime accettabili,
     prefer_low_traffic, avoid_historic_centres, remove_spurs). Includi solo quelli che il
     testo giustifica, anche se non esplicitamente nominati con questi campi — se il testo
     implica altri vincoli utili non elencati qui, aggiungili con un nome descrittivo.

3. "waypoints" — LISTA ORDINATA DI PUNTI GEOGRAFICI, ciascuno con un ruolo:
   - ANCHOR: punto di raccordo con il percorso originale (inizio e fine della porzione
     modificata — corrispondono concettualmente a preserve_before/preserve_after). Quasi
     sempre almeno due: uno dove la modifica si stacca dal percorso originale, uno dove vi
     si riallaccia.
   - MANDATORY: il percorso finale DEVE attraversare esattamente questo punto — l'utente
     lo ha specificato come tappa obbligata (strada o luogo nominato esplicitamente come
     parte del tragitto voluto). Diventa una coordinata grezza vincolante (nelle regole
     esistenti dell'app, ogni coordinata grezza è SEMPRE mandatory).
   - SHAPING: riferimento indicativo/direzionale, utile per orientare il routing ma NON
     vincolante (es. "verso X" quando X è solo un'indicazione di direzione generale, non
     una tappa che il percorso deve toccare esattamente).

   Per ciascun waypoint fornisci:
   - "name": il nome proprio da cercare, il più NUDO possibile (es. "Via Alberici", non
     "Via Alberici, Monte San Vito" — il comune va in inferred_context, non qui).
   - "explicit_context": SOLO termini che l'UTENTE ha scritto letteralmente nel testo per
     situare questo luogo (es. se il testo dice "il Grottino sulla SP13", "SP13" è
     explicit_context perché l'utente lo ha scritto). Lista vuota se l'utente non ha
     fornito nessun contesto aggiuntivo oltre al nome stesso.
   - "inferred_context": qualificatori che AGGIUNGI TU dalla tua conoscenza (comune,
     provincia, frazione, regione) per aiutare a situare il luogo — MAI scritti
     dall'utente. Questi servono solo da riferimento informativo per chi legge l'output:
     NON verranno usati come vincolo rigido nella query di geocodifica (un comune inferito
     e sbagliato farebbe fallire la ricerca per intero invece di essere ignorato — motivo
     per cui va tenuto separato). Riportali comunque, sono utili come informazione.
   - "leaving_road" / "toward_place" (per OGNI ANCHOR, valutazione OBBLIGATORIA — non
     lasciarli null per pigrizia o per default): per CIASCUN ANCHOR, prima di scrivere la
     risposta chiediti esplicitamente: "il testo descrive che il tracciato ORIGINALE resta
     su una strada X fino a questo punto, e qui — invece di continuare/girare come farebbe
     normalmente verso una direzione Y — il nuovo percorso fa qualcosa di diverso?" Questo
     pattern può comparire anche senza le parole esatte "invece di" (es. "arrivati a P
     percorrendo X, invece di girare verso Y, continuare su X" descrive lo stesso pattern
     su X, non su Y). Se la risposta è SÌ:
       - leaving_road = il nome della strada X ESATTAMENTE come scritto nel testo — la
         strada che il tracciato ORIGINALE lascia in questo punto (non quella nuova).
       - toward_place = il riferimento di direzione Y che il tracciato ORIGINALE seguirebbe
         SENZA la modifica (serve a verificare, sul tracciato originale, di aver trovato il
         punto giusto — NON è la nuova direzione scelta dall'utente, che può essere
         completamente diversa).
     Usa null per ENTRAMBI i campi SOLO dopo aver controllato esplicitamente e concluso che
     il testo non nomina, per QUESTO specifico ANCHOR, né una strada da abbandonare né un
     riferimento di direzione — tipicamente il caso di un ANCHOR che descrive solo un
     raccordo/arrivo (es. "ci si ricongiunge al percorso originale in P"), dove il testo non
     dice quale strada il tracciato originale lasciasse per arrivare a P. null è un esito
     legittimo in quel caso, non un errore — ma dev'essere una conclusione esplicita, non
     l'assenza di uno sguardo al pattern.

     Esempio (pattern generale, diverso dal caso che analizzerai):
     Testo: "Segui la SP361 fino a Cantiano; lì, invece di proseguire dritto verso Gubbio,
     svolta per Cagli e prosegui su quella provinciale fino a ricongiungerti al percorso
     originale nei pressi di Acqualagna."
     → ANCHOR di distacco (a Cantiano): "leaving_road": "SP361", "toward_place": "Gubbio"
       (SP361 è la strada che il tracciato ORIGINALE lascia a Cantiano; Gubbio è la
       direzione che il tracciato ORIGINALE avrebbe preso proseguendo dritto — NON Cagli,
       che è la nuova direzione scelta dall'utente).

   - "rejoin_road" / "rejoin_reference_place" (per OGNI ANCHOR, stessa valutazione OBBLIGATORIA
     del pattern sopra — non è un ripiego da provare solo se leaving_road è null, è un pattern
     SEPARATO e altrettanto comune, spesso il PIÙ comune: ogni modifica deve ricongiungersi al
     percorso originale da qualche parte). Chiediti: "il testo descrive che il NUOVO tracciato
     si ricongiunge/torna/raccorda al percorso originale su una strada nominata, vicino a un
     riferimento geografico?" A differenza del distacco, qui NON c'è una transizione da
     descrivere — il tracciato originale è semplicemente già su quella strada in quella zona.
     Se la risposta è SÌ:
       - rejoin_road = il nome o riferimento di percorrenza della strada di raccordo
         ESATTAMENTE come scritto nel testo (es. "SP13", anche se è un numero di percorrenza
         e non un nome proprio — è normale ed è l'informazione più utile qui).
       - rejoin_reference_place = il riferimento geografico che l'utente ha dato per situare
         il punto (es. "il Grottino") — NON il nome della strada stessa, serve a restringere
         DOVE lungo quella strada cercare, non a identificarla.
     Usa null per entrambi SOLO dopo aver controllato esplicitamente che il testo non descrive
     questo pattern per QUESTO ANCHOR (es. se descrive invece un distacco, o nessuno dei due).

     Continuando l'esempio sopra — "...ricongiungerti al percorso originale nei pressi di
     Acqualagna, sulla SP3": → ANCHOR di raccordo (nei pressi di Acqualagna): "rejoin_road":
     "SP3", "rejoin_reference_place": "Acqualagna" (leaving_road/toward_place restano null per
     QUESTO ANCHOR: il testo non descrive un distacco qui, descrive un raccordo).
   NON inventare mai un luogo o una strada non menzionati o chiaramente implicati dal testo.

Rispondi in italiano per tutti i campi testuali (rationale, notes, description, ecc.).
Rispondi SOLO con JSON valido, nello schema seguente (includi sempre tutti i campi; usa
null/[] dove non applicabile — non omettere chiavi):

{
  "decision": {
    "has_alternative": true,
    "option_a_id": "3a",
    "option_a_description": "...",
    "option_b_id": "3b",
    "option_b_description": "...",
    "criteria": ["criterio 1 citato dall'utente", "criterio 2", "..."],
    "choice": "3a",
    "rationale": "motivazione in italiano, esplicitando se è una stima qualitativa"
  },
  "constraints": {
    "target_distance_km": 45,
    "distance_tolerance_km": 2,
    "preserve_before": "descrizione testuale del punto di inizio modifica",
    "preserve_after": "descrizione testuale del punto di fine modifica",
    "mandatory_roads": ["..."],
    "mandatory_places": ["..."],
    "optional_places": ["..."],
    "avoid_roads": ["..."],
    "constraints": {
      "maximum_200m_grade_pct": 13,
      "prefer_low_traffic": true,
      "avoid_historic_centres": true,
      "remove_spurs": true
    }
  },
  "waypoints": [
    {"role": "ANCHOR", "name": "...", "explicit_context": [], "inferred_context": ["..."], "notes": "...",
     "leaving_road": "Via Brecciate", "toward_place": "Montemarciano",
     "rejoin_road": null, "rejoin_reference_place": null},
    {"role": "ANCHOR", "name": "...", "explicit_context": [], "inferred_context": ["..."], "notes": "...",
     "leaving_road": null, "toward_place": null,
     "rejoin_road": "SP3", "rejoin_reference_place": "Acqualagna"},
    {"role": "MANDATORY", "name": "...", "explicit_context": ["..."], "inferred_context": ["..."], "notes": "...",
     "leaving_road": null, "toward_place": null, "rejoin_road": null, "rejoin_reference_place": null},
    {"role": "SHAPING", "name": "...", "explicit_context": [], "inferred_context": ["..."], "notes": "...",
     "leaving_road": null, "toward_place": null, "rejoin_road": null, "rejoin_reference_place": null}
  ]
}"""


def _build_user_prompt(facts: dict, instructions: str) -> str:
    return (
        "GPX originale — fatti oggettivi:\n"
        f"- Distanza attuale: {facts['distance_km']} km\n"
        f"- Dislivello positivo attuale: {facts['elevation_gain_m']} m\n"
        f"- Punto di partenza: {facts['start'][0]:.5f}, {facts['start'][1]:.5f}\n"
        f"- Punto di arrivo: {facts['end'][0]:.5f}, {facts['end'][1]:.5f}\n"
        f"- Numero di punti traccia: {facts['num_points']}\n\n"
        "Istruzioni testuali dell'utente (verbatim):\n"
        + instructions.strip()
    )


def _extract_json(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    return m.group(1) if m else text


# ── Geocodifica waypoint: motore fuzzy vincolato geograficamente ─────────────

def _resolve_waypoint_candidates(wp: EditWaypoint, viewbox: tuple[float, float, float, float]) -> None:
    """
    Geocodifica wp.name col motore fuzzy vincolato geograficamente
    (geocoding_agent.fuzzy_geocode_in_area, già validato in isolamento) —
    elenca TUTTI i candidati trovati da qualunque variante testuale generata,
    nessuna scelta automatica del "migliore" (disambiguazione rimandata a un
    passo successivo). Applica anche la mappatura ruolo→marcatore concordata.

    Nota: explicit_context/inferred_context restano nell'output come
    informazione (prodotti dal modello), ma NON entrano nella query di
    geocodifica — solo "name" viene passato al motore fuzzy, che genera le
    proprie varianti (articolo/prefisso/desinenza) a partire da quello.
    """
    wp.marker = role_marker_suffix(wp.role)
    try:
        result = fuzzy_geocode_in_area(wp.name, gpx_bbox=viewbox)
    except Exception as exc:
        log.warning("Geocodifica fuzzy fallita per waypoint %r: %s", wp.name, exc)
        return
    wp.variants_tried = result["variants_tried"]
    wp.variants_with_hits = result["variants_with_hits"]
    wp.candidates = [GeocodeCandidate(**c) for c in result["candidates"]]


# ── ANCHOR: map-matching sul GPX originale (invece della geocodifica singola) ─

def _resolve_anchor_via_mapmatching(
    wp: EditWaypoint,
    gpx_points: list[tuple[float, float]],
    viewbox: tuple[float, float, float, float],
    area_graph: "map_matcher.AreaGraph",
) -> bool:
    """
    Prova a risolvere un ANCHOR con leaving_road valorizzato via map-matching
    (map_matcher.resolve_anchor) invece della geocodifica singola — il
    risultato è un punto preciso sul GPX ORIGINALE, non una coordinata
    geocodificata isolata. Ritorna True se risolto (wp.anchor_match
    popolato), False se non risolvibile (il chiamante ricade allora sulla
    geocodifica fuzzy normale, invariata).

    Il centro approssimativo per la finestra locale viene dalla geocodifica
    fuzzy di wp.name (stesso motore di _resolve_waypoint_candidates): se
    restituisce più candidati (es. più segmenti della stessa via in punti
    diversi del tracciato), li prova in ordine finché uno produce una
    transizione valida — un tentativo sequenziale sui candidati già
    raccolti, non uno scoring/disambiguazione nuova.
    """
    try:
        geocoded = fuzzy_geocode_in_area(wp.leaving_road, gpx_bbox=viewbox)
    except Exception as exc:
        log.warning("Geocodifica del centro approssimativo fallita per ANCHOR %r: %s", wp.name, exc)
        return False
    candidates = geocoded["candidates"]
    if not candidates:
        return False

    target = None
    if wp.toward_place:
        try:
            target_geocoded = fuzzy_geocode_in_area(wp.toward_place, gpx_bbox=viewbox)
        except Exception as exc:
            log.warning("Geocodifica di toward_place=%r fallita: %s", wp.toward_place, exc)
            target_geocoded = None
        if target_geocoded and target_geocoded["candidates"]:
            best = target_geocoded["candidates"][0]
            target = (best["lat"], best["lon"])

    last_result: dict | None = None
    for c in candidates:
        center = (c["lat"], c["lon"])
        result = map_matcher.resolve_anchor(area_graph, gpx_points, center, wp.leaving_road, target=target)
        last_result = result
        if result.get("resolved"):
            wp.anchor_match = result
            return True

    wp.anchor_match = last_result  # ultimo tentativo (con errore) per trasparenza, anche se non risolto
    return False


def _resolve_rejoin_via_mapmatching(
    wp: EditWaypoint,
    gpx_points: list[tuple[float, float]],
    viewbox: tuple[float, float, float, float],
    area_graph: "map_matcher.AreaGraph",
) -> bool:
    """
    Prova a risolvere un ANCHOR con rejoin_road valorizzato via map-matching
    (map_matcher.resolve_rejoin) — pattern opposto al distacco (nessuna
    transizione da cercare, il tracciato originale è già sulla via giusta,
    basta confermare dove). Ritorna True se risolto (wp.anchor_match
    popolato), False se non risolvibile (il chiamante ricade sulla
    geocodifica fuzzy normale, invariata).

    Il centro approssimativo per la finestra locale viene dalla geocodifica
    fuzzy di rejoin_reference_place (il riferimento geografico, es. "il
    Grottino" — non rejoin_road stesso, che è spesso un numero di
    percorrenza poco utile da geocodificare direttamente). Se manca
    rejoin_reference_place, ricade su wp.name. Stesso tentativo sequenziale
    sui candidati geocodificati già usato per il distacco — non è
    disambiguazione/scoring nuovo.
    """
    reference_text = wp.rejoin_reference_place or wp.name
    try:
        geocoded = fuzzy_geocode_in_area(reference_text, gpx_bbox=viewbox)
    except Exception as exc:
        log.warning("Geocodifica del riferimento di raccordo fallita per ANCHOR %r: %s", wp.name, exc)
        return False
    candidates = geocoded["candidates"]
    if not candidates:
        return False

    last_result: dict | None = None
    for c in candidates:
        center = (c["lat"], c["lon"])
        result = map_matcher.resolve_rejoin(area_graph, gpx_points, center, wp.rejoin_road)
        last_result = result
        if result.get("resolved"):
            wp.anchor_match = result
            return True

    wp.anchor_match = last_result  # ultimo tentativo (con errore) per trasparenza, anche se non risolto
    return False


# ── Rete di sicurezza: secondo passaggio mirato per ANCHOR rimasti senza
# pattern (né distacco né raccordo) dopo la prima estrazione ─────────────────
# Il primo passaggio (un solo prompt grande, che produce decision+constraints
# +waypoints insieme) può non applicare in modo affidabile i pattern
# leaving_road/toward_place (distacco) e rejoin_road/rejoin_reference_place
# (raccordo) su OGNI ANCHOR pertinente — osservato su run ripetute. Prima di
# ricadere sulla geocodifica standard, un secondo prompt molto più piccolo e
# mirato (nessuno schema decision/constraints, solo la domanda specifica per
# QUESTO waypoint, su ENTRAMBI i pattern) prova a estrarlo isolatamente.

class _AnchorFollowup(BaseModel):
    model_config = ConfigDict(extra="allow")
    leaving_road: str | None = None
    toward_place: str | None = None
    rejoin_road: str | None = None
    rejoin_reference_place: str | None = None


_ANCHOR_FOLLOWUP_SYSTEM_PROMPT = """Sei un assistente che estrae con precisione un singolo dettaglio da un testo.

Ti vengono date le istruzioni complete per modificare un percorso GPX e UN singolo waypoint
di raccordo (ANCHOR) già identificato in una prima analisi (nome, eventuali note e contesto
già estratti).

Un ANCHOR descrive uno di due pattern possibili rispetto al tracciato ORIGINALE — valutali
ENTRAMBI, in questo ordine, per QUESTO specifico waypoint:

1. DISTACCO: il tracciato ORIGINALE resta su una strada X fino a questo punto, e qui — invece
   di continuare/girare verso una direzione Y come farebbe normalmente — il nuovo percorso fa
   qualcosa di diverso. Il pattern può comparire senza le parole esatte "invece di". Se SÌ:
   leaving_road = la strada X abbandonata (nome esatto come scritto nel testo), toward_place =
   la direzione Y che il tracciato ORIGINALE avrebbe seguito SENZA la modifica (non la nuova
   direzione scelta dall'utente).

2. RACCORDO: il NUOVO tracciato si ricongiunge/torna/raccorda al percorso originale su una
   strada nominata, vicino a un riferimento geografico — qui non c'è transizione da descrivere,
   il tracciato originale è semplicemente già su quella strada in quella zona. Se SÌ:
   rejoin_road = il nome/riferimento di percorrenza della strada di raccordo esattamente come
   scritto nel testo (anche un numero di percorrenza come "SP13" va bene), rejoin_reference_place
   = il riferimento geografico dato per situare il punto (es. "il Grottino" — non il nome della
   strada stessa).

Al massimo UNO dei due pattern si applica per lo stesso ANCHOR. Se NESSUNO dei due si applica
(es. il waypoint descrive solo un punto generico senza dettagli sufficienti) rispondi con TUTTI
e quattro i campi null — non inventare una strada/direzione/riferimento che il testo non
menziona per QUESTO waypoint specifico. null su tutti e quattro è un esito legittimo, non un
fallimento.

Rispondi SOLO con JSON valido, niente altro: {"leaving_road": "..." | null, "toward_place": "..." | null, "rejoin_road": "..." | null, "rejoin_reference_place": "..." | null}"""


def _followup_anchor_pattern(instructions: str, wp: EditWaypoint) -> _AnchorFollowup:
    """
    Rete di sicurezza secondaria (vedi nota sopra la classe _AnchorFollowup):
    un prompt piccolo e mirato su UN SOLO waypoint ANCHOR, invocato solo se
    il primo passaggio non ha riconosciuto né il pattern di distacco né
    quello di raccordo per quel waypoint. Ritorna un _AnchorFollowup con
    tutti i campi a None se anche questo passaggio non trova nessun pattern
    (o fallisce per un errore) — un ANCHOR generico, senza né una strada da
    abbandonare né un riferimento di raccordo descritti nel testo, è un
    esito legittimo, non un errore: il chiamante ricade sulla geocodifica
    standard in entrambi i casi.
    """
    user_prompt = (
        "Istruzioni complete dell'utente (verbatim):\n" + instructions.strip() +
        f"\n\nWaypoint ANCHOR da valutare — nome: {wp.name!r}, note: {wp.notes!r}, "
        f"contesto esplicito già estratto per questo waypoint: {wp.explicit_context!r}"
    )
    try:
        text = ai_client.generate_json(_ANCHOR_FOLLOWUP_SYSTEM_PROMPT, user_prompt, max_tokens=300)
        raw = json.loads(_extract_json(text))
        return _AnchorFollowup.model_validate(raw)
    except Exception as exc:
        log.warning("Rete di sicurezza pattern ANCHOR fallita per waypoint %r: %s", wp.name, exc)
        return _AnchorFollowup()


# ── Entry point ─────────────────────────────────────────────────────────────────

def parse_edit_intent(gpx_path: str, instructions: str) -> dict:
    """
    Stadio 1 — Intent Parser.

    gpx_path     : percorso al GPX originale da modificare.
    instructions : istruzioni testuali libere dell'utente.

    Ritorna un dict con:
      "gpx_facts"        : fatti oggettivi sul GPX (incluso il viewbox usato per la
                           geocodifica) passati al modello come contesto
      "raw_response"     : testo grezzo restituito dal modello, prima di qualunque
                            parsing/validazione — per ispezione diretta
      "result"           : IntentParseResult validato (i waypoint hanno marker/candidates
                            già popolati dal motore fuzzy), None se la validazione
                            Pydantic fallisce
      "validation_error" : messaggio di errore se la validazione fallisce, altrimenti None
    """
    ai_client.check_api_key()

    facts = _gpx_quick_facts(gpx_path)
    user_prompt = _build_user_prompt(facts, instructions)

    text = ai_client.generate_json(_SYSTEM_PROMPT, user_prompt, max_tokens=4000)
    if not text or not text.strip():
        raise RuntimeError("Intent Parser: nessuna risposta dal modello.")

    output: dict = {
        "gpx_facts": facts,
        "raw_response": text,
        "result": None,
        "validation_error": None,
    }

    try:
        raw = json.loads(_extract_json(text))
        validated = IntentParseResult.model_validate(raw)
    except Exception as exc:
        output["validation_error"] = f"{type(exc).__name__}: {exc}"
        return output

    # Rete di sicurezza: il primo passaggio (un solo prompt che produce
    # decision+constraints+waypoints insieme) può non applicare in modo
    # affidabile i pattern di distacco/raccordo su ogni ANCHOR pertinente
    # (osservato su run ripetute) — per ogni ANCHOR rimasto senza NESSUNO dei
    # due pattern, un secondo prompt piccolo e mirato ci riprova isolato
    # prima di ricadere sulla geocodifica standard.
    for wp in validated.waypoints:
        if wp.role != "ANCHOR":
            continue
        if wp.leaving_road:
            wp.leaving_road_source = "first_pass"
            continue
        if wp.rejoin_road:
            wp.rejoin_road_source = "first_pass"
            continue
        followup = _followup_anchor_pattern(instructions, wp)
        if followup.leaving_road:
            wp.leaving_road = followup.leaving_road
            wp.toward_place = followup.toward_place
            wp.leaving_road_source = "followup"
        elif followup.rejoin_road:
            wp.rejoin_road = followup.rejoin_road
            wp.rejoin_reference_place = followup.rejoin_reference_place
            wp.rejoin_road_source = "followup"

    # Grafo stradale costruito al più una volta per chiamata (una query
    # Overpass, ~7s misurati) — riusato da OGNI ANCHOR con un pattern
    # riconosciuto (distacco O raccordo) in questa run, mai ricostruito per
    # ciascuno. Costruito solo se serve davvero (nessun ANCHOR con un
    # pattern → nessuna chiamata Overpass).
    area_graph: "map_matcher.AreaGraph | None" = None
    needs_area_graph = any(
        wp.role == "ANCHOR" and (wp.leaving_road or wp.rejoin_road) for wp in validated.waypoints
    )
    if needs_area_graph:
        try:
            area_graph = map_matcher.build_area_graph(facts["tight_bbox"])
        except OverpassUnavailable as exc:
            log.warning("Area graph non disponibile (Overpass irraggiungibile): %s — ANCHOR ricadranno sulla geocodifica fuzzy.", exc)
            area_graph = None

    for wp in validated.waypoints:
        resolved_via_mapmatching = False
        if wp.role == "ANCHOR" and area_graph is not None:
            wp.marker = role_marker_suffix(wp.role)
            if wp.leaving_road:
                resolved_via_mapmatching = _resolve_anchor_via_mapmatching(
                    wp, facts["points"], facts["viewbox"], area_graph,
                )
            elif wp.rejoin_road:
                resolved_via_mapmatching = _resolve_rejoin_via_mapmatching(
                    wp, facts["points"], facts["viewbox"], area_graph,
                )
        if not resolved_via_mapmatching:
            _resolve_waypoint_candidates(wp, facts["viewbox"])

    output["result"] = validated
    return output
