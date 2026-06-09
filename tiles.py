"""
Offline map tiles for GeoPort.

Provides a Flask blueprint that:
  * proxies map tiles through a local on-disk cache
      GET /tiles/<provider>/<z>/<x>/<y>.png
    - served from cache when present (works fully offline),
    - fetched from the real provider and cached on first view when online,
    - falls back to a neutral "offline" placeholder when neither is possible.
  * pre-downloads a whole region for guaranteed offline use
      POST /download_area      {provider, north, south, east, west, min_zoom, max_zoom}
      GET  /download_status
      POST /cancel_download
  * reports connectivity + cache stats and can clear the cache
      GET  /offline_status
      GET  /tile_providers
      POST /clear_cache        {provider?}

Tiles are stored as  ~/GeoPort/tiles/<provider>/<z>/<x>/<y>.png  so the cache is
easy to inspect, back up, or copy between machines.

Only key-free tile sources are used by default so the app works out of the box.
Bulk downloading still has to respect each provider's usage policy; keep areas
modest and zoom ranges sensible.
"""

import os
import math
import time
import zlib
import struct
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Blueprint, jsonify, request, Response, send_file

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

HOME_DIR = os.path.expanduser("~")
CACHE_ROOT = os.path.join(HOME_DIR, "GeoPort", "tiles")

USER_AGENT = "GeoPort-Offline/1.0 (+https://github.com/davesc63/GeoPort)"
FETCH_TIMEOUT = 6          # seconds for a single live tile fetch
DOWNLOAD_WORKERS = 5       # parallel fetches during an area download
MAX_AREA_TILES = 250_000   # refuse runaway area downloads above this

# Tile providers we are willing to proxy. {s} = subdomain, substituted from
# `subdomains`. Note Esri uses z/y/x order, handled by the template itself.
#
# Order matters — it drives the basemap dropdown. The default providers are
# key-free sources that permit light/app usage (Carto, Esri). OpenStreetMap's
# OWN tile servers (tile.openstreetmap.org) actively block proxy/app access
# ("Access blocked" tiles), so they are intentionally NOT offered here; use
# Carto, which is rendered from the same OpenStreetMap data.
PROVIDERS = {
    "carto_voyager": {
        "name": "Streets (Carto Voyager)",
        "url": "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
        "subdomains": ["a", "b", "c", "d"],
        "max_zoom": 20,
        "attribution": '&copy; OpenStreetMap contributors &copy; CARTO',
    },
    "carto_light": {
        "name": "Light (Carto Positron)",
        "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "subdomains": ["a", "b", "c", "d"],
        "max_zoom": 20,
        "attribution": '&copy; OpenStreetMap contributors &copy; CARTO',
    },
    "carto_dark": {
        "name": "Dark (Carto Dark Matter)",
        "url": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        "subdomains": ["a", "b", "c", "d"],
        "max_zoom": 20,
        "attribution": '&copy; OpenStreetMap contributors &copy; CARTO',
    },
    "esri_sat": {
        "name": "Satellite (Esri)",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "subdomains": [],
        "max_zoom": 19,
        "attribution": 'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community',
    },
    "topo": {
        "name": "Topographic (OpenTopoMap)",
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "subdomains": ["a", "b", "c"],
        "max_zoom": 17,
        "attribution": '&copy; OpenStreetMap contributors, SRTM | &copy; OpenTopoMap (CC-BY-SA)',
    },
}

DEFAULT_PROVIDER = "carto_voyager"

# --------------------------------------------------------------------------- #
# Connectivity (cheap, cached so we don't probe on every tile)
# --------------------------------------------------------------------------- #

_online_state = {"online": True, "checked_at": 0.0}
_online_lock = threading.Lock()
_ONLINE_TTL = 15  # seconds


def is_online():
    """Best-effort, low-latency connectivity check with a short-lived cache."""
    now = time.time()
    with _online_lock:
        if now - _online_state["checked_at"] < _ONLINE_TTL:
            return _online_state["online"]
    online = False
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=1.0):
                online = True
                break
        except OSError:
            continue
    with _online_lock:
        _online_state["online"] = online
        _online_state["checked_at"] = now
    return online


