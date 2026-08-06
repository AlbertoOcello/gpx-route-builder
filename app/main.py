"""
GPX Route Builder — UI Streamlit
Tabs: 📐 Planner · 🏗️ Builder · 🧭 GPX Optimizer · 🔋 Analisi Giro · 🛠️ Utility · 🏁 Post Ride
"""
import datetime
import json
import logging
import os
import re
import shutil
import tempfile
from collections import deque
from pathlib import Path

# Configurazione logging applicativa — deve avvenire prima che qualunque modulo
# sottostante (brouter_client, candidate_generator, planner_agent, ecc.) emetta
# log, altrimenti restano invisibili (nessun handler = solo WARNING+ silenzioso
# su stderr). basicConfig() è no-op se già chiamato altrove — sicuro anche nei
# rerun ripetuti di Streamlit.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

import folium
import gpxpy
import streamlit as st
from geopy.distance import geodesic
from streamlit_folium import st_folium

from brouter_client import ensure_tile, get_route
from candidate_generator import generate_candidates
from decision_agent import run_decision
from geocoding_agent import geocode_candidate, geocode_search_raw, reverse_geocode_address
from gpx_analyzer import OUT_AND_BACK_WARN_THRESHOLD_PCT, analyze_gpx
from gpx_optimizer import is_already_optimized, optimize_gpx
from i18n import t, render_language_selector, active_lang
import ride_analysis_agent as ride_analysis
from learning_agent import update_user_memory_from_feedback
from models import RouteRequest, StartPoint, UserWaypointInput
from planner_agent import (
    build_prompt,
    build_raw_route_prompt,
    generate_raw_route,
    generate_strategies,
    _geocode_user_waypoints,
    _COORD_RE,
)
from scoring_engine import score_candidate
from user_memory import load_user_memory, merge_memory_with_request
import db

def _app_version() -> str:
    try:
        return (Path(__file__).parent.parent / "VERSION").read_text().strip()
    except OSError:
        return "dev"


def _build_commit() -> str:
    # "Baked" nell'immagine Docker al momento della build (Dockerfile ARG
    # GIT_COMMIT) — in sviluppo locale senza build-arg resta "unknown" a
    # livello di ENV, mostrato come "dev" invece di rompere.
    commit = os.environ.get("GIT_COMMIT", "unknown")
    return "dev" if commit == "unknown" else commit


class _TalkingLog:
    """
    Log a scorrimento stile terminale dentro un container st.status(): mostra
    solo le ultime `maxlen` righe (deque), l'ultima sempre in fondo con 🔄
    (passo in corso), le precedenti con ✅ (completate) — non un log che
    cresce all'infinito. Riscrive l'intero blocco in un st.empty() ad ogni
    step invece di fare append incrementale nativo.
    """

    def __init__(self, placeholder, maxlen: int = 5):
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._placeholder = placeholder

    def step(self, message: str) -> None:
        if self._lines and self._lines[-1].startswith("🔄"):
            self._lines[-1] = "✅" + self._lines[-1][1:]
        self._lines.append(f"🔄 {message}")
        self._render()

    def finish(self, message: str | None = None) -> None:
        if self._lines and self._lines[-1].startswith("🔄"):
            self._lines[-1] = "✅" + self._lines[-1][1:]
        if message:
            self._lines.append(f"✅ {message}")
        self._render()

    def _render(self) -> None:
        self._placeholder.markdown("  \n".join(self._lines) or "…")


st.set_page_config(page_title=t("app.page_title"), layout="wide")
st.title(t("app.title"))
st.caption(f"v{_app_version()} · build {_build_commit()}")
render_language_selector()

tab_file, tab_planner, tab_builder, tab_optimizer, tab_ride, tab_post_ride, tab_utility = st.tabs([
    t("tabs.file"),
    t("tabs.planner"),
    t("tabs.builder"),
    t("tabs.optimizer"),
    t("tabs.ride"),
    t("tabs.post_ride"),
    t("tabs.utility"),
])

_PLANNED_DIR = Path("routes/planned")
_PLANNED_DIR.mkdir(parents=True, exist_ok=True)

# Report HTML di Ride Analysis per candidati collegati a una route pianificata
# (redesign persistenza) — path referenziato da ride_analysis_history nel JSON
# della route, stesso pattern dei GPX generati dal Builder. I GPX esterni non
# collegati restano volatili (solo session_state + download), come deciso.
_RIDE_REPORTS_DIR = Path("routes/ride_reports")

# Soglia (bassa, informativa) per mostrare quale waypoint ha causato uno spuntone
# andata/ritorno — più bassa di OUT_AND_BACK_WARN_THRESHOLD_PCT (20%, warning
# vero e proprio): qui vogliamo informare anche su casi minori, tono neutro.
_OUT_AND_BACK_INFO_THRESHOLD_PCT = 5.0

# Preferenza dislivello — 3 livelli qualitativi condivisi tra Planner e Builder
# (sostituisce il vecchio input numerico max_elevation_gain_m, vedi scoring_engine.py).
_ELEV_PREF_CODES = ["none", "prefer_avoid", "avoid_max"]


def _elev_pref_selectbox(default_code: str, key: str) -> str:
    """Selectbox condivisa Planner/Builder per la preferenza dislivello. Ritorna il codice scelto."""
    labels = [t(f"planner.form.elevation_pref_{c}") for c in _ELEV_PREF_CODES]
    default_idx = _ELEV_PREF_CODES.index(default_code) if default_code in _ELEV_PREF_CODES else 0
    chosen_label = st.selectbox(t("planner.form.elevation_pref"), labels, index=default_idx, key=key)
    return _ELEV_PREF_CODES[labels.index(chosen_label)]


# ─── Helpers condivisi ────────────────────────────────────────────────────────

