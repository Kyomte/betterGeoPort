"""
GeoPort — iOS location simulator (offline-maps + multi-device rebuild).

Based on GeoPort by davesc63 (https://github.com/davesc63/GeoPort), GPL-3.0.

This rebuild:
  * serves map tiles through a local offline cache (see tiles.py),
  * controls several iOS devices at once, each with its own simulated
    location, plus a "Set all" broadcast (see device_manager.py),
  * removes the api.geoport.me telemetry phone-home,
  * starts instantly with no internet (version/fuel lookups are best-effort
    in the background), and binds to localhost only,
  * runs on macOS (sudo) and Windows (UAC / Administrator).
"""

import os
import sys
import time
import ctypes
import socket
import signal
import locale
import random
import logging
import argparse
import threading
import subprocess
import webbrowser

import requests
import pycountry
from flask import Flask, jsonify, render_template, request

from pymobiledevice3.usbmux import list_devices
from pymobiledevice3.lockdown import create_using_usbmux, create_using_tcp

from tiles import tiles_bp, is_online
from device_manager import DeviceManager, is_ios_17_plus, device_lockdown

# --------------------------------------------------------------------------- #
# Args / logging / app
# --------------------------------------------------------------------------- #

parser = argparse.ArgumentParser()
parser.add_argument('--no-browser', action='store_true', help='Skip auto opening the browser')
parser.add_argument('--port', type=int, help='Port to listen on')
parser.add_argument('--wifihost', type=str, help='WiFi IP address to connect to')
parser.add_argument('--udid', type=str, help='Device UDID to target')
parser.add_argument('--host', type=str, default='127.0.0.1', help='Interface to bind (default localhost)')
parser.add_argument('--no-elevate', action='store_true',
                    help='Windows: do not relaunch as Administrator via UAC')
args = parser.parse_args()

_log_dir = os.path.join(os.path.expanduser("~"), "GeoPort")
os.makedirs(_log_dir, exist_ok=True)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.StreamHandler(),
                              logging.FileHandler(os.path.join(_log_dir, "geoport.log"),
                                                  encoding="utf-8")])
logger = logging.getLogger("GeoPort")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("werkzeug").disabled = True

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True   # pick up template edits without a restart
app.register_blueprint(tiles_bp)
manager = DeviceManager()
pending = {}                      # udid -> (lat, lng) staged by /update_location

# This server controls real devices and runs as root/Administrator, so reject any request
# whose Host header isn't local. That blocks DNS-rebinding attacks where a
# malicious website resolves its name to 127.0.0.1 to reach this server.
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
if args.host and args.host not in ("127.0.0.1", "0.0.0.0"):
    _ALLOWED_HOSTS.add(args.host)


@app.before_request
def _guard_host():
    host = (request.host or "").rsplit(":", 1)[0].strip("[]")
    if host not in _ALLOWED_HOSTS:
        return jsonify({"error": "Forbidden host"}), 403

APP_VERSION_NUMBER = "4.1.0-offline"
APP_VERSION_TYPE = "offline+multi"
GITHUB_REPO = "davesc63/GeoPort"
FUEL_API_URL = "https://projectzerothree.info/api.php?format=json"

home_dir = os.path.expanduser("~")
is_windows = sys.platform == 'win32'
current_platform = {'win32': 'Windows', 'linux': 'Linux', 'darwin': 'MacOS'}.get(sys.platform, 'Unknown')
chosen_port = 54321

# Best-effort metadata refreshed in the background so '/' never blocks offline.
app_meta = {"version_message": None, "broadcast": "", "fuel": None, "user_locale": None}


def _is_admin():
    """root on macOS/Linux, an elevated (UAC) token on Windows."""
    if is_windows:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:                               # noqa: BLE001
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


# On Windows, usbmux is provided by Apple Mobile Device Service (installed with
# the "Apple Devices" app or iTunes), which listens on this local port.
AMDS_ADDRESS = ("127.0.0.1", 27015)
AMDS_MISSING = ("Apple Mobile Device Service not found — install the Apple Devices "
                "app (Microsoft Store) or iTunes, then press Refresh.")


