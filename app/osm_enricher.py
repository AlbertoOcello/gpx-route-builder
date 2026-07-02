"""
OSM Tag Enricher (Fase 9) — SRS §5.5.
Interroga Overpass API per caratterizzare superficie, tipo di strada e
presenza di sentieri lungo una traccia GPX campionata.

Utilizzo principale (da scoring_engine o pipeline):
    from osm_enricher import enrich_gpx
    enrichment = enrich_gpx(gpx_path)

Il dict di ritorno (SRS §5.5 output):
    asphalt_percent, gravel_percent, cobblestone_percent,
    main_road_percent, secondary_percent, trail_percent, trail_count,
    cobblestone_detected, ss16_detected,
    inferred_percent, unresolved_percent,
    partial, samples_total, samples_resolved, elapsed_s

Quirk Overpass: il body deve essere raw (non URL-encoded) e lo User-Agent
deve essere curl-like — i client Python normali ottengono HTTP 406.
L'HTTP 406 è anche transiente (CDN round-robin su backend broken): si riprova
con backoff breve prima di dichiarare il batch fallito.
"""
from __future__ import annotations

import time
from pathlib import Path

import gpxpy
import httpx
from geopy.distance import geodesic

# ── Parametri di rete ─────────────────────────────────────────────────────────

OVERPASS_URL    = "https://overpass-api.de/api/interpreter"
SAMPLE_INTERVAL = 1000         # m tra un campione e il successivo
BATCH_SIZE      = 10           # punti per query (piccoli batch → meno 429)
AROUND_RADIUS   = 50           # raggio OSM way search (m)
MAX_ASSIGN_DIST = 80           # distanza max per assegnare un way a un campione (m)
REQUEST_TIMEOUT = 45.0
SLEEP_BETWEEN   = 10.0         # s tra batch (rate limiting cortese)
RETRY_DELAYS_406 = [2, 4, 8, 15, 25]   # CDN backend errato — breve backoff
RETRY_DELAYS_504 = [10, 25]             # server overload
RETRY_DELAYS_429 = [30, 60]             # rate limit — lunga attesa
PARTIAL_THRESHOLD = 0.35       # >35% campioni non risolti → partial=True

# ── Set di classificazione ────────────────────────────────────────────────────

_ASPHALT_SURFACES = frozenset({
    "asphalt", "paved", "concrete", "tarmac", "bituminous",
    "concrete:plates", "concrete:lanes",
})
_GRAVEL_SURFACES = frozenset({
    "gravel", "fine_gravel", "compacted", "dirt", "unpaved",
    "ground", "sand", "earth", "mud", "grass", "woodchips",
})
_COBBLE_SURFACES = frozenset({
    "cobblestone", "sett", "paving_stones", "cobbles",
    "unhewn_cobblestone", "cobblestone:flattened",
})
_MAIN_HIGHWAYS = frozenset({        # traffico intenso: penalità pesante
    "primary", "trunk", "motorway",
    "primary_link", "trunk_link", "motorway_link",
})
_SECONDARY_HIGHWAYS = frozenset({   # SP Strade Provinciali: penalità leggera
    "secondary", "secondary_link",
})
_TRAIL_HIGHWAYS = frozenset({       # sentieri/ciclabili: proxy natura/scenic
    "path", "track", "bridleway", "cycleway",
})
_PEDESTRIAN_HIGHWAYS = frozenset({
    "footway", "pedestrian", "steps", "elevator", "corridor",
})


# ── Classificazione tag OSM ───────────────────────────────────────────────────

def _classify(tags: dict) -> dict:
    surface   = tags.get("surface",   "").lower().strip()
    highway   = tags.get("highway",   "").lower().strip()
    tracktype = tags.get("tracktype", "").lower().strip()
    ref       = tags.get("ref",       "").upper()

    is_asphalt = is_gravel = is_cobblestone = False
    inferred = False

    if surface in _ASPHALT_SURFACES:
        is_asphalt = True
    elif surface in _GRAVEL_SURFACES:
        is_gravel = True
    elif surface in _COBBLE_SURFACES:
        is_cobblestone = True
    else:
        inferred = True
        if tracktype == "grade1":
            is_asphalt = True
        elif tracktype in {"grade2", "grade3", "grade4", "grade5"}:
            is_gravel = True
        elif highway in {"track", "path", "bridleway"}:
            is_gravel = True
        elif highway in {"cycleway", "residential", "unclassified", "tertiary",
                         "secondary", "primary", "trunk", "service",
                         "living_street", "road"}:
            is_asphalt = True
        else:
            is_asphalt = True

    is_cycleway = (
        highway in _TRAIL_HIGHWAYS
        or (highway in {"path", "track"} and tags.get("bicycle") == "designated")
    )

    return {
        "is_asphalt":     is_asphalt,
        "is_gravel":      is_gravel,
        "is_cobblestone": is_cobblestone,
        "is_main_road":   highway in _MAIN_HIGHWAYS and not is_cycleway,
        "is_secondary":   highway in _SECONDARY_HIGHWAYS and not is_cycleway,
        "is_trail":       highway in _TRAIL_HIGHWAYS or is_cycleway,
        "is_pedestrian":  highway in _PEDESTRIAN_HIGHWAYS,
        # ss16_detected solo su carreggiata motorizzata (non su piste ciclabili adiacenti)
        "ss16_detected":  ("SS16" in ref or "SS 16" in ref)
                          and highway in (_MAIN_HIGHWAYS | _SECONDARY_HIGHWAYS)
                          and not is_cycleway,
        "inferred":       inferred,
    }


