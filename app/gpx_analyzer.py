"""
Analyzer base per file GPX: distanza, dislivello, loop/endpoint check.
"""
import math

import gpxpy
from geopy.distance import geodesic

# ── Rilevamento tratti andata/ritorno sovrapposti ("a lecca-lecca") ───────────
# Un percorso può chiudersi correttamente (start==end) ed essere comunque "falso":
# va da A a B e ripercorre la stessa strada all'indietro invece di fare un anello
# vero. loop_closed non lo rileva perché guarda solo l'endpoint, non la forma.
_OAB_MIN_GAP_M = 150.0    # distanza-lungo-tracciato minima per non essere "adiacenti"
                          # (esclude tornanti stretti/curve, che restano vicini in cumulativa)
_OAB_PROXIMITY_M = 30.0   # sotto questa distanza euclidea due punti sono "sullo stesso asse"
_OAB_MIN_RUN_M = 100.0    # lunghezza minima di un tratto sovrapposto consecutivo per contarlo
                          # (esclude incroci puntuali tipo percorso a otto)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _out_and_back_percent(
    points: list[tuple[float, float]],
    min_gap_m: float = _OAB_MIN_GAP_M,
    proximity_m: float = _OAB_PROXIMITY_M,
    min_run_m: float = _OAB_MIN_RUN_M,
) -> float:
    """
    Percentuale di tracciato che ripercorre sé stesso (andata == ritorno sullo
    stesso asse). Per ogni punto cerca il più vicino tra i punti che sono
    lontani almeno min_gap_m *lungo il tracciato* (non in linea d'aria): un
    tornante stretto ha punti vicini anche in cumulativa, quindi resta escluso;
    un vero tratto di ritorno ha punti vicini in spazio ma lontani in cumulativa.
    Conta solo run consecutivi di sovrapposizione lunghi almeno min_run_m, per
    escludere incroci puntuali (percorso a otto, breve tratto in comune tra
    due anse).
    """
    n = len(points)
    if n < 4:
        return 0.0

    cum = [0.0] * n
    for i in range(1, n):
        cum[i] = cum[i - 1] + _haversine_m(*points[i - 1], *points[i])
    total_m = cum[-1]
    if total_m <= 0:
        return 0.0

    overlap = [False] * n
    for i in range(n):
        lat_i, lon_i = points[i]
        ci = cum[i]
        for j in range(n):
            if abs(cum[j] - ci) <= min_gap_m:
                continue
            if _haversine_m(lat_i, lon_i, points[j][0], points[j][1]) < proximity_m:
                overlap[i] = True
                break

    overlapped_m = 0.0
    run_start = None
    for i in range(n):
        if overlap[i]:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                run_len = cum[i - 1] - cum[run_start]
                if run_len >= min_run_m:
                    overlapped_m += run_len
                run_start = None
    if run_start is not None:
        run_len = cum[n - 1] - cum[run_start]
        if run_len >= min_run_m:
            overlapped_m += run_len

    return round(min(100.0, 100.0 * overlapped_m / total_m), 1)


def analyze_gpx(gpx_path: str,
                 route_type: str = "loop",
                 expected_end: tuple[float, float] | None = None) -> dict:
    """
    gpx_path: percorso al file GPX da analizzare.
    route_type: "loop" | "out_and_back" | "point_to_point"
    expected_end: (lon, lat) del punto di arrivo richiesto, usato solo per point_to_point.

    Ritorna un dizionario con i campi principali di GPXAnalysis (v0.3).
    """
    with open(gpx_path, "r") as f:
        gpx = gpxpy.parse(f)

    # Estrai tutti i punti come lista
    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            points.extend(segment.points)

    if not points:
        raise ValueError("Il file GPX non contiene punti traccia")

    # Distanza totale (gpxpy la calcola in metri)
    distance_m = gpx.length_2d()
    distance_km = round(distance_m / 1000, 2)

    # Dislivello (uphill, downhill) in metri
    uphill, downhill = gpx.get_uphill_downhill()
    elevation_gain_m = round(uphill, 1)
    elevation_loss_m = round(downhill, 1)

    start = points[0]
    end = points[-1]

    out_and_back_percent = _out_and_back_percent(
        [(p.latitude, p.longitude) for p in points]
    )

    result = {
        "distance_km": distance_km,
        "elevation_gain_m": elevation_gain_m,
        "elevation_loss_m": elevation_loss_m,
        "loop_closed": None,
        "endpoint_match_m": None,
        "out_and_back_percent": out_and_back_percent,
        "violations": [],
    }

    if route_type in ("loop", "out_and_back"):
        # loop_closed: distanza tra punto iniziale e finale della traccia
        closure_distance_m = geodesic(
            (start.latitude, start.longitude),
            (end.latitude, end.longitude),
        ).meters
        result["loop_closed"] = closure_distance_m < 100  # soglia tolleranza 100m
        result["closure_distance_m"] = round(closure_distance_m, 1)

    elif route_type == "point_to_point":
        if expected_end is None:
            raise ValueError("Per point_to_point serve specificare expected_end=(lon, lat)")
        end_lon, end_lat = expected_end
        endpoint_match_m = geodesic(
            (end.latitude, end.longitude),
            (end_lat, end_lon),
        ).meters
        result["endpoint_match_m"] = round(endpoint_match_m, 1)

    return result


if __name__ == "__main__":
    analysis = analyze_gpx(
        "routes/generated/test_wrapper.gpx",
        route_type="point_to_point",
        expected_end=(13.2400, 43.7200),
    )
    print(analysis)
