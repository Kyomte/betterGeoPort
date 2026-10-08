"""
Per-device session management for GeoPort (multi-device).

The original GeoPort kept a single "active" device in module-level globals
(udid, lockdown, rsd_host/port, location, terminate flags), so connecting a
second iPhone clobbered the first.  This module replaces that with one
``DeviceSession`` per UDID, each owning its own tunnel thread, its own location
thread, and its own simulated coordinate, coordinated by a ``DeviceManager``.

The pymobiledevice3 call sequences (RSD discovery + QUIC tunnel for iOS 17+,
lockdown + DVT LocationSimulation) are ported faithfully from the original so
on-device behaviour matches; they are simply made instance-based and
thread-safe instead of global.
"""

import os
import re
import sys
import time
import socket
import asyncio
import logging
import plistlib
import threading

from pymobiledevice3.bonjour import browse_mobdev2
from pymobiledevice3.lockdown import create_using_usbmux, create_using_tcp
from pymobiledevice3.services.amfi import AmfiService
from pymobiledevice3.common import get_home_folder
from pymobiledevice3.services.mobile_image_mounter import (
    auto_mount, MobileImageMounterService, PersonalizedImageMounter)
from pymobiledevice3.exceptions import DeviceHasPasscodeSetError
from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.utils import stop_remoted_if_required, resume_remoted_if_required, get_rsds
from pymobiledevice3.remote.tunnel_service import (
    create_core_device_tunnel_service_using_rsd,
    create_core_device_tunnel_service_using_remotepairing,
    get_remote_pairing_tunnel_services,
    CoreDeviceTunnelProxy,
    RemotePairingTunnel,
)

logger = logging.getLogger("GeoPort")


if sys.platform == "win32":
    async def _tun_read_task_ipv6_only(self):
        """Forward only IPv6 packets from the Wintun adapter to the device.

        pymobiledevice3 4.13.x forwards *every* packet Windows emits on the
        adapter, including IPv4 chatter on the 169.254.x.x address Windows
        auto-assigns it. The device frames the tunnel stream by IPv6 header
        length, so a single IPv4 packet desyncs it: the first connection works,
        then every later one times out (WinError 121). Upstream pymobiledevice3
        drops non-IPv6 packets; do the same. async_read() is also cancellable,
        so the tunnel can shut down cleanly.
        """
        try:
            while True:
                packet = await self.tun.async_read()
                if packet and (packet[0] >> 4) == 6:
                    await self.send_packet_to_device(packet)
        except ConnectionResetError:
            logger.warning("tunnel: connection reset while forwarding to the device")
        except OSError as exc:
            logger.warning(f"tunnel: {exc.__class__.__name__} while forwarding to the device")

    RemotePairingTunnel.tun_read_task = _tun_read_task_ipv6_only

# Discovering RSD services, toggling macOS `remoted` (a no-op elsewhere) and
# creating the TUN adapter (utun on macOS, Wintun on Windows) must not happen
# from two devices at once, or the tunnels race.  Serialise *setup*; tunnels
# then run in parallel once established.
_SETUP_LOCK = threading.Lock()
BONJOUR_TIMEOUT = 5


def _windows_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                   # noqa: BLE001
        return False


LOST_CONNECTION = ("Lost the connection to the device (cable unplugged, Wi-Fi dropped, "
                   "or it went to sleep). Unlock it and press Connect again.")

DEVICE_ANSWER_TIMEOUT = 20
NOT_ANSWERING = ("The device stopped answering while betterGeoPort was getting it ready. "
                 "Unlock it, keep the screen on, and press Connect again.")


class DeviceNotAnswering(RuntimeError):
    pass


