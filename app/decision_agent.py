"""
Decision Agent (Fase 8) — SRS §6.5.
Riceve i candidati con punteggi e produce un DecisionReport motivato via Claude API.

Gestisce anche il caso di due candidati "equivalenti" (Δscore < 5 punti):
in quel caso formula una domanda all'utente invece di scegliere autonomamente.
"""
from __future__ import annotations

import json
import os
import re

from dotenv import load_dotenv

import ai_client
from pydantic import BaseModel, model_validator

load_dotenv()


# ── Modello dati (SRS §5.6) ───────────────────────────────────────────────────

class RankingEntry(BaseModel):
    id: str
    total_score: float
    rank: int
    note: str


class DecisionReport(BaseModel):
    winner: str | None              # "A" | "B" | "C" | null se serve input utente
    rationale: str
    question_for_user: str | None = None
    # Opzioni strutturate per la UI (radio buttons) — obbligatorie quando winner=null.
    # Formato atteso:
    #   caso "tutti scartati" : ["Allarga tolleranza a ±10 km", "Allarga tolleranza a ±15 km",
    #                            "Rigenera nuove strategie", "Annulla"]
    #   caso "equivalenti"    : ["Candidato X — nome (profilo)", "Candidato Y — nome (profilo)"]
    options: list[str] = []
    ranking: list[RankingEntry]

    @model_validator(mode="after")
    def check_question_when_no_winner(self) -> "DecisionReport":
        if self.winner is None:
            # options è il campo critico per la UI (radio buttons)
            if not self.options:
                raise ValueError("Se winner=null, options (lista non vuota) è obbligatoria.")
            # question_for_user fallback al rationale se il modello lo omette
            if not self.question_for_user and self.rationale:
                self.question_for_user = self.rationale
        return self


# ── Prompt di sistema ─────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """Sei un esperto cicloturistico che aiuta a scegliere il percorso migliore tra tre candidati.

Ricevi i candidati con i loro punteggi di scoring. Nota importante sui punteggi:
- I punteggi "placeholder: true" (traffic, surface, scenic, user_preferences) sono stime neutre
  perché il modulo OSM Tag Enricher non è ancora attivo. Non dar loro peso nella decisione.
- I punteggi "placeholder: false" (distance_match, elevation) sono REALI, basati sulla traccia GPX.

Regole di decisione:
1. I candidati con discarded=true sono già esclusi: non possono vincere.
2. Tra i candidati validi, scegli quello con total_score più alto (preferendo i punteggi reali).
3. Se NON ci sono candidati validi (tutti discarded=true): imposta winner=null,
   spiega il problema in rationale, e usa ESATTAMENTE queste opzioni strutturate:
     options: ["Allarga tolleranza a ±10 km", "Allarga tolleranza a ±15 km",
               "Rigenera nuove strategie", "Annulla"]
4. Se due candidati validi hanno total_score con differenza < 5 punti: imposta winner=null,
   formula una domanda in italiano in question_for_user, e in options elenca i candidati
   in competizione nel formato ESATTO: "Candidato {id} — {strategy_name} ({profile})"
   (es. "Candidato B — Valle Cesano (gravel)").
5. Rispondi SOLO con JSON valido, niente testo prima o dopo.

Schema JSON atteso (includere SEMPRE tutti i campi):
{
  "winner": "A" | "B" | "C" | null,
  "rationale": "motivazione in italiano, 2-3 frasi, citando dati reali (km, dislivello)",
  "question_for_user": "domanda in italiano se winner=null, altrimenti null",
  "options": [],
  "ranking": [
    {"id": "A", "total_score": 0.0, "rank": 3, "note": "scartato — motivo"},
    {"id": "B", "total_score": 63.1, "rank": 2, "note": "..."},
    {"id": "C", "total_score": 79.0, "rank": 1, "note": "..."}
  ]
}"""


def _extract_json(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    return m.group(1) if m else text


def run_decision(candidates: list[dict], scored: list[dict], request: dict) -> DecisionReport:
    """
    candidates : lista candidate dict da Candidate Generator (SRS §5.4 esteso)
    scored     : lista scoring dict da scoring_engine.score_candidate()
    request    : RouteRequest dict (SRS §5.1)

    Ritorna DecisionReport validato con Pydantic (SRS §5.6).
    """
    ai_client.check_api_key()

    # Payload compatto: solo i campi utili al modello
    payload = []
    for c, s in zip(candidates, scored):
        payload.append({
            "id": c["id"],
            "strategy_name": c["strategy_name"],
            "profile": c["profile"],
            "route_type": c["route_type"],
            "analysis": {
                "distance_km": c["analysis"]["distance_km"],
                "elevation_gain_m": c["analysis"]["elevation_gain_m"],
                "loop_closed": c["analysis"].get("loop_closed"),
            },
            "scoring": {
                "total_score": s["total_score"],
                "discarded": s["discarded"],
                "discard_reason": s["discard_reason"],
                "component_scores": {
                    k: {"score": v["score"], "placeholder": v["placeholder"]}
                    for k, v in s["component_scores"].items()
                },
            },
        })

    user_prompt = (
        f"Richiesta utente: {request['target_km']} km, "
        f"tipo={request['route_type']}, "
        f"tolleranza=±{request.get('distance_tolerance_km', 5)} km, "
        f"dislivello max={request.get('max_elevation_gain_m', 'N/D')} m.\n\n"
        "Candidati con punteggi:\n"
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )

    text = ai_client.generate_json(_SYSTEM_PROMPT, user_prompt, max_tokens=2000)
    if not text:
        raise RuntimeError("Decision Agent: nessuna risposta.")
    raw = json.loads(_extract_json(text))
    return DecisionReport.model_validate(raw)
