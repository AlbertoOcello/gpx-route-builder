"""
Test Fase 8 — Scoring Engine + Decision Agent
Uso: venv/bin/python test_fase8.py

Carica i candidati da data/last_test_candidates.json (generati da test_pipeline.py).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "app")

from scoring_engine import score_candidate
from decision_agent import run_decision

# ── Carica dati ───────────────────────────────────────────────────────────────
src = Path("data/last_test_candidates.json")
if not src.exists():
    print("[ERRORE] data/last_test_candidates.json non trovato.")
    print("         Esegui prima: venv/bin/python test_pipeline.py")
    sys.exit(1)

data = json.loads(src.read_text())
request = data["request"]
candidates = [c for c in data["candidates"] if c["status"] == "ok"]

TARGET_KM = request["target_km"]
TOLERANCE = request.get("distance_tolerance_km", 5)

# ── Scoring Engine ────────────────────────────────────────────────────────────
print("=" * 65)
print("SCORING ENGINE (SRS §8)")
print("=" * 65)
print(f"Target: {TARGET_KM} km ±{TOLERANCE} km  |  Hard discard: |diff| > 2×{TOLERANCE}={2*TOLERANCE} km\n")

scored = []
for c in candidates:
    s = score_candidate(c["analysis"], request)
    scored.append(s)

    label = f"Candidato {c['id']} — {c['strategy_name']} [{c['profile']}]"
    dist  = c["analysis"]["distance_km"]
    elev  = c["analysis"]["elevation_gain_m"]
    diff  = dist - TARGET_KM
    sign  = "+" if diff >= 0 else ""

    print(label)
    print(f"  Distanza reale: {dist} km ({sign}{diff:.2f} km)")
    print(f"  Dislivello:     {elev} m")

    if s["discarded"]:
        print(f"  ✗ SCARTATO — {s['discard_reason']}")
    else:
        print(f"  ✓ Valido")

    print(f"  Punteggi componente:")
    for name, v in s["component_scores"].items():
        tag = "[PLACEHOLDER — Fase 9/10]" if v["placeholder"] else "[REALE]"
        print(f"    {name:<20} {v['score']:>6.1f}/100  {tag}")
    print(f"  → TOTAL SCORE: {s['total_score']:.2f}/100")
    print()

# Riepilogo tabella
print("-" * 65)
print(f"{'ID':<4} {'Profilo':<12} {'km':<8} {'diff':<10} {'Dislivello':<12} {'Score':<8} {'Stato'}")
print("-" * 65)
for c, s in zip(candidates, scored):
    dist = c["analysis"]["distance_km"]
    diff = dist - TARGET_KM
    sign = "+" if diff >= 0 else ""
    stato = "SCARTATO" if s["discarded"] else "valido"
    print(
        f"  {c['id']:<3} {c['profile']:<12} {dist:<8.1f} "
        f"{sign}{diff:<8.2f} {c['analysis']['elevation_gain_m']:<12.0f} "
        f"{s['total_score']:<8.2f} {stato}"
    )
print("-" * 65)

# ── Decision Agent ────────────────────────────────────────────────────────────
print()
print("=" * 65)
print("DECISION AGENT — Claude API (SRS §6.5)")
print("=" * 65)

report = run_decision(candidates, scored, request)

print()
if report.winner:
    winner_c = next((c for c in candidates if c["id"] == report.winner), None)
    print(f"★ PERCORSO SCELTO: {report.winner} — {winner_c['strategy_name'] if winner_c else '?'}")
    print()
    print(f"Motivazione:\n  {report.rationale}")
else:
    print("⚠ Due candidati equivalenti — domanda all'utente:")
    print(f"  → {report.question_for_user}")

print()
print("Classifica finale:")
for r in sorted(report.ranking, key=lambda x: x.rank):
    bar = "★" if report.winner and r.id == report.winner else " "
    print(f"  {bar} #{r.rank}  [{r.id}]  score={r.total_score:.1f}  — {r.note}")

# ── Salva report ──────────────────────────────────────────────────────────────
out = Path("data/last_test_decision.json")
out.write_text(
    json.dumps(
        {
            "request": request,
            "scored": [
                {"id": c["id"], "strategy_name": c["strategy_name"], **s}
                for c, s in zip(candidates, scored)
            ],
            "decision": report.model_dump(),
        },
        indent=2, ensure_ascii=False,
    )
)
print(f"\nReport salvato in {out}")
