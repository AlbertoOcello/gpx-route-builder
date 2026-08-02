"""
Analyzer base per file GPX: distanza, dislivello, loop/endpoint check.
"""
import math

import gpxpy
from geopy.distance import geodesic

# ── Rilevamento tratti andata/ritorno sovrapposti ("a lecca-lecca") ───────────
# Un percorso può chiudersi correttamente (start==end) ed essere comunque "falso":
# va da A a B e ripercorre la stessa strada all'indietro invece di fare un anello
# vero. loop_closed non lo rileva perché guarda solo l'endpoint, non la forma.
_OAB_MIN_GAP_M = 150.0    # distanza-lungo-tracciato minima per non essere "adiacenti"
                          # (esclude tornanti stretti/curve, che restano vicini in cumulativa)
_OAB_PROXIMITY_M = 30.0   # sotto questa distanza euclidea due punti sono "sullo stesso asse"
_OAB_MIN_RUN_M = 100.0    # lunghezza minima di un tratto sovrapposto consecutivo per contarlo
                          # (esclude incroci puntuali tipo percorso a otto)

# Soglia di comunicazione (non di rilevamento): sopra questa percentuale il
# problema va segnalato all'utente/AI in modo esplicito — condivisa da Builder
# (main.py) e Decision Agent (decision_agent.py), non duplicare il numero altrove.
OUT_AND_BACK_WARN_THRESHOLD_PCT = 20.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _find_out_and_back_runs(
    points: list[tuple[float, float]],
    min_gap_m: float,
    proximity_m: float,
    min_run_m: float,
) -> tuple[float, list[float], list[tuple[tuple[int, int, float], tuple[int, int, float]]]]:
    """
    Rilevamento di base, condiviso da _detect_out_and_back (sola diagnosi) e
    cut_out_and_back_deviations (Fix A, taglio fisico). Per ogni punto cerca il
    più vicino tra i punti che sono lontani almeno min_gap_m *lungo il
    tracciato* (non in linea d'aria): un tornante stretto ha punti vicini anche
    in cumulativa, quindi resta escluso; un vero tratto di ritorno ha punti
    vicini in spazio ma lontani in cumulativa. Conta solo run consecutivi di
    sovrapposizione lunghi almeno min_run_m, per escludere incroci puntuali
    (percorso a otto, breve tratto in comune tra due anse).

    Ogni "spuntone" andata/ritorno produce DUE run speculari (la gamba di andata
    e quella di ritorno). Li accoppia (ordinati per posizione, esclusa
    l'eventuale coppia di chiusura anello start≈end).

    Ritorna (percent, cum, paired) dove paired è una lista di (leg_a, leg_b)
    con leg_a/leg_b = (start_idx, end_idx, run_len_m); leg_a è la gamba di
    andata (indici minori), leg_b quella di ritorno (indici maggiori).
    """
    n = len(points)
    if n < 4:
        return 0.0, [], []

    cum = [0.0] * n
    for i in range(1, n):
        cum[i] = cum[i - 1] + _haversine_m(*points[i - 1], *points[i])
    total_m = cum[-1]
    if total_m <= 0:
        return 0.0, cum, []

    overlap = [False] * n
    for i in range(n):
        lat_i, lon_i = points[i]
        ci = cum[i]
        for j in range(n):
            if abs(cum[j] - ci) <= min_gap_m:
                continue
            if _haversine_m(lat_i, lon_i, points[j][0], points[j][1]) < proximity_m:
                overlap[i] = True
                break

    runs: list[tuple[int, int, float]] = []  # (start_idx, end_idx, run_len_m)
    run_start = None
    for i in range(n):
        if overlap[i]:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                run_len = cum[i - 1] - cum[run_start]
                if run_len >= min_run_m:
                    runs.append((run_start, i - 1, run_len))
                run_start = None
    if run_start is not None:
        run_len = cum[n - 1] - cum[run_start]
        if run_len >= min_run_m:
            runs.append((run_start, n - 1, run_len))

    overlapped_m = sum(r[2] for r in runs)
    percent = round(min(100.0, 100.0 * overlapped_m / total_m), 1)

    # Esclude la coppia di chiusura anello (start≈end, non è uno spuntone su
    # un waypoint) e accoppia i run rimanenti in ordine di posizione.
    closure = [r for r in runs if r[0] == 0 or r[1] == n - 1]
    others = sorted((r for r in runs if r not in closure), key=lambda r: r[0])
    paired = [(others[i], others[i + 1]) for i in range(0, len(others) - 1, 2)]

    return percent, cum, paired


