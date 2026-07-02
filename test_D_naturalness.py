"""
Test D — Naturalness score nei candidati Fase 9.

Usa i dati di enrichment OSM già cached (data/candB_osm_enrichment.json,
data/candC_osm_enrichment.json) e aggiunge near_natural_percent simulato
per validare la formula _naturalness_score senza rieseguire Overpass.

Confronta: scoring Fase 9 (senza naturalness) vs Fase 10 (con naturalness).

Uso: venv/bin/python test_D_naturalness.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "app")

from user_memory import load_user_memory, merge_memory_with_request
from scoring_engine import score_candidate, _naturalness_score

# ── Dati simulati near_natural_percent ───────────────────────────────────────
# Stima ragionata basata sulla conoscenza dei percorsi:
#   C (fastbike, SP road, Cesano): poche aree naturali vicino alla SP
#   B (gravel, colline Cesano):    più aree boschive e rurali

NEAR_NATURAL_OVERRIDES = {
    "C": 12.0,   # SP roads, qualche campo ma pochi elementi naturali OSM
    "B": 38.0,   # colline, sterrati rurali, bordoforesta
}

# ── Carica dati ───────────────────────────────────────────────────────────────

cand_file = Path("data/last_test_candidates.json")
if not cand_file.exists():
    print("[ERRORE] data/last_test_candidates.json non trovato. Esegui prima test_pipeline.py")
    sys.exit(1)

raw      = json.loads(cand_file.read_text())
cands    = [c for c in raw["candidates"] if c["status"] == "ok"]
base_req = raw["request"]

memory  = load_user_memory()
request = merge_memory_with_request(base_req, memory)

print("=" * 70)
print("TEST D — Naturalness score (pesi aggiornati Fase 10)")
print("=" * 70)
print(f"  Candidati: {[c['id'] for c in cands]}")
print(f"  near_natural_percent: {NEAR_NATURAL_OVERRIDES}  (simulato)")
print()

# ── Naturalness standalone (formula) ─────────────────────────────────────────
print("NATURALNESS SCORE — formula standalone")
print("-" * 45)
print(f"  {'ID':<4} {'trail%':>8} {'near_nat%':>10} {'gravel%':>8} {'nat_score':>10}")
print(f"  {'-'*4} {'-'*8} {'-'*10} {'-'*8} {'-'*10}")

for cand in cands:
    cid = cand["id"]
    cache = Path(f"data/cand{cid}_osm_enrichment.json")
    if not cache.exists():
        print(f"  {cid}    — cache non trovata, skip")
        continue
    enr = json.loads(cache.read_text())
    enr.setdefault("trail_percent", 0.0)
    enr.setdefault("near_natural_percent", NEAR_NATURAL_OVERRIDES.get(cid, 0.0))

    trail_pct  = enr.get("trail_percent", 0)
    nat_pct    = enr.get("near_natural_percent", 0)
    gravel_pct = enr.get("gravel_percent", 0)
    nat_score  = _naturalness_score(enr)

    print(f"  {cid:<4} {trail_pct:>8.1f} {nat_pct:>10.1f} {gravel_pct:>8.1f} {nat_score:>10.1f}")

# ── Score completo con naturalness ────────────────────────────────────────────
print()
print("SCORING COMPLETO — componenti (Fase 10 pesi)")
print("-" * 70)

COMPONENTS = ["distance_match", "elevation", "surface", "traffic",
              "scenic", "naturalness", "user_preferences"]

scored = []
for cand in cands:
    cid = cand["id"]
    cache = Path(f"data/cand{cid}_osm_enrichment.json")
    enr = None
    if cache.exists():
        enr = json.loads(cache.read_text())
        enr.setdefault("trail_percent", 0.0)
        enr["near_natural_percent"] = NEAR_NATURAL_OVERRIDES.get(cid, 0.0)

    s = score_candidate(cand["analysis"], request, enrichment=enr)
    scored.append(s)

    src = s["osm_source"]
    print(f"\n  Candidato {cid} — {cand['strategy_name']} [{cand['profile']}]")
    print(f"  OSM source: {src}")
    cs = s["component_scores"]
    for comp in COMPONENTS:
        c = cs.get(comp, {})
        ph_tag = " [PH]" if c.get("placeholder") else ""
        src_tag = f" ({c.get('source', '?')})"
        print(f"    {comp:<20}: {c.get('score', 0):>6.1f}{ph_tag}{src_tag}")
    status = "SCARTATO" if s["discarded"] else "VALIDO"
    print(f"  TOTALE: {s['total_score']:.2f}  [{status}]")
    if s["discarded"]:
        print(f"  Motivo: {s['discard_reason']}")

# ── Delta Fase 9 vs Fase 10 ───────────────────────────────────────────────────
fase9_file = Path("data/last_test_fase9.json")
if fase9_file.exists():
    fase9 = json.loads(fase9_file.read_text())
    old_map = {s["id"]: s["total_score"] for s in fase9.get("scored", [])}

    print()
    print("DELTA — Fase 10 (naturalness) vs Fase 9")
    print("-" * 50)
    print(f"  {'ID':<4} {'Fase 9':>8} {'Fase 10':>8} {'Delta':>8}")
    print(f"  {'-'*4} {'-'*8} {'-'*8} {'-'*8}")
    for cand, s in zip(cands, scored):
        cid  = cand["id"]
        old  = old_map.get(cid, 0.0)
        new  = s["total_score"]
        mark = " SCARTATO" if s["discarded"] else ""
        print(f"  {cid:<4} {old:>8.2f} {new:>8.2f} {new - old:>+8.2f}{mark}")

# ── Classifica finale ─────────────────────────────────────────────────────────
print()
print("CLASSIFICA FINALE")
print("-" * 50)
pairs = list(zip(cands, scored))
valid   = sorted([(c, s) for c, s in pairs if not s["discarded"]],
                 key=lambda x: x[1]["total_score"], reverse=True)
invalid = [(c, s) for c, s in pairs if s["discarded"]]
rank = 1
for c, s in valid + invalid:
    prefix = f"#{rank}" if not s["discarded"] else " ✗"
    print(f"  {prefix}  {c['id']} — {c['strategy_name']} [{c['profile']}]  "
          f"score={s['total_score']:.1f}")
    if not s["discarded"]:
        rank += 1
