"""
Database SQLite unificato — SRS §10.
File: data/gpx_route_builder.sqlite

Contiene tutte le tabelle operative più geocoding_cache (migrata da geocoding_cache.db).
Le funzioni pubbliche sono thread-safe per l'uso in Streamlit (connessioni short-lived).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent.parent / "data" / "gpx_route_builder.sqlite"
_OLD_CACHE_PATH = Path(__file__).parent.parent / "data" / "geocoding_cache.db"

# ── Percorso pubblico (usato da geocoding_agent.py) ──────────────────────────
DB_PATH = _DB_PATH


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    """
    Crea tutte le tabelle se non esistono e migra geocoding_cache dal vecchio DB.
    Sicuro da chiamare a ogni avvio (idempotente).
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    ddl = """
    -- route_gen_id era un FK a routes_generated, tabella morta rimossa dal
    -- redesign persistenza (mai riletta da nessuna query, vedi routes_generated/
    -- routes_approved/user_feedback sotto) — colonna lasciata orfana (nessun
    -- vincolo REFERENCES) perché routes_rejected stessa non è mai scritta
    -- (save_route_rejected() non è chiamata da nessuna parte, come
    -- save_route_approved() già rimossa): non tocchiamo la tabella in sé,
    -- non richiesto in questo redesign, ma il riferimento pendente andava
    -- comunque sistemato.
    CREATE TABLE IF NOT EXISTS routes_rejected (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT    DEFAULT (datetime('now')),
        route_gen_id    INTEGER,
        candidate_id    TEXT    NOT NULL,
        strategy_name   TEXT,
        profile         TEXT,
        distance_km     REAL,
        elevation_gain_m REAL,
        reason          TEXT
    );

    -- POI locali
    CREATE TABLE IF NOT EXISTS known_places (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        lat         REAL,
        lon         REAL,
        place_type  TEXT,           -- borgo, città, landmark, ...
        notes       TEXT
    );

    CREATE TABLE IF NOT EXISTS known_avoid_roads (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL UNIQUE,
        reason      TEXT,
        added_at    TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS known_water_points (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT,
        lat         REAL,
        lon         REAL,
        notes       TEXT
    );

    CREATE TABLE IF NOT EXISTS known_bars (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT,
        lat         REAL,
        lon         REAL,
        notes       TEXT
    );

    CREATE TABLE IF NOT EXISTS known_scenic_points (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT,
        lat         REAL,
        lon         REAL,
        notes       TEXT
    );

    -- Geocoding cache (unificata da geocoding_cache.db)
    CREATE TABLE IF NOT EXISTS geocoding_cache (
        query       TEXT    PRIMARY KEY,
        lat         REAL    NOT NULL,
        lon         REAL    NOT NULL,
        display     TEXT,
        created     TEXT    DEFAULT (datetime('now'))
    );

    -- Segnaposto mappa su "Confronta uscita"
    CREATE TABLE IF NOT EXISTS route_map_annotations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at  TEXT    DEFAULT (datetime('now')),
        route_name  TEXT    NOT NULL,
        lat         REAL    NOT NULL,
        lon         REAL    NOT NULL,
        comment     TEXT    NOT NULL,
        category    TEXT    DEFAULT 'generico'  -- 'problema', 'bello', 'attenzione', 'generico'
    );
    CREATE INDEX IF NOT EXISTS idx_rma_route ON route_map_annotations(route_name);

    -- Ostacoli noti da evitare (alimentati dai segnaposto 'problema')
    CREATE TABLE IF NOT EXISTS known_obstacles (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT    DEFAULT (datetime('now')),
        lat             REAL    NOT NULL,
        lon             REAL    NOT NULL,
        description     TEXT    NOT NULL,
        route_name      TEXT,           -- origine (route_name che ha generato l'ostacolo)
        annotation_id   INTEGER,        -- FK a route_map_annotations.id (soft)
        active          INTEGER DEFAULT 1   -- 0 = risolto/disattivato
    );
    CREATE INDEX IF NOT EXISTS idx_ko_active ON known_obstacles(active);

    -- Profili ciclista/bici per Analisi Giro
    CREATE TABLE IF NOT EXISTS ride_profiles (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at          TEXT    DEFAULT (datetime('now')),
        name                TEXT    NOT NULL UNIQUE,
        bike_model          TEXT,
        bike_type           TEXT,
        wh                  REAL,
        assistance_level    INTEGER,
        battery_pct         REAL,
        bike_weight_kg      REAL,
        driver_weight_kg    REAL,
        driver_age          INTEGER,
        driver_sex          TEXT,
        driver_fitness      INTEGER,
        driver_fcmax        INTEGER,
        driver_health_notes TEXT
    );

    """

    with get_conn() as conn:
        conn.executescript(ddl)

        # Redesign persistenza: routes_generated/routes_approved/user_feedback erano
        # un'isola morta — mai rilette da nessuna query, routes_approved mai scritta.
        # Il feedback vive ora in feedback_history dentro il JSON della route; i
        # segnaposto "problema" vengono promossi direttamente a known_obstacles.
        # DROP IF EXISTS è idempotente: no-op ai riavvii successivi al primo.
        conn.execute("DROP TABLE IF EXISTS routes_approved")
        conn.execute("DROP TABLE IF EXISTS user_feedback")
        conn.execute("DROP TABLE IF EXISTS routes_generated")

        # ALTER TABLE idempotente: colonna zona su known_obstacles (redesign
        # persistenza — filtra gli ostacoli per zona di partenza invece di
        # applicarli indistintamente a qualunque route futura). Righe esistenti:
        # zona resta NULL, nessun dato perso.
        ko_cols = {r[1] for r in conn.execute("PRAGMA table_info(known_obstacles)").fetchall()}
        if "zona" not in ko_cols:
            conn.execute("ALTER TABLE known_obstacles ADD COLUMN zona TEXT")

        # ALTER TABLE idempotente: nuove colonne ride_profiles
        rp_cols = {r[1] for r in conn.execute("PRAGMA table_info(ride_profiles)").fetchall()}
        if "riding_style" not in rp_cols:
            conn.execute("ALTER TABLE ride_profiles ADD COLUMN riding_style TEXT")
        if "min_battery_pct" not in rp_cols:
            conn.execute("ALTER TABLE ride_profiles ADD COLUMN min_battery_pct REAL")
        if "avg_assist_ratio" not in rp_cols:
            conn.execute("ALTER TABLE ride_profiles ADD COLUMN avg_assist_ratio REAL")
        if "assist_ratio_sample_count" not in rp_cols:
            conn.execute("ALTER TABLE ride_profiles ADD COLUMN assist_ratio_sample_count INTEGER DEFAULT 0")

    _migrate_geocoding_cache()
    _seed_known_avoid_roads()


