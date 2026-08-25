"""
Visualizzazione grafica delle salite (profilo SVG + tabella) — porta lo stile
fornito dall'utente (visualizzazione_salita.html), self-contained (nessuna
libreria esterna).

SVG e tabella sono generati lato server (qui, in Python) — non via JS come
nella prima versione/nell'HTML di riferimento dell'utente. Motivo: il report
HTML scaricabile viene spesso aperto su telefono tramite l'anteprima rapida
di Mail/Files (iOS "Quick Look") o webview simili, che renderizzano l'HTML
statico ma NON eseguono JavaScript — con la versione solo-JS, in quei
contesti il grafico restava vuoto e la tabella mostrava solo l'intestazione
(bug segnalato: "sul telefono fa vedere solo i titoli della tabella"). Ora
grafico e tabella sono presenti e leggibili anche a JS disattivato; lo
script rimasto serve solo per l'interattività (click per selezionare una
salita, tooltip on-hover) come miglioramento progressivo, non come requisito.

Un'unica funzione (render_climb_chart_html) genera il blocco HTML/CSS/JS,
riusata identica sia per l'embed live in Streamlit (st.components.v1.html,
vedi main.py) sia nel report HTML scaricabile (ride_analysis_agent.
render_html_report) — nessuna duplicazione della logica di rendering tra le
due viste. Nessuna dipendenza da Streamlit/i18n qui: stringhe bilingui IT/EN
inline (stesso pattern già usato in ride_analysis_agent.render_html_report,
che genera anch'esso un report standalone), non il sistema t()/translations.yaml
(quello è per l'app Streamlit, questo modulo deve restare utilizzabile anche
da candidate_generator.py e da un contesto non-Streamlit).
"""
from __future__ import annotations

import html as _html
import json
import math
import uuid

# Geometria SVG condivisa tra il rendering statico (Python, sotto) e i
# commenti/calcoli che la richiamano — stessi valori dell'HTML di riferimento
# (viewBox 1000x330), non tarati per una singola route.
_SVG_W, _SVG_H, _SVG_L, _SVG_R, _SVG_T, _SVG_B = 1000, 330, 52, 20, 20, 42


def _svg_x(km: float, max_km: float) -> float:
    if max_km <= 0:
        return _SVG_L
    return _SVG_L + km / max_km * (_SVG_W - _SVG_L - _SVG_R)


def _svg_y(ele: float, max_ele: float) -> float:
    if max_ele <= 0:
        return _SVG_H - _SVG_B
    return _SVG_H - _SVG_B - ele / max_ele * (_SVG_H - _SVG_T - _SVG_B)


def _elevation_at(km: float, profile: list[list[float]]) -> float:
    """Elevazione del punto profilo più vicino a km — porta 1:1 elevationAt() dell'HTML di riferimento."""
    if not profile:
        return 0.0
    return min(profile, key=lambda p: abs(p[0] - km))[1]

# Livelli di difficoltà del grafico (bande colorate) — soglie proprie di
# QUESTA visualizzazione, su max_200m_pct, tarate per riprodurre esattamente
# i livelli dell'HTML di riferimento dell'utente (moderate/hard/extreme):
# indipendenti dalla classificazione dolce/moderata/impegnativa esistente
# altrove nell'app (quella è su avg_gradient_percent, con soglie diverse,
# usata per la tabella "Salite principali" — vedi gpx_analyzer._classify_climb).
_LEVEL_HARD_PCT = 10.0
_LEVEL_EXTREME_PCT = 15.0

