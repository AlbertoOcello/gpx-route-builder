"""
Learning Agent base (SRS §6.6, Fase 10).
Aggiorna user_memory.yaml in base al feedback utente su un percorso completato.
Nessun ML — solo regole semplici e media mobile esponenziale (EMA).
"""
from __future__ import annotations

from user_memory import load_user_memory, save_user_memory

# Peso della nuova osservazione nella media mobile (0 < alpha ≤ 1).
# alpha=0.2 → la nuova misura pesa il 20%, la storia l'80%.
_EMA_ALPHA = 0.2


def update_user_memory_from_feedback(
    feedback: dict,
    route_analysis: dict,
) -> dict:
    """
    Aggiorna UserMemory dal feedback utente (SRS §6.6).

    feedback keys attesi:
      rating (int 1-5), too_traffic, too_gravel, too_hard,
      good_surface, nice_views, would_repeat (bool), notes (str)

    route_analysis keys attesi:
      distance_km (float), elevation_gain_m (float)

    Ritorna la memory aggiornata (già salvata su disco).
    """
    memory = load_user_memory()
    prefs = memory.setdefault("preferences", {})
    history = memory.setdefault("history", {})

    dist_km = float(route_analysis.get("distance_km", 0))
    elev_m = float(route_analysis.get("elevation_gain_m", 0))

    history["total_runs"] = history.get("total_runs", 0) + 1

    if feedback.get("would_repeat"):
        # Feedback positivo: aggiorna medie comode con EMA
        history["approved"] = history.get("approved", 0) + 1

        old_dist = float(prefs.get("comfortable_distance_km", dist_km))
        prefs["comfortable_distance_km"] = round(
            (1 - _EMA_ALPHA) * old_dist + _EMA_ALPHA * dist_km, 1
        )

        old_elev = float(prefs.get("comfortable_elevation_gain_m", elev_m))
        prefs["comfortable_elevation_gain_m"] = round(
            (1 - _EMA_ALPHA) * old_elev + _EMA_ALPHA * elev_m, 1
        )
    else:
        history["rejected"] = history.get("rejected", 0) + 1

    # Troppo gravel → riduce preferenza gravel (-5%, floor 0)
    if feedback.get("too_gravel"):
        old_pct = int(prefs.get("preferred_gravel_percent", 40))
        prefs["preferred_gravel_percent"] = max(0, old_pct - 5)

    # Troppo duro → abbassa max_elevation_gain_m (-50m, floor 200)
    if feedback.get("too_hard"):
        old_max = int(prefs.get("max_elevation_gain_m", 700))
        prefs["max_elevation_gain_m"] = max(200, old_max - 50)

    save_user_memory(memory)
    return memory
