"""
Test B — Direzione geografica vincola i waypoint via.

Scenario: geographic_direction="Sud" con partenza da Senigallia (lat=43.7137).
Il Planner deve scegliere waypoint via con lat < 43.7137 (a sud della partenza).

Uso: venv/bin/python test_B_direction.py
"""
import json
import sys

sys.path.insert(0, "app")

from planner_agent import generate_strategies
from geocoding_agent import geocode_candidate

START_LAT = 43.7136520
START_LON = 13.2278056

request = {
    "start": {"name": "Senigallia", "lat": START_LAT, "lon": START_LON},
    "target_km": 60,
    "distance_tolerance_km": 5,
    "route_type": "loop",
    "waypoints_stages": [],
    "preferred_direction": [],
    "desired_places": [],
    "avoid_places": ["SS16"],
    "max_elevation_gain_m": 800,
    "free_text": "",
    "geographic_direction": "Sud",
}

print("=" * 65)
print("TEST B — Direzione geografica Sud")
print("=" * 65)
print(f"  Partenza: {START_LAT:.4f} lat")
print(f"  Vincolo: tutti i waypoint via devono avere lat < {START_LAT:.4f}")
print()

strategies = generate_strategies(request)

print("Geocodifica waypoint...")
geocoded = [geocode_candidate(s) for s in strategies]

via_violations = 0
via_compliant  = 0

for i, strat in enumerate(geocoded, 1):
    print(f"\nStrategia {i}: {strat['name']} [{strat['profile']}]")
    for wp in strat["waypoints"]:
        if wp["role"] != "via":
            continue
        lat = wp.get("lat")
        lon = wp.get("lon")
        geocoding_err = wp.get("geocoding_error")
        direction_ok = lat is not None and lat < START_LAT

        status = "OK" if direction_ok else ("ERR-geocoding" if geocoding_err else "FAIL-direzione")
        print(f"  via '{wp['name']}': lat={lat}  → {status}")
        if geocoding_err:
            continue
        if direction_ok:
            via_compliant += 1
        else:
            via_violations += 1

print()
print("=" * 65)
print("RISULTATO")
print("=" * 65)
total_via = via_compliant + via_violations

if total_via == 0:
    print("  [SKIP] Nessun waypoint via geocodificato correttamente")
elif via_violations == 0:
    print(f"  [OK] Tutti i {via_compliant} waypoint via sono a sud della partenza")
else:
    pct = via_compliant / total_via * 100
    print(f"  [PARZIALE] {via_compliant}/{total_via} ({pct:.0f}%) waypoint via a sud")
    print(f"  Violazioni direzione: {via_violations}")
    if pct >= 70:
        print("  → Accettabile (≥ 70% conformità)")
    else:
        print("  → FAIL (< 70% conformità)")
