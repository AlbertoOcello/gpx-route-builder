"""
Test C3 — Traversal con waypoint di approccio dall'entroterra (fix SS16).

Verifica che il Planner, con la nuova regola APPROCCIO DALL'ENTROTERRA,
inserisca un waypoint via a monte (es. Chiaravalle) prima del waypoint
traversal per il Parco del Cormorano, forzando BRouter a risalire la
valle Esino invece del corridoio costiero SS16.

Pipeline:
  1. Planner Agent → 3 strategie con approccio inland esplicito
  2. Scelta strategia con traversal + verifica waypoint approccio
  3. Geocoding Agent → coordinate waypoint
  4. Traversal expansion (Area Resolver) → nodi sentiero Esino
  5. BRouter trekking → GPX con rotta via valle Esino
  6. GPX Analyzer → distanza/dislivello
  7. OSM Tag Enricher → trail_percent, ss16_detected
  8. Mappa comparativa: vecchio (test_C2) vs nuovo (test_C3)

Confronto target:
  C2 (via costa):    trail=7.1%  ss16=True   near_natural=89.3%
  C3 (via entroterra): trail=?   ss16=False? near_natural=?

Uso: venv/bin/python test_C3_inland.py
"""
import json
import math
import sys
import time
from pathlib import Path

import gpxpy
import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from geopy.distance import geodesic

sys.path.insert(0, "app")

from planner_agent import generate_strategies
from geocoding_agent import geocode_candidate
from candidate_generator import _expand_traversal_waypoints, _apply_loop_fix
from brouter_client import get_route
from gpx_analyzer import analyze_gpx
from osm_enricher import enrich_gpx, sample_track

GPX_OUT    = "data/test_C3_traversal.gpx"
PNG_OUT    = "data/test_C3_map.png"
GPX_OLD    = "data/test_C2_traversal.gpx"
RESULT_OUT = "data/test_C3_result.json"

# Nodi T1-T2-T3 già noti (stessi dell'Area Resolver nel test C2/C3)
# Verranno anche ri-estratti live dall'Area Resolver durante l'expand traversal.
TRAVERSAL_NODES_PREV = [
    (43.648993, 13.347773),
    (43.644172, 13.345757),
    (43.643047, 13.351445),
]

# ── 1. Planner Agent ──────────────────────────────────────────────────────────

print("=" * 70)
print("TEST C3 — Traversal con approccio da entroterra (fix SS16)")
print("=" * 70)

request = {
    "start": {"name": "Senigallia", "lat": 43.7136520, "lon": 13.2278056},
    "target_km": 45,
    "distance_tolerance_km": 8,
    "route_type": "loop",
    "waypoints_stages": [],
    "preferred_direction": [],
    "desired_places": [],
    "avoid_places": ["SS16"],
    "max_elevation_gain_m": 600,
    "free_text": (
        "Voglio assolutamente attraversare il Parco del Cormorano percorrendo "
        "il sentiero fluviale lungo il fiume Esino. "
        "Evita assolutamente la SS16 — usa la valle del Esino dall'entroterra."
    ),
    "geographic_direction": "Libera",
}

print(f"\n[1/7] Planner Agent...")
strategies = generate_strategies(request)

print(f"\n  {len(strategies)} strategie generate:")
traversal_strategy = None

for i, s in enumerate(strategies, 1):
    has_traversal = any(w.get("traversal") for w in s["waypoints"])
    has_approach  = any(
        not w.get("traversal") and w.get("role") == "via"
        and j > 0                                           # non è il primo via
        and strategies[i - 1]["waypoints"][j - 1].get("traversal") if j > 0 else False
        for j, w in enumerate(s["waypoints"])
    )
    print(f"\n  Strategia {i}: {s['name']} [{s['profile']}]")
    via_names = []
    for wp in s["waypoints"]:
        tag = " [TRAVERSAL]" if wp.get("traversal") else ""
        print(f"    [{wp['role']}] {wp['name']}{tag}")
        if wp["role"] == "via":
            via_names.append(wp["name"])

    if has_traversal and traversal_strategy is None:
        traversal_strategy = s

