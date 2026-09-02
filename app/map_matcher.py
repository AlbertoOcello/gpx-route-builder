"""
Map Matcher — associa punti di un GPX a vie OSM reali (Hidden Markov Model,
via leuvenmapmatching), per risolvere i waypoint di ruolo ANCHOR nel Route
Editor Agent (route_editor_agent.py).

Problema diverso dalla geocodifica (geocoding_agent.py, già risolta): un
ANCHOR non è "dove si trova questo luogo nel mondo", è "dove nel GPX
ORIGINALE SPECIFICO il tracciato lascia una via per andarne su un'altra"
(es. "il punto in cui il percorso attuale gira verso Montemarciano invece
di continuare su Via Brecciata").

Fattibilità validata (investigazione precedente, dati reali su
Panoramica_Alta_D2-Pre.gpx, 36km/6103 punti):
  - Una singola query Overpass sul bbox del GPX (+ margine) basta per
    l'intera area — non serve incrementale (~108km²: 2608 way, 17762 nodi
    geometria, 7.4s, un solo tentativo).
  - Costruzione dell'InMemMap da quelle way: <1s.
  - Match locale (finestra di poche centinaia di punti attorno a un
    ANCHOR): <1s, precisione ~24m sul caso reale "Via Brecciata".
  - Match sull'INTERO tracciato (6103 punti): NON sempre completa con i
    parametri di default (fermato al 97%, 5914/6103) — per questo le
    funzioni qui operano SEMPRE su una finestra locale, mai sull'intero GPX.

Pensato per essere riusabile: la stessa AreaGraph/match_window serviranno
anche per la disambiguazione multi-segmento "Via Brecciata"/"SP13" (prossimo
passo naturale, non ancora implementato — vedi route_editor_agent.py).
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable

from geopy.distance import geodesic
from leuvenmapmatching.map.inmem import InMemMap
from leuvenmapmatching.matcher.distance import DistanceMatcher

from area_resolver import query_overpass
from geocoding_agent import generate_fuzzy_variants

log = logging.getLogger(__name__)

_MATCH_ROAD_TYPES = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential|"
    "service|living_street|track|cycleway"
)
# Margine attorno al bbox richiesto per la query Overpass — misurato: un
# bbox di ~90km² (36km di GPX, nessun margine) + 300m -> ~108km², 2608 way,
# 7.4s in un'unica chiamata.
_GRAPH_MARGIN_KM_DEFAULT = 0.3

_WINDOW_RADIUS_M_DEFAULT = 500.0   # raggio attorno al centro approssimativo (es. da geocodifica)
# Il raccordo usa tipicamente un riferimento di LUOGO (es. "Grottino", una
# frazione) invece che il nome di una via — la geocodifica di un luogo è
# strutturalmente meno precisa rispetto alla via che effettivamente passa lì
# vicino (misurato sul caso reale: ~48m dalla via, ma ~1.9-2.1km dal centroide
# del luogo geocodificato) — raggio più ampio di resolve_anchor, che invece
# geocodifica direttamente il nome della via di distacco (più preciso).
_REJOIN_WINDOW_RADIUS_M_DEFAULT = 3000.0
_WINDOW_PADDING_POINTS = 30        # punti extra ai due lati della finestra individuata

_MATCHER_MAX_DIST = 50.0
_MATCHER_MAX_DIST_INIT = 25.0
_MATCHER_OBS_NOISE = 10.0

_DIRECTION_LOOKAHEAD_M_DEFAULT = 150.0
_DIRECTION_TOLERANCE_DEG_DEFAULT = 90.0


# ── Grafo stradale di un'area (una query Overpass, riusabile) ────────────────

@dataclass
class AreaGraph:
    """
    Grafo stradale di un'area, pronto per il map-matching. Costruito con UNA
    query Overpass (build_area_graph) e riusabile per più finestre/ANCHOR
    nella stessa area (stesso GPX) senza ripetere la query — l'ho misurato
    a ~7s, non è un costo da pagare per ogni ANCHOR.
    """
    map_con: InMemMap
    edge_to_way: dict[tuple[int, int], dict]
    bbox: tuple[float, float, float, float]
    n_ways: int
    build_seconds: float


def _bbox_with_margin(bbox: tuple[float, float, float, float], margin_km: float) -> tuple[float, float, float, float]:
    south, west, north, east = bbox
    mean_lat = (south + north) / 2
    dlat = margin_km / 111.0
    dlon = margin_km / (111.0 * max(0.1, math.cos(math.radians(mean_lat))))
    return (south - dlat, west - dlon, north + dlat, east + dlon)


def build_area_graph(
    bbox: tuple[float, float, float, float],
    margin_km: float = _GRAPH_MARGIN_KM_DEFAULT,
    road_types: str = _MATCH_ROAD_TYPES,
    on_progress: Callable[[str], None] | None = None,
) -> AreaGraph:
    """
    Scarica le vie OSM entro bbox (+margin_km) via Overpass — riusa
    area_resolver.query_overpass (stessa funzione di retry/mirror già in
    uso per lo snap-to-road del Builder, nessuna query duplicata) — e
    costruisce un InMemMap (leuvenmapmatching) con tutti i nodi/archi.

    bbox = (south, west, north, east), es. il bounding box del GPX originale
    (nessun margine largo tipo quello usato per la geocodifica — qui basta
    qualche centinaio di metri, la strada deve solo essere "abbastanza
    dentro" l'area del tracciato).

    Solleva area_resolver.OverpassUnavailable se il servizio non risponde
    dopo tutti i tentativi — si propaga al chiamante invariata.
    """
    t0 = time.perf_counter()

    south, west, north, east = _bbox_with_margin(bbox, margin_km)
    query = (
        f"[out:json][timeout:90];\n"
        f"(\n"
        f'  way["highway"~"^({road_types})$"]'
        f"({south:.6f},{west:.6f},{north:.6f},{east:.6f});\n"
        f");\n"
        f"out geom;\n"
    )
    ways = query_overpass(query, on_progress=on_progress)

    map_con = InMemMap("route_editor_area", use_latlon=True, use_rtree=True, index_edges=True)
    edge_to_way: dict[tuple[int, int], dict] = {}
    for way in ways:
        node_ids = way.get("nodes") or []
        geom = way.get("geometry") or []
        if len(node_ids) != len(geom) or len(node_ids) < 2:
            continue
        for nid, pt in zip(node_ids, geom):
            map_con.add_node(nid, (pt["lat"], pt["lon"]))
        oneway = (way.get("tags") or {}).get("oneway") == "yes"
        for a, b in zip(node_ids, node_ids[1:]):
            map_con.add_edge(a, b)
            edge_to_way[(a, b)] = way
            if not oneway:
                map_con.add_edge(b, a)
                edge_to_way[(b, a)] = way

    return AreaGraph(
        map_con=map_con, edge_to_way=edge_to_way, bbox=(south, west, north, east),
        n_ways=len(ways), build_seconds=time.perf_counter() - t0,
    )


# ── Matching di una finestra locale ───────────────────────────────────────────

@dataclass
class MatchedPoint:
    lat: float
    lon: float
    edge: tuple[int, int] | None
    way_name: str | None
    way_ref: str | None = None  # tag "ref" OSM (es. "SP13") — vedi find_rejoin_point


def match_window(
    area_graph: AreaGraph,
    points: list[tuple[float, float]],
    max_dist: float = _MATCHER_MAX_DIST,
    max_dist_init: float = _MATCHER_MAX_DIST_INIT,
    obs_noise: float = _MATCHER_OBS_NOISE,
) -> list[MatchedPoint]:
    """
    Map-matching (HMM, leuvenmapmatching.DistanceMatcher) di UNA FINESTRA
    LOCALE di punti — mai l'intero tracciato (vedi nota di fattibilità in
    testa al modulo: sull'intero GPX il match non sempre completa). Ogni
    punto restituito porta l'arco (nodo_a, nodo_b) del grafo a cui è stato
    agganciato, il nome via (tag "name") e il riferimento di percorrenza
    (tag "ref", es. "SP13" — una provinciale numerata spesso attraversa
    molti segmenti con "name" locale diverso mattonella per mattonella,
    "ref" è l'unico tag stabile lungo tutto il percorso; find_transition
    continua a usare solo way_name, invariato, way_ref serve solo a
    find_rejoin_point).

    Ritorna una lista lunga al più quanto `points` (può essere più corta se
    il matcher si ferma prima di raggiungere la fine della finestra).
    """
    if not points:
        return []
    matcher = DistanceMatcher(
        area_graph.map_con, max_dist=max_dist, max_dist_init=max_dist_init,
        min_prob_norm=0.0001, non_emitting_states=True, obs_noise=obs_noise,
    )
    states, _ = matcher.match(points)
    result = []
    for (lat, lon), edge in zip(points, states):
        way = None
        if edge:
            way = area_graph.edge_to_way.get(edge) or area_graph.edge_to_way.get((edge[1], edge[0]))
        tags = (way.get("tags") or {}) if way else {}
        result.append(MatchedPoint(lat=lat, lon=lon, edge=edge, way_name=tags.get("name"), way_ref=tags.get("ref")))
    return result


# ── Individuazione della transizione (l'ANCHOR) ───────────────────────────────

def _normalize_road_name(name: str) -> str:
    return name.strip().lower()


def _acceptable_spellings(road_name: str) -> set[str]:
    """
    Varianti di ortografia accettabili per `road_name` — riusa
    generate_fuzzy_variants (geocoding_agent.py, già validato) invece di
    un secondo normalizzatore: lo stesso motivo per cui serve lì (l'utente
    scrive "Via Brecciate", OSM tagga "Via Brecciata") si ripresenta qui
    identico, quando l'Intent Parser riporta il nome via come l'ha scritto
    l'utente invece della forma esatta OSM.
    """
    return {_normalize_road_name(v) for v in generate_fuzzy_variants(road_name)} | {_normalize_road_name(road_name)}


@dataclass
class AnchorTransition:
    index: int  # indice in `matched` (== nella finestra locale) del primo punto sulla nuova via
    from_name: str | None
    to_name: str | None
    anchor_lat: float
    anchor_lon: float
    interpolation: str  # "osm_node" (nodo di intersezione reale) | "trackpoint_midpoint"


def find_transition(
    area_graph: AreaGraph,
    matched: list[MatchedPoint],
    from_road: str,
) -> AnchorTransition | None:
    """
    Cerca, nella sequenza di vie agganciate, il primo punto in cui il
    tracciato lascia `from_road` (confrontato con tolleranza ortografica,
    vedi _acceptable_spellings) per un'altra via — quello è il candidato
    ANCHOR. Il punto di giunzione preciso è il nodo OSM condiviso tra
    l'arco precedente e quello successivo, se esiste (la vera intersezione
    stradale, interpolation="osm_node"); altrimenti il punto medio tra i
    due trackpoint del GPX a cavallo della transizione
    (interpolation="trackpoint_midpoint" — comunque più preciso di scegliere
    uno dei due trackpoint esistenti così com'è).

    Ritorna None se `from_road` non compare mai nella finestra, o non c'è
    mai una transizione successiva a un'altra via (non-None).
    """
    acceptable = _acceptable_spellings(from_road)
    on_target = False
    for i, mp in enumerate(matched):
        if mp.way_name and _normalize_road_name(mp.way_name) in acceptable:
            on_target = True
            continue
        if on_target and mp.way_name:
            prev = matched[i - 1]
            anchor_lat, anchor_lon, method = (prev.lat + mp.lat) / 2, (prev.lon + mp.lon) / 2, "trackpoint_midpoint"
            if prev.edge and mp.edge:
                shared = set(prev.edge) & set(mp.edge)
                if shared:
                    node_id = next(iter(shared))
                    coords = area_graph.map_con.node_coordinates(node_id)
                    if coords:
                        anchor_lat, anchor_lon, method = coords[0], coords[1], "osm_node"
            return AnchorTransition(
                index=i, from_name=prev.way_name, to_name=mp.way_name,
                anchor_lat=anchor_lat, anchor_lon=anchor_lon, interpolation=method,
            )
    return None


# ── Individuazione del punto di raccordo (pattern opposto al distacco) ───────
# Il distacco (find_transition, sopra) cerca una TRANSIZIONE nella sequenza
# del tracciato originale (strada A → strada B). Il raccordo cerca invece un
# punto di raccordo GEOMETRICAMENTE sensato — non più "dove il nome/ref
# combacia esattamente", da quando un caso reale (Grottino/SP13, indagine
# precedente) ha mostrato che il tracciato originale può oggettivamente
# percorrere una via diversa da quella nominata dall'utente (lì: SP2 invece
# di SP13, due strade fisicamente distinte a poche decine di metri l'una
# dall'altra) senza che questo sia un errore di ricerca — allargare la
# finestra o rincorrere il nome esatto non avrebbe aiutato, il tracciato
# semplicemente non passa di lì. Il nome/ref indicato dall'utente resta
# comunque utile: non più come filtro che scarta i punti che non
# corrispondono, ma come VERIFICA INFORMATIVA riportata sul risultato
# (road_name_mismatch), pronta per un'eventuale conferma utente allo Stadio 3
# (non costruito qui — solo la struttura dati).
#
# Considerate e scartate come base per la selezione geometrica: la ricerca
# esaustiva chiusura-based di _detect_spurs/_detect_out_and_back
# (gpx_analyzer.py) risolve un problema diverso (il tracciato che rivisita SE
# STESSO, un'andata/ritorno) — qui serve scegliere un punto vicino a un
# riferimento ESTERNO (il luogo geocodificato dall'utente), non un'altra
# porzione dello stesso tracciato; cut_range_in_gpx ("✂️ Cancella tratto") è
# interamente manuale (range scelto dall'utente via slider), nessuna
# selezione automatica da riusare. Stesso spirito (nessuna dipendenza nuova,
# solo geometria su punti già disponibili) riapplicato qui su misura.

@dataclass
class RejoinPoint:
    index: int  # indice nella finestra locale (in `window`) del punto scelto
    road_name: str | None  # via realmente agganciata in quel punto dal map-matching (None se non agganciato)
    anchor_lat: float
    anchor_lon: float
    distance_to_reference_m: float | None
    road_name_mismatch: dict | None  # {"expected": ..., "found": ...} se road_name non combacia col hint — mai bloccante


def _normalize_ref(ref: str) -> str:
    """Normalizzazione per i tag "ref" (es. "SP13"): oltre a lower/strip,
    rimuove anche gli spazi interni — "SP 13" e "SP13" sono la stessa
    provinciale, la differenza è solo di formattazione OSM/utente."""
    return ref.strip().lower().replace(" ", "")


def _u_turn_angle_deg(window: list[tuple[float, float]], index: int, span_m: float = 60.0) -> float:
    """
    Angolo (0°-180°) tra il bearing di arrivo a `window[index]` (dal punto
    ~span_m prima) e il bearing di partenza da lì (verso il punto ~span_m
    dopo) — vicino a 180° indica un'inversione a U proprio in quel punto
    (tipico di un punto interno a un'andata/ritorno, un cattivo candidato per
    un raccordo pulito); vicino a 0° indica prosecuzione dolce/rettilinea.
    Ritorna 0.0 (nessuna penalità) se la finestra è troppo corta da un lato
    per giudicare — meglio non scartare un candidato per mancanza di dati
    piuttosto che presumere un'inversione che potremmo non vedere.
    """
    n = len(window)
    back_idx, dist = index, 0.0
    while back_idx > 0 and dist < span_m:
        dist += geodesic(window[back_idx - 1], window[back_idx]).meters
        back_idx -= 1
    fwd_idx, dist = index, 0.0
    while fwd_idx < n - 1 and dist < span_m:
        dist += geodesic(window[fwd_idx], window[fwd_idx + 1]).meters
        fwd_idx += 1
    if back_idx == index or fwd_idx == index:
        return 0.0
    incoming = _bearing_deg(*window[back_idx], *window[index])
    outgoing = _bearing_deg(*window[index], *window[fwd_idx])
    return _angular_diff_deg(incoming, outgoing)


_REJOIN_U_TURN_REJECT_DEG = 120.0  # oltre questa soglia, il punto è scartato come "inversione a U"


def find_rejoin_point(
    area_graph: AreaGraph,
    window: list[tuple[float, float]],
    target_road_name: str | None = None,
    reference_point: tuple[float, float] | None = None,
) -> RejoinPoint | None:
    """
    Selezione GEOMETRICA (non più basata sul tag) del punto di raccordo più
    plausibile nella finestra: il punto più vicino a `reference_point` la cui
    percorrenza è coerente col resto del tracciato (nessuna inversione a U
    netta proprio lì, vedi _u_turn_angle_deg — un'inversione indicherebbe un
    punto interno a un'andata/ritorno, non un raccordo pulito). Essendo
    sempre un punto realmente presente in `window`, la continuità con il
    resto del tracciato originale è garantita per costruzione, non richiede
    un controllo separato.

    `target_road_name` (opzionale) è un HINT, non un filtro: dopo aver
    scelto il punto per via geometrica, il map-matching di quel punto
    (match_window, stessa funzione di find_transition — nessuna
    duplicazione) verifica SOLO in modo informativo se la via realmente
    agganciata lì corrisponde al nome/ref indicato (tolleranza ortografica
    _acceptable_spellings sul nome, _normalize_ref sul tag "ref" — stessa
    tolleranza già usata prima che questa funzione diventasse tag-first).
    Se non corrisponde, il punto è comunque accettato: road_name_mismatch
    riporta {"expected", "found"} per trasparenza (utile a un'eventuale
    conferma utente allo Stadio 3, non costruita qui). Se target_road_name è
    None, o se corrisponde, road_name_mismatch resta None.

    Ritorna None solo se `window` è vuota.
    """
    if not window:
        return None

    if reference_point is not None:
        ranked = sorted(range(len(window)), key=lambda i: geodesic(reference_point, window[i]).meters)
        index = next((i for i in ranked if _u_turn_angle_deg(window, i) < _REJOIN_U_TURN_REJECT_DEG), ranked[0])
        dist = geodesic(reference_point, window[index]).meters
    else:
        index, dist = 0, None

    lat, lon = window[index]

    matched = match_window(area_graph, window)
    mp = matched[index] if index < len(matched) else None
    found_name = (mp.way_name or mp.way_ref) if mp else None

    mismatch = None
    if target_road_name:
        matches_hint = False
        if mp is not None:
            acceptable_names = _acceptable_spellings(target_road_name)
            target_ref = _normalize_ref(target_road_name)
            matches_hint = (
                (mp.way_name and _normalize_road_name(mp.way_name) in acceptable_names)
                or (mp.way_ref and _normalize_ref(mp.way_ref) == target_ref)
            )
        if not matches_hint:
            mismatch = {"expected": target_road_name, "found": found_name}

    return RejoinPoint(
        index=index, road_name=found_name, anchor_lat=lat, anchor_lon=lon,
        distance_to_reference_m=dist, road_name_mismatch=mismatch,
    )


# ── Verifica di direzione ──────────────────────────────────────────────────────

def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Bearing iniziale in gradi [0,360) dal punto 1 al punto 2 — formula
    standard great-circle. Non riusata da planner_agent._bearing (privata,
    modulo diverso — Planner genera percorsi da zero, dominio concettualmente
    distinto): poche righe di trigonometria standard, duplicarle qui evita
    un accoppiamento cross-dominio per una formula banale, diverso dal caso
    di query_overpass (logica di retry/mirror non banale, quella sì riusata).
    """
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _angular_diff_deg(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


@dataclass
class DirectionCheck:
    expected_bearing_deg: float
    actual_bearing_deg: float
    diff_deg: float
    consistent: bool


def verify_direction(
    anchor: tuple[float, float],
    forward_points: list[tuple[float, float]],
    target: tuple[float, float],
    lookahead_m: float = _DIRECTION_LOOKAHEAD_M_DEFAULT,
    tolerance_deg: float = _DIRECTION_TOLERANCE_DEG_DEFAULT,
) -> DirectionCheck | None:
    """
    Confronta il bearing REALE del tracciato dopo l'anchor (dall'anchor al
    primo punto di forward_points ad almeno lookahead_m di distanza) con il
    bearing ATTESO (dall'anchor verso `target`, es. le coordinate
    geocodificate del luogo che l'utente ha indicato come direzione, "verso
    Montemarciano") — se la differenza supera tolerance_deg, il tracciato
    NON sta andando nella direzione descritta (es. sta tornando indietro).

    Ritorna None se forward_points non raggiunge mai lookahead_m dall'anchor
    (finestra troppo corta per un giudizio affidabile) — non un
    consistent=False: un "non so", non un "sbagliato".
    """
    far_point = None
    for lat, lon in forward_points:
        if geodesic(anchor, (lat, lon)).meters >= lookahead_m:
            far_point = (lat, lon)
            break
    if far_point is None:
        return None
    expected = _bearing_deg(anchor[0], anchor[1], target[0], target[1])
    actual = _bearing_deg(anchor[0], anchor[1], far_point[0], far_point[1])
    diff = _angular_diff_deg(expected, actual)
    return DirectionCheck(
        expected_bearing_deg=expected, actual_bearing_deg=actual,
        diff_deg=diff, consistent=diff <= tolerance_deg,
    )


# ── Finestra locale attorno a un centro approssimativo ────────────────────────

def local_window_indices(
    gpx_points: list[tuple[float, float]],
    center: tuple[float, float],
    radius_m: float = _WINDOW_RADIUS_M_DEFAULT,
    padding_points: int = _WINDOW_PADDING_POINTS,
) -> tuple[int, int] | None:
    """
    (start_idx, end_idx) [end esclusivo] del primo tratto CONTIGUO di
    gpx_points entro radius_m da `center`, allargato di padding_points su
    ciascun lato — None se nessun punto è entro radius_m.

    "Primo tratto": se il tracciato passa più volte vicino a `center` (es.
    un anello che si autointerseca vicino a quel punto), viene preso solo
    il primo incontro; il caso multi-passaggio non è gestito in questo giro.
    """
    within = [i for i, p in enumerate(gpx_points) if geodesic(center, p).meters <= radius_m]
    if not within:
        return None
    start = within[0]
    end = start
    for i in within[1:]:
        if i == end + 1:
            end = i
        else:
            break
    lo = max(0, start - padding_points)
    hi = min(len(gpx_points), end + 1 + padding_points)
    return lo, hi


# ── Pipeline completa per un ANCHOR ───────────────────────────────────────────

def resolve_anchor(
    area_graph: AreaGraph,
    gpx_points: list[tuple[float, float]],
    center: tuple[float, float],
    from_road: str,
    target: tuple[float, float] | None = None,
    window_radius_m: float = _WINDOW_RADIUS_M_DEFAULT,
    lookahead_m: float = _DIRECTION_LOOKAHEAD_M_DEFAULT,
    tolerance_deg: float = _DIRECTION_TOLERANCE_DEG_DEFAULT,
) -> dict:
    """
    Pipeline completa per un ANCHOR che descrive una transizione tra vie:
      1. individua la finestra locale del GPX attorno a `center` (stima
         approssimativa, es. da geocodifica del testo del waypoint) —
         MAI l'intero tracciato;
      2. map-matching solo su quella finestra (match_window);
      3. trova la transizione da `from_road` a un'altra via (find_transition);
      4. se `target` è dato, verifica che il tracciato dopo la transizione
         vada nella sua direzione (verify_direction).

    Ritorna un dict con tutti i risultati intermedi (non solo il risultato
    finale) per trasparenza/debug — "resolved": False con "error" se un
    passaggio fallisce.
    """
    window = local_window_indices(gpx_points, center, radius_m=window_radius_m)
    if window is None:
        return {"resolved": False, "error": f"Nessun punto del GPX entro {window_radius_m:.0f}m da {center}"}
    lo, hi = window
    local_points = gpx_points[lo:hi]

    matched = match_window(area_graph, local_points)
    transition = find_transition(area_graph, matched, from_road)
    if transition is None:
        return {
            "resolved": False,
            "error": f"Nessuna transizione da {from_road!r} trovata nella finestra GPX [{lo},{hi})",
            "window": (lo, hi),
        }

    result: dict = {
        "resolved": True,
        "window": (lo, hi),
        "gpx_index_before": lo + transition.index - 1,
        "gpx_index_after": lo + transition.index,
        "from_road": transition.from_name,
        "to_road": transition.to_name,
        "anchor_lat": transition.anchor_lat,
        "anchor_lon": transition.anchor_lon,
        "interpolation": transition.interpolation,
        "direction_check": None,
    }

    if target is not None:
        forward = local_points[transition.index:]
        check = verify_direction(
            (transition.anchor_lat, transition.anchor_lon), forward, target,
            lookahead_m=lookahead_m, tolerance_deg=tolerance_deg,
        )
        if check is not None:
            result["direction_check"] = {
                "expected_bearing_deg": round(check.expected_bearing_deg, 1),
                "actual_bearing_deg": round(check.actual_bearing_deg, 1),
                "diff_deg": round(check.diff_deg, 1),
                "consistent": check.consistent,
            }

    return result


def resolve_rejoin(
    area_graph: AreaGraph,
    gpx_points: list[tuple[float, float]],
    center: tuple[float, float],
    target_road: str | None = None,
    window_radius_m: float = _REJOIN_WINDOW_RADIUS_M_DEFAULT,
) -> dict:
    """
    Pipeline completa per un ANCHOR di tipo RACCORDO/ARRIVO — selezione
    GEOMETRICA del punto (vicinanza a `center`, coerenza di direzione), non
    più basata sul combaciare di `target_road` (vedi nota sopra
    find_rejoin_point: un caso reale ha mostrato che il tracciato originale
    può oggettivamente percorrere una via diversa da quella nominata
    dall'utente, senza che sia un errore). `target_road` è quindi opzionale
    e usato solo come verifica informativa (road_name_mismatch nel
    risultato), mai come filtro che fa fallire la risoluzione.

      1. individua la finestra locale del GPX attorno a `center` — MAI
         l'intero tracciato (stessa local_window_indices di resolve_anchor);
      2. selezione geometrica del punto + verifica informativa del nome via
         (find_rejoin_point — nessuna transizione da cercare).

    Nessuna verifica di direzione "verso un target" qui (a differenza di
    resolve_anchor): la coerenza di direzione è già parte della selezione
    geometrica del punto stesso (scarta le inversioni a U locali), non c'è
    un luogo di destinazione esterno da confrontare.

    Ritorna un dict con tutti i risultati intermedi per trasparenza/debug —
    "resolved": False con "error" SOLO se la finestra locale è vuota (nessun
    punto del GPX vicino a `center`); un `target_road` che non corrisponde
    non fa più fallire la risoluzione, vedi "road_name_mismatch".
    """
    window = local_window_indices(gpx_points, center, radius_m=window_radius_m)
    if window is None:
        return {"resolved": False, "error": f"Nessun punto del GPX entro {window_radius_m:.0f}m da {center}"}
    lo, hi = window
    local_points = gpx_points[lo:hi]

    rejoin = find_rejoin_point(area_graph, local_points, target_road, reference_point=center)
    if rejoin is None:
        return {"resolved": False, "error": "Finestra locale vuota", "window": (lo, hi)}

    return {
        "resolved": True,
        "window": (lo, hi),
        "gpx_index": lo + rejoin.index,
        "road": rejoin.road_name,
        "anchor_lat": rejoin.anchor_lat,
        "anchor_lon": rejoin.anchor_lon,
        "distance_to_reference_m": (
            round(rejoin.distance_to_reference_m, 1) if rejoin.distance_to_reference_m is not None else None
        ),
        "road_name_mismatch": rejoin.road_name_mismatch,
    }