def _amds_running():
    try:
        with socket.create_connection(AMDS_ADDRESS, timeout=0.5):
            return True
    except OSError:
        return False


def setup_notices():
    """Things the user must fix before devices will work (shown as a banner)."""
    notices = []
    if is_windows:
        if not _is_admin():
            notices.append("Not running as Administrator — connecting iOS 17+ devices needs "
                           "admin rights. Restart betterGeoPort and accept the UAC prompt.")
        if not _amds_running():
            notices.append(AMDS_MISSING)
    elif not _is_admin():
        notices.append("Not running as root — connecting iOS 17+ devices needs sudo.")
    return " ".join(notices)

# --------------------------------------------------------------------------- #
# Best-effort background metadata (never blocks the UI)
# --------------------------------------------------------------------------- #

def get_user_country():
    try:
        loc, _ = locale.getlocale()
        if loc:
            country = pycountry.countries.get(alpha_2=loc.split('_')[-1])
            if country:
                return country.name
    except Exception:                                   # noqa: BLE001
        pass
    # No third-party IP-geolocation fallback (privacy): just default the map.
    return None


def refresh_app_meta():
    app_meta["user_locale"] = get_user_country()
    try:
        url = f'https://raw.githubusercontent.com/{GITHUB_REPO}/main/CURRENT_VERSION'
        gh = requests.get(url, timeout=2).text.strip()
        if gh and gh > APP_VERSION_NUMBER:
            app_meta["version_message"] = f"Upstream GeoPort {gh} is available."
    except Exception:                                   # noqa: BLE001
        pass
    try:
        app_meta["fuel"] = requests.get(FUEL_API_URL, timeout=3).json()
    except Exception:                                   # noqa: BLE001
        app_meta["fuel"] = None


# --------------------------------------------------------------------------- #
# Device listing (ported from the original; works against real hardware)
# --------------------------------------------------------------------------- #

@app.route('/list_devices')
def list_devices_route():
    try:
        connected = {}

        def add(udid, conn_type, info):
            connected.setdefault(udid, {}).setdefault(conn_type, []).append(info)

        if args.wifihost:
            ld = create_using_tcp(hostname=args.wifihost, identifier=args.udid)
            info = ld.short_info
            try:
                ld.enable_wifi_connections = True
            except Exception:                           # noqa: BLE001
                pass
            info['wifiState'] = True
            info['userLocale'] = app_meta.get("user_locale")
            info['ConnectionType'] = 'Network'
            add(args.udid, "Manual Wifi", info)

        for device in list_devices():
            udid = device.serial
            conn_type = device.connection_type
            ld = create_using_usbmux(udid, connection_type=conn_type, autopair=True)
            info = ld.short_info
            try:
                if not ld.enable_wifi_connections:
                    ld.enable_wifi_connections = True
                    logger.info(f"Enabled Wi-Fi sync for {info.get('DeviceName')} "
                                f"(appears over Wi-Fi shortly; keep it on the same network)")
            except Exception as exc:                    # noqa: BLE001
                logger.info(f"Wi-Fi sync toggle failed for {info.get('DeviceName')}: {exc}")
            info['wifiState'] = True
            info['userLocale'] = app_meta.get("user_locale")
            add(udid, "Wifi" if conn_type == "Network" else conn_type, info)

        return jsonify(connected)
    except Exception as exc:                            # noqa: BLE001
        logger.error(f"list_devices error: {exc}")
        if is_windows and not _amds_running():
            return jsonify({'error': AMDS_MISSING})
        return jsonify({'error': str(exc)})


# --------------------------------------------------------------------------- #
# Per-device connection
# --------------------------------------------------------------------------- #

def _check_developer_mode(udid, conn_type):
    try:
        ld = device_lockdown(udid, conn_type)   # resolves + caches Wi-Fi IP if needed
        return bool(ld.developer_mode_status)
    except Exception as exc:                            # noqa: BLE001
        logger.error(f"developer_mode check failed: {exc}")
        return False


