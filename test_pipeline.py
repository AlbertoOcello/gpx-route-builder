"""
Test pipeline completa Fase 6 → 6bis → 7
Uso: venv/bin/python test_pipeline.py
"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "app")

logging.basicConfig(level=logging.INFO, format="  %(levelname)-7s %(message)s")

import httpx

# ── Verifica BRouter ──────────────────────────────────────────────────────────
print("Verifica BRouter su localhost:17777...")
try:
    r = httpx.get(
        "http://localhost:17777/brouter",
        params={"lonlats": "13.2278,43.7137|13.2300,43.7150",
                "profile": "trekking", "alternativeidx": 0, "format": "gpx"},
        timeout=5.0,
    )
    print(f"[OK] BRouter attivo (HTTP {r.status_code})\n")
except Exception as e:
    print(f"[ERRORE] BRouter non raggiungibile: {e}")
    sys.exit(1)

from planner_agent import generate_strategies
from geocoding_agent import geocode_candidate
from candidate_generator import generate_candidates

REQUEST = {
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
TARGET_KM  = REQUEST["target_km"]
TOLERANCE  = REQUEST["distance_tolerance_km"]

# ── Fase 6: Planner Agent ─────────────────────────────────────────────────────
print("=" * 65)
print("FASE 6 — Planner Agent (Claude API)")
print("=" * 65)
strategies = generate_strategies(REQUEST)
for i, s in enumerate(strategies, 1):
    print(f"\nStrategia {i}: {s['name']}  [{s['profile']}]")
    print(f"  Rationale: {s['rationale']}")
    print(f"  Waypoints ({len(s['waypoints'])}):", end="")
    for wp in s["waypoints"]:
        print(f"  [{wp['role']}] {wp['name']}", end="")
    print()

# ── Fase 6bis: Geocoding Agent ────────────────────────────────────────────────
print()
print("=" * 65)
print("FASE 6bis — Geocoding Agent (Nominatim)")
print("=" * 65)
geocoded = []
for s in strategies:
    g = geocode_candidate(s)
    geocoded.append(g)
    errors = [w for w in g["waypoints"] if w.get("geocoding_error")]
    ok     = [w for w in g["waypoints"] if not w.get("needs_geocoding") and not w.get("geocoding_error")]
    print(f"  {g['name']}: {len(ok)} OK, {len(errors)} errori"
          + (f"  — {[e['geocoding_error'] for e in errors]}" if errors else ""))

Path("data/last_test_strategies.json").write_text(
    json.dumps({"saved_at": datetime.now(timezone.utc).isoformat(),
                "request": REQUEST, "strategies": geocoded},
               indent=2, ensure_ascii=False)
)
print("  → strategie geocodificate salvate in data/last_test_strategies.json")

# ── Fase 7: Candidate Generator ───────────────────────────────────────────────
print()
print("=" * 65)
print("FASE 7 — Candidate Generator (BRouter)")
print("=" * 65)
candidates = generate_candidates(geocoded, request=REQUEST)

# ── Tabella risultati ─────────────────────────────────────────────────────────
print()
print("=" * 65)
print("RISULTATI — distanza reale vs target")
print("=" * 65)
print(f"{'#':<4} {'Profilo':<10} {'km reali':>9} {'diff':>8} {'entro ±5':>9} {'entro ±10':>10} {'loop_closed':>12} {'status'}")
print("-" * 65)

within_5  = 0
within_10 = 0
for c in candidates:
    if c["status"] == "failed":
        print(f"  {c['id']}  {c['profile']:<10}  {'—':>8}  {'—':>7}  {'—':>8}  {'—':>9}  {'—':>11}  FALLITO")
        continue
    a    = c["analysis"]
    dist = a["distance_km"]
    diff = dist - TARGET_KM
    sign = "+" if diff >= 0 else ""
    w5   = abs(diff) <= TOLERANCE
    w10  = abs(diff) <= 10
    lc   = "SI ✓" if a.get("loop_closed") else "NO ✗"
    if w5:  within_5  += 1
    if w10: within_10 += 1
    print(
        f"  {c['id']}  {c['profile']:<10}  {dist:>7.1f}km  "
        f"{sign}{diff:>5.1f}km  {'SI' if w5 else 'NO':>8}  "
        f"{'SI' if w10 else 'NO':>9}  {lc:>11}  {c['status']}"
    )

print("-" * 65)
print(f"  Entro ±{TOLERANCE} km: {within_5}/3    Entro ±10 km: {within_10}/3")
print(f"  {'✓ Obiettivo raggiunto' if within_5 >= 2 else ('~ Miglioramento (±10 km)' if within_10 >= 2 else '✗ Distanza ancora fuori target')}")

# ── Salva candidati ───────────────────────────────────────────────────────────
out = Path("data/last_test_candidates.json")
out.write_text(
    json.dumps({"saved_at": datetime.now(timezone.utc).isoformat(),
                "request": REQUEST, "candidates": candidates},
               indent=2, ensure_ascii=False, default=str)
)
print(f"\nCandidati salvati in {out}")
