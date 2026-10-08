"""
Hardware-free, network-free smoke test of the Flask app on whatever OS runs it
(CI runs it on macOS and Windows): page + setup banner, platform notices, Host
guard, device listing, port check, and the tile cache incl. Carto API keys.
"""
import os
import sys
import json
import socket
import tempfile

sys.argv = [sys.argv[0]]                 # main.py parses CLI args at import
import tiles
import main
import device_manager

tmp = tempfile.mkdtemp()
tiles.CACHE_ROOT = os.path.join(tmp, "tiles")
tiles.CONFIG_PATH = os.path.join(tmp, "config.json")
os.environ.pop("CARTO_API_KEY", None)

# No network: every upstream tile fetch returns a fake PNG and is recorded.
requested = []


class _FakeResp:
    status_code = 200
    content = b"\x89PNG\r\n\x1a\nfake tile"


def _fake_get(url, timeout=None):
    requested.append(url)
    return _FakeResp()


tiles._session.get = _fake_get
tiles.is_online = lambda: True
main.is_online = lambda: True

client = main.app.test_client()

# ---- page + setup banner ------------------------------------------------- #
notice = main.setup_notices()
r = client.get("/")
html = r.get_data(as_text=True)
assert r.status_code == 200, r.status_code
assert 'id="setupWarn"' in html
assert ('style="display:block"' in html) == bool(notice)
assert notice[:30] in html
print(f"PASS: page renders on {main.current_platform}; banner: {notice or '(none)'}")

# ---- platform-specific setup notices ------------------------------------- #
msg = client.get("/app_meta").get_json()["setup_message"]
if main.is_windows:
    assert ("Administrator" in msg) == (not main._is_admin())
    assert (main._amds_notice() in msg) == (not main._amds_running())
    assert "sudo" not in msg
else:
    assert ("sudo" in msg) == (not main._is_admin())
    assert "Administrator" not in msg and "Apple Mobile Device Service" not in msg
assert ("Apple Devices" in device_manager._WIFI_UNAVAILABLE) == main.is_windows
print("PASS: setup notices + Wi-Fi help match the platform")

# ---- Host guard (anti DNS-rebinding) ------------------------------------- #
assert client.get("/", base_url="http://evil.example").status_code == 403
assert client.get("/", base_url="http://127.0.0.1:54321").status_code == 200
print("PASS: non-local Host header rejected")

# ---- device listing never crashes ---------------------------------------- #
r = client.get("/list_devices")
assert r.status_code == 200 and isinstance(r.get_json(), dict), r.get_data()
if main.is_windows and not main._amds_running():
    assert r.get_json() == {"error": main._amds_notice()}
print(f"PASS: /list_devices -> {r.get_json()}")

# ---- one device that can't be reached doesn't hide the others ------------ #
from types import SimpleNamespace
real = (main.list_devices, main.create_using_usbmux, main.start_amds, main.discover_wifi)
main.start_amds = lambda wait=0: True
main.discover_wifi = lambda exclude=(): {}       # no real devices on this machine's Wi-Fi


def fake_create(udid, connection_type=None, autopair=True):
    if udid == "GONE":                           # e.g. a stale Wi-Fi entry
        raise RuntimeError("got an error message: {'MessageType': 'Result', 'Number': 3}")
    return SimpleNamespace(short_info={"DeviceName": "Good"}, enable_wifi_connections=True)


main.create_using_usbmux = fake_create
main.list_devices = lambda: [SimpleNamespace(serial="GONE", connection_type="Network"),
                             SimpleNamespace(serial="GOOD", connection_type="USB")]
try:
    assert list(client.get("/list_devices").get_json()) == ["GOOD"]
    main.list_devices = lambda: [SimpleNamespace(serial="GONE", connection_type="Network")]
    assert "'Number': 3" in client.get("/list_devices").get_json()["error"]
finally:
    main.list_devices, main.create_using_usbmux, main.start_amds, main.discover_wifi = real
print("PASS: an unreachable device is skipped, not fatal to the whole list")

# ---- paired devices on Wi-Fi that usbmux didn't list are found directly --- #
real = (main.list_devices, main.create_using_usbmux, main.start_amds,
        main.discover_wifi, main.direct_wifi_lockdown)
excluded = []


def fake_discover(exclude=()):
    excluded.append(set(exclude))
    return {"WIFI": "192.168.1.207", "ASLEEP": "192.168.1.208"}


