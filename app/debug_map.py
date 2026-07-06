"""
debug_map.py — verifica _matplotlib_track_png_b64 e render_html_report.

Eseguire nel container:
  python app/debug_map.py [percorso/al/file.gpx]

Senza argomenti usa punti sintetici.
"""
import sys, io, base64, json, math

# Punti sintetici (anello 60 punti attorno a Senigallia)
_SYNTHETIC = [
    (43.71 + 0.03 * math.sin(i / 8), 13.22 + 0.06 * math.cos(i / 8))
    for i in range(60)
]

def main():
    # --- 1. track_points
    if len(sys.argv) > 1:
        import gpxpy
        with open(sys.argv[1], "rb") as f:
            gpx = gpxpy.parse(f)
        pts = [
            (pt.latitude, pt.longitude)
            for track in gpx.tracks
            for seg in track.segments
            for pt in seg.points
        ]
        print(f"[1] GPX caricato: {len(pts)} punti totali")
        step = max(1, len(pts) // 600)
        track_points = pts[::step]
        print(f"[1] track_points campionati: {len(track_points)}")
    else:
        track_points = _SYNTHETIC
        print(f"[1] Punti sintetici: {len(track_points)}")

    if len(track_points) < 2:
        print("[FAIL] track_points vuoti o insufficienti")
        sys.exit(1)

    # --- 2. _matplotlib_track_png_b64
    from ride_analysis_agent import _matplotlib_track_png_b64, _track_to_svg

    b64 = _matplotlib_track_png_b64(track_points)
    if b64:
        png = base64.b64decode(b64)
        print(f"[2] ✅ PNG generato: {len(png)} byte, base64 len={len(b64)}")
        assert png[:4] == b'\x89PNG', "Header PNG non valido!"
        print(f"[2] ✅ Header PNG valido")
    else:
        print("[2] ❌ _matplotlib_track_png_b64 ha restituito None — controlla i log sopra")

    # --- 3. SVG fallback
    svg = _track_to_svg(track_points)
    print(f"[3] SVG fallback: {'✅ generato' if '<polyline' in svg else '❌ vuoto'} ({len(svg)} chars)")

    # --- 4. render_html_report
    gpx_stats = {
        "distance_km": 42.0,
        "elevation_gain_m": 350.0,
        "elevation_loss_m": 350.0,
        "max_elevation_m": 180.0,
        "min_elevation_m": 5.0,
        "gpx_name": "Test Route",
        "gpx_filename": "test_route_B",
        "track_points": track_points,
    }
    profile = {
        "name": "Debug Profile",
        "bike_type": "ebike",
        "bike_model": "TestBike",
        "wh": 500,
        "battery_pct": 100,
        "min_battery_pct": 10,
        "bike_weight_kg": 22,
        "riding_style": "mixed",
        "driver_weight_kg": 75,
        "driver_age": 45,
        "driver_sex": "M",
        "driver_fitness": 3,
        "driver_fcmax": 170,
        "driver_health_notes": None,
    }
    analysis = {
        "battery_pct_consumed": 55.0,
        "range_remaining_km": 18.0,
        "estimated_assistance_level": 2.5,
        "calories_kcal": 1200,
        "time_estimate_min": 150,
        "avg_hr_bpm": 135,
        "fatigue_index": 6,
        "advice": ["Porta acqua extra", "Usa eco in pianura", "Controlla i freni"],
        "disclaimer": "Analisi indicativa.",
    }

    from ride_analysis_agent import render_html_report
    html = render_html_report(analysis, gpx_stats, profile, "it",
                              route_narrative="Percorso di test lungo la costa.")

    has_img  = 'data:image/png;base64,' in html
    has_svg  = '<polyline' in html
    has_h1   = 'route-title' in html

    print(f"[4] render_html_report: {len(html)} chars")
    print(f"    <img base64>  : {'✅' if has_img else '❌'}")
    print(f"    <svg polyline>: {'✅' if has_svg else '❌ (solo se PNG fallito)'}")
    print(f"    h1 route-title: {'✅' if has_h1 else '❌'}")

    out = "debug_report.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[4] Report scritto in {out} — aprilo nel browser per verifica visiva")

if __name__ == "__main__":
    main()