def _detect_out_and_back(
    points: list[tuple[float, float]],
    min_gap_m: float = _OAB_MIN_GAP_M,
    proximity_m: float = _OAB_PROXIMITY_M,
    min_run_m: float = _OAB_MIN_RUN_M,
) -> dict:
    """
    Per ciascuna coppia di run speculari calcola l'apice — il punto di
    inversione reale, a metà tra la fine della gamba di andata e l'inizio di
    quella di ritorno — utile per risalire al via-point che lo ha causato
    (vedi candidate_generator, che abbina l'apice al via-point più vicino).

    Ritorna {"percent": float, "apexes": [{"apex_lat","apex_lon","overlap_km"}]}.
    """
    percent, cum, paired = _find_out_and_back_runs(points, min_gap_m, proximity_m, min_run_m)

    apexes = []
    for leg_a, leg_b in paired:
        apex_cum = (cum[leg_a[1]] + cum[leg_b[0]]) / 2
        apex_idx = min(range(len(points)), key=lambda k: abs(cum[k] - apex_cum))
        apexes.append({
            "apex_lat": points[apex_idx][0],
            "apex_lon": points[apex_idx][1],
            "overlap_km": round((leg_a[2] + leg_b[2]) / 1000, 2),
        })

    return {"percent": percent, "apexes": apexes}


def _out_and_back_percent(
    points: list[tuple[float, float]],
    min_gap_m: float = _OAB_MIN_GAP_M,
    proximity_m: float = _OAB_PROXIMITY_M,
    min_run_m: float = _OAB_MIN_RUN_M,
) -> float:
    """Compatibilità: solo la percentuale, senza gli apici. Vedi _detect_out_and_back."""
    return _detect_out_and_back(points, min_gap_m, proximity_m, min_run_m)["percent"]


# ── Fix A: taglio automatico delle deviazioni andata/ritorno ──────────────────
# Se BRouter passa due volte per lo stesso punto (bivio) con una deviazione
# significativa in mezzo, quella deviazione è sempre geometricamente "sbagliata"
# (un vero anello non ripercorre sé stesso) — va rimossa dal GPX finale, a meno
# che l'apice non coincida con un via-point mandatory (l'utente lo ha chiesto
# esplicitamente, va rispettato). Non tenta di spiegare la causa (waypoint
# soft mal posizionato, geocoding sbagliato, vicolo cieco reale): la taglia e
# basta, per definizione geometrica.
_OAB_CUT_MIN_DEVIATION_M = 300.0   # sotto questa percorrenza totale (andata+ritorno) non si taglia
_OAB_CUT_PROTECT_TOLERANCE_M = 150.0  # stessa tolleranza di attribuzione di Fix 2


