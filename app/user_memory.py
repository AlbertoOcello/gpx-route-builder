"""
UserMemory (SRS §5.7, §9.3) — lettura e scrittura delle preferenze utente persistenti.
File: config/user_memory.yaml
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

_MEMORY_PATH = Path(__file__).parent.parent / "config" / "user_memory.yaml"


def load_user_memory() -> dict:
    """Carica la UserMemory da YAML. Ritorna dict vuoto se il file non esiste."""
    if not _MEMORY_PATH.exists():
        return {}
    with open(_MEMORY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_user_memory(memory: dict) -> None:
    """Salva la UserMemory su YAML aggiornando updated_at."""
    memory["updated_at"] = date.today().isoformat()
    with open(_MEMORY_PATH, "w", encoding="utf-8") as f:
        yaml.dump(
            memory,
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            indent=2,
        )


def merge_memory_with_request(request: dict, memory: dict, obstacles: list[dict] | None = None) -> dict:
    """
    Fonde le preferenze UserMemory con i parametri espliciti del form.
    Regola SRS §6.1: i parametri espliciti dell'utente vincono sempre.

    Effetti:
      - Aggiunge le strade "avoid_always" della memory a avoid_places (unione).
      - Se preferred_direction nel form è vuoto, usa preferred_themes dalla memory.
      - max_elevation_gain_m: usa il valore del form se esplicitato, altrimenti memory.
    """
    merged = dict(request)

    # Ostacoli noti: iniettati nel free_text così il Planner Agent li considera
    if obstacles:
        obs_lines = "\n".join(
            f"- OSTACOLO NOTO ({o['lat']:.5f},{o['lon']:.5f}): {o['description']}"
            for o in obstacles
        )
        obs_header = "### OSTACOLI NOTI DA EVITARE (segnalati da uscite precedenti):\n"
        existing_ft = merged.get("free_text", "") or ""
        merged["free_text"] = (obs_header + obs_lines + "\n\n" + existing_ft).strip()
        merged["known_obstacles"] = [
            {"lat": o["lat"], "lon": o["lon"], "description": o["description"]}
            for o in obstacles
        ]

    if not memory:
        return merged

    prefs = memory.get("preferences", {})
    avoid = memory.get("avoid_always", {})

    # Unione avoid_places (form + memory, nessuna duplicazione)
    mem_roads = avoid.get("roads", [])
    form_avoid = list(merged.get("avoid_places", []))
    for road in mem_roads:
        if road not in form_avoid:
            form_avoid.append(road)
    merged["avoid_places"] = form_avoid

    # Superfici da evitare (cobblestone, mud, ecc.) — usate da OSM hard penalties
    mem_surfaces = avoid.get("surface_types", [])
    form_surfaces = list(merged.get("avoid_surfaces", []))
    for surf in mem_surfaces:
        if surf not in form_surfaces:
            form_surfaces.append(surf)
    merged["avoid_surfaces"] = form_surfaces

    # Preferenze fondo stradale: propagate dal form solo se non già presenti
    if "preferred_gravel_percent" not in merged and prefs.get("preferred_gravel_percent") is not None:
        merged["preferred_gravel_percent"] = prefs["preferred_gravel_percent"]
    if "max_gravel_percent" not in merged and prefs.get("max_gravel_percent") is not None:
        merged["max_gravel_percent"] = prefs["max_gravel_percent"]

    # Direzioni preferite: usa memory solo se il form non ha specificato nulla
    if not merged.get("preferred_direction") and prefs.get("preferred_themes"):
        merged["preferred_direction"] = list(prefs["preferred_themes"])

    return merged