if traversal_strategy is None:
    print("\n  [WARN] Nessuna strategia con traversal — uso la prima")
    traversal_strategy = strategies[0]

print(f"\n  → Strategia scelta: '{traversal_strategy['name']}' [{traversal_strategy['profile']}]")

# Verifica presenza waypoint approccio (via non-traversal prima del traversal)
wps = traversal_strategy["waypoints"]
via_list = [w for w in wps if w["role"] == "via"]
approach_found = False
for idx, wp in enumerate(via_list):
    if wp.get("traversal") and idx > 0 and not via_list[idx - 1].get("traversal"):
        approach_found = True
        print(f"  [OK] Waypoint approccio trovato: '{via_list[idx-1]['name']}'")
        print(f"       seguito da traversal:        '{wp['name']}'")
        break

if not approach_found:
    print("  [WARN] Nessun waypoint approccio prima del traversal — "
          "il Planner non ha applicato la regola entroterra. Continuo comunque.")

# ── 2. Geocoding ──────────────────────────────────────────────────────────────

print(f"\n[2/7] Geocoding Agent...")
geocoded = geocode_candidate(traversal_strategy)

print("  Waypoint geocodificati:")
for wp in geocoded["waypoints"]:
    err = wp.get("geocoding_error")
    coord = f"({wp.get('lat'):.4f}, {wp.get('lon'):.4f})" if wp.get("lat") else "[FAIL]"
    tag = " [TRAVERSAL]" if wp.get("traversal") else ""
    print(f"    [{wp['role']}] {wp['name']}{tag}  {coord}"
          + (f" ERR:{err}" if err else ""))

# ── 3. Traversal expansion ────────────────────────────────────────────────────

print(f"\n[3/7] Traversal expansion (Area Resolver)...")

# Fallback: se il geocoding del waypoint traversal è fallito, inietta le
# coordinate note (Marina di Montemarciano, da test C) per permettere
# comunque all'Area Resolver di girare.
FALLBACK_TRAVERSAL = (43.6507325, 13.3444526)   # Marina di Montemarciano
FALLBACK_AREA      = "Parco del Cormorano, sentiero fluviale fiume Esino"

wps = geocoded["waypoints"]
for wp in wps:
    if wp.get("traversal") and wp.get("geocoding_error"):
        print(f"  [FALLBACK] '{wp['name']}' non geocodificato → "
              f"uso coordinate note Marina di Montemarciano "
              f"({FALLBACK_TRAVERSAL[0]:.4f}, {FALLBACK_TRAVERSAL[1]:.4f})")
        wp["lat"]             = FALLBACK_TRAVERSAL[0]
        wp["lon"]             = FALLBACK_TRAVERSAL[1]
        wp["area_hint"]       = FALLBACK_AREA
        wp["geocoding_error"] = None   # rimuovi l'errore, ora ha coordinate

expanded = _expand_traversal_waypoints(geocoded)

# Recupera i nodi traversal espansi
traversal_expanded_nodes = [
    (wp["lat"], wp["lon"])
    for wp in expanded["waypoints"]
    if wp.get("_traversal_expanded")
]

if traversal_expanded_nodes:
    print(f"  Nodi traversal espansi ({len(traversal_expanded_nodes)}):")
    for i, (lat, lon) in enumerate(traversal_expanded_nodes, 1):
        print(f"    [T{i}] lat={lat:.6f}  lon={lon:.6f}")
else:
    print("  [WARN] Area Resolver non ha trovato nodi → uso TRAVERSAL_NODES_PREV")
    traversal_expanded_nodes = TRAVERSAL_NODES_PREV

# ── 4. Loop fix + BRouter ─────────────────────────────────────────────────────

print(f"\n[4/7] Loop fix e BRouter ({traversal_strategy['profile']})...")
fixed = _apply_loop_fix(expanded)

lonlat = [
    (float(wp["lon"]), float(wp["lat"]))
    for wp in fixed["waypoints"]
    if wp.get("lat") is not None and wp.get("lon") is not None
]

