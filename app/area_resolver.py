"""
Area Resolver — trova sentieri OSM reali per waypoint traversal (SRS §6.3bis).

Quando il Planner segna un waypoint via con traversal=True, il Candidate Generator
chiama questo modulo per sostituire il singolo waypoint con una sequenza ordinata
di 2–4 nodi reali lungo il sentiero/traccia principale dell'area indicata.

Uso:
    from area_resolver import resolve_area_traversal
    pts = resolve_area_traversal("Parco del Cormorano", (43.6373, 13.3625))
    # [(43.638, 13.361), (43.635, 13.358), (43.632, 13.355)]
    # lista vuota se nessun sentiero trovato → usa il waypoint originale
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import httpx
from geopy.distance import geodesic

log = logging.getLogger(__name__)

# Mirror pubblici di Overpass, provati a rotazione a ogni tentativo — se l'endpoint
# principale è sotto stress (429/504, osservato in produzione su "Polverigi_5"),
# il tentativo successivo prova un host diverso invece di ripetere la stessa
# richiesta contro lo stesso servizio degradato.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]
SEARCH_RADIUS   = 600    # m — raggio di ricerca sentieri attorno alla pos. approssimativa
N_POINTS        = 3      # nodi campionati lungo il sentiero scelto
REQUEST_TIMEOUT = 30.0
RETRY_DELAYS    = [4, 10, 20, 40]

_TRAIL_TYPES = "path|track|bridleway|cycleway"

_SNAP_ROAD_TYPES = (
    "primary|secondary|tertiary|unclassified|residential|service|cycleway|track"
)
SNAP_SEARCH_RADIUS = 600  # m

# Filtro anti-vicolo-cieco/accesso-privato (diagnosi "Polverigi_9": lo snap aveva
# agganciato un waypoint a una highway=service di soli 3 nodi, ~pochi metri —
# tecnicamente passa il filtro per tipo, ma è quasi certamente un cortile/accesso
# privato, non una strada di passaggio, e costringe BRouter a un vero e proprio
# andata/ritorno per toccarlo). Scartiamo le vie troppo corte o con troppo pochi
# nodi indipendentemente dal tag highway.
MIN_WAY_LENGTH_M = 80.0
MIN_WAY_NODES = 4
# Se nessuna via supera il filtro nel raggio originale, un solo raddoppio prima
# di ricadere nel comportamento esistente (fallback BRouter diretto o esclusione).
MAX_SNAP_RADIUS_M = 1200

# Cache in-memory dei soli aggangi RIUSCITI di snap_to_nearest_road, condivisa
# tra tutte le route generate nel processo (la rete stradale non cambia nel
# tempo). I fallimenti non vengono mai cachati: un errore temporaneo di Overpass
# non deve bloccare permanentemente un waypoint per le route successive.
_snap_cache: dict[tuple[float, float, int], tuple[float, float]] = {}


class OverpassUnavailable(Exception):
    """
    Sollevata da _query_overpass quando NESSUNO degli endpoint Overpass risponde
    con successo dopo tutti i tentativi — distingue questo caso (servizio non
    raggiungibile) da una risposta 200 con zero risultati (nessuna strada/sentiero
    trovato nel raggio, esito geografico legittimo).
    """


def resolve_area_traversal(
    area_hint: str,
    approx_location: tuple[float, float],
    n_points: int = N_POINTS,
) -> list[tuple[float, float]]:
    """
    Ritorna n_points coordinate (lat, lon) equidistribuite lungo il sentiero
    principale trovato entro SEARCH_RADIUS dalla posizione approssimativa.

    Restituisce [] se nessun sentiero è trovato (il Candidate Generator usa
    allora il waypoint originale geocodificato senza espansione traversal).
    """
    lat, lon = approx_location
    query = (
        f"[out:json][timeout:25];\n"
        f"(\n"
        f'  way(around:{SEARCH_RADIUS},{lat:.6f},{lon:.6f})'
        f'  ["highway"~"^({_TRAIL_TYPES})$"];\n'
        f");\n"
        f"out body geom;\n"
    )

    try:
        ways = _query_overpass(query)
    except OverpassUnavailable:
        return []
    if not ways:
        return []

    # Scegli il way con più nodi di geometria (il sentiero più lungo / continuo)
    ways.sort(key=lambda w: len(w.get("geometry", [])), reverse=True)
    best = ways[0]
    geom = best.get("geometry", [])
    if len(geom) < 2:
        return []

    pts = [(g["lat"], g["lon"]) for g in geom]

    # Campiona n_points nodi equidistribuiti (inclusi primo e ultimo)
    if len(pts) <= n_points:
        return pts

    step = (len(pts) - 1) / (n_points - 1)
    return [pts[round(i * step)] for i in range(n_points)]


def _way_length_m(way: dict) -> float:
    geom = way.get("geometry", [])
    total = 0.0
    for i in range(1, len(geom)):
        total += geodesic(
            (geom[i - 1]["lat"], geom[i - 1]["lon"]),
            (geom[i]["lat"], geom[i]["lon"]),
        ).meters
    return total


def _filter_real_roads(ways: list[dict]) -> list[dict]:
    """
    Scarta vie troppo corte o con troppo pochi nodi — indipendentemente dal tag
    highway, sono quasi sempre accessi privati/cortili/vicoli ciechi minori
    (es. una highway=service di 3 nodi verso un cortile passa il filtro per
    tipo ma non è una strada di passaggio — diagnosi "Polverigi_9"/Agugliano).
    """
    out = []
    for w in ways:
        geom = w.get("geometry", [])
        if len(geom) < MIN_WAY_NODES:
            continue
        if _way_length_m(w) < MIN_WAY_LENGTH_M:
            continue
        out.append(w)
    return out


def snap_to_nearest_road(
    lat: float,
    lon: float,
    radius_m: int = SNAP_SEARCH_RADIUS,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[float, float] | None:
    """
    Trova il punto più vicino su una strada carrabile reale entro radius_m da
    (lat, lon) — usato per agganciare i waypoint utente "soft" (SRS §6.3) a
    un punto che BRouter può effettivamente attraversare, invece di escluderli.

    Considera solo highway di passaggio reale (primary/secondary/tertiary/
    unclassified/residential/service/cycleway/track), escludendo footway/steps/
    pedestrian/path isolati (troppo minori/spesso vicoli ciechi in un centro
    abitato — quelli restano di competenza di resolve_area_traversal). Scarta
    inoltre le vie troppo corte/con pochi nodi (MIN_WAY_LENGTH_M/MIN_WAY_NODES)
    — probabili accessi privati anche se il tag highway le farebbe passare.

    Se nessuna via supera il filtro nel raggio richiesto, allarga il raggio UNA
    volta (raddoppiato, fino a MAX_SNAP_RADIUS_M) prima di arrendersi — una via
    di passaggio vera potrebbe semplicemente essere un po' più lontana della
    prima via (scartata) trovata nel raggio originale.

    Tra le vie che superano il filtro, preferisce quelle con tag `ref` o `name`
    (più probabile siano strade di passaggio vere): la ricerca del punto più
    vicino avviene prima solo su quelle, e ricade sulle altre solo se nessuna
    via con ref/name è nel raggio.

    Ritorna (lat, lon) del punto agganciato, oppure None se nessuna strada
    valida è stata trovata nemmeno dopo l'allargamento del raggio (il chiamante
    mantiene in quel caso il comportamento di fallback esistente).

    Solleva OverpassUnavailable se il servizio non risponde con successo dopo
    tutti i tentativi — il chiamante la distingue esplicitamente dal caso "nessuna
    strada trovata" (vedi candidate_generator._resolve_soft_snaps). Non viene
    ritentata con raggio allargato: se il servizio è giù, allargare il raggio
    non aiuta, serve solo altra attesa.

    Gli aggangi riusciti sono cachati a livello di modulo (coordinata arrotondata
    a 5 decimali, ~1m di precisione): la stessa coordinata richiesta da route
    diverse in futuro non ripete la query di rete.
    """
    cache_key = (round(lat, 5), round(lon, 5), radius_m)
    if cache_key in _snap_cache:
        return _snap_cache[cache_key]

    def _query_at_radius(r: int) -> list[dict]:
        query = (
            f"[out:json][timeout:25];\n"
            f"(\n"
            f'  way(around:{r},{lat:.6f},{lon:.6f})'
            f'  ["highway"~"^({_SNAP_ROAD_TYPES})$"];\n'
            f");\n"
            f"out body geom;\n"
        )
        return _query_overpass(query, on_progress=on_progress)  # OverpassUnavailable si propaga al chiamante

    current_radius = radius_m
    candidates_filtered: list[dict] = []
    while True:
        ways = _query_at_radius(current_radius)
        candidates_filtered = _filter_real_roads(ways)
        if candidates_filtered or current_radius >= MAX_SNAP_RADIUS_M:
            break
        next_radius = min(current_radius * 2, MAX_SNAP_RADIUS_M)
        log.info(
            "snap_to_nearest_road (%.6f,%.6f): nessuna via valida entro %dm "
            "(%d vie trovate, tutte scartate per lunghezza/nodi insufficienti) "
            "— allargo il raggio a %dm.",
            lat, lon, current_radius, len(ways), next_radius,
        )
        current_radius = next_radius

    if not candidates_filtered:
        return None

    named_ways = [
        w for w in candidates_filtered
        if (w.get("tags") or {}).get("ref") or (w.get("tags") or {}).get("name")
    ]
    candidates = named_ways or candidates_filtered

    best_point: tuple[float, float] | None = None
    best_dist = float("inf")
    for way in candidates:
        for g in way.get("geometry", []):
            d = geodesic((lat, lon), (g["lat"], g["lon"])).meters
            if d < best_dist:
                best_dist = d
                best_point = (g["lat"], g["lon"])

    if best_point is not None:
        _snap_cache[cache_key] = best_point
    return best_point


# ── Rete ──────────────────────────────────────────────────────────────────────

def _query_overpass(
    query_str: str,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """
    Interroga Overpass con retry, ruotando tra OVERPASS_URLS a ogni tentativo
    (round-robin) invece di ripetere sempre lo stesso host — se l'endpoint
    principale è sotto stress (visto in produzione: sequenze di 429/504 durate
    oltre un minuto), i tentativi successivi provano un mirror diverso.

    Ritorna la lista di way OSM su una risposta 200 (anche vuota, se l'area
    non ha strade nel raggio — esito legittimo). Solleva OverpassUnavailable
    se NESSUN tentativo, su NESSUN mirror, ottiene una risposta 200.

    on_progress(message), se fornito, viene chiamato ad ogni nuovo tentativo —
    questo è il caso che più spesso fa sembrare l'app bloccata per minuti
    (429/504 a ripetizione), quindi va reso visibile all'utente.
    """
    raw_body = ("data=" + query_str).encode("utf-8")
    headers  = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "curl/8.7.1",
    }

    attempts = RETRY_DELAYS + [None]
    for attempt, delay in enumerate(attempts):
        url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
        try:
            r = httpx.post(url, content=raw_body, headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return [e for e in r.json().get("elements", []) if e.get("type") == "way"]
        except httpx.TimeoutException:
            pass
        except Exception:
            pass
        if delay is not None:
            next_attempt = attempt + 2  # 1-indexed, il prossimo tentativo
            log.info(
                "Overpass non risponde, nuovo tentativo (%d/%d, mirror alternativo)...",
                next_attempt, len(attempts),
            )
            if on_progress:
                try:
                    on_progress(
                        f"⏳ Overpass not responding, retrying ({next_attempt}/{len(attempts)}, "
                        f"alternate mirror)..."
                    )
                except Exception:
                    pass
            time.sleep(delay)

    raise OverpassUnavailable(
        f"Overpass non ha risposto con successo dopo {len(attempts)} tentativi "
        f"su {len(OVERPASS_URLS)} endpoint diversi"
    )