_STRINGS = {
    "it": {
        "summary_route": "Percorso",
        "summary_route_note": "{n} salite rilevanti",
        "summary_danger": "Danger waypoint",
        "summary_danger_note": "almeno 200 m al {pct:g}%",
        "summary_longest": "Più lungo",
        "col_num": "#",
        "col_zone": "Località",
        "col_km": "Km",
        "col_length": "Lunghezza",
        "col_gain": "Dislivello",
        "col_avg": "Media",
        "col_hard": "Tratto più duro",
        "col_diff": "Difficoltà",
        "legend_moderate": "Moderata",
        "legend_hard": "Impegnativa",
        "legend_extreme": "Molto impegnativa",
        "legend_hardmark": "200 m più duri",
        "level_moderate": "Moderata",
        "level_hard": "Impegnativa",
        "level_extreme": "Molto impegnativa",
        "zone_unavailable": "Zona non disponibile",
        "per200": "per 200 m",
        "per500": "per 500 m",
        "no_climbs": "Nessuna salita rilevante rilevata su questo percorso.",
        "note_tpl": "{length} al {avg:.1f}% di media; il tratto più duro sono 200 m al {hard200:.1f}%.",
        "note_danger_suffix": " ⚠️ Sopra la soglia di allerta ({threshold:g}% su 200 m).",
        "min_at_10": "≈ {min} min a 10 km/h",
    },
    "en": {
        "summary_route": "Route",
        "summary_route_note": "{n} significant climbs",
        "summary_danger": "Danger waypoints",
        "summary_danger_note": "at least 200 m at {pct:g}%",
        "summary_longest": "Longest",
        "col_num": "#",
        "col_zone": "Location",
        "col_km": "Km",
        "col_length": "Length",
        "col_gain": "Elevation gain",
        "col_avg": "Average",
        "col_hard": "Hardest stretch",
        "col_diff": "Difficulty",
        "legend_moderate": "Moderate",
        "legend_hard": "Challenging",
        "legend_extreme": "Very challenging",
        "legend_hardmark": "Hardest 200 m",
        "level_moderate": "Moderate",
        "level_hard": "Challenging",
        "level_extreme": "Very challenging",
        "zone_unavailable": "Location unavailable",
        "per200": "over 200 m",
        "per500": "over 500 m",
        "no_climbs": "No significant climb detected on this route.",
        "note_tpl": "{length} at an average of {avg:.1f}%; the hardest stretch is 200 m at {hard200:.1f}%.",
        "note_danger_suffix": " ⚠️ Above the warning threshold ({threshold:g}% over 200 m).",
        "min_at_10": "≈ {min} min at 10 km/h",
    },
}


def _nice_step(value_range: float, target_ticks: int = 6) -> float:
    """Passo 'tondo' (1/2/5×10^n) per le griglie degli assi, dato un range e un
    numero di tick desiderato — non un valore hardcoded per una singola route
    (l'HTML di riferimento aveva step fissi tarati sulla sua unica route)."""
    if value_range <= 0:
        return 1.0
    raw = value_range / target_ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        step = m * magnitude
        if step >= raw:
            return step
    return 10 * magnitude


def _level_for(hard200: float) -> str:
    if hard200 >= _LEVEL_EXTREME_PCT:
        return "extreme"
    if hard200 >= _LEVEL_HARD_PCT:
        return "hard"
    return "moderate"


_ZONE_COUNTRY_TOKENS = {"italia", "italy"}


def _short_zone(full_address: str) -> str:
    """
    Etichetta breve per la colonna 'Località'/il nome della salita, a partire
    dall'indirizzo Nominatim completo (geocoding_agent.geocode_climbs) — quello
    resta intatto nel dato persistito (JSON della route), qui si accorcia solo
    per la visualizzazione: un indirizzo completo ("5, Via Palombara, Gaggiola,
    Montemarciano, Ancona, Marche, 60018, Italia") è illeggibile in una cella
    di tabella stretta. Scarta i componenti puramente numerici (civico, CAP) e
    il paese finale, tiene i primi due componenti restanti (tipicamente
    via/frazione + comune) — euristica semplice, non un parser di indirizzi:
    non riproduce esattamente le zone scritte a mano dell'HTML di riferimento
    dell'utente, ma resta leggibile per qualunque indirizzo Nominatim reale.
    """
    parts = [p.strip() for p in full_address.split(",") if p.strip()]
    parts = [p for p in parts if not p.replace(" ", "").isdigit()]
    if parts and parts[-1].lower() in _ZONE_COUNTRY_TOKENS:
        parts = parts[:-1]
    if not parts:
        return full_address
    return ", ".join(parts[:2])


