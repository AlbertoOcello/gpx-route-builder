"""
GPX Optimizer — genera un file GPX derivato ottimizzato per la navigazione
turn-by-turn su Garmin, a partire da un GPX già generato (Builder o esterno).

Non modifica mai il file sorgente. Nessuna chiamata di rete: lavora solo
sulla geometria già presente nel file caricato.
"""
from __future__ import annotations

import logging
import math

import gpxpy

from geopy.distance import geodesic

from gpx_analyzer import detect_climbs, gpx_creator_string

log = logging.getLogger(__name__)

OPTIMIZER_MARKER = "gpxoptimizer:v1"

ANGLE_THRESHOLD_DEG = 8.0
MAX_GAP_M = 100.0
MIN_GAP_M = 8.0
SHARP_TURN_DEG = 35.0
WAYPOINT_INTERVAL_KM = 5.0

# ── Danger Waypoints — avvisi pre-salita, solo OsmAnd ──────────────────────────
# Su OsmAnd un <wpt> GPX standard genera davvero un avviso di prossimità
# quando ci si avvicina; su Garmin Edge lo stesso waypoint diventa solo una
# "Saved location" senza alcun avviso — servirebbe un file FIT con Course
# Point, non implementato (fuori scope). Per questo add_danger_waypoints() è
# una funzione a sé, mai chiamata da optimize_gpx()/il percorso Garmin: va
# invocata esplicitamente, solo quando l'utente sceglie la variante OsmAnd.
CLIMB_DANGER_TYPE = "ClimbDanger"
DANGER_THRESHOLD_PCT_DEFAULT = 13.0   # soglia su max_200m_pct (gpx_analyzer.detect_climbs)
WARNING_DISTANCE_M_DEFAULT = 170.0    # anticipo del waypoint rispetto al tratto più duro
WARNING_DISTANCE_MIN_M = 150.0
WARNING_DISTANCE_MAX_M = 250.0


def is_already_optimized(gpx: gpxpy.gpx.GPX) -> bool:
    """True se il GPX porta già il marcatore gpxoptimizer:v1 in <keywords>."""
    return OPTIMIZER_MARKER in (gpx.keywords or "")


def _bearing_deg(p1, p2) -> float:
    lat1, lon1 = math.radians(p1.latitude), math.radians(p1.longitude)
    lat2, lon2 = math.radians(p2.latitude), math.radians(p2.longitude)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _bearing_diff_deg(b1: float, b2: float) -> float:
    diff = abs(b2 - b1) % 360
    return 360 - diff if diff > 180 else diff


def _dist_m(p1, p2) -> float:
    return geodesic((p1.latitude, p1.longitude), (p2.latitude, p2.longitude)).meters


def _thin_segment_points(
    points: list,
    angle_threshold_deg: float,
    max_gap_m: float,
    min_gap_m: float,
    sharp_turn_deg: float,
) -> list:
    """
    Densità adattiva: infittisce su curve, dirada sui rettilinei.

    cum_dist/cum_curv accumulano la distanza PERCORSA (somma dei singoli
    passi, non la beeline tra ultimo punto tenuto e quello corrente) e la
    curvatura cumulata dall'ultimo punto tenuto — su un arco ampio ma fatto
    di tanti piccoli angoli, la beeline sottostima il percorso reale e
    lasciava che MAX_GAP_M non scattasse mai (bug storico: gap fino a 700+m).
    """
    if len(points) <= 2:
        return list(points)

    kept = [points[0]]
    cum_dist = 0.0
    cum_curv = 0.0
    prev_bearing = _bearing_deg(points[0], points[1])

    for i in range(1, len(points) - 1):
        prev, p = points[i - 1], points[i]

        bearing = _bearing_deg(prev, p)
        turn = _bearing_diff_deg(bearing, prev_bearing)
        prev_bearing = bearing

        cum_curv += turn
        cum_dist += _dist_m(prev, p)

        is_sharp = turn >= sharp_turn_deg
        keep = cum_curv > angle_threshold_deg or cum_dist > max_gap_m or is_sharp

        if not keep:
            continue

        if cum_dist < min_gap_m and not is_sharp:
            continue  # troppo vicino — anti-jitter, ma non azzera gli accumulatori

        kept.append(p)
        cum_dist = 0.0
        cum_curv = 0.0

    kept.append(points[-1])
    return kept


