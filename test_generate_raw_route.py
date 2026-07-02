"""
Test Fase 1 — generate_raw_route() con e senza waypoint utente.

Input:  user_waypoints = ["Ostra", "43.71,13.23", "Corinaldo", "Barbara"]
Output: (a) system prompt, (b) ricerche web, (c) narrativa, (d) waypoint ordinati
"""
import json
import sys

sys.path.insert(0, "app")

from models import RouteRequest, StartPoint
from planner_agent import (
    _SYSTEM_PROMPT_RAW,
    _geocode_user_waypoints,
    build_raw_route_prompt,
    generate_raw_route,
)

# ── Costruisci la richiesta ───────────────────────────────────────────────────

request = RouteRequest(
    start=StartPoint(name="Senigallia", lat=43.7136, lon=13.2278),
    route_type="loop",
    target_km=60.0,
    distance_tolerance_km=5.0,
    user_waypoints=[],
    scenery_theme="storico_culturale",
    athletic_theme="medio",
    geographic_direction="Nord",   # Test B — waypoint devono essere a Nord (315°–45°)
)

# ── (a) Mostra il nuovo system prompt ────────────────────────────────────────

print("=" * 70)
print("(a) SYSTEM PROMPT — _SYSTEM_PROMPT_RAW")
print("=" * 70)
print(_SYSTEM_PROMPT_RAW)

# ── Geocodifica i waypoint utente (senza API call Claude) ────────────────────

print("\n" + "=" * 70)
print("Geocodifica user_waypoints...")
print("=" * 70)
geocoded = _geocode_user_waypoints(request.user_waypoints, region="Senigallia, Italia")
for wp in geocoded:
    status = "OK" if not wp["geocoding_failed"] else "FAIL"
    print(f"  [{status}]  {wp['name']:20s} → lat={wp.get('lat')}, lon={wp.get('lon')}")

# Mostra il user prompt costruito con i waypoint geocodificati
_, user_prompt = build_raw_route_prompt(request, geocoded)
print("\n" + "=" * 70)
print("USER PROMPT (build_raw_route_prompt):")
print("=" * 70)
print(user_prompt)

# ── (b) Chiama Claude e mostra i waypoint ordinati ───────────────────────────

print("\n" + "=" * 70)
print("(b) Chiamata Claude — generate_raw_route()")
print("=" * 70)

try:
    ordered, warnings, search_queries, route_narrative = generate_raw_route(request)

    if search_queries:
        print(f"\nRicerche web ({len(search_queries)}):")
        for i, q in enumerate(search_queries, 1):
            print(f"  {i}. {q!r}")

    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for w in warnings:
            print(f"  ⚠  {w}")

    if route_narrative:
        print(f"\nNarrativa percorso:\n{route_narrative}")

    print(f"\nWaypoint ordinati ({len(ordered)} totali):\n")
    for wp in ordered:
        src_label = f"[{wp.source}]"
        rat = f"  → {wp.rationale}" if getattr(wp, "rationale", None) else ""
        print(f"  {wp.order}.  {wp.role:5s}  {src_label:9s}  {wp.name:25s}  "
              f"lat={wp.lat:.5f}  lon={wp.lon:.5f}{rat}")
    print()
    print("JSON completo:")
    print(json.dumps([wp.model_dump() for wp in ordered], ensure_ascii=False, indent=2))
except Exception as exc:
    print(f"\nERRORE: {exc}")
    raise
