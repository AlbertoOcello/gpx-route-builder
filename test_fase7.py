"""
Test end-to-end Fase 7 — Candidate Generator
Uso: venv/bin/python test_fase7.py
Richiede:
  - BRouter attivo su localhost:17777
  - data/last_test_strategies.json (da test_fase6.py)
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, "app")

logging.basicConfig(
    level=logging.INFO,
    format="  %(levelname)-7s %(message)s",
)

import httpx
from candidate_generator import generate_candidates

# ── Verifica BRouter ──────────────────────────────────────────────────────────
print("Controllo BRouter su localhost:17777...")
try:
    r = httpx.get(
        "http://localhost:17777/brouter",
        params={
            "lonlats": "13.2278,43.7137|13.2300,43.7150",
            "profile": "trekking",
            "alternativeidx": 0,
            "format": "gpx",
        },
        timeout=5.0,
    )
    if r.status_code == 200:
        print("[OK] BRouter attivo\n")
    else:
        print(f"[WARN] BRouter risponde {r.status_code}: {r.text[:200]}")
except Exception as e:
    print(f"[ERRORE] BRouter non raggiungibile: {e}")
    sys.exit(1)

# ── Carica strategie geocodificate ────────────────────────────────────────────
src = Path("data/last_test_strategies.json")
if not src.exists():
    print(f"[ERRORE] {src} non trovato — esegui prima test_fase6.py")
    sys.exit(1)

data       = json.loads(src.read_text())
strategies = data["strategies"]
request    = data["request"]
target_km  = request["target_km"]
tolerance  = request.get("distance_tolerance_km", 5)

print(f"Caricate {len(strategies)} strategie da {src}")
print(f"Target: {target_km} km  (±{tolerance} km)")
print()
print("=" * 65)
print("CANDIDATE GENERATOR")
print("=" * 65)

candidates = generate_candidates(strategies, request=request)

# ── Risultati ─────────────────────────────────────────────────────────────────
print()
print("=" * 65)
print("RISULTATI")
print("=" * 65)

for c in candidates:
    status_tag = {"ok": "OK", "retried": "OK (retry)", "failed": "FALLITO"}[c["status"]]
    print(f"\nCandidato {c['id']}: {c['strategy_name']}  [{status_tag}]")
    print(f"  Profilo: {c['profile']}")

    if c["status"] == "failed":
        print(f"  Motivo fallimento: {c.get('failure_reason', '?')}")
        continue

    a = c["analysis"]
    diff = a["distance_km"] - target_km
    sign = "+" if diff >= 0 else ""

    print(f"  Distanza reale : {a['distance_km']:.1f} km  "
          f"(target {target_km} km,  diff {sign}{diff:.1f} km)")
    print(f"  Dislivello     : +{a['elevation_gain_m']:.0f} m / -{a['elevation_loss_m']:.0f} m")

    if a.get("loop_closed") is not None:
        closed  = "SI ✓" if a["loop_closed"] else "NO ✗"
        closure = a.get("closure_distance_m", "?")
        print(f"  loop_closed    : {closed}  (distanza chiusura: {closure} m)")

    if a.get("endpoint_match_m") is not None:
        print(f"  endpoint_match : {a['endpoint_match_m']} m")

    violations = a.get("violations", [])
    print(f"  Violazioni     : {violations if violations else 'nessuna'}")
    print(f"  GPX            : {c['gpx_path']}")

# ── Salva risultati ───────────────────────────────────────────────────────────
from datetime import datetime, timezone
out = Path("data/last_test_candidates.json")
out.write_text(
    json.dumps(
        {"saved_at": datetime.now(timezone.utc).isoformat(), "request": request, "candidates": candidates},
        indent=2,
        ensure_ascii=False,
        default=str,
    )
)
print(f"\nCandidati salvati in {out}")