def device_call(fn, *args):
    """Return fn(*args), but raise DeviceNotAnswering if it hasn't returned
    within DEVICE_ANSWER_TIMEOUT seconds. pymobiledevice3's device sockets have
    no timeout, so a device that stops answering mid-request (seen on Windows
    during Connect) would otherwise block forever. The stuck call is left on a
    daemon thread, so it can't hold up quitting."""
    result = {}

    def run():
        try:
            result["value"] = fn(*args)
        except BaseException as exc:                    # noqa: BLE001
            result["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(DEVICE_ANSWER_TIMEOUT)
    if worker.is_alive():
        raise DeviceNotAnswering(NOT_ANSWERING)
    if "error" in result:
        raise result["error"]
    return result["value"]


def tunnel_alive(client):
    """False once either direction of a pymobiledevice3 tunnel has stopped or
    the device closed its end (the tunnel then carries nothing)."""
    for name in ("_tun_read_task", "_sock_read_task"):
        task = getattr(client, name, None)
        if task is not None and task.done():
            return False
    writer = getattr(client, "_writer", None)
    return not (writer is not None and writer.is_closing())


CLEAR_ACK_TIMEOUT = 5


def clear_simulated_location(dvt):
    """Put the device back on its real GPS location.

    iOS keeps a simulated location until it is cleared or the device restarts;
    closing the connection does not end it. pymobiledevice3's
    LocationSimulation.clear() only sends the request, so also wait for the
    device's reply before the connection is closed under it."""
    channel = dvt.make_channel(LocationSimulation.IDENTIFIER)
    channel.stopLocationSimulation()
    dvt.service.socket.settimeout(CLEAR_ACK_TIMEOUT)
    try:
        channel.receive_plist()
    except socket.timeout:
        logger.warning("location reset sent, but the device didn't acknowledge it")


def is_ios_17_plus(version_string):
    try:
        return int(str(version_string).split('.')[0]) >= 17
    except (ValueError, IndexError, AttributeError):
        return False


def _ver2(version_string):
    try:
        parts = [int(x) for x in str(version_string).split('.')[:2]]
        return (parts[0], parts[1] if len(parts) > 1 else 0)
    except (ValueError, AttributeError):
        return (0, 0)


def is_legacy_quic(version_string):
    """iOS 17.0–17.3 use the RSD / remote-pairing QUIC tunnel; iOS 17.4+
    (incl. iOS 18 / 26) use the lockdown CoreDevice TCP tunnel proxy."""
    return (17, 0) <= _ver2(version_string) <= (17, 3)


if sys.platform == "win32":
    _WIFI_UNAVAILABLE = (
        "This device isn't available over Wi-Fi yet. Connect it once by USB, turn on "
        "Wi-Fi sync for it in the Apple Devices app (or \"Sync with this device over "
        "Wi-Fi\" in iTunes), and keep it awake on the same network — or use USB.")
else:
    _WIFI_UNAVAILABLE = (
        "This device isn't available over Wi-Fi yet. macOS registers a "
        "device for Wi-Fi automatically once it stays connected to the "
        "same network (that's why the iPhone works). Keep it awake on "
        "Wi-Fi, or use USB.")


WIFI_ASLEEP = ("Your device is on this Wi-Fi but isn't answering. It's probably asleep: "
               "unlock it and keep the screen on, then try again.")


def device_lockdown(udid, connection_type, discover=False):
    """Return a lockdown for the device over the requested transport.
    Wi-Fi (Network) goes through the OS usbmux (macOS usbmuxd / Windows Apple
    Mobile Device Service) when it lists the device on Wi-Fi, else straight to
    the device if discover_wifi() found it. Raises a friendly error if the
    device isn't reachable over Wi-Fi."""
    conn = "Network" if connection_type in ("Network", "Manual") else "USB"
    try:
        return create_using_usbmux(udid, connection_type=conn, autopair=True)
    except Exception:                                   # noqa: BLE001
        if conn != "Network":
            raise
    if udid not in wifi_hosts:
        raise RuntimeError(_WIFI_UNAVAILABLE)
    try:
        return direct_wifi_lockdown(udid)
    except OSError:
        raise RuntimeError(WIFI_ASLEEP)


# ----- Wi-Fi without the OS usbmux ------------------------------------------ #
# The OS usbmux only lists a device on Wi-Fi after it has found and connected
# to it itself, and that can stall (seen on Windows after the computer switched
# networks) while the device is right there and reachable. So also look for
# paired devices' Wi-Fi sync announcements (Bonjour _apple-mobdev2._tcp, named
# after the device's Wi-Fi MAC) and talk to them directly; on the device's
# side it's the same lockdown connection.

if sys.platform == "win32":
    PAIR_RECORD_DIRS = [os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                                     "Apple", "Lockdown")]
