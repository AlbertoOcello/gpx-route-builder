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
from gpx_analyzer import OUT_AND_BACK_WARN_THRESHOLD_PCT
from pydantic import BaseModel, model_validator

load_dotenv()


# ── Modello dati (SRS §5.6) ───────────────────────────────────────────────────

class RankingEntry(BaseModel):
    id: str
    total_score: float
    rank: int
    note: str


class DecisionReport(BaseModel):
    winner: str | None              # id del candidato vincitore, o null se serve input utente
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

_OAB_PROMPT_NOTE = (
    f"- IMPORTANTE: se analysis.out_and_back_percent di un candidato supera "
    f"{OUT_AND_BACK_WARN_THRESHOLD_PCT:.0f}%, quel percorso ripercorre parzialmente sé stesso "
    f"(un tratto di andata/ritorno sulla stessa strada, non un anello pulito) — è già penalizzato "
    f"nel total_score, ma se questo è un fattore rilevante nella differenza di punteggio tra i "
    f"candidati DEVI menzionarlo esplicitamente in rationale (es. \"ripercorre parzialmente sé "
    f"stesso per un tratto di andata/ritorno\"), così come già fai per dislivello e distanza. "
    f"Come per la distanza, non è mai da solo motivo di esclusione (discarded).\n"
    f"- Se analysis.out_and_back_attributions non è vuoto, indica quale via-point specifico causa "
    f"il tratto ripercorso (waypoint_name) e quanto è lungo (overlap_km): quando menzioni "
    f"l'andata/ritorno in rationale, sii specifico citando il nome del waypoint invece di restare "
    f"generico — es. \"il tratto ripercorso è dovuto al waypoint Polverigi, che ha un solo accesso "
    f"stradale\" invece di una frase vaga senza nominare la causa.\n"
)

_SYSTEM_PROMPT = """Sei un esperto cicloturistico che aiuta a scegliere il percorso migliore tra i candidati proposti (possono essere 1, 2 o più).

Ricevi i candidati con i loro punteggi di scoring. Nota importante sui punteggi:
- I punteggi "placeholder: true" (traffic, surface, scenic, user_preferences) sono stime neutre
  perché il modulo OSM Tag Enricher non è ancora attivo. Non dar loro peso nella decisione.
- I punteggi "placeholder: false" (distance_match, elevation) sono REALI, basati sulla traccia GPX.
- IMPORTANTE: la distanza (distance_match) NON è mai da sola motivo di esclusione. Un candidato
  con distanza fuori tolleranza resta comunque eleggibile a vincere se ha il total_score più alto
  tra i candidati non scartati — la distanza pesa nel punteggio, non nell'eleggibilità. Non
  trattare "nessun candidato raggiunge il target di distanza" come equivalente a "tutti scartati":
  sono condizioni diverse, solo la seconda blocca la scelta di un vincitore.
""" + _OAB_PROMPT_NOTE + """
Regole di decisione:
1. I candidati con discarded=true sono già esclusi: non possono vincere. discarded=true è
   impostato SOLO per motivi geometrici/OSM (anello non chiuso, sterrato/pavé oltre soglia,
   SS16 rilevata, ostacolo noto) — mai per la sola distanza fuori tolleranza.
2. Tra i candidati con discarded=false, scegli quello con total_score più alto (preferendo i
   punteggi reali), anche se la sua distanza è fuori tolleranza: menzionalo pure in rationale,
   ma non è un motivo per non scegliere un vincitore.
3. Se NON ci sono candidati validi (tutti discarded=true): imposta winner=null,
   spiega il problema in rationale, e usa ESATTAMENTE queste opzioni strutturate:
     options: ["Allarga tolleranza a ±10 km", "Allarga tolleranza a ±15 km",
               "Rigenera nuove strategie", "Annulla"]
4. Se due o più candidati con discarded=false hanno total_score con differenza < 5 punti:
   imposta winner=null, formula una domanda in italiano in question_for_user, e in options elenca
   i candidati in competizione nel formato ESATTO: "Candidato {id} — {strategy_name} ({profile})"
   (es. "Candidato B — Valle Cesano (gravel)"). Questa regola richiede almeno due candidati
   eleggibili per scattare: se il pool contiene un solo candidato con discarded=false, quello
   vince direttamente per la regola 2, senza bisogno di confrontarlo con nessun altro.
5. Se è presente una sezione "Percorsi realmente pedalati (riferimento informativo)": quei
   percorsi NON sono candidati — non hanno un id nel payload candidati e non possono MAI
   comparire in winner, ranking od options. Usali solo come contesto (es. per notare che il
   vincitore scelto è più/meno impegnativo di quanto l'utente ha già pedalato realmente).
6. Rispondi SOLO con JSON valido, niente testo prima o dopo.

Schema JSON atteso (includere SEMPRE tutti i campi). Gli id validi per "winner" e per ogni
voce di "ranking" sono quelli presenti nel payload candidati qui sotto — il payload può
contenere 1, 2 o più candidati, non necessariamente 3:
{
  "winner": "<id candidato>" | null,
  "rationale": "motivazione in italiano, 2-3 frasi, citando dati reali (km, dislivello)",
  "question_for_user": "domanda in italiano se winner=null, altrimenti null",
  "options": [],
  "ranking": [
    {"id": "<id>", "total_score": 0.0, "rank": 1, "note": "motivo del ranking, o 'scartato — motivo'"}
  ]
}"""


def _extract_json(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    return m.group(1) if m else text


def run_decision(
    candidates: list[dict], scored: list[dict], request: dict,
    actual_rides: list[dict] | None = None,
) -> DecisionReport:
    """
    candidates   : lista candidate dict da Candidate Generator (SRS §5.4 esteso)
    scored       : lista scoring dict da scoring_engine.score_candidate()
    request      : RouteRequest dict (SRS §5.1)
    actual_rides : percorsi realmente pedalati (Opzione D), opzionale — solo
                   riferimento informativo per il modello (Builder redesign,
                   Parte C): non hanno un id nel payload candidati e non
                   possono mai vincere (niente request["target_km"] concettuale
                   per una D, restano fuori da score_candidate/run_decision
                   come candidati veri e propri).

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
                "out_and_back_percent": c["analysis"].get("out_and_back_percent"),
                "out_and_back_attributions": c["analysis"].get("out_and_back_attributions", []),
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

    if actual_rides:
        rides_payload = [
            {
                "uploaded_at": r.get("uploaded_at"),
                "distance_km": (r.get("analysis") or {}).get("distance_km"),
                "elevation_gain_m": (r.get("analysis") or {}).get("elevation_gain_m"),
            }
            for r in actual_rides
        ]
        user_prompt += (
            "\n\nPercorsi realmente pedalati (riferimento informativo, NON candidati — "
            "non hanno un id, non possono comparire in winner/ranking/options):\n"
            + json.dumps(rides_payload, indent=2, ensure_ascii=False)
        )

    text = ai_client.generate_json(_SYSTEM_PROMPT, user_prompt, max_tokens=2000)
    if not text:
        raise RuntimeError("Decision Agent: nessuna risposta.")
    raw = json.loads(_extract_json(text))
    return DecisionReport.model_validate(raw)