def cut_out_and_back_deviations(
    points: list[tuple[float, float]],
    elevations: list[float | None],
    protected_points: list[tuple[float, float]],
    min_gap_m: float = _OAB_MIN_GAP_M,
    proximity_m: float = _OAB_PROXIMITY_M,
    min_run_m: float = _OAB_MIN_RUN_M,
    min_deviation_m: float = _OAB_CUT_MIN_DEVIATION_M,
    protect_tolerance_m: float = _OAB_CUT_PROTECT_TOLERANCE_M,
) -> dict:
    """
    Rimuove iterativamente le deviazioni andata/ritorno da (points, elevations),
    ricucendo il tracciato direttamente al punto di bivio. Un apice è
    "protetto" (mai tagliato) se entro protect_tolerance_m da uno dei
    protected_points (via-point mandatory, start, end) — l'utente ha chiesto
    esplicitamente quel punto. Una deviazione è tagliata solo se la percorrenza
    totale (andata + ritorno) è almeno min_deviation_m, per non intaccare
    overlap minori già esclusi dal rilevamento di base (tornanti, incroci).

    Dopo ogni taglio il rilevamento riparte da capo sul tracciato aggiornato
    (gli indici cambiano), per gestire correttamente deviazioni multiple o
    annidate senza lasciare residui.

    Ritorna {"points", "elevations", "kept_indices", "cuts"}:
      - kept_indices: indici nell'array points/elevations ORIGINALE dei punti
        sopravvissuti (stesso ordine) — utile a chi deve ritagliare una lista
        parallela di oggetti (es. i GPXTrackPoint originali) senza doverli
        ricostruire da lat/lon.
      - cuts: [{"apex_lat","apex_lon","overlap_km"}], nell'ordine in cui sono
        stati effettivamente rimossi.
    """
    pts = list(points)
    eles = list(elevations)
    kept_indices = list(range(len(points)))
    cuts = []

    while True:
        _, cum, paired = _find_out_and_back_runs(pts, min_gap_m, proximity_m, min_run_m)
        if not paired:
            break

        cut_made = False
        for leg_a, leg_b in paired:
            deviation_m = leg_a[2] + leg_b[2]
            if deviation_m < min_deviation_m:
                continue

            apex_cum = (cum[leg_a[1]] + cum[leg_b[0]]) / 2
            apex_idx = min(range(len(pts)), key=lambda k: abs(cum[k] - apex_cum))
            apex_lat, apex_lon = pts[apex_idx]

            protected = any(
                _haversine_m(apex_lat, apex_lon, p_lat, p_lon) <= protect_tolerance_m
                for p_lat, p_lon in protected_points
            )
            if protected:
                continue

            cut_start, cut_end = leg_a[0], leg_b[1]
            cuts.append({
                "apex_lat": apex_lat,
                "apex_lon": apex_lon,
                "overlap_km": round(deviation_m / 1000, 2),
            })
            pts = pts[:cut_start] + pts[cut_end + 1:]
            eles = eles[:cut_start] + eles[cut_end + 1:]
            kept_indices = kept_indices[:cut_start] + kept_indices[cut_end + 1:]
            cut_made = True
            break  # tracciato cambiato: ricomincia il rilevamento da capo

        if not cut_made:
            break

    return {"points": pts, "elevations": eles, "kept_indices": kept_indices, "cuts": cuts}


def cut_out_and_back_in_gpx(gpx_path: str, protected_points: list[tuple[float, float]]) -> list[dict]:
    """
    Applica cut_out_and_back_deviations al file GPX su disco, sovrascrivendolo
    con il tracciato tagliato (i GPXTrackPoint originali sopravvissuti sono
    riusati as-is, via kept_indices — nessuna ricostruzione da lat/lon, quindi
    elevazione/eventuali extension restano intatte sui punti mantenuti).

    Va chiamata PRIMA di analyze_gpx: quest'ultima rilegge il file già tagliato
    e ricalcola tutte le metriche (distanza, dislivello, salite,
    out_and_back_percent) sul percorso finale, non su quello originale.

    Ritorna la lista dei tagli effettuati (vedi cut_out_and_back_deviations) —
    lista vuota se non c'era nulla da tagliare (nessuna riscrittura in quel caso).
    """
    with open(gpx_path, "r") as f:
        gpx = gpxpy.parse(f)

    track_points = []
    for track in gpx.tracks:
        for segment in track.segments:
            track_points.extend(segment.points)

    if len(track_points) < 4:
        return []

    latlon = [(p.latitude, p.longitude) for p in track_points]
    eles = [p.elevation for p in track_points]

    result = cut_out_and_back_deviations(latlon, eles, protected_points)
    if not result["cuts"]:
        return []

    kept = set(result["kept_indices"])
    offset = 0
    for track in gpx.tracks:
        for segment in track.segments:
            n = len(segment.points)
            segment.points = [
                p for i, p in enumerate(segment.points) if (offset + i) in kept
            ]
            offset += n

    with open(gpx_path, "w", encoding="utf-8") as f:
        f.write(gpx.to_xml())

    return result["cuts"]


