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


# ── Rilevamento tratti andata/ritorno sovrapposti ("corni") ───────────────────
# Un percorso può chiudersi correttamente (start==end) ed essere comunque "falso":
# va da A a B e ripercorre la stessa strada all'indietro invece di fare un anello
# vero. loop_closed non lo rileva perché guarda solo l'endpoint, non la forma.
#
# Ricerca esaustiva chiusura-based (porta remove_gpx_spurs.py dell'utente,
# verificata empiricamente contro l'approccio precedente "trova punti vicini
# poi raggruppa in run": quello vecchio perdeva sistematicamente i corni a
# forma di piccola ansa — dove solo i punti di giunzione coincidono, non
# l'intero percorso intermedio — perché richiedeva parallelismo punto-per-
# punto lungo tutta la deviazione: limite strutturale del metodo, non di
# soglia. Su due tracciati reali con 3 corni noti ciascuno, il metodo a "run"
# ne perdeva 1/3 in entrambi i casi). Per ogni coppia (start, end) con
# start < end: un corno è candidato se la percorrenza tra i due punti
# (excursion) è tra minimum_length_m e maximum_length_m, e la distanza in
# linea d'aria tra i due punti (closure) è sotto closure_radius_m.
_OAB_CLOSURE_RADIUS_M = 5.0      # sotto questa distanza in linea d'aria, due punti sono "lo stesso punto"
_OAB_MIN_LENGTH_M = 100.0        # percorrenza minima (andata+ritorno) per contare come corno
_OAB_MAX_LENGTH_M = 3_000.0      # percorrenza massima — oltre, non è più un "corno" ma parte del percorso
_OAB_PROTECTION_RADIUS_M = 50.0  # un corno è protetto se un waypoint mandatory è entro questa distanza
                                  # da QUALUNQUE punto del suo intervallo (non solo l'apice, a differenza
                                  # del check precedente) — più conservativo: meglio non tagliare per
                                  # errore un corno legato a un mandatory che il contrario. Un raggio di
                                  # 150m (come altrove nell'app) sarebbe qui eccessivo: con il check
                                  # sull'intero intervallo (decine di punti, non un singolo apice) 50m dà
                                  # già molte occasioni di match; 150m rischierebbe di proteggere corni
                                  # che passano solo vicino a un mandatory senza realmente servirlo,
                                  # vanificando il taglio automatico.

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


def _cumulative_distances_m(points: list[tuple[float, float]]) -> list[float]:
    cum = [0.0] * len(points)
    for i in range(1, len(points)):
        cum[i] = cum[i - 1] + _haversine_m(*points[i - 1], *points[i])
    return cum


def _segment_contains_protected_point(
    points: list[tuple[float, float]],
    start: int,
    end: int,
    protected_points: list[tuple[float, float]],
    protection_radius_m: float,
) -> bool:
    return any(
        _haversine_m(lat, lon, p_lat, p_lon) <= protection_radius_m
        for lat, lon in points[start:end + 1]
        for p_lat, p_lon in protected_points
    )


def _spur_apex_index(points: list[tuple[float, float]], start: int, end: int) -> int:
    """
    Punto di massima distanza in linea d'aria dal punto di ingresso del corno
    (points[start]) — la punta della deviazione. remove_gpx_spurs.py non
    calcola un apice esplicito (gli basta start:end per tagliare); qui serve
    per l'attribuzione spuntone→waypoint già esistente in candidate_generator
    (_attribute_cuts, _attribute_out_and_back) e per la nota informativa UI.
    """
    origin_lat, origin_lon = points[start]
    return max(
        range(start, end + 1),
        key=lambda i: _haversine_m(origin_lat, origin_lon, points[i][0], points[i][1]),
    )


class _Spur:
    __slots__ = ("start", "end", "excursion_m", "closure_m")

    def __init__(self, start: int, end: int, excursion_m: float, closure_m: float):
        self.start = start
        self.end = end
        self.excursion_m = excursion_m
        self.closure_m = closure_m


