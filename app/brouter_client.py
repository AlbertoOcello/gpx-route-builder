"""
Wrapper minimale per chiamare il server BRouter locale.
"""
import math
import os
from pathlib import Path

import httpx

_base = os.environ.get("BROUTER_URL", "http://localhost:17777")
BROUTER_URL = _base.rstrip("/") + "/brouter"

_SEGMENTS_DIR = Path(__file__).parent.parent / "brouter" / "segments4"
_TILE_BASE_URL = "https://brouter.de/brouter/segments4"


def _tile_name(lat: float, lon: float) -> str:
    lon_base = int(math.floor(lon / 5)) * 5
    lat_base = int(math.floor(lat / 5)) * 5
    return f"E{lon_base}_N{lat_base}.rd5"


def ensure_tile(
    lat: float,
    lon: float,
    progress_cb=None,
) -> tuple[bool, str]:
    """Ensure the BRouter segment tile covering (lat, lon) exists locally.

    Tile name: E{floor(lon/5)*5}_N{floor(lat/5)*5}.rd5  (5°×5° grid, BRouter standard).
    Downloads from brouter.de if not present; writes atomically via a .tmp file.
    progress_cb: optional callable(fraction: float 0..1) for UI progress bars.
    Returns (ok, message).
    """
    name = _tile_name(lat, lon)
    path = _SEGMENTS_DIR / name

    if path.exists():
        return True, f"Tile {name} già presente."

    _SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)
    url = f"{_TILE_BASE_URL}/{name}"
    tmp = path.with_suffix(".tmp")

    try:
        with httpx.stream("GET", url, timeout=300.0, follow_redirects=True) as r:
            if r.status_code == 404:
                return (
                    False,
                    f"Tile {name} non trovato su brouter.de — "
                    "area probabilmente fuori dalla copertura disponibile.",
                )
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(downloaded / total, 1.0))
        tmp.rename(path)
        size_mb = path.stat().st_size // (1024 * 1024)
        return True, f"Tile {name} scaricato ({size_mb} MB)."
    except Exception as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return False, f"Errore download tile {name}: {exc}"


def get_route(waypoints: list[tuple[float, float]],
              profile: str = "trekking",
              output_path: str = "route.gpx") -> str:
    """
    waypoints: lista di tuple (lon, lat), almeno 2 punti (partenza e arrivo).
    profile: nome profilo BRouter (es. 'trekking', 'gravel', futuro 'eleglide_gravel_easy').
    output_path: percorso del file GPX da salvare.

    Ritorna il percorso del file salvato.
    """
    if len(waypoints) < 2:
        raise ValueError("Servono almeno 2 waypoint (partenza e arrivo)")

    lonlats = "|".join(f"{lon},{lat}" for lon, lat in waypoints)

    params = {
        "lonlats": lonlats,
        "profile": profile,
        "alternativeidx": 0,
        "format": "gpx",
    }

    response = httpx.get(BROUTER_URL, params=params, timeout=30.0)

    if response.status_code != 200:
        raise RuntimeError(
            f"BRouter ha risposto con errore {response.status_code}: {response.text}"
        )

    with open(output_path, "wb") as f:
        f.write(response.content)

    return output_path


if __name__ == "__main__":
    # Test rapido: stesso percorso di prova fatto con curl
    path = get_route(
        waypoints=[(13.2278, 43.7137), (13.2400, 43.7200)],
        profile="trekking",
        output_path="routes/generated/test_wrapper.gpx",
    )
    print(f"GPX salvato in: {path}")
