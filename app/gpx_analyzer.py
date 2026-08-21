"""
Analyzer base per file GPX: distanza, dislivello, loop/endpoint check.
"""
import math
import os
from pathlib import Path

import gpxpy
from geopy.distance import geodesic
from shapely.geometry import LineString


def gpx_creator_string() -> str:
    """
    Stringa per l'attributo <gpx creator="..."> (metadato standard GPX,
    sull'elemento radice — non <metadata>). Stessa fonte di verità di
    main.py._app_version()/_build_commit() (file VERSION alla root del repo,
    env GIT_COMMIT "baked" nell'immagine Docker al build) — non duplicata qui
    come nuova sorgente, solo riletta: gpx_analyzer.py non può importare da
    main.py (main.py importa da qui, non viceversa) né gpx_optimizer.py/
    candidate_generator.py, entrambi scrittori di GPX, dipendono da main.py.
    """
    try:
        version = (Path(__file__).parent.parent / "VERSION").read_text().strip()
    except OSError:
        version = "dev"
    commit = os.environ.get("GIT_COMMIT", "unknown")
    commit = "dev" if commit == "unknown" else commit
    return f"GPX Route Builder v{version} (build {commit})"


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
    vicini in spazio ma lontani in cumulativa.

    Ogni "spuntone" andata/ritorno produce DUE run speculari (la gamba di
    andata e quella di ritorno) — vengono accoppiati per posizione PRIMA di
    applicare la soglia min_run_m, e la soglia si applica alla percorrenza
    COMBINATA della coppia (andata + ritorno), non a ciascuna gamba
    singolarmente: vicino al vertice di uno spuntone il matching punto-punto
    può interrompersi per un breve tratto (drift naturale della strada),
    spezzando la sovrapposizione in due gambe più corte che, prese
    singolarmente, potrebbero cadere sotto soglia pur essendo insieme ben
    oltre. La chiusura d'anello (start≈end, non è una gamba di uno spuntone
    su un via-point) resta filtrata individualmente, come le altre coppie
    scartate: esclude incroci puntuali (percorso a otto, breve tratto in
    comune tra due anse).

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

    raw_runs: list[tuple[int, int, float]] = []  # (start_idx, end_idx, run_len_m) — non ancora filtrati
    run_start = None
    for i in range(n):
        if overlap[i]:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                raw_runs.append((run_start, i - 1, cum[i - 1] - cum[run_start]))
                run_start = None
    if run_start is not None:
        raw_runs.append((run_start, n - 1, cum[n - 1] - cum[run_start]))

    # Chiusura anello (start≈end): non è la gamba di uno spuntone, resta
    # filtrata individualmente. Il resto viene accoppiato per posizione PRIMA
    # del filtro min_run_m (vedi docstring).
    closure = [r for r in raw_runs if (r[0] == 0 or r[1] == n - 1) and r[2] >= min_run_m]
    others = sorted((r for r in raw_runs if r[0] != 0 and r[1] != n - 1), key=lambda r: r[0])
    candidate_pairs = [(others[i], others[i + 1]) for i in range(0, len(others) - 1, 2)]
    paired = [(leg_a, leg_b) for leg_a, leg_b in candidate_pairs if leg_a[2] + leg_b[2] >= min_run_m]

    overlapped_m = sum(r[2] for r in closure) + sum(leg_a[2] + leg_b[2] for leg_a, leg_b in paired)
    percent = round(min(100.0, 100.0 * overlapped_m / total_m), 1)

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
            cut_start, cut_end = leg_a[0], leg_b[1]
            # Percorrenza reale bivio→rientro (non la somma delle sole porzioni
            # "matchate" delle due gambe, leg_a[2]+leg_b[2]): quest'ultima
            # sottostima lo spuntone perché esclude il tratto vicino al vertice
            # dove il matching punto-punto si interrompe — tratto che il taglio
            # rimuove comunque (cut_start→cut_end lo include per intero).
            deviation_m = cum[cut_end] - cum[cut_start]
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

            cuts.append({
                "apex_lat": apex_lat,
                "apex_lon": apex_lon,
                "overlap_km": round(deviation_m / 1000, 2),
            })
            pts, eles, kept_indices = _splice_out_range(pts, eles, kept_indices, cut_start, cut_end)
            cut_made = True
            break  # tracciato cambiato: ricomincia il rilevamento da capo

        if not cut_made:
            break

    return {"points": pts, "elevations": eles, "kept_indices": kept_indices, "cuts": cuts}


