"""
ride_analysis_agent.py — Analisi ciclistica personalizzata.

Flusso:
  1. analyze_gpx_bytes()  — estrae stats dal file GPX caricato
  2. run_analysis()        — chiama AI con stats + profilo, ritorna dict strutturato
  3. render_html_report()  — genera HTML scaricabile (con disclaimer in evidenza)
"""
from __future__ import annotations

import io
import json
from datetime import datetime

import gpxpy

import ai_client


def analyze_gpx_bytes(file_bytes: bytes) -> dict:
    """Parse GPX bytes and return distance/elevation stats."""
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
    uphill, downhill = gpx.get_uphill_downhill()
    elevations = [pt.elevation for pt in points if pt.elevation is not None]

    return {
        "distance_km": round(distance_m / 1000, 2),
        "elevation_gain_m": round(uphill, 0),
        "elevation_loss_m": round(downhill, 0),
        "max_elevation_m": round(max(elevations), 0) if elevations else None,
        "min_elevation_m": round(min(elevations), 0) if elevations else None,
    }


def run_analysis(gpx_stats: dict, profile: dict, lang: str) -> dict:
    """
    Call the AI with GPX stats + rider profile.
    Returns a dict with keys:
      battery_pct_consumed, range_remaining_km (null if not ebike),
      calories_kcal, time_estimate_min, avg_hr_bpm (null if no fcmax),
      fatigue_index (1-10), advice (list[str]), disclaimer (str)
    """
    is_ebike = (profile.get("bike_type") or "").lower() == "ebike"
    lang_instr = "Rispondi in italiano." if lang == "it" else "Reply in English."

    system = f"""Sei un esperto di ciclismo, biomeccanica e fisiologia dello sport.
Analizza il giro ciclabile descritto e fornisci stime personalizzate realistiche.
{lang_instr}
Rispondi ESCLUSIVAMENTE con JSON valido, senza testo aggiuntivo, senza markdown, senza code fence.
Schema JSON da rispettare ESATTAMENTE (non aggiungere né rimuovere chiavi):
{{
  "battery_pct_consumed": <float 0-100 oppure null se non ebike>,
  "range_remaining_km": <float oppure null se non ebike>,
  "calories_kcal": <integer>,
  "time_estimate_min": <integer>,
  "avg_hr_bpm": <integer oppure null se FC max non disponibile>,
  "fatigue_index": <integer 1-10>,
  "advice": [<string>, <string>, <string>],
  "disclaimer": "<testo disclaimer medico nella lingua richiesta>"
}}
Il disclaimer deve essere: "Questa analisi è puramente indicativa e non costituisce diagnosi medica. Consulta un medico per valutazioni sulla tua salute." in italiano oppure "This analysis is indicative only and does not constitute medical advice. Consult a doctor for health assessments." in inglese."""

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
        if profile.get("assistance_level"):
            lines.append(f"- Livello assistenza: {profile['assistance_level']}/5")
        if profile.get("battery_pct") is not None:
            lines.append(f"- Stato batteria iniziale: {profile['battery_pct']}%")
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

    if not is_ebike:
        lines += ["", "Nota: NON è una ebike → battery_pct_consumed e range_remaining_km DEVONO essere null."]
    if not profile.get("driver_fcmax"):
        lines += ["", "Nota: FC max non disponibile → avg_hr_bpm DEVE essere null."]

    prompt = "\n".join(lines)
    raw = ai_client.generate_json(system, prompt, max_tokens=1200)
    return json.loads(raw)


