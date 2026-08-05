"""
Candidate Generator (Fase 7) — chiama BRouter per ogni CandidateRoute (SRS §6.3).

Pipeline per ogni slot (A/B/C):
  1. _apply_loop_fix  — sovrascrive end con coordinate bit-identiche a start (loop)
  2. _waypoints_to_lonlat — converte in lista (lon, lat) per BRouter
  3. brouter_client.get_route — genera il GPX reale
  4. gpx_analyzer.analyze_gpx — distanza, dislivello, loop_closed, endpoint_match
  5. In caso di errore: un retry automatico via Planner + Geocoding Agent (SRS §6.3)
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

import gpxpy
from geopy.distance import geodesic

from brouter_client import get_route
from gpx_analyzer import analyze_gpx, cut_out_and_back_in_gpx, gpx_creator_string
from area_resolver import OverpassUnavailable, resolve_area_traversal, snap_to_nearest_road

log = logging.getLogger(__name__)

_ROUTES_DIR = Path(__file__).parent.parent / "routes" / "generated"
_SLOT_LABELS = ["A", "B", "C"]


# ─── Traversal expansion ──────────────────────────────────────────────────────

def _expand_traversal_waypoints(strategy: dict) -> dict:
    """
    Sostituisce ogni waypoint via con traversal=True con la sequenza di nodi
    del sentiero OSM reale trovata dall'Area Resolver.

    Se l'Area Resolver non trova sentieri, il waypoint originale è mantenuto.
    Require che il Geocoding Agent abbia già popolato lat/lon.
    """
    new_waypoints = []
    for wp in strategy["waypoints"]:
        if wp.get("role") != "via" or not wp.get("traversal"):
            new_waypoints.append(wp)
            continue

        lat = wp.get("lat")
        lon = wp.get("lon")
        area_hint = wp.get("area_hint") or wp.get("name", "")

        if lat is None or lon is None:
            log.warning("Traversal waypoint '%s' senza coordinate — skip espansione", wp.get("name"))
            new_waypoints.append(wp)
            continue

        log.info("Traversal: risolvo sentieri per '%s' intorno a (%.4f, %.4f)", area_hint, lat, lon)
        trail_pts = resolve_area_traversal(area_hint, (lat, lon))

        if not trail_pts:
            log.info("Traversal: nessun sentiero trovato per '%s' — uso waypoint originale", area_hint)
            new_waypoints.append(wp)
        else:
            log.info(
                "Traversal: %d nodi trovati per '%s' → espando waypoint",
                len(trail_pts), area_hint,
            )
            for i, (t_lat, t_lon) in enumerate(trail_pts):
                new_waypoints.append({
                    "role": "via",
                    "name": f"{area_hint} [{i + 1}/{len(trail_pts)}]",
                    "lat": t_lat,
                    "lon": t_lon,
                    "needs_geocoding": False,
                    "traversal": False,
                    "area_hint": None,
                    "_traversal_expanded": True,
                })

    return {**strategy, "waypoints": new_waypoints}


# ─── Loop fix ─────────────────────────────────────────────────────────────────

def _apply_loop_fix(strategy: dict) -> dict:
    """
    REGOLA LOOP obbligatoria (SRS §6.3) — fonte di verità prima di chiamare BRouter.

    Per route_type == "loop": copia lat/lon del waypoint "start" sul waypoint "end",
    rendendoli bit-identici. Nominatim restituisce spesso centroidi diversi per la
    stessa stringa (es. "Senigallia" geocodificato due volte → due coordinate diverse),
    il che causerebbe un anello aperto. BRouter richiede start == end per chiuderlo.

    Questo fix è responsabilità del Candidate Generator, non del Geocoding Agent.
    """
    if strategy["route_type"] != "loop":
        return strategy

    waypoints = list(strategy["waypoints"])
    start_wp = next((w for w in waypoints if w["role"] == "start"), None)
    end_idx  = next((i for i, w in enumerate(waypoints) if w["role"] == "end"), None)

    if start_wp is None or end_idx is None:
        log.warning("LOOP FIX: start o end mancante in '%s', skip.", strategy["name"])
        return strategy

    old_end = waypoints[end_idx]
    if old_end["lat"] != start_wp["lat"] or old_end["lon"] != start_wp["lon"]:
        log.info(
            "LOOP FIX [%s]: end (%.6f, %.6f) → (%.6f, %.6f)  [copiato da start]",
            strategy["name"],
            old_end["lat"], old_end["lon"],
            start_wp["lat"], start_wp["lon"],
        )
        waypoints[end_idx] = {**old_end, "lat": start_wp["lat"], "lon": start_wp["lon"]}
    else:
        log.info("LOOP FIX [%s]: start == end già identici, nessuna modifica.", strategy["name"])

    return {**strategy, "waypoints": waypoints}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_soft_via(wp: dict) -> bool:
    return wp.get("role") == "via" and not wp.get("mandatory")


def _snap_key(wp: dict) -> tuple:
    """Chiave stabile per un waypoint soft, indipendente dalla posizione nella
    lista — robusta anche se un'altra strategia espande un waypoint traversal
    altrove nella sequenza (shiftando gli indici)."""
    return (wp.get("name"), wp.get("lat"), wp.get("lon"))


def _resolve_soft_snaps(
    waypoints: list[dict],
    on_progress: Callable[[str], None] | None = None,
) -> dict[tuple, dict]:
    """
    Risolve UNA SOLA VOLTA, per l'intera generazione (condivisa tra i 3
    candidati A/B/C), lo snap-to-road di ogni waypoint "via" soft.

    Prima di questo fix, _waypoints_to_lonlat chiamava snap_to_nearest_road
    separatamente per ciascuno dei 3 candidati — 3 query Overpass indipendenti
    per la STESSA coordinata. Sotto stress del servizio (429/504, osservato in
    produzione su "Polverigi_5"), una chiamata poteva riuscire e un'altra no in
    modo non deterministico, producendo liste di via-point diverse tra A/B/C
    per lo stesso waypoint e percorsi imprevedibili. Risolvendo una volta sola
    e condividendo il risultato, i 3 candidati usano sempre lo stesso set.

    Ritorna un dict {chiave(_snap_key) → dict}. Il dict di ogni waypoint è uno di:
      {"lat","lon", "source": "overpass"}          — snap a strada reale riuscito
      {"lat","lon", "source": "brouter_raw_fallback"} — Overpass indisponibile,
        coordinata geocodificata grezza passata a BRouter (che fa comunque il suo
        snap nativo al nodo instradabile più vicino — qualità inferiore ma il
        waypoint resta incluso invece di sparire dal percorso)
      {"excluded": "no_road" | "no_coords"}        — nessuna strada nel raggio
        (esito geografico legittimo di Overpass) o coordinate mancanti
    """
    def _emit(message: str) -> None:
        if on_progress:
            try:
                on_progress(message)
            except Exception:
                pass

    snaps: dict[tuple, dict] = {}
    for wp in waypoints:
        if not _is_soft_via(wp):
            continue
        key = _snap_key(wp)
        if key in snaps:
            continue  # stesso waypoint (nome+coordinate) già risolto

        lat, lon = wp.get("lat"), wp.get("lon")
        if lat is None or lon is None:
            log.warning("Waypoint soft '%s' senza coordinate — escluso.", wp.get("name"))
            snaps[key] = {"excluded": "no_coords"}
            continue

        _emit(f"🔎 Attaching waypoint '{wp.get('name')}'...")
        try:
            snapped = snap_to_nearest_road(float(lat), float(lon), on_progress=on_progress)
        except OverpassUnavailable as exc:
            # Fallback: Overpass è indisponibile ma BRouter è locale (tile .rd5
            # già scaricate, nessuna dipendenza di rete per il routing) — passargli
            # la coordinata geocodificata grezza come via-point normale gli basta
            # per fare il proprio snap nativo al nodo instradabile più vicino.
            # Qualità inferiore allo snap "a strada di passaggio vera" di Overpass
            # (può finire su una via secondaria vicino al centro), ma sempre
            # meglio di far sparire del tutto il waypoint dal percorso.
            log.warning(
                "Waypoint soft '%s' — Overpass non disponibile (%s): incluso con "
                "aggancio grezzo via BRouter diretto (coordinata geocodificata "
                "originale, non pre-snappata a una strada reale) invece di essere escluso.",
                wp.get("name"), exc,
            )
            snaps[key] = {"lat": float(lat), "lon": float(lon), "source": "brouter_raw_fallback"}
            continue

        if snapped is None:
            log.warning(
                "Waypoint soft '%s' — nessuna strada trovata nel raggio, escluso "
                "dalla chiamata BRouter.", wp.get("name"),
            )
            snaps[key] = {"excluded": "no_road"}
            continue

        snap_lat, snap_lon = snapped
        snap_dist_m = geodesic((lat, lon), (snap_lat, snap_lon)).meters
        log.info(
            "Waypoint soft '%s' agganciato a strada: (%.6f,%.6f) → (%.6f,%.6f)  [%.0f m dall'originale]",
            wp.get("name"), lat, lon, snap_lat, snap_lon, snap_dist_m,
        )
        snaps[key] = {"lat": snap_lat, "lon": snap_lon, "source": "overpass"}

    return snaps


def _waypoints_to_lonlat(strategy: dict, snaps: dict[tuple, dict]) -> list[tuple[float, float]]:
    """
    Converte i waypoint (già in ordine start→via…→end) in lista (lon, lat) per BRouter.
    Solleva ValueError se un waypoint incluso non ha coordinate (geocoding incompleto).

    Start ed end sono sempre inclusi. Un waypoint "via" con mandatory non True è
    "soft" — a prescindere da chi lo ha proposto (utente o Planner AI): è un
    riferimento di zona, non un punto che BRouter deve toccare esattamente. Lo
    snap alla strada più vicina è già stato risolto una volta sola per l'intera
    generazione da _resolve_soft_snaps (condiviso tra i 3 candidati) — qui si
    legge solo il risultato precomputato.
    """
    result = []
    for wp in strategy["waypoints"]:
        if _is_soft_via(wp):
            snap = snaps.get(_snap_key(wp))
            if not snap or "excluded" in snap:
                continue
            result.append((snap["lon"], snap["lat"]))
            continue
        if wp.get("lat") is None or wp.get("lon") is None:
            raise ValueError(
                f"Waypoint '{wp.get('name')}' ({wp.get('role')}) manca di coordinate — "
                "verificare che il Geocoding Agent sia stato eseguito correttamente."
            )
        result.append((float(wp["lon"]), float(wp["lat"])))
    return result


# Tolleranza per abbinare l'apice di uno spuntone andata/ritorno (vedi
# gpx_analyzer._detect_out_and_back) al via-point che lo ha causato — l'apice
# di uno spuntone è quasi sempre il punto stesso che BRouter doveva toccare
# (diagnosi "Polverigi_9": Agugliano e Polverigi trovati a 0-27m dall'apice).
_OAB_ATTRIBUTION_TOLERANCE_M = 150.0


def _named_via_points(strategy: dict, snaps: dict[tuple, dict]) -> list[tuple[str, float, float, bool]]:
    """
    Stessa logica di inclusione/esclusione di _waypoints_to_lonlat (coordinata
    post-snap per i soft, esatta per i mandatory/start/end), ma conserva anche
    nome e "protezione" (mandatory=True per tutto ciò che non è un via soft:
    start, end, via mandatory, nodi da traversal — l'utente li ha chiesti
    esplicitamente, mai da tagliare in Fix A). Usata per l'attribuzione
    spuntone→waypoint (Fix 2) e per decidere cosa proteggere dal taglio
    automatico (Fix A) — non per la chiamata BRouter.
    """
    result = []
    for wp in strategy["waypoints"]:
        if _is_soft_via(wp):
            snap = snaps.get(_snap_key(wp))
            if not snap or "excluded" in snap:
                continue
            result.append((wp.get("name"), snap["lat"], snap["lon"], False))
            continue
        if wp.get("lat") is None or wp.get("lon") is None:
            continue
        result.append((wp.get("name"), float(wp["lat"]), float(wp["lon"]), True))
    return result


def _attribute_out_and_back(
    apexes: list[dict],
    named_via_points: list[tuple[str, float, float, bool]],
) -> list[dict]:
    """
    Per ciascun apice di spuntone (gpx_analyzer._detect_out_and_back) trova il
    via-point post-snap più vicino entro _OAB_ATTRIBUTION_TOLERANCE_M. Se nessun
    via-point è abbastanza vicino, l'apice resta senza attribuzione (probabilmente
    non causato da un singolo waypoint isolato, es. un vincolo della rete stradale
    tra due punti lontani da qualunque via-point).

    Ritorna una lista di {"waypoint_name", "overlap_km"}.
    """
    attributions = []
    for apex in apexes:
        best_name, best_dist = None, float("inf")
        for name, lat, lon, _mandatory in named_via_points:
            d = geodesic((apex["apex_lat"], apex["apex_lon"]), (lat, lon)).meters
            if d < best_dist:
                best_dist = d
                best_name = name
        if best_name is not None and best_dist <= _OAB_ATTRIBUTION_TOLERANCE_M:
            attributions.append({"waypoint_name": best_name, "overlap_km": apex["overlap_km"]})
    return attributions


def _protected_points(named_via_points: list[tuple[str, float, float, bool]]) -> list[tuple[float, float]]:
    """Coordinate dei via-point mandatory/start/end — mai tagliabili da Fix A."""
    return [(lat, lon) for _name, lat, lon, mandatory in named_via_points if mandatory]


def _attribute_cuts(
    cuts: list[dict],
    named_via_points: list[tuple[str, float, float, bool]],
) -> list[dict]:
    """
    Come _attribute_out_and_back, ma per i tagli EFFETTIVAMENTE rimossi da Fix A
    (cut_out_and_back_in_gpx) — usata solo per capire, a scopo di notifica UI,
    se un waypoint soft è stato "perso" (la sua unica via d'accesso era proprio
    la deviazione tagliata). Se un taglio non è abbastanza vicino a nessun
    via-point (es. causa esterna, non un singolo waypoint) resta senza nome:
    la nota informativa in quel caso resta generica.
    """
    attributions = []
    for cut in cuts:
        best_name, best_dist = None, float("inf")
        for name, lat, lon, mandatory in named_via_points:
            if mandatory:
                continue  # i mandatory non vengono mai tagliati, non possono essere "persi"
            d = geodesic((cut["apex_lat"], cut["apex_lon"]), (lat, lon)).meters
            if d < best_dist:
                best_dist = d
                best_name = name
        attributions.append({
            "waypoint_name": best_name if best_dist <= _OAB_ATTRIBUTION_TOLERANCE_M else None,
            "overlap_km": cut["overlap_km"],
        })
    return attributions


def _expected_end(strategy: dict) -> tuple[float, float] | None:
    """Ritorna (lon, lat) dell'end atteso — solo per gpx_analyzer in point_to_point."""
    if strategy["route_type"] != "point_to_point":
        return None
    end = next((w for w in strategy["waypoints"] if w["role"] == "end"), None)
    return (float(end["lon"]), float(end["lat"])) if end else None