def _splice_out_range(
    points: list,
    elevations: list,
    kept_indices: list[int],
    start_idx: int,
    end_idx: int,
) -> tuple[list, list, list[int]]:
    """
    Rimuove points[start_idx:end_idx+1] (e le liste parallele elevations/
    kept_indices) e ricuce direttamente i due capi rimasti — l'unica
    operazione fisica di "taglio" del tracciato, condivisa dal rilevamento
    automatico degli spuntoni andata/ritorno (cut_out_and_back_deviations)
    e dal taglio manuale esplicito (Builder → "Cancella tratto",
    cut_range_in_gpx). start_idx/end_idx sono inclusi nel range rimosso.
    """
    return (
        points[:start_idx] + points[end_idx + 1:],
        elevations[:start_idx] + elevations[end_idx + 1:],
        kept_indices[:start_idx] + kept_indices[end_idx + 1:],
    )


def _apply_kept_indices_to_gpx(gpx: "gpxpy.gpx.GPX", kept_indices: list[int], out_path: str) -> None:
    """
    Filtra i punti di un oggetto gpxpy.gpx.GPX già parsato tenendo solo quelli
    in kept_indices (indici nell'ordine di iterazione tracks→segments→points,
    stesso ordine di track_points nei chiamanti) e scrive il risultato in
    out_path — riusa gpxpy.to_xml() per la serializzazione (i GPXTrackPoint
    originali sopravvissuti sono riusati as-is, elevazione/extension incluse).
    Ricucitura fisica su disco condivisa dal taglio automatico
    (cut_out_and_back_in_gpx) e dal taglio manuale (cut_range_in_gpx).
    """
    kept = set(kept_indices)
    offset = 0
    for track in gpx.tracks:
        for segment in track.segments:
            n = len(segment.points)
            segment.points = [
                p for i, p in enumerate(segment.points) if (offset + i) in kept
            ]
            offset += n

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(gpx.to_xml())


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

    _apply_kept_indices_to_gpx(gpx, result["kept_indices"], gpx_path)
    return result["cuts"]


# ── Taglio manuale esplicito ("✂️ Cancella tratto", tab Builder) ──────────────
# A differenza del taglio automatico sopra (rileva geometricamente gli
# spuntoni andata/ritorno), qui l'intervallo da rimuovere è scelto
# esplicitamente dall'utente (slider km) — nessun rilevamento, solo
# localizzazione indici + stessa ricucitura fisica (_splice_out_range /
# _apply_kept_indices_to_gpx) riusata dal Fix A qui sopra.
_MANUAL_CUT_MIN_REMAINING_POINTS = 2


