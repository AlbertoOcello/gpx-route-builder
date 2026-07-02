"""
Test Scoring Fix — verifica le tre correzioni allo Scoring Engine / OSM Enricher.

Fix 1  _assign_nearest(): preferisce ciclabile a strada principale entro 20 m
Fix 2  _classify():       ss16_detected solo su carreggiata motorizzata (non ciclabili)
Fix 3  aggregate():       sconto 30% per attraversamenti isolati (1 campione); ss16_detected
                          solo per presenza sostenuta (≥2 campioni consecutivi)

Uso: venv/bin/python test_scoring_fix.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "app")

from osm_enricher import _classify, _consecutive_runs, aggregate, enrich_gpx
from scoring_engine import score_candidate

# ═══════════════════════════════════════════════════════════════════════════════
# SEZIONE 1 — Unit test sintetici (nessuna chiamata Overpass)
# ═══════════════════════════════════════════════════════════════════════════════

print("=" * 70)
print("SEZIONE 1 — Unit test sintetici (no Overpass)")
print("=" * 70)

# ── 1a. _consecutive_runs ────────────────────────────────────────────────────
print("\n[1a] _consecutive_runs()")

cases = [
    ([False, False, True, False, False],   (1, 0), "1 isolato"),
    ([False, True, True, False, False],    (0, 2), "1 run da 2"),
    ([True, True, True, False, True],      (1, 3), "1 run da 3 + 1 isolato"),
    ([False, False, False, False, False],  (0, 0), "tutti False"),
    ([True, True, True, True, True],       (0, 5), "tutti True"),
    ([True, False, True, False, True],     (3, 0), "tre isolati"),
]

ok = True
for flags, expected, label in cases:
    result = _consecutive_runs(flags)
    status = "OK" if result == expected else "FAIL"
    if status == "FAIL":
        ok = False
    print(f"  {status}  {label:30s}  atteso={expected}  got={result}")

print()

# ── 1b. _classify() — ss16_detected non si attiva su ciclabili ───────────────
print("[1b] _classify() — ss16_detected su ciclabili")

cycleway_ss16 = _classify({"highway": "cycleway", "ref": "SS16", "surface": "asphalt"})
primary_ss16  = _classify({"highway": "primary",  "ref": "SS16", "surface": "asphalt"})
path_bike     = _classify({"highway": "path", "bicycle": "designated", "ref": "SS16"})

print(f"  highway=cycleway  ref=SS16  → ss16_detected={cycleway_ss16['ss16_detected']}  "
      f"is_trail={cycleway_ss16['is_trail']}  is_main_road={cycleway_ss16['is_main_road']}")
print(f"  highway=primary   ref=SS16  → ss16_detected={primary_ss16['ss16_detected']}  "
      f"is_trail={primary_ss16['is_trail']}  is_main_road={primary_ss16['is_main_road']}")
print(f"  highway=path+bike ref=SS16  → ss16_detected={path_bike['ss16_detected']}  "
      f"is_trail={path_bike['is_trail']}  is_main_road={path_bike['is_main_road']}")

assert not cycleway_ss16["ss16_detected"], "FAIL: cycleway con ref=SS16 non deve triggherare ss16_detected"
assert     primary_ss16["ss16_detected"],  "FAIL: primary con ref=SS16 deve triggherare ss16_detected"
assert not path_bike["ss16_detected"],     "FAIL: path+bicycle=designated con ref=SS16 non deve triggherare"
assert     cycleway_ss16["is_trail"],      "FAIL: cycleway deve essere is_trail"
assert not cycleway_ss16["is_main_road"],  "FAIL: cycleway non deve essere is_main_road"
print("  → Tutti OK")
print()

# ── 1c. aggregate() — sconto attraversamento isolato ─────────────────────────
print("[1c] aggregate() — sconto per attraversamento isolato (Fix 3)")

def make_class(is_main, is_trail=False, ss16=False):
    return {
        "is_asphalt": True, "is_gravel": False, "is_cobblestone": False,
        "is_main_road": is_main, "is_secondary": False,
        "is_trail": is_trail, "is_pedestrian": False,
        "ss16_detected": ss16, "inferred": False,
    }

# Scenario A: 1 campione isolato su SS16 tra 9 campioni normali
classes_a = (
    [make_class(False)] * 4
    + [make_class(True, ss16=True)]   # 1 campione isolato
    + [make_class(False)] * 5
)
agg_a = aggregate(classes_a, n_total=10)
print(f"  Scenario A (1 campione isolato su SS16 / 10 totali):")
print(f"    main_road_percent_raw    = {agg_a['main_road_percent_raw']:.1f}%  "
      f"(atteso 10.0%)")
print(f"    main_road_percent        = {agg_a['main_road_percent']:.1f}%  "
      f"(atteso 3.0% = 0.3×10%)")
print(f"    ss16_detected            = {agg_a['ss16_detected']}  "
      f"(atteso False — 1 isolato non basta)")
print(f"    ss16_isolated_count      = {agg_a['ss16_isolated_count']}")
print(f"    ss16_sustained_count     = {agg_a['ss16_sustained_count']}")

# Scenario B: 2 campioni consecutivi su SS16
classes_b = (
    [make_class(False)] * 4
    + [make_class(True, ss16=True), make_class(True, ss16=True)]   # run di 2
    + [make_class(False)] * 4
)
agg_b = aggregate(classes_b, n_total=10)
print(f"\n  Scenario B (2 campioni consecutivi su SS16 / 10 totali):")
print(f"    main_road_percent_raw    = {agg_b['main_road_percent_raw']:.1f}%  "
      f"(atteso 20.0%)")
print(f"    main_road_percent        = {agg_b['main_road_percent']:.1f}%  "
      f"(atteso 20.0% — tratto sostenuto, nessuno sconto)")
print(f"    ss16_detected            = {agg_b['ss16_detected']}  "
      f"(atteso True — run di 2)")
print(f"    ss16_sustained_count     = {agg_b['ss16_sustained_count']}")

assert agg_a["main_road_percent_raw"] == 10.0
assert agg_a["main_road_percent"]     ==  3.0
assert not agg_a["ss16_detected"]
assert     agg_b["ss16_detected"]
print()

# ── 1d. Impatto sul traffic_score ─────────────────────────────────────────────
print("[1d] Impatto su traffic_score()")

def fake_enrich(main_pct, sec_pct=16.7):
    return {"main_road_percent": main_pct, "secondary_percent": sec_pct,
            "partial": False, "ss16_detected": False}

from scoring_engine import _traffic_score   # noqa: E402 (import dopo sys.path)

ts_old  = _traffic_score(fake_enrich(10.0))   # scenario A prima del fix
ts_new  = _traffic_score(fake_enrich( 3.0))   # scenario A dopo il fix (sconto isolato)
ts_none = _traffic_score(fake_enrich( 0.0))   # nessuna strada principale

print(f"  traffic_score con 10% main (pre-fix):  {ts_old:.1f}  "
      f"→ penalità SS16 singolo attraversamento")
print(f"  traffic_score con  3% main (post-fix): {ts_new:.1f}  "
      f"→ attraversamento scontato al 30%")
print(f"  traffic_score con  0% main (ottimale): {ts_none:.1f}")
print(f"  Miglioramento per l'attraversamento isolato: +{ts_new - ts_old:.1f} pt")
print()

if ok:
    print("  ✔  Tutti gli unit test sintetici superati.")
else:
    print("  ✘  Alcuni unit test FALLITI — controlla i fix.")

# ═══════════════════════════════════════════════════════════════════════════════
# SEZIONE 2 — Re-run enricher su GPX reali
# ═══════════════════════════════════════════════════════════════════════════════

GPX_C2 = "data/test_C2_traversal.gpx"
GPX_C3 = "data/test_C3_traversal.gpx"

OLD_C2 = Path("data/test_C2_result.json")
OLD_C3 = Path("data/test_C3_result.json")

print("=" * 70)
print("SEZIONE 2 — Re-run enricher su GPX reali con nuovo codice")
print("=" * 70)

results = {}

for tag, gpx_path, old_path in [
    ("C2", GPX_C2, OLD_C2),
    ("C3", GPX_C3, OLD_C3),
]:
    if not Path(gpx_path).exists():
        print(f"\n[{tag}] GPX non trovato: {gpx_path} — skip")
        continue

    old = {}
    if old_path.exists():
        old = json.loads(old_path.read_text()).get("enrichment", {})

    cooldown = 45
    print(f"\n[{tag}] Cooldown {cooldown}s per rispettare rate-limit Overpass...")
    time.sleep(cooldown)

    print(f"[{tag}] Avvio enricher su {gpx_path}  (sleep_between=15s)")
    enr = enrich_gpx(gpx_path, verbose=True, sleep_between=15.0)

    if enr.get("partial"):
        print(f"  [PARTIAL] {enr['unresolved_percent']:.1f}% non risolti — skip scoring")
        results[tag] = None
        continue

    results[tag] = enr

    print(f"\n  === CONFRONTO {tag} ===")
    fields = [
        ("trail_percent",         "trail_percent"),
        ("near_natural_percent",  "near_natural_percent"),
        ("main_road_percent_raw", "main_road_percent"),       # old non aveva _raw
        ("main_road_percent",     None),                      # nuovo campo effettivo
        ("main_road_isolated_count", None),
        ("main_road_sustained_count", None),
        ("secondary_percent",     "secondary_percent"),
        ("ss16_detected",         "ss16_detected"),
        ("ss16_sustained_count",  None),
    ]
    for new_key, old_key in fields:
        new_val = enr.get(new_key, "—")
        old_val = old.get(old_key or new_key, "—") if old else "—"
        marker = ""
        if new_key in ("ss16_detected", "main_road_percent", "main_road_percent_raw"):
            if new_val != old_val:
                marker = "  ← CAMBIATO"
        print(f"  {new_key:30s} = {str(new_val):8}  (prima: {old_val}){marker}")

    # Calcola traffic_score
    ts = _traffic_score(enr)
    old_main = old.get("main_road_percent", 0) if old else 0
    ts_old   = _traffic_score({"main_road_percent": old_main,
                                "secondary_percent": old.get("secondary_percent", 0)})
    print(f"\n  traffic_score   NUOVO  = {ts:.1f}")
    print(f"  traffic_score   VECCHIO = {ts_old:.1f}")
    if ts != ts_old:
        print(f"  Δ traffic_score = {ts - ts_old:+.1f} pt")

print()
print("=" * 70)
print("RIEPILOGO FIX")
print("=" * 70)
print("Fix 1 _assign_nearest: preferisce ciclabile/sentiero a strada principale (entro 20m)")
print("Fix 2 _classify:       ss16_detected=False su ciclabili con ref=SS16")
print("Fix 3 aggregate:       main_road_percent scontato del 70% per attraversamenti isolati")
print("                       ss16_detected=True solo per tratti sostenuti (≥2 campioni)")
