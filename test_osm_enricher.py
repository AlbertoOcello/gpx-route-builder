"""
OSM Tag Enricher — Proof of Concept (Fase 9 / SRS §5.5).

Strategia: campionamento ogni SAMPLE_INTERVAL_M metri → query Overpass batched
(BATCH_SIZE punti per chiamata API con `out body geom`) → matching geometrico
punto→way più vicino → classificazione surface/highway → aggregazione percentuali.

Vantaggi rispetto a query per-punto:
  - Campionamento a 1000m → ~58 punti per 58km
  - 10 punti per batch → 6 chiamate API, 10s di pausa tra batch
  - Runtime atteso: ~70-90 s (6 × 10s sleep + latenza)
  - Rate limiting cortese: nessun 429 in condizioni normali

Uso: venv/bin/python test_osm_enricher.py [<gpx_path>]
Senza argomenti usa il candidato C da data/last_test_candidates.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import gpxpy
import httpx
from geopy.distance import geodesic

# ── Costanti ──────────────────────────────────────────────────────────────────

OVERPASS_URL    = "https://overpass-api.de/api/interpreter"
SAMPLE_INTERVAL = 1000         # metri tra un campione e il successivo (1km → ~58 punti per 58km)
BATCH_SIZE      = 10           # punti per chiamata Overpass (piccoli batch → meno 429)
AROUND_RADIUS   = 50           # raggio ricerca way (m) — 50m copre node spacing tipico OSM
MAX_ASSIGN_DIST = 80           # distanza max per assegnare un way a un campione (m)
REQUEST_TIMEOUT = 45.0         # timeout httpx per ogni query Overpass
SLEEP_BETWEEN    = float(
    __import__("os").environ.get("OSM_SLEEP_BETWEEN", "5")
)                              # override: OSM_SLEEP_BETWEEN=60 per cooldown dopo rate limit
RETRY_DELAYS_406 = [2, 4, 8, 15, 25]  # backoff per 406 (CDN backend errato — non è rate limit)
RETRY_DELAYS_504 = [10, 25]    # backoff secondi per 504 (server overload)
RETRY_DELAYS_429 = [30, 60]    # backoff secondi per 429 (rate limit — aspetta molto di più)
PARTIAL_THRESHOLD = 0.35       # se >35% campioni falliscono → "partial"

# ── Surface/highway classification ────────────────────────────────────────────

_ASPHALT_SURFACES = frozenset({
    "asphalt", "paved", "concrete", "tarmac", "bituminous",
    "concrete:plates", "concrete:lanes",
})
_GRAVEL_SURFACES = frozenset({
    "gravel", "fine_gravel", "compacted", "dirt", "unpaved",
    "ground", "sand", "earth", "mud", "grass", "woodchips",
})
_COBBLE_SURFACES = frozenset({
    "cobblestone", "sett", "paving_stones", "cobbles",
    "unhewn_cobblestone", "cobblestone:flattened",
})
_MAIN_HIGHWAYS = frozenset({
    "primary", "trunk", "motorway",
    "primary_link", "trunk_link", "motorway_link",
})
_SECONDARY_HIGHWAYS = frozenset({
    "secondary", "secondary_link",
})
_TRAIL_HIGHWAYS = frozenset({
    "path", "track", "bridleway", "cycleway",
})
_PEDESTRIAN_HIGHWAYS = frozenset({
    "footway", "pedestrian", "steps", "elevator", "corridor",
})


def _classify(tags: dict) -> dict:
    """
    Classifica i tag OSM di un way in categorie di superficie e tipo di strada.
    Quando manca il tag 'surface' usa tracktype e highway come inference.
    Ritorna dict con flags booleani + metadati di debug.
    """
    surface   = tags.get("surface",   "").lower().strip()
    highway   = tags.get("highway",   "").lower().strip()
    tracktype = tags.get("tracktype", "").lower().strip()
    ref       = tags.get("ref",       "").upper()

    is_asphalt     = False
    is_gravel      = False
    is_cobblestone = False
    inferred       = False

    if surface in _ASPHALT_SURFACES:
        is_asphalt = True
    elif surface in _GRAVEL_SURFACES:
        is_gravel = True
    elif surface in _COBBLE_SURFACES:
        is_cobblestone = True
    else:
        # Nessun tag surface → inference da tracktype o highway
        inferred = True
        if tracktype == "grade1":
            is_asphalt = True
        elif tracktype in {"grade2", "grade3", "grade4", "grade5"}:
            is_gravel = True
        elif highway == "track":
            is_gravel = True
        elif highway in {"path", "bridleway"}:
            is_gravel = True
        elif highway in {"cycleway", "residential", "unclassified", "tertiary",
                         "secondary", "primary", "trunk", "service",
                         "living_street", "road"}:
            is_asphalt = True
        else:
            is_asphalt = True  # default sicuro per highway sconosciuto

    return {
        "is_asphalt":     is_asphalt,
        "is_gravel":      is_gravel,
        "is_cobblestone": is_cobblestone,
        "is_main_road":   highway in _MAIN_HIGHWAYS,        # primary/trunk/motorway
        "is_secondary":   highway in _SECONDARY_HIGHWAYS,   # secondary — accettabile in bici
        "is_trail":       highway in _TRAIL_HIGHWAYS,        # path/track/bridleway/cycleway
        "is_pedestrian":  highway in _PEDESTRIAN_HIGHWAYS,
        "ss16_detected":  "SS16" in ref or "SS 16" in ref,
        "inferred":       inferred,
        "_surface":       surface or "(nessuno)",
        "_highway":       highway,
        "_tracktype":     tracktype,
    }


# ── Sampling ──────────────────────────────────────────────────────────────────

def sample_track(gpx_path: str, interval_m: int = SAMPLE_INTERVAL) -> list[tuple[float, float]]:
    """
    Campiona la traccia GPX ogni interval_m metri.
    Ritorna lista di (lat, lon) inclusi primo e ultimo punto.
    """
    with open(gpx_path) as f:
        gpx = gpxpy.parse(f)

    all_pts = [
        pt for track in gpx.tracks
        for seg in track.segments
        for pt in seg.points
    ]
    if not all_pts:
        return []

    sampled = [(all_pts[0].latitude, all_pts[0].longitude)]
    acc = 0.0

    for i in range(1, len(all_pts)):
        p1, p2 = all_pts[i - 1], all_pts[i]
        acc += geodesic(
            (p1.latitude, p1.longitude),
            (p2.latitude, p2.longitude),
        ).meters
        if acc >= interval_m:
            sampled.append((p2.latitude, p2.longitude))
            acc = 0.0

    # Assicura che l'ultimo punto sia incluso
    last = (all_pts[-1].latitude, all_pts[-1].longitude)
    if sampled[-1] != last:
        sampled.append(last)

    return sampled


# ── Overpass query ─────────────────────────────────────────────────────────────

def _build_query(points: list[tuple[float, float]], radius_m: int) -> str:
    """Costruisce la query Overpass per un batch di punti."""
    union = "\n".join(
        f'  way(around:{radius_m},{lat},{lon})["highway"];'
        for lat, lon in points
    )
    return (
        f"[out:json][timeout:30];\n"
        f"(\n{union}\n);\n"
        f"out body geom;\n"
    )


def _query_overpass(points: list[tuple[float, float]], radius_m: int = AROUND_RADIUS) -> list[dict] | None:
    """
    Chiama Overpass per un batch di punti.
    Ritorna lista di way dicts (con tags + geometry) o None in caso di errore definitivo.

    Quirk server: il body deve essere raw (non URL-encoded) e lo User-Agent deve essere
    curl-like — Python UA + valori percent-encoded → HTTP 406.
    Retry separati per 429 (rate limit → lunga attesa) e 504 (overload → attesa media).
    406 non è ritentato: è un errore permanente del batch, viene saltato.
    """
    query = _build_query(points, radius_m)
    raw_body = ("data=" + query).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "curl/8.7.1",
    }

    attempts_406 = list(RETRY_DELAYS_406) + [None]
    attempts_504 = list(RETRY_DELAYS_504) + [None]
    attempts_429 = list(RETRY_DELAYS_429) + [None]
    idx_406 = idx_504 = idx_429 = 0

    while True:
        try:
            resp = httpx.post(OVERPASS_URL, content=raw_body, headers=headers,
                              timeout=REQUEST_TIMEOUT)

            if resp.status_code == 200:
                return [e for e in resp.json().get("elements", [])
                        if e.get("type") == "way"]

            elif resp.status_code in (504, 503):
                delay = attempts_504[idx_504] if idx_504 < len(attempts_504) else None
                if delay is not None:
                    print(f"    HTTP {resp.status_code} — retry in {delay}s...")
                    time.sleep(delay)
                    idx_504 += 1
                    continue
                return None

            elif resp.status_code == 429:
                delay = attempts_429[idx_429] if idx_429 < len(attempts_429) else None
                if delay is not None:
                    print(f"    HTTP 429 rate-limit — attendo {delay}s prima di riprovare...")
                    time.sleep(delay)
                    idx_429 += 1
                    continue
                return None

            elif resp.status_code == 406:
                # Transient: CDN round-robin hits a broken backend. Short delays.
                delay = attempts_406[idx_406] if idx_406 < len(attempts_406) else None
                if delay is not None:
                    print(f"    HTTP 406 (backend errato) — retry in {delay}s...")
                    time.sleep(delay)
                    idx_406 += 1
                    continue
                print(f"    HTTP 406 — batch saltato dopo {len(RETRY_DELAYS_406)} retry")
                return None

            else:
                print(f"    HTTP {resp.status_code} — batch saltato")
                return None

        except httpx.TimeoutException:
            delay = attempts_504[idx_504] if idx_504 < len(attempts_504) else None
            if delay is not None:
                print(f"    Timeout — retry in {delay}s...")
                time.sleep(delay)
                idx_504 += 1
                continue
            return None
        except Exception as e:
            print(f"    Errore rete: {e}")
            return None


# ── Geometry matching ─────────────────────────────────────────────────────────

def _way_min_dist(lat: float, lon: float, way: dict) -> float:
    """Distanza minima da (lat,lon) a qualsiasi nodo della geometria del way."""
    geom = way.get("geometry", [])
    if not geom:
        return float("inf")
    return min(
        geodesic((lat, lon), (g["lat"], g["lon"])).meters
        for g in geom
    )


def _assign_nearest(
    sample_points: list[tuple[float, float]],
    ways: list[dict],
    max_dist: float = MAX_ASSIGN_DIST,
) -> list[dict | None]:
    """
    Per ogni campione trova il way più vicino (distanza ≤ max_dist m).
    Preferisce way con tag surface esplicito e non-pedonali su pedestrian/steps.
    """
    assigned = []
    for lat, lon in sample_points:
        candidates: list[tuple[float, dict]] = []
        for way in ways:
            d = _way_min_dist(lat, lon, way)
            if d <= max_dist:
                candidates.append((d, way))

        if not candidates:
            assigned.append(None)
            continue

        candidates.sort(key=lambda x: x[0])

        # Preferisci un way non-pedonale se esiste entro 15m in più del più vicino
        nearest_dist, nearest_way = candidates[0]
        hw0 = nearest_way.get("tags", {}).get("highway", "")
        if hw0 in _PEDESTRIAN_HIGHWAYS:
            for d, w in candidates[1:]:
                if d - nearest_dist <= 15:
                    if w.get("tags", {}).get("highway", "") not in _PEDESTRIAN_HIGHWAYS:
                        nearest_way = w
                        break

        assigned.append(nearest_way)

    return assigned


# ── Aggregazione ──────────────────────────────────────────────────────────────

def aggregate(classifications: list[dict | None], n_total: int) -> dict:
    """
    Calcola percentuali e flag booleani dalla lista di classificazioni.

    n_total: numero totale di campioni (inclusi quelli non risolti), usato per
    calcolare unresolved_percent sempre visibile in output.
    """
    valid = [c for c in classifications if c is not None]
    n_resolved = len(valid)

    asphalt   = sum(1 for c in valid if c["is_asphalt"])
    gravel    = sum(1 for c in valid if c["is_gravel"])
    cobble    = sum(1 for c in valid if c["is_cobblestone"])
    main      = sum(1 for c in valid if c["is_main_road"])
    secondary = sum(1 for c in valid if c["is_secondary"])
    trail     = sum(1 for c in valid if c["is_trail"])
    ss16      = any(c["ss16_detected"] for c in valid)
    inferred  = sum(1 for c in valid if c["inferred"])

    t = n_resolved or 1  # evita divisione per zero

    return {
        "gravel_percent":      round(gravel    / t * 100, 1),
        "asphalt_percent":     round(asphalt   / t * 100, 1),
        "cobblestone_percent": round(cobble    / t * 100, 1),
        "main_road_percent":   round(main      / t * 100, 1),   # primary/trunk/motorway
        "secondary_percent":   round(secondary / t * 100, 1),   # secondary (SP) — accettabile
        "trail_percent":       round(trail     / t * 100, 1),   # path/track/bridleway/cycleway
        "trail_count":         trail,                            # numero assoluto campioni sentiero
        "cobblestone_detected": cobble > 0,
        "ss16_detected":       ss16,
        "inferred_percent":    round(inferred  / t * 100, 1),
        "unresolved_percent":  round((n_total - n_resolved) / n_total * 100, 1),
    }


# ── Funzione principale ───────────────────────────────────────────────────────

def enrich_gpx(gpx_path: str, verbose: bool = True) -> dict:
    """
    Arricchisce un file GPX con dati di superficie OSM (SRS §5.5).

    Ritorna dict con:
      gravel_percent, asphalt_percent, main_road_percent, cobblestone_detected,
      ss16_detected, partial, samples_total, samples_resolved, failure_rate,
      inferred_percent, elapsed_s
    """
    t0 = time.time()

    # Step 1: Campionamento
    sample_pts = sample_track(gpx_path, SAMPLE_INTERVAL)
    n = len(sample_pts)
    if verbose:
        print(f"  Campioni ogni {SAMPLE_INTERVAL} m → {n} punti da interrogare")

    # Step 2: Query Overpass a batch
    batches = [sample_pts[i: i + BATCH_SIZE] for i in range(0, n, BATCH_SIZE)]
    if verbose:
        print(f"  {len(batches)} batch da ≤{BATCH_SIZE} punti — "
              f"raggio={AROUND_RADIUS}m — sleep={SLEEP_BETWEEN}s tra batch")

    all_classifications: list[dict | None] = []
    batch_failures = 0

    for b_idx, batch in enumerate(batches):
        if verbose:
            print(f"  Batch {b_idx + 1}/{len(batches)} ({len(batch)} punti)...", end=" ", flush=True)

        t_batch = time.time()
        ways = _query_overpass(batch)
        elapsed_batch = time.time() - t_batch

        if ways is None:
            # Fallimento definitivo del batch
            batch_failures += len(batch)
            all_classifications.extend([None] * len(batch))
            if verbose:
                print(f"FALLITO ({elapsed_batch:.1f}s)")
        else:
            assigned = _assign_nearest(batch, ways, MAX_ASSIGN_DIST)
            classed  = [_classify(w["tags"]) if w else None for w in assigned]
            all_classifications.extend(classed)
            resolved = sum(1 for c in classed if c is not None)
            if verbose:
                print(f"{resolved}/{len(batch)} risolti  ({elapsed_batch:.1f}s, {len(ways)} way trovati)")

        if b_idx < len(batches) - 1:
            time.sleep(SLEEP_BETWEEN)

    # Step 3: Verifica threshold "partial"
    n_resolved   = sum(1 for c in all_classifications if c is not None)
    failure_rate = (n - n_resolved) / n
    partial      = failure_rate > PARTIAL_THRESHOLD

    result: dict = {
        "partial":            partial,
        "samples_total":      n,
        "samples_resolved":   n_resolved,
        "failure_rate":       round(failure_rate, 3),
        "unresolved_percent": round(failure_rate * 100, 1),  # sempre visibile per trasparenza UI
        "elapsed_s":          round(time.time() - t0, 1),
    }

    if partial:
        result["reason"] = (
            f"Troppi campioni non risolti ({failure_rate:.0%} > {PARTIAL_THRESHOLD:.0%}). "
            "I dati OSM non sono affidabili — mantenere i placeholder."
        )
    else:
        result.update(aggregate(all_classifications, n))

    return result


# ── Debug table ───────────────────────────────────────────────────────────────

def _debug_sample(gpx_path: str, n_show: int = 15):
    """Mostra la classificazione dei primi n campioni per ispezione visiva."""
    sample_pts = sample_track(gpx_path, SAMPLE_INTERVAL)[:n_show]
    ways = _query_overpass(sample_pts) or []
    assigned = _assign_nearest(sample_pts, ways)

    print(f"\n{'#':<4} {'lat':>10} {'lon':>10}  {'dist_m':>6}  "
          f"{'highway':<16} {'surface':<16} {'tracktype':<10} {'inf':>4} {'categ'}")
    print("-" * 90)

    for i, (pt, way) in enumerate(zip(sample_pts, assigned)):
        lat, lon = pt
        if way is None:
            print(f"{i+1:<4} {lat:>10.5f} {lon:>10.5f}  {'—':>6}  {'(nessun way)':40}")
            continue
        d = round(_way_min_dist(lat, lon, way), 1)
        tags = way.get("tags", {})
        c = _classify(tags)
        categ = ("ASPH" if c["is_asphalt"] else
                 "GRAV" if c["is_gravel"] else
                 "COBB" if c["is_cobblestone"] else "??")
        if c["is_trail"]:
            categ += "+TRAIL"      # path/track/bridleway/cycleway
        elif c["is_main_road"]:
            categ += "+MAIN"       # primary/trunk/motorway
        elif c["is_secondary"]:
            categ += "+SP"         # secondary = Strada Provinciale
        print(f"{i+1:<4} {lat:>10.5f} {lon:>10.5f}  {d:>6.1f}  "
              f"{tags.get('highway','?'):<16} "
              f"{tags.get('surface','—'):<16} "
              f"{tags.get('tracktype','—'):<10} "
              f"{'Y' if c['inferred'] else 'N':>4}  {categ}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Individua il GPX da usare
    if len(sys.argv) > 1:
        gpx_path = sys.argv[1]
    else:
        cand_file = Path("data/last_test_candidates.json")
        if not cand_file.exists():
            print("[ERRORE] data/last_test_candidates.json non trovato. Esegui prima test_pipeline.py")
            sys.exit(1)
        data = json.loads(cand_file.read_text())
        # Preferisci candidato C (fastbike), fallback al primo disponibile
        winner = next(
            (c for c in data["candidates"] if c["id"] == "C" and c["status"] == "ok"),
            next((c for c in data["candidates"] if c["status"] == "ok"), None),
        )
        if not winner:
            print("[ERRORE] Nessun candidato valido in last_test_candidates.json")
            sys.exit(1)
        gpx_path = winner["gpx_path"]
        print(f"GPX: {winner['id']} — {winner['strategy_name']} [{winner['profile']}]")
        print(f"     {winner['analysis']['distance_km']} km  |  "
              f"{winner['analysis']['elevation_gain_m']} m dislivello")

    if not Path(gpx_path).exists():
        print(f"[ERRORE] File non trovato: {gpx_path}")
        sys.exit(1)

    print()
    print("=" * 65)
    print("STEP 1-2  Campionamento + Query Overpass (batched)")
    print("=" * 65)
    result = enrich_gpx(gpx_path, verbose=True)

    print()
    print("=" * 65)
    print("STEP 3  Dettaglio primi 15 campioni (ispezione)")
    print("=" * 65)
    _debug_sample(gpx_path)

    print()
    print("=" * 65)
    print("RISULTATI FINALI")
    print("=" * 65)
    print(f"  Tempo totale          : {result['elapsed_s']} s")
    print(f"  Campioni totali       : {result['samples_total']}")
    print(f"  Campioni risolti      : {result['samples_resolved']} "
          f"({result['samples_resolved']/result['samples_total']*100:.1f}%)")
    print(f"  Non risolti           : {result['unresolved_percent']} %  "
          f"(sempre visibile — trasparenza per la UI)")
    print(f"  Partial (inaffidabile): {'SI ✗' if result['partial'] else 'NO ✓'}")

    if not result["partial"]:
        print()
        print(f"  asphalt_percent       : {result.get('asphalt_percent', '—')} %")
        print(f"  gravel_percent        : {result.get('gravel_percent', '—')} %")
        print(f"  cobblestone_percent   : {result.get('cobblestone_percent', '—')} %")
        print(f"  trail_percent         : {result.get('trail_percent', '—')} %  "
              f"(path/track/bridleway/cycleway — {result.get('trail_count', 0)} campioni)")
        print(f"  main_road_percent     : {result.get('main_road_percent', '—')} %  "
              f"(solo primary/trunk/motorway)")
        print(f"  secondary_percent     : {result.get('secondary_percent', '—')} %  "
              f"(SP Strade Provinciali — accettabili in bici)")
        print(f"  cobblestone_detected  : {result.get('cobblestone_detected', '—')}")
        print(f"  ss16_detected         : {result.get('ss16_detected', '—')}")
        print(f"  inferred_percent      : {result.get('inferred_percent', '—')} %  "
              f"(senza tag 'surface' OSM → classificato da highway/tracktype)")
        print()
        n_res   = result["samples_resolved"]
        trail_c = result.get("trail_count", 0)
        road_c  = n_res - trail_c
        asph    = result.get("asphalt_percent", 0)
        grav    = result.get("gravel_percent", 0)
        main    = result.get("main_road_percent", 0)
        sec     = result.get("secondary_percent", 0)
        trail_p = result.get("trail_percent", 0)
        user_max_gravel = 20   # da user_memory.yaml
        print("  Analisi tratto fluviale vs approccio stradale:")
        if trail_c > 0:
            print(f"    Campioni su sentiero (path/track):  {trail_c}/{n_res} = {trail_p}%")
            print(f"    Campioni su strada di approccio:    {road_c}/{n_res} = {100-trail_p}%")
        else:
            print(f"    Nessun campione su sentiero (path/track) rilevato.")
            print(f"    Tutti i {n_res} campioni su infrastruttura stradale.")
        print()
        print("  Plausibilità:")
        print(f"    Asfalto      {asph}% → {'OK ✓' if asph >= 60 else 'BASSO — atteso >60% per fastbike'}")
        print(f"    Gravel       {grav}% → {'OK ✓' if grav <= user_max_gravel else f'ALTO ✗ — supera max_gravel={user_max_gravel}% (UserMemory)'}")
        print(f"    Sentiero     {trail_p}% → {'OK ✓ tratto fluviale riconosciuto' if trail_p > 0 else 'nessun sentiero — percorso interamente su strada'}")
        print(f"    Strade ad alta percorrenza (main) {main}% → {'OK ✓' if main < 10 else 'ALTO ✗ — traffico intenso'}")
        print(f"    Strade provinciali (secondary/SP) {sec}% → {'OK ✓' if sec < 60 else 'INFO — percorso prevalentemente su SP'}")
        print(f"    SS16 rilevata: {'ATTENZIONE ✗' if result.get('ss16_detected') else 'no ✓'}")
    else:
        print(f"\n  Motivo: {result.get('reason', '')}")

    # Salva risultato su disco
    out = Path("data/last_osm_enrichment.json")
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n  Risultato salvato in {out}")