@app.route('/connect_device', methods=['POST'])
def connect_device():
    data = request.get_json(force=True, silent=True) or {}
    udid = data.get('udid')
    conn_type = data.get('connType')
    ios_version = data.get('ios_version')
    if not udid:
        return jsonify({'error': 'No udid provided'}), 400

    if not _check_developer_mode(udid, conn_type):
        return jsonify({'developer_mode_required': True})

    sess = manager.get_or_create(udid, conn_type, ios_version,
                                 name=data.get('deviceName'), device_class=data.get('deviceClass'))

    # Make sure the Developer Disk Image is mounted (idempotent; needs internet
    # only the very first time per iOS build, then it is cached locally).
    try:
        sess.mount_developer_image()
    except Exception as exc:                            # noqa: BLE001
        logger.info(f"[{sess.name}] mount note: {exc}")

    ok, err = sess.connect()
    return jsonify({'connected': ok, 'device': sess.to_dict(), 'error': err})


@app.route('/enable_developer_mode', methods=['POST'])
def enable_developer_mode_route():
    data = request.get_json(force=True, silent=True) or {}
    udid = data.get('udid')
    conn_type = data.get('connType')
    ios_version = data.get('ios_version')
    if not udid:
        return jsonify({'error': 'No udid provided'}), 400
    sess = manager.get_or_create(udid, conn_type, ios_version,
                                 name=data.get('deviceName'), device_class=data.get('deviceClass'))
    ok, err = sess.ensure_developer_mode()
    if not ok:
        return jsonify({'error': err})
    try:
        sess.mount_developer_image()
    except Exception as exc:                            # noqa: BLE001
        logger.info(f"[{sess.name}] mount note: {exc}")
    return jsonify({'success': True, 'udid': udid})


@app.route('/mount_developer_image', methods=['POST'])
def mount_developer_image_route():
    data = request.get_json(force=True, silent=True) or {}
    sess = manager.get(data.get('udid'))
    if not sess:
        return jsonify({'error': 'Device not connected'}), 400
    try:
        sess.mount_developer_image()
        return jsonify({'success': True})
    except Exception as exc:                            # noqa: BLE001
        return jsonify({'error': str(exc)})


@app.route('/disconnect_device', methods=['POST'])
def disconnect_device():
    data = request.get_json(force=True, silent=True) or {}
    manager.remove(data.get('udid'))
    return jsonify({'disconnected': True})


# --------------------------------------------------------------------------- #
# Location: per-device + broadcast
# --------------------------------------------------------------------------- #

@app.route('/update_location', methods=['POST'])
def update_location():
    data = request.get_json(force=True, silent=True) or {}
    udid = data.get('udid')
    try:
        pending[udid] = (float(data['lat']), float(data['lng']))
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'Invalid coordinates'}), 400
    return jsonify({'updated': True})


def _coords_from(data, udid):
    if data.get('lat') is not None and data.get('lng') is not None:
        return float(data['lat']), float(data['lng'])
    if udid in pending:
        return pending[udid]
    return None


@app.route('/set_location', methods=['POST'])
def set_location():
    data = request.get_json(force=True, silent=True) or {}
    udid = data.get('udid')
    sess = manager.get(udid)
    if not sess:
        return jsonify({'error': 'Device not connected'}), 400
    coords = _coords_from(data, udid)
    if coords is None:
        return jsonify({'error': 'No coordinates'}), 400
    sess.set_location(*coords)
    return jsonify({'success': True, 'udid': udid, 'location': {'lat': coords[0], 'lng': coords[1]}})


@app.route('/stop_location', methods=['POST'])
def stop_location():
    data = request.get_json(force=True, silent=True) or {}
    sess = manager.get(data.get('udid'))
    if not sess:
        return jsonify({'error': 'Device not connected'}), 400
    ok, err = sess.stop_location()
    return jsonify({'success': ok, 'error': err})


@app.route('/set_all_locations', methods=['POST'])
def set_all_locations():
    data = request.get_json(force=True, silent=True) or {}
    try:
        lat, lng = float(data['lat']), float(data['lng'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'Invalid coordinates'}), 400
    results = manager.set_all(lat, lng)
    return jsonify({'results': results, 'location': {'lat': lat, 'lng': lng}})


