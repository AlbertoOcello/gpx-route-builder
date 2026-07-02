"""
Test flusso "nessun candidato valido" — Fase 8.

Simula il caso in cui tutti i candidati vengono scartati (tolleranza=0.5 km)
usando i candidati già generati in data/last_test_candidates.json.
Verifica che il Decision Agent restituisca options: list[str] e che la UI
possa mostrarli come radio buttons.

Uso: venv/bin/python test_fase8_question.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "app")

from scoring_engine import score_candidate
from decision_agent import run_decision

# Carica candidati esistenti
src = Path("data/last_test_candidates.json")
if not src.exists():
    print("[ERRORE] Esegui prima test_pipeline.py per generare i candidati.")
    sys.exit(1)

data = json.loads(src.read_text())
candidates = [c for c in data["candidates"] if c["status"] == "ok"]

# Request con tolleranza 0.5 km → tutti e 3 vengono scartati
# soglia hard: |diff| > 2×0.5 = 1 km
# A: |41.6-60|=18.4 > 1  → SCARTATO
# B: |66.4-60|=6.4  > 1  → SCARTATO
# C: |58.7-60|=1.32 > 1  → SCARTATO
tight_request = {
    **data["request"],
    "distance_tolerance_km": 0.5,
}

print("=" * 65)
print("TEST: Tutti i candidati scartati (tolleranza = ±0.5 km)")
print("=" * 65)
print(f"Soglia hard discard: |diff| > 2×0.5 = 1 km\n")

scored = []
for c in candidates:
    s = score_candidate(c["analysis"], tight_request)
    scored.append(s)
    diff = abs(c["analysis"]["distance_km"] - tight_request["target_km"])
    status = "✗ SCARTATO" if s["discarded"] else "✓ valido"
    print(f"  {c['id']} {c['strategy_name']}: {c['analysis']['distance_km']:.1f} km "
          f"(diff={diff:.1f} km) → {status}")

all_discarded = all(s["discarded"] for s in scored)
print(f"\n→ Tutti scartati: {'SI ✓' if all_discarded else 'NO ✗'}")
if not all_discarded:
    print("  Aumenta il criterio di scarto o usa candidati più distanti dal target.")
    sys.exit(1)

print()
print("=" * 65)
print("DECISION AGENT — caso 'tutti scartati'")
print("=" * 65)

report = run_decision(candidates, scored, tight_request)

print(f"\nwinner: {report.winner}")
print(f"rationale: {report.rationale}")
print(f"\nquestion_for_user:\n  {report.question_for_user}")
print(f"\noptions ({len(report.options)}):")
for i, opt in enumerate(report.options, 1):
    print(f"  [{i}] {opt}")

# Verifica che options sia strutturato e non vuoto
assert report.winner is None, "Expected winner=None when all discarded"
assert len(report.options) >= 2, "Expected at least 2 options"
print(f"\n✓ DecisionReport ha {len(report.options)} options strutturate — UI può mostrarle come radio buttons")

# Simula la scelta utente "Allarga tolleranza a ±10 km"
opt_10 = next((o for o in report.options if "10 km" in o), None)
if opt_10:
    print(f"\nSimulazione: utente sceglie '{opt_10}'")
    print(f"→ Il nuovo RouteRequest avrebbe distance_tolerance_km=10")
    new_req = {**tight_request, "distance_tolerance_km": 10}
    scored_new = [score_candidate(c["analysis"], new_req) for c in candidates]
    valid_new = [c["id"] for c, s in zip(candidates, scored_new) if not s["discarded"]]
    print(f"→ Con tolleranza ±10 km i candidati validi sarebbero: {valid_new}")
    assert len(valid_new) >= 1, "Expected at least 1 valid candidate with ±10 km tolerance"
    print(f"✓ Con ±10 km ci sono {len(valid_new)} candidati validi — pipeline ha senso rilanciare")
