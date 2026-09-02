"""
Route Editor Agent — flusso MANUALE (solo click), due modalità.

Doppio uso: script standalone (render_standalone_app, `streamlit run
route_editor_manual_splice_tool.py` — comportamento invariato, nessun import
da main.py qui) E corpo del tab "Manual" di main.py (render_manual_tab,
importata direttamente — nessun import da main.py qui dentro, quindi nessun
rischio di eseguire l'intera app; il collegamento è a senso unico). Mappa e
pattern click→coordinata reimplementati con le stesse librerie (folium/
streamlit-folium) e la stessa convenzione già in uso nell'app.

Nessun Intent Parser / geocodifica fuzzy / map-matching qui: un solo tipo di
click, nessuna selezione dedicata di "distacco" vs "raccordo" — la deduzione
(quando applicabile) è puramente posizionale. Il collegamento con l'Intent
Parser (come suggerimento opzionale di partenza) resta per un secondo step.

Interazione con i punti — un solo meccanismo, sempre uguale (nessun tipo dato
"ANCHOR" separato, mai): esiste solo una lista ordinata di coordinate; il
ruolo (distacco/intermedio/raccordo) è SEMPRE dedotto al volo dalla posizione
nella lista al momento del render, mai salvato. Click su spazio vuoto della
mappa → inserisce un nuovo punto nella posizione a costo minimo (cheapest
insertion: minimizza la deviazione aggiuntiva rispetto alla coppia di punti
consecutivi più vicina, o l'estensione a un capo se più economica). Click su
un marker esistente → lo seleziona (nessun inserimento); da selezionato si
può eliminare o spostare (il prossimo click sulla mappa ne sostituisce la
posizione). Inserire/spostare/eliminare un punto ricalcola i ruoli
semplicemente perché ricalcola le posizioni — stessa identica operazione di
lista sia per un punto "interno" sia per quello che risulta essere il
distacco o il raccordo.

Due modalità, selezionate da un file uploader opzionale in cima:
- Nessun GPX caricato → MODALITÀ 1 "genera da zero": mappa vuota, ogni click
  è un via-point mandatory, "Genera" chiama BRouter sull'intera sequenza e
  basta — nessun tracciato originale a cui agganciarsi.
- GPX caricato → MODALITÀ 2 "modifica" (comportamento già validato,
  invariato): primo/ultimo click agganciati al punto più vicino sul
  tracciato caricato (distacco/raccordo), "Genera" chiama BRouter e sostituisce
  quell'intervallo del tracciato originale col tratto generato.

BRouter: riusa brouter_client.get_route così com'è (stessa funzione usata da
candidate_generator.py); profilo selezionabile (stessa lista di
main.py — Builder → profili) per sperimentare, default "trekking".

Fusione (solo Modalità 2): stesso principio fisico di
gpx_analyzer._splice_out_range (taglio manuale "✂️ Cancella tratto"/taglio
automatico andata-ritorno) — qui l'intervallo rimosso viene SOSTITUITO con la
geometria BRouter invece di essere ricucito a vuoto, quindi reimplementata in
splice_in_segment() sotto piuttosto che riusare _splice_out_range direttamente
(che assume rimozione pura, nessun inserto).

Uso:
    streamlit run app/route_editor_manual_splice_tool.py
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import folium
import gpxpy
import streamlit as st
import streamlit.components.v1 as st_components
from streamlit_folium import st_folium

from brouter_client import get_route
from climb_chart import render_climb_chart_html
from geocoding_agent import geocode_climbs
from gpx_analyzer import analyze_gpx
from gpx_optimizer import DANGER_THRESHOLD_PCT_DEFAULT

_OUT_DIR = Path("routes/generated/manual_splice_test")
_DEFAULT_CENTER = (43.7159, 13.2183)  # Senigallia — centro mappa quando non c'è alcun GPX caricato

# Stessa lista profili offerta in Builder (main.py, bld_profiles) — un solo
# profilo per chiamata qui, non un multiselect: questo strumento serve a
# esplorare l'effetto di UN profilo alla volta, non a generare varianti.
_PROFILE_OPTIONS = [
    "trekking", "ebike_asphalt_safe", "ebike_gravel_easy", "ebike_scenic",
    "roadbike_fast", "gravel", "fastbike",
]
_DEFAULT_PROFILE = "ebike_asphalt_safe"

_SELECTED_KEY = "rem_selected_idx"
_MOVE_MODE_KEY = "rem_move_mode"
_MAP_CENTER_KEY = "rem_manual_map_center"
_MAP_HEIGHT = 700


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _track_length_km(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(points)):
        total += _haversine_m(*points[i - 1], *points[i])
    return total / 1000


def _nearest_index(points: list[tuple[float, float]], lat: float, lon: float) -> tuple[int, float]:
    best_i, best_d = 0, float("inf")
    for i, (plat, plon) in enumerate(points):
        d = _haversine_m(lat, lon, plat, plon)
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


def _cheapest_insert_index(points: list[tuple[float, float]], new_pt: tuple[float, float]) -> int:
    """
    Indice in cui inserire new_pt in points (lista aperta, non un ciclo) al
    costo minimo — standard "cheapest insertion": per ogni coppia consecutiva
    esistente (a, b), il costo di inserire tra i due è
    dist(a,new)+dist(new,b)-dist(a,b) (deviazione aggiuntiva); i due estremi
    della lista sono trattati come archi virtuali (costo = distanza dal solo
    capo), così un click oltre l'inizio/la fine estende la sequenza invece di
    schiacciarsi in mezzo a due punti vicini.
    """
    n = len(points)
    if n == 0:
        return 0
    if n == 1:
        return 1

    best_idx, best_cost = n, _haversine_m(*points[-1], *new_pt)  # append in fondo
    prepend_cost = _haversine_m(*new_pt, *points[0])
    if prepend_cost < best_cost:
        best_idx, best_cost = 0, prepend_cost

    for i in range(n - 1):
        a, b = points[i], points[i + 1]
        cost = _haversine_m(*a, *new_pt) + _haversine_m(*new_pt, *b) - _haversine_m(*a, *b)
        if cost < best_cost:
            best_idx, best_cost = i + 1, cost

    return best_idx


def _role_label_and_color(mode: str, i: int, n: int) -> tuple[str, str]:
    """Ruolo dedotto SEMPRE dalla posizione corrente — mai salvato."""
    if n == 1:
        return ("Unico punto", "cadetblue")
    if i == 0:
        return (("Distacco" if mode == "modifica" else "Partenza"), "orange")
    if i == n - 1:
        return (("Raccordo" if mode == "modifica" else "Arrivo"), "purple")
    return ("Intermedio", "gray")


def _render_climb_chart_embed(climbs: list[dict], distance_km: float, profile: dict, title: str = "") -> None:
    """
    Embed grafico salite (profilo SVG + tabella) — stesso identico
    generatore HTML di main.py::_render_climb_chart (render_climb_chart_html,
    climb_chart.py), qui reimplementato solo nel sottile wrapper di
    embedding (st.components.v1.html) perché _render_climb_chart stessa vive
    in main.py e importarla eseguirebbe l'intera app Streamlit principale.
    """
    html = render_climb_chart_html(
        climbs, distance_km,
        profile.get("distances_km"), profile.get("elevations_m"),
        lang="it", danger_threshold_pct=DANGER_THRESHOLD_PCT_DEFAULT,
        title=title,
    )
    height = 620 + max(0, len(climbs) - 3) * 40
    st_components.html(html, height=min(height, 1400), scrolling=True)


def _load_track_latlon(gpx_path: str) -> list[tuple[float, float]]:
    with open(gpx_path) as f:
        gpx = gpxpy.parse(f)
    pts = []
    for track in gpx.tracks:
        for seg in track.segments:
            pts.extend((p.latitude, p.longitude) for p in seg.points)
    return pts


def _load_single_segment_gpx(gpx_path: str):
    """Ritorna (gpx_object, track_points) — supporta solo file a 1 track/1
    segment, come gli altri file gestiti da quest'app (output BRouter)."""
    with open(gpx_path) as f:
        gpx = gpxpy.parse(f)
    if len(gpx.tracks) != 1 or len(gpx.tracks[0].segments) != 1:
        raise ValueError(
            f"{gpx_path}: atteso esattamente 1 track/1 segment, trovati "
            f"{len(gpx.tracks)} track / "
            f"{sum(len(t.segments) for t in gpx.tracks)} segment totali"
        )
    return gpx, gpx.tracks[0].segments[0].points


