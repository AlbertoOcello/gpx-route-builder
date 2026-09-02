"""
Route Editor Agent — strumento standalone di validazione visiva per gli
ANCHOR trovati dal map-matching (route_editor_agent.py / map_matcher.py).

NON integrato in main.py — script Streamlit isolato, coerente con come è
stato sviluppato tutto il resto di questa funzionalità finora (Stadio 1
testato in isolamento, mai wired nell'app principale). Nessun import da
main.py: importarlo eseguirebbe l'intera app Streamlit principale come
effetto collaterale (i suoi tab, il suo st.set_page_config, ecc.) — la
mappa e il pattern click→coordinata sono reimplementati qui in autonomia,
stesse librerie (folium/streamlit-folium) e stessa convenzione visiva già
in uso nell'app (blu = tracciato, marker colorati per ruolo), stesso
meccanismo di correzione via click già usato per la geolocalizzazione nel
Planner (main.py, tab Planner → "Geolocalizza": st_folium con
returned_objects=["last_clicked"], dedup su session_state, rerun sul nuovo
click) — concetto riusato, non una seconda libreria di mappe.

Uso:
    streamlit run app/route_editor_validation_tool.py

Perché una cache su disco: la pipeline reale (parse_edit_intent) chiama LLM
+ Overpass + geocodifica, ~90-100s misurati — ripeterla ad ogni rerun di
Streamlit (che accade ad ogni interazione UI, incluso un click sulla mappa
per correggere un ANCHOR) sarebbe impraticabile per uno strumento di
ispezione interattiva. La pipeline gira una volta, il risultato è cachato
su disco; un bottone esplicito la rilancia quando serve una run fresca.

Nessuna generazione BRouter/candidati (Stadio 2) — solo ispezione/
correzione manuale degli ANCHOR trovati allo Stadio 1.
"""
from __future__ import annotations

import pickle
import tempfile
from datetime import datetime
from pathlib import Path

import folium
import streamlit as st
from streamlit_folium import st_folium

from route_editor_agent import parse_edit_intent

GPX_PATH = "/Users/albertoocello/Downloads/Panoramica_Alta_D2-Pre.gpx"
INSTRUCTIONS = """Allegato il gpx... le modifiche seguenti:
1. Vorrei realizzare una route che sia un po più lunga fino a circa 45 Km e che allunghi quella allegata nella parte centrale.
2. Arrivati a montemarciano marina e percorrendo la via brecciate, invece di girare verso montemarciano, continuare per la via brecciata fino a raggiungere la via Alberici. Qui girare verso Alberici.
3. Da qui proseguire verso Monte San Vito e poi proseguire verso Morro D'Alba. Qui ci sono due possibilità: 3a. Via santa Lucia, via Marsciano sulla SP13 3b. Via santi neri, via santa maria del fiore verso Santa Maria del Fiore appunto. Attraversare Santa Maria del fiore arrivando a Morro D'Alba e quindi sulla SP13
4. Sulla Sp13 scendere verso il Grottino, superare il grottino e raccordarsi sulla SP13 al percorso precedente.
la scelta tra 3a e 3b va fatta considerando eventuali strappi in salita sopra il 10% e il kilometraggio totale e il traffico sulla SP13."""

_CACHE_PATH = Path(tempfile.gettempdir()) / "gpxrb_route_editor_validation_cache.pkl"

_ROLE_COLORS = {"distacco": "orange", "raccordo": "purple", "geocodifica": "gray"}


st.set_page_config(page_title="Route Editor — Validazione ANCHOR", layout="wide")
st.title("🔍 Route Editor Agent — Validazione visiva ANCHOR")
st.caption(
    "Strumento standalone di test, NON integrato in main.py. Stadio 1 (Intent Parser) soltanto — "
    "nessuna generazione BRouter/candidati (Stadio 2)."
)

with st.expander("Testo dell'istruzione usata per il caso di test", expanded=False):
    st.text(INSTRUCTIONS)


def _run_pipeline() -> dict:
    with st.spinner("Eseguo l'Intent Parser (LLM + geocodifica fuzzy + map-matching, ~90-100s)..."):
        result = parse_edit_intent(GPX_PATH, INSTRUCTIONS)
    with open(_CACHE_PATH, "wb") as f:
        pickle.dump(result, f)
    return result


