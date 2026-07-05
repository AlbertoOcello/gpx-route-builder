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
    -- Pipeline runs
    CREATE TABLE IF NOT EXISTS routes_generated (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT    DEFAULT (datetime('now')),
        request         TEXT    NOT NULL,   -- JSON RouteRequest
        candidates      TEXT    NOT NULL,   -- JSON list[CandidateRoute + analysis]
        scores          TEXT    NOT NULL,   -- JSON list[scoring result]
        decision        TEXT    NOT NULL,   -- JSON DecisionReport
        winner_id       TEXT                -- "A" | "B" | "C" | null
    );

    CREATE TABLE IF NOT EXISTS routes_approved (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT    DEFAULT (datetime('now')),
        route_gen_id    INTEGER REFERENCES routes_generated(id),
        candidate_id    TEXT    NOT NULL,
        strategy_name   TEXT,
        profile         TEXT,
        distance_km     REAL,
        elevation_gain_m REAL,
        gpx_path        TEXT
    );

    CREATE TABLE IF NOT EXISTS routes_rejected (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT    DEFAULT (datetime('now')),
        route_gen_id    INTEGER REFERENCES routes_generated(id),
        candidate_id    TEXT    NOT NULL,
        strategy_name   TEXT,
        profile         TEXT,
        distance_km     REAL,
        elevation_gain_m REAL,
        reason          TEXT
    );

    -- Feedback utente (SRS §11)
    CREATE TABLE IF NOT EXISTS user_feedback (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT    DEFAULT (datetime('now')),
        route_gen_id    INTEGER REFERENCES routes_generated(id),
        candidate_id    TEXT    NOT NULL,
        rating          INTEGER,    -- 1–5
        too_traffic     INTEGER,    -- 0/1
        too_gravel      INTEGER,    -- 0/1
        too_hard        INTEGER,    -- 0/1
        good_surface    INTEGER,    -- 0/1
        nice_views      INTEGER,    -- 0/1
        would_repeat    INTEGER,    -- 0/1
        notes           TEXT
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

    -- Colonna annotations_json in user_feedback (idempotente via IF NOT EXISTS non supportata
    -- per ALTER, lo gestisce il codice Python sotto)
    """

    with get_conn() as conn:
        conn.executescript(ddl)
        # ALTER TABLE idempotente: aggiungi annotations_json a user_feedback se mancante
        cols = {r[1] for r in conn.execute("PRAGMA table_info(user_feedback)").fetchall()}
        if "annotations_json" not in cols:
            conn.execute("ALTER TABLE user_feedback ADD COLUMN annotations_json TEXT")

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

def save_pipeline_run(
    request: dict,
    candidates: list[dict],
    scored: list[dict],
    decision: dict,
) -> int:
    """Salva un run completo della pipeline in routes_generated. Ritorna l'id inserito."""
    winner_id = decision.get("winner")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO routes_generated (request, candidates, scores, decision, winner_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                json.dumps(request, ensure_ascii=False),
                json.dumps(candidates, ensure_ascii=False, default=str),
                json.dumps(scored, ensure_ascii=False),
                json.dumps(decision, ensure_ascii=False),
                winner_id,
            ),
        )
        return cur.lastrowid


def save_feedback(
    route_gen_id: int,
    candidate_id: str,
    rating: int,
    too_traffic: bool,
    too_gravel: bool,
    too_hard: bool,
    good_surface: bool,
    nice_views: bool,
    would_repeat: bool,
    notes: str,
    annotations: list[dict] | None = None,
) -> int:
    """Salva il feedback utente. Ritorna l'id inserito.

    annotations: lista di segnaposto route_map_annotations al momento del salvataggio
                 (snapshot JSON). I segnaposto 'problema' vengono promossi automaticamente
                 a known_obstacles dalla funzione chiamante.
    """
    annotations_json = json.dumps(annotations or [], ensure_ascii=False)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO user_feedback "
            "(route_gen_id, candidate_id, rating, too_traffic, too_gravel, "
            " too_hard, good_surface, nice_views, would_repeat, notes, annotations_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                route_gen_id, candidate_id, rating,
                int(too_traffic), int(too_gravel), int(too_hard),
                int(good_surface), int(nice_views), int(would_repeat),
                notes, annotations_json,
            ),
        )
        return cur.lastrowid


def save_route_approved(route_gen_id: int, candidate: dict) -> int:
    a = candidate["analysis"]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO routes_approved "
            "(route_gen_id, candidate_id, strategy_name, profile, "
            " distance_km, elevation_gain_m, gpx_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                route_gen_id,
                candidate["id"],
                candidate.get("strategy_name"),
                candidate.get("profile"),
                a.get("distance_km"),
                a.get("elevation_gain_m"),
                candidate.get("gpx_path"),
            ),
        )
        return cur.lastrowid


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
) -> int:
    """Inserisce un ostacolo noto. Idempotente: se esiste già (stessa lat/lon arrotondate
    a 5 decimali, ~1 m), aggiorna la descrizione e riattiva invece di duplicare."""
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
                "annotation_id=COALESCE(?,annotation_id) WHERE id=?",
                (description, route_name, annotation_id, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO known_obstacles (lat, lon, description, route_name, annotation_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (lat, lon, description, route_name, annotation_id),
        )
        return cur.lastrowid


def get_active_obstacles() -> list[dict]:
    """Ritorna tutti gli ostacoli attivi (active=1)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, lat, lon, description, route_name, created_at "
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
        "routes_generated", "routes_approved", "routes_rejected",
        "user_feedback", "geocoding_cache",
        "known_places", "known_avoid_roads",
        "known_water_points", "known_bars", "known_scenic_points",
        "route_map_annotations",
    ]
    with get_conn() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


# ── Ride profiles (Analisi Giro) ─────────────────────────────────────────────

def save_ride_profile(
    name: str,
    bike_model: str | None = None,
    bike_type: str | None = None,
    wh: float | None = None,
    assistance_level: int | None = None,
    battery_pct: float | None = None,
    bike_weight_kg: float | None = None,
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
                   bike_model=?, bike_type=?, wh=?, assistance_level=?,
                   battery_pct=?, bike_weight_kg=?, driver_weight_kg=?,
                   driver_age=?, driver_sex=?, driver_fitness=?,
                   driver_fcmax=?, driver_health_notes=?
                   WHERE name=?""",
                (
                    bike_model, bike_type, wh, assistance_level,
                    battery_pct, bike_weight_kg, driver_weight_kg,
                    driver_age, driver_sex, driver_fitness,
                    driver_fcmax, driver_health_notes, name,
                ),
            )
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO ride_profiles
               (name, bike_model, bike_type, wh, assistance_level,
                battery_pct, bike_weight_kg, driver_weight_kg,
                driver_age, driver_sex, driver_fitness,
                driver_fcmax, driver_health_notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name, bike_model, bike_type, wh, assistance_level,
                battery_pct, bike_weight_kg, driver_weight_kg,
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


# Inizializza al primo import
init_db()