# ── Rilevamento salite ──────────────────────────────────────────────────────
# Costanti condivise tra rilevamento/classificazione salite (qui) e colorazione
# del profilo altimetrico punto-per-punto (Builder/Ride Analysis) — non
# duplicare queste soglie altrove, importarle da qui.
_CLIMB_RESAMPLE_STEP_M = 20.0     # passo di ricampionamento uniforme lungo il tracciato
_CLIMB_SMOOTH_WINDOW_M = 75.0     # media mobile elevazione prima del calcolo gradiente (50-100m)
_CLIMB_MAX_GRAD_WINDOW_M = 100.0  # finestra corta per max_gradient_percent (cattura uno strappo isolato)
_CLIMB_MERGE_GAP_M = 100.0        # gap breve tra due tratti in salita → uniti in una sola salita

_CLIMB_MIN_GRADIENT_PCT = 2.0     # sotto questa soglia non è una salita rilevante
_CLIMB_MIN_LENGTH_M = 200.0       # sotto questa lunghezza non è una salita rilevante (rumore/saliscendi)
_CLIMB_SHORT_LENGTH_M = 300.0     # sotto questa lunghezza, con pendenza ≥8%, è "strappo_breve" non "impegnativa"

# Fasce di pendenza — condivise da classificazione salite E colore istantaneo nel grafico.
_GRAD_DOLCE_MAX_PCT = 4.0     # < 4%  → dolce / verde
_GRAD_MODERATA_MAX_PCT = 8.0  # 4-8%  → moderata / giallo-arancio ; ≥8% → impegnativa (o rosso)

_GRADIENT_COLOR_GREEN = "#27ae60"
_GRADIENT_COLOR_YELLOW = "#f39c12"
_GRADIENT_COLOR_RED = "#e74c3c"

_CLIMB_CLASS_EMOJI = {
    "dolce": "🟢",
    "moderata": "🟡",
    "impegnativa": "🔴",
    "strappo_breve": "⚡",
}


def gradient_color(gradient_percent: float) -> str:
    """
    Colore per fascia di pendenza istantanea, condiviso con la classificazione
    salite: <4% verde, 4-8% giallo/arancio, ≥8% rosso. Le discese (gradiente
    negativo) contano come "verde" — non è uno sforzo in salita.
    """
    if gradient_percent < _GRAD_DOLCE_MAX_PCT:
        return _GRADIENT_COLOR_GREEN
    if gradient_percent < _GRAD_MODERATA_MAX_PCT:
        return _GRADIENT_COLOR_YELLOW
    return _GRADIENT_COLOR_RED


def _classify_climb(avg_gradient_percent: float, length_m: float) -> str:
    if avg_gradient_percent < _GRAD_DOLCE_MAX_PCT:
        return "dolce"
    if avg_gradient_percent < _GRAD_MODERATA_MAX_PCT:
        return "moderata"
    return "impegnativa" if length_m >= _CLIMB_SHORT_LENGTH_M else "strappo_breve"