# ── Campionamento GPX ─────────────────────────────────────────────────────────

def sample_track(gpx_path: str, interval_m: int = SAMPLE_INTERVAL) -> list[tuple[float, float]]:
    with open(gpx_path) as f:
        gpx = gpxpy.parse(f)

    all_pts = [pt for t in gpx.tracks for s in t.segments for pt in s.points]
    if not all_pts:
        return []

    sampled = [(all_pts[0].latitude, all_pts[0].longitude)]
    acc = 0.0
    for i in range(1, len(all_pts)):
        p1, p2 = all_pts[i - 1], all_pts[i]
        acc += geodesic((p1.latitude, p1.longitude), (p2.latitude, p2.longitude)).meters
        if acc >= interval_m:
            sampled.append((p2.latitude, p2.longitude))
            acc = 0.0

    last = (all_pts[-1].latitude, all_pts[-1].longitude)
    if sampled[-1] != last:
        sampled.append(last)
    return sampled


# ── Query Overpass ────────────────────────────────────────────────────────────

def _is_natural_element(tags: dict) -> bool:
    """True se l'elemento OSM rappresenta un'area/linea naturale, verde o idrica."""
    if tags.get("natural"):
        return True
    if tags.get("waterway") in {"river", "stream", "canal", "ditch"}:
        return True
    if tags.get("leisure") in {"park", "nature_reserve", "garden"}:
        return True
    if tags.get("landuse") in {"forest", "meadow", "grass", "nature_reserve", "greenfield"}:
        return True
    return False


def _build_query(points: list[tuple[float, float]], radius_m: int) -> str:
    nat_radius = radius_m * 2   # 100 m per elementi naturali
    hw_union = "\n".join(
        f'  way(around:{radius_m},{lat},{lon})["highway"];'
        for lat, lon in points
    )
    nat_union = "\n".join(
        f'  way(around:{nat_radius},{lat},{lon})["natural"];\n'
        f'  way(around:{nat_radius},{lat},{lon})["waterway"~"^(river|stream|canal)$"];\n'
        f'  way(around:{nat_radius},{lat},{lon})["leisure"~"^(park|nature_reserve|garden)$"];\n'
        f'  way(around:{nat_radius},{lat},{lon})["landuse"~"^(forest|meadow|grass|nature_reserve)$"];'
        for lat, lon in points
    )
    return f"[out:json][timeout:45];\n(\n{hw_union}\n{nat_union}\n);\nout tags geom;\n"


def _check_near_natural(
    lat: float,
    lon: float,
    ways: list[dict],
    radius: float = 100.0,
) -> bool:
    """True se esiste almeno un elemento naturale/verde/idrico entro `radius` m."""
    for w in ways:
        if not _is_natural_element(w.get("tags", {})):
            continue
        if _way_min_dist(lat, lon, w) <= radius:
            return True
    return False