else:
    PAIR_RECORD_DIRS = ["/var/db/lockdown", "/var/lib/lockdown"]
wifi_hosts = {}                     # udid -> (IPv4 address, pair record), from discover_wifi()


def paired_wifi_macs():
    """{Wi-Fi MAC: (udid, pair record)} for every device paired with this
    computer (the OS's pair records, and pymobiledevice3's own)."""
    found = {}
    for folder in PAIR_RECORD_DIRS + [str(get_home_folder())]:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            udid, ext = os.path.splitext(name)
            if ext != ".plist" or not re.fullmatch(r"[0-9A-Fa-f-]{24,40}", udid):
                continue
            try:
                with open(os.path.join(folder, name), "rb") as f:
                    record = plistlib.load(f)
            except Exception:                           # noqa: BLE001
                continue
            mac = str(record.get("WiFiMACAddress") or "").lower()
            if mac and record.get("HostPrivateKey"):
                found.setdefault(mac, (udid, record))
    return found


def discover_wifi(exclude=(), timeout=3):
    """Paired devices announcing themselves on the local network, other than
    the `exclude` UDIDs: {udid: IPv4 address}. Remembers them for
    device_lockdown(). Doesn't browse if there's nobody else to look for."""
    macs = {mac: v for mac, v in paired_wifi_macs().items() if v[0] not in exclude}
    if not macs:
        return {}
    try:
        answers = asyncio.run(browse_mobdev2(timeout=timeout))
    except Exception as exc:                            # noqa: BLE001
        logger.info(f"Wi-Fi discovery failed: {exc.__class__.__name__}: {exc}")
        return {}
    found = {}
    for answer in answers:
        mac = answer.name.split("@", 1)[0].lower()      # "<wifi mac>@<ipv6>…"
        ipv4 = next((ip for ip in answer.ips if "." in ip and ":" not in ip), None)
        if mac in macs and ipv4:
            udid, record = macs[mac]
            wifi_hosts[udid] = (ipv4, record)
            found[udid] = ipv4
    return found


def direct_wifi_lockdown(udid):
    """Lockdown straight to a device discover_wifi() found. Raises OSError
    (after ~1 s) if it doesn't answer, e.g. it's asleep."""
    host, record = wifi_hosts[udid]
    return create_using_tcp(hostname=host, identifier=udid, autopair=False, pair_record=record)