def _detect_spurs(
    points: list[tuple[float, float]],
    protected_points: list[tuple[float, float]],
    closure_radius_m: float = _OAB_CLOSURE_RADIUS_M,
    minimum_length_m: float = _OAB_MIN_LENGTH_M,
    maximum_length_m: float = _OAB_MAX_LENGTH_M,
    protection_radius_m: float = _OAB_PROTECTION_RADIUS_M,
) -> tuple[list["_Spur"], list["_Spur"]]:
    """
    Ricerca esaustiva di tutte le coppie (start, end) la cui percorrenza
    (excursion) è tra minimum_length_m e maximum_length_m e la cui distanza in
    linea d'aria (closure) è sotto closure_radius_m — porta 1:1 la logica di
    remove_gpx_spurs.detect_spurs (script dell'utente, verificato
    empiricamente contro il vecchio approccio "trova punti vicini poi
    raggruppa in run": quello vecchio perdeva strutturalmente i corni a forma
    di ansa, dove solo i punti di giunzione coincidono e non l'intero
    percorso intermedio). L'early break quando excursion supera
    maximum_length_m rende il costo O(n·k) — non O(n²) — dove k è il numero
    di punti entro maximum_length_m di percorrenza: fondamentale per le
    performance, non va rimosso.

    Tra candidati sovrapposti si tiene il più lungo (excursion_m maggiore) e
    si scartano quelli annidati — un singolo corno produce molte coppie
    annidate (indici via via più vicini al vertice), tenerle tutte
    duplicherebbe il taglio dello stesso corno.

    Ritorna (selected, protected_spurs): selected sono i corni tagliabili,
    protected_spurs quelli con un waypoint mandatory entro protection_radius_m
    da un punto qualunque del loro intervallo — mai tagliati, riportati solo
    per diagnosi (vedi _detect_out_and_back, out_and_back_attributions).
    Entrambe le liste sono ordinate per start crescente.
    """
    n = len(points)
    if n < 4:
        return [], []

    cum = _cumulative_distances_m(points)
    candidates: list[_Spur] = []

    for start in range(n):
        lat_start, lon_start = points[start]
        cum_start = cum[start]
        for end in range(start + 1, n):
            excursion = cum[end] - cum_start
            if excursion < minimum_length_m:
                continue
            if excursion > maximum_length_m:
                break

            closure = _haversine_m(lat_start, lon_start, points[end][0], points[end][1])
            if closure <= closure_radius_m:
                candidates.append(_Spur(start, end, excursion, closure))

    selected: list[_Spur] = []
    protected_spurs: list[_Spur] = []
    for candidate in sorted(candidates, key=lambda s: s.excursion_m, reverse=True):
        if any(
            not (candidate.end < existing.start or candidate.start > existing.end)
            for existing in selected + protected_spurs
        ):
            continue

        if _segment_contains_protected_point(
            points, candidate.start, candidate.end, protected_points, protection_radius_m
        ):
            protected_spurs.append(candidate)
        else:
            selected.append(candidate)

    return (
        sorted(selected, key=lambda s: s.start),
        sorted(protected_spurs, key=lambda s: s.start),
    )


def _detect_out_and_back(
    points: list[tuple[float, float]],
    closure_radius_m: float = _OAB_CLOSURE_RADIUS_M,
    minimum_length_m: float = _OAB_MIN_LENGTH_M,
    maximum_length_m: float = _OAB_MAX_LENGTH_M,
) -> dict:
    """
    Diagnosi sola-lettura: rileva TUTTI i corni presenti nel tracciato (non
    riceve protected_points, quindi _detect_spurs li mette tutti in
    "selected" — qui non interessa distinguere tagliabili da protetti, solo
    riportarli tutti) e ne calcola l'apice, per l'attribuzione a un waypoint
    (candidate_generator._attribute_out_and_back) e per out_and_back_percent.

    Ritorna {"percent": float, "apexes": [{"apex_lat","apex_lon","overlap_km"}]}.
    """
    n = len(points)
    if n < 4:
        return {"percent": 0.0, "apexes": []}

    total_m = _cumulative_distances_m(points)[-1]
    if total_m <= 0:
        return {"percent": 0.0, "apexes": []}

    spurs, _ = _detect_spurs(points, [], closure_radius_m, minimum_length_m, maximum_length_m)

    apexes = []
    overlapped_m = 0.0
    for spur in spurs:
        apex_idx = _spur_apex_index(points, spur.start, spur.end)
        apexes.append({
            "apex_lat": points[apex_idx][0],
            "apex_lon": points[apex_idx][1],
            "overlap_km": round(spur.excursion_m / 1000, 2),
        })
        overlapped_m += spur.excursion_m

    percent = round(min(100.0, 100.0 * overlapped_m / total_m), 1)
    return {"percent": percent, "apexes": apexes}