def _query_overpass(
    points: list[tuple[float, float]],
    radius_m: int = AROUND_RADIUS,
    sleep_between: float = SLEEP_BETWEEN,
) -> list[dict] | None:
    query    = _build_query(points, radius_m)
    raw_body = ("data=" + query).encode("utf-8")
    headers  = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "curl/8.7.1",
    }

    idx_406 = idx_504 = idx_429 = 0
    delays_406 = list(RETRY_DELAYS_406) + [None]
    delays_504 = list(RETRY_DELAYS_504) + [None]
    delays_429 = list(RETRY_DELAYS_429) + [None]

    while True:
        try:
            resp = httpx.post(OVERPASS_URL, content=raw_body, headers=headers,
                              timeout=REQUEST_TIMEOUT)

            if resp.status_code == 200:
                return [e for e in resp.json().get("elements", [])
                        if e.get("type") == "way"]

            elif resp.status_code in (504, 503):
                d = delays_504[idx_504] if idx_504 < len(delays_504) else None
                if d is not None:
                    time.sleep(d); idx_504 += 1; continue
                return None

            elif resp.status_code == 429:
                d = delays_429[idx_429] if idx_429 < len(delays_429) else None
                if d is not None:
                    time.sleep(d); idx_429 += 1; continue
                return None

            elif resp.status_code == 406:
                d = delays_406[idx_406] if idx_406 < len(delays_406) else None
                if d is not None:
                    time.sleep(d); idx_406 += 1; continue
                return None

            else:
                return None

        except httpx.TimeoutException:
            d = delays_504[idx_504] if idx_504 < len(delays_504) else None
            if d is not None:
                time.sleep(d); idx_504 += 1; continue
            return None
        except Exception:
            return None


# ── Matching geometrico ───────────────────────────────────────────────────────

def _way_min_dist(lat: float, lon: float, way: dict) -> float:
    geom = way.get("geometry", [])
    if not geom:
        return float("inf")
    return min(geodesic((lat, lon), (g["lat"], g["lon"])).meters for g in geom)


def _assign_nearest(
    sample_points: list[tuple[float, float]],
    ways: list[dict],
    max_dist: float = MAX_ASSIGN_DIST,
) -> list[dict | None]:
    assigned = []
    for lat, lon in sample_points:
        candidates = [(d, w) for w in ways
                      if (d := _way_min_dist(lat, lon, w)) <= max_dist]
        if not candidates:
            assigned.append(None)
            continue

        candidates.sort(key=lambda x: x[0])
        nearest_dist, nearest_way = candidates[0]

        # Preferisci non-pedonale se entro 15 m dal più vicino
        if nearest_way.get("tags", {}).get("highway", "") in _PEDESTRIAN_HIGHWAYS:
            for d, w in candidates[1:]:
                if d - nearest_dist <= 15:
                    if w.get("tags", {}).get("highway", "") not in _PEDESTRIAN_HIGHWAYS:
                        nearest_way = w
                        break

        # Preferisci ciclabile/sentiero a strada principale (primary/trunk) entro 20 m
        # Modella la realtà: un ciclista sulla pista adiacente a SS16 non è "su SS16"
        if nearest_way.get("tags", {}).get("highway", "") in _MAIN_HIGHWAYS:
            for d, w in candidates[1:]:
                if d - nearest_dist > 20:
                    break
                if w.get("tags", {}).get("highway", "") in _TRAIL_HIGHWAYS:
                    nearest_way = w
                    break

        assigned.append(nearest_way)
    return assigned


# ── Aggregazione ──────────────────────────────────────────────────────────────

def _consecutive_runs(flags: list[bool]) -> tuple[int, int]:
    """(isolated, sustained): isolated = campioni in run esattamente di 1; sustained = in run ≥ 2."""
    isolated = sustained = run = 0
    for f in flags:
        if f:
            run += 1
        else:
            if   run == 1: isolated  += 1
            elif run  > 1: sustained += run
            run = 0
    if   run == 1: isolated  += 1
    elif run  > 1: sustained += run
    return isolated, sustained


