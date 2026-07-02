"""
Analyzer base per file GPX: distanza, dislivello, loop/endpoint check.
"""
import gpxpy
from geopy.distance import geodesic


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

    result = {
        "distance_km": distance_km,
        "elevation_gain_m": elevation_gain_m,
        "elevation_loss_m": elevation_loss_m,
        "loop_closed": None,
        "endpoint_match_m": None,
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