def _out_and_back_percent(
    points: list[tuple[float, float]],
    closure_radius_m: float = _OAB_CLOSURE_RADIUS_M,
    minimum_length_m: float = _OAB_MIN_LENGTH_M,
    maximum_length_m: float = _OAB_MAX_LENGTH_M,
) -> float:
    """Compatibilità: solo la percentuale, senza gli apici. Vedi _detect_out_and_back."""
    return _detect_out_and_back(points, closure_radius_m, minimum_length_m, maximum_length_m)["percent"]


# ── Fix A: taglio automatico delle deviazioni andata/ritorno ──────────────────
# Se BRouter passa due volte vicino allo stesso punto (bivio) con una
# deviazione significativa in mezzo, quella deviazione è sempre
# geometricamente "sbagliata" (un vero anello non ripercorre sé stesso) — va
# rimossa dal GPX finale, a meno che il corno non contenga un via-point
# mandatory (l'utente lo ha chiesto esplicitamente, va rispettato). Non tenta
# di spiegare la causa (waypoint soft mal posizionato, geocoding sbagliato,
# vicolo cieco reale): la taglia e basta, per definizione geometrica.


def cut_out_and_back_deviations(
    points: list[tuple[float, float]],
    elevations: list[float | None],
    protected_points: list[tuple[float, float]],
    closure_radius_m: float = _OAB_CLOSURE_RADIUS_M,
    minimum_length_m: float = _OAB_MIN_LENGTH_M,
    maximum_length_m: float = _OAB_MAX_LENGTH_M,
    protection_radius_m: float = _OAB_PROTECTION_RADIUS_M,
) -> dict:
    """
    Rimuove le deviazioni andata/ritorno da (points, elevations), ricucendo il
    tracciato direttamente al punto di bivio. Rilevamento in singola passata
    (_detect_spurs, ricerca esaustiva chiusura-based) — non un ciclo
    "taglia→ririleva→taglia": i corni selezionati sono per costruzione non
    sovrapposti (già deduplicati in _detect_spurs), quindi si tagliano tutti
    in un colpo solo, in ordine decrescente di start per non invalidare gli
    indici dei tagli successivi (come remove_gpx_spurs.py). Il vecchio ciclo
    iterativo — ririlevava da zero sull'intero tracciato dopo ogni taglio,
    O(n²) per iterazione — non terminava (>2m39s, interrotto manualmente) su
    un candidate reale di 2143 punti; la singola passata di rilevamento qui
    resta nell'ordine dei secondi anche a 10-12mila punti.

    Un corno è "protetto" (mai tagliato) se un waypoint mandatory è entro
    protection_radius_m da un punto qualunque del suo intervallo — vedi
    _detect_spurs e _OAB_PROTECTION_RADIUS_M.

    Dopo il taglio, un solo giro di verifica (self-check, come
    remove_gpx_spurs.py): ririleva sul tracciato tagliato con le stesse
    soglie e segnala se restano corni tagliabili — caso limite raro (es. due
    corni adiacenti la cui unione supera minimum_length_m solo dopo aver
    rimosso quello in mezzo), da loggare più che correggere all'infinito con
    un altro ciclo automatico.

    Ritorna {"points", "elevations", "kept_indices", "cuts"}:
      - kept_indices: indici nell'array points/elevations ORIGINALE dei punti
        sopravvissuti (stesso ordine) — utile a chi deve ritagliare una lista
        parallela di oggetti (es. i GPXTrackPoint originali) senza doverli
        ricostruire da lat/lon.
      - cuts: [{"apex_lat","apex_lon","overlap_km"}], ordinati per start
        crescente (ordine lungo il tracciato originale).
    """
    pts = list(points)
    eles = list(elevations)
    kept_indices = list(range(len(points)))

    selected, _protected_spurs = _detect_spurs(
        pts, protected_points, closure_radius_m, minimum_length_m, maximum_length_m, protection_radius_m
    )

    cuts = []
    for spur in selected:
        apex_idx = _spur_apex_index(pts, spur.start, spur.end)
        cuts.append({
            "apex_lat": pts[apex_idx][0],
            "apex_lon": pts[apex_idx][1],
            "overlap_km": round(spur.excursion_m / 1000, 2),
        })

    # Conserva il punto di diramazione (spur.start); il punto finale
    # (spur.end) è praticamente coincidente ed è eliminato con l'escursione.
    for spur in sorted(selected, key=lambda s: s.start, reverse=True):
        pts, eles, kept_indices = _splice_out_range(pts, eles, kept_indices, spur.start + 1, spur.end)

    if selected:
        remaining, _ = _detect_spurs(
            pts, protected_points, closure_radius_m, minimum_length_m, maximum_length_m, protection_radius_m
        )
        if remaining:
            print(
                f"cut_out_and_back_deviations: {len(remaining)} corno/i tagliabile/i residuo/i "
                "dopo il taglio (self-check) — non ritagliati automaticamente."
            )

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
# Ricerca esaustiva su griglia ricampionata a passo fisso (porta
# analisi_salita.py dell'utente, validata sui casi reali noti — vedi
# _resample_track_for_climbs/detect_climbs più sotto): PRIMA di qualunque
# calcolo, l'intero tracciato è ricampionato per interpolazione a passo
# uniforme _CLIMB_RESAMPLE_STEP_M. Fix strutturale: nella versione precedente
# l'ampiezza EFFETTIVA di ogni finestra di calcolo dipendeva da quanto erano
# fitti i punti GPS/BRouter grezzi — in tratti radi pochi punti concordi
# producevano pendenze fisicamente implausibili (35-39%), non correggibili
# con più smoothing senza appiattire dati puliti altrove. Su griglia uniforme
# ogni finestra (100/200/500m) copre sempre la stessa distanza reale,
# indipendentemente dalla densità sorgente.
_CLIMB_RESAMPLE_STEP_M = 10.0       # passo di ricampionamento uniforme lungo il tracciato
_CLIMB_SMOOTH_RADIUS_PTS = 5        # raggio finestra triangolare, in punti (5×10m per lato ≈ 100m totali)
_CLIMB_START_GRADE_PCT = 2.0        # gradiente rolling sui 200m per riconoscere l'inizio di una salita
_CLIMB_START_WINDOW_M = 200.0
_CLIMB_END_STALL_M = 250.0          # fine salita: 250m senza un nuovo massimo di quota (assorbe brevi falsopiani)
_CLIMB_MIN_LENGTH_M = 400.0         # sotto questa lunghezza non è una salita vera (era 200m)
_CLIMB_MIN_GAIN_M = 20.0            # sotto questo dislivello non è una salita vera (nuovo requisito)
_CLIMB_MERGE_GAP_M = 200.0          # salite separate da meno di questo si fondono...
_CLIMB_MERGE_MAX_DROP_M = 8.0       # ...se il calo di quota nel mezzo non supera questo (evita di fondere due cime distinte)
_CLIMB_GRAD_WINDOW_100M = 100.0     # tre orizzonti di pendenza per ogni salita, non un solo "max_gradient_percent"
_CLIMB_GRAD_WINDOW_200M = 200.0
_CLIMB_GRAD_WINDOW_500M = 500.0