@app.route('/stop_all_locations', methods=['POST'])
def stop_all_locations():
    return jsonify({'results': manager.stop_all()})


@app.route('/device_status')
def device_status():
    return jsonify({'devices': manager.status()})


# --------------------------------------------------------------------------- #
# Fuel overlay (kept; degrades to empty offline)
# --------------------------------------------------------------------------- #

@app.route('/api/fuel_types')
def get_fuel_types():
    region = request.args.get('region', 'All')
    data = app_meta.get("fuel")
    if not data:
        return jsonify({}), 503
    prices = next((r['prices'] for r in data['regions'] if r['region'] == region), [])
    return jsonify(list({e['type'] for e in prices}))


@app.route('/api/data/<fuel_type>')
def get_fuel_data(fuel_type):
    region = request.args.get('region', 'All')
    data = app_meta.get("fuel")
    if not data:
        return jsonify({}), 503
    prices = next((r['prices'] for r in data['regions'] if r['region'] == region), [])
    return jsonify(next((e for e in prices if e['type'] == fuel_type), None))


# --------------------------------------------------------------------------- #
# Page + lifecycle
# --------------------------------------------------------------------------- #

@app.route('/')
def index():
    return render_template(
        'map.html',
        version_message=app_meta.get("version_message"),
        github_broadcast=app_meta.get("broadcast", ""),
        user_locale=app_meta.get("user_locale"),
        app_version_num=APP_VERSION_NUMBER,
        app_version_type=APP_VERSION_TYPE,
        error_message=None,
        current_platform=current_platform,
        setup_message=setup_notices(),
    )


@app.route('/favicon.ico')
def favicon():
    return ('', 204)


@app.route('/app_meta')
def app_meta_route():
    return jsonify({**app_meta, "online": is_online(),
                    "app_version": APP_VERSION_NUMBER, "setup_message": setup_notices()})


@app.route('/exit', methods=['POST'])
def exit_app():
    logger.warning("Shutting down GeoPort")
    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"success": True, "message": "Server is shutting down..."})


def _shutdown():
    manager.shutdown()
    time.sleep(0.5)
    if not is_windows:              # on Windows os.kill() is TerminateProcess
        os.kill(os.getpid(), signal.SIGINT)
    os._exit(0)


def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)           # Windows retries refused localhost connects for ~2s
        return s.connect_ex(('127.0.0.1', port)) == 0


def choose_port():
    global chosen_port
    chosen_port = args.port or 54321
    if is_port_in_use(chosen_port):
        chosen_port = random.randint(49215, 65535)
    logger.info(f"Serving: http://localhost:{chosen_port}")
    return chosen_port


def open_browser():
    time.sleep(1.5)
    url = f'http://localhost:{chosen_port}'
    try:
        if is_windows:
            # We are usually elevated here; going through explorer.exe hands the
            # URL to the user's (unelevated) shell so the browser isn't run as admin.
            subprocess.Popen(["explorer.exe", url])
        else:
            webbrowser.get().open(url)
    except Exception:                                   # noqa: BLE001
        pass


def relaunch_as_admin():
    """Windows: re-run this program elevated via the UAC prompt.
    Returns True if the elevated copy was started (this one should exit)."""
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        params = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, os.getcwd(), 1)
    return rc > 32                  # <= 32 is an error, e.g. the user declined UAC


if __name__ == '__main__':
    if is_windows:
        if not _is_admin() and not args.no_elevate:
            if relaunch_as_admin():
                sys.exit(0)
            logger.warning("UAC elevation declined — continuing without Administrator rights.")
        try:
            ctypes.windll.kernel32.SetConsoleTitleW("betterGeoPort — close this window to quit")
        except Exception:                               # noqa: BLE001
            pass

    notice = setup_notices()
    if notice:
        logger.warning("*" * 60)
        logger.warning(notice)
        logger.warning("*" * 60)

    threading.Thread(target=refresh_app_meta, daemon=True).start()
    choose_port()
    if not args.no_browser:
        threading.Thread(target=open_browser, daemon=True).start()

    app.run(debug=False, use_reloader=False, threaded=True,
            port=chosen_port, host=args.host)