def cut_range_in_gpx(gpx_path: str, start_km: float, end_km: float, out_path: str) -> dict:
    """
    Rimuove dal GPX in gpx_path l'intervallo [start_km, end_km] (selezione
    esplicita dell'utente lungo il tracciato, in km cumulativi) e scrive il
    risultato in out_path — non sovrascrive mai gpx_path: usata sia per
    l'anteprima live (out_path temporaneo, analizzato e poi scartato) sia
    per il salvataggio definitivo (out_path permanente).

    Ritorna:
      "gap_m": distanza in linea d'aria tra il punto immediatamente prima
        dell'inizio del taglio e quello immediatamente dopo la fine — None
        se il taglio tocca un estremo del percorso (nessun "prima"/"dopo"
        da confrontare).
      "removed_km": lunghezza reale rimossa lungo il tracciato (può
        differire leggermente da end_km-start_km per lo snap ai punti più
        vicini).
      "removed_coords": [(lat, lon), ...] dei punti rimossi, per
        evidenziare il tratto sulla mappa di anteprima.

    Solleva ValueError se il GPX ha meno di 2 punti o se il taglio
    lascerebbe meno di _MANUAL_CUT_MIN_REMAINING_POINTS punti nel percorso.
    """
    with open(gpx_path, "r") as f:
        gpx = gpxpy.parse(f)

    track_points = []
    for track in gpx.tracks:
        for segment in track.segments:
            track_points.extend(segment.points)

    n = len(track_points)
    if n < 2:
        raise ValueError("GPX con meno di 2 punti, impossibile tagliare")

    cum = [0.0] * n
    for i in range(1, n):
        cum[i] = cum[i - 1] + _haversine_m(
            track_points[i - 1].latitude, track_points[i - 1].longitude,
            track_points[i].latitude, track_points[i].longitude,
        )

    lo_km, hi_km = min(start_km, end_km), max(start_km, end_km)
    start_idx = min(range(n), key=lambda i: abs(cum[i] - lo_km * 1000))
    end_idx = min(range(n), key=lambda i: abs(cum[i] - hi_km * 1000))
    if end_idx < start_idx:
        start_idx, end_idx = end_idx, start_idx

    gap_m = None
    if start_idx > 0 and end_idx < n - 1:
        p_before, p_after = track_points[start_idx - 1], track_points[end_idx + 1]
        gap_m = _haversine_m(
            p_before.latitude, p_before.longitude, p_after.latitude, p_after.longitude,
        )

    removed_coords = [(p.latitude, p.longitude) for p in track_points[start_idx:end_idx + 1]]
    removed_km = (cum[end_idx] - cum[start_idx]) / 1000

    all_indices = list(range(n))
    _, _, kept_indices = _splice_out_range(all_indices, all_indices, all_indices, start_idx, end_idx)
    if len(kept_indices) < _MANUAL_CUT_MIN_REMAINING_POINTS:
        raise ValueError("Il taglio lascerebbe troppo pochi punti nel percorso")

    _apply_kept_indices_to_gpx(gpx, kept_indices, out_path)

    return {"gap_m": gap_m, "removed_km": removed_km, "removed_coords": removed_coords}


# ── Rilevamento salite ──────────────────────────────────────────────────────
# Costanti condivise tra rilevamento/classificazione salite (qui) e colorazione
# del profilo altimetrico punto-per-punto (Builder/Ride Analysis) — non
# duplicare queste soglie altrove, importarle da qui.
_CLIMB_RESAMPLE_STEP_M = 20.0     # passo di ricampionamento uniforme lungo il tracciato
_CLIMB_MAX_GRAD_WINDOW_M = 100.0  # finestra corta per max_gradient_percent (cattura uno strappo isolato)
_CLIMB_MERGE_GAP_M = 100.0        # gap breve tra due tratti in salita → uniti in una sola salita

# max_gradient_percent è marcato max_gradient_low_confidence quando è SIA
# fisicamente implausibile in assoluto SIA un picco isolato molto più ripido
# della salita che lo contiene — entrambe le condizioni, non una sola.
# NON basato sul conteggio di punti GPX grezzi nella finestra: misurato e
# scartato — riflette solo come è stato generato il file (profilo BRouter,
# passaggio dal GPX Optimizer) e non la qualità del dato in quel punto: sui
# file "Marche" puliti l'87-97% delle salite risultava sotto soglia punti pur
# con pendenze plausibili (fino a 20.8%), mentre il caso Via Silente noto
# (38.2%) restava sopra soglia — l'esatto contrario dell'intento.
_MAX_GRAD_LOW_CONFIDENCE_ABS_PCT = 30.0    # sopra questa pendenza assoluta, implausibile su strada reale
_MAX_GRAD_LOW_CONFIDENCE_RATIO = 2.5       # E il picco è più di 2.5× la pendenza media della salita