# max_gradient_percent, esposto per compatibilità, è un ALIAS di max_200m_pct
# (non max_100m_pct): è il valore più rappresentativo da mostrare come singolo
# numero nelle viste che non elencano i tre orizzonti — vedi Via Silente-tappa_5
# nella verifica: max_100m=37.6%, max_200m=28.7%, max_500m=27.0%, il 100m da
# solo è un picco isolato fuorviante come "la" pendenza della salita.
#
# max_gradient_low_confidence resta invece ancorato a max_100m_pct — è
# l'orizzonte più sensibile, quello che cattura davvero un picco isolato
# implausibile; il ricampionamento a griglia fissa risolve il bug strutturale
# della versione precedente (finestra la cui ampiezza effettiva dipendeva
# dalla densità dei punti sorgente) ma NON fa sparire un dato sorgente
# realmente anomalo su un tratto breve — verificato: sulla salita incriminata
# max_100m_pct=37.6% con media=10.2% (rapporto 3.68×) continua a superare
# entrambe le soglie sotto, quindi il flag resta necessario, solo ancorato
# all'orizzonte giusto invece che a un "max_gradient_percent" ambiguo.
_MAX_GRAD_LOW_CONFIDENCE_ABS_PCT = 30.0    # sopra questa pendenza assoluta (sui 100m), implausibile su strada reale
_MAX_GRAD_LOW_CONFIDENCE_RATIO = 2.5       # E il picco sui 100m è più di 2.5× la pendenza media della salita

