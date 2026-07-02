"""
Planner Agent — genera strategie di routing via Claude API (SRS §6.2).

Pipeline a due fasi:
  Fase 1 (generate_raw_route):  Planner → sequenza ordinata di WaypointOrdered
  Fase 2 (generate_strategies): Planner → 3 CandidateRoute (pipeline vecchia)

NOTE su UserMemory (SRS §9, §6.1):
  Il parametro `user_memory` è già previsto nelle firme e integrato dove utile.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable

from dotenv import load_dotenv

import ai_client
from geopy.distance import geodesic
from pydantic import BaseModel, model_validator

from models import PlannerOutput, RouteRequest, WaypointOrdered

log = logging.getLogger(__name__)

load_dotenv()

_SYSTEM_PROMPT = """Sei un planner cicloturistico esperto delle Marche, Italia.
Conosci le strade bianche, i sentieri, i borghi e le colline della regione.
Il tuo compito è produrre esattamente tre strategie di routing per BRouter,
diversificate per profilo BRouter, direzione geografica e carattere del percorso.

REGOLA FONDAMENTALE (SRS §1.3):
Non generare MAI sequenze di punti GPS, tracce o file GPX.
Fornisci solo nomi di borghi/luoghi/incroci noti come waypoint:
BRouter calcolerà il percorso reale tra di essi.

REGOLA WAYPOINT — obbligatoria, senza eccezioni:
- Il waypoint con role "start" è l'unico che può avere lat e lon (usa i valori
  ricevuti in input). Imposta sempre needs_geocoding: false per lo start.
- I waypoint con role "via" e "end" devono avere SEMPRE:
    lat: null
    lon: null
    needs_geocoding: true
  Non inserire mai coordinate per via/end, nemmeno se le conosci.
  Le coordinate verranno calcolate da Nominatim nel passo successivo (Geocoding Agent).

CALIBRAZIONE DISTANZA — regola critica per non sforare il target:
Su terreno collinare marchigiano, BRouter produce percorsi su strada che sono
tipicamente 1.3–1.6× la distanza in linea d'aria tra i waypoint consecutivi
(curve, dislivelli, strade non rettilinee). Di conseguenza:
  - La somma delle distanze in linea d'aria tra i waypoint deve essere circa
    target_km ÷ 1.4  (usa 1.4 come fattore medio).
  - Per un loop da 60 km: somma linea d'aria ≈ 43 km.
  - Usa 2–3 waypoint "via" al massimo (mai più di 3 per loop ≤ 70 km).
    Più waypoint → percorso più lungo del previsto.
  - Ogni tratto tra waypoint consecutivi deve essere ≈ (target_km ÷ 1.4) ÷ (n_via+1) km
    in linea d'aria, dove n_via è il numero di waypoint via scelti.
  - Scegli borghi reali a quella distanza approssimativa dalla partenza, non i più lontani.

PRIORITÀ TESTO LIBERO — regola assoluta:
Se il campo `free_text` dell'utente contraddice un parametro strutturato del form
(es. "va bene anche 850m" quando max_elevation_gain_m=700), segui SEMPRE il testo
libero e ignora il parametro strutturato in conflitto.
Dichiara nel campo `free_text_overrides` la lista dei parametri sovrascritti
(es. ["max_elevation_gain_m: 700 → 850"]) e imposta `max_elevation_gain_m_effective`
al valore intero che userai effettivamente per il routing.
Se il testo libero non contraddice alcun parametro, `free_text_overrides` è []
e `max_elevation_gain_m_effective` è null.

PREFERENZA NATURA — sempre attiva:
Quando possibile, preferisci percorsi che attraversino ambienti naturali:
sentieri (path/track/ciclabili), parchi, boschi, fiumi, coste, zone rurali.
Questo è un obiettivo sistematico, indipendente da quanto esplicitamente richiesto.

