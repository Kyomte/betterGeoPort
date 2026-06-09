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

import time
import asyncio
import logging
import threading

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.amfi import AmfiService
from pymobiledevice3.services.mobile_image_mounter import auto_mount
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
)

logger = logging.getLogger("GeoPort")

# Discovering RSD services and toggling macOS `remoted` must not happen from two
# devices at once, or the tunnels race.  Serialise *setup*; tunnels then run in
# parallel once established.
_SETUP_LOCK = threading.Lock()
BONJOUR_TIMEOUT = 5


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


def device_lockdown(udid, connection_type, discover=False):
    """Return a lockdown for the device over the requested transport.
    Wi-Fi (Network) requires macOS to have promoted the device to a usbmux
    'Network' connection — the CoreDevice tunnel only works over that, not over
    a raw TCP lockdown. Raises a friendly error if the device isn't on Wi-Fi."""
    conn = "Network" if connection_type in ("Network", "Manual") else "USB"
    try:
        return create_using_usbmux(udid, connection_type=conn, autopair=True)
    except Exception:                                   # noqa: BLE001
        if conn == "Network":
            raise RuntimeError(
                "This device isn't available over Wi-Fi yet. macOS registers a "
                "device for Wi-Fi automatically once it stays connected to the "
                "same network (that's why the iPhone works). Keep it awake on "
                "Wi-Fi, or use USB.")
        raise


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
        self._terminate_tunnel.clear()
        self.rsd_host = self.rsd_port = None
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
            self._tunnel_error = f"{exc.__class__.__name__}: {exc}".strip()
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
        # auto_mount is an async coroutine in pymobiledevice3 4.13.x.
        lockdown = device_lockdown(self.udid, self.connection_type)
        try:
            asyncio.run(auto_mount(lockdown))
            logger.info(f"[{self.name}] developer image mounted")
        except Exception as exc:                        # noqa: BLE001
            if "already" in str(exc).lower() or "AlreadyMounted" in exc.__class__.__name__:
                logger.info(f"[{self.name}] developer image already mounted")
                return
            raise

    # ----- location ---------------------------------------------------- #
    def set_location(self, lat, lng):
        with self._lock:
            self._stop_location_thread()
            self.location = (lat, lng)
            self._terminate_location = threading.Event()
            self._location_thread = threading.Thread(
                target=self._location_worker, args=(lat, lng), daemon=True)
            self._location_thread.start()
            self.status = "locating"

    def _location_worker(self, lat, lng):
        try:
            if is_ios_17_plus(self.ios_version):
                asyncio.run(self._location_worker_rsd(lat, lng))
            else:
                with DvtSecureSocketProxyService(lockdown=self.lockdown) as dvt:
                    LocationSimulation(dvt).clear()
                    LocationSimulation(dvt).set(lat, lng)
                    logger.warning(f"[{self.name}] Location set {lat},{lng}")
                    while not self._terminate_location.is_set():
                        time.sleep(0.5)
        except ConnectionResetError:
            self.last_error = "Connection reset — try Stop Location to clear old connections."
            logger.error(f"[{self.name}] {self.last_error}")
        except Exception as exc:                        # noqa: BLE001
            self.last_error = str(exc)
            logger.error(f"[{self.name}] set location error: {exc}")

    async def _location_worker_rsd(self, lat, lng):
        async with RemoteServiceDiscoveryService((self.rsd_host, int(self.rsd_port))) as rsd:
            with DvtSecureSocketProxyService(rsd) as dvt:
                LocationSimulation(dvt).set(lat, lng)
                logger.warning(f"[{self.name}] Location set {lat},{lng}")
                while not self._terminate_location.is_set():
                    await asyncio.sleep(0.5)

    def _stop_location_thread(self):
        self._terminate_location.set()
        if self._location_thread and self._location_thread.is_alive():
            self._location_thread.join(timeout=3)
        self._location_thread = None

    def stop_location(self):
        """Stop this device's location thread and clear the simulated location on-device."""
        with self._lock:
            self._stop_location_thread()
            try:
                if is_ios_17_plus(self.ios_version):
                    if self.rsd_host and self.rsd_port:
                        asyncio.run(self._clear_location_rsd())
                elif self.lockdown is not None:
                    with DvtSecureSocketProxyService(lockdown=self.lockdown) as dvt:
                        LocationSimulation(dvt).clear()
                self.location = None
                self.status = "connected"
                logger.warning(f"[{self.name}] Location cleared")
                return True, None
            except Exception as exc:                    # noqa: BLE001
                self.last_error = str(exc)
                return False, str(exc)

    async def _clear_location_rsd(self):
        async with RemoteServiceDiscoveryService((self.rsd_host, int(self.rsd_port))) as rsd:
            with DvtSecureSocketProxyService(rsd) as dvt:
                LocationSimulation(dvt).clear()

    def disconnect(self):
        with self._lock:
            try:
                self.stop_location()
            except Exception:                           # noqa: BLE001
                pass
            self._terminate_tunnel.set()
            if self._tunnel_thread and self._tunnel_thread.is_alive():
                self._tunnel_thread.join(timeout=3)
            self.rsd_host = self.rsd_port = self.lockdown = None
            self.status = "idle"


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
        with self._lock:
            sess = self._sessions.pop(udid, None)
        if sess:
            sess.disconnect()

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
                sess.set_location(lat, lng)
                results[sess.udid] = "ok"
            except Exception as exc:                    # noqa: BLE001
                results[sess.udid] = f"error: {exc}"
        return results

    def stop_all(self):
        results = {}
        for sess in self.connected_sessions():
            ok, err = sess.stop_location()
            results[sess.udid] = "ok" if ok else f"error: {err}"
        return results

    def shutdown(self):
        for sess in self.sessions():
            try:
                sess.disconnect()
            except Exception:                           # noqa: BLE001
                pass