def _validate_max_gap(original_points: list, optimized_points: list, max_gap_m: float) -> list[dict]:
    """
    Un accumulatore a soglia rileva il superamento di max_gap_m solo DOPO
    aver già fatto il passo che lo supera: un overshoot fino all'ampiezza
    del passo originale più grande nel tratto è quindi inerente
    all'algoritmo, non un bug — il gap è "giustificato" se
    gap <= max_gap_m + passo_originale_più_grande_nel_tratto.
    Un gap che eccede anche questo margine, con punti intermedi disponibili
    che sarebbero dovuti essere tenuti, è invece un vero bug del thinning.
    """
    orig_index = {id(pt): i for i, pt in enumerate(original_points)}

    violations = []
    for a, b in zip(optimized_points, optimized_points[1:]):
        gap = _dist_m(a, b)
        if gap <= max_gap_m:
            continue

        idx_a = orig_index.get(id(a))
        idx_b = orig_index.get(id(b))
        if idx_a is None or idx_b is None or idx_b <= idx_a + 1:
            continue  # nessun punto intermedio nel sorgente: gap inerente alla sparsità

        max_single_step = max(
            _dist_m(original_points[j], original_points[j + 1])
            for j in range(idx_a, idx_b)
        )
        if gap <= max_gap_m + max_single_step:
            continue  # overshoot spiegato dalla granularità del sorgente: non è un bug

        violations.append({
            "lat": a.latitude,
            "lon": a.longitude,
            "gap_m": round(gap, 1),
            "intermediate_points_dropped": idx_b - idx_a - 1,
        })

    return violations


def _build_orientation_waypoints(gpx: gpxpy.gpx.GPX, route_name: str, interval_km: float) -> list:
    """Un <wpt> muto (nessuna estensione) ogni interval_km + uno a Start."""
    all_points = [pt for track in gpx.tracks for seg in track.segments for pt in seg.points]
    if not all_points:
        return []

    waypoints = [
        gpxpy.gpx.GPXWaypoint(
            latitude=all_points[0].latitude,
            longitude=all_points[0].longitude,
            elevation=all_points[0].elevation,
            name=f"{route_name} — Start",
        )
    ]

    cumulative_km = 0.0
    next_marker_km = interval_km
    for prev, cur in zip(all_points, all_points[1:]):
        cumulative_km += _dist_m(prev, cur) / 1000.0
        if cumulative_km >= next_marker_km:
            waypoints.append(
                gpxpy.gpx.GPXWaypoint(
                    latitude=cur.latitude,
                    longitude=cur.longitude,
                    elevation=cur.elevation,
                    name=f"{route_name} — {next_marker_km:.0f} km",
                )
            )
            next_marker_km += interval_km

    return waypoints


def optimize_gpx(
    input_path: str,
    route_name: str,
    output_path: str,
    angle_threshold_deg: float = ANGLE_THRESHOLD_DEG,
    max_gap_m: float = MAX_GAP_M,
    min_gap_m: float = MIN_GAP_M,
    sharp_turn_deg: float = SHARP_TURN_DEG,
    waypoint_interval_km: float = WAYPOINT_INTERVAL_KM,
) -> dict:
    """
    Scrive in output_path una copia ottimizzata di input_path (densità
    adattiva + waypoint muti di orientamento). Il file sorgente non viene
    mai toccato. Ritorna le statistiche prima/dopo.
    """
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            gpx = gpxpy.parse(f)
    except Exception as exc:
        raise ValueError(f"File GPX non valido o illeggibile: {exc}") from exc

    if not gpx.tracks:
        raise ValueError("Il file GPX non contiene tracce (<trk>).")

    points_before = sum(len(seg.points) for track in gpx.tracks for seg in track.segments)
    if points_before == 0:
        raise ValueError("Il file GPX non contiene punti traccia.")

    gap_violations = []
    for track in gpx.tracks:
        for segment in track.segments:
            original_points = list(segment.points)
            segment.points = _thin_segment_points(
                original_points, angle_threshold_deg, max_gap_m, min_gap_m, sharp_turn_deg
            )
            gap_violations.extend(_validate_max_gap(original_points, segment.points, max_gap_m))

    if gap_violations:
        log.warning(
            "gpx_optimizer: %d gap ingiustificati > %.0fm dopo il thinning (possibile bug): %s",
            len(gap_violations), max_gap_m, gap_violations,
        )

    points_after = sum(len(seg.points) for track in gpx.tracks for seg in track.segments)

    orientation_wpts = _build_orientation_waypoints(gpx, route_name, waypoint_interval_km)
    gpx.waypoints = orientation_wpts

    optimized_name = f"{route_name} - optimized"
    gpx.name = optimized_name
    for track in gpx.tracks:
        track.name = optimized_name
    gpx.keywords = OPTIMIZER_MARKER
    gpx.creator = f"{gpx_creator_string()} — optimized"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(gpx.to_xml())

    reduction_pct = round((1 - points_after / points_before) * 100, 1) if points_before else 0.0

    return {
        "points_before": points_before,
        "points_after": points_after,
        "waypoints_added": len(orientation_wpts),
        "reduction_pct": reduction_pct,
    }


