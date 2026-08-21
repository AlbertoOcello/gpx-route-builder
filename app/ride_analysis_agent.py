"""
ride_analysis_agent.py — Analisi ciclistica personalizzata.

Flusso:
  1. analyze_gpx_bytes()  — estrae stats + metadata + track points dal GPX
  2. run_analysis()        — chiama AI con stats + profilo, ritorna dict strutturato
  3. render_html_report()  — genera HTML scaricabile con mappa PNG (tile
                             OpenStreetMap reali via staticmap) e narrativa
                             del percorso
"""
from __future__ import annotations

import html as _html_mod
import io
import json
import logging
import time
from datetime import datetime

import gpxpy
from geopy.distance import geodesic

import ai_client
from gpx_analyzer import detect_climbs, smooth_elevations, sum_uphill_downhill

_log = logging.getLogger(__name__)

# Descrizioni verbose per riding_style usate nel prompt AI
_STYLE_DESC_IT = {
    "eco":     "risparmio massimo batteria (usa assistenza minima, solo nei tratti più duri)",
    "mixed":   "pedalata mista (alterna livelli bassi e medi, equilibrio sforzo/batteria)",
    "comfort": "comfort totale (assistenza medio-alta per mantenere la frequenza cardiaca bassa)",
    "max":     "massima assistenza (usa sempre il livello più alto disponibile)",
}
_STYLE_DESC_EN = {
    "eco":     "maximum battery saving (minimal assistance, only on the hardest sections)",
    "mixed":   "mixed riding (alternates low and medium levels, balances effort and battery)",
    "comfort": "full comfort (medium-high assistance to keep heart rate low)",
    "max":     "maximum assistance (always uses the highest available level)",
}
_STYLE_LABELS = {
    "it": {
        "eco": "🔋 Risparmio batteria",
        "mixed": "⚡ Pedalata mista",
        "comfort": "😌 Comfort totale",
        "max": "🚀 Massima assistenza",
    },
    "en": {
        "eco": "🔋 Battery saving",
        "mixed": "⚡ Mixed riding",
        "comfort": "😌 Full comfort",
        "max": "🚀 Maximum assistance",
    },
}


