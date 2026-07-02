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