def fake_direct(udid):
    if udid == "ASLEEP":
        raise TimeoutError("timed out")
    return SimpleNamespace(short_info={"DeviceName": "Wi-Fi phone"})


main.start_amds = lambda wait=0: True
main.create_using_usbmux = fake_create
main.discover_wifi, main.direct_wifi_lockdown = fake_discover, fake_direct
try:
    main.list_devices = lambda: [SimpleNamespace(serial="GOOD", connection_type="Network")]
    r = client.get("/list_devices").get_json()
    assert r["WIFI"]["Wifi"][0]["DeviceName"] == "Wi-Fi phone" and "Wifi" in r["GOOD"], r
    assert "ASLEEP" not in r and excluded[-1] == {"GOOD"}, "usbmux's Wi-Fi devices aren't re-probed"
    main.list_devices = lambda: []
    main.discover_wifi = lambda exclude=(): {"ASLEEP": "192.168.1.208"}
    assert client.get("/list_devices").get_json() == {"error": main.WIFI_ASLEEP}
finally:
    (main.list_devices, main.create_using_usbmux, main.start_amds,
     main.discover_wifi, main.direct_wifi_lockdown) = real
print("PASS: paired devices on Wi-Fi are listed even when usbmux doesn't; asleep ones get a hint")

# ---- a device that stops answering fails Connect instead of hanging it ---- #
import threading
import time
release = threading.Event()


def silent_device(*a, **k):
    release.wait()
    raise ConnectionError("gave up")


real = (device_manager.DEVICE_ANSWER_TIMEOUT, main.device_lockdown, device_manager.device_lockdown)
device_manager.DEVICE_ANSWER_TIMEOUT = 0.2
connect = dict(udid="SILENT", connType="USB", ios_version="26.5", deviceName="iPad")
try:
    main.device_lockdown = silent_device         # silent from the Developer Mode check on
    r = client.post("/connect_device", json=connect).get_json()
    assert r == {"connected": False, "error": device_manager.NOT_ANSWERING}, r
    main.device_lockdown = lambda udid, conn: SimpleNamespace(developer_mode_status=True)
    device_manager.device_lockdown = silent_device   # goes silent while being prepared
    for attempt in (1, 2):                       # ...and Connect can be pressed again
        started = time.monotonic()
        r = client.post("/connect_device", json=connect).get_json()
        assert time.monotonic() - started < 2, "Connect must give up, not hang"
        assert r["error"] == device_manager.NOT_ANSWERING and r["device"]["status"] == "error", r
finally:
    device_manager.DEVICE_ANSWER_TIMEOUT, main.device_lockdown, device_manager.device_lockdown = real
    release.set()
    main.manager.remove("SILENT")
print("PASS: a device that stops answering fails Connect (and can be retried) instead of hanging it")

# ---- Windows: start Apple's device service when it isn't running --------- #
import subprocess
launched, up = [], [False]
real = (main._amds_running, main._apple_devices_installed, main._itunes_amds_installed)


def fake_popen(cmd, *a, **k):
    launched.append(cmd)
    up[0] = True                                 # opening the app starts the service


main._amds_running = lambda: up[0]
main._apple_devices_installed = lambda: True
main._itunes_amds_installed = lambda: False
main.subprocess = SimpleNamespace(Popen=fake_popen)
main._amds_last_start[0] = float("-inf")
try:
    assert main.start_amds(wait=2) is True
    assert launched == [["explorer.exe", r"shell:AppsFolder\AppleInc.AppleDevices_nzyj5cx40ttqa!App"]]
    up[0] = False
    assert main.start_amds() is False and len(launched) == 1, "opens it at most once a minute"
    assert (main.AMDS_STARTING in main.setup_notices()) == main.is_windows
    main._apple_devices_installed = lambda: False
    main._amds_last_start[0] = float("-inf")
    assert main.start_amds() is False and len(launched) == 1, "nothing installed to start"
    assert (main.AMDS_MISSING in main.setup_notices()) == main.is_windows
finally:
    main._amds_running, main._apple_devices_installed, main._itunes_amds_installed = real
    main.subprocess = subprocess
    main._amds_last_start[0] = float("-inf")
print("PASS: opens Apple Devices to start its device service, at most once a minute")

# ---- port check ---------------------------------------------------------- #
with socket.socket() as srv:
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    busy = srv.getsockname()[1]
    assert main.is_port_in_use(busy)