def _migrate_geocoding_cache() -> None:
    """Copia le righe dal vecchio geocoding_cache.db nel DB unificato (idempotente)."""
    if not _OLD_CACHE_PATH.exists():
        return
    try:
        old = sqlite3.connect(_OLD_CACHE_PATH)
        rows = old.execute(
            "SELECT query, lat, lon, display, created FROM geocoding_cache"
        ).fetchall()
        old.close()
    except Exception:
        return

    if not rows:
        return

    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO geocoding_cache (query, lat, lon, display, created) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def _seed_known_avoid_roads() -> None:
    """Popola known_avoid_roads con le strade da evitare sempre (da UserMemory iniziale)."""
    roads = [("SS16", "statale ad alto traffico, vietata per piacevolezza del percorso")]
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO known_avoid_roads (name, reason) VALUES (?, ?)",
            roads,
        )


# ── CRUD pipeline ─────────────────────────────────────────────────────────────
# save_pipeline_run/save_feedback/save_route_approved rimosse (redesign
# persistenza): scrivevano su routes_generated/user_feedback/routes_approved,
# tabelle mai rilette da nessuna query — isola morta, ora eliminata (vedi
# init_db). Il feedback vive in feedback_history nel JSON della route; i
# segnaposto "problema" vengono promossi a known_obstacles da main.py.


def save_route_rejected(route_gen_id: int, candidate: dict, reason: str = "") -> int:
    a = candidate["analysis"]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO routes_rejected "
            "(route_gen_id, candidate_id, strategy_name, profile, "
            " distance_km, elevation_gain_m, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                route_gen_id,
                candidate["id"],
                candidate.get("strategy_name"),
                candidate.get("profile"),
                a.get("distance_km"),
                a.get("elevation_gain_m"),
                reason,
            ),
        )
        return cur.lastrowid


# ── Known obstacles (da segnaposto 'problema') ────────────────────────────────

def save_known_obstacle(
    lat: float,
    lon: float,
    description: str,
    route_name: str | None = None,
    annotation_id: int | None = None,
    zona: str | None = None,
) -> int:
    """Inserisce un ostacolo noto. Idempotente: se esiste già (stessa lat/lon arrotondate
    a 5 decimali, ~1 m), aggiorna la descrizione e riattiva invece di duplicare.

    zona: nome del comune/città di partenza della route che ha segnalato
    l'ostacolo (request.start.name) — usato da get_active_obstacles() per
    limitare gli ostacoli considerati alle sole route future con la stessa
    zona di partenza, invece di applicarli indistintamente a qualunque route.
    """
    lat_r = round(lat, 5)
    lon_r = round(lon, 5)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM known_obstacles WHERE round(lat,5)=? AND round(lon,5)=?",
            (lat_r, lon_r),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE known_obstacles SET description=?, route_name=?, active=1, "
                "annotation_id=COALESCE(?,annotation_id), zona=COALESCE(?,zona) WHERE id=?",
                (description, route_name, annotation_id, zona, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO known_obstacles (lat, lon, description, route_name, annotation_id, zona) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (lat, lon, description, route_name, annotation_id, zona),
        )
        return cur.lastrowid