# --------------------------------------------------------------------------- #
# Placeholder tile (256x256 solid light-grey PNG, built without PIL)
# --------------------------------------------------------------------------- #

def _solid_png(width, height, rgba):
    """Return PNG bytes for a solid-colour image (no external deps)."""
    r, g, b, a = rgba

    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    row = bytes([0]) + bytes([r, g, b, a]) * width                # filter byte + pixels
    raw = row * height
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


_PLACEHOLDER_PNG = _solid_png(256, 256, (60, 60, 66, 255))


# --------------------------------------------------------------------------- #
# Cache helpers
# --------------------------------------------------------------------------- #

def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tile_path(provider, z, x, y):
    return os.path.join(CACHE_ROOT, provider, str(z), str(x), f"{y}.png")


def _upstream_url(provider, z, x, y):
    meta = PROVIDERS[provider]
    url = meta["url"].replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
    subs = meta.get("subdomains") or []
    if "{s}" in url and subs:
        url = url.replace("{s}", subs[(x + y) % len(subs)])
    return url


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


def _fetch_tile(provider, z, x, y):
    """Fetch a tile from upstream and write it to the cache. Returns bytes or None."""
    try:
        resp = _session.get(_upstream_url(provider, z, x, y), timeout=FETCH_TIMEOUT)
        if resp.status_code == 200 and resp.content:
            path = _tile_path(provider, z, x, y)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(resp.content)
            os.replace(tmp, path)
            return resp.content
    except requests.RequestException:
        pass
    return None


# --------------------------------------------------------------------------- #
# Slippy-map maths for area downloads
# --------------------------------------------------------------------------- #

def deg2num(lat, lon, zoom):
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    x = min(max(x, 0), n - 1)
    y = min(max(y, 0), n - 1)
    return x, y


def _iter_area_tiles(north, south, east, west, min_zoom, max_zoom):
    for z in range(min_zoom, max_zoom + 1):
        x0, y0 = deg2num(north, west, z)
        x1, y1 = deg2num(south, east, z)
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                yield z, x, y


def _count_area_tiles(north, south, east, west, min_zoom, max_zoom):
    total = 0
    for z in range(min_zoom, max_zoom + 1):
        x0, y0 = deg2num(north, west, z)
        x1, y1 = deg2num(south, east, z)
        total += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
    return total


# --------------------------------------------------------------------------- #
# Background area-download job (one at a time)
# --------------------------------------------------------------------------- #

_job = {
    "active": False, "provider": None, "total": 0, "done": 0,
    "cached": 0, "fetched": 0, "errors": 0, "cancel": False,
    "started_at": 0.0, "finished_at": 0.0, "message": "",
}
_job_lock = threading.Lock()


def _run_download(provider, bounds, min_zoom, max_zoom, total):
    def worker(tile):
        if _job["cancel"]:
            return
        z, x, y = tile
        path = _tile_path(provider, z, x, y)
        if os.path.exists(path):
            with _job_lock:
                _job["cached"] += 1
                _job["done"] += 1
            return
        data = _fetch_tile(provider, z, x, y)
        with _job_lock:
            if data:
                _job["fetched"] += 1
            else:
                _job["errors"] += 1
            _job["done"] += 1

    try:
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            tiles = _iter_area_tiles(bounds["north"], bounds["south"],
                                     bounds["east"], bounds["west"], min_zoom, max_zoom)
            list(pool.map(worker, tiles))
    finally:
        with _job_lock:
            _job["active"] = False
            _job["finished_at"] = time.time()
            _job["message"] = "Cancelled" if _job["cancel"] else "Complete"


# --------------------------------------------------------------------------- #
# Blueprint / routes
# --------------------------------------------------------------------------- #

tiles_bp = Blueprint("tiles", __name__)