print(f"  Waypoint BRouter ({len(lonlat)} punti):")
wp_flat = [w for w in fixed["waypoints"] if w.get("lat") is not None]
for i, wp in enumerate(wp_flat):
    tag = " ← approccio" if (wp["role"] == "via" and not wp.get("traversal")
                              and not wp.get("_traversal_expanded")) else ""
    tag = " ← traversal" if wp.get("_traversal_expanded") else tag
    print(f"    [{i}] {wp['name']}{tag}  lon={wp['lon']:.4f} lat={wp['lat']:.4f}")

Path(GPX_OUT).parent.mkdir(parents=True, exist_ok=True)
get_route(lonlat, profile=traversal_strategy["profile"], output_path=GPX_OUT)
print(f"  GPX salvato: {GPX_OUT}")

# ── 5. GPX Analyzer ───────────────────────────────────────────────────────────

print(f"\n[5/7] GPX Analyzer...")
analysis = analyze_gpx(GPX_OUT, route_type="loop")
print(f"  Distanza    : {analysis['distance_km']:.1f} km")
print(f"  Dislivello  : {analysis['elevation_gain_m']:.0f} m")
print(f"  Loop chiuso : {analysis.get('loop_closed')}")

# ── 6. OSM Tag Enricher ───────────────────────────────────────────────────────

cooldown = 25
print(f"\n[6/7] OSM Tag Enricher — cooldown {cooldown}s...")
time.sleep(cooldown)

enr = enrich_gpx(GPX_OUT, verbose=True, sleep_between=12.0)
print()
if enr.get("partial"):
    print(f"  [PARTIAL] {enr.get('unresolved_percent')}% non risolti")

trail_pct   = enr.get("trail_percent",        0)
nat_pct     = enr.get("near_natural_percent", 0)
ss16        = enr.get("ss16_detected",         False)
main_pct    = enr.get("main_road_percent",     0)
sec_pct     = enr.get("secondary_percent",     0)
asph_pct    = enr.get("asphalt_percent",       0)
grav_pct    = enr.get("gravel_percent",        0)
n_resolved  = enr.get("samples_resolved",      0)
n_total     = enr.get("samples_total",         0)

# ── 7. Query SS16 per mappa ──────────────────────────────────────────────────

print(f"\n[7/7] Query Overpass SS16 per mappa (5s cooldown)...")
time.sleep(5)

samples_new = sample_track(GPX_OUT, interval_m=1000)
all_pts_new = [
    (pt.latitude, pt.longitude)
    for track in gpxpy.parse(open(GPX_OUT)).tracks
    for seg in track.segments
    for pt in seg.points
]
lats_new = [p[0] for p in all_pts_new]
lons_new = [p[1] for p in all_pts_new]

# Bbox che copre entrambe le rotte (vecchia + nuova)
all_lats = lats_new
all_lons = lons_new
if Path(GPX_OLD).exists():
    old_pts = [
        (pt.latitude, pt.longitude)
        for track in gpxpy.parse(open(GPX_OLD)).tracks
        for seg in track.segments
        for pt in seg.points
    ]
    all_lats += [p[0] for p in old_pts]
    all_lons += [p[1] for p in old_pts]

bbox = (min(all_lats) - 0.01, min(all_lons) - 0.01,
        max(all_lats) + 0.01, max(all_lons) + 0.01)

query = (
    f"[out:json][timeout:25];\n"
    f'way["ref"~"SS 16|SS16"]'
    f"({bbox[0]:.4f},{bbox[1]:.4f},{bbox[2]:.4f},{bbox[3]:.4f});\n"
    f"out body geom;\n"
)
raw_body = ("data=" + query).encode("utf-8")
headers  = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "curl/8.7.1"}

ss16_ways = []
try:
    resp = httpx.post("https://overpass-api.de/api/interpreter",
                      content=raw_body, headers=headers, timeout=30.0)
    if resp.status_code == 200:
        ss16_ways = [e for e in resp.json().get("elements", []) if e.get("type") == "way"]
        print(f"  {len(ss16_ways)} way SS16 in bbox")
    else:
        print(f"  HTTP {resp.status_code} — mappa senza SS16")
