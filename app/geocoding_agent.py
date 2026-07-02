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


def geocode_place(name: str, region: str = "Marche, Italia") -> tuple[float, float] | None:
    """
    Ritorna (lat, lon) o None se il luogo non viene trovato come SETTLEMENT.

    Logica:
    1. Controlla la cache SQLite — ritorna immediatamente senza API call su hit.
    2. Su cache miss: sleep 1s + Nominatim con limit=5.
    3. Filtra i risultati a place_rank ≤ SETTLEMENT_RANK_MAX (≤25).
       Scarta POI, banche, strade: evita che "BCC Corinaldo" batta "Corinaldo" comune.
    4. Se nessun settlement trovato: ritorna None (il chiamante prova una regione più ampia).
    5. Tra i settlement trovati, sceglie quello con place_rank minimo.
    6. Salva in cache solo i risultati salvati.

    Propaga eccezioni di rete/servizio (GeocoderTimedOut, ecc.) al chiamante.
    """
    query = f"{name}, {region}"

    with _get_conn() as conn:
        # ① Cache check — PRIMA della chiamata API
        row = conn.execute(
            "SELECT lat, lon FROM geocoding_cache WHERE query = ?", (query,)
        ).fetchone()
        if row:
            return float(row[0]), float(row[1])  # Cache hit: nessuna API call

        # ② Rate limit — 1 req/sec obbligatorio per Nominatim pubblico
        time.sleep(1.0)

        # ③ Chiamata API — limit=5 per poter filtrare tra più risultati
        results = _GEOLOCATOR.geocode(query, exactly_one=False, limit=5) or []

        # ④ Filtra a soli settlement (place_rank ≤ 25) — scarta POI e strade
        settlements = [r for r in results if int(r.raw.get("place_rank", 99)) <= _SETTLEMENT_RANK_MAX]
        if not settlements:
            return None  # Nessun settlement trovato → il chiamante prova fallback

        # ⑤ Scegli il settlement geograficamente più rilevante (place_rank minimo)
        location = min(settlements, key=lambda r: int(r.raw.get("place_rank", 99)))

        conn.execute(
            "INSERT OR REPLACE INTO geocoding_cache (query, lat, lon, display) VALUES (?, ?, ?, ?)",
            (query, location.latitude, location.longitude, location.address),
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
