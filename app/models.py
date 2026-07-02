"""
Modelli Pydantic condivisi — SRS §5.1 (RouteRequest), §5.4 (CandidateRoute).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


# ── Schema della richiesta percorso (pipeline a due fasi) ─────────────────────

class StartPoint(BaseModel):
    name: str
    lat: float
    lon: float


class RouteRequest(BaseModel):
    """
    Schema unificato della richiesta percorso.

    Copre sia la Fase 1 (Planner: genera bozza waypoint) sia la Fase 2
    (Builder: calcola candidati reali BRouter + Scoring).

    I campi marcati "backward compat" replicano il vecchio dict informale e
    consentono di usare RouteRequest al posto del raw dict senza breaking changes.
    """
    # ── Partenza/arrivo ──────────────────────────────────────────────────────
    start: StartPoint
    end: dict[str, Any] | None = None          # solo per point_to_point

    # ── Distanza e tipo ──────────────────────────────────────────────────────
    target_km: float = 60.0
    distance_tolerance_km: float = 5.0
    route_type: Literal["loop", "out_and_back", "point_to_point"] = "loop"
    candidate_count: int = 3

    # ── Waypoint espliciti dell'utente (Fase 1 — nuovi) ──────────────────────
    # Ogni elemento è un nome libero ("Corinaldo") oppure "lat,lon" ("43.65,13.05").
    # Ordine non vincolante: il Planner li riordina geograficamente.
    user_waypoints: list[str] = []

    # ── Temi indipendenti (Fase 1 — nuovi) ───────────────────────────────────
    scenery_theme: Literal[
        "naturalistico", "storico_culturale", "panoramico", "misto"
    ] = "misto"

    athletic_theme: Literal[
        "tranquillo", "medio", "impegnativo", "sportivo"
    ] = "medio"

    # ── Regole stradali graduali (Fase 2 — nuove) ────────────────────────────
    # motorway è sempre escluso a prescindere; non serve un campo.
    max_ss_percent: float = Field(
        default=8.0,
        description="% massima di tratto sostenuto (>1 km) su SS (highway=trunk/primary).",
    )
    max_sp_percent: float = Field(
        default=20.0,
        description="% massima di tratto sostenuto (>1 km) su SP (highway=secondary).",
    )
    # Eccezioni puntuali per nome (es. ["SS16", "SP7"]) — sempre hard-escluse.
    avoid_named_roads: list[str] = []

    # ── Campi backward-compat (vecchio dict informale) ────────────────────────
    max_elevation_gain_m: int = 700
    free_text: str = ""
    geographic_direction: str | None = None
    waypoints_stages: list[str] = []           # legacy: tappe intermedie per nome
    preferred_direction: list[str] = []        # legacy: "colline, borghi"
    desired_places: list[str] = []             # legacy
    avoid_places: list[str] = []               # legacy → usa avoid_named_roads nei flussi nuovi

    model_config = {"extra": "allow"}          # consente campi aggiunti da merge_memory_with_request


# ── Output del Planner (Fase 1) ───────────────────────────────────────────────

class WaypointOrdered(BaseModel):
    """
    Waypoint post-geocoding restituito dal Planner nella nuova pipeline a due fasi.

    A differenza di Waypoint (interno al vecchio flusso), qui:
    - le coordinate sono SEMPRE valorizzate (geocoding già avvenuto)
    - source traccia se il punto viene dall'utente o è stato aggiunto dal Planner
    - order è la posizione nella sequenza ottimizzata
    """
    role: Literal["start", "via", "end"]
    name: str
    lat: float
    lon: float
    source: Literal["user", "planner"]
    order: int = 0
    rationale: str | None = None  # motivazione e fonte (solo waypoint "planner")

    model_config = {"extra": "allow"}


# ── Modelli vecchio flusso (backward compat) ──────────────────────────────────

class Waypoint(BaseModel):
    role: Literal["start", "via", "end"]
    name: str
    lat: float | None = None
    lon: float | None = None
    needs_geocoding: bool = False
    traversal: bool = False          # via waypoint da espandere con sentieri OSM reali
    area_hint: str | None = None     # nome dell'area da attraversare (usato da area_resolver)

    model_config = {"extra": "allow"}  # geocoding_error aggiunto post-validazione

    @model_validator(mode="after")
    def enforce_geocoding_for_non_start(self) -> "Waypoint":
        """
        Via e end passano SEMPRE per il Geocoding Agent (SRS §6.2bis).
        Anche se il modello restituisce coordinate che "conosce", vengono
        azzerate qui per garantire coerenza della pipeline.
        """
        if self.role in ("via", "end"):
            self.lat = None
            self.lon = None
            self.needs_geocoding = True
        return self


class CandidateRoute(BaseModel):
    name: str
    route_type: Literal["loop", "out_and_back", "point_to_point"]
    profile: Literal["trekking", "gravel", "fastbike"]
    requires_geocoding: bool
    estimated_km: float
    rationale: str
    waypoints: list[Waypoint]
    free_text_overrides: list[str] = []       # parametri sovrascritti dal testo libero
    max_elevation_gain_m_effective: int | None = None  # dislivello effettivo post-override

    model_config = {"extra": "allow"}

    @model_validator(mode="after")
    def check_waypoints(self) -> CandidateRoute:
        roles = {w.role for w in self.waypoints}
        missing = {"start", "end"} - roles
        if missing:
            raise ValueError(f"Waypoint obbligatori mancanti: {missing}")
        return self


class PlannerOutput(BaseModel):
    strategies: list[CandidateRoute]

    @model_validator(mode="after")
    def check_strategy_count(self) -> PlannerOutput:
        if len(self.strategies) != 3:
            raise ValueError(
                f"Il Planner deve restituire esattamente 3 strategie, "
                f"ne ha restituite {len(self.strategies)}."
            )
        return self
