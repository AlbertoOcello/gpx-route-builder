"""
Scoring Engine (Fase 8 + 9) — SRS §8.
Calcola punteggi normalizzati [0–100] per ogni candidato e applica penalità hard.

Punteggi REALI:
  distance_match, elevation  — da dati GPX
  surface, traffic, scenic   — da OSM Tag Enricher (Fase 9)

Punteggi ancora PLACEHOLDER (in attesa di Fase futura):
  user_preferences           — richiede analisi POI, storico gradimento

Come usare:
  result = score_candidate(analysis, request, enrichment=enrich_gpx(gpx_path))
  # Se enrichment è None o partial=True → surface/traffic/scenic ricadono al placeholder 70.
"""
from __future__ import annotations

from pathlib import Path

import yaml

try:
    from geopy.distance import geodesic as _geodesic
    _HAS_GEOPY = True
except ImportError:
    _HAS_GEOPY = False

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "scoring_weights.yaml"
_PLACEHOLDER = 70.0   # valore neutro per componenti non ancora calcolabili


def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ── Punteggi GPX-based (reali, già presenti in Fase 8) ───────────────────────

def _distance_match_score(distance_km: float, target_km: float, tolerance_km: float) -> float:
    diff = abs(distance_km - target_km)
    if diff <= tolerance_km:
        return 100.0
    if diff <= 2 * tolerance_km:
        return 100.0 * (1.0 - (diff - tolerance_km) / tolerance_km)
    return 0.0


def _elevation_score(elevation_gain_m: float, max_elevation_gain_m: float) -> float:
    if elevation_gain_m <= max_elevation_gain_m:
        return 100.0
    return max(0.0, 100.0 * (1.0 - (elevation_gain_m - max_elevation_gain_m) / max_elevation_gain_m))


# ── Punteggi OSM-based (Fase 9) ───────────────────────────────────────────────

def _surface_score(enrichment: dict, request: dict) -> float:
    """
    Punteggio superficie basato su gravel_percent vs preferenze UserMemory.

    preferred_gravel_percent (default 10%): percentuale ideale di sterrato.
    max_gravel_percent       (default 20%): soglia massima tollerata.

    Logic:
      gravel ≤ preferred → 100
      gravel in (preferred, max] → 100 → 60  (lineare)
      gravel > max       → hard discard (gestito separatamente), score crolla verso 0
      cobblestone: -4 pt per punto percentuale (max penalità -25)
    """
    preferred = float(request.get("preferred_gravel_percent", 10))
    maximum   = float(request.get("max_gravel_percent", 20))

    actual_gravel = float(enrichment.get("gravel_percent", 0))
    actual_cobble = float(enrichment.get("cobblestone_percent", 0))

    if actual_gravel <= preferred:
        gravel_score = 100.0
    elif actual_gravel <= maximum:
        t = (actual_gravel - preferred) / max(maximum - preferred, 1e-9)
        gravel_score = 100.0 - 40.0 * t          # 100 → 60
    else:
        excess = actual_gravel - maximum
        gravel_score = max(0.0, 60.0 - excess * 5.0)

    cobble_penalty = min(25.0, actual_cobble * 4.0)
    return round(max(0.0, gravel_score - cobble_penalty), 1)


def _traffic_score(enrichment: dict) -> float:
    """
    Punteggio traffico basato su main_road_percent e secondary_percent.

    main_road (primary/trunk/motorway): penalità pesante (-7 pt per %).
    secondary  (SP Strade Provinciali): penalità leggera  (-0.35 pt per %).
    Composito: 75% peso main, 25% peso secondary.
    """
    main_pct = float(enrichment.get("main_road_percent", 0))
    sec_pct  = float(enrichment.get("secondary_percent", 0))

    main_score = max(0.0, 100.0 - main_pct * 7.0)
    sec_score  = max(0.0, 100.0 - sec_pct  * 0.35)

    return round(0.75 * main_score + 0.25 * sec_score, 1)


def _scenic_score_proxy(enrichment: dict) -> float:
    """
    PROXY SEMPLIFICATO: trail_percent (path/track/bridleway/cycleway) come
    indicatore di naturalità/panoramicità.
      0% sentieri  → 40
      10% sentieri → 64
      25% sentieri → ~100

    NOTA: questa è un'approssimazione. Da raffinare in Fase futura con:
      POI panoramici OSM, vicinanza a corsi d'acqua/coste/creste,
      dati altimetrici, tag scenic/natural/tourism.
    """
    trail_pct = float(enrichment.get("trail_percent", 0))
    return round(min(100.0, 40.0 + trail_pct * 2.4), 1)