def generate_from_scratch(
    click_sequence: list[tuple[float, float]],
    out_path: str,
    profile: str = _DEFAULT_PROFILE,
) -> dict:
    """MODALITÀ 1: nessun tracciato originale — chiama BRouter sull'intera
    sequenza di click (tutti mandatory) e basta."""
    if len(click_sequence) < 2:
        raise ValueError("Servono almeno 2 waypoint")

    lonlat = [(lon, lat) for lat, lon in click_sequence]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    get_route(lonlat, profile=profile, output_path=str(out_path))

    _, points = _load_single_segment_gpx(out_path)
    if not points:
        raise RuntimeError("BRouter ha restituito un percorso vuoto")

    latlon = [(p.latitude, p.longitude) for p in points]
    return {
        "n_points": len(points),
        "distance_km": round(_track_length_km(latlon), 2),
        "out_path": str(out_path),
    }


def splice_in_segment(
    original_gpx_path: str,
    click_sequence: list[tuple[float, float]],
    out_path: str,
    profile: str = _DEFAULT_PROFILE,
) -> dict:
    """
    MODALITÀ 2: genera con BRouter un nuovo tratto dal punto del tracciato
    originale più vicino al PRIMO click al punto più vicino all'ULTIMO click,
    passando per i click intermedi (in ordine, mandatory) — poi sostituisce
    l'intervallo del tracciato originale tra quei due punti col tratto
    generato. Non sovrascrive mai original_gpx_path.
    """
    if len(click_sequence) < 2:
        raise ValueError("Servono almeno 2 waypoint (il primo e l'ultimo)")

    gpx, track_points = _load_single_segment_gpx(original_gpx_path)
    latlon = [(p.latitude, p.longitude) for p in track_points]

    first_click, last_click = click_sequence[0], click_sequence[-1]
    intermediate = click_sequence[1:-1]

    idx_first, dist_first = _nearest_index(latlon, *first_click)
    idx_last, dist_last = _nearest_index(latlon, *last_click)

    reversed_order = idx_first > idx_last
    lo_idx, hi_idx = sorted((idx_first, idx_last))
    snap_lo, snap_hi = latlon[lo_idx], latlon[hi_idx]

    if not reversed_order:
        brouter_wp_latlon = [snap_lo] + intermediate + [snap_hi]
    else:
        brouter_wp_latlon = [snap_lo] + list(reversed(intermediate)) + [snap_hi]
    brouter_wp_lonlat = [(lon, lat) for lat, lon in brouter_wp_latlon]

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    brouter_gpx_path = _OUT_DIR / "brouter_leg.gpx"
    get_route(brouter_wp_lonlat, profile=profile, output_path=str(brouter_gpx_path))

    with open(brouter_gpx_path) as f:
        brouter_gpx = gpxpy.parse(f)
    brouter_points = []
    for t in brouter_gpx.tracks:
        for s in t.segments:
            brouter_points.extend(s.points)
    if not brouter_points:
        raise RuntimeError("BRouter ha restituito un percorso vuoto")

    new_points = track_points[:lo_idx] + brouter_points + track_points[hi_idx + 1:]
    gpx.tracks[0].segments[0].points = new_points

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(gpx.to_xml())

    return {
        "idx_first_click": idx_first, "dist_first_click_m": round(dist_first, 1),
        "idx_last_click": idx_last, "dist_last_click_m": round(dist_last, 1),
        "splice_lo_idx": lo_idx, "splice_hi_idx": hi_idx,
        "reversed_order": reversed_order,
        "n_intermediate": len(intermediate),
        "n_brouter_points": len(brouter_points),
        "n_final_points": len(new_points),
        "brouter_waypoints_lonlat": brouter_wp_lonlat,
        "out_path": str(out_path),
    }