AREA TRAVERSAL — quando free_text menziona l'attraversamento di un'area:
Se l'utente vuole attraversare un'area specifica (es. "voglio passare per il Parco
del Cormorano", "percorri il bosco di X", "segui il sentiero fluviale"), marca
il waypoint via corrispondente con:
  "traversal": true
  "area_hint": "nome esatto dell'area da attraversare"
Il Candidate Generator sostituirà questo waypoint con sentieri OSM reali.

TRAVERSAL — `name` MUST be a real Nominatim-geocodable toponym (frazione/comune):
  ✓ "Marina di Montemarciano"   → accesso foce Esino / Parco del Cormorano
  ✓ "Falconara Marittima"       → comune sulla foce Esino (alternativa)
  ✗ "Foce Esino, Montemarciano" → descrizione geografica, NON geocodificabile
  ✗ "Parco del Cormorano"       → nome parco, assente in Nominatim
Regola: usa sempre il nome di una frazione o comune noto, mai descrizioni composte.

TRAVERSAL — APPROCCIO DALL'ENTROTERRA (obbligatorio per aree fluviali e costiere):
Se l'area traversal è alla foce di un fiume, vicina alla costa o in un corridoio
pianeggiante dove SS16/strade principali sono l'opzione più diretta, aggiungi SEMPRE
un waypoint via NON-traversal IMMEDIATAMENTE PRIMA del waypoint traversal nella lista:
  - Scegli un borgo della stessa valle fluviale, a monte dell'area traversal
  - Per Parco del Cormorano / foce Esino → approccio da Chiaravalle (a monte sul Esino)
  - Per foce Misa → approccio da Ostra o Senigallia entroterra
  - Per foce Metauro → approccio da Calcinelli o Fano entroterra
  - Il waypoint di approccio ha traversal: false, needs_geocoding: true
  - Scopo: BRouter risale la valle fluviale dall'interno invece di seguire il
    corridoio costiero (SS16 Adriatica), che è la via diretta ma da evitare.
Non aggiungere l'approccio se l'area traversal è già nell'entroterra collinare.

Rispondi SOLO con JSON valido, senza testo prima o dopo il JSON."""


# ── Fase 1 — sistema prompt con ricerca web ───────────────────────────────────

_SYSTEM_PROMPT_RAW = """Sei un esperto pianificatore di percorsi cicloturistici.
Il tuo compito: produrre una sequenza ordinata di waypoint REALI e SPECIFICI per BRouter.

══ FASE 1 — RICERCA WEB (obbligatoria se user_waypoints è vuoto o scarso) ═════

Prima di scegliere i waypoint, usa il tool web_search per esplorare opzioni reali
e aggiornate nella zona richiesta. Non affidarti solo alla memoria pregressa.
Fai 1–3 ricerche mirate: sostituisci {zona} con la città di partenza del percorso.

  scenery_theme = naturalistico:
    → "{zona} Marche parchi naturali aree verdi fiumi sentieri bici"
    → "{zona} riserve naturali percorso cicloturistico Marche blog"

  scenery_theme = storico_culturale:
    → "{zona} Marche borghi medievali castelli centri storici cicloturismo"
    → "{zona} abbazie monasteri itinerario bicicletta Marche"

  scenery_theme = panoramico:
    → "{zona} Marche belvedere punti panoramici strade crinale colline bici"
    → "{zona} panorami percorso cicloturistico blog Marche"

  scenery_theme = misto:
    → una query naturalistica + una storica (non due query dello stesso tipo)

  Sempre (aggiuntiva):
    → "cicloturismo {zona} itinerari consigliati blog forum"

  PRIORITÀ ASSOLUTA — free_text:
    Se free_text menziona interessi specifici (vigneti, abbazie, sentieri fluviali,
    parchi particolari…), costruisci almeno una query su QUELL'interesse prima delle
    altre — ha precedenza assoluta anche nella scelta delle query di ricerca.

Usa i risultati per scegliere luoghi con carattere specifico: destinazioni segnalate
da blog locali, siti turistici regionali, club ciclistici. Evita nomi ovvi e generici.

══ FASE 2 — GENERA I WAYPOINT (dopo la ricerca web) ════════════════════════════

REGOLA FONDAMENTALE:
Non inventare coordinate. Per i waypoint utente usa le coordinate esatte fornite.
Per i waypoint planner usa le coordinate che conosci del luogo scelto (approssimate
sono ok — verranno rifinite in geocoding).

REGOLA LOOP:
Se route_type è "loop" o "out_and_back", il waypoint "end" ha le stesse
coordinate del waypoint "start" e source="user".

TEMI PAESAGGISTICI — guida per i waypoint "planner" che aggiungi tu:
  naturalistico:     zone fluviali, laghi, boschi, parchi naturali, campagna
  storico_culturale: borghi medievali, abbazie, castelli, centri storici murati
  panoramico:        cime, creste collinari, strade di crinale con ampie viste
  misto:             varietà — alterna naturale, storico, urbano

TEMI ATLETICI — guida sulla distribuzione:
  tranquillo:  tappe ravvicinate, fondovalle e ciclabili
  medio:       bilanciamento, salite moderate accettabili
  impegnativo: salite sostenute, percorsi in quota
  sportivo:    tratti lunghi e diretti, massimizza la distanza

CALIBRAZIONE DISTANZA:
BRouter produce percorsi 1.3–1.5× la distanza in linea d'aria.
Somma linee d'aria waypoint ≈ target_km ÷ 1.4.
Per loop ≤ 80 km usa al massimo 4 waypoint "via" complessivi (utente + planner).

DIREZIONE GEOGRAFICA (se specificata nel prompt utente):
Se geographic_direction è diversa da "Libera", i waypoint "via" aggiunti dal Planner
devono trovarsi approssimativamente nel settore angolare ±45° intorno a quella
direzione, calcolato dal punto di partenza:
  Nord:  settore 315°–45°  (latitudine maggiore della partenza, entroterra a N/NE/NO)
  Est:   settore 45°–135°  (verso est rispetto alla partenza)
  Sud:   settore 135°–225° (latitudine minore della partenza)
  Ovest: settore 225°–315° (verso ovest rispetto alla partenza)
Non occorre precisione trigonometrica: basta che i waypoint scelti si trovino
chiaramente nel quadrante giusto. Waypoint utente (source="user") non sono vincolati.

WAYPOINT UTENTE vs. PLANNER:
- Waypoint "user": già geocodificati. Non modificare le coordinate.
- Waypoint "planner": aggiungi 2–3 usando i risultati della ricerca web.
  Per ciascuno includi il campo "rationale" con motivazione e fonte:
    "rationale": "Trecastelli — borgo medievale Valle del Misa, segnalato su
                  ciclovia-marche.it come tappa storica caratteristica"
  Se non hai trovato una fonte esplicita, descrivi perché lo hai scelto.

SEQUENZA OTTIMALE:
Ordina i waypoint nella sequenza che minimizza la distanza totale in linea d'aria.

VARIETÀ — REGOLA ANTI-RIPETIZIONE:
Non scegliere sempre i borghi più celebri e più citati (es. Corinaldo, Ostra).
Usa i risultati web per scoprire luoghi meno ovvi: frazioni collinari, abbazie,
punti panoramici, borghi minori segnalati da blog locali o club ciclistici.
Se un luogo compare spesso nelle ricerche ma è il solito "top hit" dell'area,
preferisci la seconda o terza opzione più caratteristica e meno battuta.

PRIORITÀ ASSOLUTA:
Il campo "free_text" ha precedenza su tutti gli altri parametri.

══ FASE 3 — NARRATIVA DEL PERCORSO ════════════════════════════════════════════

Prima del JSON, genera il campo "route_narrative": una descrizione discorsiva in
italiano (4–6 frasi) che spiega:
  1. Perché hai scelto questa sequenza di waypoint.
  2. Le bellezze/caratteristiche principali del percorso
     (es. "un anello che si snoda tra i vigneti del Verdicchio, toccando il
     borgo murato di Corinaldo e risalendo la Valle del Misa con vista sulle
     colline dell'entroterra marchigiano").
  3. Come risponde al tema richiesto (scenery_theme / athletic_theme) e al
     testo libero dell'utente se presente.
  4. Una nota onesta che il percorso reale (Fase 2 / BRouter) potrà discostarsi
     da questa visione ideale per vincoli di routing, disponibilità di strade, ecc.

Tono: evocativo ma preciso. Il ciclista deve capire "lo spirito" del percorso.

══ OUTPUT ══════════════════════════════════════════════════════════════════════
Dopo la ricerca e l'analisi, restituisci il JSON nel formato specificato.
Il JSON deve essere l'ULTIMO blocco nella risposta e contenere TUTTI i campi:
  "route_narrative" (stringa narrativa) + "ordered_waypoints" (array waypoint)."""


# ── Regex per coordinate "lat,lon" ────────────────────────────────────────────

_COORD_RE = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")


# ── Modello Pydantic interno per validare la risposta di generate_raw_route ──

class _RawRouteOutput(BaseModel):
    route_narrative: str = ""
    ordered_waypoints: list[WaypointOrdered]

    @model_validator(mode="after")
    def check_roles(self) -> "_RawRouteOutput":
        roles = [w.role for w in self.ordered_waypoints]
        if "start" not in roles:
            raise ValueError("Manca il waypoint 'start'")
        if "end" not in roles:
            raise ValueError("Manca il waypoint 'end'")
        return self


def _parse_user_waypoint(s: str) -> tuple[str, float | None, float | None]:
    """Restituisce (name, lat|None, lon|None). Se è una coordinata 'lat,lon', parsifica direttamente."""
    m = _COORD_RE.match(s.strip())
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return f"{lat},{lon}", lat, lon
    return s.strip(), None, None


# ── Normalizzazione waypoint utente ──────────────────────────────────────────

_ADMIN_PREFIX_RE = re.compile(
    r"""^\s*(?:
        comune\s+di | municipio\s+di | citt[àa]\s+di |
        frazione\s+di | fraz\.\s*(?:di\s+)? |
        localit[àa]\s+(?:di\s+)? | loc\.\s* |
        contrada\s+ | c\.da\s+ | borgata\s+
    )\s*""",
    re.IGNORECASE | re.VERBOSE,
)


def _local_normalize(name: str) -> str:
    """Rimuove prefissi amministrativi ovvi (es. 'comune di') senza chiamate API."""
    cleaned = _ADMIN_PREFIX_RE.sub("", name).strip().strip("\"'")
    return cleaned if cleaned else name


def _claude_normalize(query_name: str, start_name: str, region: str) -> str:
    """
    Chiama l'AI configurata per correggere un nome di luogo che ha già fallito il geocoding.
    Restituisce query_name invariato se la chiamata fallisce o non cambia nulla.
    """
    try:
        normalized = ai_client.generate(
            system="Sei un assistente geografico per percorsi cicloturistici in Italia.",
            prompt=(
                f"Percorso cicloturistico, partenza da {start_name} ({region}).\n"
                f"Waypoint scritto dall'utente (già pre-normalizzato): \"{query_name}\"\n"
                f"Nominatim non ha trovato questo luogo. Restituisci il nome Nominatim-friendly\n"
                f"più plausibile nella zona di {start_name}:\n"
                f"- Espandi abbreviazioni: S.→San/Santa/Sant', Mt.→Monte, V.→Valle\n"
                f"- Privilegia il luogo geograficamente più vicino a {start_name}\n"
                f"- Non aggiungere regione/provincia — solo il nome del luogo\n"
                f"Rispondi con UNA SOLA RIGA: il nome normalizzato, nient'altro."
            ),
            max_tokens=60,
        ).strip().strip("\"'").strip()
        if normalized and normalized.lower() != query_name.lower():
            log.info("AI normalize: '%s' → '%s'", query_name, normalized)
            return normalized
    except Exception as exc:
        log.warning("AI normalize fallito per '%s': %s", query_name, exc)
    return query_name


_PROXIMITY_WARN_KM = 2.0
# Se un waypoint geocodificato si trova a meno di questa distanza dalla partenza,
# è probabile che Nominatim abbia trovato un omonimo locale invece della località vera.


def _geocode_user_waypoints(
    user_wps: list[str],
    region: str = "Italia",
    start_coords: tuple[float, float] | None = None,
) -> list[dict]:
    """
    Geocodifica la lista di waypoint utente.
    Coordinate dirette ("43.71,13.23") vengono parsificate senza API call.
    Nomi vengono risolti via geocode_place() con cache SQLite e fallback regionale:
      1. region specificata (es. "Senigallia, Italia")
      2. "Marche, Italia"
      3. "Italia"

    Sanity check: se il risultato è < PROXIMITY_WARN_KM dalla partenza, imposta
    proximity_warning=True nel dict restituito (visibile in UI come warning).

    Restituisce list[dict] con chiavi:
      name, lat, lon, source, geocoding_failed, proximity_warning (opt.), dist_from_start_km (opt.)
    """
    from geocoding_agent import geocode_place  # import locale per evitare dipendenze circolari

    fallback_regions = list(dict.fromkeys([region, "Marche, Italia", "Italia"]))
    # Nome della città di partenza (per il contesto della normalizzazione Claude)
    start_name = region.split(",")[0].strip() if "," in region else region

    def _try_geocode(query: str) -> tuple[tuple[float, float] | None, str | None]:
        """Prova il geocoding su tutti i fallback; restituisce (coords, used_region)."""
        for fb in fallback_regions:
            c = geocode_place(query, region=fb)
            if c:
                return c, fb
        return None, None

    result = []
    for wp_str in user_wps:
        name, lat, lon = _parse_user_waypoint(wp_str)
        if lat is not None:
            entry = {"name": name, "lat": lat, "lon": lon,
                     "source": "user", "geocoding_failed": False}
            if start_coords:
                dist_km = geodesic((lat, lon), start_coords).km
                if dist_km < _PROXIMITY_WARN_KM:
                    entry["proximity_warning"] = True
                    entry["dist_from_start_km"] = round(dist_km, 2)
            result.append(entry)
            continue

        # ① Normalizzazione locale (gratuita: regex, nessuna API call)
        query_name = _local_normalize(name)
        if query_name != name:
            log.info("Normalize locale: '%s' → '%s'", name, query_name)

        # ② Geocoding con fallback regionale
        coords, used_region = _try_geocode(query_name)

        # ③ Fallback Claude: normalizzazione contestuale se ancora non trovato
        if not coords:
            claude_name = _claude_normalize(query_name, start_name, region)
            if claude_name != query_name:
                coords, used_region = _try_geocode(claude_name)
                if coords:
                    query_name = claude_name  # per logging

        if coords:
            if used_region != region:
                log.info("Geocoding '%s': trovato con fallback '%s'", query_name, used_region)
            entry = {"name": name, "lat": coords[0], "lon": coords[1],
                     "source": "user", "geocoding_failed": False}
            if start_coords:
                dist_km = geodesic(coords, start_coords).km
                if dist_km < _PROXIMITY_WARN_KM:
                    entry["proximity_warning"] = True
                    entry["dist_from_start_km"] = round(dist_km, 2)
                    log.warning(
                        "Geocoding '%s' → %s risulta a %.1f km dalla partenza "
                        "— possibile omonimo locale",
                        name, coords, dist_km,
                    )
            result.append(entry)
        else:
            log.warning("Geocoding fallito per '%s' — waypoint escluso", name)
            result.append({"name": name, "lat": None, "lon": None,
                           "source": "user", "geocoding_failed": True})
    return result


def _deduplicate_waypoints(
    waypoints: list[WaypointOrdered],
    threshold_m: float = 200.0,
) -> tuple[list[WaypointOrdered], list[str]]:
    """
    Rimuove waypoint "via" entro threshold_m da un waypoint già incluso.
    start e end non vengono mai rimossi (il loop ha end = start by design).
    Restituisce (lista deduplicata, lista avvisi leggibili).
    """
    result: list[WaypointOrdered] = []
    warnings: list[str] = []

    for wp in waypoints:
        if wp.role in ("start", "end"):
            result.append(wp)
            continue
        if wp.lat is None or wp.lon is None:
            result.append(wp)
            continue

        too_close = None
        for existing in result:
            if existing.lat is None or existing.lon is None:
                continue
            dist = geodesic((wp.lat, wp.lon), (existing.lat, existing.lon)).meters
            if dist < threshold_m:
                too_close = (existing, dist)
                break

        if too_close:
            existing, dist = too_close
            warnings.append(
                f"Waypoint '{wp.name}' ignorato — troppo vicino a '{existing.name}' ({dist:.0f} m)"
            )
        else:
            result.append(wp)

    return result, warnings


def build_raw_route_prompt(
    request: RouteRequest,
    geocoded_user_wps: list[dict],
    user_memory: dict | None = None,
) -> tuple[str, str]:
    """
    Costruisce (system_prompt, user_prompt) per generate_raw_route senza API call.
    Utile per debug/preview del prompt nella UI.
    """
    start = request.start
    route_type = request.route_type
    target_km = request.target_km
    free_text = (request.free_text or "").strip()
    avoid_roads = request.avoid_named_roads or []

    valid_wps = [w for w in geocoded_user_wps if not w.get("geocoding_failed")]
    failed_wps = [w["name"] for w in geocoded_user_wps if w.get("geocoding_failed")]
    proximity_wps = [w for w in valid_wps if w.get("proximity_warning")]

    straight_total = round(target_km / 1.4)

    geo_dir = (request.geographic_direction or "").strip() or "Libera"
    _SECTOR_LABEL = {
        "Nord": "315°–45°", "Est": "45°–135°",
        "Sud": "135°–225°", "Ovest": "225°–315°",
    }
    dir_hint = (
        f"{geo_dir} (settore {_SECTOR_LABEL[geo_dir]})"
        if geo_dir in _SECTOR_LABEL else geo_dir
    )

    lines = [
        f"route_type:      {route_type}",
        f"partenza:        {start.name} (lat={start.lat}, lon={start.lon})",
        f"target:          {target_km} km (±{request.distance_tolerance_km} km)",
        f"linea d'aria:    ≈ {straight_total} km totali",
        f"tema_paesaggio:  {request.scenery_theme}",
        f"tema_atletico:   {request.athletic_theme}",
        f"geographic_direction: {dir_hint}",
    ]

    if route_type == "point_to_point" and request.end:
        e = request.end
        lines.append(f"arrivo:          {e.get('name','Arrivo')} "
                     f"(lat={e.get('lat')}, lon={e.get('lon')})")

    if avoid_roads:
        lines.append(f"strade_vietate:  {', '.join(avoid_roads)}")

    if failed_wps:
        lines.append(f"\nATTENZIONE — waypoint non geocodificati (ignorati): {', '.join(failed_wps)}")
    if proximity_wps:
        lines.append(
            "\nATTENZIONE — waypoint molto vicini alla partenza (possibili omonimi locali): "
            + ", ".join(f"{w['name']} ({w['dist_from_start_km']:.1f}km)" for w in proximity_wps)
        )

    if free_text:
        lines.append(f"\nNOTE UTENTE (PRIORITÀ ASSOLUTA):\n\"{free_text}\"")

    if user_memory:
        prefs = user_memory.get("preferences", {})
        avoid_mem = user_memory.get("avoid_always", {})
        mem_lines = []
        if prefs.get("comfortable_distance_km"):
            mem_lines.append(f"  distanza confortevole: {prefs['comfortable_distance_km']} km")
        if prefs.get("preferred_gravel_percent") is not None:
            mem_lines.append(f"  sterrato preferito: {prefs['preferred_gravel_percent']}%")
        if avoid_mem.get("roads"):
            mem_lines.append(f"  evita sempre: {', '.join(avoid_mem['roads'])}")
        if mem_lines:
            lines.append("PREFERENZE STORICHE (informative):\n" + "\n".join(mem_lines))

    # Waypoint utente in formato JSON
    wps_json = json.dumps(valid_wps, ensure_ascii=False, indent=2)

    # Schema output di riferimento
    n_order = len(valid_wps) + 1
    end_name = start.name if route_type in ("loop", "out_and_back") else "Arrivo"
    end_lat  = start.lat  if route_type in ("loop", "out_and_back") else 0.0
    end_lon  = start.lon  if route_type in ("loop", "out_and_back") else 0.0
    schema = json.dumps(
        {
            "route_narrative": (
                "Descrizione discorsiva in italiano (4–6 frasi): "
                "perché questi waypoint, lo spirito del percorso, "
                "aderenza al tema richiesto, nota che il routing reale "
                "(BRouter Fase 2) potrà discostarsi da questa visione ideale."
            ),
            "ordered_waypoints": [
                {"role": "start", "name": start.name, "lat": start.lat,
                 "lon": start.lon, "source": "user", "order": 0,
                 "rationale": None},
                {"role": "via", "name": "...", "lat": 0.0, "lon": 0.0,
                 "source": "planner", "order": 1,
                 "rationale": "motivo della scelta — fonte web o personale"},
                {"role": "end", "name": end_name, "lat": end_lat,
                 "lon": end_lon, "source": "user", "order": n_order,
                 "rationale": None},
            ],
        },
        ensure_ascii=False,
        indent=2,
    )

    user_prompt = (
        "\n".join(lines)
        + f"\n\nWAYPOINT UTENTE (già geocodificati, non modificare le coordinate):\n{wps_json}"
        + f"\n\nSchema JSON atteso:\n{schema}"
    )

    return _SYSTEM_PROMPT_RAW, user_prompt


# ── Bearing / direzione geografica ────────────────────────────────────────────

_DIRECTION_BEARING: dict[str, float] = {
    "Nord": 0.0, "Est": 90.0, "Sud": 180.0, "Ovest": 270.0,
}
_DIRECTION_SECTOR: dict[str, str] = {
    "Nord": "315°–45°", "Est": "45°–135°",
    "Sud": "135°–225°", "Ovest": "225°–315°",
}


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing in gradi [0, 360) dal punto 1 al punto 2 (0°=Nord, 90°=Est)."""
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _angular_diff(a: float, b: float) -> float:
    """Differenza angolare minima in [0, 180] tra due angoli in gradi."""
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


def _check_bearing_compliance(
    start_lat: float,
    start_lon: float,
    waypoints: list[WaypointOrdered],
    direction: str,
    tolerance_deg: float = 45.0,
) -> list[str]:
    """
    Verifica che i waypoint 'planner' siano nel settore ±tolerance_deg
    rispetto alla direzione cardinale richiesta.
    Restituisce warning (non errori) per i waypoint fuori settore.
    """
    if direction not in _DIRECTION_BEARING:
        return []
    target = _DIRECTION_BEARING[direction]
    sector = _DIRECTION_SECTOR[direction]
    out: list[str] = []
    for wp in waypoints:
        if wp.role != "via" or wp.source != "planner":
            continue
        brg = _bearing(start_lat, start_lon, wp.lat, wp.lon)
        diff = _angular_diff(brg, target)
        if diff > tolerance_deg:
            out.append(
                f"⚠ Direzione: '{wp.name}' è a {brg:.0f}° dalla partenza "
                f"(settore atteso {direction}: {sector}, scarto {diff:.0f}°)"
            )
    return out


def generate_raw_route(
    request: RouteRequest,
    user_memory: dict | None = None,
    on_event: Callable[[str, str], None] | None = None,
) -> tuple[list[WaypointOrdered], list[str], list[str], str]:
    """
    Fase 1 della pipeline a due fasi.

    1. Geocodifica request.user_waypoints con fallback regionale
    2. Chiama Claude (claude-opus-4-8) con tool web_search abilitato:
       il modello effettua 1–3 ricerche web tematiche prima di scegliere i waypoint
    3. Valida la risposta (Pydantic), deduplica waypoint entro 200 m
    4. Restituisce (ordered_waypoints, warnings, search_queries, route_narrative)
       - warnings: geocoding fallito, proximity warning, deduplicazioni
       - search_queries: query web inviate dal modello (per debug/UI)
       - route_narrative: testo evocativo italiano 4-6 frasi sullo spirito del percorso

    on_event(kind, detail) viene chiamato a eventi chiave del processo:
      "search",     query      — il modello ha avviato una ricerca web
      "results",    ""         — i risultati web sono stati consegnati al modello
      "generating", ""         — la ricerca è conclusa, si genera il JSON finale
    Gli errori dentro on_event vengono silenziati: mai interrompere la pipeline per la UI.
    """

    def _emit(kind: str, detail: str = "") -> None:
        if on_event:
            try:
                on_event(kind, detail)
            except Exception:
                pass

    _check_api_key()

    region = f"{request.start.name}, Italia"
    start_coords = (request.start.lat, request.start.lon)
    geocoded = _geocode_user_waypoints(
        request.user_waypoints,
        region=region,
        start_coords=start_coords,
    )

    warnings: list[str] = []
    for w in geocoded:
        if w.get("geocoding_failed"):
            warnings.append(f"'{w['name']}' non geocodificato — escluso dalla pianificazione")
        elif w.get("proximity_warning"):
            warnings.append(
                f"'{w['name']}' geocodificato a {w['dist_from_start_km']:.1f} km dalla partenza "
                f"(lat={w['lat']:.4f}, lon={w['lon']:.4f}) — possibile omonimo locale. "
                "Verifica le coordinate o specifica la posizione come 'lat,lon'."
            )

    system_prompt, user_prompt = build_raw_route_prompt(request, geocoded, user_memory)

    text, search_queries = ai_client.generate_with_web_search(
        system=system_prompt,
        prompt=user_prompt,
        max_tokens=4096,
    )

    for q in search_queries:
        log.info("Planner web_search: %r", q)
        _emit("search", q)

    if search_queries:
        _emit("results", str(len(search_queries)))
    _emit("generating")

    if not text.strip():
        raise RuntimeError("Nessuna risposta dal Planner.")

    raw = json.loads(_extract_json(text))
    validated = _RawRouteOutput.model_validate(raw)

    deduped, dedup_warnings = _deduplicate_waypoints(validated.ordered_waypoints)
    warnings.extend(dedup_warnings)

    for i, wp in enumerate(deduped):
        wp.order = i

    geo_dir = (request.geographic_direction or "").strip()
    if geo_dir and geo_dir != "Libera":
        bearing_warnings = _check_bearing_compliance(
            request.start.lat, request.start.lon, deduped, geo_dir
        )
        warnings.extend(bearing_warnings)

    return deduped, warnings, search_queries, validated.route_narrative


def _check_api_key() -> None:
    ai_client.check_api_key()


def _extract_json(text: str) -> str:
    """Estrae JSON dalla risposta, gestendo eventuali code fence markdown."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    return m.group(1) if m else text


def build_prompt(
    request: dict,
    user_memory: dict | None = None,
) -> tuple[str, str]:
    """
    Costruisce (system_prompt, user_prompt) senza chiamare l'API.
    Utile per debug/preview del prompt prima della chiamata Claude.
    """
    start = request["start"]
    route_type = request["route_type"]
    target_km = request["target_km"]
    distance_tolerance_km = request.get("distance_tolerance_km", 5)
    waypoints_stages = request.get("waypoints_stages", [])
    preferred_direction = request.get("preferred_direction", [])
    desired_places = request.get("desired_places", [])
    avoid_places = request.get("avoid_places", [])
    max_elevation_gain_m = request.get("max_elevation_gain_m", 800)
    end = request.get("end")
    free_text = request.get("free_text", "").strip()
    geographic_direction = request.get("geographic_direction", "Libera")

    route_type_desc = {
        "loop": "Anello — partenza e arrivo coincidono.",
        "out_and_back": "Andata-ritorno — stesso percorso in entrambe le direzioni.",
        "point_to_point": "Solo andata — partenza e arrivo sono punti diversi.",
    }[route_type]

    lines = [
        f"Tipo di percorso: {route_type_desc}",
        f"Partenza: {start['name']} (lat={start['lat']}, lon={start['lon']})",
    ]

    if route_type == "point_to_point" and end:
        end_name = end.get("name", "Arrivo")
        if end.get("lat") and end.get("lon"):
            lines.append(f"Arrivo: {end_name} (lat={end['lat']}, lon={end['lon']})")
        else:
            lines.append(f"Arrivo: {end_name} (da geocodificare)")

    if waypoints_stages:
        lines.append(f"Tappe intermedie: {', '.join(waypoints_stages)}")

    straight_total = round(target_km / 1.4)
    # Loop/out_and_back: 2 via = 3 tratti, calibrazione più robusta di 3 via = 4 tratti
    n_via_ideal = 2 if route_type in ("loop", "out_and_back") else 3
    leg_km      = round(straight_total / (n_via_ideal + 1))
    leg_km_min  = max(5, round(leg_km * 0.7))
    leg_km_max  = round(leg_km * 1.5)
    max_radius  = round(straight_total / 2.2)   # nessun waypoint oltre questo raggio dalla partenza

    lines.append(
        f"Distanza target su strada: {target_km} km (±{distance_tolerance_km} km)"
    )
    lines.append(
        f"Calibrazione (fattore tortuosità collinare 1.4×): "
        f"somma tratti in linea d'aria ≈ {straight_total} km — "
        f"con {n_via_ideal} waypoint via → ogni tratto {leg_km} km in linea d'aria "
        f"(min {leg_km_min} km, max {leg_km_max} km)"
    )
    # Bounding box lat/lon per vincolo geografico esplicito
    lat_delta = max_radius / 111.0
    lon_delta = max_radius / (111.0 * math.cos(math.radians(start["lat"])))
    bb_lat_min = round(start["lat"] - lat_delta, 3)
    bb_lat_max = round(start["lat"] + lat_delta, 3)
    bb_lon_min = round(start["lon"] - lon_delta, 3)
    bb_lon_max = round(start["lon"] + lon_delta, 3)

    lines.append(
        f"VINCOLI DI SPAZIATURA (tutti obbligatori):\n"
        f"  1. Usa ESATTAMENTE {n_via_ideal} waypoint via.\n"
        f"  2. BOUNDING BOX — ogni waypoint via deve rientrare in:\n"
        f"       lat ∈ [{bb_lat_min}, {bb_lat_max}]\n"
        f"       lon ∈ [{bb_lon_min}, {bb_lon_max}]\n"
        f"     Prima di scegliere un luogo, verifica che le sue coordinate (lat, lon)\n"
        f"     siano all'interno di questo intervallo. Escludi i luoghi che ne sono fuori.\n"
        f"  3. I {n_via_ideal} waypoint via devono essere in direzioni diverse\n"
        f"     (es. uno a NO/NE, uno a SO/SE — mai due nella stessa zona).\n"
        f"  4. Distanza in linea d'aria tra waypoint consecutivi: {leg_km_min}–{leg_km_max} km."
    )
    lines.append(f"Dislivello massimo (form): {max_elevation_gain_m} m")

    if preferred_direction:
        lines.append(f"Direzione preferita: {', '.join(preferred_direction)}")
    if desired_places:
        lines.append(f"Luoghi desiderati: {', '.join(desired_places)}")
    if avoid_places:
        lines.append(f"Da evitare: {', '.join(avoid_places)}")

    # ── Direzione geografica ──────────────────────────────────────────────────
    if geographic_direction and geographic_direction != "Libera":
        dir_map = {
            "Nord":  f"lat(via) > {start['lat']:.4f}  (a nord della partenza)",
            "Sud":   f"lat(via) < {start['lat']:.4f}  (a sud della partenza)",
            "Est":   f"lon(via) > {start['lon']:.4f}  (a est della partenza)",
            "Ovest": f"lon(via) < {start['lon']:.4f}  (a ovest della partenza)",
        }
        constraint = dir_map.get(geographic_direction, "")
        if constraint:
            lines.append(
                f"DIREZIONE GEOGRAFICA OBBLIGATORIA: {geographic_direction} — "
                f"tutti i waypoint via devono soddisfare {constraint}. "
                f"Scegli solo borghi/luoghi in quella direzione dalla partenza."
            )

    # ── Testo libero (priorità assoluta) ─────────────────────────────────────
    if free_text:
        lines.append(
            f"NOTE LIBERE DELL'UTENTE (PRIORITÀ ASSOLUTA su tutti i parametri del form):\n"
            f"  \"{free_text}\"\n"
            f"Interpreta queste note e sovrascrivi i parametri del form dove necessario. "
            f"Elenca le sovrascritture in `free_text_overrides`."
        )

    # ── UserMemory (SRS §9, §6.1) — aggiunto in Fase 10 ──────────────────────
    if user_memory:
        mem_prefs = user_memory.get("preferences", {})
        mem_avoid = user_memory.get("avoid_always", {})
        mem_bike  = user_memory.get("bike", {})

        mem_lines = []
        if mem_bike.get("model"):
            mem_lines.append(f"  - Bici: {mem_bike['model']} ({mem_bike.get('type', 'e-bike')})")
        if mem_prefs.get("comfortable_distance_km"):
            mem_lines.append(
                f"  - Distanza confortevole storica: {mem_prefs['comfortable_distance_km']} km "
                f"(intervallo {mem_prefs.get('distance_km_min', '?')}–"
                f"{mem_prefs.get('distance_km_max', '?')} km)"
            )
        if mem_prefs.get("comfortable_elevation_gain_m"):
            mem_lines.append(
                f"  - Dislivello confortevole storico: {mem_prefs['comfortable_elevation_gain_m']} m"
            )
        pct = mem_prefs.get("preferred_gravel_percent")
        if pct is not None:
            if pct < 20:
                desc = "prevalentemente asfalto"
            elif pct > 60:
                desc = "prevalentemente sterrato"
            else:
                desc = "mix bilanciato asfalto/sterrato"
            mem_lines.append(f"  - Fondo preferito: {desc} ({pct}% sterrato)")
        if mem_prefs.get("preferred_themes"):
            mem_lines.append(f"  - Temi preferiti: {', '.join(mem_prefs['preferred_themes'])}")
        if mem_avoid.get("roads"):
            mem_lines.append(f"  - Evita SEMPRE queste strade: {', '.join(mem_avoid['roads'])}")

        if mem_lines:
            lines.append(
                "PREFERENZE STORICHE DELL'UTENTE (da UserMemory — informative, "
                "usale per calibrare i waypoint ma NON sovrascrivono i parametri espliciti sopra):\n"
                + "\n".join(mem_lines)
            )

    lines.append("Profili BRouter disponibili: trekking, gravel, fastbike")

    start_wp = json.dumps(
        {
            "role": "start",
            "name": start["name"],
            "lat": start["lat"],
            "lon": start["lon"],
            "needs_geocoding": False,
        },
        ensure_ascii=False,
    )

    # Schema di riferimento — mostra la struttura di UNA strategia (devono essere 3).
    # Mostra n_via_ideal waypoint via con nota sulla distanza; via/end senza lat/lon.
    via_rows = "".join(
        f'        {{"role": "via", "name": "Borgo a ~{leg_km} km in linea d\'aria", '
        f'"needs_geocoding": true, "traversal": false, "area_hint": null}},\n'
        for _ in range(n_via_ideal)
    )
    schema_hint = (
        "{\n"
        '  "strategies": [\n'
        "    {\n"
        '      "name": "nome breve della strategia",\n'
        f'      "route_type": "{route_type}",\n'
        '      "profile": "trekking",\n'
        '      "requires_geocoding": true,\n'
        f'      "estimated_km": {target_km},\n'
        '      "rationale": "descrizione breve della strategia",\n'
        '      "free_text_overrides": [],\n'
        '      "max_elevation_gain_m_effective": null,\n'
        f'      "waypoints": [  /* ESATTAMENTE {n_via_ideal} via + start + end */\n'
        f"        {start_wp},\n"
        + via_rows
        + '        {"role": "end", "name": "Nome Luogo", "needs_geocoding": true}\n'
        "      ]\n"
        "    },\n"
        "    { (seconda strategia — profilo diverso, direzione diversa) },\n"
        "    { (terza strategia — profilo diverso, direzione diversa) }\n"
        "  ]\n"
        "}"
    )

    user_prompt = (
        "\n".join(lines)
        + f"\n\nSchema JSON atteso — ESATTAMENTE 3 strategie, ciascuna con {n_via_ideal} via waypoint. "
        + "I waypoint via/end hanno solo 'name' e 'needs_geocoding: true', mai lat/lon:\n"
        + schema_hint
    )

    return _SYSTEM_PROMPT, user_prompt


def generate_strategies(
    request: dict,
    user_memory: dict | None = None,
) -> list[dict]:
    """
    request: RouteRequest dict (SRS §5.1)
    user_memory: UserMemory dict (SRS §9) — attivo dalla Fase 10
    Ritorna lista di 3 CandidateRoute dict validati con Pydantic (SRS §5.4)
    """
    _check_api_key()

    system_prompt, user_prompt = build_prompt(request, user_memory)

    text = ai_client.generate_json(system_prompt, user_prompt, max_tokens=4000)
    if not text:
        raise RuntimeError("Nessuna risposta dal Planner.")
    raw = json.loads(_extract_json(text))

    # Validazione Pydantic — verifica schema CandidateRoute (SRS §5.4)
    validated = PlannerOutput.model_validate(raw)
    return [s.model_dump() for s in validated.strategies]