def _parse_csv(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


def _wp_is_forced(wp: dict) -> bool:
    """
    Vero se il waypoint compare come via-point letterale nella chiamata BRouter
    (stessa regola di candidate_generator._waypoints_to_lonlat): start/end sempre,
    via "planner" sempre, via "user" solo se mandatory=True.
    """
    if wp.get("role") in ("start", "end"):
        return True
    return wp.get("source") == "planner" or bool(wp.get("mandatory"))


def _generate_route_slug(narrative: str) -> str:
    """Ask the configured AI for a short kebab-case slug based on route_narrative."""
    try:
        from ai_client import generate as _ai_generate
        raw = _ai_generate(
            system=(
                "Sei un assistente che genera slug brevi in italiano per nomi di route ciclabili. "
                "Rispondi SOLO con lo slug: 2-4 parole in minuscolo separate da underscore, "
                "senza spazi, senza punteggiatura, senza accenti. "
                "Esempio: anello_corinaldo_naturalistico"
            ),
            prompt=narrative[:500] if narrative else "route generica",
            max_tokens=30,
        )
        slug = raw.strip().lower()
        slug = re.sub(r"[^\w]", "_", slug)
        slug = re.sub(r"_+", "_", slug).strip("_")
        return slug or "nuova_route"
    except Exception:
        return "nuova_route"


def _gpx_coords(gpx_path: str) -> list[tuple[float, float]]:
    with open(gpx_path) as f:
        gpx = gpxpy.parse(f)
    return [
        (pt.latitude, pt.longitude)
        for track in gpx.tracks
        for seg in track.segments
        for pt in seg.points
    ]


def _gpx_bytes_with_name(gpx_path: str, name: str) -> bytes:
    """Rilegge il GPX da disco e riscrive <name>/<trk><name> con il nome reale
    della route prima di servirlo al download — il file su disco non cambia.
    """
    with open(gpx_path, "r", encoding="utf-8") as f:
        gpx = gpxpy.parse(f)
    gpx.name = name
    for track in gpx.tracks:
        track.name = name
    return gpx.to_xml().encode("utf-8")


def _build_map(gpx_path: str, start_lat: float, start_lon: float) -> folium.Map:
    m = folium.Map(location=[start_lat, start_lon], zoom_start=12, scrollWheelZoom=False)
    folium.Marker(
        [start_lat, start_lon],
        tooltip="Partenza",
        icon=folium.Icon(color="green"),
    ).add_to(m)
    coords = _gpx_coords(gpx_path)
    folium.PolyLine(coords, color="blue", weight=4, opacity=0.85).add_to(m)
    if coords:
        folium.Marker(coords[-1], tooltip="Fine traccia", icon=folium.Icon(color="red")).add_to(m)
    return m


def _build_multi_map(
    candidates: list,
    scored: list,
    winner_id: str | None,
    start: dict,
) -> folium.Map:
    """Mappa con tutti i candidati sovrapposti: verde=scelto, blu=valido, grigio=scartato."""
    m = folium.Map(location=[start["lat"], start["lon"]], zoom_start=12, scrollWheelZoom=False)
    folium.Marker(
        [start["lat"], start["lon"]],
        tooltip="Partenza",
        icon=folium.Icon(color="green", icon="play"),
    ).add_to(m)

    for c, s in zip(candidates, scored):
        is_winner    = c["id"] == winner_id
        is_discarded = s["discarded"]
        if is_winner:
            color, weight, dash, opacity = "#2dc653", 5, None, 0.9
        elif is_discarded:
            color, weight, dash, opacity = "#aaaaaa", 2, "10 5", 0.6
        else:
            color, weight, dash, opacity = "#3a86ff", 3.5, None, 0.85

        coords = _gpx_coords(c["gpx_path"])
        label = (
            f"{'★ ' if is_winner else ('✗ ' if is_discarded else '· ')}"
            f"{c['id']} — {c['strategy_name']} "
            f"({c['analysis']['distance_km']:.1f} km · {s['total_score']:.0f} pt"
            f"{' · SCARTATO' if is_discarded else ''})"
        )
        fg = folium.FeatureGroup(name=label, show=True)
        poly_kw = dict(color=color, weight=weight, opacity=opacity, tooltip=label)
        if dash:
            poly_kw["dash_array"] = dash
        folium.PolyLine(coords, **poly_kw).add_to(fg)
        fg.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    return m


def _render_climb_profile_chart(elevation_profile: dict):
    """
    Grafico altimetrico (km vs elevazione) colorato per fascia di pendenza
    istantanea — stesse soglie/colori di gpx_analyzer.gradient_color, nessuna
    soglia duplicata qui. Ritorna None se il profilo non ha punti sufficienti.
    """
    dists = elevation_profile.get("distances_km") or []
    elevs = elevation_profile.get("elevations_m") or []
    colors = elevation_profile.get("colors") or []
    if len(dists) < 2:
        return None

    from matplotlib.collections import LineCollection
    from matplotlib.figure import Figure

    segments = [
        [(dists[i], elevs[i]), (dists[i + 1], elevs[i + 1])]
        for i in range(len(dists) - 1)
    ]
    seg_colors = colors[1:]

    fig = Figure(figsize=(9, 2.8), dpi=100)
    ax = fig.add_subplot(111)
    ax.fill_between(dists, elevs, min(elevs), color="#eef1f6", zorder=0)
    ax.add_collection(LineCollection(segments, colors=seg_colors, linewidths=2.2, zorder=2))
    ax.set_xlim(min(dists), max(dists))
    ax.set_ylim(min(elevs) - 15, max(elevs) + 15)
    ax.set_xlabel("km")
    ax.set_ylabel("m")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    return fig


def _climbs_table_rows(climbs: list[dict], top_n: int | None = 5) -> list[dict]:
    """Righe tabella 'Salite principali', ordinate per dislivello decrescente."""
    ranked = sorted(climbs, key=lambda c: c["elevation_gain_m"], reverse=True)
    if top_n is not None:
        ranked = ranked[:top_n]
    rows = []
    for c in ranked:
        class_label = t(f"climbs.class.{c['classification']}")
        rows.append({
            t("climbs.col_start"): f"{c['start_km']:.1f} km",
            t("climbs.col_length"): f"{c['length_m']:.0f} m",
            t("climbs.col_gain"): f"{c['elevation_gain_m']:.0f} m",
            t("climbs.col_avg_grad"): f"{c['avg_gradient_percent']:.1f}%",
            t("climbs.col_max_grad"): f"{c['max_gradient_percent']:.1f}%",
            t("climbs.col_class"): f"{c['classification_emoji']} {class_label}",
        })
    return rows


def _climbs_analysis_table_rows(merged_climbs: list[dict], is_ebike: bool) -> list[dict]:
    """Righe tabella 'Salite di questo giro' (Ride Analysis) — oggettive + soggettive AI."""
    rows = []
    for c in merged_climbs:
        class_label = t(f"climbs.class.{c['classification']}")
        row = {
            t("climbs.col_start"): f"{c['start_km']:.1f} km",
            t("climbs.col_length"): f"{c['length_m']:.0f} m",
            t("climbs.col_gain"): f"{c['elevation_gain_m']:.0f} m",
            t("climbs.col_avg_grad"): f"{c['avg_gradient_percent']:.1f}%",
            t("climbs.col_max_grad"): f"{c['max_gradient_percent']:.1f}%",
            t("climbs.col_class"): f"{c['classification_emoji']} {class_label}",
            t("climbs.col_kcal"): f"{c['kcal']} kcal" if c.get("kcal") is not None else "—",
            t("climbs.col_hr"): f"{c['avg_hr_bpm']} bpm" if c.get("avg_hr_bpm") is not None else "—",
        }
        if is_ebike:
            row[t("climbs.col_battery")] = (
                f"{c['battery_pct_consumed']:.1f}%" if c.get("battery_pct_consumed") is not None else "—"
            )
        row[t("climbs.col_fatigue")] = (
            f"{c['fatigue_contribution']}/10" if c.get("fatigue_contribution") is not None else "—"
        )
        rows.append(row)
    return rows


def _estimate_route_km(ordered_wps: list[dict]) -> float:
    """Geodesic sum of consecutive waypoints × 1.6 calibration factor."""
    pts = [(wp["lat"], wp["lon"]) for wp in ordered_wps if wp.get("lat") is not None]
    if len(pts) < 2:
        return 0.0
    return sum(geodesic(pts[i], pts[i + 1]).km for i in range(len(pts) - 1)) * 1.6


def _build_preview_map(ordered_wps: list[dict], center_lat: float, center_lon: float) -> folium.Map:
    """Mappa di anteprima con linee rette tra i waypoint (nessuna traccia BRouter)."""
    m = folium.Map(location=[center_lat, center_lon], zoom_start=11, scrollWheelZoom=False)

    coords = [(wp["lat"], wp["lon"]) for wp in ordered_wps if wp.get("lat") is not None]
    if len(coords) >= 2:
        folium.PolyLine(
            coords, color="#888888", weight=2, dash_array="8 5", opacity=0.65,
            tooltip="Linee rette — percorso reale calcolato dal Builder"
        ).add_to(m)

    role_style = {
        "start": ("green", "play"),
        "end":   ("red",   "stop"),
        "via":   ("blue",  "map-marker"),
    }
    src_color = {"planner": "purple", "user": None}

    for wp in ordered_wps:
        if wp.get("lat") is None:
            continue
        role = wp.get("role", "via")
        src  = wp.get("source", "user")
        color, icon_name = role_style.get(role, ("blue", "map-marker"))
        if src == "planner":
            color = "purple"
        label = f"{wp['order']}. {wp['name']} [{role}, {src}]"
        folium.Marker(
            [wp["lat"], wp["lon"]],
            tooltip=label,
            popup=label,
            icon=folium.Icon(color=color, icon=icon_name, prefix="fa"),
        ).add_to(m)

    return m


def _detect_route_from_plan_gpx(
    uploaded_file,
    saved_routes: dict,
) -> tuple[str | None, str]:
    """Return (route_name, detection_note) or (None, warning_msg).

    Detection order:
    1. Filename stem stripped of candidate suffix (_A/_B/_C) → exact match
    2. GPX <name> metadata → exact match (same strip applied)
    3. Full filename stem → exact match
    All comparisons are case-insensitive.
    """
    stem = Path(uploaded_file.name).stem          # e.g. "anello_corinaldo_A"
    clean = re.sub(r"[_\-][ABCabc]$", "", stem)  # → "anello_corinaldo"

    route_keys_lower = {k.lower(): k for k in saved_routes}

    # 1. Cleaned stem
    if clean.lower() in route_keys_lower:
        matched = route_keys_lower[clean.lower()]
        note = f"rilevato dal nome file (`{stem}` → `{matched}`)"
        return matched, note

    # 2. GPX metadata <name>
    try:
        gpx_obj = gpxpy.parse(uploaded_file.getvalue().decode("utf-8"))
        gpx_name = (gpx_obj.name or "").strip()
        if gpx_name:
            clean_gpx = re.sub(r"[_\-][ABCabc]$", "", gpx_name)
            for candidate in (clean_gpx, gpx_name):
                if candidate.lower() in route_keys_lower:
                    matched = route_keys_lower[candidate.lower()]
                    note = f"rilevato dai metadati GPX (`<name>{gpx_name}</name>` → `{matched}`)"
                    return matched, note
    except Exception:
        pass

    # 3. Full stem (no strip)
    if stem.lower() in route_keys_lower:
        matched = route_keys_lower[stem.lower()]
        note = f"rilevato dal nome file (`{stem}`)"
        return matched, note

    known = ", ".join(f"`{k}`" for k in list(saved_routes)[:6])
    warn = (
        f"Nessuna route salvata corrisponde al file `{uploaded_file.name}`. "
        f"Route disponibili: {known}{'…' if len(saved_routes) > 6 else ''}. "
        "Rinomina il GPX pianificato con il nome della route (es. `nome_route_A.gpx`) "
        "oppure genera prima la route nel Planner."
    )
    return None, warn


def _save_comparison_to_route(route_name: str, record: dict) -> None:
    """Appende un record di confronto in comparison_history dentro il JSON della route."""
    path = _PLANNED_DIR / f"{route_name}.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    history = data.get("comparison_history", [])
    history.append(record)
    data["comparison_history"] = history
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_ride_analysis_to_route(route_name: str, record: dict) -> None:
    """Appende un record di analisi Ride Analysis in ride_analysis_history dentro
    il JSON della route — stesso pattern di _save_comparison_to_route. Il report
    HTML completo vive già su disco (_RIDE_REPORTS_DIR); qui salviamo solo il
    path e i numeri chiave in chiaro (kcal/bpm/batteria), non il contenuto."""
    path = _PLANNED_DIR / f"{route_name}.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    history = data.get("ride_analysis_history", [])
    history.append(record)
    data["ride_analysis_history"] = history
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_feedback_to_route(route_name: str, record: dict) -> None:
    """
    Appende un record di feedback soggettivo (rating/checkbox/note) in
    feedback_history dentro il JSON della route — stesso pattern di
    _save_comparison_to_route. Non include i segnaposto "problema": quelli
    vengono promossi a known_obstacles (SQLite) dal chiamante, non duplicati
    qui — il feedback_history è solo l'opinione soggettiva sulla route.
    """
    path = _PLANNED_DIR / f"{route_name}.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    history = data.get("feedback_history", [])
    history.append(record)
    data["feedback_history"] = history
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_saved_routes() -> dict[str, dict]:
    """Carica tutti i JSON da routes/planned/ — {route_name: data}."""
    routes = {}
    for p in sorted(_PLANNED_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            routes[data.get("route_name", p.stem)] = data
        except Exception:
            pass
    return routes


def _open_route_name() -> str | None:
    """
    Nome della route corrente "aperta" nel workspace (navigazione tipo
    "documento aperto") — None se nessuna route è aperta. Riusa lo stesso
    session_state già scritto da "Carica nel Planner"/File→Apri
    (pl_loaded_route_name) come unica fonte di verità, invece di introdurre
    un secondo stato da tenere sincronizzato: chi apre una route la carica
    comunque nel Planner (stesso meccanismo), quindi i due concetti
    coincidono per costruzione.
    """
    return st.session_state.get("pl_loaded_route_name")


def _open_route_pending(_load_name: str) -> None:
    """Segnala l'apertura di una route (File→Apri, o scorciatoie equivalenti
    nei placeholder workspace) — stesso meccanismo di "Carica nel Planner"."""
    st.session_state["_pl_load_route_pending"] = _load_name


def _render_no_route_placeholder(saved_routes: dict[str, dict]) -> None:
    """
    Placeholder condiviso dai tab workspace vincolati a una route aperta
    (oggi solo Builder — per costruzione richiede sempre una route salvata;
    Planner/Ride Analysis/Post Ride/GPX Optimizer restano invece sempre
    utilizzabili, vedi discussione di design). Include una scorciatoia
    "Apri" inline equivalente a quella nel tab File — st.tabs non permette
    di cambiare tab attivo da codice server-side, quindi la scorciatoia
    reale è questo mini-selettore, non un link cliccabile al tab File.
    """
    st.info(f"**{t('workspace.no_route_title')}** — {t('workspace.no_route_hint')}")
    if saved_routes:
        _ph_sel = st.selectbox(
            t("file.open_select"),
            [t("builder.select_placeholder")] + list(saved_routes.keys()),
            key="ws_placeholder_sel",
        )
        if _ph_sel != t("builder.select_placeholder"):
            if st.button(t("file.open_btn"), key="ws_placeholder_open_btn", type="primary"):
                _open_route_pending(_ph_sel)
                st.rerun()


def _has_unsaved_planner_work() -> bool:
    """
    True se c'è una pianificazione in corso nel Planner non ancora salvata —
    usato da "Nuovo" (tab File) per decidere se avvisare prima di scartarla.

    Due casi: (1) pl_dirty — una generazione IA (Pianifica/Rigenera) fatta e
    non ancora salvata, impostato/azzerato esplicitamente attorno a quei
    flussi; (2) nessuna generazione ancora, ma il form ha campi scritti
    (waypoint, nome partenza) rispetto ai default E nessuna route è
    caricata — un form "sporco" dopo aver caricato una route è normale, non
    è lavoro incompiuto.
    """
    if st.session_state.get("pl_dirty"):
        return True
    if st.session_state.get("pl_loaded_route_name"):
        return False
    if (st.session_state.get("pl_wps") or "").strip():
        return True
    if (st.session_state.get("pl_sname") or "Senigallia") != "Senigallia":
        return True
    return False


def _reset_planner_draft() -> None:
    """
    Pulisce lo stato del Planner e prepara il form vuoto per una nuova
    pianificazione (non tocca route già salvate su disco). Stessa logica
    del vecchio bottone "Reset" nel Planner — ora richiamata solo da
    "Nuovo" nel tab File (unico punto d'ingresso per "ricomincia da capo",
    coerente con la navigazione a documento aperto: avere due bottoni con
    conferme diverse per la stessa azione sarebbe stata una scappatoia per
    bypassare l'avviso di lavoro non salvato).
    """
    _pl_flow_keys = [
        "pl_result", "pl_request", "pl_naming_active", "pl_dirty",
        "pl_name_suggestion", "pl_route_name_final", "pl_loaded_route_name",
    ]
    for _k in _pl_flow_keys:
        st.session_state.pop(_k, None)
    for _k in [k for k in st.session_state if k.startswith("pl_saved_")]:
        st.session_state.pop(_k, None)
    st.session_state["_pl_reset_pending"] = True


def _normalize_builder_results(data: dict) -> list[dict]:
    """
    builder_results è una lista di run Builder (una per generazione, accumula
    nel tempo — redesign persistenza). I JSON salvati prima del redesign hanno
    builder_results come oggetto singolo (l'unica run, sovrascritta ad ogni
    generazione) o assente — qui normalizziamo entrambi i casi a lista, senza
    toccare il file su disco: la migrazione fisica non è richiesta, solo la
    lettura senza errori.
    """
    br = data.get("builder_results")
    if br is None:
        return []
    if isinstance(br, dict):
        return [br] if br else []
    return list(br)


def _count_route_artifacts(route_data: dict) -> dict:
    """
    Conta cosa verrebbe perso da un "Aggiorna [nome]" su questa route —
    usato sia per l'avviso di conferma sia per la cancellazione vera e
    propria (_delete_route_artifacts riusa le stesse cartelle/file).
    """
    builder_runs = _normalize_builder_results(route_data)
    gpx_dirs: set[str] = set()
    gpx_file_count = 0
    for run in builder_runs:
        for c in run.get("candidates", []):
            gp = c.get("gpx_path")
            if gp:
                gpx_file_count += 1
                gpx_dirs.add(str(Path(gp).parent))

    ride_history = route_data.get("ride_analysis_history", [])
    report_files = [r["report_path"] for r in ride_history if r.get("report_path")]

    return {
        "builder_runs": len(builder_runs),
        "ride_analyses": len(ride_history),
        "feedbacks": len(route_data.get("feedback_history", [])),
        "gpx_dirs": sorted(gpx_dirs),
        "gpx_file_count": gpx_file_count,
        "report_files": report_files,
    }


def _delete_route_artifacts(route_data: dict) -> None:
    """
    Cancella davvero da disco i file/cartelle referenziati da builder_results
    (routes/generated/{timestamp}/ — condivisa dai 3 candidati di uno stesso
    run, sicuro rimuoverla per intero) e ride_analysis_history (report HTML
    singoli) — non solo i riferimenti nel JSON. Chiamata da "Aggiorna [nome]"
    subito prima di riscrivere la pianificazione con le liste azzerate.
    """
    counts = _count_route_artifacts(route_data)
    for d in counts["gpx_dirs"]:
        try:
            shutil.rmtree(d)
        except OSError:
            pass
    for f in counts["report_files"]:
        try:
            Path(f).unlink(missing_ok=True)
        except OSError:
            pass


def _delete_route_completely(route_name: str, route_data: dict) -> None:
    """
    Elimina una route per intero (File → Le mie route → 🗑️): stessa cascata
    di _delete_route_artifacts (GPX generati + report Ride Analysis), più il
    JSON stesso — a differenza di "Aggiorna", che riusa _delete_route_artifacts
    da solo per poi riscrivere il JSON con le liste azzerate, qui non c'è
    nulla da riscrivere.
    """
    _delete_route_artifacts(route_data)
    (_PLANNED_DIR / f"{route_name}.json").unlink(missing_ok=True)


def _rename_route(old_name: str, new_name: str) -> Path:
    """
    Rinomina una route: file JSON su disco + campo route_name interno.
    I GPX già generati con il vecchio nome NON vengono toccati — restano
    "{old_name}_{label}" come nome interno <trk><name>, comportamento voluto
    e verificato in una sessione precedente (solo le nuove generazioni dopo
    il rename usano il nome aggiornato). Il chiamante deve aver già
    verificato che new_name non sia già in uso.
    """
    old_path = _PLANNED_DIR / f"{old_name}.json"
    data = json.loads(old_path.read_text(encoding="utf-8"))
    data["route_name"] = new_name
    new_path = _PLANNED_DIR / f"{new_name}.json"
    new_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    old_path.unlink()
    return new_path


def _fmt_size_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    return f"{n / 1000:.1f} KB"


def _find_orphan_gpx_dirs() -> list[dict]:
    """
    Cartelle in routes/generated/ non referenziate da builder_results in
    NESSUNA route salvata — confronta per nome cartella (es. "20260803_220229"),
    non per path assoluto: i path salvati nel JSON sono quelli visti dal
    processo Python (assoluti, dentro il container), mentre qui iteriamo il
    filesystem con path relativi — il nome cartella è l'unico identificatore
    stabile condiviso tra le due rappresentazioni.
    """
    referenced_names = set()
    for route_data in _load_saved_routes().values():
        for d in _count_route_artifacts(route_data)["gpx_dirs"]:
            referenced_names.add(Path(d).name)

    gen_dir = Path("routes/generated")
    orphans = []
    if gen_dir.exists():
        for d in sorted(gen_dir.iterdir()):
            if d.is_dir() and d.name not in referenced_names:
                gpx_files = list(d.glob("*.gpx"))
                orphans.append({
                    "path": d,
                    "n_files": len(gpx_files),
                    "size_bytes": sum(f.stat().st_size for f in gpx_files),
                    "mtime": d.stat().st_mtime,
                })
    return orphans


def _find_orphan_ride_reports() -> list[Path]:
    """Stessa idea di _find_orphan_gpx_dirs, per i report HTML di Ride Analysis
    (_RIDE_REPORTS_DIR) non referenziati da nessun ride_analysis_history."""
    referenced_names = set()
    for route_data in _load_saved_routes().values():
        for r in route_data.get("ride_analysis_history", []):
            rp = r.get("report_path")
            if rp:
                referenced_names.add(Path(rp).name)

    orphans = []
    if _RIDE_REPORTS_DIR.exists():
        for f in sorted(_RIDE_REPORTS_DIR.glob("*.html")):
            if f.name not in referenced_names:
                orphans.append(f)
    return orphans


def _find_route_winner_gpx(route_name: str) -> tuple[Path, str, str] | None:
    """
    (gpx_path, candidate_id, profile) del candidato vincitore dell'ultima run
    Builder per questa route — None se la route non ha ancora nessuna run,
    se l'ultima run non ha un vincitore deciso, o se il suo GPX non esiste
    più su disco. Usato dalla scorciatoia "usa il vincitore" di GPX Optimizer.
    """
    route_data = _load_saved_routes().get(route_name)
    if not route_data:
        return None
    runs = _normalize_builder_results(route_data)
    if not runs:
        return None
    winner_id = (runs[-1].get("decision") or {}).get("winner")
    if not winner_id:
        return None
    for c in runs[-1].get("candidates", []):
        if c.get("id") == winner_id and c.get("gpx_path"):
            p = Path(c["gpx_path"])
            if p.exists():
                return p, winner_id, c.get("profile", "")
    return None


def _save_planned_route(
    route_name: str,
    request: RouteRequest,
    ordered_wps: list,
    system_prompt: str,
    user_prompt: str,
    warnings: list[str],
    search_queries: list[str] | None = None,
    route_narrative: str = "",
) -> Path:
    """
    Salva la bozza Planner in routes/planned/{route_name}.json.

    Schema (redesign persistenza): la sezione di pianificazione (request/
    narrativa/waypoint/prompt) è sempre sovrascritta con l'ultimo salvataggio;
    le liste sottostanti accumulano nel tempo e vengono azzerate SOLO da un
    "Aggiorna [nome]" esplicito (vedi _delete_route_artifacts), mai da un
    salvataggio Planner qualunque — chi chiama questa funzione su una route
    già esistente deve aver già gestito quell'azzeramento a monte.
    """
    _PLANNED_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "route_name": route_name,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "request": request.model_dump(),
        "route_narrative": route_narrative,
        "ordered_waypoints": ordered_wps,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "warnings": warnings,
        "search_queries": search_queries or [],
        "builder_results": [],
        "comparison_history": [],
        "ride_analysis_history": [],
        "feedback_history": [],
    }
    path = _PLANNED_DIR / f"{route_name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ── Helper GPX ──────────────────────────────────────────────────────────
def _parse_gpx_bytes(raw: bytes):
    parsed = gpxpy.parse(raw.decode("utf-8"))
    pts = [pt for track in parsed.tracks for seg in track.segments for pt in seg.points]
    coords = [(pt.latitude, pt.longitude) for pt in pts]
    return pts, coords


def _analyze_gpx_bytes(raw: bytes) -> tuple[list, list, dict]:
    pts, coords = _parse_gpx_bytes(raw)
    with tempfile.NamedTemporaryFile(suffix=".gpx", delete=False, mode="wb") as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        closure_m = geodesic(coords[0], coords[-1]).meters if len(coords) >= 2 else 0.0
        auto_rt = "loop" if closure_m < 500 else "out_and_back"
        analysis = analyze_gpx(tmp_path, route_type=auto_rt)
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass
    return pts, coords, analysis


# ─── Tab: File ────────────────────────────────────────────────────────────────
# Punto di ingresso/gestione route, sempre accessibile (navigazione tipo
# "documento aperto" — non solo schermata di avvio). File orfani arriva
# nella parte successiva di questa ristrutturazione.
with tab_file:
    st.subheader(t("tabs.file"))
    st.caption(t("file.caption"))

    _file_open_now = _open_route_name()
    if _file_open_now:
        st.success(t("file.currently_open").format(name=_file_open_now))

    # ── Nuovo ──────────────────────────────────────────────────────────────
    st.markdown(t("file.new_header"))
    if st.button(t("file.new_btn"), key="file_new_btn"):
        if _has_unsaved_planner_work():
            st.session_state["_file_new_confirm_pending"] = True
        else:
            _reset_planner_draft()
        st.rerun()

    if st.session_state.get("_file_new_confirm_pending"):
        st.warning(t("file.new_unsaved_warning"))
        _fn_c1, _fn_c2 = st.columns(2)
        with _fn_c1:
            if st.button(t("file.new_confirm_btn"), key="file_new_confirm_yes", type="primary"):
                st.session_state.pop("_file_new_confirm_pending", None)
                _reset_planner_draft()
                st.rerun()
        with _fn_c2:
            if st.button(t("debug.cancel_delete_btn"), key="file_new_confirm_no"):
                st.session_state.pop("_file_new_confirm_pending", None)
                st.rerun()

    st.divider()

    # ── Apri ───────────────────────────────────────────────────────────────
    st.markdown(t("file.open_header"))
    _file_routes = _load_saved_routes()
    if not _file_routes:
        st.caption(t("debug.no_routes"))
    else:
        _file_sel = st.selectbox(
            t("file.open_select"),
            [t("builder.select_placeholder")] + list(_file_routes.keys()),
            key="file_open_sel",
        )
        if _file_sel != t("builder.select_placeholder"):
            if st.button(t("file.open_btn"), key="file_open_btn", type="primary"):
                _open_route_pending(_file_sel)
                st.rerun()

    # ── Recenti ────────────────────────────────────────────────────────────
    # created_at viene riscritto ad ogni salvataggio (sia "Salva come nuova"
    # che "Aggiorna", vedi _save_planned_route) — funziona già come "ultima
    # modifica" anche se il nome del campo suggerisce solo la creazione,
    # nessun nuovo campo updated_at necessario.
    st.divider()
    st.markdown(t("file.recent_header"))
    if not _file_routes:
        st.caption(t("debug.no_routes"))
    else:
        _recent_routes = sorted(
            _file_routes.items(), key=lambda kv: kv[1].get("created_at", ""), reverse=True,
        )[:5]
        for _rcname, _rcdata in _recent_routes:
            _rcc1, _rcc2 = st.columns([5, 1])
            with _rcc1:
                st.caption(f"**{_rcname}** — {_rcdata.get('created_at', '—')}")
            with _rcc2:
                if st.button(t("file.open_btn"), key=f"file_recent_open_{_rcname}"):
                    _open_route_pending(_rcname)
                    st.rerun()

    # ── Le mie route ───────────────────────────────────────────────────────
    st.divider()
    st.markdown(t("file.myroutes_header"))
    if not _file_routes:
        st.caption(t("debug.no_routes"))
    else:
        for _mname, _mdata in _file_routes.items():
            _mcounts = _count_route_artifacts(_mdata)
            _mcompares = len(_mdata.get("comparison_history", []))
            with st.container(border=True):
                _mc1, _mc2, _mc3, _mc4 = st.columns([3, 1, 1, 1])
                with _mc1:
                    st.markdown(f"**{_mname}**")
                    st.caption(
                        t("file.myroutes_summary").format(
                            created=_mdata.get("created_at", "—"),
                            builder=_mcounts["builder_runs"],
                            ride=_mcounts["ride_analyses"],
                            compare=_mcompares,
                            feedback=_mcounts["feedbacks"],
                        )
                    )
                with _mc2:
                    if st.button("📂", key=f"file_my_open_{_mname}", help=t("file.open_btn")):
                        _open_route_pending(_mname)
                        st.rerun()
                with _mc3:
                    if st.button("✏️", key=f"file_my_rename_btn_{_mname}", help=t("file.rename_btn")):
                        st.session_state[f"_file_rename_pending_{_mname}"] = True
                        st.rerun()
                with _mc4:
                    if st.button("🗑️", key=f"file_my_del_btn_{_mname}", help=t("file.delete_btn")):
                        st.session_state[f"_file_delete_pending_{_mname}"] = True
                        st.rerun()

                # ── Rinomina inline ──────────────────────────────────────────
                if st.session_state.get(f"_file_rename_pending_{_mname}"):
                    _new_name = st.text_input(
                        t("file.rename_prompt"), value=_mname, key=f"file_rename_input_{_mname}",
                    )
                    _rn_c1, _rn_c2 = st.columns(2)
                    with _rn_c1:
                        if st.button(
                            t("file.rename_confirm_btn"), key=f"file_rename_confirm_{_mname}", type="primary",
                        ):
                            _new_name_clean = _new_name.strip()
                            if not _new_name_clean:
                                st.error(t("file.rename_empty_error"))
                            elif _new_name_clean == _mname:
                                st.session_state.pop(f"_file_rename_pending_{_mname}", None)
                                st.rerun()
                            elif _new_name_clean in _file_routes:
                                st.error(t("planner.save_new_name_conflict").format(name=_new_name_clean))
                            else:
                                _rename_route(_mname, _new_name_clean)
                                # I GPX/report già generati restano col vecchio nome
                                # (comportamento voluto) — solo il riferimento alla
                                # route "aperta" segue il rename, se era questa.
                                if _open_route_name() == _mname:
                                    st.session_state["pl_loaded_route_name"] = _new_name_clean
                                st.session_state.pop(f"_file_rename_pending_{_mname}", None)
                                st.success(t("file.rename_success").format(old=_mname, new=_new_name_clean))
                                st.rerun()
                    with _rn_c2:
                        if st.button(t("debug.cancel_delete_btn"), key=f"file_rename_cancel_{_mname}"):
                            st.session_state.pop(f"_file_rename_pending_{_mname}", None)
                            st.rerun()

                # ── Elimina con doppia conferma e conteggi esatti ────────────
                if st.session_state.get(f"_file_delete_pending_{_mname}"):
                    st.warning(
                        t("file.delete_confirm_msg").format(
                            name=_mname,
                            builder=_mcounts["builder_runs"],
                            ride=_mcounts["ride_analyses"],
                            compare=_mcompares,
                            feedback=_mcounts["feedbacks"],
                            files=_mcounts["gpx_file_count"] + len(_mcounts["report_files"]),
                        )
                    )
                    _dl_c1, _dl_c2 = st.columns(2)
                    with _dl_c1:
                        if st.button(t("debug.confirm_delete_btn"), key=f"file_del_confirm_{_mname}", type="primary"):
                            _delete_route_completely(_mname, _mdata)
                            # Se stavo lavorando proprio su questa route, torno a
                            # una bozza vuota (non ha senso lasciare il Planner a
                            # mostrare dati di una route ora cancellata).
                            if _open_route_name() == _mname:
                                _reset_planner_draft()
                            st.session_state.pop(f"_file_delete_pending_{_mname}", None)
                            st.success(t("file.delete_success").format(name=_mname))
                            st.rerun()
                    with _dl_c2:
                        if st.button(t("debug.cancel_delete_btn"), key=f"file_del_cancel_{_mname}"):
                            st.session_state.pop(f"_file_delete_pending_{_mname}", None)
                            st.rerun()

    # ── File orfani ────────────────────────────────────────────────────────
    # Cartelle GPX/report non referenziate da nessuna route salvata — diverso
    # dalla sezione "Cartelle GPX generate" già esistente in Utility →
    # Gestione file, che elenca TUTTE le cartelle indiscriminatamente (nessun
    # filtro orfani lì, verificato in una parte precedente di questo lavoro):
    # qui invece solo quelle davvero non più collegate a nulla, più sicuro da
    # cancellare in blocco. Lasciata invariata la sezione in Utility.
    st.divider()
    st.markdown(t("file.orphans_header"))
    _orphan_dirs = _find_orphan_gpx_dirs()
    _orphan_reports = _find_orphan_ride_reports()
    if not _orphan_dirs and not _orphan_reports:
        st.caption(t("file.orphans_none"))
    else:
        st.caption(t("file.orphans_intro"))
        for _od in _orphan_dirs:
            _odname = _od["path"].name
            _oc1, _oc2 = st.columns([5, 1])
            with _oc1:
                _od_date = datetime.datetime.fromtimestamp(_od["mtime"]).strftime("%Y-%m-%d %H:%M")
                st.caption(
                    t("file.orphan_dir_row").format(
                        name=_odname, n=_od["n_files"], size=_fmt_size_bytes(_od["size_bytes"]), date=_od_date,
                    )
                )
            with _oc2:
                if st.button("🗑️", key=f"file_orphan_dir_del_{_odname}", help=t("file.delete_btn")):
                    st.session_state[f"_file_orphan_dir_confirm_{_odname}"] = True
                    st.rerun()
            if st.session_state.get(f"_file_orphan_dir_confirm_{_odname}"):
                st.warning(t("file.orphan_confirm_msg").format(name=_odname))
                _odc1, _odc2 = st.columns(2)
                with _odc1:
                    if st.button(t("debug.confirm_delete_btn"), key=f"file_orphan_dir_yes_{_odname}", type="primary"):
                        shutil.rmtree(_od["path"], ignore_errors=True)
                        st.session_state.pop(f"_file_orphan_dir_confirm_{_odname}", None)
                        st.rerun()
                with _odc2:
                    if st.button(t("debug.cancel_delete_btn"), key=f"file_orphan_dir_no_{_odname}"):
                        st.session_state.pop(f"_file_orphan_dir_confirm_{_odname}", None)
                        st.rerun()

        for _orf in _orphan_reports:
            _orfname = _orf.name
            _rc1, _rc2 = st.columns([5, 1])
            with _rc1:
                _orf_date = datetime.datetime.fromtimestamp(_orf.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                st.caption(
                    t("file.orphan_report_row").format(
                        name=_orfname, size=_fmt_size_bytes(_orf.stat().st_size), date=_orf_date,
                    )
                )
            with _rc2:
                if st.button("🗑️", key=f"file_orphan_rep_del_{_orfname}", help=t("file.delete_btn")):
                    st.session_state[f"_file_orphan_rep_confirm_{_orfname}"] = True
                    st.rerun()
            if st.session_state.get(f"_file_orphan_rep_confirm_{_orfname}"):
                st.warning(t("file.orphan_confirm_msg").format(name=_orfname))
                _orc1, _orc2 = st.columns(2)
                with _orc1:
                    if st.button(t("debug.confirm_delete_btn"), key=f"file_orphan_rep_yes_{_orfname}", type="primary"):
                        _orf.unlink(missing_ok=True)
                        st.session_state.pop(f"_file_orphan_rep_confirm_{_orfname}", None)
                        st.rerun()
                with _orc2:
                    if st.button(t("debug.cancel_delete_btn"), key=f"file_orphan_rep_no_{_orfname}"):
                        st.session_state.pop(f"_file_orphan_rep_confirm_{_orfname}", None)
                        st.rerun()


# ─── Tab: Planner ─────────────────────────────────────────────────────────────
with tab_planner:
    st.subheader(t("planner.subheader"))
    st.caption(t("planner.caption"))
    if _open_route_name():
        st.success(t("planner.open_route_banner").format(name=_open_route_name()))

    # Applica il reset PRIMA che i widget pl_ vengano istanziati in questo run:
    # solo un'assegnazione esplicita a session_state[key] fatta prima del
    # rendering del widget si riflette davvero sul valore mostrato nel DOM —
    # un pop() da solo pulisce lo stato Python ma lascia il widget invariato
    # a schermo (comportamento Streamlit). pl_slat/pl_slon sono gestiti dal
    # setdefault già presente più sotto: basta rimuoverli qui.
    if st.session_state.pop("_pl_reset_pending", False):
        st.session_state.pop("pl_slat", None)
        st.session_state.pop("pl_slon", None)
        st.session_state["pl_sname"] = "Senigallia"
        st.session_state["pl_km"] = 60
        st.session_state["pl_rt"] = "loop"
        st.session_state["pl_scenery"] = "misto"
        st.session_state["pl_athletic"] = "medio"
        st.session_state["pl_direction"] = t("planner.form.direction_free")
        st.session_state["pl_ename"] = ""
        # None, non un default hardcoded plausibile (era la causa del bug
        # "finisce sempre ad Ancona": 43.6158/13.5189 sembravano coordinate
        # vere e passavano inosservate se l'utente scriveva solo il nome).
        st.session_state["pl_elat"] = None
        st.session_state["pl_elon"] = None
        st.session_state["pl_wps"] = ""
        st.session_state["pl_elevpref"] = t("planner.form.elevation_pref_none")
        st.session_state["pl_avoid"] = ""
        st.session_state["pl_free"] = ""
        st.session_state.pop("pl_loaded_route_name", None)

    # Carica una route salvata nel form (bottone "📂 Carica nel Planner" in
    # Utility → Gestione file) — stesso pattern del reset sopra: i valori reali
    # al posto dei default, assegnati a session_state PRIMA che i widget pl_
    # rendano, altrimenti Streamlit non li mostrerebbe (vedi commento sopra).
    _pl_load_name = st.session_state.pop("_pl_load_route_pending", None)
    if _pl_load_name:
        _load_routes = _load_saved_routes()
        _load_data = _load_routes.get(_pl_load_name)
        if _load_data:
            _load_req = _load_data.get("request", {})
            _load_start = _load_req.get("start") or {}
            _load_end = _load_req.get("end") or {}
            _load_target_km = _load_req.get("target_km", 60.0)
            _load_unconstrained = _load_target_km == -1.0
            _KM_OPTIONS = [40, 50, 55, 60, 65, 70, 80, 100]

            st.session_state.pop("pl_slat", None)
            st.session_state.pop("pl_slon", None)
            st.session_state["pl_sname"] = _load_start.get("name", "Senigallia")
            st.session_state["pl_slat"] = _load_start.get("lat", 43.7136520)
            st.session_state["pl_slon"] = _load_start.get("lon", 13.2278056)
            st.session_state["pl_km_unconstrained"] = _load_unconstrained
            st.session_state["pl_km"] = (
                _load_target_km if _load_target_km in _KM_OPTIONS else 60
            )
            st.session_state["pl_rt"] = _load_req.get("route_type", "loop")
            st.session_state["pl_scenery"] = _load_req.get("scenery_theme", "misto")
            st.session_state["pl_athletic"] = _load_req.get("athletic_theme", "medio")
            _load_dir = _load_req.get("geographic_direction")
            st.session_state["pl_direction"] = _load_dir or t("planner.form.direction_free")
            st.session_state["pl_ename"] = _load_end.get("name", "")
            # None se la route caricata non aveva un end (es. era un loop) —
            # non le coordinate di Ancona come prima.
            st.session_state["pl_elat"] = _load_end.get("lat")
            st.session_state["pl_elon"] = _load_end.get("lon")
            # "!" finale ricostruito per i waypoint mandatory=True — stesso
            # formato testuale che _parse_pl_waypoint_line si aspetta in input.
            st.session_state["pl_wps"] = "\n".join(
                f"{w.get('name', '')}!" if w.get("mandatory") else w.get("name", "")
                for w in _load_req.get("user_waypoints", [])
            )
            st.session_state["pl_elevpref"] = t(
                f"planner.form.elevation_pref_{_load_req.get('elevation_preference', 'none')}"
            )
            st.session_state["pl_avoid"] = ", ".join(_load_req.get("avoid_named_roads", []))
            st.session_state["pl_free"] = _load_req.get("free_text", "")

            st.session_state["pl_loaded_route_name"] = _pl_load_name
            st.session_state.pop("pl_result", None)
            st.session_state.pop("pl_naming_active", None)
            st.session_state.pop("pl_name_suggestion", None)
            st.info(t("planner.loaded_from_route").format(name=_pl_load_name))

    col_form, col_map = st.columns([1, 2], gap="large")

    with col_form:
        st.markdown(t("planner.start_label"))

        # Inizializza i default UNA SOLA VOLTA (setdefault è no-op se già presenti).
        # Separare init da rendering evita che value= nel number_input sovrascriva
        # il session_state aggiornato dal geocoding a ogni rerun silenzioso.
        st.session_state.setdefault("pl_slat", 43.7136520)
        st.session_state.setdefault("pl_slon", 13.2278056)

        col_s1, col_s2, col_s3 = st.columns([2, 1, 1])
        with col_s1:
            _nc, _bc = st.columns([3, 1])
            with _nc:
                pl_start_name = st.text_input(t("planner.form.name"), value="Senigallia", key="pl_sname")
            with _bc:
                st.write("")  # allinea verticalmente con il label dell'input
                geo_btn = st.button(
                    "📍", key="btn_geo_start",
                    help=t("planner.geo_btn_help"),
                    use_container_width=True,
                )
            # Geocoding QUI: aggiorna session_state prima che col_s2/col_s3 leggano i valori.
            if geo_btn:
                _gname = (st.session_state.get("pl_sname") or "").strip()
                if _gname:
                    from geocoding_agent import geocode_place as _gp
                    with st.spinner(f"Geocoding «{_gname}»…"):
                        _gresult = _gp(_gname, region=None)
                    if _gresult:
                        st.session_state["pl_slat"] = _gresult[0]
                        st.session_state["pl_slon"] = _gresult[1]
                    else:
                        st.warning(f"«{_gname}» {t('planner.geo_not_found')}")
        # number_input senza value=: legge SOLO da session_state, mai da un default hardcoded.
        with col_s2:
            pl_start_lat = st.number_input(t("planner.form.lat"), format="%.7f", step=1e-7, key="pl_slat")
        with col_s3:
            pl_start_lon = st.number_input(t("planner.form.lon"), format="%.7f", step=1e-7, key="pl_slon")

        col_d1, col_d2 = st.columns(2)
        with col_d1:
            pl_km_unconstrained = st.checkbox(
                t("planner.form.km_unconstrained"),
                key="pl_km_unconstrained",
                help=t("planner.form.km_unconstrained_help"),
            )
            pl_target_km = st.selectbox(
                t("planner.form.target_km"), [40, 50, 55, 60, 65, 70, 80, 100],
                index=3, key="pl_km",
                disabled=pl_km_unconstrained,
            )
            pl_route_type = st.selectbox(
                t("planner.form.route_type"), ["loop", "out_and_back", "point_to_point"],
                key="pl_rt",
            )
        with col_d2:
            pl_scenery = st.selectbox(
                t("planner.form.scenery"),
                ["naturalistico", "storico_culturale", "panoramico", "misto"],
                index=3, key="pl_scenery",
            )
            pl_athletic = st.selectbox(
                t("planner.form.athletic"),
                ["tranquillo", "medio", "impegnativo", "sportivo"],
                index=1, key="pl_athletic",
            )
            pl_direction = st.selectbox(
                t("planner.form.direction"),
                [t("planner.form.direction_free"), "Nord", "Est", "Sud", "Ovest"],
                index=0, key="pl_direction",
                help=t("planner.form.direction_help"),
            )

        # Punto di arrivo — solo per point_to_point
        if pl_route_type == "point_to_point":
            st.markdown(t("planner.end_label"))
            col_e1, col_e2, col_e3 = st.columns([2, 1, 1])
            with col_e1:
                _enc, _ebc = st.columns([3, 1])
                with _enc:
                    pl_end_name = st.text_input(
                        t("planner.form.end_name"), key="pl_ename",
                        placeholder=t("planner.form.end_name_placeholder"),
                    )
                with _ebc:
                    st.write("")  # allinea verticalmente con il label dell'input
                    geo_end_btn = st.button(
                        "📍", key="btn_geo_end",
                        help=t("planner.geo_btn_help"),
                        use_container_width=True,
                    )
                # Geocoding QUI: aggiorna session_state prima che col_e2/col_e3
                # leggano i valori — stesso pattern del punto di partenza sopra.
                if geo_end_btn:
                    _gname_end = (st.session_state.get("pl_ename") or "").strip()
                    if _gname_end:
                        from geocoding_agent import geocode_place as _gp
                        with st.spinner(f"Geocoding «{_gname_end}»…"):
                            _gresult_end = _gp(_gname_end, region=None)
                        if _gresult_end:
                            st.session_state["pl_elat"] = _gresult_end[0]
                            st.session_state["pl_elon"] = _gresult_end[1]
                        else:
                            st.warning(f"«{_gname_end}» {t('planner.geo_not_found')}")
            # value=None esplicito (a differenza di Start, che ha sempre un
            # default reale): qui l'assenza di coordinate deve restare uno
            # stato visibile — campo vuoto, non un numero plausibile mai
            # aggiornato (era esattamente la causa del bug "finisce sempre ad
            # Ancona"). Il valore digitato/geocodificato resta comunque in
            # session_state tra un rerun e l'altro, come per tutti gli altri
            # widget con key= di questo form.
            with col_e2:
                pl_end_lat = st.number_input(
                    t("planner.form.end_lat"), value=None, format="%.7f", key="pl_elat",
                )
            with col_e3:
                pl_end_lon = st.number_input(
                    t("planner.form.end_lon"), value=None, format="%.7f", key="pl_elon",
                )
        else:
            pl_end_name, pl_end_lat, pl_end_lon = None, None, None

        pl_user_wps = st.text_area(
            t("planner.form.waypoints"),
            placeholder=t("planner.form.waypoints_placeholder"),
            height=120,
            key="pl_wps",
            help=t("planner.form.waypoints_help"),
        )

        # SS%/SP% rimossi dalla UI: dipendono da osm_enricher.py, mai collegato
        # alla pipeline live (vedi models.py) — mostrare un controllo che non ha
        # alcun effetto sarebbe fuorviante. Restano come default nel modello dati.
        pl_elevation_pref = _elev_pref_selectbox("none", key="pl_elevpref")

        pl_avoid_roads = st.text_input(
            t("planner.form.avoid_roads"), value="", key="pl_avoid",
        )
        pl_free_text = st.text_area(
            t("planner.form.free_text"),
            placeholder=t("planner.form.free_text_placeholder"),
            height=70,
            key="pl_free",
        )

        def _parse_pl_waypoint_line(raw: str) -> UserWaypointInput:
            """
            '!' finale (anche con spazi prima) marca il waypoint come obbligatorio.
            Una coordinata lat,lon grezza è SEMPRE mandatory, con o senza '!':
            chi scrive coordinate precise sta indicando un punto esatto del
            percorso, non un riferimento di zona come farebbe con un nome di
            località (che resta soft di default, invariato — richiede '!'
            esplicito). Lo stesso '!' su una coordinata resta valido ma
            ridondante, nessun comportamento diverso.
            """
            s = raw.strip()
            mandatory = s.endswith("!")
            if mandatory:
                s = s[:-1].rstrip()
            if _COORD_RE.match(s):
                mandatory = True
            return UserWaypointInput(name=s, mandatory=mandatory)

        def _pl_build_request() -> RouteRequest:
            raw_wps = [_parse_pl_waypoint_line(w) for w in pl_user_wps.splitlines() if w.strip()]
            end_val = None
            if pl_route_type == "point_to_point" and pl_end_name and pl_end_lat and pl_end_lon:
                end_val = {"name": pl_end_name, "lat": pl_end_lat, "lon": pl_end_lon}
            return RouteRequest(
                start=StartPoint(name=pl_start_name, lat=pl_start_lat, lon=pl_start_lon),
                end=end_val,
                route_type=pl_route_type,
                target_km=-1.0 if pl_km_unconstrained else float(pl_target_km),
                distance_tolerance_km=5.0,
                user_waypoints=raw_wps,
                scenery_theme=pl_scenery,
                athletic_theme=pl_athletic,
                geographic_direction=None if pl_direction == t("planner.form.direction_free") else pl_direction,
                elevation_preference=pl_elevation_pref,
                avoid_named_roads=_parse_csv(pl_avoid_roads),
                free_text=pl_free_text.strip(),
            )

        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            # Un solo bottone per prima generazione e rigenerazione dopo
            # modifiche — "Pianifica" e "Rigenera" eseguivano lo stesso
            # branch identico (_pl_build_request → _run_planner_generation),
            # nessuna logica reale da distinguere.
            plan_btn = st.button(t("planner.btn_plan"), type="primary", key="btn_pl_plan")
        with col_btn2:
            accept_btn = st.button(
                t("planner.btn_accept"),
                key="btn_pl_accept",
                disabled="pl_result" not in st.session_state,
                type="secondary",
            )

        # Naming UI — shown after accept_btn clicked, before final save.
        # Se una route è stata caricata dal Planner (Utility → Gestione file),
        # offre anche "Aggiorna [nome]" accanto a "Salva come nuova route".
        _pl_loaded_name = st.session_state.get("pl_loaded_route_name")
        if st.session_state.get("pl_naming_active") and "pl_result" in st.session_state:
            st.divider()
            st.markdown(t("planner.naming_header"))
            suggested = st.session_state.get("pl_name_suggestion", "nuova_route")
            pl_route_name_final = st.text_input(
                t("planner.naming_label"),
                value=_pl_loaded_name or suggested,
                key="pl_route_name_final",
                help=t("planner.naming_help"),
            )
            update_btn = False
            if _pl_loaded_name:
                col_sv0, col_sv1, col_sv2 = st.columns(3)
                with col_sv0:
                    update_btn = st.button(
                        t("planner.btn_update").format(name=_pl_loaded_name),
                        type="primary", key="btn_pl_update",
                    )
                with col_sv1:
                    confirm_save_btn = st.button(t("planner.btn_save_new"), key="btn_pl_confirm_save")
                with col_sv2:
                    cancel_naming_btn = st.button(t("planner.btn_cancel"), key="btn_pl_cancel_naming")
            else:
                col_sv1, col_sv2 = st.columns(2)
                with col_sv1:
                    confirm_save_btn = st.button(
                        t("planner.btn_save_new"), type="primary", key="btn_pl_confirm_save"
                    )
                with col_sv2:
                    cancel_naming_btn = st.button(t("planner.btn_cancel"), key="btn_pl_cancel_naming")
        else:
            pl_route_name_final = ""
            confirm_save_btn = False
            cancel_naming_btn = False
            update_btn = False

        # I messaggi di esito (successo/errore) e l'avviso di conferma con i
        # conteggi vanno qui, subito sotto i bottoni che li generano — non in
        # fondo alla pagina dopo l'intera colonna mappa/risultati (dov'erano
        # prima: con pl_result popolato, col_map è lunga, quindi "in fondo"
        # significava fuori dallo schermo senza scroll).

        # ── "Salva come nuova route" — nome diverso obbligatorio, mai sovrascrive
        # una route esistente silenziosamente (prima non c'era questo controllo:
        # riusare un nome esistente qui equivaleva a un "Aggiorna" senza avviso).
        if confirm_save_btn and "pl_result" in st.session_state:
            res = st.session_state["pl_result"]
            slug = re.sub(r"[^\w\-]", "_", pl_route_name_final.strip()) or "route"
            if slug in _load_saved_routes():
                st.error(t("planner.save_new_name_conflict").format(name=slug))
            else:
                try:
                    saved_path = _save_planned_route(
                        route_name=slug,
                        request=res["request"],
                        ordered_wps=res["ordered_wps"],
                        system_prompt=res["system_prompt"],
                        user_prompt=res["user_prompt"],
                        warnings=res["warnings"],
                        search_queries=res.get("search_queries", []),
                        route_narrative=res.get("route_narrative", ""),
                    )
                    st.session_state.pop("pl_naming_active", None)
                    st.session_state.pop("pl_name_suggestion", None)
                    # La route appena salvata diventa quella "aperta" (navigazione
                    # documento aperto) — non più azzerata: era corretto azzerarla
                    # prima che esistesse questo concetto, ora lascerebbe Builder
                    # senza una route disponibile subito dopo averla appena creata.
                    st.session_state["pl_loaded_route_name"] = slug
                    st.session_state.pop("pl_dirty", None)
                    st.success(f"{t('planner.route_saved')} `{saved_path}`")
                    st.session_state[f"pl_saved_{slug}"] = True
                except Exception as exc:
                    st.error(f"Errore salvataggio: {exc}")
            # Senza rerun qui, i tab eseguiti PRIMA di Planner nello script
            # (File, che ora è il primo tab) restano con lo snapshot di
            # session_state di inizio-run e non riflettono subito la route
            # appena aperta/salvata — scoperto testando dal vivo il ciclo
            # Nuovo→Salva→Builder. Stesso pattern già in uso per "Aggiorna"
            # (vedi sotto), qui mancava.
            st.rerun()

        # ── "Aggiorna [nome]" — sovrascrive la route caricata. Se ha già dati
        # collegati (run Builder/analisi/feedback), richiede conferma esplicita
        # mostrando cosa verrebbe cancellato davvero da disco prima di procedere.
        if update_btn and "pl_result" in st.session_state and _pl_loaded_name:
            _existing_for_update = _load_saved_routes().get(_pl_loaded_name, {})
            _counts_check = _count_route_artifacts(_existing_for_update)
            if _counts_check["builder_runs"] or _counts_check["ride_analyses"] or _counts_check["feedbacks"]:
                st.session_state["_pl_update_confirm_pending"] = _pl_loaded_name
            else:
                # Niente da perdere: aggiorna direttamente, senza interrompere con un avviso vuoto.
                try:
                    res = st.session_state["pl_result"]
                    saved_path = _save_planned_route(
                        route_name=_pl_loaded_name,
                        request=res["request"],
                        ordered_wps=res["ordered_wps"],
                        system_prompt=res["system_prompt"],
                        user_prompt=res["user_prompt"],
                        warnings=res["warnings"],
                        search_queries=res.get("search_queries", []),
                        route_narrative=res.get("route_narrative", ""),
                    )
                    st.session_state.pop("pl_naming_active", None)
                    st.session_state.pop("pl_name_suggestion", None)
                    # Resta la route aperta — vedi commento nel ramo "Salva come nuova".
                    st.session_state["pl_loaded_route_name"] = _pl_loaded_name
                    st.session_state.pop("pl_dirty", None)
                    st.success(f"{t('planner.route_updated')} `{saved_path}`")
                    st.session_state[f"pl_saved_{_pl_loaded_name}"] = True
                except Exception as exc:
                    st.error(f"Errore aggiornamento: {exc}")
            st.rerun()

        _pl_update_pending = st.session_state.get("_pl_update_confirm_pending")
        if _pl_update_pending and "pl_result" in st.session_state:
            _existing_for_update = _load_saved_routes().get(_pl_update_pending, {})
            _counts = _count_route_artifacts(_existing_for_update)
            st.warning(
                t("planner.update_confirm_msg").format(
                    name=_pl_update_pending,
                    builder=_counts["builder_runs"],
                    ride=_counts["ride_analyses"],
                    feedback=_counts["feedbacks"],
                    files=_counts["gpx_file_count"] + len(_counts["report_files"]),
                )
            )
            col_upc1, col_upc2 = st.columns(2)
            with col_upc1:
                if st.button(t("debug.confirm_delete_btn"), key="btn_pl_update_yes", type="primary"):
                    try:
                        _delete_route_artifacts(_existing_for_update)
                        # I candidati Builder in sessione referenziano i GPX appena
                        # cancellati sopra — senza questo pop, il tab Builder
                        # ri-renderizzerebbe il vecchio bld_result (stesso route_name,
                        # nessun controllo di freschezza) puntando a file non più
                        # su disco.
                        st.session_state.pop("bld_result", None)
                        res = st.session_state["pl_result"]
                        saved_path = _save_planned_route(
                            route_name=_pl_update_pending,
                            request=res["request"],
                            ordered_wps=res["ordered_wps"],
                            system_prompt=res["system_prompt"],
                            user_prompt=res["user_prompt"],
                            warnings=res["warnings"],
                            search_queries=res.get("search_queries", []),
                            route_narrative=res.get("route_narrative", ""),
                        )
                        st.session_state.pop("_pl_update_confirm_pending", None)
                        st.session_state.pop("pl_naming_active", None)
                        st.session_state.pop("pl_name_suggestion", None)
                        # Resta la route aperta — vedi commento nel ramo "Salva come nuova".
                        st.session_state["pl_loaded_route_name"] = _pl_update_pending
                        st.session_state.pop("pl_dirty", None)
                        st.success(f"{t('planner.route_updated')} `{saved_path}`")
                        st.session_state[f"pl_saved_{_pl_update_pending}"] = True
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Errore aggiornamento: {exc}")
            with col_upc2:
                if st.button(t("debug.cancel_delete_btn"), key="btn_pl_update_no"):
                    st.session_state.pop("_pl_update_confirm_pending", None)
                    st.rerun()

    # ── Esecuzione pianificazione ─────────────────────────────────────────────
    def _run_planner_generation(
        request_to_use: RouteRequest,
        memory: dict,
        geocoded_wps: list[dict] | None = None,
    ) -> None:
        """
        Fase 1 (geocodifica + check preliminare) + Fase 2 (ricerca web + Claude)
        in un unico st.status() "parlante". Se geocoded_wps è None, la
        geocodifica viene eseguita qui come primo passo (caso comune, nessuna
        pausa se tutto va a buon fine). Se invece arriva già calcolata da un
        gate precedente (utente ha cliccato "Continua comunque" dopo un
        fallimento parziale), viene riusata senza rifarla.
        """
        with st.status(t("planner.status_loading"), expanded=True) as _pl_status:
            _pl_log = _TalkingLog(st.empty())
            try:
                if geocoded_wps is None:
                    _pl_log.step(t("planner.status_geocoding"))
                    geocoded_wps = _geocode_user_waypoints(
                        request_to_use.user_waypoints,
                        region=f"{request_to_use.start.name}, Italia",
                        start_coords=(request_to_use.start.lat, request_to_use.start.lon),
                        free_text=request_to_use.free_text,
                    )
                    failed_wps = [w for w in geocoded_wps if w.get("geocoding_failed")]
                    if failed_wps:
                        gate_label = t("planner.geocode_gate_failed").format(n=len(failed_wps))
                        _pl_log.finish(gate_label)
                        _pl_status.update(label=gate_label, state="complete", expanded=True)
                        st.session_state["pl_geocode_gate"] = {
                            "request": request_to_use,
                            "memory": memory,
                            "geocoded": geocoded_wps,
                            "failed_names": [w["name"] for w in failed_wps],
                        }
                        return

                sp, up = build_raw_route_prompt(request_to_use, geocoded_wps, memory)

                _search_count: list[int] = [0]

                def _on_planner_event(kind: str, detail: str = "") -> None:
                    try:
                        if kind == "search":
                            _search_count[0] += 1
                            short = detail[:80] + "…" if len(detail) > 80 else detail
                            _pl_log.step(f"{t('planner.status_search')} {short}")
                        elif kind == "results":
                            n = _search_count[0]
                            word = t("planner.status_results_1") if n == 1 else t("planner.status_results_n")
                            _pl_log.step(f"📍 Trovati risultati ({n} {word}), analizzo...")
                        elif kind == "generating":
                            _pl_log.step(t("planner.status_ordering"))
                    except Exception:
                        pass

                _pl_log.step(t("planner.status_web"))
                ordered_wps, warnings, search_queries, route_narrative = generate_raw_route(
                    request_to_use, memory, on_event=_on_planner_event
                )

                st.session_state["pl_result"] = {
                    "request":         request_to_use,
                    "ordered_wps":     [wp.model_dump() for wp in ordered_wps],
                    "system_prompt":   sp,
                    "user_prompt":     up,
                    "warnings":        warnings,
                    "search_queries":  search_queries,
                    "route_narrative": route_narrative,
                }
                # Una generazione fresca (Pianifica o Rigenera) è per
                # definizione non ancora salvata — usato da "Nuovo" (tab
                # File) per decidere se avvisare prima di scartarla.
                st.session_state["pl_dirty"] = True
                _pl_log.finish(t("planner.status_done"))
                _pl_status.update(label=t("planner.status_done"), state="complete", expanded=False)
            except Exception as exc:
                _pl_log.finish(t("planner.status_error"))
                _pl_status.update(label=t("planner.status_error"), state="error")
                st.error(f"Errore Planner: {exc}")

    if plan_btn:
        # Safety net: nome arrivo scritto ma coordinate mai geocodificate (né
        # col bottone 📍 né a mano) — bloccare qui, non lasciar passare valori
        # vuoti/None fino a _pl_build_request come se fosse un end valido.
        if pl_route_type == "point_to_point" and (pl_end_name or "").strip() and (pl_end_lat is None or pl_end_lon is None):
            st.error(t("planner.end_not_geocoded"))
        else:
            request_to_use = _pl_build_request()
            st.session_state["pl_request"] = request_to_use
            st.session_state.pop("pl_geocode_gate", None)
            _run_planner_generation(request_to_use, load_user_memory())

    # ── Gate di geocodifica — mostrato se l'ultimo check ha trovato waypoint
    # non geocodificati; resta finché l'utente non sceglie come procedere ────
    _pl_gate = st.session_state.get("pl_geocode_gate")
    if _pl_gate:
        st.warning(t("planner.geocode_gate_warning").format(names=", ".join(_pl_gate["failed_names"])))
        st.caption(t("planner.geocode_gate_hint"))
        col_gate1, col_gate2 = st.columns(2)
        with col_gate1:
            if st.button(t("planner.btn_continue_anyway"), key="btn_pl_continue_anyway", type="primary"):
                _gate_data = st.session_state.pop("pl_geocode_gate")
                _run_planner_generation(
                    _gate_data["request"], _gate_data["memory"], geocoded_wps=_gate_data["geocoded"],
                )
        with col_gate2:
            if st.button(t("planner.btn_cancel"), key="btn_pl_gate_cancel"):
                st.session_state.pop("pl_geocode_gate", None)
                st.rerun()

    # ── Accettazione — avvia il flusso di naming ─────────────────────────────
    if accept_btn and "pl_result" in st.session_state:
        res = st.session_state["pl_result"]
        # Route caricata (Parte 3): il nome è già noto, non serve suggerirne uno
        # nuovo — il text_input sopra usa già pl_loaded_route_name come default.
        if "pl_name_suggestion" not in st.session_state and not st.session_state.get("pl_loaded_route_name"):
            narrative = res.get("route_narrative", "")
            with st.spinner(t("planner.naming_spinner")):
                suggestion = _generate_route_slug(narrative)
            st.session_state["pl_name_suggestion"] = suggestion
        st.session_state["pl_naming_active"] = True
        st.rerun()

    if cancel_naming_btn:
        st.session_state.pop("pl_naming_active", None)
        st.session_state.pop("pl_name_suggestion", None)
        st.rerun()

    # ── Anteprima mappa (colonna destra) ─────────────────────────────────────
    with col_map:
        if "pl_result" in st.session_state:
            res = st.session_state["pl_result"]
            ordered_wps = res["ordered_wps"]
            warnings    = res["warnings"]

            # Narrativa del percorso — mostrata per prima
            narrative = res.get("route_narrative", "")
            if narrative:
                st.markdown(t("planner.result_narrative_header"))
                st.info(narrative)
                st.caption(t("planner.result_narrative_caption"))

            # Avvisi geocoding e deduplicazione
            for w in warnings:
                st.warning(w)

            # Ricerche web effettuate dal Planner
            sq = res.get("search_queries", [])
            if sq:
                with st.expander(f"{t('planner.result_web_expander')} ({len(sq)})", expanded=True):
                    for i, q in enumerate(sq, 1):
                        st.markdown(f"**{i}.** `{q}`")

            # Tabella waypoint (con colonna Rationale per waypoint planner)
            wp_rows = [
                {
                    t("planner.col_ord"): wp["order"],
                    t("planner.col_role"): wp["role"],
                    t("planner.col_name"): wp["name"],
                    t("planner.col_source"): wp["source"],
                    t("planner.col_constraint"): (
                        t("planner.badge_mandatory") if _wp_is_forced(wp) else t("planner.badge_soft")
                    ),
                    t("planner.col_lat"): f"{wp['lat']:.5f}" if wp.get("lat") else "—",
                    t("planner.col_lon"): f"{wp['lon']:.5f}" if wp.get("lon") else "—",
                    t("planner.col_rationale"): wp.get("rationale") or "",
                }
                for wp in ordered_wps
            ]
            st.dataframe(wp_rows, use_container_width=True, hide_index=True)

            # Stima distanza rapida
            est_km = _estimate_route_km(ordered_wps)
            target_km = res["request"].target_km
            tol_km = res["request"].distance_tolerance_km
            if est_km > 0:
                if target_km <= 0:
                    st.markdown(t("planner.result_est_km_free").format(est=est_km))
                else:
                    st.markdown(
                        t("planner.result_est_km").format(est=est_km, target=target_km)
                    )
                    if abs(est_km - target_km) > 2 * tol_km:
                        st.warning(
                            t("planner.result_est_warning").format(
                                est=est_km, target=target_km, tol=tol_km
                            )
                        )

            # Mappa anteprima
            start = res["request"].start
            m = _build_preview_map(ordered_wps, start.lat, start.lon)
            st_folium(m, width=None, height=460, key="pl_preview_map", use_container_width=True)
            st.caption(t("planner.result_map_caption"))

            # Espandi prompts
            with st.expander(t("planner.result_prompt_expander"), expanded=False):
                st.markdown(t("planner.result_system_prompt"))
                st.code(res["system_prompt"], language="text")
                st.markdown(t("planner.result_user_prompt"))
                st.code(res["user_prompt"], language="text")
        else:
            cur_lat = st.session_state.get("pl_slat", 43.7136520)
            cur_lon = st.session_state.get("pl_slon", 13.2278056)
            m_init = folium.Map(location=[cur_lat, cur_lon], zoom_start=11, scrollWheelZoom=False)
            folium.Marker(
                [cur_lat, cur_lon],
                popup=st.session_state.get("pl_sname", "Partenza"),
                icon=folium.Icon(color="green", icon="play"),
            ).add_to(m_init)
            st_folium(m_init, width=None, height=460, key="pl_init_map", use_container_width=True)
            st.caption(t("planner.result_map_init_caption"))


# ─── Tab: Builder ─────────────────────────────────────────────────────────────
with tab_builder:
    st.subheader(t("builder.subheader"))
    st.caption(t("builder.caption"))
    if _open_route_name():
        st.success(t("builder.open_route_banner").format(name=_open_route_name()))

    # Builder lavora sempre e solo sulla route corrente aperta (navigazione
    # "documento aperto") — niente più tendina di riselezione propria, che
    # duplicherebbe lo stato già tracciato da _open_route_name(). Per
    # costruzione richiede sempre una route salvata, a differenza di Ride
    # Analysis/Post Ride/GPX Optimizer (restati intenzionalmente non
    # vincolati — vedi discussione di design, upload manuale/esterno da
    # preservare per quei tre).
    saved_routes_b = _load_saved_routes()
    bld_sel = _open_route_name()

    if bld_sel and bld_sel not in saved_routes_b:
        # Route aperta ma non più trovata su disco (rinominata/eliminata da
        # un'altra sessione/tab dopo l'apertura) — non un caso normale, ma
        # va segnalato invece di fallire silenziosamente sul dict lookup.
        st.warning(t("builder.stale_open_route").format(name=bld_sel))
        bld_sel = None

    if not bld_sel:
        _render_no_route_placeholder(saved_routes_b)
    else:
        rd_b = saved_routes_b[bld_sel]
        req_b = rd_b.get("request", {})
        wps_b = rd_b.get("ordered_waypoints", [])

        # ── Riepilogo readonly ────────────────────────────────────────────
        with st.expander(t("builder.expander_summary"), expanded=True):
            narrative_b = rd_b.get("route_narrative", "")
            if narrative_b:
                st.info(narrative_b)
            col_rb1, col_rb2, col_rb3 = st.columns(3)
            _target_km_b_disp = req_b.get("target_km", "?")
            col_rb1.metric(
                t("builder.metric_target"),
                t("builder.metric_target_free") if _target_km_b_disp in (-1, -1.0)
                else f"{_target_km_b_disp} km",
            )
            col_rb2.metric(t("builder.metric_type"), req_b.get("route_type", "—"))
            col_rb3.metric(t("builder.metric_waypoints"), len(wps_b))
            st.caption(
                f"Tema: **{req_b.get('scenery_theme','—')}** · "
                f"Atletico: **{req_b.get('athletic_theme','—')}** · "
                f"Creata: {rd_b.get('created_at','—')}"
            )
            est_km_b = _estimate_route_km(wps_b)
            if est_km_b > 0:
                st.caption(t("builder.est_km").format(est=est_km_b))

        # ── Campi specifici Builder ───────────────────────────────────────
        col_bld1, col_bld2 = st.columns(2)
        with col_bld1:
            bld_profiles = st.multiselect(
                t("builder.profiles_label"),
                ["ebike_asphalt_safe", "ebike_gravel_easy", "ebike_scenic",
                 "roadbike_fast", "trekking", "gravel", "fastbike"],
                default=["ebike_asphalt_safe", "ebike_gravel_easy", "ebike_scenic"],
                key="bld_profiles",
                help=t("builder.profiles_help"),
            )
        with col_bld2:
            # SS%/SP% rimossi: non hanno alcun effetto oggi (vedi models.py).
            # Il dislivello non è più un tetto esatto — la preferenza qui
            # riprende quella già scelta nel Planner, modificabile anche qui.
            bld_elevation_pref = _elev_pref_selectbox(
                req_b.get("elevation_preference", "none"), key="bld_elevpref",
            )

        profiles_valid = len(bld_profiles) == 3
        if not profiles_valid:
            st.warning(t("builder.warn_profiles"))

        bld_gen_btn = st.button(
            t("builder.btn_generate"),
            type="primary",
            key="btn_bld_gen",
            disabled=not profiles_valid or not wps_b,
        )

        # ── Esecuzione Builder ────────────────────────────────────────────
        if bld_gen_btn and profiles_valid and wps_b:
            import traceback as _tb
            import httpx as _httpx

            # Health-check BRouter prima di lanciare tutto
            _brouter_ok = False
            try:
                import os as _os
                _brouter_base = _os.environ.get("BROUTER_URL", "http://localhost:17777")
                _brouter_endpoint = _brouter_base.rstrip("/") + "/brouter"
                _hc = _httpx.get(_brouter_endpoint, timeout=3.0,
                                 params={"lonlats": "13.2278,43.7136|13.2279,43.7137",
                                         "profile": "trekking", "alternativeidx": 0, "format": "gpx"})
                _brouter_ok = _hc.status_code == 200
            except Exception:
                _brouter_ok = False

            if not _brouter_ok:
                _br_url = __import__("os").environ.get("BROUTER_URL", "localhost:17777")
                st.error(t("builder.brouter_error").format(url=_br_url))
            else:
                # ── Verifica / download tile OSM per la zona di partenza ──────
                _tile_lat = wps_b[0].get("lat", 43.7136)
                _tile_lon = wps_b[0].get("lon", 13.2278)
                _tile_slot = st.empty()
                with _tile_slot.container():
                    _tile_prog = st.progress(0.0, text=t("builder.tile_progress"))
                    def _on_tile_progress(frac: float, _p=_tile_prog):
                        _p.progress(frac, text=f"{t('builder.tile_download')} {frac*100:.0f}%")
                    _tile_ok, _tile_msg = ensure_tile(
                        _tile_lat, _tile_lon, progress_cb=_on_tile_progress
                    )
                if _tile_ok:
                    _tile_slot.empty()
                else:
                    _tile_slot.empty()
                    st.error(f"{t('builder.tile_error')} {_tile_msg}")
                    st.stop()

                merged_req_b = {
                    **req_b,
                    "elevation_preference": bld_elevation_pref,
                }
                # Build 3 strategy dicts from Planner waypoints (already geocoded)
                strategies_b = []
                for i, profile in enumerate(bld_profiles):
                    label = ["A", "B", "C"][i]
                    waypoints_b = [
                        {
                            "role": wp["role"],
                            "name": wp["name"],
                            "lat": wp.get("lat"),
                            "lon": wp.get("lon"),
                            "needs_geocoding": False,
                            "traversal": False,
                            "source": wp.get("source", "user"),
                            "mandatory": wp.get("mandatory", False),
                        }
                        for wp in wps_b
                    ]
                    strategies_b.append({
                        "name": f"Variante {label} ({profile})",
                        "route_type": req_b.get("route_type", "loop"),
                        "profile": profile,
                        "requires_geocoding": False,
                        "estimated_km": req_b.get("target_km", 60.0),
                        "rationale": f"Profilo {profile} sui waypoint del Planner",
                        "waypoints": waypoints_b,
                        "free_text_overrides": [],
                    })

                with st.status(t("builder.status_generating"), expanded=True) as _bld_status:
                    _bld_log = _TalkingLog(st.empty())

                    def _on_bld_progress(_msg: str, _log=_bld_log) -> None:
                        _log.step(_msg)

                    try:
                        memory_b = load_user_memory()
                        _zona_b = (merged_req_b.get("start") or {}).get("name")
                        _obstacles_b = db.get_active_obstacles(zona=_zona_b)
                        merged_req_b = merge_memory_with_request(merged_req_b, memory_b, obstacles=_obstacles_b)

                        _bld_status.update(label=t("builder.status_brouter"))
                        all_candidates_b = generate_candidates(
                            strategies_b, request=None, route_name=bld_sel,
                            on_progress=_on_bld_progress,
                        )
                        ok_candidates_b = [c for c in all_candidates_b if c["status"] in ("ok", "retried")]
                        failed_b = [c for c in all_candidates_b if c["status"] == "failed"]

                        if failed_b:
                            for fc in failed_b:
                                st.warning(
                                    f"{t('builder.warn_candidate_failed').format(id=fc['id'], profile=fc['profile'])} "
                                    f"`{fc.get('failure_reason', 'errore sconosciuto')}`"
                                )

                        if not ok_candidates_b:
                            _bld_log.finish(t("builder.status_all_failed"))
                            _bld_status.update(label=t("builder.status_all_failed"), state="error")
                            st.error(t("builder.err_all_failed"))
                        else:
                            _bld_log.step(t("builder.status_scoring"))
                            _bld_status.update(label=t("builder.status_scoring"))
                            scored_b = [score_candidate(c["analysis"], merged_req_b) for c in ok_candidates_b]

                            _bld_log.step(t("builder.status_decision"))
                            _bld_status.update(label=t("builder.status_decision"))
                            report_b = run_decision(ok_candidates_b, scored_b, merged_req_b)

                            st.session_state["bld_result"] = {
                                "route_name": bld_sel,
                                "request": merged_req_b,
                                "all_candidates": all_candidates_b,
                                "candidates": ok_candidates_b,
                                "scored": scored_b,
                                "decision": report_b.model_dump(),
                            }
                            _bld_log.finish(t("builder.status_done").format(n=len(ok_candidates_b)))
                            _bld_status.update(
                                label=t("builder.status_done").format(n=len(ok_candidates_b)),
                                state="complete", expanded=False,
                            )

                            # Accoda un nuovo run in builder_results (lista, accumula
                            # nel tempo — redesign persistenza) invece di sovrascrivere
                            # l'unica run precedente.
                            try:
                                rd_b_updated = dict(rd_b)
                                _builder_runs = _normalize_builder_results(rd_b)
                                _builder_runs.append({
                                    "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                                    "candidates": [
                                        {
                                            "id": c["id"],
                                            "strategy_name": c["strategy_name"],
                                            "profile": c["profile"],
                                            "status": c["status"],
                                            "gpx_path": c.get("gpx_path"),
                                            "analysis": c.get("analysis"),
                                            "failure_reason": c.get("failure_reason"),
                                        }
                                        for c in all_candidates_b
                                    ],
                                    "scored": scored_b,
                                    "decision": report_b.model_dump(),
                                })
                                rd_b_updated["builder_results"] = _builder_runs
                                json_path = _PLANNED_DIR / f"{bld_sel}.json"
                                json_path.write_text(
                                    json.dumps(rd_b_updated, ensure_ascii=False, indent=2),
                                    encoding="utf-8",
                                )
                            except Exception:
                                pass

                    except Exception as exc:
                        _bld_log.finish(t("builder.err_builder"))
                        _bld_status.update(label=t("builder.err_builder"), state="error")
                        st.error(f"Errore Builder: {exc}")
                        with st.expander("Traceback completo", expanded=True):
                            st.code(_tb.format_exc(), language="text")

        # ── Risultati Builder ─────────────────────────────────────────────
        bld_res = st.session_state.get("bld_result")
        if bld_res and bld_res.get("route_name") == bld_sel:
            candidates_b  = bld_res["candidates"]      # only ok/retried
            all_cands_b   = bld_res.get("all_candidates", candidates_b)
            scored_b      = bld_res["scored"]
            decision_b    = bld_res["decision"]
            req_res_b     = bld_res["request"]

            target_km_b = req_res_b.get("target_km", 60.0) if isinstance(req_res_b, dict) else req_res_b.target_km
            start_b     = req_res_b.get("start") if isinstance(req_res_b, dict) else {}
            if isinstance(start_b, dict):
                start_lat_b = start_b.get("lat", 43.7136)
                start_lon_b = start_b.get("lon", 13.2278)
            else:
                start_lat_b, start_lon_b = 43.7136, 13.2278

            winner_id_b = decision_b.get("winner")

            # Fix 4 (+ fallback BRouter successivo): nota aggregata se uno o più
            # waypoint soft sono stati inclusi con aggancio grezzo via BRouter
            # invece che tramite Overpass, per un problema temporaneo del servizio
            # di geocoding — non sono più esclusi, solo di qualità inferiore.
            # Stessa lista per tutti i candidati (snap condiviso, Fix 1).
            _raw_fallback_b = (all_cands_b[0].get("soft_waypoint_issues", {}).get("raw_fallback")
                                if all_cands_b else [])
            if _raw_fallback_b:
                st.warning(
                    t("builder.snap_service_unavailable").format(
                        n=len(_raw_fallback_b), names=", ".join(_raw_fallback_b)
                    )
                )

            st.divider()
            st.subheader(t("builder.result_subheader"))

            # Table: scored candidates first, then failed ones
            pairs_b  = list(zip(candidates_b, scored_b))
            valid_b  = sorted([(c, s) for c, s in pairs_b if not s["discarded"]],
                              key=lambda x: x[1]["total_score"], reverse=True)
            invalid_b = [(c, s) for c, s in pairs_b if s["discarded"]]
            ordered_b = valid_b + invalid_b
            failed_cands_b = [c for c in all_cands_b if c["status"] == "failed"]

            target_unconstrained_b = target_km_b is None or float(target_km_b) <= 0

            rows_b = []
            for c, s in ordered_b:
                dist = c["analysis"]["distance_km"]
                is_w = c["id"] == winner_id_b
                indicator = "★" if is_w else ("✗" if s["discarded"] else "·")
                warn_parts_b = []
                if s.get("distance_warning"):
                    warn_parts_b.append(s["distance_warning"])
                oab_pct_b = c["analysis"].get("out_and_back_percent")
                if oab_pct_b is not None and oab_pct_b >= OUT_AND_BACK_WARN_THRESHOLD_PCT:
                    warn_parts_b.append(t("builder.out_and_back_warning").format(pct=oab_pct_b))
                if s["discarded"]:
                    stato = f"{t('builder.status_discarded')} — {s['discard_reason']}"
                elif warn_parts_b:
                    stato = "⚠ " + " · ".join(warn_parts_b)
                else:
                    stato = t("builder.status_valid")
                if target_unconstrained_b:
                    vs_target_disp = "—"
                else:
                    diff = dist - target_km_b
                    vs_target_disp = f"{'+' if diff >= 0 else ''}{diff:.1f}"
                rows_b.append({
                    "  ": indicator,
                    t("builder.col_id"): c["id"],
                    t("builder.col_name"): c["strategy_name"],
                    t("builder.col_profile"): c["profile"],
                    t("builder.col_km"): f"{dist:.1f}",
                    t("builder.col_vs_target"): vs_target_disp,
                    t("builder.col_elev"): f"{c['analysis']['elevation_gain_m']:.0f}",
                    t("builder.col_score"): f"{s['total_score']:.1f}",
                    t("builder.col_status"): stato,
                })
            for c in failed_cands_b:
                rows_b.append({
                    "  ": "✗",
                    t("builder.col_id"): c["id"],
                    t("builder.col_name"): c["strategy_name"],
                    t("builder.col_profile"): c["profile"],
                    t("builder.col_km"): "—",
                    t("builder.col_vs_target"): "—",
                    t("builder.col_elev"): "—",
                    t("builder.col_score"): "—",
                    t("builder.col_status"): f"{t('builder.status_error_brouter')} {c.get('failure_reason', '?')[:80]}",
                })

            if not rows_b:
                st.warning(t("builder.no_candidates"))
            else:
                st.dataframe(rows_b, use_container_width=True, hide_index=True)

            # Map — only when there are ok candidates
            if ordered_b:
                st.subheader(t("builder.explore_subheader"))
                view_opts_b = [t("builder.explore_all")] + [
                    f"{'★ ' if c['id'] == winner_id_b else ('✗ ' if s['discarded'] else '· ')}"
                    f"{c['id']} — {c['strategy_name']} ({c['analysis']['distance_km']:.1f} km · {s['total_score']:.0f}pt)"
                    for c, s in ordered_b
                ]
                sel_view_b = st.radio(
                    t("builder.explore_radio"), view_opts_b, horizontal=True, key="bld_explore_radio"
                )

                if sel_view_b == view_opts_b[0]:
                    try:
                        m_b = _build_multi_map(
                            candidates_b, scored_b, winner_id_b,
                            {"lat": start_lat_b, "lon": start_lon_b},
                        )
                    except FileNotFoundError:
                        # bld_result in sessione punta a GPX non più su disco
                        # (route aggiornata/rigenerata altrove) — non ancora
                        # coperto da un'invalidazione esplicita in questo punto.
                        st.warning(t("builder.stale_results_warning"))
                    else:
                        st_folium(m_b, width=None, height=520, key="bld_map_multi", use_container_width=True)
                else:
                    idx_b = view_opts_b.index(sel_view_b) - 1
                    c_b, s_b = ordered_b[idx_b]
                    if s_b["discarded"]:
                        st.error(f"✗ Candidato scartato: {s_b['discard_reason']}")
                    else:
                        col_bm1, col_bm2, col_bm3 = st.columns(3)
                        col_bm1.metric(t("builder.col_score"), f"{s_b['total_score']:.1f}/100")
                        col_bm2.metric(t("analizza.metric_distance"), f"{c_b['analysis']['distance_km']:.1f} km")
                        col_bm3.metric(
                            t("analizza.metric_elev_up"), f"{c_b['analysis']['elevation_gain_m']:.0f} m",
                            help=t("analizza.metric_elev_up_help"),
                        )
                        if s_b.get("distance_warning"):
                            st.warning(f"⚠ {s_b['distance_warning']}")
                        for _removed_name in c_b["analysis"].get("removed_waypoints", []):
                            st.info(t("builder.out_and_back_removed").format(name=_removed_name))
                        _oab_pct_single = c_b["analysis"].get("out_and_back_percent") or 0.0
                        if _oab_pct_single >= _OUT_AND_BACK_INFO_THRESHOLD_PCT:
                            for _attr in c_b["analysis"].get("out_and_back_attributions", []):
                                st.info(
                                    t("builder.out_and_back_attribution").format(
                                        name=_attr["waypoint_name"], km=_attr["overlap_km"]
                                    )
                                )
                    try:
                        m_b = _build_map(c_b["gpx_path"], start_lat_b, start_lon_b)
                        gpx_bytes_b = _gpx_bytes_with_name(c_b["gpx_path"], f"{bld_sel}_{c_b['id']}")
                    except FileNotFoundError:
                        st.warning(t("builder.stale_results_warning"))
                    else:
                        st_folium(m_b, width=None, height=520, key=f"bld_map_{c_b['id']}", use_container_width=True)
                        st.download_button(
                            label=f"{t('builder.btn_download')} {c_b['id']} {c_b['strategy_name']}",
                            data=gpx_bytes_b,
                            file_name=f"{bld_sel}_{c_b['id']}.gpx",
                            mime="application/gpx+xml",
                            key=f"bld_dl_{c_b['id']}",
                        )

                    # ── Analisi salite (Parte 2 — oggettiva) ────────────────
                    climbs_b = c_b["analysis"].get("climbs") or []
                    profile_b = c_b["analysis"].get("elevation_profile") or {}
                    st.markdown(f"**{t('climbs.header')}**")
                    fig_climb_b = _render_climb_profile_chart(profile_b)
                    if fig_climb_b is not None:
                        st.pyplot(fig_climb_b, use_container_width=True)
                    if climbs_b:
                        st.caption(t("climbs.main_climbs_caption").format(n=len(climbs_b)))
                        st.dataframe(
                            _climbs_table_rows(climbs_b, top_n=5),
                            use_container_width=True, hide_index=True,
                        )
                    else:
                        st.caption(t("climbs.no_climbs"))

            # Decision Agent
            st.divider()
            if winner_id_b:
                rationale_b = decision_b.get("rationale", "")
                if rationale_b:
                    st.markdown(f"{t('builder.decision_motivation')} {rationale_b}")
                winner_c_b = next((c for c in candidates_b if c["id"] == winner_id_b), None)
                winner_s_b = next((s for c, s in zip(candidates_b, scored_b) if c["id"] == winner_id_b), None)
                if winner_c_b:
                    st.success(
                        t("builder.winner_label").format(
                            id=winner_id_b,
                            name=winner_c_b["strategy_name"],
                            profile=winner_c_b["profile"],
                            km=winner_c_b["analysis"]["distance_km"],
                            elev=winner_c_b["analysis"]["elevation_gain_m"],
                        )
                        + (f"  ·  Score: {winner_s_b['total_score']:.1f}/100" if winner_s_b else "")
                    )
                    if winner_c_b.get("gpx_path"):
                        try:
                            winner_gpx = _gpx_bytes_with_name(
                                winner_c_b["gpx_path"], f"{bld_sel}_{winner_id_b}"
                            )
                        except FileNotFoundError:
                            st.warning(t("builder.stale_results_warning"))
                        else:
                            st.download_button(
                                label=f"{t('builder.winner_dl')} {winner_id_b} {winner_c_b['strategy_name']}",
                                data=winner_gpx,
                                file_name=f"{bld_sel}_{winner_id_b}.gpx",
                                mime="application/gpx+xml",
                                key="bld_dl_winner",
                            )
            else:
                q_b    = decision_b.get("question_for_user", "")
                opts_b = decision_b.get("options", [])
                rat_b  = decision_b.get("rationale", "")
                if rat_b:
                    st.warning(f"**Decision Agent:** {rat_b}")
                if q_b:
                    st.info(f"{t('builder.decision_choice')} {q_b}")
                if opts_b:
                    st.radio(t("builder.decision_option"), opts_b, key="bld_opt_radio")


# ─── GPX Optimizer — corpo condiviso ────────────────────────────────────────
# Due punti di accesso, stesso strumento sottostante (design concordato,
# Parte 5): il tab di primo livello (workspace, scorciatoia "usa il
# vincitore" se una route è aperta) e Utility (sempre upload manuale, per
# ottimizzare file esterni non legati a nessuna route — comportamento
# identico a quello storico). key_prefix tiene separati i widget key tra le
# due istanze, altrimenti Streamlit rifiuterebbe le key duplicate.
def _render_gpx_optimizer(key_prefix: str, open_route_name: str | None = None) -> None:
    st.info(t("optimizer.explainer"))

    _winner_key = f"_{key_prefix}_opt_use_winner"
    if open_route_name:
        _winner = _find_route_winner_gpx(open_route_name)
        if _winner:
            _wpath, _wid, _wprofile = _winner
            if st.button(
                t("optimizer.use_winner_btn").format(name=open_route_name, id=_wid, profile=_wprofile),
                key=f"{key_prefix}_opt_use_winner_btn",
            ):
                st.session_state[_winner_key] = str(_wpath)
                st.rerun()

    opt_uploaded = st.file_uploader(
        t("optimizer.uploader_label"), type=["gpx"], key=f"{key_prefix}_opt_uploader",
    )

    # L'upload manuale ha sempre precedenza sulla scorciatoia "usa il
    # vincitore" (se presenti entrambi nello stesso rerun, capita solo se
    # l'utente carica un file dopo aver già cliccato la scorciatoia).
    _source_bytes = None
    _source_name = None
    if opt_uploaded is not None:
        st.session_state.pop(_winner_key, None)
        _source_bytes = opt_uploaded.getvalue()
        _source_name = opt_uploaded.name
    elif st.session_state.get(_winner_key):
        _wpath = st.session_state[_winner_key]
        try:
            with open(_wpath, "rb") as f:
                _source_bytes = f.read()
            _source_name = Path(_wpath).name
            st.caption(t("optimizer.using_winner_caption").format(name=_source_name))
        except OSError:
            st.session_state.pop(_winner_key, None)

    if _source_bytes is not None:
        try:
            opt_gpx_parsed = gpxpy.parse(_source_bytes.decode("utf-8"))

            opt_can_proceed = True
            if is_already_optimized(opt_gpx_parsed):
                st.warning(t("optimizer.already_optimized"))
                opt_force = st.checkbox(t("optimizer.force_reoptimize"), key=f"{key_prefix}_opt_force_reopt")
                opt_can_proceed = opt_force

            opt_default_name = opt_gpx_parsed.name or Path(_source_name).stem
            _last_key = f"{key_prefix}_opt_last_file"
            _name_key = f"{key_prefix}_opt_route_name"
            if st.session_state.get(_last_key) != _source_name:
                st.session_state[_last_key] = _source_name
                st.session_state[_name_key] = opt_default_name
            opt_route_name = st.text_input(t("optimizer.route_name_label"), key=_name_key)

            if st.button(t("optimizer.btn_run"), key=f"{key_prefix}_opt_btn_run", disabled=not opt_can_proceed):
                with tempfile.NamedTemporaryFile(suffix=".gpx", delete=False, mode="wb") as tmp_in:
                    tmp_in.write(_source_bytes)
                    opt_in_path = tmp_in.name
                with tempfile.NamedTemporaryFile(suffix=".gpx", delete=False) as tmp_out:
                    opt_out_path = tmp_out.name

                try:
                    opt_stats = optimize_gpx(opt_in_path, opt_route_name, opt_out_path)

                    col_o1, col_o2, col_o3 = st.columns(3)
                    col_o1.metric(t("optimizer.metric_points_before"), opt_stats["points_before"])
                    col_o2.metric(
                        t("optimizer.metric_points_after"),
                        opt_stats["points_after"],
                        delta=f"-{opt_stats['reduction_pct']:.1f}%",
                    )
                    col_o3.metric(t("optimizer.metric_waypoints"), opt_stats["waypoints_added"])

                    with open(opt_out_path, "rb") as f:
                        opt_out_bytes = f.read()
                    st.download_button(
                        label=t("optimizer.btn_download"),
                        data=opt_out_bytes,
                        file_name=f"{opt_route_name}_edge840.gpx",
                        mime="application/gpx+xml",
                        key=f"{key_prefix}_opt_dl_btn",
                    )
                finally:
                    for _p in (opt_in_path, opt_out_path):
                        try: os.unlink(_p)
                        except OSError: pass
        except Exception as e:
            st.error(f"{t('optimizer.parse_error')} {e}")


# ─── Tab: GPX Optimizer ────────────────────────────────────────────────────────
with tab_optimizer:
    st.subheader(t("optimizer.subheader"))
    st.caption(t("optimizer.caption"))
    _render_gpx_optimizer("ws", open_route_name=_open_route_name())


# ─── Tab: 🔋 Analisi Giro ─────────────────────────────────────────────────────
with tab_ride:
    st.subheader(t("ride_analysis.subheader"))
    st.caption(t("ride_analysis.caption"))

    col_prof, col_main = st.columns([1, 2], gap="large")

    # ── Left column: profile management ──────────────────────────────────────
    with col_prof:
        st.subheader(t("ride_analysis.profile_header"))

        # Consume pending selection BEFORE the widget renders
        if "_ride_profile_pending_sel" in st.session_state:
            st.session_state["ride_profile_sel"] = st.session_state.pop("_ride_profile_pending_sel")

        _profiles = db.list_ride_profiles()
        _profile_names = [p["name"] for p in _profiles]
        _sel_options = [t("ride_analysis.profile_new")] + _profile_names

        _sel = st.selectbox(
            t("ride_analysis.profile_select"),
            _sel_options,
            key="ride_profile_sel",
        )
        _is_new_profile = _sel == t("ride_analysis.profile_new")
        _existing = (
            {} if _is_new_profile
            else next((p for p in _profiles if p["name"] == _sel), {})
        )

        # Form key includes selected profile to reset fields on profile change
        _form_key = f"ride_profile_form_{_sel}"
        with st.form(_form_key):
            _profile_name = st.text_input(
                t("ride_analysis.profile_name"),
                value=_existing.get("name", ""),
                placeholder=t("ride_analysis.profile_name_ph"),
            )

            st.markdown(f"**{t('ride_analysis.bike_header')}**")
            _bike_model = st.text_input(
                t("ride_analysis.bike_model"),
                value=_existing.get("bike_model", ""),
                placeholder=t("ride_analysis.bike_model_ph"),
            )

            _bike_codes = ["ebike", "muscolare", "gravel", "mtb", "road"]
            _bike_labels = [t(f"ride_analysis.bike_types.{c}") for c in _bike_codes]
            _cur_type = _existing.get("bike_type", "ebike")
            _cur_type_idx = (
                _bike_codes.index(_cur_type) if _cur_type in _bike_codes else 0
            )
            _bike_label_sel = st.selectbox(
                t("ride_analysis.bike_type"), _bike_labels, index=_cur_type_idx
            )
            _bike_type = _bike_codes[_bike_labels.index(_bike_label_sel)]
            _is_ebike_form = _bike_type == "ebike"

            _bike_weight = st.number_input(
                t("ride_analysis.bike_weight"),
                min_value=5.0, max_value=50.0,
                value=float(_existing.get("bike_weight_kg") or 12.0),
                step=0.5,
            )

            if _is_ebike_form:
                st.caption(t("ride_analysis.bike_ebike_only"))
                _wh = st.number_input(
                    t("ride_analysis.bike_wh"),
                    min_value=0, max_value=2000,
                    value=int(_existing.get("wh") or 500),
                )
                _battery_pct = st.number_input(
                    t("ride_analysis.bike_battery_pct"),
                    min_value=0, max_value=100,
                    value=int(_existing.get("battery_pct") or 100),
                )
                _min_battery_pct = st.number_input(
                    t("ride_analysis.bike_min_battery"),
                    min_value=0, max_value=80,
                    value=int(_existing.get("min_battery_pct") or 0),
                    help=t("ride_analysis.bike_min_battery_help"),
                )
                _style_codes = ["eco", "mixed", "comfort", "max"]
                _style_labels = [t(f"ride_analysis.riding_style_{c}") for c in _style_codes]
                _cur_style = _existing.get("riding_style") or "mixed"
                _cur_style_idx = (
                    _style_codes.index(_cur_style) if _cur_style in _style_codes else 1
                )
                _riding_style_label = st.selectbox(
                    t("ride_analysis.riding_style"),
                    _style_labels,
                    index=_cur_style_idx,
                    help=t("ride_analysis.riding_style_help"),
                )
                _riding_style = _style_codes[_style_labels.index(_riding_style_label)]
            else:
                _wh = None
                _battery_pct = None
                _min_battery_pct = None
                _riding_style = None

            st.markdown(f"**{t('ride_analysis.driver_header')}**")
            _driver_weight = st.number_input(
                t("ride_analysis.driver_weight"),
                min_value=30.0, max_value=200.0,
                value=float(_existing.get("driver_weight_kg") or 75.0),
                step=0.5,
            )
            _driver_age = st.number_input(
                t("ride_analysis.driver_age"),
                min_value=10, max_value=100,
                value=int(_existing.get("driver_age") or 35),
            )
            _sex_opts = ["M", "F", t("ride_analysis.sex_other")]
            _cur_sex = _existing.get("driver_sex", "M")
            _sex_idx = _sex_opts.index(_cur_sex) if _cur_sex in _sex_opts else 0
            _driver_sex = st.selectbox(
                t("ride_analysis.driver_sex"), _sex_opts, index=_sex_idx
            )
            _driver_fitness = st.slider(
                t("ride_analysis.driver_fitness"),
                1, 5,
                value=int(_existing.get("driver_fitness") or 3),
            )
            _driver_fcmax = st.number_input(
                t("ride_analysis.driver_fcmax"),
                min_value=0, max_value=250,
                value=int(_existing.get("driver_fcmax") or 0),
                help="0 = skip",
            )
            _driver_health = st.text_area(
                t("ride_analysis.driver_health"),
                value=_existing.get("driver_health_notes", ""),
                placeholder=t("ride_analysis.driver_health_ph"),
            )

            _csave, _cdel = st.columns(2)
            with _csave:
                _do_save = st.form_submit_button(t("ride_analysis.btn_save"), type="primary")
            with _cdel:
                _do_delete = st.form_submit_button(
                    t("ride_analysis.btn_delete"),
                    disabled=_is_new_profile,
                )

        if _do_save:
            if not _profile_name.strip():
                st.error(t("ride_analysis.name_required"))
            else:
                try:
                    db.save_ride_profile(
                        name=_profile_name.strip(),
                        bike_model=_bike_model or None,
                        bike_type=_bike_type,
                        wh=_wh,
                        battery_pct=_battery_pct,
                        min_battery_pct=_min_battery_pct,
                        bike_weight_kg=_bike_weight,
                        riding_style=_riding_style,
                        driver_weight_kg=_driver_weight,
                        driver_age=_driver_age,
                        driver_sex=_driver_sex,
                        driver_fitness=_driver_fitness,
                        driver_fcmax=int(_driver_fcmax) if _driver_fcmax else None,
                        driver_health_notes=_driver_health or None,
                    )
                    st.success(t("ride_analysis.saved_ok").format(name=_profile_name.strip()))
                    st.session_state["_ride_profile_pending_sel"] = _profile_name.strip()
                    st.rerun()
                except Exception as _e:
                    st.error(f"❌ {_e}")

        if _do_delete and not _is_new_profile and _existing:
            db.delete_ride_profile(_existing["id"])
            st.success(t("ride_analysis.deleted_ok"))
            st.rerun()

    # ── Right column: GPX upload + analysis ──────────────────────────────────
    with col_main:
        st.subheader(t("ride_analysis.gpx_header"))
        _gpx_file = st.file_uploader(
            t("ride_analysis.gpx_upload"),
            type=["gpx"],
            key="ride_gpx_upload",
        )

        _gpx_stats = None
        if _gpx_file:
            try:
                _gpx_bytes = _gpx_file.read()
                _gpx_stats = ride_analysis.analyze_gpx_bytes(_gpx_bytes)
                _gpx_stats["gpx_filename"] = Path(_gpx_file.name).stem
                st.subheader(t("ride_analysis.gpx_stats"))
                _gc1, _gc2, _gc3, _gc4 = st.columns(4)
                _gc1.metric(t("ride_analysis.gpx_dist"), f"{_gpx_stats['distance_km']} km")
                _gc2.metric(t("ride_analysis.gpx_elev_up"), f"{_gpx_stats['elevation_gain_m']:.0f} m")
                _gc3.metric(t("ride_analysis.gpx_elev_down"), f"{_gpx_stats['elevation_loss_m']:.0f} m")
                if _gpx_stats.get("max_elevation_m"):
                    _gc4.metric(t("ride_analysis.gpx_max_alt"), f"{_gpx_stats['max_elevation_m']:.0f} m")
            except Exception as _e:
                st.error(f"{t('ride_analysis.gpx_error')}: {_e}")

        # Optional route link — reads narrative from a saved Planner route.
        # Auto-detect: Builder GPX filenames follow {route_name}_{strategy}.gpx;
        # strip the last _X suffix to recover the Planner route name.
        _saved_routes = _load_saved_routes()
        if _gpx_file and st.session_state.get("_ride_last_gpx") != _gpx_file.name:
            st.session_state["_ride_last_gpx"] = _gpx_file.name
            _stem = Path(_gpx_file.name).stem       # e.g. "anello_senigallia_B"
            _base = re.sub(r"_[A-Z]$", "", _stem)   # strip single-letter strategy suffix
            if _base in _saved_routes:
                st.session_state["ride_route_sel"] = _base  # pre-select before widget renders

        _route_narrative = None
        _route_sel = None  # nome route collegata, None se GPX esterno non collegato
        if _saved_routes:
            _route_link_none = t("ride_analysis.route_link_none")
            _sel = st.selectbox(
                t("ride_analysis.route_link"),
                options=[_route_link_none] + sorted(_saved_routes.keys()),
                key="ride_route_sel",
            )
            if _sel != _route_link_none:
                _route_narrative = _saved_routes[_sel].get("route_narrative")
                _route_sel = _sel

        # Determine active profile (must be saved, not "new")
        _active_profile = None if _is_new_profile else (_existing or None)

        if not _gpx_stats:
            st.info(t("ride_analysis.need_gpx"))
        elif not _active_profile:
            st.info(t("ride_analysis.need_profile"))

        if _gpx_stats and _active_profile:
            if st.button(t("ride_analysis.btn_analyze"), type="primary", key="ride_btn_analyze"):
                with st.spinner(t("ride_analysis.analyzing")):
                    try:
                        _lang = active_lang()
                        _result = ride_analysis.run_analysis(_gpx_stats, _active_profile, _lang)
                        st.session_state["ride_result"] = _result
                        st.session_state["ride_result_gpx"] = _gpx_stats
                        st.session_state["ride_result_profile"] = _active_profile
                        st.session_state["ride_result_lang"] = _lang

                        # Solo per candidati collegati a una route pianificata — un
                        # GPX esterno non collegato resta volatile (solo session_state
                        # + download manuale), come deciso. Il report HTML va su disco
                        # (path referenziato, stesso pattern dei GPX del Builder — non
                        # contenuto inline nel JSON).
                        if _route_sel:
                            _html_hist = ride_analysis.render_html_report(
                                _result, _gpx_stats, _active_profile, _lang,
                                route_narrative=_route_narrative,
                            )
                            _RIDE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
                            _ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                            _report_path = _RIDE_REPORTS_DIR / f"{_route_sel}_{_ts}.html"
                            _report_path.write_text(_html_hist, encoding="utf-8")
                            _save_ride_analysis_to_route(_route_sel, {
                                "recorded_at": datetime.datetime.now().isoformat(timespec="seconds"),
                                "report_path": str(_report_path),
                                "gpx_filename": _gpx_stats.get("gpx_filename"),
                                "calories_kcal": _result.get("calories_kcal"),
                                "avg_hr_bpm": _result.get("avg_hr_bpm"),
                                "battery_pct_consumed": _result.get("battery_pct_consumed"),
                                "fatigue_index": _result.get("fatigue_index"),
                            })
                    except Exception as _e:
                        st.error(f"❌ {_e}")

        if "ride_result" in st.session_state:
            _r = st.session_state["ride_result"]
            _rg = st.session_state["ride_result_gpx"]
            _rp = st.session_state["ride_result_profile"]
            _rl = st.session_state.get("ride_result_lang", active_lang())

            st.subheader(t("ride_analysis.results_header"))
            _r_is_ebike = (_rp.get("bike_type") or "").lower() == "ebike"

            # Battery row (ebike only)
            if _r_is_ebike:
                _rb1, _rb2, _rb3 = st.columns(3)
                _batt_v = _r.get("battery_pct_consumed")
                _rng_v = _r.get("range_remaining_km")
                _assist_v = _r.get("estimated_assistance_level")
                _rb1.metric(
                    t("ride_analysis.battery_consumed"),
                    f"{_batt_v:.0f}%" if _batt_v is not None else t("ride_analysis.ebike_na"),
                )
                _rb2.metric(
                    t("ride_analysis.range_remaining"),
                    f"{_rng_v:.0f} km" if _rng_v is not None else t("ride_analysis.ebike_na"),
                )
                _rb3.metric(
                    t("ride_analysis.est_assist"),
                    f"{_assist_v:.1f}/5" if _assist_v is not None else t("ride_analysis.ebike_na"),
                )

            # Main metrics row
            _rm1, _rm2, _rm3, _rm4 = st.columns(4)
            _time_min = _r.get("time_estimate_min")
            if _time_min:
                _th, _tm = divmod(int(_time_min), 60)
                _time_str = f"{_th}h {_tm:02d}m" if _th else f"{_tm}min"
            else:
                _time_str = "—"
            _avg_hr = _r.get("avg_hr_bpm")
            _rm1.metric(t("ride_analysis.calories"), f"{_r.get('calories_kcal', '—')} kcal")
            _rm2.metric(t("ride_analysis.time_est"), _time_str)
            _rm3.metric(
                t("ride_analysis.avg_hr"),
                f"{_avg_hr} bpm" if _avg_hr else t("ride_analysis.hr_na"),
            )
            _rm4.metric(t("ride_analysis.fatigue"), f"{_r.get('fatigue_index', '—')}/10")

            # Salite di questo giro (Parte 3 — oggettivo + soggettivo per profilo bici+ciclista)
            _climbs_obj = _rg.get("climbs") or []
            _merged_climbs = ride_analysis.merge_climbs_analysis(_climbs_obj, _r.get("climbs_analysis"))
            if _merged_climbs:
                st.subheader(t("ride_analysis.climbs_header"))
                _hardest_idx = _r.get("hardest_climb_index")
                _hardest_reason = _r.get("hardest_climb_reason") or ""
                _hc = next((c for c in _merged_climbs if c["climb_index"] == _hardest_idx), None)
                if _hc:
                    _hc_class_label = t(f"climbs.class.{_hc['classification']}")
                    st.info(
                        f"{t('ride_analysis.hardest_climb_badge')} — km {_hc['start_km']:.1f} "
                        f"({_hc['classification_emoji']} {_hc_class_label})  \n_{_hardest_reason}_"
                    )
                st.dataframe(
                    _climbs_analysis_table_rows(_merged_climbs, _r_is_ebike),
                    use_container_width=True, hide_index=True,
                )

            # Advice
            _advice = _r.get("advice") or []
            if _advice:
                st.subheader(t("ride_analysis.advice_header"))
                for _adv in _advice:
                    st.markdown(f"• {_adv}")

            # Disclaimer
            _disc = _r.get("disclaimer", "")
            if _disc:
                st.warning(f"⚠️ {_disc}")

            # HTML report download — filename from GPX metadata
            _html = ride_analysis.render_html_report(
                _r, _rg, _rp, _rl,
                route_narrative=_route_narrative,
            )
            _gpx_base = _rg.get("gpx_filename") or re.sub(r"[^\w]+", "_", (_rg.get("gpx_name") or "analisi")).strip("_").lower() or "analisi"
            st.download_button(
                label=t("ride_analysis.download_btn"),
                data=_html.encode("utf-8"),
                file_name=f"{_gpx_base}_analysis.html",
                mime="text/html",
                key="ride_download_btn",
            )


# ─── Tab: Post Ride ───────────────────────────────────────────────────────────
with tab_post_ride:
    st.caption(t("post_ride.caption"))

    _pr_cmp, _pr_fb = st.tabs([
        t("analizza.sub_compare"),
        t("analizza.sub_feedback"),
    ])

    with _pr_cmp:
        st.caption(t("analizza.compare_caption"))

        _CATEGORY_ICONS = {
            "problema":   ("red",    "exclamation-sign"),
            "bello":      ("green",  "star"),
            "attenzione": ("orange", "warning-sign"),
            "generico":   ("purple", "map-marker"),
        }
        _CATEGORY_CSS = {
            "problema": "#d62728", "bello": "#2ca02c",
            "attenzione": "#ff7f0e", "generico": "#9467bd",
        }

        col_cmp1, col_cmp2 = st.columns(2)
        with col_cmp1:
            gpx_plan = st.file_uploader(t("analizza.gpx_plan_label"), type=["gpx"], key="cmp_plan")
        with col_cmp2:
            gpx_real = st.file_uploader(t("analizza.gpx_real_label"), type=["gpx"], key="cmp_real")

        # ── Rilevamento automatico route_name dal GPX pianificato ──────────
        _saved_r_cmp = _load_saved_routes()
        _cmp_rname: str | None = None

        if gpx_plan:
            _detected, _detect_note = _detect_route_from_plan_gpx(gpx_plan, _saved_r_cmp)
            if _detected:
                st.success(t("analizza.route_detected").format(name=_detected, note=_detect_note))
                _cmp_rname = _detected
            else:
                st.warning(_detect_note)

        if not gpx_plan:
            st.info(t("analizza.no_plan"))

        if gpx_plan and gpx_real:
            try:
                pts_p, coords_p, analysis_p = _analyze_gpx_bytes(gpx_plan.getvalue())
                pts_r, coords_r, analysis_r = _analyze_gpx_bytes(gpx_real.getvalue())

                if not pts_p or not pts_r:
                    st.warning(t("analizza.no_tracks"))
                else:
                    # Metriche a confronto
                    st.markdown(t("analizza.compare_metrics_header"))
                    hdr, c1, c2, c3 = st.columns([1.5, 1, 1, 1])
                    hdr.markdown(t("analizza.col_metric"))
                    c1.markdown(t("analizza.col_planned"))
                    c2.markdown(t("analizza.col_real"))
                    c3.markdown(t("analizza.col_diff"))

                    def _row(label, v1, v2, fmt="{:+.1f}"):
                        hdr.markdown(label)
                        c1.markdown(f"{v1:.1f}")
                        c2.markdown(f"{v2:.1f}")
                        diff = v2 - v1
                        color = "green" if abs(diff) < abs(v1) * 0.05 else "orange"
                        c3.markdown(f":{color}[{fmt.format(diff)}]")

                    _row(t("analizza.row_distance"), analysis_p["distance_km"], analysis_r["distance_km"])
                    _row(t("analizza.row_elev_up"), analysis_p["elevation_gain_m"], analysis_r["elevation_gain_m"])
                    _row(t("analizza.row_elev_down"), analysis_p["elevation_loss_m"], analysis_r["elevation_loss_m"])

                    # Deviazione massima
                    st.markdown(t("analizza.max_dev_header"))
                    with st.spinner(t("analizza.calc_dev_spinner")):
                        sample_p = coords_p[::max(1, len(coords_p) // 300)]
                        sample_r = coords_r[::max(1, len(coords_r) // 300)]
                        max_dev_m = 0.0
                        max_dev_coord = None
                        for pt_r in sample_r:
                            min_dist = min(geodesic(pt_r, pt_p).meters for pt_p in sample_p)
                            if min_dist > max_dev_m:
                                max_dev_m = min_dist
                                max_dev_coord = pt_r

                    if max_dev_m < 100:
                        dev_label = t("analizza.dev_identical").format(m=max_dev_m)
                    elif max_dev_m < 500:
                        dev_label = t("analizza.dev_moderate").format(m=max_dev_m)
                    else:
                        dev_label = t("analizza.dev_large").format(m=max_dev_m)

                    st.metric(t("analizza.max_dev_label"), dev_label)

                    if max_dev_coord:
                        maps_url = f"https://www.google.com/maps?q={max_dev_coord[0]:.6f},{max_dev_coord[1]:.6f}"
                        st.markdown(
                            f"{t('analizza.max_dev_point')} `{max_dev_coord[0]:.5f}, {max_dev_coord[1]:.5f}` "
                            f"— [{t('analizza.max_dev_gmaps')}]({maps_url})"
                        )

                    # Salva confronto nella route JSON (se route identificata)
                    if _cmp_rname and not st.session_state.get(f"cmp_saved_{_cmp_rname}_{gpx_real.name}"):
                        _save_comparison_to_route(_cmp_rname, {
                            "compared_at": datetime.datetime.now().isoformat(timespec="seconds"),
                            "gpx_planned": gpx_plan.name,
                            "gpx_real": gpx_real.name,
                            "planned_km": round(analysis_p["distance_km"], 2),
                            "real_km": round(analysis_r["distance_km"], 2),
                            "planned_elev_gain": round(analysis_p["elevation_gain_m"]),
                            "real_elev_gain": round(analysis_r["elevation_gain_m"]),
                            "max_deviation_m": round(max_dev_m),
                        })
                        st.session_state[f"cmp_saved_{_cmp_rname}_{gpx_real.name}"] = True

                    # ── Mappa sovrapposta con click handler ─────────────────
                    st.markdown(t("analizza.map_header"))
                    st.caption(t("analizza.map_caption"))

                    # Segnaposto esistenti da DB (solo se route identificata)
                    _existing_annots = db.get_map_annotations(_cmp_rname) if _cmp_rname else []

                    ctr = coords_p[0] if coords_p else coords_r[0]
                    m_cmp = folium.Map(location=list(ctr), zoom_start=12, scrollWheelZoom=False)

                    fg_p = folium.FeatureGroup(name="Pianificato (blu)", show=True)
                    folium.PolyLine(coords_p, color="#3a86ff", weight=3, opacity=0.8, tooltip="Pianificato").add_to(fg_p)
                    fg_p.add_to(m_cmp)
                    fg_r = folium.FeatureGroup(name="Reale (arancio)", show=True)
                    folium.PolyLine(coords_r, color="#f4831f", weight=3, opacity=0.8, tooltip="Reale").add_to(fg_r)
                    fg_r.add_to(m_cmp)

                    if max_dev_coord:
                        folium.Marker(
                            list(max_dev_coord),
                            tooltip=f"Max deviazione: {max_dev_m:.0f} m",
                            icon=folium.Icon(color="red", icon="exclamation-sign"),
                        ).add_to(m_cmp)

                    fg_ann = folium.FeatureGroup(name="Segnaposto", show=True)
                    for _ann in _existing_annots:
                        _acat = _ann.get("category", "generico")
                        _acol, _aico = _CATEGORY_ICONS.get(_acat, ("purple", "map-marker"))
                        folium.Marker(
                            [_ann["lat"], _ann["lon"]],
                            tooltip=f"[{_acat}] {_ann['comment'][:40]}",
                            popup=folium.Popup(
                                f"<b>{_acat.upper()}</b><br>{_ann['comment']}<br>"
                                f"<small>{_ann['created_at'][:16]}</small>",
                                max_width=220,
                            ),
                            icon=folium.Icon(color=_acol, icon=_aico, prefix="glyphicon"),
                        ).add_to(fg_ann)
                    fg_ann.add_to(m_cmp)
                    folium.LayerControl(collapsed=False).add_to(m_cmp)

                    _map_result = st_folium(
                        m_cmp,
                        width=None,
                        height=540,
                        key="cmp_map",
                        use_container_width=True,
                        returned_objects=["last_clicked"],
                    )

                    # ── Pannello aggiunta segnaposto ───────────────────────
                    _clicked = _map_result.get("last_clicked") if _map_result else None
                    if _clicked and _clicked != st.session_state.get("cmp_last_click_processed"):
                        st.session_state["cmp_pending_click"] = {
                            "lat": _clicked["lat"], "lng": _clicked["lng"],
                        }

                    _pending = st.session_state.get("cmp_pending_click")
                    if _pending:
                        st.divider()
                        if not _cmp_rname:
                            st.warning(t("analizza.pin_no_route"))
                        else:
                            st.markdown(t("analizza.pin_header").format(route=_cmp_rname))
                            col_pin1, col_pin2, col_pin3 = st.columns([1, 1, 2])
                            with col_pin1:
                                _pin_lat = st.number_input(
                                    t("analizza.pin_lat"), value=_pending["lat"], format="%.6f", key="pin_lat",
                                )
                            with col_pin2:
                                _pin_lon = st.number_input(
                                    t("analizza.pin_lon"), value=_pending["lng"], format="%.6f", key="pin_lon",
                                )
                            with col_pin3:
                                _pin_cat = st.selectbox(
                                    t("analizza.pin_type"), ["generico", "problema", "bello", "attenzione"], key="pin_cat",
                                )
                            _pin_comment = st.text_input(
                                t("analizza.pin_comment"),
                                key="pin_comment",
                                placeholder=t("analizza.pin_comment_placeholder"),
                            )
                            col_pba, col_pbb = st.columns(2)
                            with col_pba:
                                if st.button(t("analizza.btn_pin_save"), type="primary", key="btn_pin_save"):
                                    if _pin_comment.strip():
                                        db.save_map_annotation(
                                            route_name=_cmp_rname,
                                            lat=_pin_lat,
                                            lon=_pin_lon,
                                            comment=_pin_comment.strip(),
                                            category=_pin_cat,
                                        )
                                        st.session_state["cmp_last_click_processed"] = _clicked
                                        st.session_state.pop("cmp_pending_click", None)
                                        st.success(f"Salvato: [{_pin_cat}] {_pin_comment.strip()[:50]}")
                                        st.rerun()
                                    else:
                                        st.warning(t("analizza.pin_no_comment"))
                            with col_pbb:
                                if st.button(t("analizza.btn_pin_cancel"), key="btn_pin_cancel"):
                                    st.session_state["cmp_last_click_processed"] = _clicked
                                    st.session_state.pop("cmp_pending_click", None)
                                    st.rerun()

                    # ── Lista segnaposto salvati ────────────────────────────
                    if _existing_annots:
                        st.divider()
                        st.markdown(t("analizza.pins_header").format(n=len(_existing_annots)))
                        for _ann in _existing_annots:
                            _acat = _ann.get("category", "generico")
                            col_al, col_ar = st.columns([5, 1])
                            with col_al:
                                st.markdown(
                                    f"<span style='color:{_CATEGORY_CSS.get(_acat,'#9467bd')};font-weight:bold'>"
                                    f"[{_acat}]</span> {_ann['comment']} "
                                    f"<small style='color:gray'>({_ann['lat']:.5f}, {_ann['lon']:.5f}) "
                                    f"· {_ann['created_at'][:16]}</small>",
                                    unsafe_allow_html=True,
                                )
                            with col_ar:
                                if st.button("🗑", key=f"del_ann_{_ann['id']}", help="Elimina"):
                                    db.delete_map_annotation(_ann["id"])
                                    st.rerun()

            except Exception as e:
                import traceback as _tb
                st.error(f"Errore confronto GPX: {e}")
                with st.expander("Traceback"):
                    st.code(_tb.format_exc())

        elif gpx_plan and not gpx_real:
            st.info(t("analizza.load_real"))

    with _pr_fb:
        st.caption(t("analizza.fb_caption"))

        saved_r_fb = _load_saved_routes()
        if not saved_r_fb:
            st.info(t("analizza.fb_no_routes"))
        else:
            fb_route_sel = st.selectbox(
                t("analizza.fb_route_label"),
                ["— seleziona —"] + list(saved_r_fb.keys()),
                key="fb_route_sel",
            )
            if fb_route_sel != "— seleziona —":
                rd_fb = saved_r_fb[fb_route_sel]
                req_fb = rd_fb.get("request", {})
                narrative_fb = rd_fb.get("route_narrative", "")

                if narrative_fb:
                    st.info(narrative_fb)
                st.caption(
                    f"Target: {req_fb.get('target_km','?')} km · "
                    f"{req_fb.get('route_type','?')} · "
                    f"Tema: {req_fb.get('scenery_theme','—')} / {req_fb.get('athletic_theme','—')}"
                )

                # Builder results — pick candidate (run più recente = ultimo elemento)
                _builder_runs_fb = _normalize_builder_results(rd_fb)
                builder_res = _builder_runs_fb[-1] if _builder_runs_fb else {}
                builder_cands = builder_res.get("candidates", []) if builder_res else []
                ok_cands_fb = [c for c in builder_cands if c.get("status") in ("ok", "retried")]

                fb_candidate_id = None
                if ok_cands_fb:
                    cand_options = ["— nessuno/non so —"] + [
                        f"{c['id']} — {c['strategy_name']} ({c['profile']})"
                        for c in ok_cands_fb
                    ]
                    fb_cand_sel = st.selectbox(t("analizza.fb_candidate_label"), cand_options, key="fb_cand_sel")
                    if fb_cand_sel != "— nessuno/non so —":
                        fb_candidate_id = fb_cand_sel.split(" — ")[0]
                else:
                    st.caption(t("analizza.fb_no_candidate"))

                # ── Anteprima segnaposto (prima del form) ───────────────────
                _fb_annotations = db.get_map_annotations(fb_route_sel)
                if _fb_annotations:
                    _CAT_STYLE = {
                        "problema":   ("🔴", "#d62728"),
                        "bello":      ("🟢", "#2ca02c"),
                        "attenzione": ("🟠", "#ff7f0e"),
                        "generico":   ("🟣", "#9467bd"),
                    }
                    _n_prob = sum(1 for a in _fb_annotations if a.get("category") == "problema")
                    _preview_header = t("analizza.fb_pins_preview").format(n=len(_fb_annotations))
                    if _n_prob:
                        _preview_header += t("analizza.fb_pins_problems").format(n=_n_prob)
                    st.markdown(_preview_header)
                    for _ann in _fb_annotations:
                        _acat = _ann.get("category", "generico")
                        _icon, _color = _CAT_STYLE.get(_acat, ("🟣", "#9467bd"))
                        st.markdown(
                            f"{_icon} "
                            f"<span style='color:{_color};font-weight:600'>[{_acat}]</span> "
                            f"{_ann['comment']} "
                            f"<span style='color:#888;font-size:0.85em'>"
                            f"({_ann['lat']:.5f}, {_ann['lon']:.5f}) · {_ann['created_at'][:16]}"
                            f"</span>",
                            unsafe_allow_html=True,
                        )
                else:
                    st.caption(t("analizza.fb_no_pins"))

                with st.form("analizza_fb_form"):
                    fb_rating = st.slider(t("analizza.fb_rating"), 1, 5, 4)
                    st.markdown(t("analizza.fb_char_header"))
                    col_fa, col_fb_c = st.columns(2)
                    with col_fa:
                        fb_traffic = st.checkbox(t("analizza.fb_traffic"))
                        fb_gravel  = st.checkbox(t("analizza.fb_gravel"))
                        fb_hard    = st.checkbox(t("analizza.fb_hard"))
                    with col_fb_c:
                        fb_surface = st.checkbox(t("analizza.fb_surface"))
                        fb_views   = st.checkbox(t("analizza.fb_views"))
                        fb_repeat  = st.checkbox(t("analizza.fb_repeat"), value=True)
                    fb_notes = st.text_area(t("analizza.fb_notes"), placeholder=t("analizza.fb_notes_placeholder"))
                    fb_submitted = st.form_submit_button(t("analizza.btn_fb_save"))

                if fb_submitted:
                    try:
                        # Opinione soggettiva sulla route → feedback_history nel JSON
                        # (non SQLite: è specifica di questa route, non trasversale).
                        _save_feedback_to_route(fb_route_sel, {
                            "recorded_at": datetime.datetime.now().isoformat(timespec="seconds"),
                            "candidate_id": fb_candidate_id or "—",
                            "rating": fb_rating,
                            "too_traffic": fb_traffic,
                            "too_gravel": fb_gravel,
                            "too_hard": fb_hard,
                            "good_surface": fb_surface,
                            "nice_views": fb_views,
                            "would_repeat": fb_repeat,
                            "notes": fb_notes,
                        })

                        # Segnaposto "problema" → promossi a known_obstacles (SQLite,
                        # trasversale: vale per qualunque route futura con la stessa
                        # zona di partenza, indipendentemente da cosa succede poi a
                        # questo JSON). zona = comune di partenza della route corrente.
                        _zona_fb = (req_fb.get("start") or {}).get("name")
                        _problems = [a for a in _fb_annotations if a.get("category") == "problema"]
                        for _p in _problems:
                            db.save_known_obstacle(
                                lat=_p["lat"],
                                lon=_p["lon"],
                                description=_p["comment"],
                                route_name=fb_route_sel,
                                annotation_id=_p["id"],
                                zona=_zona_fb,
                            )

                        _msg = t("analizza.fb_saved")
                        if _problems:
                            _msg += t("analizza.fb_obstacles").format(n=len(_problems))
                        st.success(_msg)
                    except Exception as exc:
                        st.error(f"Errore salvataggio feedback: {exc}")


# ─── Tab: Utility ─────────────────────────────────────────────────────────────
with tab_utility:
    st.caption(t("utility.caption"))

    _util_geo, _util_optimizer, _util_viewgpx, _util_debug = st.tabs([
        t("utility.sub_geo"),
        t("utility.sub_optimizer"),
        t("utility.sub_viewgpx"),
        t("utility.sub_debug"),
    ])

    # ── Sotto-tab: Geolocalizza ───────────────────────────────────────────────
    with _util_geo:
        st.subheader(t("geo.subheader"))
        st.caption(t("geo.caption"))

        def _geo_type_badge(r: dict) -> str:
            rank = r["place_rank"]
            cls  = r["class_"]
            typ  = r["type_"]
            if rank <= 16: return "🏙️ città"
            if rank <= 18: return "🏘️ comune"
            if rank <= 19: return "🏚️ villaggio"
            if rank <= 25: return "🏠 frazione"
            if cls == "highway":   return f"🛣️ {typ}"
            if cls == "amenity":   return f"🏪 {typ}"
            if cls == "natural":   return f"🌿 {typ}"
            if cls == "tourism":   return f"🗺️ {typ}"
            return f"📌 {typ or cls or '?'}"

        def _geo_short(display: str, n: int = 3) -> str:
            parts = [p.strip() for p in display.split(",")]
            return ", ".join(parts[:n])

        col_geo_l, col_geo_r = st.columns([1, 2], gap="large")

        # ── Colonna sinistra: controlli + risultati + click output ─────────────────
        with col_geo_l:
            st.markdown(t("geo.search_header"))
            geo_query = st.text_input(
                "Nome",
                placeholder=t("geo.search_placeholder"),
                key="geo_q",
                label_visibility="collapsed",
            )
            col_gs1, col_gs2 = st.columns([2, 1])
            with col_gs1:
                geo_search_btn = st.button(t("geo.btn_search"), key="btn_geo_s", type="primary",
                                           use_container_width=True)
            with col_gs2:
                geo_only_it = st.checkbox(t("geo.only_italy"), value=True, key="geo_it")

            if geo_search_btn:
                if not geo_query.strip():
                    st.warning(t("geo.warn_empty"))
                else:
                    with st.spinner(t("geo.spinner_nominatim")):
                        cc = "it" if geo_only_it else None
                        try:
                            hits = geocode_search_raw(geo_query.strip(), limit=10, country_codes=cc)
                            st.session_state["geo_results"] = hits
                            st.session_state["geo_sel"]     = None
                        except Exception as exc:
                            st.error(f"Errore Nominatim: {exc}")
                            st.session_state["geo_results"] = []

            # ── Lista risultati ────────────────────────────────────────────────────
            geo_results = st.session_state.get("geo_results", [])
            if "geo_results" in st.session_state and not geo_results:
                st.warning(t("geo.warn_no_results"))

            for i, r in enumerate(geo_results):
                badge  = _geo_type_badge(r)
                short  = _geo_short(r["display_name"])
                is_sel = st.session_state.get("geo_sel") == i
                pfx    = "▶ " if is_sel else ""

                rc1, rc2 = st.columns([5, 2])
                with rc1:
                    if st.button(
                        f"{pfx}{badge}  {short}",
                        key=f"geo_rb_{i}",
                        help=r["display_name"],
                        use_container_width=True,
                    ):
                        st.session_state["geo_sel"] = i
                        st.rerun()
                with rc2:
                    st.caption(f"`{r['lat']:.4f}`  \n`{r['lon']:.4f}`")

            # ── Output selezione ricerca ───────────────────────────────────────────
            geo_sel = st.session_state.get("geo_sel")
            if geo_sel is not None and geo_results:
                r = geo_results[geo_sel]
                coord_s = f"{r['lat']:.5f},{r['lon']:.5f}"
                st.divider()
                st.markdown(
                    f"**{_geo_type_badge(r)}** — {_geo_short(r['display_name'], 2)}  \n"
                    f"<small>{r['display_name']}</small>",
                    unsafe_allow_html=True,
                )
                st.markdown(t("geo.copy_planner"))
                st.code(coord_s, language=None)

            # ── Output click sulla mappa ───────────────────────────────────────────
            geo_clicked = st.session_state.get("geo_clicked")
            if geo_clicked:
                lat_c, lon_c = geo_clicked
                coord_s = f"{lat_c:.5f},{lon_c:.5f}"
                rev = st.session_state.get("geo_rev", "")
                st.divider()
                st.markdown(t("geo.click_header"))
                if rev:
                    st.caption(rev)
                st.markdown(t("geo.copy_planner"))
                st.code(coord_s, language=None)

        # ── Colonna destra: mappa interattiva ─────────────────────────────────────
        with col_geo_r:
            st.caption(t("geo.map_caption"))

            geo_results   = st.session_state.get("geo_results", [])
            geo_sel       = st.session_state.get("geo_sel")
            geo_clicked   = st.session_state.get("geo_clicked")

            # Centro e zoom iniziali
            if geo_sel is not None and geo_results:
                r_sel   = geo_results[geo_sel]
                geo_ctr = [r_sel["lat"], r_sel["lon"]]
                geo_z   = 13
            elif geo_clicked:
                geo_ctr = list(geo_clicked)
                geo_z   = 13
            elif geo_results:
                geo_ctr = [geo_results[0]["lat"], geo_results[0]["lon"]]
                geo_z   = 10
            else:
                geo_ctr = [43.55, 13.10]   # Centro Marche
                geo_z   = 9

            m_geo = folium.Map(location=geo_ctr, zoom_start=geo_z, scrollWheelZoom=False)

            # Marcatori risultati ricerca
            for i, r in enumerate(geo_results):
                is_sel = i == geo_sel
                folium.Marker(
                    [r["lat"], r["lon"]],
                    tooltip=(
                        f"{'▶ ' if is_sel else ''}"
                        f"[{_geo_type_badge(r)}] {_geo_short(r['display_name'], 2)}"
                    ),
                    popup=folium.Popup(
                        f"<b>{_geo_short(r['display_name'], 2)}</b><br>"
                        f"rank={r['place_rank']} · {r['class_']}/{r['type_']}<br>"
                        f"<small>{r['display_name']}</small>",
                        max_width=280,
                    ),
                    icon=folium.Icon(
                        color="red"  if is_sel else "blue",
                        icon="star"  if is_sel else "circle",
                        prefix="fa",
                    ),
                ).add_to(m_geo)

            # Marcatore punto cliccato
            if geo_clicked:
                rev = st.session_state.get("geo_rev", "")
                folium.Marker(
                    list(geo_clicked),
                    tooltip=f"📍 {_geo_short(rev, 2) if rev else 'click'}",
                    popup=folium.Popup(
                        f"<b>{geo_clicked[0]:.5f}, {geo_clicked[1]:.5f}</b><br>"
                        f"<small>{rev}</small>",
                        max_width=280,
                    ),
                    icon=folium.Icon(color="green", icon="crosshairs", prefix="fa"),
                ).add_to(m_geo)

            map_data_geo = st_folium(
                m_geo,
                width=None,
                height=520,
                returned_objects=["last_clicked"],
                key="geo_map",
                use_container_width=True,
            )

            # Gestisci click sulla mappa
            if map_data_geo and map_data_geo.get("last_clicked"):
                lc = map_data_geo["last_clicked"]
                new_c = (round(float(lc["lat"]), 7), round(float(lc["lng"]), 7))
                if st.session_state.get("geo_clicked") != new_c:
                    st.session_state["geo_clicked"] = new_c
                    with st.spinner(t("geo.reverse_spinner")):
                        try:
                            addr = reverse_geocode_address(new_c[0], new_c[1])
                            st.session_state["geo_rev"] = addr or t("geo.no_address")
                        except Exception as exc:
                            st.session_state["geo_rev"] = f"Errore: {exc}"
                    st.rerun()

    # ── Sotto-tab: GPX Optimizer ───────────────────────────────────────────────
    # Stesso strumento del tab di primo livello (vedi _render_gpx_optimizer),
    # qui sempre senza scorciatoia "usa il vincitore" — per ottimizzare file
    # esterni non legati a nessuna route, comportamento identico a quello
    # storico. La vecchia "Gestione file" (carica/elimina route, cartelle GPX)
    # è stata ritirata da qui: superata dal tab File — Le mie route (stessa
    # cosa ma con cascata reale e rinomina) e File orfani (stessa cosa ma con
    # filtro sicuro sui soli orfani, invece di elencare tutto indiscriminatamente).
    with _util_optimizer:
        st.caption(t("utility.optimizer_caption"))
        _render_gpx_optimizer("util", open_route_name=None)

    # ── Sotto-tab: Visualizza GPX ─────────────────────────────────────────────
    with _util_viewgpx:
        st.caption(t("utility.viewgpx_caption"))
        st.caption(t("analizza.viz_caption"))
        uploaded_gpx = st.file_uploader(t("analizza.viz_uploader"), type=["gpx"], key="viz_uploader")

        if uploaded_gpx is not None:
            try:
                raw_bytes = uploaded_gpx.getvalue()
                pts_v, coords_v, analysis_v = _analyze_gpx_bytes(raw_bytes)
                closure_m_v = geodesic(coords_v[0], coords_v[-1]).meters if len(coords_v) >= 2 else 0.0
                auto_rt_v = "loop" if closure_m_v < 500 else "out_and_back"

                if not pts_v:
                    st.warning(t("analizza.viz_no_tracks"))
                else:
                    col_v1, col_v2, col_v3, col_v4 = st.columns(4)
                    col_v1.metric(t("analizza.metric_distance"), f"{analysis_v['distance_km']:.1f} km")
                    col_v2.metric(
                        t("analizza.metric_elev_up"), f"{analysis_v['elevation_gain_m']:.0f} m",
                        help=t("analizza.metric_elev_up_help"),
                    )
                    col_v3.metric(t("analizza.metric_elev_down"), f"{analysis_v['elevation_loss_m']:.0f} m")
                    if auto_rt_v == "loop":
                        loop_icon = "✅" if analysis_v.get("loop_closed") else "⚠️"
                        col_v4.metric(t("analizza.metric_loop"), f"{loop_icon} ({analysis_v.get('closure_distance_m', 0):.0f} m)")
                    else:
                        col_v4.metric(t("analizza.metric_type"), t("analizza.metric_oadb"))

                    st.caption(f"**{len(pts_v)} punti GPX** · {auto_rt_v} · chiusura {closure_m_v:.0f} m")

                    m_v = folium.Map(location=list(coords_v[0]), zoom_start=12, scrollWheelZoom=False)
                    folium.PolyLine(coords_v, color="#3a86ff", weight=4, opacity=0.85).add_to(m_v)
                    folium.Marker(list(coords_v[0]), tooltip="Partenza", icon=folium.Icon(color="green", icon="play")).add_to(m_v)
                    folium.Marker(list(coords_v[-1]), tooltip="Fine", icon=folium.Icon(color="red", icon="stop")).add_to(m_v)
                    st_folium(m_v, width=None, height=520, key="viz_map", use_container_width=True)
            except Exception as e:
                st.error(f"Errore lettura GPX: {e}")

    # ── Sotto-tab: Debug ──────────────────────────────────────────────────────
    with _util_debug:
        st.info(t("debug.info"))

        # ── Sezione 1: Route pianificate (Planner) ────────────────────────────────
        with st.expander(t("debug.routes_expander"), expanded=True):
            saved_routes = _load_saved_routes()
            if not saved_routes:
                st.caption(t("debug.no_routes"))
            else:
                route_names = list(saved_routes.keys())
                dbg_sel = st.selectbox(
                    t("debug.select_route"), ["— seleziona —"] + route_names, key="dbg_route_sel"
                )
                if dbg_sel != "— seleziona —":
                    rd = saved_routes[dbg_sel]

                    sub1, sub2, sub3 = st.tabs([t("debug.sub_json"), t("debug.sub_prompt"), t("debug.sub_score")])

                    with sub1:
                        wps = rd.get("ordered_waypoints", [])
                        req_rd = rd.get("request", {})
                        st.markdown(
                            f"**{dbg_sel}** — {req_rd.get('target_km','?')} km · "
                            f"{req_rd.get('route_type','?')} · creata {rd.get('created_at','—')}"
                        )
                        dbg_narrative = rd.get("route_narrative", "")
                        if dbg_narrative:
                            st.markdown(t("debug.narrative_label"))
                            st.info(dbg_narrative)
                        if rd.get("warnings"):
                            for w in rd["warnings"]:
                                st.warning(w)
                        dbg_sq = rd.get("search_queries", [])
                        if dbg_sq:
                            with st.expander(f"{t('debug.web_searches')} ({len(dbg_sq)})", expanded=False):
                                for _qi, _q in enumerate(dbg_sq, 1):
                                    st.markdown(f"**{_qi}.** `{_q}`")
                        wp_rows = [
                            {
                                t("planner.col_ord"): wp.get("order", i),
                                t("planner.col_role"): wp.get("role"),
                                t("planner.col_name"): wp.get("name"),
                                t("planner.col_source"): wp.get("source"),
                                t("planner.col_constraint"): (
                                    t("planner.badge_mandatory") if _wp_is_forced(wp) else t("planner.badge_soft")
                                ),
                                t("planner.col_lat"): wp.get("lat"),
                                t("planner.col_lon"): wp.get("lon"),
                                t("planner.col_rationale"): wp.get("rationale") or "",
                            }
                            for i, wp in enumerate(wps)
                        ]
                        st.dataframe(wp_rows, use_container_width=True, hide_index=True)
                        with st.expander("JSON completo"):
                            st.json(rd)

                    with sub2:
                        sp = rd.get("system_prompt", "")
                        up = rd.get("user_prompt", "")
                        if sp or up:
                            st.markdown(t("planner.result_system_prompt"))
                            st.code(sp, language="text")
                            st.markdown(t("planner.result_user_prompt"))
                            st.code(up, language="text")
                        else:
                            st.caption(t("debug.no_prompt"))

                    with sub3:
                        _br_runs = _normalize_builder_results(rd)
                        scores = _br_runs[-1].get("scored", []) if _br_runs else []
                        if scores:
                            st.json(scores)
                        else:
                            st.caption(t("debug.no_scores"))

        # ── Sezione 2: Debug solo BRouter ─────────────────────────────────────────
        with st.expander(t("debug.brouter_expander"), expanded=False):
            st.caption(t("debug.brouter_caption"))

            col1, col2 = st.columns(2)
            with col1:
                dbr_start_lat = st.number_input(t("debug.lat_start"), value=43.7136520, format="%.7f", key="dbr_slat")
            with col2:
                dbr_start_lon = st.number_input(t("debug.lon_start"), value=13.2278056, format="%.7f", key="dbr_slon")

            dbr_target_km  = st.selectbox(t("debug.target_km"), [50, 55, 60, 65, 70], index=2, key="dbr_km")
            dbr_route_type = st.selectbox(t("debug.route_type"), ["loop", "out_and_back", "point_to_point"], key="dbr_rt")
            dbr_profile    = st.selectbox(t("debug.profile"), ["trekking", "gravel", "fastbike"], key="dbr_prof")

            dbr_end_lat = dbr_end_lon = None
            if dbr_route_type == "point_to_point":
                col3, col4 = st.columns(2)
                with col3:
                    dbr_end_lat = st.number_input(t("debug.lat_end"), value=43.7200000, format="%.7f", key="dbr_elat")
                with col4:
                    dbr_end_lon = st.number_input(t("debug.lon_end"), value=13.2400000, format="%.7f", key="dbr_elon")

            if st.button(t("debug.btn_brouter"), key="btn_dbr"):
                try:
                    if dbr_route_type == "point_to_point":
                        waypoints = [(dbr_start_lon, dbr_start_lat), (dbr_end_lon, dbr_end_lat)]
                    else:
                        waypoints = [
                            (dbr_start_lon, dbr_start_lat),
                            (dbr_start_lon + 0.05, dbr_start_lat + 0.05),
                        ]
                        if dbr_route_type == "loop":
                            waypoints.append((dbr_start_lon, dbr_start_lat))

                    gpx_path = get_route(
                        waypoints, profile=dbr_profile,
                        output_path="routes/generated/dbr_test.gpx",
                    )
                    analysis = analyze_gpx(
                        gpx_path,
                        route_type=dbr_route_type,
                        expected_end=(dbr_end_lon, dbr_end_lat)
                        if dbr_route_type == "point_to_point" else None,
                    )
                    st.session_state["dbr_gpx_path"] = gpx_path
                    st.session_state["dbr_analysis"]  = analysis
                    st.session_state["dbr_slat_v"]    = dbr_start_lat
                    st.session_state["dbr_slon_v"]    = dbr_start_lon
                except Exception as e:
                    st.error(f"Errore BRouter: {e}")

            if "dbr_gpx_path" in st.session_state:
                st.success(f"GPX: {st.session_state['dbr_gpx_path']}")
                st.json(st.session_state["dbr_analysis"])
                m = _build_map(
                    st.session_state["dbr_gpx_path"],
                    st.session_state["dbr_slat_v"],
                    st.session_state["dbr_slon_v"],
                )
                st_folium(m, width=750, height=450, key="dbr_map")

        # ── Sezione 3: Debug solo Planner ─────────────────────────────────────────
        with st.expander(t("debug.planner_expander"), expanded=False):
            st.caption(t("debug.planner_caption"))

            col1, col2, col3 = st.columns([2, 1, 1])
            with col1:
                dbp_start_name = st.text_input(t("debug.name_start"), value="Casa Senigallia", key="dbp_sname")
            with col2:
                dbp_start_lat = st.number_input(t("planner.form.lat"), value=43.7136520, format="%.7f", key="dbp_slat")
            with col3:
                dbp_start_lon = st.number_input(t("planner.form.lon"), value=13.2278056, format="%.7f", key="dbp_slon")

            col4, col5 = st.columns(2)
            with col4:
                dbp_target_km  = st.selectbox(t("debug.distance_km"), [50, 55, 60, 65, 70], index=2, key="dbp_km")
                dbp_route_type = st.selectbox(t("debug.route_type"), ["loop", "out_and_back", "point_to_point"], key="dbp_rt")
            with col5:
                dbp_max_elev  = st.number_input(t("debug.max_elev"), value=700, step=50, key="dbp_elev")
                dbp_preferred = st.text_input(t("debug.preferred_dir"), value="colline, borghi", key="dbp_pref")

            dbp_desired  = st.text_input(t("debug.desired_places"), value="", key="dbp_des")
            dbp_avoid    = st.text_input(t("debug.avoid"), value="SS16", key="dbp_av")
            dbp_free_txt = st.text_area(t("debug.free_text"), height=60, key="dbp_free")
            dbp_geo_dir  = st.selectbox(
                t("debug.geo_dir"), [t("planner.form.direction_free"), "Nord", "Sud", "Est", "Ovest"], key="dbp_gdir",
            )

            dbp_end_name = None
            dbp_stages   = []
            if dbp_route_type == "point_to_point":
                col_e1, col_e2, col_e3 = st.columns([2, 1, 1])
                with col_e1:
                    dbp_end_name = st.text_input(t("debug.end_name"), value="Corinaldo", key="dbp_ename")
                with col_e2:
                    dbp_end_lat_raw = st.number_input(
                        t("debug.end_lat_geo"), value=0.0, format="%.7f", key="dbp_elat"
                    )
                    dbp_end_lat = dbp_end_lat_raw if dbp_end_lat_raw != 0.0 else None
                with col_e3:
                    dbp_end_lon_raw = st.number_input(
                        t("debug.end_lon_geo"), value=0.0, format="%.7f", key="dbp_elon"
                    )
                    dbp_end_lon = dbp_end_lon_raw if dbp_end_lon_raw != 0.0 else None

                dbp_stages_raw = st.text_input(t("debug.stages"), value="Ostra, Trecastelli", key="dbp_stages")
                dbp_stages = [s.strip() for s in dbp_stages_raw.split(",") if s.strip()]

            def _dbp_build_request() -> dict:
                req = {
                    "start": {"name": dbp_start_name, "lat": dbp_start_lat, "lon": dbp_start_lon},
                    "target_km": dbp_target_km,
                    "distance_tolerance_km": 5,
                    "route_type": dbp_route_type,
                    "waypoints_stages": dbp_stages,
                    "preferred_direction": _parse_csv(dbp_preferred),
                    "desired_places": _parse_csv(dbp_desired),
                    "avoid_places": _parse_csv(dbp_avoid),
                    "max_elevation_gain_m": dbp_max_elev,
                    "candidate_count": 3,
                    "free_text": dbp_free_txt.strip(),
                    "geographic_direction": None if dbp_geo_dir == t("planner.form.direction_free") else dbp_geo_dir,
                }
                if dbp_route_type == "point_to_point" and dbp_end_name:
                    req["end"] = {
                        "name": dbp_end_name,
                        "lat": st.session_state.get("dbp_elat") or None,
                        "lon": st.session_state.get("dbp_elon") or None,
                    }
                return req

            col_b1, col_b2, _ = st.columns([1, 1, 2])
            with col_b1:
                show_prompt_btn = st.button(t("debug.btn_show_prompt"), key="btn_dbp_prompt")
            with col_b2:
                generate_btn = st.button(t("debug.btn_generate"), key="btn_dbp_generate")

            if show_prompt_btn or generate_btn:
                dbp_req = _dbp_build_request()
                memory = load_user_memory()
                merged = merge_memory_with_request(dbp_req, memory)
                sp, up = build_prompt(merged, user_memory=memory)
                st.session_state["dbp_prompt"] = (sp, up)

            if "dbp_prompt" in st.session_state:
                sp, up = st.session_state["dbp_prompt"]
                with st.expander(t("debug.prompt_expander"), expanded=True):
                    st.markdown(t("planner.result_system_prompt"))
                    st.code(sp, language="text")
                    st.markdown(t("planner.result_user_prompt"))
                    st.code(up, language="text")

            if generate_btn:
                dbp_req = _dbp_build_request()
                with st.spinner("Claude sta pianificando le strategie..."):
                    try:
                        strategies = generate_strategies(dbp_req)
                        st.session_state["dbp_strategies"] = strategies
                        st.session_state["dbp_geocoded"]   = False
                        st.success(f"{len(strategies)} strategie generate.")
                    except Exception as e:
                        st.error(f"Errore Planner Agent: {e}")

            if st.button(
                t("debug.geocode_btn"),
                key="btn_dbp_geo",
                disabled="dbp_strategies" not in st.session_state,
            ):
                with st.spinner(t("debug.geocode_spinner")):
                    try:
                        geocoded = [geocode_candidate(s) for s in st.session_state["dbp_strategies"]]
                        st.session_state["dbp_strategies"] = geocoded
                        st.session_state["dbp_geocoded"]   = True
                        st.success(t("debug.geocode_done"))
                    except Exception as e:
                        st.error(f"Errore geocodifica: {e}")

            if "dbp_strategies" in st.session_state:
                strategies = st.session_state["dbp_strategies"]
                geocoded_flag = st.session_state.get("dbp_geocoded", False)
                status_label = t("debug.geocode_waypoints_label") if geocoded_flag else ""
                st.subheader(f"{t('debug.strategies_header')} ({len(strategies)}){status_label}")
                for i, s in enumerate(strategies, 1):
                    label = (
                        f"Strategia {i}: {s.get('name', '—')}"
                        f"  [{s.get('profile', '?')} · ~{s.get('estimated_km', '?')} km]"
                    )
                    with st.expander(label):
                        st.write(
                            f"**Tipo:** {s.get('route_type')} &nbsp;|&nbsp; "
                            f"**Profilo:** {s.get('profile')} &nbsp;|&nbsp; "
                            f"**~{s.get('estimated_km')} km**"
                        )
                        st.write(f"**Rationale:** {s.get('rationale', '—')}")
                        wps = s.get("waypoints", [])
                        if wps:
                            wp_rows = [
                                {
                                    t("debug.col_role"): wp.get("role", ""),
                                    t("debug.col_name"): wp.get("name", ""),
                                    t("debug.col_lat"): wp.get("lat"),
                                    t("debug.col_lon"): wp.get("lon"),
                                    t("debug.col_geocoding"): (
                                        t("debug.geocoding_error") if wp.get("geocoding_error")
                                        else (t("debug.geocoding_ok") if not wp.get("needs_geocoding") else t("debug.geocoding_todo"))
                                    ),
                                }
                                for wp in wps
                            ]
                            st.dataframe(wp_rows, use_container_width=True)
                        st.json(s)

        # ── Sezione 4: Known obstacles ────────────────────────────────────────────
        with st.expander(t("debug.obstacles_expander"), expanded=False):
            _obs_list = db.get_active_obstacles()
            if not _obs_list:
                st.caption(t("debug.no_obstacles"))
            else:
                st.caption(t("debug.obstacles_active").format(n=len(_obs_list)))
                for _obs in _obs_list:
                    _ocol1, _ocol2 = st.columns([6, 1])
                    with _ocol1:
                        maps_url_obs = f"https://www.google.com/maps?q={_obs['lat']:.6f},{_obs['lon']:.6f}"
                        st.markdown(
                            f"🔴 **{_obs['description']}** "
                            f"<small>({_obs['lat']:.5f}, {_obs['lon']:.5f}) "
                            f"· zona: {_obs.get('zona') or '—'} "
                            f"· {_obs.get('route_name','—')} · {_obs['created_at'][:10]}</small> "
                            f"[↗ Maps]({maps_url_obs})",
                            unsafe_allow_html=True,
                        )
                    with _ocol2:
                        if st.button("🔕", key=f"deact_obs_{_obs['id']}", help=t("debug.obstacle_deactivate_help")):
                            db.deactivate_obstacle(_obs["id"])
                            st.rerun()