def aggregate(
    classifications: list[dict | None],
    n_total: int,
    natural_flags: list[bool] | None = None,
) -> dict:
    valid      = [c for c in classifications if c is not None]
    n_resolved = len(valid)
    t          = n_resolved or 1

    asphalt   = sum(1 for c in valid if c["is_asphalt"])
    gravel    = sum(1 for c in valid if c["is_gravel"])
    cobble    = sum(1 for c in valid if c["is_cobblestone"])
    secondary = sum(1 for c in valid if c["is_secondary"])
    trail     = sum(1 for c in valid if c["is_trail"])
    inferred  = sum(1 for c in valid if c["inferred"])

    # Analisi run consecutivi per main road e SS16
    # Un attraversamento isolato (ponte singolo, 1 campione) pesa 30% di un tratto continuato
    main_flags = [bool(c["is_main_road"]) for c in valid]
    ss16_flags = [bool(c["ss16_detected"]) for c in valid]

    main_isolated, main_sustained    = _consecutive_runs(main_flags)
    ss16_isolated, ss16_sustained    = _consecutive_runs(ss16_flags)

    main_raw      = main_isolated + main_sustained
    main_effective = main_sustained + 0.3 * main_isolated   # sconto per attraversamenti brevi

    # ss16_detected = True solo se presenza sostenuta (≥2 campioni consecutivi)
    # Un singolo ponte obbligato non costituisce "rotta su SS16"
    ss16_detected = ss16_sustained > 0

    near_natural_count = sum(1 for f in (natural_flags or []) if f)
    n_nat = len(natural_flags) if natural_flags else 1

    return {
        "asphalt_percent":          round(asphalt      / t * 100, 1),
        "gravel_percent":           round(gravel       / t * 100, 1),
        "cobblestone_percent":      round(cobble       / t * 100, 1),
        "main_road_percent":        round(main_effective / t * 100, 1),  # effettivo (scontato)
        "main_road_percent_raw":    round(main_raw     / t * 100, 1),    # grezzo (tutti i campioni)
        "main_road_isolated_count": main_isolated,
        "main_road_sustained_count": main_sustained,
        "secondary_percent":        round(secondary    / t * 100, 1),
        "trail_percent":            round(trail        / t * 100, 1),
        "trail_count":              trail,
        "cobblestone_detected":     cobble > 0,
        "ss16_detected":            ss16_detected,
        "ss16_isolated_count":      ss16_isolated,
        "ss16_sustained_count":     ss16_sustained,
        "inferred_percent":         round(inferred     / t * 100, 1),
        "unresolved_percent":       round((n_total - n_resolved) / n_total * 100, 1),
        "near_natural_percent":     round(near_natural_count / n_nat * 100, 1),
        "near_natural_count":       near_natural_count,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def enrich_gpx(
    gpx_path: str,
    verbose: bool = False,
    sleep_between: float = SLEEP_BETWEEN,
) -> dict:
    """
    Arricchisce un file GPX con dati OSM di superficie e tipo di strada.

    Ritorna dict con i campi SRS §5.5 più metadati (partial, elapsed_s, ecc.).
    Se partial=True i campi di superficie non sono presenti e devono essere
    sostituiti da placeholder nello Scoring Engine.
    """
    t0 = time.time()

    gpx_path = str(gpx_path)
    sample_pts = sample_track(gpx_path, SAMPLE_INTERVAL)
    n = len(sample_pts)
    if n == 0:
        return {"partial": True, "reason": "GPX vuoto o senza traccia", "elapsed_s": 0.0,
                "samples_total": 0, "samples_resolved": 0,
                "failure_rate": 1.0, "unresolved_percent": 100.0}

    if verbose:
        print(f"  OSM enricher: {n} campioni, {len(range(0, n, BATCH_SIZE))} batch, "
              f"sleep={sleep_between}s tra batch")

    batches = [sample_pts[i: i + BATCH_SIZE] for i in range(0, n, BATCH_SIZE)]
    all_classifications: list[dict | None] = []
    all_natural_flags:   list[bool]        = []

    for b_idx, batch in enumerate(batches):
        ways = _query_overpass(batch, sleep_between=sleep_between)
        if ways is None:
            all_classifications.extend([None] * len(batch))
            all_natural_flags.extend([False] * len(batch))
            if verbose:
                print(f"  Batch {b_idx + 1}/{len(batches)}: FALLITO")
        else:
            hw_ways  = [w for w in ways if w.get("tags", {}).get("highway")]
            assigned = _assign_nearest(batch, hw_ways)
            classed  = [_classify(w["tags"]) if w else None for w in assigned]
            natural  = [_check_near_natural(lat, lon, ways) for lat, lon in batch]
            all_classifications.extend(classed)
            all_natural_flags.extend(natural)
            if verbose:
                resolved = sum(1 for c in classed if c is not None)
                nat_count = sum(natural)
                print(f"  Batch {b_idx + 1}/{len(batches)}: "
                      f"{resolved}/{len(batch)} risolti, {nat_count} near-natural")

        if b_idx < len(batches) - 1:
            time.sleep(sleep_between)

    n_resolved   = sum(1 for c in all_classifications if c is not None)
    failure_rate = (n - n_resolved) / n
    partial      = failure_rate > PARTIAL_THRESHOLD

    result: dict = {
        "partial":            partial,
        "samples_total":      n,
        "samples_resolved":   n_resolved,
        "failure_rate":       round(failure_rate, 3),
        "unresolved_percent": round(failure_rate * 100, 1),
        "elapsed_s":          round(time.time() - t0, 1),
    }

    if partial:
        result["reason"] = (
            f"Troppi campioni non risolti ({failure_rate:.0%} > {PARTIAL_THRESHOLD:.0%}). "
            "I dati OSM non sono affidabili — mantieni i placeholder."
        )
    else:
        result.update(aggregate(all_classifications, n, natural_flags=all_natural_flags))

    return result
