"""
Candidate Generator (Fase 7) — chiama BRouter per ogni CandidateRoute (SRS §6.3).

Pipeline per ogni slot (A/B/C):
  1. _apply_loop_fix  — sovrascrive end con coordinate bit-identiche a start (loop)
  2. _waypoints_to_lonlat — converte in lista (lon, lat) per BRouter
  3. brouter_client.get_route — genera il GPX reale
  4. gpx_analyzer.analyze_gpx — distanza, dislivello, loop_closed, endpoint_match
  5. In caso di errore: un retry automatico via Planner + Geocoding Agent (SRS §6.3)
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from brouter_client import get_route
from gpx_analyzer import analyze_gpx
from area_resolver import resolve_area_traversal

log = logging.getLogger(__name__)

_ROUTES_DIR = Path(__file__).parent.parent / "routes" / "generated"
_SLOT_LABELS = ["A", "B", "C"]


# ─── Traversal expansion ──────────────────────────────────────────────────────

def _expand_traversal_waypoints(strategy: dict) -> dict:
    """
    Sostituisce ogni waypoint via con traversal=True con la sequenza di nodi
    del sentiero OSM reale trovata dall'Area Resolver.

    Se l'Area Resolver non trova sentieri, il waypoint originale è mantenuto.
    Require che il Geocoding Agent abbia già popolato lat/lon.
    """
    new_waypoints = []
    for wp in strategy["waypoints"]:
        if wp.get("role") != "via" or not wp.get("traversal"):
            new_waypoints.append(wp)
            continue

        lat = wp.get("lat")
        lon = wp.get("lon")
        area_hint = wp.get("area_hint") or wp.get("name", "")

        if lat is None or lon is None:
            log.warning("Traversal waypoint '%s' senza coordinate — skip espansione", wp.get("name"))
            new_waypoints.append(wp)
            continue

        log.info("Traversal: risolvo sentieri per '%s' intorno a (%.4f, %.4f)", area_hint, lat, lon)
        trail_pts = resolve_area_traversal(area_hint, (lat, lon))

        if not trail_pts:
            log.info("Traversal: nessun sentiero trovato per '%s' — uso waypoint originale", area_hint)
            new_waypoints.append(wp)
        else:
            log.info(
                "Traversal: %d nodi trovati per '%s' → espando waypoint",
                len(trail_pts), area_hint,
            )
            for i, (t_lat, t_lon) in enumerate(trail_pts):
                new_waypoints.append({
                    "role": "via",
                    "name": f"{area_hint} [{i + 1}/{len(trail_pts)}]",
                    "lat": t_lat,
                    "lon": t_lon,
                    "needs_geocoding": False,
                    "traversal": False,
                    "area_hint": None,
                    "_traversal_expanded": True,
                })

    return {**strategy, "waypoints": new_waypoints}


# ─── Loop fix ─────────────────────────────────────────────────────────────────

def _apply_loop_fix(strategy: dict) -> dict:
    """
    REGOLA LOOP obbligatoria (SRS §6.3) — fonte di verità prima di chiamare BRouter.

    Per route_type == "loop": copia lat/lon del waypoint "start" sul waypoint "end",
    rendendoli bit-identici. Nominatim restituisce spesso centroidi diversi per la
    stessa stringa (es. "Senigallia" geocodificato due volte → due coordinate diverse),
    il che causerebbe un anello aperto. BRouter richiede start == end per chiuderlo.

    Questo fix è responsabilità del Candidate Generator, non del Geocoding Agent.
    """
    if strategy["route_type"] != "loop":
        return strategy

    waypoints = list(strategy["waypoints"])
    start_wp = next((w for w in waypoints if w["role"] == "start"), None)
    end_idx  = next((i for i, w in enumerate(waypoints) if w["role"] == "end"), None)

    if start_wp is None or end_idx is None:
        log.warning("LOOP FIX: start o end mancante in '%s', skip.", strategy["name"])
        return strategy

    old_end = waypoints[end_idx]
    if old_end["lat"] != start_wp["lat"] or old_end["lon"] != start_wp["lon"]:
        log.info(
            "LOOP FIX [%s]: end (%.6f, %.6f) → (%.6f, %.6f)  [copiato da start]",
            strategy["name"],
            old_end["lat"], old_end["lon"],
            start_wp["lat"], start_wp["lon"],
        )
        waypoints[end_idx] = {**old_end, "lat": start_wp["lat"], "lon": start_wp["lon"]}
    else:
        log.info("LOOP FIX [%s]: start == end già identici, nessuna modifica.", strategy["name"])

    return {**strategy, "waypoints": waypoints}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _waypoints_to_lonlat(strategy: dict) -> list[tuple[float, float]]:
    """
    Converte i waypoint (già in ordine start→via…→end) in lista (lon, lat) per BRouter.
    Solleva ValueError se un waypoint non ha coordinate (geocoding incompleto).
    """
    result = []
    for wp in strategy["waypoints"]:
        if wp.get("lat") is None or wp.get("lon") is None:
            raise ValueError(
                f"Waypoint '{wp.get('name')}' ({wp.get('role')}) manca di coordinate — "
                "verificare che il Geocoding Agent sia stato eseguito correttamente."
            )
        result.append((float(wp["lon"]), float(wp["lat"])))
    return result


def _expected_end(strategy: dict) -> tuple[float, float] | None:
    """Ritorna (lon, lat) dell'end atteso — solo per gpx_analyzer in point_to_point."""
    if strategy["route_type"] != "point_to_point":
        return None
    end = next((w for w in strategy["waypoints"] if w["role"] == "end"), None)
    return (float(end["lon"]), float(end["lat"])) if end else None