col_run1, col_run2 = st.columns([1, 3])
with col_run1:
    force_rerun = st.button("🔄 Rilancia pipeline (LLM+Overpass, ~90s)")

if force_rerun:
    st.session_state["pipeline_output"] = _run_pipeline()
elif "pipeline_output" not in st.session_state:
    if _CACHE_PATH.exists():
        with open(_CACHE_PATH, "rb") as f:
            st.session_state["pipeline_output"] = pickle.load(f)
        with col_run2:
            st.caption(
                f"Risultato in cache da {datetime.fromtimestamp(_CACHE_PATH.stat().st_mtime):%Y-%m-%d %H:%M:%S} "
                "— usa 'Rilancia pipeline' per una run fresca."
            )
    else:
        st.session_state["pipeline_output"] = _run_pipeline()

output = st.session_state["pipeline_output"]

if output["validation_error"]:
    st.error(f"Validazione Intent Parser fallita: {output['validation_error']}")
    st.stop()

result = output["result"]
gpx_facts = output["gpx_facts"]
anchors = [(i, wp) for i, wp in enumerate(result.waypoints) if wp.role == "ANCHOR"]

st.subheader(f"Mappa — tracciato originale + {len(anchors)} ANCHOR trovati")

start_lat, start_lon = gpx_facts["start"]
m = folium.Map(location=[start_lat, start_lon], zoom_start=13, scrollWheelZoom=True)
folium.PolyLine(
    gpx_facts["points"], color="blue", weight=3, opacity=0.75,
    tooltip="Tracciato originale (Panoramica_Alta_D2-Pre.gpx)",
).add_to(m)
folium.Marker(
    gpx_facts["points"][0], tooltip="Partenza", icon=folium.Icon(color="green", icon="play"),
).add_to(m)

# Inquadra automaticamente partenza + tutti gli ANCHOR trovati — senza,
# la mappa si centra solo sulla partenza e gli ANCHOR (spesso a diversi km)
# restano fuori dalla vista iniziale, richiedendo pan/zoom manuale.
_bounds_points = [gpx_facts["points"][0]]


def _popup_for_anchor(wp, am: dict) -> tuple[str, str]:
    """(kind, html) — kind è "distacco" o "raccordo", dedotto dalle chiavi presenti in am."""
    if "from_road" in am:
        dc = am.get("direction_check") or {}
        if dc:
            direction_html = (
                f"{'✅ coerente' if dc.get('consistent') else '⚠️ NON coerente'} "
                f"(atteso {dc.get('expected_bearing_deg', '?')}°, "
                f"reale {dc.get('actual_bearing_deg', '?')}°, "
                f"diff {dc.get('diff_deg', '?')}°)"
            )
        else:
            direction_html = "non verificata (nessun toward_place)"
        html = (
            f"<b>ANCHOR di DISTACCO — {wp.name}</b><br>"
            f"Da: <b>{am['from_road']}</b> → A: <b>{am['to_road']}</b><br>"
            f"Coordinate: {am['anchor_lat']:.6f}, {am['anchor_lon']:.6f}<br>"
            f"Interpolazione: {am.get('interpolation')}<br>"
            f"gpx_index: {am['gpx_index_before']} → {am['gpx_index_after']}<br>"
            f"Direzione: {direction_html}"
        )
        return "distacco", html

    mismatch = am.get("road_name_mismatch")
    mismatch_html = (
        f"<br>⚠️ <b>Discrepanza nome via</b>: atteso '{mismatch['expected']}', "
        f"trovato '{mismatch['found']}'"
        if mismatch else "<br>✅ Nome via confermato (nessuna discrepanza)"
    )
    html = (
        f"<b>ANCHOR di RACCORDO — {wp.name}</b><br>"
        f"Via trovata: <b>{am.get('road')}</b><br>"
        f"Coordinate: {am['anchor_lat']:.6f}, {am['anchor_lon']:.6f}<br>"
        f"gpx_index: {am.get('gpx_index')}<br>"
        f"Distanza dal riferimento geocodificato: {am.get('distance_to_reference_m')} m"
        f"{mismatch_html}"
    )
    return "raccordo", html