# Smoothing elevazione — condiviso da detect_climbs() E da elevation_gain_m/
# elevation_loss_m (analyze_gpx / ride_analysis_agent.analyze_gpx_bytes).
# Il dato sorgente (SRTM via BRouter) può contenere rumore a dente di sega
# anche di ±30-65% su pochi metri, sovrapposto a salite vere — confermato
# sistemico su alcune aree (Cilento/Via Silente: 5-9% dei segmenti brevi
# sopra soglia, contro 0-0.3% nelle Marche). Una singola media mobile non
# basta: i picchi isolati vanno abbattuti PRIMA con un filtro a mediana
# (finestra a distanza reale, robusta alla densità irregolare dei punti
# grezzi — non punti fissi, altrimenti si ricade nello stesso bug), poi il
# residuo viene smussato con una media mobile leggera sul dato ricampionato.
_ELEV_MEDIAN_WINDOW_M = 40.0  # finestra mediana (distanza reale) per abbattere outlier isolati
_ELEV_SMOOTH_WINDOW_M = 75.0  # media mobile leggera sul residuo, dopo ricampionamento (50-100m)

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


def _valid_elevation_pairs(
    distances_cumulative_m: list[float],
    elevations_m: list[float | None],
) -> tuple[list[float], list[float]]:
    """Scarta i punti senza elevazione. Ritorna (distanze, elevazioni) parallele."""
    pairs = [(d, e) for d, e in zip(distances_cumulative_m, elevations_m) if e is not None]
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _resample_series(
    xs: list[float],
    ys: list[float],
    step_m: float,
) -> tuple[list[float], list[float]]:
    """
    Ricampiona (xs, ys) — già filtrati/paralleli, xs crescente — a passo
    uniforme step_m via interpolazione lineare. Ritorna liste vuote se i dati
    sono insufficienti.
    """
    if len(xs) < 4:
        return [], []

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