def _set_gpx_name(gpx_path: Path, strategy: dict, label: str) -> None:
    """Il Garmin Edge 840 legge il nome route da <trk><name>, non dal filename."""
    name = strategy.get("name") or f"Route {label}"
    try:
        with open(gpx_path, "r", encoding="utf-8") as f:
            gpx = gpxpy.parse(f)

        gpx.name = name
        for track in gpx.tracks:
            track.name = name
        gpx.creator = gpx_creator_string()

        with open(gpx_path, "w", encoding="utf-8") as f:
            f.write(gpx.to_xml())
    except Exception as exc:
        log.warning("Impossibile impostare il nome GPX per '%s': %s", gpx_path, exc)


def _log_brouter_call(strategy: dict, lonlat: list[tuple[float, float]]) -> None:
    """Stampa coordinate start/end passate a BRouter — per verifica loop fix."""
    start_c = lonlat[0]
    end_c   = lonlat[-1]
    match   = "IDENTICHE ✓" if start_c == end_c else f"DIVERSE ✗  Δ=({end_c[1]-start_c[1]:.6f}, {end_c[0]-start_c[0]:.6f})"
    log.info(
        "→ BRouter [%s] profilo=%s  wp=%d  start=(%.6f,%.6f)  end=(%.6f,%.6f)  %s",
        strategy["name"], strategy["profile"], len(lonlat),
        start_c[1], start_c[0],
        end_c[1], end_c[0],
        match,
    )