class DeviceSession:
    """Owns one device's connection, tunnel thread and location thread."""

    def __init__(self, udid, connection_type, ios_version, name=None, device_class=None):
        self.udid = udid
        self.connection_type = connection_type          # "USB" | "Network" | "Manual"
        self.ios_version = ios_version
        self.name = name or udid
        self.device_class = device_class or "iDevice"

        self.lockdown = None                            # used for iOS < 17
        self.rsd_host = None
        self.rsd_port = None

        self._tunnel_thread = None
        self._terminate_tunnel = threading.Event()
        self._tunnel_error = None
        self._location_thread = None
        self._terminate_location = threading.Event()

        self.location = None                            # (lat, lng) currently simulated
        self.status = "idle"                            # idle|connecting|connected|locating|error
        self.last_error = None
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()             # only guards begin_connect()

    # ----- serialisable view for the UI -------------------------------- #
    def to_dict(self):
        return {
            "udid": self.udid,
            "name": self.name,
            "deviceClass": self.device_class,
            "iosVersion": self.ios_version,
            "connectionType": self.connection_type,
            "status": self.status,
            "location": {"lat": self.location[0], "lng": self.location[1]} if self.location else None,
            "connected": self.rsd_host is not None or self.lockdown is not None,
            "lastError": self.last_error,
        }

    # ----- connection -------------------------------------------------- #
    def begin_connect(self):
        """Mark a connect as in progress; False if one already is (so a second
        click doesn't open a second tunnel to the same device)."""
        with self._state_lock:
            if self.status == "connecting":
                return False
            self.status = "connecting"
            self.last_error = None
            return True

    def fail_connect(self, message):
        """End a connect that begin_connect() started but that failed before
        connect() (e.g. preparing the device), so Connect can be pressed again."""
        with self._state_lock:
            self.status = "error"
            self.last_error = message
        logger.error(f"[{self.name}] connect failed: {message}")

    def connect(self):
        """Establish the tunnel (iOS 17+) or lockdown (iOS < 17) for this device."""
        with self._lock:
            self.status = "connecting"
            self.last_error = None
            try:
                if is_ios_17_plus(self.ios_version):
                    self._tunnel_error = None
                    self._start_tunnel_blocking()
                    if not self.rsd_host or not self.rsd_port:
                        raise RuntimeError(self._tunnel_error or
                            "Tunnel did not establish — device not discovered "
                            "(unlock it; for USB check the cable; for Wi-Fi same network).")
                else:
                    self.lockdown = create_using_usbmux(self.udid, autopair=True)
                self.status = "connected"
                return True, None
            except Exception as exc:                    # noqa: BLE001
                self.status = "error"
                self.last_error = str(exc)
                logger.error(f"[{self.name}] connect failed: {exc}")
                return False, str(exc)

    def _start_tunnel_blocking(self, attempts=20):
        """Spawn the per-device tunnel thread and wait for rsd_host/port."""
        self._close_tunnel()                            # never two tunnels to one device
        self._terminate_tunnel.clear()
        self._tunnel_thread = threading.Thread(target=self._tunnel_worker, daemon=True)
        self._tunnel_thread.start()
        for _ in range(attempts):
            if self.rsd_host and self.rsd_port:
                return
            if self.status == "error":
                return
            time.sleep(1)

    def _tunnel_worker(self):
        try:
            logger.info(f"[{self.name}] starting {self.connection_type} tunnel (iOS {self.ios_version})")
            if is_legacy_quic(self.ios_version):
                # iOS 17.0–17.3
                if self.connection_type in ("Network", "Manual"):
                    asyncio.run(self._wifi_quic_tunnel())
                else:
                    asyncio.run(self._usb_quic_tunnel())
            else:
                # iOS 17.4+ (incl. iOS 26): lockdown TCP tunnel over USB or Wi-Fi
                asyncio.run(self._tcp_tunnel())
        except Exception as exc:                        # noqa: BLE001
            import traceback
            if str(exc) == LOST_CONNECTION:
                self._tunnel_error = LOST_CONNECTION
            else:
                self._tunnel_error = f"{exc.__class__.__name__}: {exc}".strip()
            # The tunnel is gone, but the device keeps simulating self.location
            # until it is cleared, so remember it (stop_location reconnects).
            self.rsd_host = self.rsd_port = None
            self._terminate_location.set()
            if sys.platform == "win32" and not _windows_admin():
                # Creating the Wintun adapter is what fails without elevation.
                self._tunnel_error += (" — iOS 17+ tunnels need Administrator rights; "
                                       "restart betterGeoPort and accept the UAC prompt.")
            self.status = "error"
            self.last_error = self._tunnel_error
            logger.error(f"[{self.name}] tunnel error: {self._tunnel_error}")
            logger.error(traceback.format_exc())

    async def _usb_quic_tunnel(self):
        # RSD discovery + remoted toggling are serialised across devices.
        with _SETUP_LOCK:
            logger.info(f"[{self.name}] USB: stopping remoted + discovering RSD ({BONJOUR_TIMEOUT}s)")
            stop_remoted_if_required()
            rsds = await get_rsds(BONJOUR_TIMEOUT)
            logger.info(f"[{self.name}] USB: found {len(rsds)} RSD service(s): "
                        f"{[getattr(r, 'udid', '?') for r in rsds]}")
            match = [r for r in rsds if getattr(r, "udid", None) == self.udid]
            if not match:
                resume_remoted_if_required()
                raise RuntimeError("Device not found via RemoteServiceDiscovery (USB). "
                                   "Reconnect the cable and unlock the device.")
            logger.info(f"[{self.name}] USB: creating tunnel service + starting QUIC tunnel")
            service = await create_core_device_tunnel_service_using_rsd(match[0], autopair=True)
            tunnel_cm = service.start_quic_tunnel()
            tunnel_result = await tunnel_cm.__aenter__()
            resume_remoted_if_required()
        try:
            self.rsd_host = tunnel_result.address
            self.rsd_port = str(tunnel_result.port)
            logger.info(f"[{self.name}] QUIC tunnel {self.rsd_host}:{self.rsd_port}")
            while not self._terminate_tunnel.is_set():
                await asyncio.sleep(0.5)
        finally:
            await tunnel_cm.__aexit__(None, None, None)

    async def _wifi_quic_tunnel(self):
        with _SETUP_LOCK:
            stop_remoted_if_required()
            logger.info(f"[{self.name}] WiFi: browsing remote-pairing services (Bonjour, {BONJOUR_TIMEOUT}s)")
            services = await get_remote_pairing_tunnel_services(BONJOUR_TIMEOUT)
            logger.info(f"[{self.name}] WiFi: found {len(services)} service(s): "
                        f"{[getattr(s, 'remote_identifier', '?') for s in services]}")
            match = [s for s in services if getattr(s, "remote_identifier", None) == self.udid]
            target = match[0] if match else (services[0] if services else None)
            if target is None:
                resume_remoted_if_required()
                raise RuntimeError("Device not found via remote pairing (Bonjour). "
                                   "Make sure the iPhone is awake, unlocked, and on the same Wi-Fi.")
            logger.info(f"[{self.name}] WiFi: connecting {getattr(target,'hostname','?')}:{getattr(target,'port','?')}")
            service = await create_core_device_tunnel_service_using_remotepairing(
                self.udid, target.hostname, target.port)
            logger.info(f"[{self.name}] WiFi: starting QUIC tunnel")
            tunnel_cm = service.start_quic_tunnel()
            tunnel_result = await tunnel_cm.__aenter__()
            resume_remoted_if_required()
        try:
            self.rsd_host = tunnel_result.address
            self.rsd_port = str(tunnel_result.port)
            logger.info(f"[{self.name}] WiFi QUIC tunnel {self.rsd_host}:{self.rsd_port}")
            while not self._terminate_tunnel.is_set():
                await asyncio.sleep(0.5)
        finally:
            await tunnel_cm.__aexit__(None, None, None)

    async def _tcp_tunnel(self):
        """iOS 17.4+ lockdown CoreDevice TCP tunnel. The same code path serves
        USB and Wi-Fi — the transport is decided by the lockdown connection_type
        ('USB' over cable, 'Network' over Wi-Fi)."""
        conn = "Network" if self.connection_type in ("Network", "Manual") else "USB"
        with _SETUP_LOCK:
            logger.info(f"[{self.name}] {conn}: opening lockdown + CoreDevice TCP tunnel")
            stop_remoted_if_required()
            try:
                # discover=False: the IP was already resolved + cached during the
                # developer-mode check, so we never call asyncio.run inside this loop.
                lockdown = device_lockdown(self.udid, self.connection_type, discover=False)
                service = CoreDeviceTunnelProxy(lockdown)
                tunnel_cm = service.start_tcp_tunnel()
                tunnel_result = await tunnel_cm.__aenter__()
            finally:
                resume_remoted_if_required()
            logger.info(f"[{self.name}] tunnel established: {tunnel_result.address}:{tunnel_result.port}")
        try:
            self.rsd_host = tunnel_result.address
            self.rsd_port = str(tunnel_result.port)
            while not self._terminate_tunnel.is_set():
                await asyncio.sleep(0.5)
                if not tunnel_alive(tunnel_result.client):
                    raise ConnectionError(LOST_CONNECTION)
        finally:
            await tunnel_cm.__aexit__(None, None, None)

    # ----- developer mode + image ------------------------------------- #
    def ensure_developer_mode(self):
        lockdown = device_lockdown(self.udid, self.connection_type)
        if lockdown.developer_mode_status:
            return True, None
        try:
            AmfiService(lockdown).enable_developer_mode()
        except DeviceHasPasscodeSetError:
            return False, ("Device has a passcode set. Temporarily remove the passcode "
                           "(Settings → Face ID & Passcode) to enable Developer Mode.")
        return True, None

    def mount_developer_image(self):
        """Make sure the developer disk image is mounted (needed for location
        simulation; the device unmounts it on every restart).

        pymobiledevice3 4.13.x's auto_mount() re-downloads the ~16 MB
        personalized image from GitHub whenever its cached build differs from
        a hard-coded build ID, which is always the case now, so on every
        connect, even when the image is already mounted. That made Connect
        take minutes on slow Wi-Fi. Check first, and reuse the cached image.

        The check is time-limited (device_call): a device that stopped
        answering here once left Connect stuck on "connecting" for good. The
        mount itself isn't, as a first-time download can take minutes."""
        lockdown = device_call(device_lockdown, self.udid, self.connection_type)
        if not is_ios_17_plus(self.ios_version):
            return self._auto_mount(lockdown)
        if device_call(lambda: MobileImageMounterService(lockdown=lockdown).is_image_mounted("Personalized")):
            logger.info(f"[{self.name}] developer image already mounted")
            return
        cache = get_home_folder() / "Xcode_iOS_DDI_Personalized"
        files = [cache / "Image.dmg", cache / "BuildManifest.plist", cache / "Image.trustcache"]
        if all(f.exists() for f in files):
            started = time.time()
            logger.info(f"[{self.name}] mounting developer image (cached copy)…")
            try:
                asyncio.run(PersonalizedImageMounter(lockdown=lockdown).mount(*files))
                logger.info(f"[{self.name}] developer image mounted ({time.time() - started:.0f}s)")
                return
            except Exception as exc:                    # noqa: BLE001
                if "AlreadyMounted" in exc.__class__.__name__:
                    logger.info(f"[{self.name}] developer image already mounted")
                    return
                logger.info(f"[{self.name}] cached image didn't mount "
                            f"({exc.__class__.__name__}: {exc}); downloading a fresh one")
        self._auto_mount(lockdown)

    def _auto_mount(self, lockdown):
        started = time.time()
        logger.info(f"[{self.name}] downloading + mounting developer image "
                    f"(one-time, can take a few minutes on slow networks)…")
        try:
            asyncio.run(auto_mount(lockdown))           # async in pymobiledevice3 4.13.x
            logger.info(f"[{self.name}] developer image mounted ({time.time() - started:.0f}s)")
        except Exception as exc:                        # noqa: BLE001
            if "already" in str(exc).lower() or "AlreadyMounted" in exc.__class__.__name__:
                logger.info(f"[{self.name}] developer image already mounted")
                return
            raise

    # ----- location ---------------------------------------------------- #
    def set_location(self, lat, lng, wait=0):
        """Start simulating (lat, lng). Status is "setting" until the device
        confirms it ("locating"), or "error" if it fails — never "locating"
        on a mere attempt. With wait > 0, block up to that many seconds for the
        outcome and return (ok, error); ok is None while still pending."""
        done = threading.Event()
        with self._lock:
            self._stop_location_thread()
            self.location = None
            self.last_error = None
            self.status = "setting"
            terminate = self._terminate_location = threading.Event()
            self._location_thread = threading.Thread(
                target=self._location_worker, args=(lat, lng, terminate, done), daemon=True)
            self._location_thread.start()
        if wait and done.wait(wait):
            return self.status == "locating", self.last_error
        return None, None

    def _location_ok(self, lat, lng, terminate, done):
        if not terminate.is_set():                      # not superseded by a newer set
            self.location = (lat, lng)
            self.status = "locating"
            self.last_error = None
            logger.warning(f"[{self.name}] Location set {lat},{lng}")
        done.set()

    def _location_failed(self, message, terminate, done):
        if not terminate.is_set():
            self.location = None
            self.status = "error"
            self.last_error = message
            logger.error(f"[{self.name}] set location error: {message}")
        done.set()

    def _location_worker(self, lat, lng, terminate, done):
        try:
            if is_ios_17_plus(self.ios_version):
                asyncio.run(self._location_worker_rsd(lat, lng, terminate, done))
            else:
                with DvtSecureSocketProxyService(lockdown=self.lockdown) as dvt:
                    if terminate.is_set():              # stopped while connecting
                        done.set()
                        return
                    LocationSimulation(dvt).clear()
                    LocationSimulation(dvt).set(lat, lng)
                    self._location_ok(lat, lng, terminate, done)
                    while not terminate.is_set():
                        time.sleep(0.5)
        except ConnectionResetError:
            self._location_failed("Connection reset — try ↩ Real location, or Disconnect and "
                                  "Connect again.", terminate, done)
        except Exception as exc:                        # noqa: BLE001
            if getattr(exc, "winerror", None) == 121 or isinstance(exc, TimeoutError):
                message = ("The device didn't answer through the tunnel (timed out). "
                           "Disconnect and Connect again; for Wi-Fi keep it unlocked nearby.")
            else:
                message = str(exc) or exc.__class__.__name__
            self._location_failed(message, terminate, done)

    async def _location_worker_rsd(self, lat, lng, terminate, done):
        async with RemoteServiceDiscoveryService((self.rsd_host, int(self.rsd_port))) as rsd:
            with DvtSecureSocketProxyService(rsd) as dvt:
                if terminate.is_set():                  # stopped while connecting: a late
                    done.set()                          # set would undo the reset
                    return
                LocationSimulation(dvt).set(lat, lng)
                self._location_ok(lat, lng, terminate, done)
                while not terminate.is_set():
                    await asyncio.sleep(0.5)

    def _stop_location_thread(self):
        self._terminate_location.set()
        if self._location_thread and self._location_thread.is_alive():
            self._location_thread.join(timeout=3)
        self._location_thread = None

    def linked(self):
        """True while there is a live connection to send commands over."""
        if is_ios_17_plus(self.ios_version):
            return bool(self.rsd_host and self.rsd_port)
        return self.lockdown is not None

    def stop_location(self, reconnect=True):
        """Stop simulating and put the device back on its real location,
        confirmed by the device. If the connection has dropped (or the clear
        fails over a stale one), reconnect and try again, unless
        reconnect=False. Returns (ok, error)."""
        with self._lock:
            self._stop_location_thread()
            error = None
            for attempt in range(2 if reconnect else 1):
                if attempt or not self.linked():
                    if not reconnect:
                        break
                    if not self.begin_connect():
                        return False, "Still connecting. Try again in a moment."
                    try:
                        self.mount_developer_image()    # unmounted if the device restarted
                    except Exception as exc:            # noqa: BLE001
                        logger.info(f"[{self.name}] mount note: {exc.__class__.__name__}: {exc}")
                    ok, error = self.connect()
                    if not ok:
                        break
                try:
                    self._clear_on_device()
                except Exception as exc:                # noqa: BLE001
                    error = str(exc) or exc.__class__.__name__
                    logger.warning(f"[{self.name}] location reset failed: {error}")
                    continue
                self.location = None
                self.status = "connected"
                self.last_error = None
                logger.warning(f"[{self.name}] Back to the real location")
                return True, None
            if error is None:                           # not linked, reconnect=False
                if self.location is None:
                    return True, None                   # nothing we set is left on it
                error = "the connection to the device was lost"
            self.status = "error"
            self.last_error = ("Couldn't put the device back on its real location "
                               f"({error}). Unlock it and press ↩ Real location.")
            return False, self.last_error

    def _clear_on_device(self):
        if is_ios_17_plus(self.ios_version):
            asyncio.run(self._clear_location_rsd())
        else:
            with DvtSecureSocketProxyService(lockdown=self.lockdown) as dvt:
                clear_simulated_location(dvt)

    async def _clear_location_rsd(self):
        async with RemoteServiceDiscoveryService((self.rsd_host, int(self.rsd_port))) as rsd:
            with DvtSecureSocketProxyService(rsd) as dvt:
                clear_simulated_location(dvt)

    def _close_tunnel(self):
        self._terminate_tunnel.set()
        if self._tunnel_thread and self._tunnel_thread.is_alive():
            self._tunnel_thread.join(timeout=3)
        self._tunnel_thread = None
        self.rsd_host = self.rsd_port = None

    def disconnect(self):
        """Put the device back on its real location, then close the connection.
        Doesn't reconnect to do it (quitting can't wait for that)."""
        with self._lock:
            try:
                ok, error = self.stop_location(reconnect=False)
            except Exception as exc:                    # noqa: BLE001
                ok, error = False, str(exc)
            self._close_tunnel()
            self.lockdown = None
            self.status = "idle"
            return ok, error