@tiles_bp.route("/tiles/<provider>/<int:z>/<int:x>/<int:y>.png")
def get_tile(provider, z, x, y):
    if provider not in PROVIDERS:
        return Response(_PLACEHOLDER_PNG, mimetype="image/png")

    path = _tile_path(provider, z, x, y)
    if os.path.exists(path):
        resp = send_file(path, mimetype="image/png")
        resp.headers["X-GeoPort-Tile"] = "cache"
        return resp

    if is_online():
        data = _fetch_tile(provider, z, x, y)
        if data:
            resp = Response(data, mimetype="image/png")
            resp.headers["X-GeoPort-Tile"] = "live"
            return resp

    # Offline and uncached: neutral placeholder so the map stays usable.
    resp = Response(_PLACEHOLDER_PNG, mimetype="image/png")
    resp.headers["X-GeoPort-Tile"] = "placeholder"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@tiles_bp.route("/tile_providers")
def tile_providers():
    return jsonify({
        "default": DEFAULT_PROVIDER,
        "providers": [
            {"key": k, "name": v["name"], "max_zoom": v["max_zoom"],
             "attribution": v["attribution"]}
            for k, v in PROVIDERS.items()
        ],
    })


@tiles_bp.route("/offline_status")
def offline_status():
    tile_count = 0
    byte_count = 0
    if os.path.isdir(CACHE_ROOT):
        for root, _dirs, files in os.walk(CACHE_ROOT):
            for f in files:
                if f.endswith(".png"):
                    tile_count += 1
                    try:
                        byte_count += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
    return jsonify({
        "online": is_online(),
        "cache_tiles": tile_count,
        "cache_bytes": byte_count,
        "cache_dir": CACHE_ROOT,
    })


@tiles_bp.route("/estimate_area", methods=["POST"])
def estimate_area():
    d = request.get_json(force=True, silent=True) or {}
    try:
        total = _count_area_tiles(
            float(d["north"]), float(d["south"]), float(d["east"]), float(d["west"]),
            int(d["min_zoom"]), int(d["max_zoom"]))
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Invalid bounds"}), 400
    return jsonify({"tiles": total, "too_large": total > MAX_AREA_TILES,
                    "max_tiles": MAX_AREA_TILES})


@tiles_bp.route("/download_area", methods=["POST"])
def download_area():
    d = request.get_json(force=True, silent=True) or {}
    provider = d.get("provider", DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        return jsonify({"error": "Unknown provider"}), 400
    try:
        bounds = {k: float(d[k]) for k in ("north", "south", "east", "west")}
        min_zoom = int(d["min_zoom"])
        max_zoom = int(d["max_zoom"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Invalid bounds or zoom"}), 400

    if min_zoom > max_zoom:
        min_zoom, max_zoom = max_zoom, min_zoom

    total = _count_area_tiles(bounds["north"], bounds["south"],
                              bounds["east"], bounds["west"], min_zoom, max_zoom)
    if total > MAX_AREA_TILES:
        return jsonify({"error": f"Area too large: {total} tiles (max {MAX_AREA_TILES}). "
                                 f"Reduce the area or max zoom.", "tiles": total}), 400

    with _job_lock:
        if _job["active"]:
            return jsonify({"error": "A download is already running"}), 409
        _job.update({"active": True, "provider": provider, "total": total, "done": 0,
                     "cached": 0, "fetched": 0, "errors": 0, "cancel": False,
                     "started_at": time.time(), "finished_at": 0.0, "message": "Downloading"})

    threading.Thread(target=_run_download,
                     args=(provider, bounds, min_zoom, max_zoom, total),
                     daemon=True).start()
    return jsonify({"started": True, "tiles": total, "provider": provider})


@tiles_bp.route("/download_status")
def download_status():
    with _job_lock:
        return jsonify(dict(_job))


@tiles_bp.route("/cancel_download", methods=["POST"])
def cancel_download():
    with _job_lock:
        _job["cancel"] = True
    return jsonify({"cancelling": True})


@tiles_bp.route("/clear_cache", methods=["POST"])
def clear_cache():
    d = request.get_json(force=True, silent=True) or {}
    provider = d.get("provider")
    target = os.path.join(CACHE_ROOT, provider) if provider else CACHE_ROOT
    removed = 0
    if os.path.isdir(target):
        for root, _dirs, files in os.walk(target, topdown=False):
            for f in files:
                if f.endswith(".png"):
                    try:
                        os.remove(os.path.join(root, f))
                        removed += 1
                    except OSError:
                        pass
            try:
                os.rmdir(root)
            except OSError:
                pass
    return jsonify({"cleared": removed})