assert not main.is_port_in_use(busy)
print("PASS: port-in-use check")

# ---- stable address (saved locations live in the browser, per origin) ---- #
import threading
from werkzeug.serving import make_server
srv = make_server("127.0.0.1", 0, main.app, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
assert main.running_instance(srv.server_port), "should recognise a running betterGeoPort"
with socket.socket() as other:                   # some other program on a port
    other.bind(("127.0.0.1", 0))
    other.listen()
    taken = other.getsockname()[1]
    assert not main.running_instance(taken)
    main.args.port = taken
    picked = main.choose_port()
    main.args.port = None
    assert taken < picked < taken + 20, (taken, picked)   # next free port, not random
srv.shutdown()
print("PASS: reuses a running instance; busy port falls back to the next one")

# ---- saved locations UI is present (stored in localStorage only) --------- #
for el in ('id="savedList"', 'id="saveName"', 'id="saveBtn"', "localStorage"):
    assert el in html, el
assert "/saved_locations" not in html          # nothing is sent to the server
print("PASS: saved-locations UI present, browser-only")

# ---- Windows tunnel: only IPv6 may reach the device ---------------------- #
if main.is_windows:
    import asyncio
    from pymobiledevice3.remote.tunnel_service import RemotePairingTunnel

    class _FakeTun:
        def __init__(self, packets):
            self.packets = list(packets)

        async def async_read(self):
            if not self.packets:
                raise ConnectionResetError
            return self.packets.pop(0)

    class _FakeTunnel:
        tun_read_task = RemotePairingTunnel.tun_read_task

        def __init__(self, packets):
            self.tun, self.sent = _FakeTun(packets), []

        async def send_packet_to_device(self, packet):
            self.sent.append(packet)

    ipv4 = bytes([0x45]) + bytes(19)                  # Windows' 169.254.x.x chatter
    ipv6 = bytes([0x60]) + bytes(39)
    t = _FakeTunnel([ipv4, ipv6, None, ipv4, ipv6])
    asyncio.run(t.tun_read_task())
    assert t.sent == [ipv6, ipv6], t.sent
    print("PASS: Windows tunnel forwards only IPv6 packets to the device")

# ---- tiles: Carto without a key ------------------------------------------ #
carto = "/tiles/carto_voyager/6/31/24.png"
carto_path = tiles._tile_path("carto_voyager", 6, 31, 24)
r = client.get(carto)
assert r.headers["X-GeoPort-Tile"] == "live"
assert "?key=" not in requested[-1]
assert not os.path.exists(carto_path), "keyless Carto watermark must not be cached"
area = dict(north=40.5, south=40.3, east=-3.6, west=-3.8, min_zoom=10, max_zoom=10)
r = client.post("/download_area", json=dict(provider="carto_voyager", **area))
assert r.status_code == 400 and "API key" in r.get_json()["error"]
print("PASS: keyless Carto shown live, not cached, area download refused")

# ---- tiles: Carto with a key (config file, Notepad-style BOM) ------------ #
with open(tiles.CONFIG_PATH, "w", encoding="utf-8-sig") as fh:
    json.dump({"carto_api_key": " test key/+ "}, fh)
r = client.get(carto)
assert r.headers["X-GeoPort-Tile"] == "live"
assert requested[-1].endswith("?key=test%20key%2F%2B"), requested[-1]
assert os.path.exists(carto_path)
assert client.get(carto).headers["X-GeoPort-Tile"] == "cache"
os.environ["CARTO_API_KEY"] = "envkey"
assert tiles._upstream_url("carto_light", 1, 0, 0).endswith("?key=envkey")
del os.environ["CARTO_API_KEY"]
assert "?key=" not in tiles._upstream_url("esri_sat", 6, 31, 24)
print("PASS: Carto key from config/env appended upstream; keyed tiles cached")

# ---- tiles: offline ------------------------------------------------------ #
tiles.is_online = lambda: False
before = len(requested)
assert client.get(carto).headers["X-GeoPort-Tile"] == "cache"
assert client.get("/tiles/esri_sat/3/7/3.png").headers["X-GeoPort-Tile"] == "placeholder"
assert len(requested) == before, "offline must not hit the network"
print("PASS: offline serves cache, else placeholder, without network")

print("\nSMOKE TEST PASSED")