except Exception as e:
    print(f"  Errore: {e}")

ss16_nodes_all = [
    (g["lat"], g["lon"]) for w in ss16_ways for g in w.get("geometry", [])
]

# Crossing più vicino sulla nuova traccia
ss16_crossing_new     = None
ss16_crossing_new_dist = float("inf")
for lat, lon in all_pts_new:
    d = min((geodesic((lat, lon), nd).meters for nd in ss16_nodes_all), default=float("inf"))
    if d < ss16_crossing_new_dist:
        ss16_crossing_new_dist = d
        ss16_crossing_new = (lat, lon)

# ── Risultati numerici ────────────────────────────────────────────────────────

print()
print("=" * 70)
print("CONFRONTO C2 (via costa) vs C3 (via entroterra)")
print("=" * 70)
print(f"  {'Metrica':<28} {'C2 (costa)':>14} {'C3 (entroterra)':>16}")
print(f"  {'-'*28} {'-'*14} {'-'*16}")
print(f"  {'Distanza (km)':<28} {'27.8':>14} {analysis['distance_km']:>16.1f}")
print(f"  {'trail_percent (%)':<28} {'7.1':>14} {trail_pct:>16.1f}")
print(f"  {'near_natural_pct (%)':<28} {'89.3':>14} {nat_pct:>16.1f}")
print(f"  {'main_road_percent (%)':<28} {'3.6':>14} {main_pct:>16.1f}")
print(f"  {'secondary_percent (%)':<28} {'0.0':>14} {sec_pct:>16.1f}")
print(f"  {'ss16_detected':<28} {'True':>14} {str(ss16):>16}")
print(f"  {'dist_min a SS16 (m)':<28} {'0':>14} {ss16_crossing_new_dist:>16.0f}")

# ── Plot ─────────────────────────────────────────────────────────────────────

mean_lat  = sum(all_lats) / len(all_lats)
geo_aspect = 1.0 / math.cos(math.radians(mean_lat))

fig, ax = plt.subplots(figsize=(14, 11))

# SS16
first_ss16 = True
for way in ss16_ways:
    geom = way.get("geometry", [])
    if not geom:
        continue
    ax.plot(
        [g["lon"] for g in geom], [g["lat"] for g in geom],
        color="#fb8500", linewidth=4, alpha=0.45, zorder=2, solid_capstyle="round",
        label="SS16 (geometria OSM)" if first_ss16 else "",
    )
    first_ss16 = False

# Traccia C2 (vecchia, grigia, per confronto)
if Path(GPX_OLD).exists():
    old_lons = [p[1] for p in old_pts]
    old_lats = [p[0] for p in old_pts]
    ax.plot(
        old_lons, old_lats,
        color="#999999", linewidth=1.2, alpha=0.5, zorder=3, linestyle="--",
        label="C2 — via costa (ss16=True, trail=7.1%)",
    )

# Traccia C3 (nuova, blu)
ax.plot(
    lons_new, lats_new,
    color="#3a86ff", linewidth=1.8, alpha=0.80, zorder=4,
    label=f"C3 — via entroterra (ss16={ss16}, trail={trail_pct:.1f}%)",
)

# Campioni enricher C3
s_lons = [p[1] for p in samples_new]
s_lats = [p[0] for p in samples_new]
ax.scatter(s_lons, s_lats, s=20, c="#3a86ff", alpha=0.5, zorder=5,
           label=f"Campioni enricher C3 (n={len(samples_new)})")

# Crossing SS16 sulla nuova traccia
if ss16_crossing_new:
    marker = "✓ lontano" if ss16_crossing_new_dist > 100 else "⚠ vicino"
    ax.scatter(
        [ss16_crossing_new[1]], [ss16_crossing_new[0]],
        s=350, c="#ffb703" if ss16_crossing_new_dist < 100 else "#aaffaa",
        marker="*", zorder=9,
        edgecolors="#e36414" if ss16_crossing_new_dist < 100 else "#33aa33",
        linewidths=1.8,
        label=f"Punto più vicino a SS16 — C3 (dist={ss16_crossing_new_dist:.0f} m)  {marker}",
    )