def _resample_elevation_profile(
    distances_cumulative_m: list[float],
    elevations_m: list[float | None],
    step_m: float,
) -> tuple[list[float], list[float]]:
    """
    Ricampiona (distanza, elevazione) a passo uniforme step_m via interpolazione
    lineare, dopo aver scartato i punti senza elevazione. Ritorna liste vuote
    se i dati validi sono insufficienti.
    """
    pairs = [(d, e) for d, e in zip(distances_cumulative_m, elevations_m) if e is not None]
    if len(pairs) < 4:
        return [], []

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    total = xs[-1]
    if total <= 0:
        return [], []

    n_steps = max(1, int(total // step_m))
    resampled_d = [i * step_m for i in range(n_steps + 1)]

    resampled_e = []
    j = 0
    for d in resampled_d:
        while j < len(xs) - 2 and xs[j + 1] < d:
            j += 1
        x0, x1 = xs[j], xs[j + 1]
        y0, y1 = ys[j], ys[j + 1]
        t = 0.0 if x1 == x0 else (d - x0) / (x1 - x0)
        t = min(1.0, max(0.0, t))
        resampled_e.append(y0 + t * (y1 - y0))

    return resampled_d, resampled_e


def _moving_average(values: list[float], window_pts: int) -> list[float]:
    """Media mobile centrata, window_pts espresso in numero di punti (serie uniforme)."""
    n = len(values)
    if n == 0 or window_pts <= 1:
        return list(values)
    half = window_pts // 2
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def detect_climbs(
    distances_cumulative_m: list[float],
    elevations_m: list[float | None],
) -> dict:
    """
    Rileva le salite lungo un tracciato a partire da distanza cumulativa (m) ed
    elevazione (m) per punto (stesso ordine/lunghezza di points).

    Ritorna:
      "climbs": list[dict] — una per salita rilevata, con start_km, length_m,
        elevation_gain_m, avg_gradient_percent, max_gradient_percent,
        classification (dolce/moderata/impegnativa/strappo_breve),
        has_steep_section (bool), note (str|None).
      "profile_distances_km": list[float] — profilo ricampionato/smoothed, per grafico.
      "profile_elevations_m": list[float] — elevazione smoothed corrispondente.
      "profile_colors": list[str] — colore per fascia di pendenza istantanea,
        stesso index di profile_distances_km/profile_elevations_m.
    """
    empty = {
        "climbs": [],
        "profile_distances_km": [],
        "profile_elevations_m": [],
        "profile_colors": [],
    }

    rd, re_ = _resample_elevation_profile(
        distances_cumulative_m, elevations_m, _CLIMB_RESAMPLE_STEP_M
    )
    n = len(rd)
    if n < 4:
        return empty

    smooth_window_pts = max(1, round(_CLIMB_SMOOTH_WINDOW_M / _CLIMB_RESAMPLE_STEP_M))
    smoothed = _moving_average(re_, smooth_window_pts)

    # Gradiente istantaneo punto-per-punto sulla serie smoothed (per detection + colore grafico)
    grad = [0.0] * n
    for i in range(1, n):
        dd = rd[i] - rd[i - 1]
        grad[i] = (smoothed[i] - smoothed[i - 1]) / dd * 100.0 if dd > 0 else 0.0
    if n > 1:
        grad[0] = grad[1]

    profile_colors = [gradient_color(g) for g in grad]

    # ── Individua run contigui sopra soglia, poi unisce quelli separati da gap brevi ──
    raw_runs: list[tuple[int, int]] = []
    run_start = None
    for i in range(n):
        if grad[i] >= _CLIMB_MIN_GRADIENT_PCT:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                raw_runs.append((run_start, i - 1))
                run_start = None
    if run_start is not None:
        raw_runs.append((run_start, n - 1))

    merged_runs: list[tuple[int, int]] = []
    for run in raw_runs:
        if merged_runs and (rd[run[0]] - rd[merged_runs[-1][1]]) <= _CLIMB_MERGE_GAP_M:
            merged_runs[-1] = (merged_runs[-1][0], run[1])
        else:
            merged_runs.append(run)

    max_grad_offset_pts = max(1, round(_CLIMB_MAX_GRAD_WINDOW_M / _CLIMB_RESAMPLE_STEP_M))

    climbs = []
    for start_idx, end_idx in merged_runs:
        length_m = rd[end_idx] - rd[start_idx]
        if length_m < _CLIMB_MIN_LENGTH_M:
            continue

        elevation_gain_m = smoothed[end_idx] - smoothed[start_idx]
        if elevation_gain_m <= 0:
            continue
        avg_gradient_percent = elevation_gain_m / length_m * 100.0

        max_gradient_percent = 0.0
        for i in range(start_idx, end_idx):
            j = min(end_idx, i + max_grad_offset_pts)
            dd = rd[j] - rd[i]
            if dd <= 0:
                continue
            g = (smoothed[j] - smoothed[i]) / dd * 100.0
            max_gradient_percent = max(max_gradient_percent, g)
        max_gradient_percent = max(max_gradient_percent, avg_gradient_percent)

        classification = _classify_climb(avg_gradient_percent, length_m)
        has_steep_section = (
            max_gradient_percent >= _GRAD_MODERATA_MAX_PCT
            and avg_gradient_percent < _GRAD_MODERATA_MAX_PCT
        )

        climbs.append({
            "start_km": round(rd[start_idx] / 1000, 2),
            "length_m": round(length_m, 0),
            "elevation_gain_m": round(elevation_gain_m, 1),
            "avg_gradient_percent": round(avg_gradient_percent, 1),
            "max_gradient_percent": round(max_gradient_percent, 1),
            "classification": classification,
            "classification_emoji": _CLIMB_CLASS_EMOJI[classification],
            "has_steep_section": has_steep_section,
            "note": (
                f"contiene un tratto al {max_gradient_percent:.0f}%"
                if has_steep_section else None
            ),
        })

    return {
        "climbs": climbs,
        "profile_distances_km": [round(d / 1000, 3) for d in rd],
        "profile_elevations_m": [round(e, 1) for e in smoothed],
        "profile_colors": profile_colors,
    }


def analyze_gpx(gpx_path: str,
                 route_type: str = "loop",
                 expected_end: tuple[float, float] | None = None) -> dict:
    """
    gpx_path: percorso al file GPX da analizzare.
    route_type: "loop" | "out_and_back" | "point_to_point"
    expected_end: (lon, lat) del punto di arrivo richiesto, usato solo per point_to_point.

    Ritorna un dizionario con i campi principali di GPXAnalysis (v0.3).
    """
    with open(gpx_path, "r") as f:
        gpx = gpxpy.parse(f)

    # Estrai tutti i punti come lista
    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            points.extend(segment.points)

    if not points:
        raise ValueError("Il file GPX non contiene punti traccia")

    # Distanza totale (gpxpy la calcola in metri)
    distance_m = gpx.length_2d()
    distance_km = round(distance_m / 1000, 2)

    # Dislivello (uphill, downhill) in metri
    uphill, downhill = gpx.get_uphill_downhill()
    elevation_gain_m = round(uphill, 1)
    elevation_loss_m = round(downhill, 1)

    start = points[0]
    end = points[-1]

    oab = _detect_out_and_back([(p.latitude, p.longitude) for p in points])
    out_and_back_percent = oab["percent"]

    cum_m = [0.0] * len(points)
    for i in range(1, len(points)):
        cum_m[i] = cum_m[i - 1] + _haversine_m(
            points[i - 1].latitude, points[i - 1].longitude,
            points[i].latitude, points[i].longitude,
        )
    climb_data = detect_climbs(cum_m, [p.elevation for p in points])

    result = {
        "distance_km": distance_km,
        "elevation_gain_m": elevation_gain_m,
        "elevation_loss_m": elevation_loss_m,
        "loop_closed": None,
        "endpoint_match_m": None,
        "out_and_back_percent": out_and_back_percent,
        "out_and_back_apexes": oab["apexes"],
        "climbs": climb_data["climbs"],
        "elevation_profile": {
            "distances_km": climb_data["profile_distances_km"],
            "elevations_m": climb_data["profile_elevations_m"],
            "colors": climb_data["profile_colors"],
        },
        "violations": [],
    }

    if route_type in ("loop", "out_and_back"):
        # loop_closed: distanza tra punto iniziale e finale della traccia
        closure_distance_m = geodesic(
            (start.latitude, start.longitude),
            (end.latitude, end.longitude),
        ).meters
        result["loop_closed"] = closure_distance_m < 100  # soglia tolleranza 100m
        result["closure_distance_m"] = round(closure_distance_m, 1)

    elif route_type == "point_to_point":
        if expected_end is None:
            raise ValueError("Per point_to_point serve specificare expected_end=(lon, lat)")
        end_lon, end_lat = expected_end
        endpoint_match_m = geodesic(
            (end.latitude, end.longitude),
            (end_lat, end_lon),
        ).meters
        result["endpoint_match_m"] = round(endpoint_match_m, 1)

    return result


if __name__ == "__main__":
    analysis = analyze_gpx(
        "routes/generated/test_wrapper.gpx",
        route_type="point_to_point",
        expected_end=(13.2400, 43.7200),
    )
    print(analysis)