# Smoothing elevazione per elevation_gain_m/elevation_loss_m (analyze_gpx,
# ride_analysis_agent.analyze_gpx_bytes) — pipeline DISTINTA da quella di
# detect_climbs() qui sotto, deliberatamente non unificata: il filtro a
# mediana pre-resample qui sotto è stato validato empiricamente contro il
# rumore a dente di sega di SRTM (±30-65% su pochi metri, sistemico su
# Cilento/Via Silente) e sostituirlo con la sola media triangolare del nuovo
# algoritmo salite rischierebbe di reintrodurre quel bug sul dislivello
# totale, senza che i tre file di verifica di questa sessione lo coprano
# (nessuna regressione osservata, ma nessuna prova a favore nemmeno). Unificare
# è un lavoro di validazione a sé, non sproporzionato ma indipendente da
# questo step — vedi nota architetturale in detect_climbs().
_ELEV_MEDIAN_WINDOW_M = 40.0  # finestra mediana (distanza reale) per abbattere outlier isolati
_ELEV_SMOOTH_WINDOW_M = 75.0  # media mobile leggera sul residuo, dopo ricampionamento (50-100m)

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


def _classify_climb(avg_gradient_percent: float) -> str:
    """
    Niente più "strappo_breve": nasceva per una salita breve (<300m) e ripida
    (≥8%) rilevata col vecchio minimo di 200m — con _CLIMB_MIN_LENGTH_M ora a
    400m nessuna salita rilevata può più essere sotto i 300m, il ramo era
    diventato irraggiungibile.
    """
    if avg_gradient_percent < _GRAD_DOLCE_MAX_PCT:
        return "dolce"
    if avg_gradient_percent < _GRAD_MODERATA_MAX_PCT:
        return "moderata"
    return "impegnativa"


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