class DeviceManager:
    """Registry of DeviceSessions keyed by UDID, plus broadcast helpers."""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get(self, udid):
        with self._lock:
            return self._sessions.get(udid)

    def get_or_create(self, udid, connection_type, ios_version, name=None, device_class=None):
        with self._lock:
            sess = self._sessions.get(udid)
            if sess is None:
                sess = DeviceSession(udid, connection_type, ios_version, name, device_class)
                self._sessions[udid] = sess
            else:
                # keep connection details fresh
                sess.connection_type = connection_type
                sess.ios_version = ios_version
                if name:
                    sess.name = name
            return sess

    def remove(self, udid):
        """Disconnect and forget a device. Returns (ok, error) for putting it
        back on its real location."""
        with self._lock:
            sess = self._sessions.pop(udid, None)
        if sess:
            return sess.disconnect()
        return True, None

    def sessions(self):
        with self._lock:
            return list(self._sessions.values())

    def connected_sessions(self):
        return [s for s in self.sessions() if s.rsd_host or s.lockdown]

    def status(self):
        return [s.to_dict() for s in self.sessions()]

    # ----- broadcast --------------------------------------------------- #
    def set_all(self, lat, lng):
        results = {}
        for sess in self.connected_sessions():
            try:
                sess.set_location(lat, lng)             # outcome shows up in status()
                results[sess.udid] = "setting"
            except Exception as exc:                    # noqa: BLE001
                results[sess.udid] = f"error: {exc}"
        return results

    def stop_all(self):
        """Put every device back on its real location, including ones whose
        connection dropped while they were still simulating one."""
        results = {}
        for sess in self.sessions():
            if not (sess.linked() or sess.location):
                continue
            ok, err = sess.stop_location()
            results[sess.udid] = "ok" if ok else f"error: {err}"
        return results

    def shutdown(self, timeout=4):
        """Disconnect every device, putting each back on its real location.
        In parallel and for at most `timeout` seconds: Windows ends the process
        about 5 s after its console window is closed."""
        threads = [threading.Thread(target=self._disconnect_quietly, args=(s,), daemon=True)
                   for s in self.sessions()]
        for t in threads:
            t.start()
        deadline = time.monotonic() + timeout
        for t in threads:
            t.join(max(0, deadline - time.monotonic()))

    @staticmethod
    def _disconnect_quietly(sess):
        try:
            ok, err = sess.disconnect()
            if not ok:
                logger.error(f"[{sess.name}] left on the simulated location: {err}")
        except Exception as exc:                        # noqa: BLE001
            logger.error(f"[{sess.name}] disconnect failed: {exc}")