def _log_brouter_call(strategy: dict, lonlat: list[tuple[float, float]]) -> None:
    """Stampa coordinate start/end passate a BRouter — per verifica loop fix."""
    start_c = lonlat[0]
    end_c   = lonlat[-1]
    match   = "IDENTICHE ✓" if start_c == end_c else f"DIVERSE ✗  Δ=({end_c[1]-start_c[1]:.6f}, {end_c[0]-start_c[0]:.6f})"
    log.info(
        "→ BRouter [%s] profilo=%s  wp=%d  start=(%.6f,%.6f)  end=(%.6f,%.6f)  %s",
        strategy["name"], strategy["profile"], len(lonlat),
        start_c[1], start_c[0],
        end_c[1], end_c[0],
        match,
    )


# ─── Retry via Planner Agent ──────────────────────────────────────────────────

def _get_alternative(failed_strategy: dict, request: dict) -> dict | None:
    """
    Chiede al Planner una nuova terna di strategie e restituisce quella
    con profilo diverso dal fallito (oppure la prima disponibile).
    Geocodifica il risultato prima di restituirlo.
    """
    from planner_agent import generate_strategies
    from geocoding_agent import geocode_candidate

    log.info("Retry: richiedo strategia alternativa al Planner per slot fallito...")
    try:
        candidates = generate_strategies(request)
    except Exception as exc:
        log.error("Retry Planner fallito: %s", exc)
        return None

    failed_profile = failed_strategy.get("profile")
    alt = next((c for c in candidates if c["profile"] != failed_profile), candidates[0])
    log.info("Retry: alternativa scelta → '%s' (profilo=%s)", alt["name"], alt["profile"])
    return geocode_candidate(alt)


# ─── Core ─────────────────────────────────────────────────────────────────────

def _generate_one(
    strategy: dict,
    label: str,
    run_dir: Path,
    request: dict | None,
    attempt: int,
) -> dict:
    # 1a. Espandi waypoint traversal con sentieri OSM reali (se presenti)
    if any(w.get("traversal") for w in strategy.get("waypoints", [])):
        strategy = _expand_traversal_waypoints(strategy)

    # 1b. Applica loop fix (fonte di verità finale prima di BRouter)
    strategy = _apply_loop_fix(strategy)

    lonlat   = _waypoints_to_lonlat(strategy)
    gpx_path = run_dir / f"candidate_{label}.gpx"

    _log_brouter_call(strategy, lonlat)

    try:
        # 2. Chiama BRouter
        get_route(lonlat, profile=strategy["profile"], output_path=str(gpx_path))

        # 3. Analizza GPX
        analysis = analyze_gpx(
            str(gpx_path),
            route_type=strategy["route_type"],
            expected_end=_expected_end(strategy),
        )

        return {
            "id": label,
            "strategy_name": strategy["name"],
            "profile": strategy["profile"],
            "route_type": strategy["route_type"],
            "gpx_path": str(gpx_path),
            "status": "retried" if attempt == 2 else "ok",
            "analysis": analysis,
        }

    except Exception as exc:
        failure_reason = str(exc)
        log.warning("Candidato %s fallito (attempt %d): %s", label, attempt, failure_reason)

        if attempt == 1 and request is not None:
            alt = _get_alternative(strategy, request)
            if alt is not None:
                return _generate_one(alt, label, run_dir, request=None, attempt=2)

        return {
            "id": label,
            "strategy_name": strategy["name"],
            "profile": strategy["profile"],
            "route_type": strategy["route_type"],
            "gpx_path": None,
            "status": "failed",
            "failure_reason": failure_reason,
            "analysis": None,
        }


def generate_candidates(
    strategies: list[dict],
    request: dict | None = None,
) -> list[dict]:
    """
    strategies: CandidateRoute geocodificati (output di geocode_candidate).
    request:    RouteRequest originale — necessario per retry via Planner (SRS §6.3).

    Ritorna lista con id (A/B/C), status (ok/retried/failed), gpx_path, analysis.
    Ogni candidato è indipendente: un fallimento non blocca gli altri.
    """
    run_dir = _ROUTES_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    log.info("Output GPX in: %s", run_dir)

    results = []
    for i, strategy in enumerate(strategies):
        label = _SLOT_LABELS[i] if i < len(_SLOT_LABELS) else str(i)
        result = _generate_one(strategy, label, run_dir, request=request, attempt=1)
        results.append(result)

    return results