anchor_options = ["(nessuno)"]
for i, wp in anchors:
    am = wp.anchor_match
    override = st.session_state.get(f"anchor_override_{i}")

    if am and am.get("resolved"):
        kind, popup_html = _popup_for_anchor(wp, am)
        lat, lon = am["anchor_lat"], am["anchor_lon"]
        folium.Marker(
            [lat, lon],
            tooltip=f"{kind.upper()} — {wp.name}",
            popup=folium.Popup(popup_html, max_width=340),
            icon=folium.Icon(color=_ROLE_COLORS[kind], icon="info-sign"),
        ).add_to(m)
        _bounds_points.append((lat, lon))
    else:
        kind = "geocodifica"
        err = (am or {}).get("error") if am else "nessun pattern map-matching riconosciuto"
        popup_html = (
            f"<b>ANCHOR (fallback geocodifica) — {wp.name}</b><br>"
            f"Map-matching non risolto: {err}<br>"
            f"Candidati geocodificati: {len(wp.candidates)}"
        )
        if wp.candidates:
            c = wp.candidates[0]
            lat, lon = c.lat, c.lon
            folium.Marker(
                [lat, lon],
                tooltip=f"GEOCODIFICA (non map-matching) — {wp.name}",
                popup=folium.Popup(popup_html, max_width=340),
                icon=folium.Icon(color=_ROLE_COLORS[kind], icon="question-sign"),
            ).add_to(m)
            _bounds_points.append((lat, lon))

    if override:
        folium.Marker(
            list(override),
            tooltip=f"Correzione manuale — {wp.name}",
            popup=f"Posizione corretta manualmente per: {wp.name}",
            icon=folium.Icon(color="red", icon="crosshairs", prefix="fa"),
        ).add_to(m)
        _bounds_points.append(tuple(override))

    anchor_options.append(f"{i}: {wp.name} ({wp.role})")

if len(_bounds_points) > 1:
    m.fit_bounds(_bounds_points, padding=(30, 30))

map_data = st_folium(
    m, width=None, height=600, returned_objects=["last_clicked"],
    key="validation_map", use_container_width=True,
)

st.divider()
st.subheader("Correggi manualmente un ANCHOR")
st.caption(
    "Clicca un punto sulla mappa sopra, poi seleziona a quale ANCHOR assegnarlo come correzione "
    "manuale — stesso meccanismo click→coordinata già usato per la geolocalizzazione nel Planner."
)
sel = st.selectbox("ANCHOR da correggere con l'ultimo punto cliccato", anchor_options, key="anchor_to_correct")

clicked = map_data.get("last_clicked") if map_data else None
if clicked and sel != "(nessuno)":
    idx = int(sel.split(":")[0])
    new_c = (round(float(clicked["lat"]), 6), round(float(clicked["lng"]), 6))
    if st.session_state.get(f"anchor_override_{idx}") != new_c:
        st.session_state[f"anchor_override_{idx}"] = new_c
        st.rerun()

if st.button("↩️ Azzera tutte le correzioni manuali"):
    for i, _ in anchors:
        st.session_state.pop(f"anchor_override_{i}", None)
    st.rerun()

st.divider()
st.subheader("Riepilogo ANCHOR")
rows = []
for i, wp in anchors:
    am = wp.anchor_match
    override = st.session_state.get(f"anchor_override_{i}")
    resolved = bool(am and am.get("resolved"))
    rows.append({
        "idx": i,
        "name": wp.name,
        "pattern": "distacco" if wp.leaving_road else ("raccordo" if wp.rejoin_road else "—"),
        "risolto (map-matching)": "✅" if resolved else "❌",
        "coordinate trovate": f"{am['anchor_lat']:.6f}, {am['anchor_lon']:.6f}" if resolved else "—",
        "discrepanza nome via": (
            f"atteso {am['road_name_mismatch']['expected']!r}, trovato {am['road_name_mismatch']['found']!r}"
            if resolved and am.get("road_name_mismatch") else ("—" if resolved else "n/d")
        ),
        "correzione manuale": f"{override[0]:.6f}, {override[1]:.6f}" if override else "—",
    })
st.dataframe(rows, use_container_width=True, hide_index=True)
