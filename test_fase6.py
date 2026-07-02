"""
Test end-to-end Fase 6 + 6bis
Uso: venv/bin/python test_fase6.py
Richiede: ANTHROPIC_API_KEY in .env o nell'ambiente
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "app")

from planner_agent import generate_strategies
from geocoding_agent import geocode_candidate

_OUT = Path("data/last_test_strategies.json")

request = {
    "start": {"name": "Senigallia", "lat": 43.7136520, "lon": 13.2278056},
    "target_km": 60,
    "distance_tolerance_km": 5,
    "route_type": "loop",
    "waypoints_stages": [],
    "preferred_direction": ["colline", "borghi"],
    "desired_places": [],
    "avoid_places": ["SS16"],
    "max_elevation_gain_m": 700,
    "candidate_count": 3,
}

print("=" * 60)
print("(a) PLANNER AGENT — 3 strategie JSON")
print("=" * 60)
strategies = generate_strategies(request)
for i, s in enumerate(strategies, 1):
    print(f"\nStrategia {i}: {s['name']}")
    print(f"  Profilo : {s['profile']}")
    print(f"  Tipo    : {s['route_type']}")
    print(f"  ~km     : {s['estimated_km']}")
    print(f"  Rationale: {s['rationale']}")
    print(f"  Waypoints:")
    for wp in s["waypoints"]:
        if wp.get("needs_geocoding"):
            coords = "(da geocodificare)"
        else:
            coords = f"lat={wp.get('lat')}, lon={wp.get('lon')}"
        print(f"    [{wp['role']}] {wp['name']}  {coords}")

print()
print("=" * 60)
print("(b)+(c) GEOCODING AGENT — risultati e fallimenti")
print("=" * 60)
for i, s in enumerate(strategies, 1):
    print(f"\nGeocoding strategia {i}: {s['name']}")
    geocoded = geocode_candidate(s)
    for wp in geocoded["waypoints"]:
        if wp.get("geocoding_error"):
            status = f"[FALLITO] {wp['geocoding_error']}"
        elif not wp.get("needs_geocoding") and wp.get("lat") is not None:
            status = f"[OK] lat={wp['lat']:.5f}, lon={wp['lon']:.5f}"
        else:
            status = "[PENDENTE]"
        print(f"  [{wp['role']}] {wp['name']:30s}  {status}")
    if geocoded["requires_geocoding"]:
        print("  [WARN] Rimangono waypoint non risolti in questa strategia")
    strategies[i - 1] = geocoded  # aggiorna con i dati geocodificati

payload = {
    "saved_at": datetime.now(timezone.utc).isoformat(),
    "request": request,
    "strategies": strategies,
}
_OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
print(f"\nStrategie salvate in {_OUT}")