def _median_filter_by_distance(
    xs: list[float],
    ys: list[float],
    window_m: float,
) -> list[float]:
    """
    Filtro a mediana su finestra di distanza REALE (non un conteggio fisso di
    punti) — robusto a spaziatura irregolare dei punti grezzi: un tratto con
    punti radi non riceve una finestra effettivamente più larga (in metri) di
    uno con punti fitti, il contrario del bug che questo filtro deve evitare.
    Ogni punto i è sostituito dalla mediana dei valori entro ±window_m/2 da
    xs[i]. Two-pointer: O(n) avanzamento degli estremi finestra + O(k log k)
    per l'ordinamento locale (k = punti nella finestra).
    """
    n = len(ys)
    if n == 0:
        return []
    half = window_m / 2.0
    out = [0.0] * n
    lo = 0
    for i in range(n):
        d = xs[i]
        while xs[lo] < d - half:
            lo += 1
        hi = lo
        while hi < n - 1 and xs[hi + 1] <= d + half:
            hi += 1
        window_vals = sorted(ys[lo:hi + 1])
        out[i] = window_vals[len(window_vals) // 2]
    return out


def smooth_elevations(
    distances_cumulative_m: list[float],
    elevations_m: list[float | None],
    step_m: float = _CLIMB_RESAMPLE_STEP_M,
) -> tuple[list[float], list[float]]:
    """
    Profilo elevazione smussato in due stadi, condiviso da detect_climbs() e
    da chi calcola elevation_gain_m/elevation_loss_m (analyze_gpx,
    ride_analysis_agent.analyze_gpx_bytes) — stessa logica ovunque, non
    duplicarla. 1) mediana a distanza reale sul dato grezzo (abbatte i picchi
    isolati del rumore sorgente senza appiattire pendenze reali sostenute);
    2) ricampionamento a passo uniforme + media mobile leggera sul residuo.
    Ritorna (distanze ricampionate uniformi, elevazioni smussate) — liste
    vuote se i dati validi sono insufficienti.
    """
    xs, ys = _valid_elevation_pairs(distances_cumulative_m, elevations_m)
    if len(xs) < 4:
        return [], []

    median_ys = _median_filter_by_distance(xs, ys, _ELEV_MEDIAN_WINDOW_M)
    rd, re_ = _resample_series(xs, median_ys, step_m)
    if not rd:
        return rd, re_

    ma_win_pts = max(1, round(_ELEV_SMOOTH_WINDOW_M / step_m))
    smoothed = _moving_average(re_, ma_win_pts)
    return rd, smoothed


def sum_uphill_downhill(elevations_m: list[float]) -> tuple[float, float]:
    """
    Dislivello positivo/negativo totale per somma dei delta punto-per-punto
    su una serie (tipicamente smoothed_elevations di smooth_elevations()) —
    stessa idea di gpxpy.get_uphill_downhill() ma su dato già smussato contro
    il rumore sorgente, invece che sul dato grezzo.
    """
    uphill = downhill = 0.0
    for i in range(1, len(elevations_m)):
        delta = elevations_m[i] - elevations_m[i - 1]
        if delta > 0:
            uphill += delta
        else:
            downhill += -delta
    return uphill, downhill


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

    rd, smoothed = smooth_elevations(distances_cumulative_m, elevations_m)
    n = len(rd)
    if n < 4:
        return empty

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

        # Confidenza: implausibile in assoluto E picco isolato molto più
        # ripido della media della salita — non un limite fisico della
        # bici/strada, probabilmente un limite di risoluzione del dato
        # elevazione sorgente (vedi costanti). Segnalato, non nascosto.
        max_gradient_low_confidence = (
            max_gradient_percent > _MAX_GRAD_LOW_CONFIDENCE_ABS_PCT
            and (max_gradient_percent / avg_gradient_percent) > _MAX_GRAD_LOW_CONFIDENCE_RATIO
        )

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
            "max_gradient_low_confidence": max_gradient_low_confidence,
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

    cum_m = [0.0] * len(points)
    for i in range(1, len(points)):
        cum_m[i] = cum_m[i - 1] + _haversine_m(
            points[i - 1].latitude, points[i - 1].longitude,
            points[i].latitude, points[i].longitude,
        )

    # Dislivello (uphill, downhill) in metri — su elevazione smussata
    # (smooth_elevations), non gpx.get_uphill_downhill() sul dato grezzo: il
    # rumore sorgente (SRTM via BRouter) gonfia altrimenti sia il gradiente
    # istantaneo sia il dislivello totale (vedi detect_climbs).
    _, smoothed_elev = smooth_elevations(cum_m, [p.elevation for p in points])
    uphill, downhill = sum_uphill_downhill(smoothed_elev)
    elevation_gain_m = round(uphill, 1)
    elevation_loss_m = round(downhill, 1)

    start = points[0]
    end = points[-1]

    oab = _detect_out_and_back([(p.latitude, p.longitude) for p in points])
    out_and_back_percent = oab["percent"]

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


# ── Bozza waypoint da percorso reale (Planner, route "solo Opzione D") ────────
# Quando una route ha solo un actual_ride e nessuna pianificazione salvata,
# estrae waypoint rappresentativi dal tracciato reale via semplificazione
# geometrica (Douglas-Peucker) per precompilare il campo "Desired waypoints"
# del Planner — punto di partenza esplicitamente escluso qui (resta al
# form, vedi main.py), così l'utente può anche ricollegare il giro a un
# punto di partenza diverso.
_DRAFT_WP_TARGET_COUNT_DEFAULT = 15    # deve coincidere col default dello slider in main.py
_DRAFT_WP_TARGET_MARGIN = 2            # margine attorno a target_count per la ricerca binaria (non un numero esatto rigido)
_DRAFT_WP_MIN_SPACING_M = 150.0       # sotto questa distanza reciproca, cluster ridondante
_DRAFT_WP_ENDPOINT_EXCLUSION_M = 350.0  # sotto questa distanza dallo start/end del tracciato, ridondante con lo start del loop
_DRAFT_WP_TOLERANCE_LO_DEG = 0.00001   # ~1m — quasi nessuna semplificazione
_DRAFT_WP_TOLERANCE_HI_DEG = 0.05      # ~5km — semplificazione aggressiva
_DRAFT_WP_SEARCH_ITERATIONS = 25


def extract_draft_waypoints_from_gpx(
    gpx_path: str,
    target_count: int = _DRAFT_WP_TARGET_COUNT_DEFAULT,
    target_margin: int = _DRAFT_WP_TARGET_MARGIN,
    min_spacing_m: float = _DRAFT_WP_MIN_SPACING_M,
    endpoint_exclusion_m: float = _DRAFT_WP_ENDPOINT_EXCLUSION_M,
) -> list[tuple[float, float]]:
    """
    Estrae waypoint rappresentativi da un GPX (percorso realmente pedalato,
    Opzione D) via semplificazione Douglas-Peucker (shapely
    LineString.simplify), per precompilare il campo "Desired waypoints" del
    Planner quando si riparte da una route "solo D" (nessuna pianificazione
    salvata).

    target_count è il numero di waypoint desiderato dall'utente (slider in
    main.py) — la ricerca binaria sulla tolleranza (nello stesso sistema di
    coordinate del GPX, gradi) converge verso [target_count-target_margin,
    target_count+target_margin], non un numero esatto rigido: Douglas-Peucker
    è monotona (più tolleranza → uguali o meno punti sopravvissuti), quindi
    la ricerca binaria è valida entro questo margine. Se il range non viene
    mai raggiunto esattamente entro _DRAFT_WP_SEARCH_ITERATIONS iterazioni,
    ritorna il risultato più vicino incontrato durante la ricerca.

    Dopo la semplificazione, filtra: punti reciprocamente più vicini di
    min_spacing_m (cluster ridondanti che DP a volte lascia vicino a curve
    strette) e punti entro endpoint_exclusion_m dal punto di partenza/arrivo
    del tracciato stesso (ridondanti con lo start del loop, che l'utente
    configura a parte nel form — non toccato da questa funzione). Questi
    filtri si applicano sempre, indipendentemente da target_count: il
    risultato finale può quindi avere meno punti del target scelto.

    Ritorna [(lat, lon), ...] nell'ordine del tracciato — il tracciato
    stesso (non semplificato) se ha meno di target_count-target_margin punti.
    """
    target_min = max(2, target_count - target_margin)
    target_max = target_count + target_margin

    with open(gpx_path, "r") as f:
        gpx = gpxpy.parse(f)

    track_points = [
        (pt.latitude, pt.longitude)
        for track in gpx.tracks
        for segment in track.segments
        for pt in segment.points
    ]
    if len(track_points) < target_min:
        return track_points

    # shapely usa (x, y) = (lon, lat), non (lat, lon).
    line = LineString([(lon, lat) for lat, lon in track_points])

    lo, hi = _DRAFT_WP_TOLERANCE_LO_DEG, _DRAFT_WP_TOLERANCE_HI_DEG
    best_coords = list(line.coords)
    best_diff = abs(len(best_coords) - target_max)
    for _ in range(_DRAFT_WP_SEARCH_ITERATIONS):
        mid = (lo + hi) / 2
        coords = list(line.simplify(mid, preserve_topology=False).coords)
        n = len(coords)

        diff = 0 if target_min <= n <= target_max else min(abs(n - target_min), abs(n - target_max))
        if diff < best_diff:
            best_diff = diff
            best_coords = coords

        if target_min <= n <= target_max:
            break
        elif n > target_max:
            lo = mid  # troppi punti: serve più tolleranza (semplificazione più aggressiva)
        else:
            hi = mid  # troppo pochi punti: serve meno tolleranza

    waypoints = [(lat, lon) for lon, lat in best_coords]

    # Filtro 1: punti troppo vicini al proprio start/end del tracciato reale.
    start_pt, end_pt = track_points[0], track_points[-1]
    waypoints = [
        (lat, lon) for lat, lon in waypoints
        if geodesic((lat, lon), start_pt).meters >= endpoint_exclusion_m
        and geodesic((lat, lon), end_pt).meters >= endpoint_exclusion_m
    ]

    # Filtro 2: punti reciprocamente troppo vicini — greedy in ordine di
    # tracciato, tiene il primo di ogni gruppo ravvicinato.
    filtered: list[tuple[float, float]] = []
    for lat, lon in waypoints:
        if not filtered or geodesic((lat, lon), filtered[-1]).meters >= min_spacing_m:
            filtered.append((lat, lon))

    return filtered


if __name__ == "__main__":
    analysis = analyze_gpx(
        "routes/generated/test_wrapper.gpx",
        route_type="point_to_point",
        expected_end=(13.2400, 43.7200),
    )
    print(analysis)