# Nodi traversal (stessi T1-T2-T3 dell'Area Resolver)
t_nodes = traversal_expanded_nodes if traversal_expanded_nodes else TRAVERSAL_NODES_PREV
t_lons = [p[1] for p in t_nodes]
t_lats = [p[0] for p in t_nodes]
ax.scatter(
    t_lons, t_lats,
    s=220, c="#e63946", marker="D", zorder=8,
    edgecolors="#8d0801", linewidths=1.8,
    label="Nodi traversal — Area Resolver (Parco Cormorano)",
)
for i, (lat, lon) in enumerate(t_nodes, 1):
    ax.annotate(f"  T{i}", (lon, lat), fontsize=9, color="#8d0801",
                fontweight="bold", va="center", zorder=9)

# Waypoint approccio (via non-traversal, non-expanded, nel mezzo della rotta)
approach_wp = next(
    (w for w in fixed["waypoints"]
     if w["role"] == "via"
     and not w.get("traversal")
     and not w.get("_traversal_expanded")
     and w.get("lat") is not None),
    None,
)
if approach_wp:
    ax.scatter(
        [approach_wp["lon"]], [approach_wp["lat"]],
        s=180, c="#8ecae6", marker="^", zorder=8,
        edgecolors="#023e8a", linewidths=1.8,
        label=f"Waypoint approccio: {approach_wp['name']}",
    )
    ax.annotate(
        f"  {approach_wp['name'].split(',')[0]}",
        (approach_wp["lon"], approach_wp["lat"]),
        fontsize=9, color="#023e8a", fontweight="bold", va="center", zorder=9,
    )

# Start / End
ax.scatter(
    [lons_new[0]], [lats_new[0]],
    s=220, c="#2dc653", marker="o", zorder=9,
    edgecolors="#1a5e30", linewidths=1.8,
    label="Start/End — Senigallia",
)
ax.annotate("  Senigallia", (lons_new[0], lats_new[0]),
            fontsize=9.5, color="#1a5e30", fontweight="bold", va="center", zorder=10)

ss16_verdict = "EVITATA ✓" if not ss16 else "RILEVATA ✗"
ax.set_aspect(geo_aspect)
ax.set_xlabel("Longitudine", fontsize=11)
ax.set_ylabel("Latitudine", fontsize=11)
ax.set_title(
    f"Test C3  —  approccio dall'entroterra via valle Esino\n"
    f"trail={trail_pct:.1f}%  ·  near_natural={nat_pct:.1f}%  ·  "
    f"SS16 {ss16_verdict}  ·  dist_SS16≥{ss16_crossing_new_dist:.0f} m\n"
    f"(linea tratteggiata grigia = C2 via costa per confronto)",
    fontsize=10.5, pad=12,
)
ax.legend(loc="upper right", fontsize=8.5, framealpha=0.92, edgecolor="#cccccc")
ax.grid(True, alpha=0.22, linestyle="--", color="#888888")
ax.tick_params(labelsize=9)

plt.tight_layout()
plt.savefig(PNG_OUT, dpi=150, bbox_inches="tight")
print(f"\n  Mappa salvata: {PNG_OUT}")

# Salva JSON
Path(RESULT_OUT).write_text(json.dumps({
    "strategy_name": traversal_strategy["name"],
    "profile": traversal_strategy["profile"],
    "approach_found": approach_found,
    "approach_wp": approach_wp["name"] if approach_wp else None,
    "traversal_nodes": traversal_expanded_nodes,
    "analysis": analysis,
    "enrichment": enr,
    "ss16_crossing_dist_m": ss16_crossing_new_dist,
    "comparison": {
        "C2": {"trail_pct": 7.1, "ss16": True,  "near_natural": 89.3, "dist_km": 27.8},
        "C3": {"trail_pct": trail_pct, "ss16": ss16, "near_natural": nat_pct,
               "dist_km": analysis["distance_km"]},
    },
}, indent=2, ensure_ascii=False))
print(f"  Risultato salvato: {RESULT_OUT}")
