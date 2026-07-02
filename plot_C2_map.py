"""
Plot mappa Test C2 — traccia GPX + traversal nodes + SS16 crossing.

Genera data/test_C2_map.png
Uso: venv/bin/python plot_C2_map.py
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
from osm_enricher import sample_track

# ── Costanti ──────────────────────────────────────────────────────────────────

GPX_PATH = "data/test_C2_traversal.gpx"
OUTPUT_PNG = "data/test_C2_map.png"

TRAVERSAL_NODES = [
    (43.648993, 13.347773),   # T1
    (43.644172, 13.345757),   # T2
    (43.643047, 13.351445),   # T3
]

# ── 1. Parse GPX ──────────────────────────────────────────────────────────────

print("[1/4] Lettura GPX...")
with open(GPX_PATH) as f:
    gpx = gpxpy.parse(f)

all_pts = [
    (pt.latitude, pt.longitude)
    for track in gpx.tracks
    for seg in track.segments
    for pt in seg.points
]
lats = [p[0] for p in all_pts]
lons = [p[1] for p in all_pts]
print(f"  {len(all_pts)} punti traccia")

# Campioni enricher (ogni ~1 km, stessa logica di osm_enricher)
samples = sample_track(GPX_PATH, interval_m=1000)
print(f"  {len(samples)} campioni enricher")

# ── 2. Query Overpass: geometria SS16 ─────────────────────────────────────────

print("[2/4] Query Overpass per SS16 (bounding box)...")
time.sleep(5)   # breve pausa per rispettare il rate-limit dopo il test precedente

lat_min = min(lats) - 0.01
lat_max = max(lats) + 0.01
lon_min = min(lons) - 0.01
lon_max = max(lons) + 0.01

query = (
    f"[out:json][timeout:25];\n"
    f'way["ref"~"SS 16|SS16"]'
    f"({lat_min:.4f},{lon_min:.4f},{lat_max:.4f},{lon_max:.4f});\n"
    f"out body geom;\n"
)
raw_body = ("data=" + query).encode("utf-8")
headers  = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "curl/8.7.1",
}

ss16_ways = []
try:
    resp = httpx.post(
        "https://overpass-api.de/api/interpreter",
        content=raw_body, headers=headers, timeout=30.0,
    )
    if resp.status_code == 200:
        ss16_ways = [e for e in resp.json().get("elements", []) if e.get("type") == "way"]
        print(f"  {len(ss16_ways)} way SS16 trovate nella bbox")
    else:
        print(f"  Overpass HTTP {resp.status_code} — disegno senza SS16")
except Exception as e:
    print(f"  Errore Overpass: {e} — disegno senza SS16")

# ── 3. Punto della traccia più vicino a SS16 ──────────────────────────────────

print("[3/4] Calcolo intersezione traccia/SS16...")

ss16_nodes = [
    (g["lat"], g["lon"])
    for w in ss16_ways
    for g in w.get("geometry", [])
]

ss16_crossing     = None
ss16_crossing_dist = float("inf")

if ss16_nodes:
    for lat, lon in all_pts:
        d = min(geodesic((lat, lon), nd).meters for nd in ss16_nodes)
        if d < ss16_crossing_dist:
            ss16_crossing_dist = d
            ss16_crossing = (lat, lon)
    print(f"  Punto più vicino: ({ss16_crossing[0]:.5f}, {ss16_crossing[1]:.5f})  "
          f"dist={ss16_crossing_dist:.0f} m da SS16")

# ── 4. Plot ───────────────────────────────────────────────────────────────────

print("[4/4] Generazione immagine...")

mean_lat = sum(lats) / len(lats)
# aspect ratio corretto per lat ~43.7° (compensa la convergenza dei meridiani)
geo_aspect = 1.0 / math.cos(math.radians(mean_lat))   # ≈ 1.385

fig, ax = plt.subplots(figsize=(13, 10))

# ── Traccia GPX ──
ax.plot(
    lons, lats,
    color="#3a86ff", linewidth=1.4, alpha=0.65, zorder=2,
    label="Traccia GPX — 27.8 km trekking",
)

# ── Campioni enricher ──
s_lons = [p[1] for p in samples]
s_lats = [p[0] for p in samples]
ax.scatter(
    s_lons, s_lats,
    s=22, c="#3a86ff", alpha=0.55, zorder=3,
    label=f"Campioni enricher OSM (n={len(samples)}, ogni ~1 km)",
)

# ── SS16 (polyline per ogni way) ──
first_ss16 = True
for way in ss16_ways:
    geom = way.get("geometry", [])
    if not geom:
        continue
    g_lons = [g["lon"] for g in geom]
    g_lats = [g["lat"] for g in geom]
    ax.plot(
        g_lons, g_lats,
        color="#fb8500", linewidth=4, alpha=0.5, zorder=4, solid_capstyle="round",
        label="SS16 (geometria OSM)" if first_ss16 else "",
    )
    first_ss16 = False

# ── Punto di intersezione traccia/SS16 ──
if ss16_crossing:
    ax.scatter(
        [ss16_crossing[1]], [ss16_crossing[0]],
        s=400, c="#ffb703", marker="*", zorder=8,
        edgecolors="#e36414", linewidths=1.8,
        label=f"Incrocio traccia↔SS16 (dist={ss16_crossing_dist:.0f} m)",
    )
    ax.annotate(
        "SS16\ncrossing",
        xy=(ss16_crossing[1], ss16_crossing[0]),
        xytext=(6, 8), textcoords="offset points",
        fontsize=8.5, color="#e36414", fontweight="bold", zorder=9,
    )

# ── Nodi traversal Area Resolver ──
t_lons = [p[1] for p in TRAVERSAL_NODES]
t_lats = [p[0] for p in TRAVERSAL_NODES]
ax.scatter(
    t_lons, t_lats,
    s=220, c="#e63946", marker="D", zorder=7,
    edgecolors="#8d0801", linewidths=1.8,
    label="Nodi traversal — Area Resolver (Parco Cormorano)",
)
for i, (lat, lon) in enumerate(TRAVERSAL_NODES, 1):
    ax.annotate(
        f"  T{i}",
        xy=(lon, lat),
        fontsize=9, color="#8d0801", fontweight="bold",
        va="center", zorder=8,
    )

# ── Start / End ──
ax.scatter(
    [lons[0]], [lats[0]],
    s=220, c="#2dc653", marker="o", zorder=8,
    edgecolors="#1a5e30", linewidths=1.8,
    label="Start / End — Senigallia",
)
ax.annotate(
    "  Senigallia",
    xy=(lons[0], lats[0]),
    fontsize=9.5, color="#1a5e30", fontweight="bold",
    va="center", zorder=9,
)

# ── Formattazione assi ──
ax.set_aspect(geo_aspect)
ax.set_xlabel("Longitudine", fontsize=11)
ax.set_ylabel("Latitudine", fontsize=11)
ax.set_title(
    "Test C2  —  loop Senigallia/Esino con traversal Parco del Cormorano\n"
    "trail_percent = 7.1% (2 campioni)  ·  near_natural_pct = 89.3%  ·  SS16 rilevata (3.6%)",
    fontsize=11, pad=12,
)
ax.legend(loc="upper right", fontsize=9, framealpha=0.92, edgecolor="#cccccc")
ax.grid(True, alpha=0.22, linestyle="--", color="#888888")
ax.tick_params(labelsize=9)

plt.tight_layout()
Path(OUTPUT_PNG).parent.mkdir(parents=True, exist_ok=True)
plt.savefig(OUTPUT_PNG, dpi=150, bbox_inches="tight")
print(f"  Salvato: {OUTPUT_PNG}")
