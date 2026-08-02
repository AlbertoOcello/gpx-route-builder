"""
Geocoding Agent (Fase 6bis) — converte nomi di luogo in coordinate via Nominatim
con cache SQLite locale (SRS §6.2bis, §10).

Rate limit: 1 req/sec per Nominatim pubblico (sleep PRIMA di ogni chiamata API).
Cache: interrogata PRIMA di chiamare Nominatim; cache hit non chiama l'API.
Errori: geocoding fallito su singolo waypoint non blocca la pipeline.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from geopy.geocoders import Nominatim
from geopy.exc import GeocoderServiceError, GeocoderTimedOut, GeocoderUnavailable

# DB unificato (SRS §10) — geocoding_cache è ora in gpx_route_builder.sqlite
from db import DB_PATH as _DB_PATH  # noqa: E402 (import dopo stdlib)

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
        place_class = [r for r in settlements if r.raw.get("class") == "place"]
        pool = place_class or settlements
        location = min(pool, key=lambda r: int(r.raw.get("place_rank", 99)))

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


def reverse_geocode_address(lat: float, lon: float) -> str | None:
    """Ritorna l'indirizzo Nominatim più vicino a (lat, lon), in italiano."""
    time.sleep(1.0)
    loc = _GEOLOCATOR.reverse((lat, lon), language="it")
    return loc.address if loc else None


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