def _decimate(distances_km: list[float], elevations_m: list[float], target_points: int = 300) -> list[list[float]]:
    """Sottocampiona il profilo (già a passo fisso 10m da detect_climbs) per il
    solo scopo grafico — un percorso di 200km a 10m di risoluzione produrrebbe
    >20.000 punti, inutili per un SVG largo 1000px e pesanti da incorporare
    come JSON inline nella pagina."""
    n = len(distances_km)
    if n == 0:
        return []
    stride = max(1, n // target_points)
    points = [[round(distances_km[i], 3), round(elevations_m[i], 1)] for i in range(0, n, stride)]
    last = [round(distances_km[-1], 3), round(elevations_m[-1], 1)]
    if points[-1] != last:
        points.append(last)
    return points


def render_climb_chart_html(
    climbs: list[dict],
    distance_km: float,
    profile_distances_km: list[float] | None = None,
    profile_elevations_m: list[float] | None = None,
    lang: str = "it",
    danger_threshold_pct: float = 13.0,
    title: str = "",
) -> str:
    """
    Genera il blocco HTML/CSS/JS (profilo altimetrico SVG + tabella salite),
    self-contained — nessuna libreria esterna, stesso stile di
    visualizzazione_salita.html (bande colorate moderate/hard/extreme, tick
    neri sul tratto dei 200 m più duri su OGNI salita, tabella con zona
    geocodificata e difficoltà).

    climbs: output di gpx_analyzer.detect_climbs()["climbs"] — richiede
    start_km, length_m, elevation_gain_m, avg_gradient_percent, max_200m_pct,
    max_500m_pct, hard_start_km, zone (quest'ultimo popolato da
    geocoding_agent.geocode_climbs PRIMA di chiamare questa funzione — qui non
    si fa reverse geocoding, solo rendering).

    profile_distances_km/profile_elevations_m: opzionali (da detect_climbs()),
    se assenti il grafico mostra solo le bande salita senza la linea di quota.

    Ritorna un frammento HTML (non un documento completo: niente <!doctype>/
    <html>/<body>) — pensato per essere incorporato sia in
    st.components.v1.html() sia dentro un report HTML più ampio già dotato
    della propria struttura di pagina.
    """
    s = _STRINGS.get(lang, _STRINGS["it"])
    element_id = f"climb-profile-{uuid.uuid4().hex[:10]}"

    if not climbs:
        return f'<div class="card" style="color:#73777b;font-size:.9rem">{s["no_climbs"]}</div>'

    profile = _decimate(profile_distances_km or [], profile_elevations_m or [])

    js_climbs = []
    for i, c in enumerate(climbs, start=1):
        start_km = c["start_km"]
        end_km = round(start_km + c["length_m"] / 1000.0, 3)
        hard200 = c["max_200m_pct"]
        hard500 = c.get("max_500m_pct")
        level = _level_for(hard200)
        danger = hard200 >= danger_threshold_pct
        full_zone = c.get("zone") or s["zone_unavailable"]
        zone = _short_zone(full_zone) if c.get("zone") else full_zone
        length_km = c["length_m"] / 1000.0
        length_str = f"{length_km:.2f} km"
        note = s["note_tpl"].format(length=length_str, avg=c["avg_gradient_percent"], hard200=hard200)
        if danger:
            note += s["note_danger_suffix"].format(threshold=danger_threshold_pct)

        js_climbs.append({
            "id": i,
            "start": round(start_km, 3),
            "end": end_km,
            "length": length_str,
            "gain": round(c["elevation_gain_m"]),
            "avg": round(c["avg_gradient_percent"], 1),
            "hard200": round(hard200, 1),
            "hard500": round(hard500, 1) if hard500 is not None else None,
            "hardStart": round(c["hard_start_km"], 3),
            "name": zone,
            "fullZone": full_zone,
            "label": s[f"level_{level}"],
            "level": level,
            "danger": danger,
            "note": note,
        })

    danger_count = sum(1 for c in js_climbs if c["danger"])
    longest = max(js_climbs, key=lambda c: c["end"] - c["start"])
    default_selected = max(js_climbs, key=lambda c: c["hard200"])["id"]

    max_ele = max((p[1] for p in profile), default=max((c["gain"] for c in js_climbs), default=100))
    ele_step = _nice_step(max_ele, target_ticks=5)
    km_step = _nice_step(distance_km, target_ticks=6)
    max_ele_padded = max_ele + ele_step * 0.3

    # ── Rendering statico SVG + tabella (Python, non JS — vedi nota in testa
    # al modulo: deve essere leggibile anche senza JavaScript). ──
    grid_svg: list[str] = []
    n_ele_ticks = int(max_ele_padded // ele_step) + 1 if ele_step > 0 else 0
    for i in range(n_ele_ticks + 1):
        e = i * ele_step
        if e > max_ele_padded:
            break
        yy = _svg_y(e, max_ele_padded)
        grid_svg.append(f'<line x1="{_SVG_L}" y1="{yy:.1f}" x2="{_SVG_W - _SVG_R}" y2="{yy:.1f}" class="grid-line"></line>')
        grid_svg.append(f'<text x="{_SVG_L - 8}" y="{yy + 4:.1f}" class="axis-label" text-anchor="end">{round(e)} m</text>')
    n_km_ticks = int(distance_km // km_step) + 1 if km_step > 0 else 0
    for i in range(n_km_ticks + 1):
        km = i * km_step
        if km > distance_km:
            break
        xx = _svg_x(km, distance_km)
        grid_svg.append(f'<text x="{xx:.1f}" y="{_SVG_H - 13}" class="axis-label" text-anchor="middle">{round(km)} km</text>')

    profile_svg = ""
    if profile:
        line_path = " ".join(
            f"{'L' if i else 'M'}{_svg_x(p[0], distance_km):.1f},{_svg_y(p[1], max_ele_padded):.1f}"
            for i, p in enumerate(profile)
        )
        area_path = (
            f"{line_path} L{_svg_x(profile[-1][0], distance_km):.1f},{_SVG_H - _SVG_B} "
            f"L{_svg_x(0, distance_km):.1f},{_SVG_H - _SVG_B} Z"
        )
        profile_svg = f'<path d="{area_path}" class="profile-area"></path><path d="{line_path}" class="profile-line"></path>'

    band_svg: list[str] = []
    hardest_svg: list[str] = []
    marker_svg: list[str] = []
    rows_html: list[str] = []
    for c in js_climbs:
        aria = _html.escape(f'{c["id"]}. {c["name"]}')
        is_default = c["id"] == default_selected
        selected_cls = " selected" if is_default else ""

        x_start = _svg_x(c["start"], distance_km)
        x_end = _svg_x(c["end"], distance_km)
        width = max(4.0, x_end - x_start)
        band_svg.append(
            f'<rect x="{x_start:.1f}" y="{_SVG_T}" width="{width:.1f}" height="{_SVG_H - _SVG_T - _SVG_B}" '
            f'class="climb-band {c["level"]}{selected_cls}" data-id="{c["id"]}" aria-label="{aria}"></rect>'
        )

        hard_end = min(c["hardStart"] + 0.2, c["end"])
        y1 = _svg_y(_elevation_at(c["hardStart"], profile), max_ele_padded) - 7
        y2 = _svg_y(_elevation_at(hard_end, profile), max_ele_padded) - 7
        hardest_svg.append(
            f'<line x1="{_svg_x(c["hardStart"], distance_km):.1f}" x2="{_svg_x(hard_end, distance_km):.1f}" '
            f'y1="{y1:.1f}" y2="{y2:.1f}" class="hardest-line"></line>'
        )

        mid = (c["start"] + c["end"]) / 2
        mx = _svg_x(mid, distance_km)
        my = max(_SVG_T + 14, _svg_y(_elevation_at(mid, profile), max_ele_padded) - 18)
        marker_svg.append(
            f'<g class="climb-marker" data-id="{c["id"]}" role="button" aria-label="{aria}">'
            f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="11"></circle>'
            f'<text x="{mx:.1f}" y="{my + 1:.1f}">{c["id"]}</text></g>'
        )

        hard_cell = f'{c["hard200"]}% {s["per200"]}'
        if c["hard500"] is not None:
            hard_cell += f' · {c["hard500"]}% {s["per500"]}'
        rows_html.append(
            f'<tr data-id="{c["id"]}" aria-selected="{"true" if is_default else "false"}">'
            f'<td>{c["id"]}</td>'
            f'<td title="{_html.escape(c["fullZone"])}">{_html.escape(c["name"])}</td>'
            f'<td class="text-end text-nowrap">{c["start"]:.2f}</td>'
            f'<td class="text-end text-nowrap">{c["length"]}</td>'
            f'<td class="text-end">+{c["gain"]} m</td>'
            f'<td class="text-end">{c["avg"]}%</td>'
            f'<td>{hard_cell}</td>'
            f'<td><span class="difficulty {c["level"]}">{_html.escape(c["label"])}</span></td>'
            f'</tr>'
        )

    svg_content = "".join(grid_svg) + "".join(band_svg) + profile_svg + "".join(hardest_svg) + "".join(marker_svg)
    tbody_html = "".join(rows_html)

    def _detail_html(c: dict) -> str:
        duration = round((c["end"] - c["start"]) * 6)
        hard_extra = f' · {c["hard500"]}% {s["per500"]}' if c["hard500"] is not None else ""
        critical_cls = "critical" if c["level"] == "extreme" else ""
        min_txt = s["min_at_10"].format(min=duration)
        return (
            f'<strong>{c["id"]}. {_html.escape(c["name"])}</strong>'
            f'<span>km {c["start"]:.2f}–{c["end"]:.2f} · {c["length"]} · +{c["gain"]} m</span>'
            f'<span>{c["avg"]}%</span>'
            f'<span class="{critical_cls}"><strong>{c["hard200"]}% {s["per200"]}</strong>{hard_extra}</span>'
            f'<span class="text-muted">{min_txt} · {_html.escape(c["note"])}</span>'
        )

    default_climb = next(c for c in js_climbs if c["id"] == default_selected)
    detail_html = _detail_html(default_climb)

    # Placeholder JS (${duration}) sostituito qui in Python — non tramite
    # str.format() dentro l'espressione f-string, dove il raddoppio {{ }}
    # dell'f-string non si applica (si applica solo al testo letterale del
    # corpo dell'f-string, non a un'espressione annidata al suo interno).
    min_at_10_js = s["min_at_10"].replace("{min}", "${duration}")

    payload_climbs = json.dumps(js_climbs, separators=(",", ":"), ensure_ascii=False)
    aria_title = title or s["summary_route"]

    return f'''<div id="{element_id}">
  <div class="viz-grid climb-summary" aria-label="{s["summary_route"]}">
    <div class="card viz-stat"><span class="text-muted">{s["summary_route"]}</span><strong class="viz-stat-value">{distance_km:.2f} km</strong><span class="text-small text-muted">{s["summary_route_note"].format(n=len(js_climbs))}</span></div>
    <div class="card viz-stat"><span class="text-muted">{s["summary_danger"]}</span><strong class="viz-stat-value">{danger_count}</strong><span class="text-small text-muted">{s["summary_danger_note"].format(pct=danger_threshold_pct)}</span></div>
    <div class="card viz-stat"><span class="text-muted">{s["summary_longest"]}</span><strong class="viz-stat-value">{longest["length"]}</strong><span class="text-small text-muted">{longest["name"]}</span></div>
  </div>

  <div class="profile-wrap">
    <svg id="elevation-chart" viewBox="0 0 1000 330" role="img" aria-labelledby="elevation-title elevation-desc">
      <title id="elevation-title">{aria_title}</title>
      <desc id="elevation-desc">{s["summary_route_note"].format(n=len(js_climbs))}</desc>
      {svg_content}
    </svg>
    <div id="chart-tooltip" class="tooltip climb-tooltip" hidden></div>
  </div>

  <div class="viz-row climb-legend text-small">
    <span><i class="legend-swatch moderate"></i>{s["legend_moderate"]}</span>
    <span><i class="legend-swatch hard"></i>{s["legend_hard"]}</span>
    <span><i class="legend-swatch extreme"></i>{s["legend_extreme"]}</span>
    <span><i class="hard-mark"></i>{s["legend_hardmark"]}</span>
  </div>

  <div id="selected-climb" class="card selected-climb" aria-live="polite">{detail_html}</div>

  <div class="table-responsive">
    <table class="table table-sm climb-table">
      <thead><tr><th>{s["col_num"]}</th><th>{s["col_zone"]}</th><th class="text-end">{s["col_km"]}</th><th class="text-end">{s["col_length"]}</th><th class="text-end">{s["col_gain"]}</th><th class="text-end">{s["col_avg"]}</th><th>{s["col_hard"]}</th><th>{s["col_diff"]}</th></tr></thead>
      <tbody id="climb-table-body">{tbody_html}</tbody>
    </table>
  </div>
</div>

<style>
:root {{ --background:#ffffff; --foreground:#202124; --card:#f5f5f5; --card-foreground:#202124; --muted:#f1f3f4; --muted-foreground:#73777b; --accent:#e8f0fe; --accent-foreground:#202124; --destructive:#e5482d; --border:#dfe3e7; --viz-series-1:#3094ee; --viz-series-3:#5fc98a; --viz-series-5:#8b75df; }}
#{element_id} {{ color: var(--foreground); width: 100%; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background: var(--background); }}
#{element_id} * {{ box-sizing: border-box; }}
#{element_id} .viz-grid {{ display:grid; gap:12px; }}
#{element_id} .viz-row {{ display:flex; align-items:center; flex-wrap:wrap; }}
#{element_id} .card {{ background:var(--card); color:var(--card-foreground); border-radius:14px; padding:14px; }}
#{element_id} .viz-stat-value {{ display:block; font-size:24px; font-weight:500; }}
#{element_id} .text-muted {{ color:var(--muted-foreground); }}
#{element_id} .text-small {{ font-size:13px; }}
#{element_id} .table-responsive {{ overflow-x:auto; }}
#{element_id} .table {{ width:100%; border-collapse:collapse; }}
#{element_id} .table th, #{element_id} .table td {{ padding:10px; border-bottom:1px solid var(--border); text-align:left; }}
#{element_id} .text-end {{ text-align:right!important; }}
#{element_id} .text-nowrap {{ white-space:nowrap; }}
#{element_id} .tooltip {{ background:var(--foreground); color:var(--background); padding:8px; border-radius:6px; font-size:13px; }}
#{element_id} .climb-summary {{ margin-bottom: 16px; grid-template-columns: repeat(3,minmax(0,1fr)); }}
#{element_id} .profile-wrap {{ position: relative; width: 100%; }}
#{element_id} #elevation-chart {{ display: block; width: 100%; height: auto; overflow: visible; }}
#{element_id} .grid-line {{ stroke: var(--border); stroke-width: 1; }}
#{element_id} .axis-label {{ fill: var(--muted-foreground); font-size: 13px; }}
#{element_id} .profile-area {{ fill: color-mix(in srgb, var(--viz-series-1) 18%, transparent); }}
#{element_id} .profile-line {{ fill: none; stroke: var(--viz-series-1); stroke-width: 2.5; vector-effect: non-scaling-stroke; }}
#{element_id} .climb-band {{ opacity: .15; cursor: pointer; }}
#{element_id} .climb-band.selected {{ opacity: .28; }}
#{element_id} .climb-band.moderate {{ fill: var(--viz-series-3); }}
#{element_id} .climb-band.hard {{ fill: var(--viz-series-5); }}
#{element_id} .climb-band.extreme {{ fill: var(--destructive); }}
#{element_id} .hardest-line {{ stroke: var(--foreground); stroke-width: 5; stroke-linecap: round; pointer-events: none; }}
#{element_id} .climb-marker {{ cursor: pointer; }}
#{element_id} .climb-marker circle {{ fill: var(--card); stroke: var(--foreground); stroke-width: 2; }}
#{element_id} .climb-marker text {{ fill: var(--card-foreground); font-size: 13px; font-weight: 500; text-anchor: middle; dominant-baseline: central; }}
#{element_id} .climb-legend {{ gap: 18px; margin: 4px 0 12px; color: var(--muted-foreground); }}
#{element_id} .climb-legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
#{element_id} .legend-swatch {{ width: 12px; height: 12px; display: inline-block; opacity: .45; }}
#{element_id} .legend-swatch.moderate {{ background: var(--viz-series-3); }}
#{element_id} .legend-swatch.hard {{ background: var(--viz-series-5); }}
#{element_id} .legend-swatch.extreme {{ background: var(--destructive); }}
#{element_id} .hard-mark {{ width: 18px; border-top: 4px solid var(--foreground); display: inline-block; }}
#{element_id} .selected-climb {{ margin-bottom: 12px; display: flex; gap: 18px; align-items: baseline; flex-wrap: wrap; }}
#{element_id} .selected-climb strong {{ font-weight: 500; }}
#{element_id} .selected-climb .critical {{ color: var(--destructive); }}
#{element_id} .climb-table tbody tr {{ cursor: pointer; }}
#{element_id} .climb-table tbody tr[aria-selected="true"] {{ background: var(--accent); color: var(--accent-foreground); }}
#{element_id} .difficulty {{ white-space: nowrap; }}
#{element_id} .difficulty::before {{ content: ""; display: inline-block; width: 9px; height: 9px; margin-right: 6px; }}
#{element_id} .difficulty.moderate::before {{ background: var(--viz-series-3); }}
#{element_id} .difficulty.hard::before {{ background: var(--viz-series-5); }}
#{element_id} .difficulty.extreme::before {{ background: var(--destructive); }}
#{element_id} .climb-tooltip {{ position: absolute; pointer-events: none; max-width: 240px; z-index: 3; }}
@media (max-width: 560px) {{
  #{element_id} .climb-summary {{ grid-template-columns: 1fr; }}
  #{element_id} .selected-climb {{ display: block; }}
  #{element_id} .selected-climb span {{ display: block; margin-top: 4px; }}
}}
</style>

<script>
(() => {{
  // Interattività (click per selezionare, tooltip on-hover) SOLO come
  // miglioramento progressivo — grafico e tabella sono già completi e
  // leggibili nell'HTML statico sopra, senza bisogno di questo script
  // (vedi nota in testa al modulo: JS può non essere eseguito, es. anteprima
  // rapida Mail/Files su iOS).
  const root = document.getElementById('{element_id}');
  const tooltip = root.querySelector('#chart-tooltip');
  const detail = root.querySelector('#selected-climb');
  const tbody = root.querySelector('#climb-table-body');
  const climbs = {payload_climbs};
  const byId = new Map(climbs.map(c => [c.id, c]));

  root.querySelectorAll('.climb-band, .climb-marker').forEach(node => {{
    const c = byId.get(Number(node.dataset.id));
    if (!c) return;
    node.addEventListener('click', () => selectClimb(c.id));
    node.addEventListener('mousemove', ev => showTooltip(ev, c));
    node.addEventListener('mouseleave', hideTooltip);
  }});
  tbody.querySelectorAll('tr').forEach(row => {{
    row.addEventListener('click', () => selectClimb(Number(row.dataset.id)));
  }});

  function selectClimb(id) {{
    const c = byId.get(id);
    if (!c) return;
    root.querySelectorAll('.climb-band').forEach(n => n.classList.toggle('selected', Number(n.dataset.id) === id));
    tbody.querySelectorAll('tr').forEach(n => n.setAttribute('aria-selected', String(Number(n.dataset.id) === id)));
    const duration = Math.round((c.end - c.start) * 6);
    detail.innerHTML = `<strong>${{c.id}}. ${{c.name}}</strong><span>km ${{c.start.toFixed(2)}}–${{c.end.toFixed(2)}} · ${{c.length}} · +${{c.gain}} m</span><span>${{c.avg}}%</span><span class="${{c.level==='extreme'?'critical':''}}"><strong>${{c.hard200}}% {s["per200"]}</strong>${{c.hard500?` · ${{c.hard500}}% {s["per500"]}`:''}}</span><span class="text-muted">{min_at_10_js} · ${{c.note}}</span>`;
  }}
  function showTooltip(ev, c) {{
    tooltip.hidden = false; tooltip.innerHTML = `<strong>${{c.id}}. ${{c.name}}</strong><br>${{c.length}} · +${{c.gain}} m · ${{c.avg}}%<br><strong>${{c.hard200}}% {s["per200"]}</strong>`;
    const box = root.querySelector('.profile-wrap').getBoundingClientRect(); const tip = tooltip.getBoundingClientRect();
    tooltip.style.left = `${{Math.max(4, Math.min(ev.clientX - box.left - tip.width / 2, box.width - tip.width - 4))}}px`;
    tooltip.style.top = `${{Math.max(4, ev.clientY - box.top - tip.height - 12)}}px`;
  }}
  function hideTooltip() {{ tooltip.hidden = true; }}
}})();
</script>
'''
