"""
Geocoding Agent (Fase 6bis) — converte nomi di luogo in coordinate via Nominatim
con cache SQLite locale (SRS §6.2bis, §10).

Rate limit: 1 req/sec per Nominatim pubblico (sleep PRIMA di ogni chiamata API).
Cache: interrogata PRIMA di chiamare Nominatim; cache hit non chiama l'API.
Errori: geocoding fallito su singolo waypoint non blocca la pipeline.
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
from pathlib import Path

from geopy.geocoders import Nominatim
from geopy.exc import GeocoderServiceError, GeocoderTimedOut, GeocoderUnavailable

# DB unificato (SRS §10) — geocoding_cache è ora in gpx_route_builder.sqlite
from db import DB_PATH as _DB_PATH  # noqa: E402 (import dopo stdlib)

log = logging.getLogger(__name__)

_GEOLOCATOR = Nominatim(user_agent="gpx-route-builder/0.3")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geocoding_cache (
            query    TEXT PRIMARY KEY,
            lat      REAL NOT NULL,
            lon      REAL NOT NULL,
            display  TEXT,
            created  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


_SETTLEMENT_RANK_MAX = 25
# Nominatim place_rank: country≈4, state≈8, city≈16, town≈18, village≈19,
# suburb≈22, neighbourhood≈25, street≈26-28, POI/bank/shop≈30.
# Risultati con place_rank > 25 sono POI o strade, non delle località.
# geocode_place ritorna None se trovati solo POI → triggera il fallback regionale.

# Bug confermato su "Agugliano": Nominatim restituisce SIA il confine
# amministrativo del comune (class="boundary", place_rank=16 — il centroide
# del poligono, che può cadere ovunque nel territorio comunale, anche su una
# strada sterrata senza sbocco lontana dal paese) SIA il nodo del vero
# insediamento (class="place", type="village", place_rank=19). Scegliendo il
# place_rank minimo si preferiva SEMPRE il confine amministrativo (16 < 19),
# nonostante non sia un buon riferimento geografico per il ciclismo — il paese
# vero era a 2.57 km di distanza. I risultati class="place" (village/town/
# hamlet/city/...) vanno quindi preferiti quando presenti, indipendentemente
# dal place_rank numerico.

def _class_preference_rank(class_value: str | None, place_rank: int | None, preferred_class: str) -> tuple[int, int]:
    """
    Chiave di ordinamento condivisa e generica: preferisce class==preferred_class
    su qualunque altra classe; a parità di preferenza, place_rank crescente
    (più basso = più rilevante per Nominatim). Valori più bassi = più preferito.

    Origine (fix "Agugliano", preferred_class="place"): il centroide di un
    confine comunale (class="boundary") può cadere ovunque nel territorio,
    anche lontano dal paese vero, mentre il nodo class="place" è il punto
    reale dell'insediamento. Generalizzata (fix "Via Alberici",
    preferred_class="highway") per il caso opposto: quando l'utente ha
    scritto esplicitamente un prefisso stradale, va preferita la strada, non
    un luogo/frazione omonimo.

    Usata sia da geocode_place (min() su questa chiave, preferred_class="place"
    sempre — quella funzione filtra già a soli settlement, le strade non
    competono) sia da sort_candidates_by_place_preference (per riordinare una
    lista di candidati senza scartare nulla, preferred_class scelto dal
    chiamante in base al contesto).
    """
    return (0 if class_value == preferred_class else 1, place_rank if place_rank is not None else 99)


def sort_candidates_by_place_preference(candidates: list[dict], preferred_class: str = "place") -> list[dict]:
    """
    Riordina (non filtra, non sceglie) una lista di candidati — dict con
    "class_"/"place_rank", stessa forma di geocode_search_raw/
    geocode_in_viewbox/fuzzy_geocode_in_area — applicando _class_preference_rank.

    preferred_class="place" (default): quando due candidati rappresentano lo
    stesso luogo (uno come confine amministrativo, uno come nodo place — es.
    "Monte San Vito"), il nodo place viene messo prima (fix "Agugliano").

    preferred_class="highway": il caso opposto (fix "Via Alberici") — quando
    la query originale aveva un prefisso stradale esplicito ("Via Alberici"),
    l'utente ha chiesto una via, non l'insediamento omonimo "Alberici": la
    strada va messa prima. Il chiamante (fuzzy_geocode_in_area) decide quale
    preferenza applicare in base al testo originale, non a questa funzione.

    In entrambi i casi nessun candidato viene scartato, solo riordinato.
    """
    return sorted(
        candidates,
        key=lambda c: _class_preference_rank(c.get("class_"), c.get("place_rank"), preferred_class),
    )


# Bias di prossimità (soft, non hard-restrict): quando è nota la zona della
# route (bias_coords), Nominatim viene invitato a preferire risultati dentro
# questo riquadro attorno al punto, senza escludere risultati fuori — evita
# di prendere un omonimo in un'altra regione quando esiste un match migliore
# vicino al percorso. bounded=False è ciò che rende il bias "soft".
_BIAS_BOX_DEG = 0.5


def geocode_place(
    name: str,
    region: str | None = "Marche, Italia",
    country_codes: str = "it",
    language: str = "it",
    bias_coords: tuple[float, float] | None = None,
) -> tuple[float, float] | None:
    """
    Ritorna (lat, lon) o None se il luogo non viene trovato come SETTLEMENT.

    region=None  → ricerca Italia-wide (q=name, countrycodes=it): usare per il punto
                   di partenza dove la regione è ignota.
    region=str   → appende la regione alla query per disambiguare waypoint locali
                   (comportamento storico per la pipeline Planner).
    bias_coords  → (lat, lon) opzionale attorno a cui applicare un bias di
                   prossimità soft (viewbox non bounded) — tipicamente il punto
                   di partenza della route, per preferire l'omonimo più vicino
                   senza escluderne altri.

    Logica:
    1. Controlla la cache SQLite — ritorna immediatamente senza API call su hit.
    2. Su cache miss: sleep 1s + Nominatim con countrycodes, language e bias.
    3. Filtra i risultati a place_rank ≤ SETTLEMENT_RANK_MAX (≤25).
    4. Se nessun settlement trovato: ritorna None.
    5. Tra i settlement trovati, preferisce i risultati class="place" (nodo di
       insediamento reale) sugli altri (es. class="boundary", confine
       amministrativo); tra quelli sceglie il place_rank minimo.
    6. Salva in cache (chiave include il bias, se presente, per non riusare
       un risultato scelto per una zona diversa).
    """
    query = name if region is None else f"{name}, {region}"
    cache_key = query
    if bias_coords is not None:
        cache_key = f"{query}|bias={round(bias_coords[0], 2)},{round(bias_coords[1], 2)}"

    with _get_conn() as conn:
        # ① Cache check
        row = conn.execute(
            "SELECT lat, lon FROM geocoding_cache WHERE query = ?", (cache_key,)
        ).fetchone()
        if row:
            return float(row[0]), float(row[1])

        # ② Rate limit
        time.sleep(1.0)

        # ③ Chiamata API — q=query, countrycodes e language per risultati corretti
        kwargs: dict = dict(
            exactly_one=False,
            limit=5,
            country_codes=country_codes or None,
            language=language or None,
        )
        if bias_coords is not None:
            blat, blon = bias_coords
            kwargs["viewbox"] = [
                (blat - _BIAS_BOX_DEG, blon - _BIAS_BOX_DEG),
                (blat + _BIAS_BOX_DEG, blon + _BIAS_BOX_DEG),
            ]
            kwargs["bounded"] = False  # soft: privilegia il riquadro, non esclude il resto

        results = _GEOLOCATOR.geocode(query, **kwargs) or []

        # ④ Filtra a soli settlement (place_rank ≤ 25)
        settlements = [r for r in results if int(r.raw.get("place_rank", 99)) <= _SETTLEMENT_RANK_MAX]
        if not settlements:
            return None

        # ⑤ Preferisci i nodi di insediamento reale (class="place") su confini
        # amministrativi o altro; a parità, place_rank minimo (più rilevante).
        # Chiave condivisa con sort_candidates_by_place_preference (fix Agugliano).
        location = min(
            settlements,
            key=lambda r: _class_preference_rank(
                r.raw.get("class"), int(r.raw.get("place_rank", 99)), preferred_class="place",
            ),
        )

        conn.execute(
            "INSERT OR REPLACE INTO geocoding_cache (query, lat, lon, display) VALUES (?, ?, ?, ?)",
            (cache_key, location.latitude, location.longitude, location.address),
        )
        conn.commit()
        return float(location.latitude), float(location.longitude)


def geocode_search_raw(
    query: str,
    limit: int = 10,
    country_codes: str | list[str] | None = None,
) -> list[dict]:
    """
    Cerca tutti i risultati Nominatim per `query`, senza filtro di place_rank.
    Usata dalla tab Geolocalizza: l'utente vuole vedere TUTTI i match possibili
    (inclusi POI e omonimi) per scegliere manualmente il punto giusto.
    Non usa la cache — ogni ricerca è fresca.
    """
    time.sleep(1.0)
    kwargs: dict = {"exactly_one": False, "limit": limit}
    if country_codes:
        kwargs["country_codes"] = country_codes
    results = _GEOLOCATOR.geocode(query, **kwargs) or []
    return [
        {
            "display_name": r.address,
            "lat": float(r.latitude),
            "lon": float(r.longitude),
            "place_rank": int(r.raw.get("place_rank", 99)),
            "class_": r.raw.get("class", ""),
            "type_": r.raw.get("type", ""),
        }
        for r in results
    ]


def geocode_in_viewbox(
    query: str,
    viewbox: tuple[float, float, float, float],
    limit: int = 5,
    country_codes: str | list[str] | None = "it",
    language: str = "it",
) -> list[dict]:
    """
    Come geocode_search_raw (nessun filtro place_rank — include strade e POI,
    non solo insediamenti), ma con filtro geografico REALE: bounded=True
    esclude i risultati fuori da `viewbox`, non si limita a de-priorizzarli
    come il bias soft di geocode_place (bounded=False, un solo punto + margine
    fisso, pensato per un punto di partenza singolo). Qui il chiamante passa
    un riquadro esplicito (es. il bounding box di un GPX con margine) —
    caso d'uso introdotto per il Route Editor Agent (Stadio 1): un qualificatore
    geografico inferito e sbagliato (es. il comune sbagliato aggiunto a un
    nome di via) fa fallire una query Nominatim per intero invece di essere
    ignorato — filtrare per area invece che per nome di comune evita il problema.

    viewbox = (south, west, north, east). Non usa la cache — un riquadro
    diverso per ogni chiamata la renderebbe poco efficace.

    Risultati nell'ordine restituito da Nominatim, non riordinati qui: la
    preferenza class=place-vs-highway dipende dal testo ORIGINALE prima della
    generazione varianti (c'era un prefisso stradale esplicito o no?), un
    contesto che questa funzione non ha — vedi fuzzy_geocode_in_area, che
    applica sort_candidates_by_place_preference dopo aver raccolto i
    candidati di tutte le varianti.
    """
    time.sleep(1.0)
    south, west, north, east = viewbox
    kwargs: dict = dict(
        exactly_one=False,
        limit=limit,
        country_codes=country_codes or None,
        language=language or None,
        viewbox=[(south, west), (north, east)],
        bounded=True,
    )
    results = _GEOLOCATOR.geocode(query, **kwargs) or []
    return [
        {
            "display_name": r.address,
            "lat": float(r.latitude),
            "lon": float(r.longitude),
            "place_rank": int(r.raw.get("place_rank", 99)),
            "class_": r.raw.get("class", ""),
            "type_": r.raw.get("type", ""),
        }
        for r in results
    ]


def bbox_from_center(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """
    (south, west, north, east) — bbox equivalente a un cerchio di raggio
    radius_km centrato su (lat, lon), per usare geocode_in_viewbox/
    fuzzy_geocode_in_area quando è noto solo un punto (es. la partenza di una
    route, nessun GPX ancora disponibile) invece di un intero tracciato.
    Stessa conversione km→gradi di _gpx_viewbox in route_editor_agent.py: 1°
    di latitudine ≈ 111 km ovunque, 1° di longitudine si restringe con
    cos(latitudine).
    """
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


# ── Geocodifica fuzzy vincolata geograficamente ───────────────────────────────
# Diagnosi (Route Editor Agent, Stadio 1): un input testuale impreciso ("il
# grottino" quando OSM tagga solo "Grottino") o storpiato produce zero
# risultati con una query esatta, anche dentro un viewbox corretto — il
# viewbox risolve il problema del comune sbagliato, non quello della forma
# testuale sbagliata. Qui il vincolo geografico resta SEMPRE obbligatorio
# (mai una ricerca globale poi filtrata: con un nome comune generico, una
# ricerca Italia-wide su ciascuna variante restituirebbe rumore ingestibile)
# e si aggiungono piccole trasformazioni meccaniche sul testo, non un
# dizionario linguistico completo (rimandato: vedi route_editor_agent.py).

_LEADING_ARTICLES = {"il", "lo", "la", "i", "gli", "le"}
_ROAD_PREFIXES = {"via", "strada", "corso", "viale", "piazza", "vicolo", "contrada", "c.da", "sp", "ss", "sr"}
_VOWEL_ENDINGS = ("a", "e", "o", "i")

# Le abbreviazioni SP/SS/SR (provinciale/statale/regionale) sono spesso
# scritte fuse col numero, senza spazio (es. "SP13", "SS16", "SR360") —
# via/strada/corso ecc. non lo sono mai (nessuno scrive "via123"), quindi il
# riconoscimento fuso si applica solo a queste tre abbreviazioni.
_FUSED_ROAD_PREFIX_RE = re.compile(r"^(sp|ss|sr)(\d.*)$", re.IGNORECASE)


def _strip_leading_article(text: str) -> tuple[str | None, str]:
    """(articolo rimosso, testo senza articolo) — (None, text) se la prima
    parola non è un articolo italiano riconosciuto. Gestisce anche la forma
    elisa "l'"/"l’" attaccata alla parola successiva (es. "l'ospedale")."""
    words = text.split()
    if not words:
        return None, text
    m = re.match(r"^l['’]", words[0], flags=re.IGNORECASE)
    if m:
        rest_of_word = words[0][m.end():]
        rest = " ".join(([rest_of_word] if rest_of_word else []) + words[1:])
        return words[0][:m.end()], rest
    if words[0].lower().rstrip(".,") in _LEADING_ARTICLES:
        return words[0], " ".join(words[1:])
    return None, text


def _strip_road_prefix(text: str) -> tuple[str | None, str]:
    """
    (prefisso rimosso, testo senza prefisso) — (None, text) se la prima
    parola non è un prefisso stradale comune (via/strada/corso/...), incluso
    il caso in cui l'abbreviazione (SP/SS/SR) sia fusa col numero senza
    spazio nello stesso token (es. "SP13" → prefisso "SP", resto "13").
    """
    words = text.split()
    if not words:
        return None, text
    first = words[0]
    if first.lower().rstrip(".,") in _ROAD_PREFIXES:
        return first, " ".join(words[1:])
    m = _FUSED_ROAD_PREFIX_RE.match(first)
    if m:
        return m.group(1), " ".join([m.group(2)] + words[1:])
    return None, text


def query_has_road_prefix(text: str) -> bool:
    """
    True se `text` inizia con un prefisso stradale esplicito (via/strada/
    corso/viale/vicolo/contrada-C.da/SP/SS/SR, case-insensitive, punto finale
    dell'abbreviazione tollerato) — stessa rilevazione usata per generare le
    varianti (_strip_road_prefix), qui esposta per decidere se l'utente ha
    chiesto esplicitamente una via (fix "Via Alberici": in quel caso non va
    preferito un luogo/frazione omonimo su una strada)."""
    prefix, _ = _strip_road_prefix(text)
    return prefix is not None


def _ending_variants(text: str) -> list[str]:
    """
    Le 4 varianti di desinenza (a/e/o/i) sull'ultima parola, se questa termina
    in una vocale variabile — es. "brecciate" → brecciata/brecciate/
    brecciato/brecciati. Regola puramente meccanica sull'ultima lettera
    (nessun dizionario linguistico, nessuna morfologia reale): alcune
    combinazioni prodotte sono linguisticamente insensate per costruzione —
    è il filtro geografico più l'esistenza reale in OSM a scartarle, non
    questa funzione. Ritorna [text] invariato se l'ultima parola non termina
    in a/e/o/i o è troppo corta per avere senso come desinenza.
    """
    words = text.split()
    if not words or len(words[-1]) < 2:
        return [text]
    last = words[-1]
    if last[-1].lower() not in _VOWEL_ENDINGS:
        return [text]
    stem = last[:-1]
    return [
        " ".join(words[:-1] + [stem + (ending.upper() if last[-1].isupper() else ending)])
        for ending in _VOWEL_ENDINGS
    ]


def generate_fuzzy_variants(text: str) -> list[str]:
    """
    Genera un piccolo insieme di varianti testuali plausibili da un input
    grezzo, con trasformazioni meccaniche mirate (nessun NLP/dizionario):
      - rimozione dell'articolo iniziale (il/lo/la/i/gli/le/l'), se presente
      - con/senza prefisso stradale (via/strada/corso/...), se presente
      - le 4 varianti di desinenza (a/e/o/i) sull'ultima parola
    Le trasformazioni si combinano (articolo × prefisso × desinenza), ma
    l'insieme resta piccolo: ciascun asse ha al più 2-4 stati e raramente
    tutti e tre si applicano allo stesso input. Deduplica case-insensitive,
    ordine di prima apparizione.

    Eccezione: quando il prefisso rimosso è un'abbreviazione stradale
    numerata FUSA (SP/SS/SR + cifre senza spazio, es. "SP13" — vedi
    _FUSED_ROAD_PREFIX_RE), il "resto nudo" non viene aggiunto come base
    separata — per "Via Alberici" il resto ("Alberici") è un nome proprio,
    una ricerca sensata; per "SP13" il resto ("13") è un puro numero di
    percorrenza, che come query isolata recupera qualunque cosa abbia "13"
    come civico/codice nella zona (fermate bus, bagni pubblici, parcheggi...)
    — rumore non filtrabile geograficamente. La forma completa ("SP13")
    resta comunque tentata.
    """
    text = text.strip()
    if not text:
        return []

    _, after_article = _strip_leading_article(text)
    article_bases = [text] if after_article == text else [text, after_article]

    all_bases: list[str] = []
    for base in article_bases:
        _, after_prefix = _strip_road_prefix(base)
        if after_prefix == base:
            all_bases.append(base)
            continue
        all_bases.append(base)
        first_word = base.split()[0] if base.split() else ""
        if not _FUSED_ROAD_PREFIX_RE.match(first_word):
            all_bases.append(after_prefix)

    variants: dict[str, str] = {}
    for base in all_bases:
        for v in _ending_variants(base):
            key = v.strip().lower()
            if key and key not in variants:
                variants[key] = v.strip()
    return list(variants.values())


def fuzzy_geocode_in_area(
    text: str,
    gpx_bbox: tuple[float, float, float, float] | None = None,
    center_point: tuple[float, float] | None = None,
    radius_km: float = 50.0,
    limit_per_variant: int = 5,
    country_codes: str | list[str] | None = "it",
    language: str = "it",
) -> dict:
    """
    Geocodifica robusta a input testuali imprecisi, SEMPRE vincolata
    geograficamente — mai una ricerca globale poi filtrata.

    Vincolo geografico (obbligatorio, uno dei due):
      gpx_bbox      : bounding box già calcolato (es. il bbox di un GPX + margine,
                       stesso concetto di route_editor_agent._gpx_viewbox).
      center_point  : (lat, lon) di un punto noto (es. la partenza di una route
                       senza GPX ancora disponibile) — convertito in un bbox
                       equivalente con radius_km (default 50, tarabile) via
                       bbox_from_center.

    Genera le varianti testuali (generate_fuzzy_variants), interroga Nominatim
    per ciascuna dentro il vincolo geografico (geocode_in_viewbox — stessa
    funzione, nessuna query duplicata), raccoglie TUTTI i risultati di TUTTE
    le varianti e deduplica per coordinata (arrotondata a 5 decimali, ~1m).
    Nessuna scelta del "migliore" qui: solo l'insieme completo dei candidati
    plausibili trovati — la disambiguazione è un passo successivo.

    Ritorna {"text", "viewbox", "variants_tried", "variants_with_hits", "candidates"}
    — ogni candidato include anche "matched_variant" (quale variante lo ha
    trovato per prima, se più varianti convergono sulla stessa coordinata).
    """
    if gpx_bbox is None and center_point is None:
        raise ValueError("Serve un vincolo geografico: gpx_bbox oppure center_point (+ radius_km).")
    viewbox = gpx_bbox if gpx_bbox is not None else bbox_from_center(center_point[0], center_point[1], radius_km)

    variants = generate_fuzzy_variants(text)

    seen: dict[tuple[float, float], dict] = {}
    variants_with_hits: list[str] = []
    for variant in variants:
        query = f"{variant}, Italia"
        try:
            results = geocode_in_viewbox(
                query, viewbox, limit=limit_per_variant,
                country_codes=country_codes, language=language,
            )
        except Exception as exc:
            log.warning("fuzzy_geocode_in_area: variante %r fallita: %s", variant, exc)
            continue
        if results:
            variants_with_hits.append(variant)
        for r in results:
            key = (round(r["lat"], 5), round(r["lon"], 5))
            if key not in seen:
                seen[key] = {**r, "matched_variant": variant}

    # Riordinamento finale, in base al testo ORIGINALE (prima delle varianti):
    #   - nessun prefisso stradale esplicito (es. "Monte San Vito"): preferisci
    #     class=place su confine amministrativo/altro (fix "Agugliano") — un
    #     nome di località, non di via, è stato chiesto.
    #   - prefisso stradale esplicito (es. "Via Alberici"): preferisci
    #     class=highway (fix "Via Alberici") — l'utente ha chiesto una via,
    #     non va anteposta una frazione/luogo omonimo.
    # Il merge tra varianti può aver interleaved le due rappresentazioni in un
    # ordine diverso da quello di una singola query — riapplicare la
    # preferenza qui garantisce lo stesso risultato indipendentemente da
    # quale variante abbia trovato quale rappresentazione per prima.
    preferred_class = "highway" if query_has_road_prefix(text) else "place"
    return {
        "text": text,
        "viewbox": viewbox,
        "variants_tried": variants,
        "variants_with_hits": variants_with_hits,
        "candidates": sort_candidates_by_place_preference(list(seen.values()), preferred_class=preferred_class),
    }


def reverse_geocode_address(lat: float, lon: float) -> str | None:
    """Ritorna l'indirizzo Nominatim più vicino a (lat, lon), in italiano."""
    time.sleep(1.0)
    loc = _GEOLOCATOR.reverse((lat, lon), language="it")
    return loc.address if loc else None


_CLIMB_ZONE_UNAVAILABLE = "Zona non disponibile"


def geocode_climbs(climbs: list[dict], context: str = "") -> list[dict]:
    """
    Popola climb["zone"] per ciascuna salita rilevata (gpx_analyzer.detect_climbs)
    via reverse_geocode_address() sulle coordinate del punto più duro
    (hard_lat/hard_lon) — EAGER, non lazy: va chiamata subito dopo il
    rilevamento (candidate_generator, ride_analysis_agent, Opzione D), non al
    momento in cui l'utente apre una vista grafica. Decisione esplicita
    dell'utente: il costo (1s di attesa per chiamata, cortesia Nominatim, non
    cachato qui a differenza di geocode_place — reverse_geocode_address non
    usa la cache SQLite) è accettato consapevolmente ora, in vista di un
    futuro redesign del Builder che lo renderà marginale.

    Il fallimento su una singola salita (rete irraggiungibile, nessun
    risultato) imposta zone="Zona non disponibile" e NON blocca le altre né
    il chiamante. context è solo per i log (es. nome route + slot candidato),
    facoltativo.

    Muta e ritorna la stessa lista di dict passata in input.
    """
    if not climbs:
        return climbs

    label = f" [{context}]" if context else ""
    t0 = time.perf_counter()
    failures = 0
    for climb in climbs:
        lat, lon = climb.get("hard_lat"), climb.get("hard_lon")
        if lat is None or lon is None:
            climb["zone"] = _CLIMB_ZONE_UNAVAILABLE
            failures += 1
            continue
        try:
            addr = reverse_geocode_address(lat, lon)
        except Exception as exc:
            log.warning("geocode_climbs%s: reverse geocoding fallito per (%.5f,%.5f): %s", label, lat, lon, exc)
            addr = None
        climb["zone"] = addr if addr else _CLIMB_ZONE_UNAVAILABLE
        if not addr:
            failures += 1

    elapsed = time.perf_counter() - t0
    log.info(
        "geocode_climbs%s: %d salite geocodificate in %.1fs (%d falliti/non disponibili)",
        label, len(climbs), elapsed, failures,
    )
    return climbs


def geocode_candidate(candidate: dict) -> dict:
    """
    Popola lat/lon per i waypoint con needs_geocoding=True.

    Garanzia pipeline: il fallimento su un singolo waypoint NON blocca gli altri.
    Tipi di errore distinguibili nella UI:
      - geocoding_error = "Non trovato: ..."  → luogo sconosciuto a Nominatim
      - geocoding_error = "Timeout: ..."       → Nominatim irraggiungibile/lento
      - geocoding_error = "Errore servizio: ..." → risposta HTTP non valida
    """
    updated = []
    for wp in candidate.get("waypoints", []):
        if wp.get("needs_geocoding") and wp.get("name"):
            try:
                result = geocode_place(wp["name"])
                if result:
                    wp = {**wp, "lat": result[0], "lon": result[1], "needs_geocoding": False}
                else:
                    wp = {**wp, "geocoding_error": f"Non trovato: {wp['name']}"}
            except GeocoderTimedOut:
                wp = {**wp, "geocoding_error": f"Timeout: {wp['name']}"}
            except (GeocoderUnavailable, GeocoderServiceError) as e:
                wp = {**wp, "geocoding_error": f"Errore servizio: {e}"}
            except Exception as e:
                wp = {**wp, "geocoding_error": f"Errore imprevisto: {type(e).__name__}: {e}"}
        updated.append(wp)

    still_pending = any(w.get("needs_geocoding") or w.get("geocoding_error") for w in updated)
    return {**candidate, "waypoints": updated, "requires_geocoding": still_pending}