def _interpolate_along_track(
    points: list,
    cum_m: list[float],
    distance_m: float,
) -> tuple[float, float, float | None]:
    """Punto (lat, lon, ele) a distance_m lungo il tracciato, per interpolazione lineare
    tra i due punti più vicini — stesso metodo di interpolate() in analisi_salita.py."""
    distance_m = min(max(distance_m, 0.0), cum_m[-1])
    lo, hi = 0, len(points) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if cum_m[mid] <= distance_m:
            lo = mid
        else:
            hi = mid
    a, b = points[lo], points[hi]
    span = cum_m[hi] - cum_m[lo]
    t = 0.0 if span == 0 else (distance_m - cum_m[lo]) / span
    lat = a.latitude + (b.latitude - a.latitude) * t
    lon = a.longitude + (b.longitude - a.longitude) * t
    ele = None
    if a.elevation is not None and b.elevation is not None:
        ele = a.elevation + (b.elevation - a.elevation) * t
    return lat, lon, ele


def add_danger_waypoints(
    gpx_path: str,
    danger_threshold_pct: float = DANGER_THRESHOLD_PCT_DEFAULT,
    warning_distance_m: float = WARNING_DISTANCE_M_DEFAULT,
) -> dict:
    """
    Aggiunge waypoint di avviso pre-salita al GPX in gpx_path, sovrascrivendolo
    — SOLO per OsmAnd (vedi nota sopra CLIMB_DANGER_TYPE), mai chiamata dal
    percorso Garmin (optimize_gpx). Va invocata DOPO optimize_gpx(), sul file
    già ottimizzato: rileva le salite (gpx_analyzer.detect_climbs, stessa
    pipeline usata in Builder/Ride Analysis) e inserisce un waypoint
    warning_distance_m PRIMA del punto più duro (hard_start_km) di ogni
    salita con max_200m_pct >= danger_threshold_pct.

    Idempotente: rimuove prima ogni waypoint type=ClimbDanger già presente
    (aggiunto da una chiamata precedente), poi reinserisce — rilanciarla
    (anche con soglie diverse) non duplica gli avvisi.

    Ritorna {"danger_count": int, "climbs_checked": int}.
    """
    with open(gpx_path, "r", encoding="utf-8") as f:
        gpx = gpxpy.parse(f)

    gpx.waypoints = [wp for wp in gpx.waypoints if wp.type != CLIMB_DANGER_TYPE]

    track_points = [pt for track in gpx.tracks for seg in track.segments for pt in seg.points]
    if len(track_points) < 4:
        with open(gpx_path, "w", encoding="utf-8") as f:
            f.write(gpx.to_xml())
        return {"danger_count": 0, "climbs_checked": 0}

    cum_m = [0.0] * len(track_points)
    for i in range(1, len(track_points)):
        cum_m[i] = cum_m[i - 1] + _dist_m(track_points[i - 1], track_points[i])

    climb_data = detect_climbs(
        cum_m,
        [pt.elevation for pt in track_points],
        [(pt.latitude, pt.longitude) for pt in track_points],
    )

    danger_count = 0
    for climb in climb_data["climbs"]:
        if climb["max_200m_pct"] < danger_threshold_pct:
            continue

        warn_distance_m = climb["hard_start_km"] * 1000 - warning_distance_m
        warn_lat, warn_lon, warn_ele = _interpolate_along_track(track_points, cum_m, warn_distance_m)

        gpx.waypoints.append(gpxpy.gpx.GPXWaypoint(
            latitude=warn_lat,
            longitude=warn_lon,
            elevation=warn_ele,
            name=f"⚠️ DANGER {climb['max_200m_pct']:.0f}% — RAPPORTO AGILE",
            comment=f"Tra {warning_distance_m:.0f} m: 200 m al {climb['max_200m_pct']:.1f}%",
            description=(
                f"Salita critica: inserire ora il rapporto più agile. "
                f"Tratto di 200 m al {climb['max_200m_pct']:.1f}%."
            ),
            symbol="Danger Area",
            type=CLIMB_DANGER_TYPE,
        ))
        danger_count += 1

    with open(gpx_path, "w", encoding="utf-8") as f:
        f.write(gpx.to_xml())

    return {"danger_count": danger_count, "climbs_checked": len(climb_data["climbs"])}
