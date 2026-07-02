"""
Test C2 — GPX completo con traversal BRouter + OSM enrichment.

Verifica quantitativa che i 3 nodi dell'Area Resolver migliorino il tasso
di rilevamento trail rispetto al baseline di 8.3% (singolo waypoint approssimativo).

Pipeline:
  1. Area Resolver → 3 nodi sentiero fluviale Esino (Overpass)
  2. BRouter trekking → GPX loop: Senigallia → [3 nodi Esino] → Senigallia
  3. GPX Analyzer → distanza/dislivello
  4. OSM Tag Enricher → trail_percent, near_natural_percent (Overpass)
  5. Confronto: trail_percent vs baseline 8.3% (test Cormorano singolo waypoint)

Uso: venv/bin/python test_C2_gpx.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "app")

from area_resolver import resolve_area_traversal
from brouter_client import get_route
from gpx_analyzer import analyze_gpx
from osm_enricher import enrich_gpx

# ── Parametri ─────────────────────────────────────────────────────────────────

START = {"lat": 43.7136520, "lon": 13.2278056, "name": "Senigallia"}

# Posizione approssimativa per Area Resolver (geocoded "Marina di Montemarciano"
# dal test C, che si è rivelata la migliore delle 3 candidate)
ESINO_APPROX = (43.6507325, 13.3444526)
ESINO_AREA   = "Parco del Cormorano, sentiero fluviale fiume Esino"

BASELINE_TRAIL_PCT = 8.3   # singolo waypoint approssimativo (test Cormorano precedente)
GPX_OUT = "data/test_C2_traversal.gpx"

print("=" * 70)
print("TEST C2 — GPX completo traversal Esino")
print("=" * 70)

# ── 1. Area Resolver ──────────────────────────────────────────────────────────
print("\n[1/4] Area Resolver — cerca sentieri OSM entro 600m da Esino...")

trail_nodes = resolve_area_traversal(ESINO_AREA, ESINO_APPROX, n_points=3)

if not trail_nodes:
    print("  [ERRORE] Nessun sentiero trovato. Overpass potrebbe essere in rate-limit.")
    print("  Usa OSM_SLEEP_BETWEEN=60 o riprova tra qualche minuto.")
    sys.exit(1)

print(f"  Trovati {len(trail_nodes)} nodi:")
for i, (lat, lon) in enumerate(trail_nodes, 1):
    print(f"    [{i}] lat={lat:.6f}  lon={lon:.6f}")

# ── 2. BRouter ────────────────────────────────────────────────────────────────
print(f"\n[2/4] BRouter (trekking) — loop Senigallia → Esino traversal → Senigallia...")

# Costruisce lista (lon, lat) per BRouter: start → trail nodes → end (=start, loop)
lonlat = [(START["lon"], START["lat"])]
for lat, lon in trail_nodes:
    lonlat.append((lon, lat))
lonlat.append((START["lon"], START["lat"]))

print(f"  Waypoint BRouter ({len(lonlat)} punti):")
for i, (lon, lat) in enumerate(lonlat):
    label = "start/end" if (i == 0 or i == len(lonlat) - 1) else f"trail[{i}]"
    print(f"    [{i}] {label}: lon={lon:.6f}  lat={lat:.6f}")

Path(GPX_OUT).parent.mkdir(parents=True, exist_ok=True)
get_route(lonlat, profile="trekking", output_path=GPX_OUT)
print(f"  GPX salvato: {GPX_OUT}")

# ── 3. GPX Analyzer ───────────────────────────────────────────────────────────
print(f"\n[3/4] GPX Analyzer...")

analysis = analyze_gpx(GPX_OUT, route_type="loop")
dist_km  = analysis["distance_km"]
elev_m   = analysis["elevation_gain_m"]
closed   = analysis.get("loop_closed")
print(f"  Distanza    : {dist_km:.1f} km")
print(f"  Dislivello  : {elev_m:.0f} m")
print(f"  Loop chiuso : {closed}")

n_samples = max(1, round(dist_km))   # stima campioni (1 ogni ~1 km)

# ── 4. OSM Tag Enricher ───────────────────────────────────────────────────────
cooldown = 20
print(f"\n[4/4] OSM Tag Enricher — cooldown {cooldown}s prima di Overpass...")
time.sleep(cooldown)

enr = enrich_gpx(GPX_OUT, verbose=True, sleep_between=12.0)

print()
if enr.get("partial"):
    print(f"  [PARTIAL] {enr.get('unresolved_percent')}% non risolti — risultato non affidabile")
    sys.exit(1)

trail_pct    = enr.get("trail_percent", 0)
nat_pct      = enr.get("near_natural_percent", 0)
gravel_pct   = enr.get("gravel_percent", 0)
asphalt_pct  = enr.get("asphalt_percent", 0)
trail_count  = enr.get("trail_count", 0)
n_resolved   = enr.get("samples_resolved", 0)
n_total      = enr.get("samples_total", 0)
unres_pct    = enr.get("unresolved_percent", 0)
elapsed      = enr.get("elapsed_s", 0)

# ── 5. Confronto ──────────────────────────────────────────────────────────────
print("=" * 70)
print("RISULTATI ENRICHMENT")
print("=" * 70)
print(f"  Campioni totali    : {n_total}  ({n_resolved} risolti, {unres_pct:.1f}% non risolti)")
print(f"  Durata enrichment  : {elapsed:.0f} s")
print()
print(f"  asphalt_percent    : {asphalt_pct:.1f}%")
print(f"  gravel_percent     : {gravel_pct:.1f}%")
print(f"  trail_percent      : {trail_pct:.1f}%  ({trail_count} campioni)")
print(f"  near_natural_pct   : {nat_pct:.1f}%")
print(f"  secondary_percent  : {enr.get('secondary_percent', 0):.1f}%")
print(f"  main_road_percent  : {enr.get('main_road_percent', 0):.1f}%")
print(f"  ss16_detected      : {enr.get('ss16_detected', False)}")

print()
print("=" * 70)
print("CONFRONTO CON BASELINE")
print("=" * 70)
delta = trail_pct - BASELINE_TRAIL_PCT
direction = "+" if delta >= 0 else ""
print(f"  Baseline (singolo waypoint approssimativo) : {BASELINE_TRAIL_PCT:.1f}%")
print(f"  Traversal 3 nodi (questo test)             : {trail_pct:.1f}%")
print(f"  Delta                                      : {direction}{delta:.1f} pp")
print()

if trail_count >= 2:
    print(f"  [OK] {trail_count} campioni trail rilevati — traversal routing confermato")
elif trail_count == 1:
    print(f"  [PARZIALE] Solo 1 campione trail — BRouter ha parzialmente seguito il sentiero")
else:
    print(f"  [FAIL] 0 campioni trail — BRouter non ha usato i nodi sentiero")

if trail_pct >= BASELINE_TRAIL_PCT:
    print(f"  [OK] trail_percent migliorato o invariato rispetto al baseline")
else:
    if trail_count >= 2:
        print(f"  [INFO] trail_percent inferiore ma rotta più lunga ({dist_km:.0f} km vs ~15 km) —")
        print(f"         in assoluto {trail_count} campioni trail vs 1 nel baseline")
    else:
        print(f"  [ATTENZIONE] trail_percent inferiore al baseline")

# Salva risultato
out_data = {
    "gpx_path": GPX_OUT,
    "analysis": analysis,
    "enrichment": enr,
    "trail_nodes": trail_nodes,
    "baseline_trail_pct": BASELINE_TRAIL_PCT,
    "delta_pp": round(delta, 1),
}
Path("data/test_C2_result.json").write_text(
    json.dumps(out_data, indent=2, ensure_ascii=False)
)
print()
print(f"  Risultato salvato in data/test_C2_result.json")