# ─── Retry via Planner Agent ──────────────────────────────────────────────────

def _get_alternative(failed_strategy: dict, request: dict) -> dict | None:
    """
    Chiede al Planner una nuova terna di strategie e restituisce quella
    con profilo diverso dal fallito (oppure la prima disponibile).
    Geocodifica il risultato prima di restituirlo.
    """
    from planner_agent import generate_strategies
    from geocoding_agent import geocode_candidate

    log.info("Retry: richiedo strategia alternativa al Planner per slot fallito...")
    try:
        candidates = generate_strategies(request)
    except Exception as exc:
        log.error("Retry Planner fallito: %s", exc)
        return None

    failed_profile = failed_strategy.get("profile")
    alt = next((c for c in candidates if c["profile"] != failed_profile), candidates[0])
    log.info("Retry: alternativa scelta → '%s' (profilo=%s)", alt["name"], alt["profile"])
    return geocode_candidate(alt)


# ─── Core ─────────────────────────────────────────────────────────────────────

def _generate_one(
    strategy: dict,
    label: str,
    run_dir: Path,
    request: dict | None,
    attempt: int,
    snaps: dict[tuple, dict],
    snap_issues_summary: dict,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    def _emit(message: str) -> None:
        if on_progress:
            try:
                on_progress(message)
            except Exception:
                pass

    # 1a. Espandi waypoint traversal con sentieri OSM reali (se presenti)
    if any(w.get("traversal") for w in strategy.get("waypoints", [])):
        strategy = _expand_traversal_waypoints(strategy)

    # 1b. Applica loop fix (fonte di verità finale prima di BRouter)
    strategy = _apply_loop_fix(strategy)

    lonlat   = _waypoints_to_lonlat(strategy, snaps)
    gpx_path = run_dir / f"candidate_{label}.gpx"

    _log_brouter_call(strategy, lonlat)

    _emit(f"🚴 Generating Variant {label} ({strategy['profile']}) — calling BRouter...")

    try:
        # 2. Chiama BRouter
        get_route(lonlat, profile=strategy["profile"], output_path=str(gpx_path))

        # 2b. Imposta il nome della route nel GPX (BRouter scrive un default fisso)
        _set_gpx_name(gpx_path, strategy, label)

        # 2c. Fix A: taglia le deviazioni andata/ritorno non protette PRIMA di
        # analizzare — il candidato va valutato sul percorso già tagliato, non
        # su quello grezzo di BRouter. I via-point mandatory/start/end restano
        # sempre intoccati (l'utente li ha chiesti esplicitamente).
        named_via_points = _named_via_points(strategy, snaps)
        cuts = cut_out_and_back_in_gpx(str(gpx_path), _protected_points(named_via_points))
        cut_attributions = _attribute_cuts(cuts, named_via_points)
        removed_waypoints = [c["waypoint_name"] for c in cut_attributions if c["waypoint_name"]]

        # 3. Analizza GPX — rilegge il file già tagliato: le metriche (distanza,
        # dislivello, salite, out_and_back_percent residuo) riflettono il
        # percorso finale, non quello grezzo.
        analysis = analyze_gpx(
            str(gpx_path),
            route_type=strategy["route_type"],
            expected_end=_expected_end(strategy),
        )
        analysis["out_and_back_cuts"] = cut_attributions
        analysis["removed_waypoints"] = removed_waypoints

        # 3b. Attribuisci ogni spuntone andata/ritorno RESIDUO (quelli protetti,
        # non tagliati da Fix A) al via-point più vicino al suo apice (diagnosi
        # "Polverigi_9") — solo informativo, non altera lo scoring né esclude nulla.
        analysis["out_and_back_attributions"] = _attribute_out_and_back(
            analysis.get("out_and_back_apexes", []),
            named_via_points,
        )

        _emit(f"✅ Variant {label} completed ({analysis['distance_km']:.1f} km)")

        return {
            "id": label,
            "strategy_name": strategy["name"],
            "profile": strategy["profile"],
            "route_type": strategy["route_type"],
            "gpx_path": str(gpx_path),
            "status": "retried" if attempt == 2 else "ok",
            "analysis": analysis,
            "soft_waypoint_issues": snap_issues_summary,
        }

    except Exception as exc:
        failure_reason = str(exc)
        log.warning("Candidato %s fallito (attempt %d): %s", label, attempt, failure_reason)
        _emit(f"❌ Variant {label} failed: {failure_reason}")

        if attempt == 1 and request is not None:
            alt = _get_alternative(strategy, request)
            if alt is not None:
                return _generate_one(
                    alt, label, run_dir, request=None, attempt=2,
                    snaps=snaps, snap_issues_summary=snap_issues_summary,
                    on_progress=on_progress,
                )

        return {
            "id": label,
            "strategy_name": strategy["name"],
            "profile": strategy["profile"],
            "route_type": strategy["route_type"],
            "gpx_path": None,
            "status": "failed",
            "failure_reason": failure_reason,
            "analysis": None,
            "soft_waypoint_issues": snap_issues_summary,
        }


def generate_candidates(
    strategies: list[dict],
    request: dict | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """
    strategies:  CandidateRoute geocodificati (output di geocode_candidate).
    request:     RouteRequest originale — necessario per retry via Planner (SRS §6.3).
    on_progress: callback opzionale (message: str) -> None, chiamato ad ogni
                 passo significativo (snap waypoint, retry Overpass, generazione
                 di ciascun candidato) — per la UI (log "parlante" nel Builder).
                 Default no-op: chiamate da script/test esterni non cambiano.

    Ritorna lista con id (A/B/C), status (ok/retried/failed), gpx_path, analysis.
    Ogni candidato è indipendente: un fallimento non blocca gli altri.
    """
    run_dir = _ROUTES_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    log.info("Output GPX in: %s", run_dir)

    # Fix 1: risolvi lo snap dei waypoint soft UNA VOLTA per l'intera generazione,
    # condiviso tra i 3 candidati — tutte le strategie condividono la stessa
    # sequenza di waypoint base (stesso Planner output, solo profilo diverso).
    base_waypoints = strategies[0]["waypoints"] if strategies else []
    snaps = _resolve_soft_snaps(base_waypoints, on_progress=on_progress)

    snap_issues_summary = {
        "no_road": sorted({
            wp["name"] for wp in base_waypoints
            if _is_soft_via(wp) and snaps.get(_snap_key(wp), {}).get("excluded") == "no_road"
        }),
        # Non più esclusi (vedi _resolve_soft_snaps) — inclusi ma con aggancio
        # grezzo via BRouter diretto invece che a una strada reale via Overpass.
        "raw_fallback": sorted({
            wp["name"] for wp in base_waypoints
            if _is_soft_via(wp) and snaps.get(_snap_key(wp), {}).get("source") == "brouter_raw_fallback"
        }),
    }

    results = []
    for i, strategy in enumerate(strategies):
        label = _SLOT_LABELS[i] if i < len(_SLOT_LABELS) else str(i)
        result = _generate_one(
            strategy, label, run_dir, request=request, attempt=1,
            snaps=snaps, snap_issues_summary=snap_issues_summary,
            on_progress=on_progress,
        )
        results.append(result)

    return results