def analyze_gpx_bytes(file_bytes: bytes) -> dict:
    """Parse GPX bytes and return distance/elevation stats + metadata + track points."""
    gpx = gpxpy.parse(io.BytesIO(file_bytes))
    points = [
        pt
        for track in gpx.tracks
        for seg in track.segments
        for pt in seg.points
    ]
    if not points:
        raise ValueError("Il file GPX non contiene punti traccia")

    distance_m = gpx.length_2d()
    elevations = [pt.elevation for pt in points if pt.elevation is not None]

    # Route name: gpx.name → first track name → None
    gpx_name = (
        (gpx.name or "").strip()
        or (gpx.tracks[0].name or "").strip()
        or None
    )

    # Sample track points (max 600) for the map
    step = max(1, len(points) // 600)
    sampled = [(pt.latitude, pt.longitude) for pt in points[::step]]
    last = (points[-1].latitude, points[-1].longitude)
    if sampled[-1] != last:
        sampled.append(last)

    # Rilevamento salite (stessa logica condivisa del Builder — gpx_analyzer.detect_climbs)
    cum_m = [0.0] * len(points)
    for i in range(1, len(points)):
        cum_m[i] = cum_m[i - 1] + geodesic(
            (points[i - 1].latitude, points[i - 1].longitude),
            (points[i].latitude, points[i].longitude),
        ).meters
    climb_data = detect_climbs(cum_m, [pt.elevation for pt in points])

    # Dislivello totale su elevazione smussata (gpx_analyzer.smooth_elevations),
    # non gpx.get_uphill_downhill() sul dato grezzo — stessa ragione di
    # gpx_analyzer.analyze_gpx: il rumore sorgente (SRTM via BRouter) gonfia
    # altrimenti il dislivello, e da qui dipende anche il calcolo energetico
    # deterministico (dislivello_reale_m).
    _, smoothed_elev = smooth_elevations(cum_m, [pt.elevation for pt in points])
    uphill, downhill = sum_uphill_downhill(smoothed_elev)

    return {
        "distance_km": round(distance_m / 1000, 2),
        "elevation_gain_m": round(uphill, 0),
        "elevation_loss_m": round(downhill, 0),
        "max_elevation_m": round(max(elevations), 0) if elevations else None,
        "min_elevation_m": round(min(elevations), 0) if elevations else None,
        "gpx_name": gpx_name,
        "track_points": sampled,
        "climbs": climb_data["climbs"],
        "elevation_profile": {
            "distances_km": climb_data["profile_distances_km"],
            "elevations_m": climb_data["profile_elevations_m"],
            "colors": climb_data["profile_colors"],
        },
    }


# ── Map rendering ──────────────────────────────────────────────────────────────

def _track_map_png_b64(
    track_points: list[tuple[float, float]],
    width: int = 640,
    height: int = 320,
) -> str | None:
    """
    Render GPX track as PNG with a real OpenStreetMap tile background (via
    staticmap) — stessa fonte tile (tile.openstreetmap.org) usata di default
    da Folium nelle altre mappe dell'app (Builder/Planner/Utility), qui
    scaricata e composta lato server perché il report HTML deve restare
    un'immagine statica autosufficiente, non una mappa interattiva Folium/
    Leaflet. Returns base64 string or None on any failure (e.g. tile server
    unreachable) — il chiamante omette la card mappa in tal caso, nessun crash.
    """
    if len(track_points) < 2:
        return None
    try:
        import base64
        from staticmap import CircleMarker, Line, StaticMap

        m = StaticMap(
            width, height,
            headers={"User-Agent": "gpx-route-builder/1.0"},
            tile_request_timeout=5,
        )
        coords_lonlat = [(lon, lat) for lat, lon in track_points]
        m.add_line(Line(coords_lonlat, "#0055cc", 4))
        m.add_marker(CircleMarker(coords_lonlat[0], "#27ae60", 12))
        m.add_marker(CircleMarker(coords_lonlat[-1], "#e74c3c", 12))
        image = m.render()

        buf = io.BytesIO()
        image.save(buf, format="png")
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode()
        _log.info("[ride_analysis] OSM tile map PNG ok: %d chars base64", len(b64))
        return b64
    except Exception as exc:
        _log.warning("[ride_analysis] OSM tile map PNG failed: %s", exc)
        return None


# ── AI analysis ────────────────────────────────────────────────────────────────

def run_analysis(gpx_stats: dict, profile: dict, lang: str) -> dict:
    """
    Call the AI with GPX stats + rider profile.
    Returns a dict with keys:
      battery_pct_consumed, range_remaining_km, estimated_assistance_level
        (all null if not ebike),
      calories_kcal, time_estimate_min, avg_hr_bpm (null if no fcmax),
      fatigue_index (1-10), advice (list[str]), disclaimer (str),
      climbs_analysis (list[dict], one per detected climb — empty if none),
      hardest_climb_index (int|null), hardest_climb_reason (str|null)
    """
    is_ebike = (profile.get("bike_type") or "").lower() == "ebike"
    lang_instr = "Rispondi in italiano." if lang == "it" else "Reply in English."
    style_map = _STYLE_DESC_IT if lang == "it" else _STYLE_DESC_EN
    climbs = gpx_stats.get("climbs") or []

    battery_field = (
        '  "battery_pct_consumed": <float, quota di battery_pct_consumed totale attribuibile '
        'a questa salita>,\n' if is_ebike else ""
    )

    climbs_schema = (
        "\n"
        '  "climbs_analysis": [\n'
        "    {\n"
        '      "climb_index": <integer — indice della salita, 0-based, stesso ordine della lista sopra>,\n'
        '      "kcal": <integer — calorie stimate per QUESTA salita>,\n'
        '      "avg_hr_bpm": <integer oppure null se FC max non disponibile>,\n'
        '      "fatigue_contribution": <integer 1-10 — quanto pesa QUESTA salita sulla fatica totale, '
        "per QUESTO profilo bici+ciclista>,\n"
        f"{battery_field}"
        "    }\n"
        "    // un oggetto per ogni salita elencata sopra, stesso ordine\n"
        "  ],\n"
        '  "hardest_climb_index": <integer — climb_index della salita soggettivamente più dura per '
        "QUESTO profilo, oppure null se non ci sono salite>,\n"
        '  "hardest_climb_reason": "<breve motivazione testuale, oppure null se non ci sono salite>"\n'
    )

    system = (
        "Sei un esperto di ciclismo, biomeccanica e fisiologia dello sport.\n"
        "Analizza il giro ciclabile descritto e fornisci stime personalizzate realistiche.\n"
        f"{lang_instr}\n"
        "Rispondi ESCLUSIVAMENTE con JSON valido, senza testo aggiuntivo, senza markdown, senza code fence, "
        "senza commenti (i commenti // nello schema sotto sono solo per spiegarti la struttura, NON vanno "
        "riprodotti nella risposta).\n"
        "Schema JSON da rispettare ESATTAMENTE (non aggiungere né rimuovere chiavi):\n"
        "{\n"
        '  "battery_pct_consumed": <float 0-100 oppure null se non ebike>,\n'
        '  "range_remaining_km": <float oppure null se non ebike>,\n'
        '  "estimated_assistance_level": <float 1.0-5.0 stimato dall\'AI oppure null se non ebike>,\n'
        '  "calories_kcal": <integer>,\n'
        '  "time_estimate_min": <integer>,\n'
        '  "avg_hr_bpm": <integer oppure null se FC max non disponibile>,\n'
        '  "fatigue_index": <integer 1-10>,\n'
        '  "advice": [<string>, <string>, <string>],\n'
        '  "disclaimer": "<testo disclaimer medico nella lingua richiesta>",\n'
        f"{climbs_schema}"
        "}\n"
        "Il campo estimated_assistance_level rappresenta il livello medio di assistenza (scala 1-5) "
        "che stimi questo ciclista utilizzerà su questo specifico percorso, tenendo conto dello stile "
        "dichiarato, della forma fisica, del dislivello e della distanza.\n"
        "Il disclaimer deve essere: \"Questa analisi è puramente indicativa e non costituisce diagnosi "
        "medica. Consulta un medico per valutazioni sulla tua salute.\" in italiano oppure "
        "\"This analysis is indicative only and does not constitute medical advice. Consult a doctor "
        "for health assessments.\" in inglese.\n\n"
        "Sulle salite (climbs_analysis):\n"
        "- climbs_analysis DEVE avere esattamente un elemento per ogni salita elencata nei dati GPX "
        "sotto, nello stesso ordine (se non ci sono salite elencate, climbs_analysis è un array vuoto "
        "[] e hardest_climb_index/hardest_climb_reason sono null).\n"
        "- La somma approssimativa di kcal/battery_pct_consumed su tutte le salite deve essere "
        "ragionevolmente coerente con calories_kcal/battery_pct_consumed totali sull'intero percorso "
        "(non serve quadratura esatta, ma non può superarli né essere trascurabile rispetto ad essi se "
        "le salite coprono una parte significativa del dislivello totale).\n"
        "- hardest_climb_index NON deve essere automaticamente la salita con pendenza media o massima "
        "più alta in assoluto: valuta l'impatto REALE su QUESTO ciclista con QUESTO mezzo — una salita "
        "lunga ma dolce può pesare di più su un ciclista poco allenato di uno strappo breve, ed "
        "eventualmente assistito dal motore se ebike. Spiega il perché in hardest_climb_reason."
    )

    lines = [
        "## Dati GPX del giro",
        f"- Distanza: {gpx_stats['distance_km']} km",
        f"- Dislivello positivo: {gpx_stats['elevation_gain_m']:.0f} m",
        f"- Dislivello negativo: {gpx_stats['elevation_loss_m']:.0f} m",
    ]
    if gpx_stats.get("max_elevation_m"):
        lines.append(f"- Quota massima: {gpx_stats['max_elevation_m']:.0f} m")
    if gpx_stats.get("min_elevation_m"):
        lines.append(f"- Quota minima: {gpx_stats['min_elevation_m']:.0f} m")

    lines += ["", "## Profilo bici"]
    lines.append(f"- Tipo: {profile.get('bike_type', 'non specificato')}")
    if profile.get("bike_model"):
        lines.append(f"- Modello: {profile['bike_model']}")
    if is_ebike:
        if profile.get("wh"):
            lines.append(f"- Capacità batteria: {profile['wh']} Wh")
        if profile.get("battery_pct") is not None:
            lines.append(f"- Stato batteria iniziale: {profile['battery_pct']}%")
        min_batt = profile.get("min_battery_pct") or 0
        if min_batt > 0:
            lines.append(
                f"- Autonomia minima desiderata a fine percorso: {min_batt}% "
                "(vincolo: la batteria non deve scendere sotto questo valore)"
            )
        style_code = profile.get("riding_style") or "mixed"
        lines.append(
            f"- Stile di utilizzo assistenza: {style_map.get(style_code, style_map['mixed'])}"
        )
    if profile.get("bike_weight_kg"):
        lines.append(f"- Peso bici: {profile['bike_weight_kg']} kg")

    lines += ["", "## Profilo ciclista"]
    if profile.get("driver_weight_kg"):
        lines.append(f"- Peso: {profile['driver_weight_kg']} kg")
    if profile.get("driver_age"):
        lines.append(f"- Età: {profile['driver_age']} anni")
    if profile.get("driver_sex"):
        lines.append(f"- Sesso: {profile['driver_sex']}")
    if profile.get("driver_fitness"):
        lines.append(f"- Forma fisica: {profile['driver_fitness']}/5")
    if profile.get("driver_fcmax"):
        lines.append(f"- FC max: {profile['driver_fcmax']} bpm")
    if profile.get("driver_health_notes"):
        lines.append(f"- Note salute: {profile['driver_health_notes']}")

    if climbs:
        lines += ["", "## Salite rilevate lungo il percorso (ordine = climb_index)"]
        for i, c in enumerate(climbs):
            lines.append(
                f"- [{i}] km {c['start_km']:.1f}, lunghezza {c['length_m']:.0f} m, "
                f"dislivello {c['elevation_gain_m']:.0f} m, pendenza media "
                f"{c['avg_gradient_percent']:.1f}% (max {c['max_gradient_percent']:.1f}%), "
                f"classificazione: {c['classification']}"
                + (f" — {c['note']}" if c.get("note") else "")
            )
    else:
        lines += ["", "## Salite rilevate lungo il percorso", "Nessuna salita rilevante rilevata."]

    if not is_ebike:
        lines += [
            "",
            "Nota: NON è una ebike → battery_pct_consumed, range_remaining_km e "
            "estimated_assistance_level DEVONO essere null, e gli oggetti in climbs_analysis "
            "NON devono contenere la chiave battery_pct_consumed.",
        ]
    if not profile.get("driver_fcmax"):
        lines += [
            "", "Nota: FC max non disponibile → avg_hr_bpm DEVE essere null, sia il campo "
            "totale sia quello in ciascun elemento di climbs_analysis.",
        ]

    prompt = "\n".join(lines)
    max_tokens = 1200 + 150 * len(climbs)

    _MAX_ATTEMPTS = 3  # 1 tentativo + 2 retry
    last_exc: json.JSONDecodeError | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        raw = ai_client.generate_json(system, prompt, max_tokens=max_tokens)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            # Glitch di campionamento del modello su risposte JSON lunghe/ripetitive
            # (osservato empiricamente: caratteri spuri iniettati dentro array come
            # climbs_analysis, 0-12% delle run, più frequente con molte salite) —
            # non un problema di prompt: una nuova generazione pulita risolve quasi
            # sempre. Non si tenta di "riparare" il JSON malformato: troppo fragile,
            # rischia di introdurre dati sbagliati silenziosamente.
            last_exc = exc
            _log.warning(
                "[ride_analysis] JSON non valido al tentativo %d/%d (%s) — risposta grezza:\n%s",
                attempt, _MAX_ATTEMPTS, exc, raw,
            )
            if attempt < _MAX_ATTEMPTS:
                time.sleep(1.0)

    msg = (
        f"L'analisi AI ha prodotto una risposta non valida dopo {_MAX_ATTEMPTS} tentativi — riprova."
        if lang == "it" else
        f"The AI analysis produced an invalid response after {_MAX_ATTEMPTS} attempts — please retry."
    )
    raise RuntimeError(msg) from last_exc


def merge_climbs_analysis(gpx_climbs: list[dict], climbs_analysis: list[dict] | None) -> list[dict]:
    """
    Unisce i dati oggettivi delle salite (gpx_analyzer.detect_climbs, via
    analyze_gpx_bytes) con quelli soggettivi restituiti dall'AI (run_analysis
    → climbs_analysis), abbinati per climb_index. Stesso ordine di gpx_climbs;
    i campi soggettivi sono None se l'AI non ha fornito un elemento per quell'indice.
    """
    by_index = {c.get("climb_index"): c for c in (climbs_analysis or [])}
    merged = []
    for i, obj in enumerate(gpx_climbs or []):
        subj = by_index.get(i, {})
        merged.append({
            **obj,
            "climb_index": i,
            "kcal": subj.get("kcal"),
            "avg_hr_bpm": subj.get("avg_hr_bpm"),
            "fatigue_contribution": subj.get("fatigue_contribution"),
            "battery_pct_consumed": subj.get("battery_pct_consumed"),
        })
    return merged


# ── Deterministic post-ride energy analysis ─────────────────────────────────────
# Non basata su stima AI: confronta l'energia meccanica richiesta dal percorso
# realmente pedalato con quanto dichiarato consumato dalla batteria, per
# derivare un rapporto di assistenza motore comparabile tra percorsi di
# lunghezza diversa (assist_ratio) e le calorie bruciate dal ciclista per
# differenza (rider_kcal). Solo per percorsi reali (Opzione D) con dati
# batteria inizio/fine — mai usata per candidati Builder A/B/C, che restano
# sulla stima AI ipotetica di run_analysis().

def compute_deterministic_energy_analysis(
    gpx_stats: dict,
    profile: dict,
    battery_start_pct: float,
    battery_end_pct: float,
    wind_condition: str = "none",  # "none" | "headwind" | "tailwind"
    virtual_grade_pct: float = 1.3,
    motor_efficiency: float = 0.80,
    human_efficiency: float = 0.23,
    g: float = 9.81,
    wh_per_kcal: float = 1.163,
    wind_headwind_delta_pct: float = 1.75,
    wind_tailwind_delta_pct: float = 1.3,
    assist_ratio_warn_threshold: float = 0.95,
) -> dict:
    """
    Calcola l'energia meccanica richiesta dal percorso (peso totale × g ×
    dislivello reale+virtuale), la confronta con l'energia batteria dichiarata
    consumata (capacity_used_pct × wh × efficienza motore) per ottenere
    assist_ratio, e ne deriva le calorie del ciclista per differenza.

    wind_condition corregge virtual_grade_pct solo per questo calcolo
    (correzione qualitativa, non persistita come default). assist_ratio non
    viene troncato anche se >1: è un segnale utile (giornata anomala/vento
    non dichiarato), non un errore — vedi assist_ratio_anomalous.

    Solleva ValueError se al profilo mancano i dati necessari (capacità
    batteria, peso bici/ciclista).
    """
    capacity_wh = profile.get("wh")
    bike_weight_kg = profile.get("bike_weight_kg")
    driver_weight_kg = profile.get("driver_weight_kg")
    if not capacity_wh:
        raise ValueError("Capacità batteria (Wh) mancante nel profilo — richiesta per il calcolo deterministico")
    if not bike_weight_kg or not driver_weight_kg:
        raise ValueError("Peso bici e/o peso ciclista mancanti nel profilo")

    peso_totale_kg = bike_weight_kg + driver_weight_kg
    dislivello_reale_m = gpx_stats["elevation_gain_m"]
    distanza_m = gpx_stats["distance_km"] * 1000

    effective_grade_pct = virtual_grade_pct
    if wind_condition == "headwind":
        effective_grade_pct += wind_headwind_delta_pct
    elif wind_condition == "tailwind":
        effective_grade_pct = max(0.0, effective_grade_pct - wind_tailwind_delta_pct)

    dislivello_virtuale_m = distanza_m * (effective_grade_pct / 100)
    e_required_j = peso_totale_kg * g * (dislivello_reale_m + dislivello_virtuale_m)
    e_required_wh = e_required_j / 3600

    capacity_used_pct = battery_start_pct - battery_end_pct
    capacity_used_wh = (capacity_used_pct / 100) * capacity_wh

    motor_mechanical_output_wh = capacity_used_wh * motor_efficiency
    assist_ratio = (motor_mechanical_output_wh / e_required_wh) if e_required_wh else 0.0

    rider_mechanical_output_wh = max(0.0, e_required_wh - motor_mechanical_output_wh)
    rider_kcal = (rider_mechanical_output_wh / human_efficiency) * wh_per_kcal

    return {
        "peso_totale_kg": peso_totale_kg,
        "dislivello_reale_m": dislivello_reale_m,
        "distanza_m": distanza_m,
        "virtual_grade_pct_used": effective_grade_pct,
        "dislivello_virtuale_m": dislivello_virtuale_m,
        "e_required_wh": e_required_wh,
        "battery_start_pct": battery_start_pct,
        "battery_end_pct": battery_end_pct,
        "capacity_wh": capacity_wh,
        "capacity_used_pct": capacity_used_pct,
        "capacity_used_wh": capacity_used_wh,
        "motor_mechanical_output_wh": motor_mechanical_output_wh,
        "motor_efficiency": motor_efficiency,
        "assist_ratio": assist_ratio,
        "rider_mechanical_output_wh": rider_mechanical_output_wh,
        "rider_kcal": rider_kcal,
        "wind_condition": wind_condition,
        "assist_ratio_anomalous": assist_ratio >= assist_ratio_warn_threshold,
    }


# ── HTML report ────────────────────────────────────────────────────────────────

def _dash(val: object) -> str:
    """Return str(val) or '—' if val is None/falsy-zero."""
    if val is None or val == "" or val == 0:
        return "—"
    return str(val)


def render_html_report(
    analysis: dict,
    gpx_stats: dict,
    profile: dict,
    lang: str,
    route_narrative: str | None = None,
    det_result: dict | None = None,
) -> str:
    """
    Generate a self-contained downloadable HTML report with:
    - Static PNG map with real OpenStreetMap tile background (see _track_map_png_b64)
      — omitted if the tile server is unreachable
    - Route info (name from GPX metadata, distance, elevation)
    - Planner narrative (between route data and profile, if provided)
    - Complete bike + rider profile
    - Deterministic energy calculation (det_result, real-ride only — see
      compute_deterministic_energy_analysis), when provided
    - AI analysis results — relabelled as an explicit hypothesis, visually
      distinct, only when det_result is also present (same contrast as the
      live UI; with no deterministic result to contrast against, the AI
      section stays a plain "Results" card, unchanged)
    - Personalised advice
    - Medical disclaimer
    """
    is_en = lang == "en"
    is_ebike = (profile.get("bike_type") or "").lower() == "ebike"
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    style_labels = _STYLE_LABELS.get(lang, _STYLE_LABELS["it"])

    # ── Labels ────────────────────────────────────────────────────────────────
    if is_en:
        title = "🔋 Ride Analysis Report"
        subtitle = "GPX Route Builder — Personalised cycling analysis"
        s_map = "Route Map"
        s_route = "Route Data"
        s_narrative = "Route Spirit"
        s_bike = "Bike"
        s_rider = "Rider"
        s_profile_full = "Analysis Profile"
        s_results = "Analysis Results"
        s_advice = "Personalised Advice"
        s_disclaimer = "⚠️ Medical Disclaimer"
        l_dist = "Distance"
        l_elev_up = "Elevation gain"
        l_elev_down = "Elevation loss"
        l_max_alt = "Max altitude"
        l_bike_model = "Model"
        l_bike_type = "Type"
        l_wh = "Battery capacity"
        l_battery_init = "Initial battery"
        l_min_battery = "Min. battery reserve"
        l_bike_weight = "Bike weight"
        l_style = "Usage style"
        l_driver_weight = "Weight"
        l_driver_age = "Age"
        l_driver_sex = "Sex"
        l_driver_fitness = "Fitness"
        l_driver_fcmax = "Max HR"
        l_driver_health = "Health notes"
        l_battery_consumed = "Battery consumed (est.)"
        l_range = "Remaining range (est.)"
        l_est_assist = "Est. assist. level"
        l_calories = "Calories burned"
        l_time = "Estimated time"
        l_hr = "Avg heart rate (est.)"
        l_fatigue = "Fatigue index"
        l_generated = "Generated on"
        l_start = "Start"
        l_end = "End"
        l_type_loop = "🔁 Loop"
        l_type_p2p = "➡️ Point-to-point"
        s_climbs = "Climbs on this ride"
        l_c_start = "Start km"
        l_c_length = "Length"
        l_c_gain = "Elevation gain"
        l_c_avg_grad = "Avg gradient"
        l_c_max_grad = "Max gradient"
        l_c_class = "Class"
        l_c_kcal = "Kcal"
        l_c_hr = "Avg HR"
        l_c_fatigue = "Fatigue"
        l_c_battery = "Battery"
        l_c_hardest = "💪 The hardest for you"
        l_c_no_climbs = "No significant climb detected on this route."
        l_c_low_confidence_note = (
            "ⓘ low confidence on one or more max gradients — sparse GPS/elevation "
            "data at that specific spot, the value is still shown but may not be "
            "fully reliable."
        )
        s_det = "📊 Deterministic Calculation (Real Ride)"
        l_det_e_required = "Energy required by the route"
        l_det_capacity_used = "Battery energy consumed"
        l_det_assist_ratio = "Motor assistance ratio"
        l_det_rider_kcal = "Rider calories (by difference)"
        l_det_anomalous = (
            "Unusually high ratio — possibly an undeclared strong headwind, "
            "or other conditions the model doesn't capture."
        )
        det_breakdown_line1_tpl = (
            "{battery_wh} Wh battery × {motor_pct}% motor efficiency = {motor_wh} Wh of motor work"
        )
        det_breakdown_line2_tpl = (
            "{required_wh} Wh required − {motor_wh} Wh motor = {rider_wh} Wh from your legs (→ {rider_kcal} kcal)"
        )
        s_results_ai = "⚠️ AI Estimate — a probabilistic guess, not a calculation"
        ai_caption_text = (
            "These numbers are an estimate based on your profile and route statistics — "
            "they don't measure anything real. For an actual calculation, use \"this is a "
            "real ride\" with your actual battery data."
        )
    else:
        title = "🔋 Report Analisi Giro"
        subtitle = "GPX Route Builder — Analisi ciclistica personalizzata"
        s_map = "Mappa Percorso"
        s_route = "Dati Percorso"
        s_narrative = "Spirito del Percorso"
        s_bike = "Bici"
        s_rider = "Ciclista"
        s_profile_full = "Profilo Analisi"
        s_results = "Risultati Analisi"
        s_advice = "Consigli Personalizzati"
        s_disclaimer = "⚠️ Disclaimer Medico"
        l_dist = "Distanza"
        l_elev_up = "Dislivello +"
        l_elev_down = "Dislivello −"
        l_max_alt = "Quota max"
        l_bike_model = "Modello"
        l_bike_type = "Tipo"
        l_wh = "Capacità batteria"
        l_battery_init = "Batteria iniziale"
        l_min_battery = "Autonomia minima"
        l_bike_weight = "Peso bici"
        l_style = "Stile utilizzo"
        l_driver_weight = "Peso"
        l_driver_age = "Età"
        l_driver_sex = "Sesso"
        l_driver_fitness = "Forma fisica"
        l_driver_fcmax = "FC max"
        l_driver_health = "Note salute"
        l_battery_consumed = "Batteria consumata (stima)"
        l_range = "Autonomia residua (stima)"
        l_est_assist = "Livello assist. stimato"
        l_calories = "Calorie consumate"
        l_time = "Tempo stimato"
        l_hr = "FC media stimata"
        l_fatigue = "Indice di fatica"
        l_generated = "Generato il"
        l_start = "Partenza"
        l_end = "Arrivo"
        l_type_loop = "🔁 Anello (loop)"
        l_type_p2p = "➡️ Punto-punto"
        s_climbs = "Salite di questo giro"
        l_c_start = "km inizio"
        l_c_length = "Lunghezza"
        l_c_gain = "Dislivello"
        l_c_avg_grad = "Pend. media"
        l_c_max_grad = "Pend. max"
        l_c_class = "Classe"
        l_c_kcal = "Kcal"
        l_c_hr = "FC media"
        l_c_fatigue = "Fatica"
        l_c_battery = "Batteria"
        l_c_hardest = "💪 La più dura per te"
        l_c_no_climbs = "Nessuna salita rilevante rilevata su questo percorso."
        l_c_low_confidence_note = (
            "ⓘ bassa confidenza su una o più pendenze massime — dati GPS/elevazione "
            "radi in quel punto specifico, il valore è mostrato ma potrebbe non "
            "essere del tutto affidabile."
        )
        s_det = "📊 Calcolo Deterministico (Percorso Reale)"
        l_det_e_required = "Energia richiesta dal percorso"
        l_det_capacity_used = "Energia batteria consumata"
        l_det_assist_ratio = "Rapporto assistenza motore"
        l_det_rider_kcal = "Calorie ciclista (per differenza)"
        l_det_anomalous = (
            "Rapporto insolitamente alto — possibile vento forte contrario non "
            "dichiarato, o altre condizioni che il modello non cattura."
        )
        det_breakdown_line1_tpl = (
            "{battery_wh} Wh batteria × {motor_pct}% efficienza motore = {motor_wh} Wh di lavoro motore"
        )
        det_breakdown_line2_tpl = (
            "{required_wh} Wh richiesti − {motor_wh} Wh motore = {rider_wh} Wh dalle tue gambe (→ {rider_kcal} kcal)"
        )
        s_results_ai = "⚠️ Stima AI — ipotesi probabilistica, non un calcolo"
        ai_caption_text = (
            "Questi numeri sono una stima basata su profilo e statistiche del percorso — "
            "non misurano nulla di reale. Per un calcolo vero, usa \"percorso realmente "
            "pedalato\" con i dati di batteria effettivi."
        )

    # ── Map section ───────────────────────────────────────────────────────────
    track_points = gpx_stats.get("track_points") or []
    map_section = ""
    if track_points:
        b64 = _track_map_png_b64(track_points)
        if b64:
            legend = (
                f'<span style="color:#27ae60">●</span> {l_start} &nbsp;'
                f'<span style="color:#e74c3c">●</span> {l_end}'
            )
            map_section = f"""
<div class="card">
  <h2>🗺️ {s_map}</h2>
  <img src="data:image/png;base64,{b64}"
       style="width:100%;border-radius:8px;display:block" alt="Route map">
  <div style="font-size:.75rem;color:#888;margin-top:6px;text-align:center">{legend}</div>
</div>"""

    # ── Route info ────────────────────────────────────────────────────────────
    gpx_name = gpx_stats.get("gpx_name") or "—"
    elev_up = f"{gpx_stats['elevation_gain_m']:.0f} m"
    elev_down = f"{gpx_stats['elevation_loss_m']:.0f} m"
    max_alt = (
        f"{gpx_stats['max_elevation_m']:.0f} m"
        if gpx_stats.get("max_elevation_m") else "—"
    )

    # ── Location banner (start/end + loop vs point-to-point) ───────────────────
    # Nomi già risolti dal reverse geocoding lato Streamlit (stessa funzione
    # geocoding_agent.reverse_geocode_address di Utility → Visualizza GPX,
    # con fallback alle coordinate grezze già applicato lì) e messi in cache
    # dentro gpx_stats — non ricalcolati qui. Mostrato subito dopo l'header,
    # prima della card "Dati Percorso"/GPX Statistics.
    location_section = ""
    _loc_start = gpx_stats.get("location_start_name")
    if _loc_start:
        _loc_is_loop = bool(gpx_stats.get("location_is_loop"))
        _loc_end = gpx_stats.get("location_end_name")
        _loc_type_label = l_type_loop if _loc_is_loop else l_type_p2p
        location_section = f"""
<div class="card location-card">
  <div class="loc-type">{_loc_type_label}</div>
  <div class="loc-line">📍 {l_start}: <strong>{_html_mod.escape(_loc_start)}</strong></div>"""
        if _loc_end:
            location_section += f"""
  <div class="loc-line">🏁 {l_end}: <strong>{_html_mod.escape(_loc_end)}</strong></div>"""
        location_section += """
</div>"""

    # ── Narrative section (between route data and profile) ────────────────────
    narrative_section = ""
    if route_narrative and route_narrative.strip():
        narrative_section = f"""
<div class="card">
  <h2>📖 {s_narrative}</h2>
  <p class="narrative-text">{_html_mod.escape(route_narrative.strip())}</p>
</div>"""

    # ── Profile rows ──────────────────────────────────────────────────────────
    def prow(label: str, value: str) -> str:
        return (
            f'<div class="prow">'
            f'<span class="plbl">{label}</span>'
            f'<span class="pval">{value}</span>'
            f"</div>"
        )

    bike_rows = [
        prow(l_bike_model, _dash(profile.get("bike_model"))),
        prow(l_bike_type, _dash(profile.get("bike_type"))),
        prow(l_bike_weight, f"{profile['bike_weight_kg']} kg" if profile.get("bike_weight_kg") else "—"),
    ]
    if is_ebike:
        bike_rows += [
            prow(l_wh, f"{profile['wh']} Wh" if profile.get("wh") else "—"),
            prow(l_battery_init, f"{profile['battery_pct']}%" if profile.get("battery_pct") is not None else "—"),
            prow(l_min_battery, f"{profile['min_battery_pct']}%" if profile.get("min_battery_pct") else "0%"),
            prow(l_style, style_labels.get(profile.get("riding_style") or "mixed", "—")),
        ]

    driver_rows = [
        prow(l_driver_weight, f"{profile['driver_weight_kg']} kg" if profile.get("driver_weight_kg") else "—"),
        prow(l_driver_age, f"{profile['driver_age']}" if profile.get("driver_age") else "—"),
        prow(l_driver_sex, _dash(profile.get("driver_sex"))),
        prow(l_driver_fitness, f"{profile['driver_fitness']}/5" if profile.get("driver_fitness") else "—"),
        prow(l_driver_fcmax, f"{profile['driver_fcmax']} bpm" if profile.get("driver_fcmax") else "—"),
        prow(l_driver_health, _dash(profile.get("driver_health_notes"))),
    ]

    bike_html = "\n".join(bike_rows)
    driver_html = "\n".join(driver_rows)

    # ── Results ───────────────────────────────────────────────────────────────
    time_min = analysis.get("time_estimate_min")
    if time_min:
        h, m = divmod(int(time_min), 60)
        time_str = f"{h}h {m:02d}m" if h else f"{m}min"
    else:
        time_str = "—"

    fatigue = analysis.get("fatigue_index", "—")
    fatigue_color = (
        "#27ae60" if isinstance(fatigue, int) and fatigue <= 3
        else "#f39c12" if isinstance(fatigue, int) and fatigue <= 6
        else "#e74c3c"
    )
    avg_hr = analysis.get("avg_hr_bpm")
    hr_str = f"{avg_hr} bpm" if avg_hr else "—"
    batt = analysis.get("battery_pct_consumed")
    rng = analysis.get("range_remaining_km")
    est_assist = analysis.get("estimated_assistance_level")

    battery_html = ""
    if is_ebike:
        battery_html = f"""
  <div class="metric-row">
    <div class="metric"><div class="lbl">{l_battery_consumed}</div>
      <div class="val">{f"{batt:.0f}%" if batt is not None else "—"}</div></div>
    <div class="metric"><div class="lbl">{l_range}</div>
      <div class="val">{f"{rng:.0f} km" if rng is not None else "—"}</div></div>
    <div class="metric"><div class="lbl">{l_est_assist}</div>
      <div class="val">{f"{est_assist:.1f}/5" if est_assist is not None else "—"}</div></div>
  </div>"""

    advice_items = "".join(
        f"<li>{_html_mod.escape(adv)}</li>" for adv in (analysis.get("advice") or [])
    )
    disclaimer_text = _html_mod.escape(analysis.get("disclaimer", ""))

    # ── Deterministic energy section (real-ride only) ──────────────────────────
    # When present, also relabels the Results card below as an explicit
    # hypothesis (same contrast as the live UI) — with no deterministic
    # result to contrast against, Results stays a plain, unlabelled card.
    det_section = ""
    results_card_class = "card"
    results_header = f"📊 {s_results}"
    ai_caption_html = ""
    if det_result:
        det_breakdown_line1 = det_breakdown_line1_tpl.format(
            battery_wh=f"{det_result['capacity_used_wh']:.0f}",
            motor_pct=f"{det_result['motor_efficiency'] * 100:.0f}",
            motor_wh=f"{det_result['motor_mechanical_output_wh']:.0f}",
        )
        det_breakdown_line2 = det_breakdown_line2_tpl.format(
            required_wh=f"{det_result['e_required_wh']:.0f}",
            motor_wh=f"{det_result['motor_mechanical_output_wh']:.0f}",
            rider_wh=f"{det_result['rider_mechanical_output_wh']:.0f}",
            rider_kcal=f"{det_result['rider_kcal']:.0f}",
        )
        det_warning_html = (
            f'<p class="det-warning">⚠️ {_html_mod.escape(l_det_anomalous)}</p>'
            if det_result.get("assist_ratio_anomalous") else ""
        )
        det_section = f"""
<div class="card">
  <h2>{s_det}</h2>
  <div class="metric-row">
    <div class="metric"><div class="lbl">{l_det_e_required}</div>
      <div class="val">{det_result['e_required_wh']:.0f} Wh</div></div>
    <div class="metric"><div class="lbl">{l_det_capacity_used}</div>
      <div class="val">{det_result['capacity_used_wh']:.0f} Wh ({det_result['capacity_used_pct']:.0f}%)</div></div>
    <div class="metric"><div class="lbl">{l_det_assist_ratio}</div>
      <div class="val">{det_result['assist_ratio'] * 100:.0f}%</div></div>
    <div class="metric"><div class="lbl">{l_det_rider_kcal}</div>
      <div class="val">{det_result['rider_kcal']:.0f} kcal</div></div>
  </div>
  <div class="det-breakdown">
    <div>{det_breakdown_line1}</div>
    <div>{det_breakdown_line2}</div>
  </div>
  {det_warning_html}
</div>"""
        results_card_class = "card ai-card"
        results_header = s_results_ai
        ai_caption_html = f'<p class="ai-caption">{_html_mod.escape(ai_caption_text)}</p>'

    # ── Climbs table ──────────────────────────────────────────────────────────
    merged_climbs = merge_climbs_analysis(gpx_stats.get("climbs"), analysis.get("climbs_analysis"))
    hardest_idx = analysis.get("hardest_climb_index")
    hardest_reason = analysis.get("hardest_climb_reason") or ""

    climbs_section = ""
    if merged_climbs:
        headers = [l_c_start, l_c_length, l_c_gain, l_c_avg_grad, l_c_max_grad, l_c_class,
                   l_c_kcal, l_c_hr]
        if is_ebike:
            headers.append(l_c_battery)
        headers.append(l_c_fatigue)
        head_html = "".join(f"<th>{h}</th>" for h in headers)

        colspan = len(headers)
        any_low_confidence = any(c.get("max_gradient_low_confidence") for c in merged_climbs)
        rows_html = ""
        for c in merged_climbs:
            is_hardest = c["climb_index"] == hardest_idx
            row_style = ' style="background:#fff3d6"' if is_hardest else ""
            max_grad_cell = f"{c['max_gradient_percent']:.1f}%"
            if c.get("max_gradient_low_confidence"):
                max_grad_cell += " ⓘ"
            cells = [
                f"{c['start_km']:.1f} km",
                f"{c['length_m']:.0f} m",
                f"{c['elevation_gain_m']:.0f} m",
                f"{c['avg_gradient_percent']:.1f}%",
                max_grad_cell,
                f"{c['classification_emoji']} {c['classification']}",
                f"{c['kcal']} kcal" if c.get("kcal") is not None else "—",
                f"{c['avg_hr_bpm']} bpm" if c.get("avg_hr_bpm") is not None else "—",
            ]
            if is_ebike:
                cells.append(
                    f"{c['battery_pct_consumed']:.1f}%" if c.get("battery_pct_consumed") is not None else "—"
                )
            cells.append(
                f"{c['fatigue_contribution']}/10" if c.get("fatigue_contribution") is not None else "—"
            )
            cells_html = "".join(f"<td>{cell}</td>" for cell in cells)
            rows_html += f"<tr{row_style}>{cells_html}</tr>\n"
            if is_hardest:
                rows_html += (
                    f'<tr{row_style}><td colspan="{colspan}" style="font-size:.78rem;color:#8a6d3b">'
                    f'{l_c_hardest} — {_html_mod.escape(hardest_reason)}</td></tr>\n'
                )

        low_confidence_note_html = (
            f'<p style="font-size:.78rem;color:#888;margin-top:8px">{l_c_low_confidence_note}</p>'
            if any_low_confidence else ""
        )
        climbs_section = f"""
<div class="card">
  <h2>⛰️ {s_climbs}</h2>
  <div style="overflow-x:auto">
  <table class="climbs-table">
    <thead><tr>{head_html}</tr></thead>
    <tbody>
    {rows_html}
    </tbody>
  </table>
  </div>
  {low_confidence_note_html}
</div>"""
    else:
        climbs_section = f"""
<div class="card">
  <h2>⛰️ {s_climbs}</h2>
  <p style="color:#888;font-size:.85rem">{l_c_no_climbs}</p>
</div>"""

    # ── Full HTML ─────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="{'en' if is_en else 'it'}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html_mod.escape(title)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#f4f6fb;color:#222;line-height:1.5}}
.wrap{{max-width:720px;margin:0 auto;padding:28px 16px}}
.hdr{{background:linear-gradient(135deg,#1a1a2e,#0f3460);color:#fff;border-radius:12px;padding:28px 32px;margin-bottom:20px}}
.hdr h1{{font-size:1.7rem;font-weight:700;margin-bottom:4px}}
.hdr .sub{{font-size:.88rem;opacity:.7;margin-bottom:10px}}
.hdr .meta{{font-size:.78rem;opacity:.55}}
.card{{background:#fff;border-radius:10px;padding:20px 24px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
.card h2{{font-size:1rem;font-weight:600;color:#444;margin-bottom:14px;border-bottom:2px solid #e8eef6;padding-bottom:8px}}
.metric-row{{display:flex;flex-wrap:wrap;gap:12px;margin-top:4px}}
.metric{{flex:1;min-width:120px;background:#f8fafc;border-radius:8px;padding:12px 16px}}
.lbl{{font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:#777;margin-bottom:3px}}
.val{{font-size:1.3rem;font-weight:700;color:#1a1a2e}}
.fatigue-badge{{display:inline-block;background:{fatigue_color};color:#fff;border-radius:6px;padding:5px 13px;font-size:1.3rem;font-weight:700}}
.route-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.route-cell{{background:#f8fafc;border-radius:8px;padding:10px 14px}}
.route-cell .lbl{{font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;color:#777;margin-bottom:2px}}
.route-cell .val{{font-size:1rem;font-weight:700;color:#1a1a2e}}
.route-title{{font-size:2rem;font-weight:800;color:#1a1a2e;margin-bottom:16px;line-height:1.2}}
.route-name{{font-size:1.05rem;font-weight:600;color:#0f3460;margin-bottom:12px}}
.location-card{{background:#eef4ff;border-left:4px solid #3a86ff}}
.location-card .loc-type{{font-size:1.05rem;font-weight:700;color:#0f3460;margin-bottom:8px}}
.location-card .loc-line{{font-size:.9rem;color:#333;margin-bottom:2px}}
.profile-cols{{display:grid;grid-template-columns:1fr 1fr;gap:0 32px}}
.profile-section-title{{font-size:.8rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#666;margin:12px 0 6px}}
.prow{{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #f0f0f0}}
.plbl{{font-size:.82rem;color:#888}}
.pval{{font-size:.82rem;font-weight:600;color:#222;text-align:right;max-width:55%}}
.narrative-text{{font-size:.9rem;color:#333;line-height:1.8;white-space:pre-wrap}}
.advice ul{{padding-left:20px}}
.advice li{{margin-bottom:8px;color:#333}}
.disclaimer{{background:#fffbe6;border-left:4px solid #f0b429;border-radius:0 8px 8px 0;padding:14px 18px}}
.disclaimer h2{{color:#b7791f;border:none;margin-bottom:6px;font-size:.95rem}}
.disclaimer p{{font-size:.87rem;color:#555;line-height:1.6}}
.ai-card{{background:#fffbe6;border-left:4px solid #f0b429;border-radius:0 8px 8px 0}}
.ai-card h2{{color:#b7791f;border:none}}
.ai-caption{{font-size:.82rem;color:#7a5b13;margin-bottom:10px}}
.det-breakdown{{margin-top:10px;padding:10px 14px;background:#eef7ee;border-radius:8px;font-size:.82rem;color:#2c5f2d;line-height:1.7}}
.det-warning{{margin-top:8px;font-size:.82rem;color:#b7791f;font-weight:600}}
.climbs-table{{width:100%;border-collapse:collapse;font-size:.82rem}}
.climbs-table th{{text-align:left;padding:6px 10px;color:#777;font-size:.72rem;text-transform:uppercase;letter-spacing:.03em;border-bottom:2px solid #e8eef6}}
.climbs-table td{{padding:6px 10px;border-bottom:1px solid #f0f0f0}}
@media(max-width:520px){{
  .route-grid{{grid-template-columns:1fr 1fr}}
  .profile-cols{{grid-template-columns:1fr}}
  .metric-row{{flex-direction:column}}
}}
</style>
</head>
<body>
<div class="wrap">

<h1 class="route-title">{_html_mod.escape(gpx_stats.get("gpx_filename") or gpx_name)}</h1>

<div class="hdr">
  <h1>{title}</h1>
  <div class="sub">{subtitle}</div>
  <div class="meta">{l_generated}: {now} &nbsp;·&nbsp; {_html_mod.escape(profile.get("name",""))}</div>
</div>

{location_section}

<div class="card">
  <h2>📍 {s_route}</h2>
  <div class="route-name">{_html_mod.escape(gpx_name)}</div>
  <div class="route-grid">
    <div class="route-cell"><div class="lbl">{l_dist}</div><div class="val">{gpx_stats["distance_km"]} km</div></div>
    <div class="route-cell"><div class="lbl">{l_elev_up}</div><div class="val">{elev_up}</div></div>
    <div class="route-cell"><div class="lbl">{l_elev_down}</div><div class="val">{elev_down}</div></div>
    <div class="route-cell"><div class="lbl">{l_max_alt}</div><div class="val">{max_alt}</div></div>
  </div>
</div>

{map_section}

{narrative_section}

<div class="card">
  <h2>👤 {s_profile_full}</h2>
  <div class="profile-cols">
    <div>
      <div class="profile-section-title">🚲 {s_bike}</div>
      {bike_html}
    </div>
    <div>
      <div class="profile-section-title">🏃 {s_rider}</div>
      {driver_html}
    </div>
  </div>
</div>

{det_section}

<div class="{results_card_class}">
  <h2>{results_header}</h2>
  {ai_caption_html}
  {battery_html}
  <div class="metric-row">
    <div class="metric"><div class="lbl">{l_calories}</div><div class="val">{analysis.get("calories_kcal","—")} kcal</div></div>
    <div class="metric"><div class="lbl">{l_time}</div><div class="val">{time_str}</div></div>
    <div class="metric"><div class="lbl">{l_hr}</div><div class="val">{hr_str}</div></div>
    <div class="metric"><div class="lbl">{l_fatigue}</div><div class="val"><span class="fatigue-badge">{fatigue}/10</span></div></div>
  </div>
</div>

{climbs_section}

<div class="card advice">
  <h2>💡 {s_advice}</h2>
  <ul>{advice_items}</ul>
</div>

<div class="card disclaimer">
  <h2>{s_disclaimer}</h2>
  <p>{disclaimer_text}</p>
</div>

</div>
</body>
</html>"""