# ─────────────────────────────────────────────────────────────────────────
_FORCE_SCRATCH_KEY = "rem_force_scratch"


def render_standalone_app() -> None:
    """
    Entry point dello script standalone (streamlit run
    route_editor_manual_splice_tool.py) — st.set_page_config + titolo, poi
    delega tutto il corpo a render_manual_tab() (nessun precompiled_gpx_path
    qui: comportamento identico a prima di questo refactor).
    """
    st.set_page_config(page_title="Route Editor — Flusso manuale", layout="wide")
    st.title("✂️➕ Route Editor Agent — Flusso manuale (solo click)")
    st.caption(
        "Strumento standalone di test, NON integrato in main.py. Nessun Intent "
        "Parser / geocodifica / map-matching — solo click dell'utente + BRouter."
    )
    render_manual_tab()


def render_manual_tab(
    precompiled_gpx_path: str | None = None,
    precompiled_label: str | None = None,
) -> None:
    """
    Corpo riusabile dell'editor manuale (upload/click/genera/analisi salite) —
    usato sia dallo script standalone (render_standalone_app, nessun
    precompiled_gpx_path) sia dal tab "Manual" di main.py, dove
    precompiled_gpx_path/precompiled_label sono il GPX e l'etichetta del
    candidato vincente della route aperta (se presente): precompila la
    modalità "modifica" con quel tracciato, ma resta sempre possibile
    caricare un GPX diverso (ha sempre precedenza) o passare esplicitamente a
    "crea da zero" (checkbox sotto), esattamente come richiesto per Manual
    (a differenza di Builder, mai un gating totale).

    Tutte le chiavi di session_state usate qui sono prefissate "rem_"
    (Route Editor Manual) per restare isolate dal resto, molto più ampio, di
    session_state di main.py quando questa funzione è imbottita in un tab.
    """
    uploaded = st.file_uploader(
        "GPX da modificare (opzionale — se non carichi nulla, generi un percorso da zero)",
        type=["gpx"], key="rem_uploader",
    )

    force_scratch = False
    if uploaded is None and precompiled_gpx_path:
        force_scratch = st.checkbox(
            "🆕 Parti da zero invece del candidato precompilato", key=_FORCE_SCRATCH_KEY,
        )

    if uploaded is not None:
        mode = "modifica"
        active_gpx_path = str(_OUT_DIR / "uploaded_input.gpx")
        Path(active_gpx_path).parent.mkdir(parents=True, exist_ok=True)
        with open(active_gpx_path, "wb") as f:
            f.write(uploaded.getvalue())
        source_key = f"UPLOAD:{uploaded.name}:{uploaded.size}"
    elif precompiled_gpx_path and not force_scratch:
        mode = "modifica"
        active_gpx_path = precompiled_gpx_path
        source_key = f"PRECOMPILED:{precompiled_gpx_path}"
    else:
        mode = "generazione"
        active_gpx_path = None
        source_key = "SCRATCH"

    if st.session_state.get("rem_active_source_key") != source_key:
        st.session_state["rem_active_source_key"] = source_key
        st.session_state["rem_manual_waypoints"] = []
        st.session_state["rem_manual_result"] = None
        st.session_state["rem_manual_last_click"] = None
        st.session_state["rem_manual_last_object_click"] = None
        st.session_state[_SELECTED_KEY] = None
        st.session_state[_MOVE_MODE_KEY] = False
        st.session_state[_MAP_CENTER_KEY] = None
        st.session_state["rem_manual_climb_analysis"] = None

    if mode == "modifica":
        if uploaded is not None:
            st.info(f"📂 Modalità **modifica** — GPX caricato: {uploaded.name}")
        else:
            st.info(
                f"📂 Modalità **modifica** — precompilato con il candidato vincente "
                f"della route aperta ({precompiled_label})."
            )
        original_latlon = _load_track_latlon(active_gpx_path)
    else:
        st.info("🆕 Modalità **genera da zero** — nessun GPX caricato, mappa vuota.")
        original_latlon = None

    if "rem_manual_waypoints" not in st.session_state:
        st.session_state["rem_manual_waypoints"] = []
    if "rem_manual_result" not in st.session_state:
        st.session_state["rem_manual_result"] = None
    if _SELECTED_KEY not in st.session_state:
        st.session_state[_SELECTED_KEY] = None
    if _MOVE_MODE_KEY not in st.session_state:
        st.session_state[_MOVE_MODE_KEY] = False

    wps = st.session_state["rem_manual_waypoints"]
    selected_idx = st.session_state[_SELECTED_KEY]
    move_mode = st.session_state[_MOVE_MODE_KEY]

    st.subheader("1. Costruisci la sequenza di punti")
    st.caption(
        "Un solo meccanismo: clicca uno spazio vuoto della mappa per inserire un "
        "nuovo punto (si posiziona da solo dove aggiunge meno deviazione); clicca "
        "un marker esistente per selezionarlo, poi eliminalo o spostalo dal "
        "pannello sotto la mappa. " + (
            "Il primo punto della sequenza è sempre il distacco, l'ultimo il "
            "raccordo — dedotto dalla posizione corrente, non da un tipo salvato."
            if mode == "modifica" else
            "Nessun tracciato originale qui: il ruolo (partenza/intermedio/arrivo) "
            "è solo indicativo, dedotto dalla posizione."
        )
    )

    if move_mode and selected_idx is not None and 0 <= selected_idx < len(wps):
        st.warning(
            f"🔄 Modalità sposta attiva per **Punto #{selected_idx + 1}** — "
            "clicca sulla mappa la nuova posizione."
        )

    # Zoom scelto esplicitamente dall'utente (widget Streamlit normale — persiste
    # da solo in session_state, nessun round-trip col componente mappa, quindi
    # nessuno dei problemi di jitter discussi sotto): sostituisce il fit_bounds
    # automatico, che ricalcolava zoom/vista ad ogni operazione sui waypoint.
    zoom_level = st.slider("🔍 Zoom mappa", min_value=5, max_value=18, value=13, key="rem_manual_map_zoom")

    # Il CENTRO segue sempre l'ultimo punto aggiunto/spostato (salvato al momento
    # del click, vedi sotto) — non ricalcolato da un fit_bounds sul set corrente,
    # che centrerebbe altrove ("per conto suo") invece che sul punto appena
    # toccato. Selezionare o eliminare un punto non sposta il centro.
    map_center = st.session_state.get(_MAP_CENTER_KEY) or (
        original_latlon[0] if mode == "modifica" else _DEFAULT_CENTER
    )

    # scrollWheelZoom=False: lo zoom della rotellina era troppo sensibile al
    # semplice passaggio del mouse — restano attivi solo i controlli +/- nativi
    # e lo slider "Zoom mappa" sopra (quello su cui la vista si reimposta ad ogni
    # rerun).
    m = folium.Map(location=map_center, zoom_start=zoom_level, scrollWheelZoom=False)

    if mode == "modifica":
        folium.PolyLine(
            original_latlon, color="blue", weight=3, opacity=0.75,
            tooltip="Tracciato originale caricato",
        ).add_to(m)
        folium.Marker(
            original_latlon[0], tooltip="Partenza tracciato originale",
            icon=folium.Icon(color="green", icon="play"),
        ).add_to(m)

    for i, (lat, lon) in enumerate(wps):
        _, color = _role_label_and_color(mode, i, len(wps))
        is_selected = (i == selected_idx)
        is_moving = is_selected and move_mode
        marker_color = "red" if is_moving else color
        border = "3px solid #222222" if (is_selected and not is_moving) else "2px solid white"
        size = 30 if is_selected else 26
        html = (
            f'<div style="background:{marker_color};color:white;border-radius:50%;'
            f'width:{size}px;height:{size}px;display:flex;align-items:center;'
            f'justify-content:center;font-weight:bold;font-size:13px;'
            f'border:{border};box-shadow:0 0 3px rgba(0,0,0,0.6);">{i + 1}</div>'
        )
        folium.Marker(
            [lat, lon],
            tooltip=f"Punto #{i + 1}",
            icon=folium.DivIcon(html=html, icon_size=(size, size), icon_anchor=(size // 2, size // 2)),
        ).add_to(m)

    map_data = st_folium(
        m, width=None, height=_MAP_HEIGHT,
        returned_objects=["last_clicked", "last_object_clicked", "last_object_clicked_tooltip"],
        key="rem_manual_splice_map", use_container_width=True,
    )

    # ── Un solo meccanismo di interazione ───────────────────────────────────────
    # Leaflet ferma la propagazione del click sui marker verso la mappa: cliccare
    # un marker aggiorna last_object_clicked(_tooltip) ma NON last_clicked, quindi
    # i due rami sotto sono già naturalmente esclusivi (nessuna logica ad hoc per
    # distinguerli) — un click su spazio vuoto non tocca mai last_object_clicked,
    # un click su marker non tocca mai last_clicked.
    obj_clicked = map_data.get("last_object_clicked") if map_data else None
    obj_tooltip = map_data.get("last_object_clicked_tooltip") if map_data else None
    plain_clicked = map_data.get("last_clicked") if map_data else None

    if not move_mode and obj_clicked and obj_tooltip:
        obj_key = (round(float(obj_clicked["lat"]), 6), round(float(obj_clicked["lng"]), 6), obj_tooltip)
        if st.session_state.get("rem_manual_last_object_click") != obj_key:
            st.session_state["rem_manual_last_object_click"] = obj_key
            match = re.match(r"^Punto #(\d+)$", obj_tooltip)
            if match:
                idx = int(match.group(1)) - 1
                if 0 <= idx < len(wps):
                    st.session_state[_SELECTED_KEY] = idx
                    st.rerun()

    if plain_clicked:
        new_c = (round(float(plain_clicked["lat"]), 6), round(float(plain_clicked["lng"]), 6))
        if st.session_state.get("rem_manual_last_click") != new_c:
            st.session_state["rem_manual_last_click"] = new_c
            if move_mode and selected_idx is not None and 0 <= selected_idx < len(wps):
                wps[selected_idx] = new_c
                st.session_state[_MOVE_MODE_KEY] = False
            else:
                insert_at = _cheapest_insert_index(wps, new_c)
                wps.insert(insert_at, new_c)
                if selected_idx is not None and insert_at <= selected_idx:
                    st.session_state[_SELECTED_KEY] = selected_idx + 1
            st.session_state[_MAP_CENTER_KEY] = new_c
            st.rerun()

    st.subheader("2. Punto selezionato")
    if selected_idx is not None and not (0 <= selected_idx < len(wps)):
        # selezione rimasta orfana (es. dopo un'eliminazione) — pulita silenziosamente
        st.session_state[_SELECTED_KEY] = None
        st.session_state[_MOVE_MODE_KEY] = False
        selected_idx = None

    if selected_idx is None:
        st.info("Nessun punto selezionato — clicca un marker sulla mappa per selezionarlo.")
    else:
        label, _ = _role_label_and_color(mode, selected_idx, len(wps))
        lat, lon = wps[selected_idx]
        # Riquadro compatto (non spalmato su tutta la pagina) — solo la colonna
        # stretta a sinistra è occupata, il resto della riga resta vuoto.
        col_box, _col_spacer = st.columns([1, 2])
        with col_box:
            with st.container(border=True):
                st.markdown(f"**Punto #{selected_idx + 1} — {label}**")
                st.caption(f"{lat:.6f}, {lon:.6f}")
                csel1, csel2 = st.columns(2)
                if csel1.button("🗑️ Elimina", use_container_width=True):
                    wps.pop(selected_idx)
                    st.session_state[_SELECTED_KEY] = None
                    st.session_state[_MOVE_MODE_KEY] = False
                    st.rerun()
                if not move_mode:
                    if csel2.button("📍 Sposta", use_container_width=True):
                        st.session_state[_MOVE_MODE_KEY] = True
                        st.rerun()
                else:
                    if csel2.button("❌ Annulla", use_container_width=True):
                        st.session_state[_MOVE_MODE_KEY] = False
                        st.rerun()

    st.subheader("3. Sequenza completa")
    if not wps:
        st.info("Nessun punto ancora — clicca sulla mappa sopra.")
    else:
        rows = []
        for i, (lat, lon) in enumerate(wps):
            label, _ = _role_label_and_color(mode, i, len(wps))
            rows.append({"#": i + 1, "ruolo": label, "lat": round(lat, 6), "lon": round(lon, 6)})
        st.dataframe(rows, use_container_width=True, hide_index=True)

        if st.button("↩️ Svuota tutti i punti"):
            st.session_state["rem_manual_waypoints"] = []
            st.session_state["rem_manual_result"] = None
            st.session_state[_SELECTED_KEY] = None
            st.session_state[_MOVE_MODE_KEY] = False
            st.rerun()

    st.divider()
    st.subheader("4. Genera")
    profile = st.selectbox(
        "Profilo BRouter", _PROFILE_OPTIONS,
        index=_PROFILE_OPTIONS.index(_DEFAULT_PROFILE),
        key="rem_manual_splice_profile",
    )
    can_generate = len(wps) >= 2
    gen_label = "🚴 Genera percorso da zero" if mode == "generazione" else "🚴 Genera tratto (BRouter) e fondi con l'originale"
    if st.button(gen_label, disabled=not can_generate):
        with st.spinner("Chiamo BRouter..."):
            try:
                if mode == "generazione":
                    out_path = _OUT_DIR / "scratch_result.gpx"
                    st.session_state["rem_manual_result"] = generate_from_scratch(list(wps), str(out_path), profile=profile)
                else:
                    out_path = _OUT_DIR / "spliced_result.gpx"
                    st.session_state["rem_manual_result"] = splice_in_segment(
                        active_gpx_path, list(wps), str(out_path), profile=profile,
                    )
            except Exception as exc:
                st.session_state["rem_manual_result"] = {"error": str(exc)}
        st.session_state["rem_manual_climb_analysis"] = None  # risultato precedente non più valido
        st.rerun()
    if not can_generate:
        hint = "Servono almeno 2 waypoint." if mode == "generazione" else \
            "Servono almeno 2 waypoint (il primo diventa il distacco, l'ultimo il raccordo)."
        st.caption(hint)

    result = st.session_state["rem_manual_result"]
    if result:
        if "error" in result:
            st.error(f"Generazione fallita: {result['error']}")
        elif mode == "generazione":
            st.success(f"Percorso generato: {result['n_points']} punti, {result['distance_km']} km.")
            _, pts_obj = _load_single_segment_gpx(result["out_path"])
            latlon = [(p.latitude, p.longitude) for p in pts_obj]

            st.subheader("5. Percorso generato")
            m2 = folium.Map(location=latlon[0], zoom_start=13, scrollWheelZoom=False)
            folium.PolyLine(latlon, color="red", weight=4, opacity=0.9, tooltip="Percorso generato (BRouter)").add_to(m2)
            for lat, lon in wps:
                folium.CircleMarker([lat, lon], radius=5, color="black", fill=True, fill_opacity=0.8).add_to(m2)
            m2.fit_bounds(latlon)
            st_folium(m2, width=None, height=_MAP_HEIGHT, key="rem_manual_scratch_result_map", use_container_width=True, returned_objects=[])
        else:
            st.success(
                f"Tratto generato e fuso: {result['n_brouter_points']} punti BRouter inseriti "
                f"al posto dell'intervallo originale [{result['splice_lo_idx']}, {result['splice_hi_idx']}] "
                f"— {result['n_final_points']} punti totali nel risultato."
            )
            if result["reversed_order"]:
                st.caption(
                    "Nota: l'ultimo click è risultato più vicino a un punto ANTECEDENTE (nel tracciato "
                    "originale) rispetto al primo click — ordine di marcia invertito automaticamente "
                    "per generare un tratto coerente con la direzione del tracciato."
                )
            with st.expander("Dettagli tecnici"):
                st.json(result)

            st.subheader("5. Tracciato risultante")
            st.caption("Blu = tracciato originale (invariato prima/dopo) — rosso = nuovo tratto generato.")

            _, final_points_obj = _load_single_segment_gpx(result["out_path"])
            final_latlon = [(p.latitude, p.longitude) for p in final_points_obj]

            lo = result["splice_lo_idx"]
            hi = lo + result["n_brouter_points"] - 1
            before_seg = final_latlon[:lo + 1]
            new_seg = final_latlon[lo:hi + 1]
            after_seg = final_latlon[hi:]

            m2 = folium.Map(location=final_latlon[0], zoom_start=13, scrollWheelZoom=False)
            if before_seg:
                folium.PolyLine(before_seg, color="blue", weight=3, opacity=0.6, tooltip="Originale (prima)").add_to(m2)
            folium.PolyLine(new_seg, color="red", weight=4, opacity=0.9, tooltip="Nuovo tratto (BRouter)").add_to(m2)
            if after_seg:
                folium.PolyLine(after_seg, color="blue", weight=3, opacity=0.6, tooltip="Originale (dopo)").add_to(m2)
            for lat, lon in wps:
                folium.CircleMarker([lat, lon], radius=5, color="black", fill=True, fill_opacity=0.8).add_to(m2)
            m2.fit_bounds(final_latlon)

            st_folium(m2, width=None, height=_MAP_HEIGHT, key="rem_manual_splice_result_map", use_container_width=True, returned_objects=[])

        if "error" not in result:
            st.divider()
            with st.expander("📊 Analisi salite ed energia (a richiesta)", expanded=False):
                st.caption(
                    "Calcolo non automatico (bottone esplicito) — evita di ricalcolare "
                    "ad ogni rerun. Riusa detect_climbs/render_climb_chart_html/"
                    "geocode_climbs così come sono, stessa logica del resto dell'app. "
                    "L'analisi energetica deterministica "
                    "(ride_analysis_agent.compute_deterministic_energy_analysis) "
                    "richiede dati reali di consumo batteria da una ride già "
                    "registrata — non disponibili qui (stiamo generando/modificando "
                    "un percorso, non analizzando una ride percorsa), quindi non "
                    "applicabile in questo contesto: non mostrata."
                )
                if st.button("Calcola analisi salite"):
                    with st.spinner("Calcolo salite (+ geocodifica zone)..."):
                        analysis_new = analyze_gpx(result["out_path"], route_type="loop")
                        geocode_climbs(analysis_new["climbs"], context="Route Editor manuale — risultato")
                        payload = {"new": analysis_new}
                        if mode == "modifica":
                            analysis_orig = analyze_gpx(active_gpx_path, route_type="loop")
                            geocode_climbs(analysis_orig["climbs"], context="Route Editor manuale — originale")
                            payload["orig"] = analysis_orig
                        st.session_state["rem_manual_climb_analysis"] = payload
                    st.rerun()

                climb_payload = st.session_state.get("rem_manual_climb_analysis")
                if climb_payload:
                    has_orig = "orig" in climb_payload
                    if has_orig:
                        st.markdown("#### Percorso originale")
                        a = climb_payload["orig"]
                        _render_climb_chart_embed(a["climbs"], a["distance_km"], a["elevation_profile"], title="Originale")

                    st.markdown("#### Percorso modificato" if has_orig else "#### Percorso generato")
                    b = climb_payload["new"]
                    _render_climb_chart_embed(
                        b["climbs"], b["distance_km"], b["elevation_profile"],
                        title="Modificato" if has_orig else "Generato",
                    )

                    if has_orig:
                        st.markdown("#### Confronto")
                        a_max = max((c["max_200m_pct"] for c in a["climbs"]), default=0.0)
                        b_max = max((c["max_200m_pct"] for c in b["climbs"]), default=0.0)
                        rows = [
                            {"metrica": "Distanza (km)", "originale": a["distance_km"], "modificato": b["distance_km"]},
                            {"metrica": "Dislivello + (m)", "originale": a["elevation_gain_m"], "modificato": b["elevation_gain_m"]},
                            {"metrica": "Dislivello - (m)", "originale": a["elevation_loss_m"], "modificato": b["elevation_loss_m"]},
                            {"metrica": "N. salite", "originale": len(a["climbs"]), "modificato": len(b["climbs"])},
                            {"metrica": "Pendenza max 200m (%)", "originale": round(a_max, 1), "modificato": round(b_max, 1)},
                        ]
                        st.dataframe(rows, use_container_width=True, hide_index=True)

    # Per il chiamante (tab "Manual" di main.py, che deve poter offrire il
    # salvataggio subito dopo aver invocato questa funzione) — lo script
    # standalone ignora semplicemente il valore di ritorno.
    return {
        "mode": mode,
        "active_gpx_path": active_gpx_path,
        "waypoints": list(wps),
        "profile": profile,
        "result": result,
    }


if __name__ == "__main__":
    render_standalone_app()