def render_html_report(
    analysis: dict, gpx_stats: dict, profile: dict, lang: str
) -> str:
    """Generate a downloadable HTML report with styling and prominent disclaimer."""
    is_en = lang == "en"
    is_ebike = (profile.get("bike_type") or "").lower() == "ebike"
    now = datetime.now().strftime("%d/%m/%Y %H:%M")

    # ── Localised labels ──────────────────────────────────────────────────────
    if is_en:
        title = "🔋 Ride Analysis Report"
        subtitle = "GPX Route Builder — Personalised cycling analysis"
        s_ride = "Ride Data"
        s_profile = "Profile"
        s_results = "Analysis Results"
        s_advice = "Personalised Advice"
        s_disclaimer = "⚠️ Medical Disclaimer"
        l_dist = "Distance"
        l_elev = "Elevation gain"
        l_bike = "Bike"
        l_rider = "Rider"
        l_battery = "Battery consumed (est.)"
        l_range = "Remaining range (est.)"
        l_calories = "Calories burned"
        l_time = "Estimated time"
        l_hr = "Avg heart rate (est.)"
        l_fatigue = "Fatigue index"
        l_generated = "Generated on"
    else:
        title = "🔋 Report Analisi Giro"
        subtitle = "GPX Route Builder — Analisi ciclistica personalizzata"
        s_ride = "Dati Giro"
        s_profile = "Profilo"
        s_results = "Risultati Analisi"
        s_advice = "Consigli Personalizzati"
        s_disclaimer = "⚠️ Disclaimer Medico"
        l_dist = "Distanza"
        l_elev = "Dislivello +"
        l_bike = "Bici"
        l_rider = "Ciclista"
        l_battery = "Batteria consumata (stima)"
        l_range = "Autonomia residua (stima)"
        l_calories = "Calorie consumate"
        l_time = "Tempo stimato"
        l_hr = "FC media stimata"
        l_fatigue = "Indice di fatica"
        l_generated = "Generato il"

    # ── Format values ─────────────────────────────────────────────────────────
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
    batt_str = f"{batt:.0f}%" if batt is not None and is_ebike else ("—" if is_ebike else "")
    rng_str = f"{rng:.0f} km" if rng is not None and is_ebike else ("—" if is_ebike else "")

    # ── Profile summary ───────────────────────────────────────────────────────
    bike_model = profile.get("bike_model", "")
    bike_type = profile.get("bike_type", "—")
    bike_str = f"{bike_model} ({bike_type})" if bike_model else bike_type

    w = profile.get("driver_weight_kg", "")
    a = profile.get("driver_age", "")
    f_ = profile.get("driver_fitness", "")
    age_lbl = "yo" if is_en else "anni"
    fit_lbl = "fitness" if is_en else "forma"
    rider_parts = [f"{w} kg"] if w else []
    if a:
        rider_parts.append(f"{a} {age_lbl}")
    if f_:
        rider_parts.append(f"{fit_lbl} {f_}/5")
    rider_str = " · ".join(rider_parts) or "—"

    # ── Battery block (ebike only) ────────────────────────────────────────────
    battery_html = ""
    if is_ebike:
        battery_html = f"""
      <div class="metric-row">
        <div class="metric">
          <div class="lbl">{l_battery}</div>
          <div class="val">{batt_str}</div>
        </div>
        <div class="metric">
          <div class="lbl">{l_range}</div>
          <div class="val">{rng_str}</div>
        </div>
      </div>"""

    # ── Advice list ───────────────────────────────────────────────────────────
    advice_items = "".join(
        f"<li>{a}</li>" for a in (analysis.get("advice") or [])
    )

    disclaimer_text = analysis.get("disclaimer", "")

    # ── HTML ─────────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="{'en' if is_en else 'it'}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
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
.pgrid{{display:grid;grid-template-columns:1fr 1fr;gap:8px 24px}}
.prow .lbl{{font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:#888}}
.prow .val{{font-size:.95rem;font-weight:500;color:#222}}
.advice ul{{padding-left:20px}}
.advice li{{margin-bottom:8px;color:#333}}
.disclaimer{{background:#fffbe6;border-left:4px solid #f0b429;border-radius:0 8px 8px 0;padding:14px 18px}}
.disclaimer h2{{color:#b7791f;border:none;margin-bottom:6px;font-size:.95rem}}
.disclaimer p{{font-size:.87rem;color:#555;line-height:1.6}}
@media(max-width:480px){{.pgrid{{grid-template-columns:1fr}}.metric-row{{flex-direction:column}}}}
</style>
</head>
<body>
<div class="wrap">

<div class="hdr">
  <h1>{title}</h1>
  <div class="sub">{subtitle}</div>
  <div class="meta">{l_generated}: {now} &nbsp;·&nbsp; {profile.get("name","")}</div>
</div>

<div class="card">
  <h2>🗺️ {s_ride}</h2>
  <div class="metric-row">
    <div class="metric"><div class="lbl">{l_dist}</div><div class="val">{gpx_stats["distance_km"]} km</div></div>
    <div class="metric"><div class="lbl">{l_elev}</div><div class="val">{gpx_stats["elevation_gain_m"]:.0f} m</div></div>
  </div>
</div>

<div class="card">
  <h2>👤 {s_profile}</h2>
  <div class="pgrid">
    <div class="prow"><div class="lbl">{l_bike}</div><div class="val">{bike_str}</div></div>
    <div class="prow"><div class="lbl">{l_rider}</div><div class="val">{rider_str}</div></div>
  </div>
</div>

<div class="card">
  <h2>📊 {s_results}</h2>
  {battery_html}
  <div class="metric-row">
    <div class="metric"><div class="lbl">{l_calories}</div><div class="val">{analysis.get("calories_kcal","—")} kcal</div></div>
    <div class="metric"><div class="lbl">{l_time}</div><div class="val">{time_str}</div></div>
    <div class="metric"><div class="lbl">{l_hr}</div><div class="val">{hr_str}</div></div>
    <div class="metric"><div class="lbl">{l_fatigue}</div><div class="val"><span class="fatigue-badge">{fatigue}/10</span></div></div>
  </div>
</div>

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
