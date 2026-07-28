"""
GPX Optimizer — genera un file GPX derivato ottimizzato per la navigazione
turn-by-turn su Garmin, a partire da un GPX già generato (Builder o esterno).

Non modifica mai il file sorgente. Nessuna chiamata di rete: lavora solo
sulla geometria già presente nel file caricato.
"""
from __future__ import annotations

import math

import gpxpy

from geopy.distance import geodesic

OPTIMIZER_MARKER = "gpxoptimizer:v1"

ANGLE_THRESHOLD_DEG = 8.0
MAX_GAP_M = 200.0
MIN_GAP_M = 8.0
WAYPOINT_INTERVAL_KM = 5.0


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


def _turn_angle_deg(prev, p, nxt) -> float:
    """Angolo di svolta in p: 0° = rettilineo, 180° = inversione a U."""
    diff = abs(_bearing_deg(prev, p) - _bearing_deg(p, nxt)) % 360
    return 360 - diff if diff > 180 else diff


def _dist_m(p1, p2) -> float:
    return geodesic((p1.latitude, p1.longitude), (p2.latitude, p2.longitude)).meters


def _thin_segment_points(
    points: list,
    angle_threshold_deg: float,
    max_gap_m: float,
    min_gap_m: float,
) -> list:
    """Densità adattiva: infittisce su curve/rotatorie, dirada sui rettilinei."""
    if len(points) <= 2:
        return list(points)

    kept = [points[0]]
    last_kept = points[0]

    for i in range(1, len(points) - 1):
        p = points[i]
        gap_from_last = _dist_m(last_kept, p)

        if gap_from_last < min_gap_m:
            continue  # troppo vicino all'ultimo punto tenuto — evita jitter

        angle = _turn_angle_deg(points[i - 1], p, points[i + 1])
        if angle > angle_threshold_deg or gap_from_last > max_gap_m:
            kept.append(p)
            last_kept = p

    kept.append(points[-1])
    return kept


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

    for track in gpx.tracks:
        for segment in track.segments:
            segment.points = _thin_segment_points(
                segment.points, angle_threshold_deg, max_gap_m, min_gap_m
            )

    points_after = sum(len(seg.points) for track in gpx.tracks for seg in track.segments)

    orientation_wpts = _build_orientation_waypoints(gpx, route_name, waypoint_interval_km)
    gpx.waypoints = orientation_wpts

    gpx.name = route_name
    for track in gpx.tracks:
        track.name = route_name
    gpx.keywords = OPTIMIZER_MARKER

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(gpx.to_xml())

    reduction_pct = round((1 - points_after / points_before) * 100, 1) if points_before else 0.0

    return {
        "points_before": points_before,
        "points_after": points_after,
        "waypoints_added": len(orientation_wpts),
        "reduction_pct": reduction_pct,
    }
