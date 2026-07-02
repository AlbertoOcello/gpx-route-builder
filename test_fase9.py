"""
Test Fase 9 — Scoring Engine con punteggi OSM reali.

Flusso:
  1. Carica i 3 candidati da data/last_test_candidates.json
  2. Merge request + UserMemory (per gravel prefs, avoid_surfaces, ecc.)
  3. OSM enrichment:
       A  → saltato (sarà hard-discarded per distanza prima di OSM)
       C  → caricato da data/candC_osm_enrichment.json (cache)
       B  → eseguito live via Overpass API (attesa cooldown integrata)
  4. Score con punteggi reali per surface/traffic/scenic
  5. Decision Agent
  6. Confronto con Fase 8 (placeholder): delta punteggi

Uso: venv/bin/python test_fase9.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "app")

import db
from user_memory import load_user_memory, merge_memory_with_request
from scoring_engine import score_candidate
from decision_agent import run_decision
from osm_enricher import enrich_gpx

# ── 1. Carica dati ────────────────────────────────────────────────────────────

cand_file = Path("data/last_test_candidates.json")
if not cand_file.exists():
    print("[ERRORE] data/last_test_candidates.json non trovato. Esegui prima test_pipeline.py")
    sys.exit(1)

raw = json.loads(cand_file.read_text())
candidates = [c for c in raw["candidates"] if c["status"] == "ok"]
base_request = raw["request"]

# Merge con UserMemory (aggiunge max_gravel_percent, avoid_surfaces, ecc.)
memory  = load_user_memory()
request = merge_memory_with_request(base_request, memory)

print("=" * 65)
print("TEST FASE 9 — Scoring Engine con OSM reali")
print("=" * 65)
print(f"  Candidati: {[c['id'] for c in candidates]}")
print(f"  Target: {request['target_km']} km ± {request.get('distance_tolerance_km', 5)} km")
print(f"  max_gravel_percent  : {request.get('max_gravel_percent', 20)} %")
print(f"  preferred_gravel    : {request.get('preferred_gravel_percent', 10)} %")
print(f"  avoid_surfaces      : {request.get('avoid_surfaces', [])}")
print(f"  avoid_places        : {request.get('avoid_places', [])}")

# ── 2. OSM Enrichment per ciascun candidato ───────────────────────────────────

print()
print("=" * 65)
print("OSM Enrichment")
print("=" * 65)

enrichments: dict[str, dict | None] = {}

for cand in candidates:
    cid      = cand["id"]
    dist_km  = float(cand["analysis"]["distance_km"])
    tol_km   = float(request.get("distance_tolerance_km", 5))
    diff_km  = abs(dist_km - float(request["target_km"]))

    # Candidato A: sarà scartato per distanza prima che OSM venga letto → skip
    if diff_km > 2 * tol_km:
        print(f"\n  {cid} ({dist_km} km) — SKIP enrichment "
              f"(distanza +{diff_km:.1f} km > hard-discard {2*tol_km:.0f} km)")
        enrichments[cid] = None
        continue

    # Candidato C: usa cache
    cache_file = Path(f"data/cand{cid}_osm_enrichment.json")
    if cache_file.exists():
        enrichments[cid] = json.loads(cache_file.read_text())
        e = enrichments[cid]
        print(f"\n  {cid} ({dist_km} km, {cand['profile']}) — caricato da cache")
        if e.get("partial"):
            print(f"    [PARTIAL] {e.get('unresolved_percent')}% non risolti")
        else:
            print(f"    asphalt={e.get('asphalt_percent')}%  "
                  f"gravel={e.get('gravel_percent')}%  "
                  f"trail={e.get('trail_percent', 0)}%  "
                  f"main={e.get('main_road_percent')}%  "
                  f"secondary={e.get('secondary_percent')}%  "
                  f"cobblestone={e.get('cobblestone_detected')}  "
                  f"ss16={e.get('ss16_detected')}")
        continue

    # Candidato B (o altri): enrichment live
    gpx_path = cand.get("gpx_path", "")
    if not Path(gpx_path).exists():
        print(f"\n  {cid} — GPX non trovato: {gpx_path}")
        enrichments[cid] = None
        continue

    print(f"\n  {cid} ({dist_km} km, {cand['profile']}) — enrichment live via Overpass...")
    print(f"    Cooldown 60s prima del primo batch...")
    time.sleep(60)

    e = enrich_gpx(gpx_path, verbose=True, sleep_between=15.0)
    enrichments[cid] = e

    if e.get("partial"):
        print(f"    [PARTIAL] {e.get('unresolved_percent')}% non risolti — userò placeholder")
    else:
        print(f"    asphalt={e.get('asphalt_percent')}%  "
              f"gravel={e.get('gravel_percent')}%  "
              f"trail={e.get('trail_percent', 0)}%  "
              f"main={e.get('main_road_percent')}%  "
              f"secondary={e.get('secondary_percent')}%  "
              f"cobblestone={e.get('cobblestone_detected')}  "
              f"ss16={e.get('ss16_detected')}")

    out = Path(f"data/cand{cid}_osm_enrichment.json")
    out.write_text(json.dumps(e, indent=2, ensure_ascii=False))
    print(f"    Salvato in {out}")

# ── 3. Scoring con punteggi reali ─────────────────────────────────────────────

print()
print("=" * 65)
print("SCORING — punteggi reali vs placeholder")
print("=" * 65)

scored: list[dict] = []
for cand in candidates:
    cid  = cand["id"]
    enr  = enrichments.get(cid)
    s    = score_candidate(cand["analysis"], request, enrichment=enr)
    scored.append(s)

    src = s["osm_source"]
    print(f"\n  Candidato {cid} — {cand['strategy_name']} [{cand['profile']}]"
          f"  {cand['analysis']['distance_km']} km / {cand['analysis']['elevation_gain_m']} m")
    print(f"  OSM source: {src}  "
          f"(unresolved: {s['osm_unresolved_percent']:.1f}%)")

    cs = s["component_scores"]
    for comp_name in ["distance_match", "elevation", "surface", "traffic", "scenic", "user_preferences"]:
        c = cs.get(comp_name, {})
        ph_tag = " [PH]" if c.get("placeholder") else ""
        note   = f"  ← {c.get('note','')}" if c.get("note") else ""
        print(f"    {comp_name:<18}: {c.get('score', '—'):>6.1f}{ph_tag}{note}")

    status = "SCARTATO" if s["discarded"] else "VALIDO"
    print(f"  TOTALE: {s['total_score']:.2f}  [{status}]")
    if s["discarded"]:
        print(f"  Motivo scarto: {s['discard_reason']}")

# ── 4. Confronto con Fase 8 (placeholder) ────────────────────────────────────

fase8_file = Path("data/last_test_decision.json")
if fase8_file.exists():
    fase8 = json.loads(fase8_file.read_text())
    old_scored = {s["id"]: s["total_score"] for s in fase8.get("scored", [])}

    print()
    print("=" * 65)
    print("DELTA PUNTEGGI — Fase 9 (OSM reale) vs Fase 8 (placeholder)")
    print("=" * 65)
    print(f"  {'ID':<4} {'Fase 8':>8} {'Fase 9':>8} {'Delta':>8}  Fonte OSM")
    print(f"  {'-'*4} {'-'*8} {'-'*8} {'-'*8}  ---------")
    for cand, s in zip(candidates, scored):
        cid    = cand["id"]
        old    = old_scored.get(cid, 0.0)
        new    = s["total_score"]
        delta  = new - old
        status = "SCARTATO" if s["discarded"] else ""
        print(f"  {cid:<4} {old:>8.2f} {new:>8.2f} {delta:>+8.2f}  "
              f"{s['osm_source']:<10} {status}")

# ── 5. Tabella comparativa ordinata ──────────────────────────────────────────

print()
print("=" * 65)
print("CLASSIFICA FINALE (validi per score DESC, scartati in fondo)")
print("=" * 65)

valid_pairs   = sorted(
    [(c, s) for c, s in zip(candidates, scored) if not s["discarded"]],
    key=lambda x: x[1]["total_score"], reverse=True,
)
invalid_pairs = [(c, s) for c, s in zip(candidates, scored) if s["discarded"]]

rank = 1
for cand, s in valid_pairs + invalid_pairs:
    prefix = f"#{rank}" if not s["discarded"] else " ✗"
    print(f"  {prefix}  {cand['id']} — {cand['strategy_name']} [{cand['profile']}]  "
          f"{cand['analysis']['distance_km']} km / {cand['analysis']['elevation_gain_m']} m  "
          f"score={s['total_score']:.1f}")
    if s["discarded"]:
        print(f"      Scartato: {s['discard_reason']}")
    if not s["discarded"]:
        rank += 1

# ── 6. Decision Agent ─────────────────────────────────────────────────────────

print()
print("=" * 65)
print("DECISION AGENT")
print("=" * 65)

ok_cands   = [c for c in candidates if not scored[candidates.index(c)]["discarded"]]
ok_scored  = [s for s in scored if not s["discarded"]]

report = run_decision(ok_cands, ok_scored, request)

print(f"  Winner    : {report.winner}")
print(f"  Rationale : {report.rationale}")
if report.question_for_user:
    print(f"  Question  : {report.question_for_user}")
    for i, opt in enumerate(report.options, 1):
        print(f"    [{i}] {opt}")
print()
print("  Ranking:")
for r in report.ranking:
    print(f"    #{r.rank} {r.id}  score={r.total_score:.1f}  — {r.note}")

# Salva risultato
out = {
    "request": request,
    "scored": [
        {"id": c["id"], "total_score": s["total_score"],
         "discarded": s["discarded"], "discard_reason": s["discard_reason"],
         "osm_source": s["osm_source"],
         "component_scores": {k: v["score"] for k, v in s["component_scores"].items()}}
        for c, s in zip(candidates, scored)
    ],
    "decision": report.model_dump(),
    "osm_enrichments": {cid: e for cid, e in enrichments.items() if e},
}
Path("data/last_test_fase9.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
print("\n  Risultato salvato in data/last_test_fase9.json")