def get_active_obstacles(zona: str | None = None) -> list[dict]:
    """
    Ritorna gli ostacoli attivi (active=1).

    zona=None (default): tutti gli ostacoli, indipendentemente dalla zona —
    usato dalle viste di gestione/debug dove serve vedere tutto.
    zona="Senigallia" ecc.: solo gli ostacoli con quella zona esatta, PIÙ
    quelli storici senza zona (zona IS NULL, salvati prima di questo redesign)
    — così i dati già presenti restano visibili invece di sparire silenziosamente.
    """
    with get_conn() as conn:
        if zona:
            rows = conn.execute(
                "SELECT id, lat, lon, description, route_name, zona, created_at "
                "FROM known_obstacles WHERE active=1 AND (zona=? OR zona IS NULL) "
                "ORDER BY created_at DESC",
                (zona,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, lat, lon, description, route_name, zona, created_at "
                "FROM known_obstacles WHERE active=1 ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def deactivate_obstacle(obstacle_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE known_obstacles SET active=0 WHERE id=?", (obstacle_id,))


# ── Map annotations (Confronta uscita) ───────────────────────────────────────

def save_map_annotation(
    route_name: str,
    lat: float,
    lon: float,
    comment: str,
    category: str = "generico",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO route_map_annotations (route_name, lat, lon, comment, category) "
            "VALUES (?, ?, ?, ?, ?)",
            (route_name, lat, lon, comment, category),
        )
        return cur.lastrowid


def get_map_annotations(route_name: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, lat, lon, comment, category, created_at "
            "FROM route_map_annotations WHERE route_name = ? ORDER BY created_at",
            (route_name,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_map_annotation(annotation_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM route_map_annotations WHERE id = ?", (annotation_id,))


# ── Utility ───────────────────────────────────────────────────────────────────

def db_stats() -> dict:
    """Ritorna il conteggio righe per tabella — utile per test e debug."""
    tables = [
        "routes_rejected", "geocoding_cache",
        "known_places", "known_avoid_roads", "known_obstacles",
        "known_water_points", "known_bars", "known_scenic_points",
        "route_map_annotations", "ride_profiles",
    ]
    with get_conn() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


# ── Ride profiles (Analisi Giro) ─────────────────────────────────────────────

def save_ride_profile(
    name: str,
    bike_model: str | None = None,
    bike_type: str | None = None,
    wh: float | None = None,
    battery_pct: float | None = None,
    min_battery_pct: float | None = None,
    bike_weight_kg: float | None = None,
    riding_style: str | None = None,
    driver_weight_kg: float | None = None,
    driver_age: int | None = None,
    driver_sex: str | None = None,
    driver_fitness: int | None = None,
    driver_fcmax: int | None = None,
    driver_health_notes: str | None = None,
) -> int:
    """Upsert ride profile by name. Returns the profile id."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM ride_profiles WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE ride_profiles SET
                   bike_model=?, bike_type=?, wh=?,
                   battery_pct=?, min_battery_pct=?, bike_weight_kg=?,
                   riding_style=?, driver_weight_kg=?,
                   driver_age=?, driver_sex=?, driver_fitness=?,
                   driver_fcmax=?, driver_health_notes=?
                   WHERE name=?""",
                (
                    bike_model, bike_type, wh,
                    battery_pct, min_battery_pct, bike_weight_kg,
                    riding_style, driver_weight_kg,
                    driver_age, driver_sex, driver_fitness,
                    driver_fcmax, driver_health_notes, name,
                ),
            )
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO ride_profiles
               (name, bike_model, bike_type, wh,
                battery_pct, min_battery_pct, bike_weight_kg,
                riding_style, driver_weight_kg,
                driver_age, driver_sex, driver_fitness,
                driver_fcmax, driver_health_notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name, bike_model, bike_type, wh,
                battery_pct, min_battery_pct, bike_weight_kg,
                riding_style, driver_weight_kg,
                driver_age, driver_sex, driver_fitness,
                driver_fcmax, driver_health_notes,
            ),
        )
        return cur.lastrowid


def list_ride_profiles() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ride_profiles ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_ride_profile(profile_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ride_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_ride_profile(profile_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM ride_profiles WHERE id = ?", (profile_id,))


def update_profile_assist_ratio(profile_name: str, new_ratio: float) -> dict:
    """Aggiorna la media incrementale di assist_ratio per un profilo dopo un
    nuovo calcolo deterministico (vedi ride_analysis_agent.compute_deterministic_energy_analysis).
    Ritorna {"avg_assist_ratio", "assist_ratio_sample_count"} aggiornati."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT avg_assist_ratio, assist_ratio_sample_count FROM ride_profiles WHERE name = ?",
            (profile_name,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Profilo non trovato: {profile_name}")
        prev_avg = row["avg_assist_ratio"] or 0.0
        prev_count = row["assist_ratio_sample_count"] or 0
        new_count = prev_count + 1
        new_avg = (prev_avg * prev_count + new_ratio) / new_count
        conn.execute(
            "UPDATE ride_profiles SET avg_assist_ratio = ?, assist_ratio_sample_count = ? WHERE name = ?",
            (new_avg, new_count, profile_name),
        )
        return {"avg_assist_ratio": new_avg, "assist_ratio_sample_count": new_count}


# Inizializza al primo import
init_db()
