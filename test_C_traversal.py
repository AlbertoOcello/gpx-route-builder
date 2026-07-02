"""
Test C — Traversal waypoint: Parco del Cormorano + sentiero fluviale Esino.

Scenario: free_text menziona l'attraversamento del Parco del Cormorano.
Il Planner deve:
  1. Inserire un waypoint via con traversal=True e area_hint="Parco del Cormorano"
     (o simile riferimento al Cormorano/Esino)

Il Geocoding Agent geocodifica la posizione approssimativa dell'area.
L'Area Resolver trova sentieri OSM reali (path/track) entro 600m.
Il risultato mostra i nodi del sentiero trovati.

Uso: venv/bin/python test_C_traversal.py
"""
import sys

sys.path.insert(0, "app")

from planner_agent import generate_strategies
from geocoding_agent import geocode_candidate
from area_resolver import resolve_area_traversal

request = {
    "start": {"name": "Senigallia", "lat": 43.7136520, "lon": 13.2278056},
    "target_km": 40,
    "distance_tolerance_km": 5,
    "route_type": "loop",
    "waypoints_stages": [],
    "preferred_direction": [],
    "desired_places": [],
    "avoid_places": ["SS16"],
    "max_elevation_gain_m": 600,
    "free_text": (
        "Voglio assolutamente attraversare il Parco del Cormorano percorrendo "
        "il sentiero fluviale lungo il fiume Esino. È la parte più bella del percorso."
    ),
    "geographic_direction": "Libera",
}

print("=" * 65)
print("TEST C — Traversal: Parco Cormorano / sentiero Esino")
print("=" * 65)
print(f"  free_text: \"{request['free_text']}\"")
print()

strategies = generate_strategies(request)

traversal_found = False
traversal_details = []

for i, s in enumerate(strategies, 1):
    print(f"Strategia {i}: {s['name']} [{s['profile']}]")
    for wp in s["waypoints"]:
        if wp.get("traversal"):
            hint = wp.get("area_hint", "")
            print(f"  [TRAVERSAL] via='{wp['name']}' area_hint='{hint}'")
            traversal_found = True
            traversal_details.append((s, wp))
        else:
            print(f"  via '{wp.get('name', wp['role'])}'")
    print()

print("=" * 65)
print("GEOCODIFICA + AREA RESOLVER")
print("=" * 65)

resolved_count = 0
for strat, trav_wp in traversal_details:
    geocoded = geocode_candidate(strat)

    # Trova il waypoint traversal geocodificato
    for wp in geocoded["waypoints"]:
        if wp.get("name") == trav_wp["name"] and wp.get("traversal"):
            lat = wp.get("lat")
            lon = wp.get("lon")
            area = wp.get("area_hint", wp["name"])
            print(f"\n  Traversal waypoint: '{wp['name']}'")
            print(f"  Pos. approssimativa: ({lat}, {lon})")

            if lat and lon:
                print(f"  Area Resolver → cerca sentieri entro 600m...")
                pts = resolve_area_traversal(area, (lat, lon))
                if pts:
                    print(f"  Trovati {len(pts)} nodi sul sentiero:")
                    for j, (t_lat, t_lon) in enumerate(pts, 1):
                        print(f"    [{j}] lat={t_lat:.6f}  lon={t_lon:.6f}")
                    resolved_count += 1
                else:
                    print(f"  [AVVISO] Nessun sentiero trovato — "
                          f"verifica posizione o prova con OSM_SLEEP_BETWEEN=30")
            else:
                err = wp.get("geocoding_error", "sconosciuto")
                print(f"  [ERR] Geocoding fallito: {err}")

print()
print("=" * 65)
print("RISULTATO")
print("=" * 65)

passed = 0
total  = 2

if traversal_found:
    print(f"  [OK] Traversal waypoint trovato in almeno 1 strategia")
    passed += 1
else:
    print("  [FAIL] Nessun waypoint traversal generato dal Planner")

if resolved_count > 0:
    print(f"  [OK] Area Resolver ha trovato sentieri reali per {resolved_count} waypoint traversal")
    passed += 1
else:
    print("  [FAIL] Area Resolver non ha trovato sentieri (Overpass potrebbe essere in rate-limit)")

print()
print(f"  Passati: {passed}/{total}")