def _naturalness_score(enrichment: dict) -> float:
    """
    Punteggio naturalezza basato su tre segnali OSM:
      trail_percent       → 0–50 pt  (sentieri = ambienti naturali)
      near_natural_percent → 0–30 pt  (prossimità a elementi naturali/verdi/idrici)
      gravel_percent      → 0–10 pt  (fondo naturale come segnale reinforcing)
    Base 20 (tutti i percorsi ciclabili all'aperto hanno un minimo di naturalità).
    Range: [20, 100].
    """
    trail_pct    = float(enrichment.get("trail_percent",        0))
    nat_pct      = float(enrichment.get("near_natural_percent", 0))
    gravel_pct   = float(enrichment.get("gravel_percent",       0))

    trail_comp   = min(50.0, trail_pct  * 2.0)
    area_comp    = min(30.0, nat_pct    * 0.6)
    gravel_bonus = min(10.0, gravel_pct * 0.4)

    return round(min(100.0, 20.0 + trail_comp + area_comp + gravel_bonus), 1)


# ── Score candidate (entry point) ─────────────────────────────────────────────

def score_candidate(
    analysis: dict,
    request: dict,
    enrichment: dict | None = None,
) -> dict:
    """
    Calcola lo scoring completo per un candidato (SRS §8).

    analysis   : dict GPXAnalysis (SRS §5.5) — da candidate_generator
    request    : dict RouteRequest (SRS §5.1), idealmente già fuso con UserMemory
    enrichment : dict OSM Tag Enricher (SRS §5.5) — da osm_enricher.enrich_gpx().
                 None o partial=True → surface/traffic/scenic ricadono al placeholder.

    Ritorna dict con:
      component_scores : dict{nome → {score, placeholder, source}}
      total_score      : float [0–100], pesato per scoring_weights.yaml
      discarded        : bool
      discard_reason   : str | None
      osm_unresolved_percent : float — propagato alla UI per trasparenza
    """
    cfg = _load_config()
    weights: dict[str, float] = cfg["weights"]
    penalties: dict = cfg["hard_penalties"]

    target_km    = float(request["target_km"])
    tolerance_km = float(request.get("distance_tolerance_km", 5))
    max_elev     = float(request.get("max_elevation_gain_m", 800))
    route_type   = request.get("route_type", "loop")

    distance_km      = float(analysis["distance_km"])
    elevation_gain_m = float(analysis.get("elevation_gain_m", 0))
    loop_closed      = analysis.get("loop_closed")

    # Dati OSM disponibili e affidabili?
    osm_ok = bool(enrichment and not enrichment.get("partial"))
    osm_unresolved = float((enrichment or {}).get("unresolved_percent", 100.0))

    # ── Penalità hard geometriche (SRS §8.2) ─────────────────────────────────
    discarded = False
    discard_reason: str | None = None

    diff_km = abs(distance_km - target_km)
    factor  = float(penalties.get("distance_factor_discard", 2.0))
    if diff_km > factor * tolerance_km:
        discarded = True
        discard_reason = (
            f"Distanza {distance_km:.1f} km fuori tolleranza "
            f"({diff_km:.1f} km > {factor:.0f}× {tolerance_km:.0f} km = {factor * tolerance_km:.0f} km)"
        )

    if not discarded and penalties.get("loop_open_discard", True):
        if route_type in ("loop", "out_and_back") and loop_closed is False:
            discarded = True
            discard_reason = "Anello non chiuso (loop_closed=false)"

    # ── Penalità hard OSM (SRS §8.2 — attive solo con enrichment affidabile) ─
    if not discarded and osm_ok:
        # SS16 rilevata (o qualunque strada in avoid_places)
        avoid_places = [r.upper() for r in request.get("avoid_places", [])]
        if enrichment.get("ss16_detected") and any(
            "SS16" in r or "SS 16" in r for r in avoid_places
        ):
            discarded = True
            discard_reason = "SS16 rilevata sul percorso (avoid_always in UserMemory)"

        # Gravel oltre max_gravel_percent
        if not discarded:
            max_gravel    = float(request.get("max_gravel_percent", 20))
            actual_gravel = float(enrichment.get("gravel_percent", 0))
            if actual_gravel > max_gravel:
                discarded = True
                discard_reason = (
                    f"Sterrato {actual_gravel}% supera max_gravel_percent={max_gravel:.0f}% "
                    f"(UserMemory)"
                )

        # Cobblestone rilevante (>5%) quando in avoid_surfaces
        if not discarded:
            cobble_pct     = float(enrichment.get("cobblestone_percent", 0))
            avoid_surfaces = request.get("avoid_surfaces", [])
            if cobble_pct > 5.0 and "cobblestone" in avoid_surfaces:
                discarded = True
                discard_reason = (
                    f"Pavé/acciottolato {cobble_pct}% > 5% "
                    f"(cobblestone in avoid_always di UserMemory)"
                )

    # ── Punteggi componente ───────────────────────────────────────────────────
    def _ph(label: str) -> dict:
        return {"score": _PLACEHOLDER, "placeholder": True, "source": label}

    comp: dict[str, dict] = {
        "distance_match": {
            "score": round(_distance_match_score(distance_km, target_km, tolerance_km), 1),
            "placeholder": False, "source": "gpx",
        },
        "elevation": {
            "score": round(_elevation_score(elevation_gain_m, max_elev), 1),
            "placeholder": False, "source": "gpx",
        },
        "user_preferences": _ph("fase_futura"),  # richiede analisi POI + storico
    }

    if osm_ok:
        comp["surface"] = {
            "score": _surface_score(enrichment, request),
            "placeholder": False, "source": "osm",
        }
        comp["traffic"] = {
            "score": _traffic_score(enrichment),
            "placeholder": False, "source": "osm",
        }
        comp["scenic"] = {
            "score": _scenic_score_proxy(enrichment),
            "placeholder": False, "source": "osm_proxy",
            "note": "Proxy trail_percent — approssimazione, da raffinare con POI",
        }
        comp["naturalness"] = {
            "score": _naturalness_score(enrichment),
            "placeholder": False, "source": "osm",
        }
    else:
        src = "osm_partial" if enrichment else "osm_missing"
        comp["surface"]     = _ph(src)
        comp["traffic"]     = _ph(src)
        comp["scenic"]      = _ph(src)
        comp["naturalness"] = _ph(src)

    # ── Penalità ostacoli noti (known_obstacles) ─────────────────────────────
    # Controlla se la traccia passa vicino a ostacoli noti (entro 150 m → scartato).
    # Usa i waypoint GPX del candidato (analisi["waypoints"]) se disponibili,
    # altrimenti usa start/end dal request.
    if not discarded:
        known_obs = request.get("known_obstacles", [])
        if known_obs and _HAS_GEOPY:
            # Campiona la traccia con i waypoint se presenti, altrimenti solo start
            track_pts: list[tuple[float, float]] = []
            if analysis.get("track_sample"):
                track_pts = [tuple(p) for p in analysis["track_sample"]]
            elif analysis.get("start_lat") and analysis.get("start_lon"):
                track_pts = [(analysis["start_lat"], analysis["start_lon"])]

            if track_pts:
                OBSTACLE_RADIUS_M = 150.0
                for obs in known_obs:
                    obs_pt = (obs["lat"], obs["lon"])
                    min_dist = min(
                        _geodesic(obs_pt, tp).meters for tp in track_pts
                    )
                    if min_dist < OBSTACLE_RADIUS_M:
                        discarded = True
                        discard_reason = (
                            f"Passa vicino a ostacolo noto entro {min_dist:.0f} m: "
                            f"{obs['description'][:60]} ({obs['lat']:.4f},{obs['lon']:.4f})"
                        )
                        break

    # endpoint_match: solo point_to_point (Fase 11)
    if route_type == "point_to_point":
        ep_m = float(analysis.get("endpoint_match_m") or 0)
        comp["endpoint_match"] = {
            "score": max(0.0, round(100.0 * (1.0 - ep_m / 500.0), 1)),
            "placeholder": False, "source": "gpx",
        }

    # ── Punteggio totale pesato ───────────────────────────────────────────────
    total = sum(
        comp[k]["score"] * w
        for k, w in weights.items()
        if w > 0.0 and k in comp
    )

    return {
        "component_scores":       comp,
        "total_score":            round(total, 2),
        "discarded":              discarded,
        "discard_reason":         discard_reason,
        "osm_unresolved_percent": osm_unresolved,
        "osm_source":             "real" if osm_ok else ("partial" if enrichment else "missing"),
    }
