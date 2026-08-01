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

import time

import httpx
from geopy.distance import geodesic

OVERPASS_URL    = "https://overpass-api.de/api/interpreter"
SEARCH_RADIUS   = 600    # m — raggio di ricerca sentieri attorno alla pos. approssimativa
N_POINTS        = 3      # nodi campionati lungo il sentiero scelto
REQUEST_TIMEOUT = 30.0
RETRY_DELAYS    = [4, 10, 20]

_TRAIL_TYPES = "path|track|bridleway|cycleway"

_SNAP_ROAD_TYPES = (
    "primary|secondary|tertiary|unclassified|residential|service|cycleway|track"
)
SNAP_SEARCH_RADIUS = 600  # m


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

    ways = _query_overpass(query)
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


def snap_to_nearest_road(
    lat: float,
    lon: float,
    radius_m: int = SNAP_SEARCH_RADIUS,
) -> tuple[float, float] | None:
    """
    Trova il punto più vicino su una strada carrabile reale entro radius_m da
    (lat, lon) — usato per agganciare i waypoint utente "soft" (SRS §6.3) a
    un punto che BRouter può effettivamente attraversare, invece di escluderli.

    Considera solo highway di passaggio reale (primary/secondary/tertiary/
    unclassified/residential/service/cycleway/track), escludendo footway/steps/
    pedestrian/path isolati (troppo minori/spesso vicoli ciechi in un centro
    abitato — quelli restano di competenza di resolve_area_traversal).

    Tra le vie trovate, preferisce quelle con tag `ref` o `name` (più probabile
    siano strade di passaggio vere, non stub residenziali senza sbocco): la
    ricerca del punto più vicino avviene prima solo su quelle, e ricade sulle
    altre solo se nessuna via con ref/name è nel raggio.

    Ritorna (lat, lon) del punto agganciato, oppure None se nessuna strada è
    stata trovata nel raggio (il chiamante mantiene in quel caso il comportamento
    di fallback esistente).
    """
    query = (
        f"[out:json][timeout:25];\n"
        f"(\n"
        f'  way(around:{radius_m},{lat:.6f},{lon:.6f})'
        f'  ["highway"~"^({_SNAP_ROAD_TYPES})$"];\n'
        f");\n"
        f"out body geom;\n"
    )

    ways = _query_overpass(query)
    if not ways:
        return None

    named_ways = [w for w in ways if (w.get("tags") or {}).get("ref") or (w.get("tags") or {}).get("name")]
    candidates = named_ways or ways

    best_point: tuple[float, float] | None = None
    best_dist = float("inf")
    for way in candidates:
        for g in way.get("geometry", []):
            d = geodesic((lat, lon), (g["lat"], g["lon"])).meters
            if d < best_dist:
                best_dist = d
                best_point = (g["lat"], g["lon"])

    return best_point


# ── Rete ──────────────────────────────────────────────────────────────────────

def _query_overpass(query_str: str) -> list[dict]:
    raw_body = ("data=" + query_str).encode("utf-8")
    headers  = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "curl/8.7.1",
    }

    for attempt, delay in enumerate(RETRY_DELAYS + [None]):
        try:
            r = httpx.post(
                OVERPASS_URL, content=raw_body, headers=headers, timeout=REQUEST_TIMEOUT
            )
            if r.status_code == 200:
                return [e for e in r.json().get("elements", []) if e.get("type") == "way"]
            if delay is not None:
                time.sleep(delay)
            else:
                return []
        except httpx.TimeoutException:
            if delay is not None:
                time.sleep(delay)
            else:
                return []
        except Exception:
            return []
    return []
