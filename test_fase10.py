"""
Test Fase 10 — Persistenza SQLite + UserMemory + Learning Agent + Feedback loop.

Usa i candidati già generati in data/last_test_candidates.json e
la decisione in data/last_test_decision.json per simulare un ciclo completo:
  1. Inizializza / verifica DB
  2. Mostra user_memory.yaml PRIMA
  3. Salva il pipeline run nel DB
  4. Simula un feedback positivo
  5. Salva feedback + routes_approved nel DB
  6. Aggiorna UserMemory via Learning Agent
  7. Mostra user_memory.yaml DOPO e query di verifica DB

Uso: venv/bin/python test_fase10.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "app")

import db
from user_memory import load_user_memory
from learning_agent import update_user_memory_from_feedback

# ── 1. Verifica DB ────────────────────────────────────────────────────────────
print("=" * 65)
print("1. DATABASE SQLite — stato iniziale")
print("=" * 65)
stats = db.db_stats()
for table, count in stats.items():
    print(f"  {table:<28} {count:>4} righe")

# ── 2. UserMemory PRIMA ───────────────────────────────────────────────────────
print()
print("=" * 65)
print("2. USER MEMORY — stato iniziale")
print("=" * 65)
mem_before = load_user_memory()
prefs_b = mem_before.get("preferences", {})
print(f"  comfortable_distance_km     : {prefs_b.get('comfortable_distance_km')}")
print(f"  comfortable_elevation_gain_m: {prefs_b.get('comfortable_elevation_gain_m')}")
print(f"  preferred_gravel_percent    : {prefs_b.get('preferred_gravel_percent')}")
print(f"  max_elevation_gain_m        : {prefs_b.get('max_elevation_gain_m')}")
hist_b = mem_before.get("history", {})
print(f"  history.total_runs          : {hist_b.get('total_runs', 0)}")
print(f"  history.approved            : {hist_b.get('approved', 0)}")
print(f"  history.rejected            : {hist_b.get('rejected', 0)}")

# ── 3. Carica candidati e decisione ──────────────────────────────────────────
cand_path = Path("data/last_test_candidates.json")
dec_path  = Path("data/last_test_decision.json")
if not cand_path.exists() or not dec_path.exists():
    print("\n[ERRORE] Esegui prima test_pipeline.py e test_fase8.py")
    sys.exit(1)

cand_data = json.loads(cand_path.read_text())
dec_data  = json.loads(dec_path.read_text())

request    = cand_data["request"]
candidates = [c for c in cand_data["candidates"] if c["status"] == "ok"]
scored     = dec_data["scored"]
decision   = dec_data["decision"]

# Il vincitore del test precedente era "C"
winner_id = decision["winner"]
winner_c  = next((c for c in candidates if c["id"] == winner_id), candidates[0])

print()
print("=" * 65)
print("3. SALVATAGGIO PIPELINE RUN — routes_generated")
print("=" * 65)
route_gen_id = db.save_pipeline_run(request, candidates, scored, decision)
print(f"  Salvato con id={route_gen_id}  (winner={winner_id})")

# ── 4. Feedback positivo ──────────────────────────────────────────────────────
feedback = {
    "rating"      : 5,
    "too_traffic" : False,
    "too_gravel"  : False,
    "too_hard"    : False,
    "good_surface": True,
    "nice_views"  : True,
    "would_repeat": True,
    "notes"       : "Percorso bellissimo, paesaggi collinari perfetti. Lo rifarò sicuramente.",
}

print()
print("=" * 65)
print("4. SALVATAGGIO FEEDBACK — user_feedback + routes_approved")
print("=" * 65)
fb_id = db.save_feedback(
    route_gen_id = route_gen_id,
    candidate_id = winner_c["id"],
    **{k: feedback[k] for k in [
        "rating","too_traffic","too_gravel","too_hard",
        "good_surface","nice_views","would_repeat","notes"
    ]},
)
print(f"  user_feedback id={fb_id}")

if feedback["would_repeat"]:
    app_id = db.save_route_approved(route_gen_id, winner_c)
    print(f"  routes_approved id={app_id}  [{winner_c['id']}] {winner_c['strategy_name']}")
    print(f"    {winner_c['analysis']['distance_km']} km  |  "
          f"{winner_c['analysis']['elevation_gain_m']} m dislivello")

# ── 5. Learning Agent ─────────────────────────────────────────────────────────
print()
print("=" * 65)
print("5. LEARNING AGENT — aggiornamento UserMemory")
print("=" * 65)
mem_after = update_user_memory_from_feedback(feedback, winner_c["analysis"])
prefs_a = mem_after.get("preferences", {})
hist_a  = mem_after.get("history", {})

print(f"  comfortable_distance_km      : {prefs_b.get('comfortable_distance_km')} "
      f"→ {prefs_a.get('comfortable_distance_km')}"
      f"  (EMA con sample={winner_c['analysis']['distance_km']} km)")
print(f"  comfortable_elevation_gain_m : {prefs_b.get('comfortable_elevation_gain_m')} "
      f"→ {prefs_a.get('comfortable_elevation_gain_m')}"
      f"  (EMA con sample={winner_c['analysis']['elevation_gain_m']} m)")
print(f"  preferred_gravel_percent     : {prefs_b.get('preferred_gravel_percent')} "
      f"→ {prefs_a.get('preferred_gravel_percent')}"
      f"  (too_gravel={feedback['too_gravel']})")
print(f"  max_elevation_gain_m         : {prefs_b.get('max_elevation_gain_m')} "
      f"→ {prefs_a.get('max_elevation_gain_m')}"
      f"  (too_hard={feedback['too_hard']})")
print(f"  history.total_runs           : {hist_b.get('total_runs',0)} → {hist_a.get('total_runs',0)}")
print(f"  history.approved             : {hist_b.get('approved',0)} → {hist_a.get('approved',0)}")

# ── 6. Verifica DB finale ─────────────────────────────────────────────────────
print()
print("=" * 65)
print("6. DATABASE SQLite — stato finale")
print("=" * 65)
stats_after = db.db_stats()
for table, count in stats_after.items():
    delta = count - stats[table]
    marker = f"  (+{delta})" if delta else ""
    print(f"  {table:<28} {count:>4} righe{marker}")

# Verifica query
print()
print("Query di verifica:")
with db.get_conn() as conn:
    row = conn.execute(
        "SELECT id, winner_id, created_at FROM routes_generated WHERE id=?",
        (route_gen_id,)
    ).fetchone()
    print(f"  routes_generated [{row['id']}]  winner={row['winner_id']}  at={row['created_at']}")

    row2 = conn.execute(
        "SELECT id, candidate_id, rating, would_repeat FROM user_feedback WHERE id=?",
        (fb_id,)
    ).fetchone()
    print(f"  user_feedback [{row2['id']}]  cand={row2['candidate_id']}  "
          f"rating={row2['rating']}  would_repeat={bool(row2['would_repeat'])}")

    app_rows = conn.execute(
        "SELECT id, candidate_id, distance_km, elevation_gain_m FROM routes_approved"
        " WHERE route_gen_id=?", (route_gen_id,)
    ).fetchall()
    for r in app_rows:
        print(f"  routes_approved [{r['id']}]  cand={r['candidate_id']}  "
              f"{r['distance_km']} km  {r['elevation_gain_m']} m")

    cache_count = conn.execute("SELECT COUNT(*) FROM geocoding_cache").fetchone()[0]
    print(f"  geocoding_cache             {cache_count} righe migrate")

print()
print("✓ Test Fase 10 completato")