def _resample_track_for_climbs(
    distances_cumulative_m: list[float],
    elevations_m: list[float | None],
    coordinates: list[tuple[float, float]],
    step_m: float,
) -> tuple[list[float], list[float], list[tuple[float, float]]]:
    """
    Ricampiona distanza/elevazione/coordinate a passo fisso uniforme per
    interpolazione lineare — pipeline dedicata a detect_climbs(), separata da
    smooth_elevations() (vedi nota architetturale sopra _ELEV_MEDIAN_WINDOW_M).
    Interpola anche lat/lon insieme all'elevazione: servono per hard_lat/
    hard_lon (punto più duro di ogni salita — attribuzione zona, danger
    waypoint in GPX Optimizer). Scarta i punti senza elevazione prima di
    ricampionare. Ritorna liste vuote se i dati validi sono insufficienti.
    """
    valid = [
        (d, e, c) for d, e, c in zip(distances_cumulative_m, elevations_m, coordinates)
        if e is not None
    ]
    if len(valid) < 4:
        return [], [], []

    xs = [v[0] for v in valid]
    ys = [v[1] for v in valid]
    cs = [v[2] for v in valid]
    total = xs[-1]
    if total <= 0:
        return [], [], []

    n_steps = max(1, int(total // step_m))
    rd = [i * step_m for i in range(n_steps + 1)]
    if rd[-1] < total:
        rd.append(total)

    resampled_elev: list[float] = []
    resampled_coords: list[tuple[float, float]] = []
    j = 0
    for d in rd:
        while j < len(xs) - 2 and xs[j + 1] < d:
            j += 1
        x0, x1 = xs[j], xs[j + 1]
        t = 0.0 if x1 == x0 else (d - x0) / (x1 - x0)
        t = min(1.0, max(0.0, t))
        resampled_elev.append(ys[j] + t * (ys[j + 1] - ys[j]))
        lat0, lon0 = cs[j]
        lat1, lon1 = cs[j + 1]
        resampled_coords.append((lat0 + t * (lat1 - lat0), lon0 + t * (lon1 - lon0)))

    return rd, resampled_elev, resampled_coords


def _triangular_smooth(values: list[float], radius_pts: int) -> list[float]:
    """
    Media pesata triangolare (picco al centro, decrescente linearmente verso i
    bordi) su serie a passo uniforme — porta 1:1 lo smoothing di
    analisi_salita.py, usato SOLO da detect_climbs(). Con radius_pts=5 e passo
    10m copre una finestra di circa 100m.
    """
    n = len(values)
    if n == 0:
        return []
    weights = list(range(1, radius_pts + 2)) + list(range(radius_pts, 0, -1))
    out = []
    for i in range(n):
        start, end = max(0, i - radius_pts), min(n, i + radius_pts + 1)
        selected = weights[radius_pts - (i - start): radius_pts + (end - i)]
        out.append(sum(values[j] * w for j, w in zip(range(start, end), selected)) / sum(selected))
    return out


def _rolling_grade_pct(elevations_m: list[float], index: int, window_m: float, step_m: float) -> float:
    """Pendenza tra elevations_m[index] e il punto ~window_m più avanti sulla griglia uniforme
    (o l'ultimo punto disponibile, se la salita finisce prima della finestra)."""
    count = max(1, round(window_m / step_m))
    end = min(len(elevations_m) - 1, index + count)
    actual_m = (end - index) * step_m
    return 0.0 if actual_m == 0 else (elevations_m[end] - elevations_m[index]) / actual_m * 100.0


def detect_climbs(
    distances_cumulative_m: list[float],
    elevations_m: list[float | None],
    coordinates: list[tuple[float, float]],
) -> dict:
    """
    Rileva le salite lungo un tracciato a partire da distanza cumulativa (m),
    elevazione (m) e coordinate (lat, lon) per punto (stesso ordine/lunghezza
    di points). Ricampiona l'intero tracciato a passo fisso _CLIMB_RESAMPLE_STEP_M
    prima di qualunque calcolo (vedi nota in testa alla sezione) — pipeline
    separata da smooth_elevations()/elevation_gain_m.

    Rilevamento guidato dal gradiente rolling sui 200m (non dal delta
    punto-a-punto): una salita nasce quando questo gradiente raggiunge
    _CLIMB_START_GRADE_PCT; il punto più alto raggiunto (best) assorbe brevi
    falsopiani; la salita finisce dopo _CLIMB_END_STALL_M senza un nuovo
    massimo. Richiede lunghezza ≥_CLIMB_MIN_LENGTH_M E dislivello
    ≥_CLIMB_MIN_GAIN_M per essere considerata una salita vera. Salite separate
    da un gap breve (≤_CLIMB_MERGE_GAP_M) con un calo di quota modesto nel
    mezzo (≤_CLIMB_MERGE_MAX_DROP_M) vengono fuse in una sola.

    Ritorna:
      "climbs": list[dict] — una per salita rilevata:
        start_km, length_m, elevation_gain_m, avg_gradient_percent,
        max_gradient_percent (alias di max_200m_pct, per compatibilità),
        max_100m_pct/max_200m_pct/max_500m_pct (tre orizzonti di pendenza),
        hard_start_km/hard_lat/hard_lon (punto di inizio del tratto più duro,
        cioè quello con la pendenza sui 200m più alta), max_gradient_low_confidence
        (ancorato a max_100m_pct — vedi costanti), classification
        (dolce/moderata/impegnativa), has_steep_section (bool), note (str|None),
        zone (str, vuota — riservato a un futuro collegamento col reverse
        geocoding, non popolato in questo step: vedi valutazione consegnata).
      "profile_distances_km"/"profile_elevations_m"/"profile_colors": profilo
        ricampionato/smoothed per il grafico altimetrico, stesso index.
    """
    empty = {
        "climbs": [],
        "profile_distances_km": [],
        "profile_elevations_m": [],
        "profile_colors": [],
    }

    rd, resampled_elev, resampled_coords = _resample_track_for_climbs(
        distances_cumulative_m, elevations_m, coordinates, _CLIMB_RESAMPLE_STEP_M
    )
    n = len(rd)
    if n < 4:
        return empty

    smoothed = _triangular_smooth(resampled_elev, _CLIMB_SMOOTH_RADIUS_PTS)

    # Gradiente istantaneo punto-per-punto sulla serie smoothed (solo per il colore del grafico)
    grad = [0.0] * n
    for i in range(1, n):
        dd = rd[i] - rd[i - 1]
        grad[i] = (smoothed[i] - smoothed[i - 1]) / dd * 100.0 if dd > 0 else 0.0
    if n > 1:
        grad[0] = grad[1]
    profile_colors = [gradient_color(g) for g in grad]

    # ── Rilevamento: stato "in salita" guidato dal gradiente rolling 200m ──
    active = False
    start = best = 0
    best_ele = smoothed[0]
    candidates: list[tuple[int, int]] = []
    for i in range(n):
        grade200 = _rolling_grade_pct(smoothed, i, _CLIMB_START_WINDOW_M, _CLIMB_RESAMPLE_STEP_M)
        if not active and grade200 >= _CLIMB_START_GRADE_PCT:
            active, start, best, best_ele = True, i, i, smoothed[i]
        if active:
            if smoothed[i] > best_ele:
                best, best_ele = i, smoothed[i]
            if (i - best) * _CLIMB_RESAMPLE_STEP_M >= _CLIMB_END_STALL_M or i == n - 1:
                if ((best - start) * _CLIMB_RESAMPLE_STEP_M >= _CLIMB_MIN_LENGTH_M
                        and best_ele - smoothed[start] >= _CLIMB_MIN_GAIN_M):
                    candidates.append((start, best))
                active = False

    # ── Fusione salite separate da un gap breve con calo di quota modesto ──
    merged: list[list[int]] = []
    for c_start, c_end in candidates:
        if (merged
                and (rd[c_start] - rd[merged[-1][1]]) <= _CLIMB_MERGE_GAP_M
                and smoothed[c_start] >= smoothed[merged[-1][1]] - _CLIMB_MERGE_MAX_DROP_M):
            merged[-1][1] = c_end
        else:
            merged.append([c_start, c_end])

    climbs = []
    for start_idx, end_idx in merged:
        grades100 = [_rolling_grade_pct(smoothed, i, _CLIMB_GRAD_WINDOW_100M, _CLIMB_RESAMPLE_STEP_M)
                     for i in range(start_idx, end_idx + 1)]
        grades200 = [_rolling_grade_pct(smoothed, i, _CLIMB_GRAD_WINDOW_200M, _CLIMB_RESAMPLE_STEP_M)
                     for i in range(start_idx, end_idx + 1)]
        grades500 = [_rolling_grade_pct(smoothed, i, _CLIMB_GRAD_WINDOW_500M, _CLIMB_RESAMPLE_STEP_M)
                     for i in range(start_idx, end_idx + 1)]

        max_100m_pct = max(grades100)
        max_200m_pct, hard_offset = max((g, i) for i, g in enumerate(grades200))
        max_500m_pct = max(grades500)
        hard_index = start_idx + hard_offset

        length_m = max(1.0, (end_idx - start_idx) * _CLIMB_RESAMPLE_STEP_M)
        elevation_gain_m = smoothed[end_idx] - smoothed[start_idx]
        avg_gradient_percent = elevation_gain_m / length_m * 100.0
        max_gradient_percent = max_200m_pct  # alias di compatibilità — vedi nota sopra le costanti

        # Confidenza ancorata a max_100m_pct (l'orizzonte più sensibile ai
        # picchi isolati) — implausibile in assoluto E molto più ripido della
        # media della salita, entrambe le condizioni. Segnalato, non nascosto.
        max_gradient_low_confidence = (
            max_100m_pct > _MAX_GRAD_LOW_CONFIDENCE_ABS_PCT
            and (max_100m_pct / avg_gradient_percent) > _MAX_GRAD_LOW_CONFIDENCE_RATIO
        )

        classification = _classify_climb(avg_gradient_percent)
        has_steep_section = (
            max_gradient_percent >= _GRAD_MODERATA_MAX_PCT
            and avg_gradient_percent < _GRAD_MODERATA_MAX_PCT
        )

        hard_lat, hard_lon = resampled_coords[hard_index]

        climbs.append({
            "start_km": round(rd[start_idx] / 1000, 2),
            "length_m": round(length_m, 0),
            "elevation_gain_m": round(elevation_gain_m, 1),
            "avg_gradient_percent": round(avg_gradient_percent, 1),
            "max_gradient_percent": round(max_gradient_percent, 1),
            "max_100m_pct": round(max_100m_pct, 1),
            "max_200m_pct": round(max_200m_pct, 1),
            "max_500m_pct": round(max_500m_pct, 1),
            "hard_start_km": round(rd[hard_index] / 1000, 2),
            "hard_lat": hard_lat,
            "hard_lon": hard_lon,
            "max_gradient_low_confidence": max_gradient_low_confidence,
            "classification": classification,
            "classification_emoji": _CLIMB_CLASS_EMOJI[classification],
            "has_steep_section": has_steep_section,
            "note": (
                f"contiene un tratto al {max_gradient_percent:.0f}%"
                if has_steep_section else None
            ),
            "zone": "",
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

    climb_data = detect_climbs(cum_m, [p.elevation for p in points], [(p.latitude, p.longitude) for p in points])

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
