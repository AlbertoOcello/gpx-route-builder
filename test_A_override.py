"""
Test A — Testo libero sovrascrive parametri strutturati.

Scenario: max_elevation_gain_m=700 nel form, ma le note libere dicono "va bene
fino a 850m". Il Planner deve:
  - Seguire il testo libero (max effettivo = 850m)
  - Popolare free_text_overrides: ["max_elevation_gain_m: 700 → 850"]
  - Impostare max_elevation_gain_m_effective: 850

Uso: venv/bin/python test_A_override.py
"""
import json
import sys

sys.path.insert(0, "app")

from planner_agent import generate_strategies

request = {
    "start": {"name": "Senigallia", "lat": 43.7136520, "lon": 13.2278056},
    "target_km": 60,
    "distance_tolerance_km": 5,
    "route_type": "loop",
    "waypoints_stages": [],
    "preferred_direction": ["colline"],
    "desired_places": [],
    "avoid_places": ["SS16"],
    "max_elevation_gain_m": 700,
    "free_text": (
        "Va bene anche fino a 850m di dislivello se il percorso è più bello, "
        "non preoccuparti del limite dei 700m che ho messo nel form"
    ),
    "geographic_direction": "Libera",
}

print("=" * 65)
print("TEST A — Free text override (max_elevation_gain_m 700 → 850)")
print("=" * 65)
print(f"  free_text: \"{request['free_text']}\"")
print()

strategies = generate_strategies(request)

overrides_found = 0
effective_found = 0

for i, s in enumerate(strategies, 1):
    overrides = s.get("free_text_overrides", [])
    effective = s.get("max_elevation_gain_m_effective")
    print(f"Strategia {i}: {s['name']} [{s['profile']}]")
    print(f"  rationale       : {s['rationale']}")
    print(f"  free_text_overrides     : {overrides}")
    print(f"  max_elevation_gain_m_effective: {effective}")
    print()

    if overrides:
        overrides_found += 1
    if effective and effective != 700:
        effective_found += 1

print("=" * 65)
print("RISULTATO")
print("=" * 65)
passed = 0
total  = 2

# Test 1: almeno 1 strategia ha free_text_overrides non vuoto
if overrides_found > 0:
    print(f"  [OK] free_text_overrides popolato in {overrides_found}/3 strategie")
    passed += 1
else:
    print("  [FAIL] Nessuna strategia ha free_text_overrides non vuoto")

# Test 2: almeno 1 strategia ha max_elevation_gain_m_effective > 700
if effective_found > 0:
    print(f"  [OK] max_elevation_gain_m_effective != 700 in {effective_found}/3 strategie")
    passed += 1
else:
    print("  [FAIL] Nessuna strategia ha max_elevation_gain_m_effective diverso da 700")

print()
print(f"  Passati: {passed}/{total}")
