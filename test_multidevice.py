"""
Hardware-free test of the multi-device session model.

Mocks the device-side worker so we can prove the concurrency/state model:
two devices hold independent simulated locations, controlling one does not
disturb the other, and broadcast (set_all/stop_all) hits every device.
"""
import time
import threading
import device_manager
from device_manager import DeviceManager, DeviceSession

# --- Replace the real pymobiledevice3 worker with a thread that just waits --- #
_active = {}            # udid -> bool, True while that device's loc thread runs


def fake_location_worker(self, lat, lng, terminate, done):
    _active[self.udid] = True
    try:
        self._location_ok(lat, lng, terminate, done)    # device confirmed
        while not terminate.is_set():
            time.sleep(0.02)
    finally:
        _active[self.udid] = False


_cleared = []           # udids the device was told to drop the simulated location for


def fake_clear_on_device(self):
    _cleared.append(self.udid)


DeviceSession._location_worker = fake_location_worker
DeviceSession._clear_on_device = fake_clear_on_device


def make_connected(mgr, udid, name):
    s = mgr.get_or_create(udid, "USB", "26.0", name=name, device_class="iPhone")
    s.rsd_host, s.rsd_port = "127.0.0.1", "12345"   # pretend tunnel is up
    return s


def main():
    mgr = DeviceManager()
    a = make_connected(mgr, "UDID-A", "iPhone-A")
    b = make_connected(mgr, "UDID-B", "iPad-B")

    assert len(mgr.connected_sessions()) == 2, "both devices should be connected"

    # 1) independent set
    a.set_location(40.0, -3.0)
    b.set_location(35.0, 139.0)
    time.sleep(0.1)
    assert _active["UDID-A"] and _active["UDID-B"], "both location threads should run"
    assert a.location == (40.0, -3.0) and b.location == (35.0, 139.0), "independent coords"
    assert a._location_thread is not b._location_thread, "separate threads"
    print("PASS: two devices hold independent locations simultaneously")

    # 2) stopping A must not affect B
    a.stop_location()
    time.sleep(0.1)
    assert not _active["UDID-A"], "A stopped"
    assert _active["UDID-B"], "B still running after A stopped"
    assert a.location is None and b.location == (35.0, 139.0)
    print("PASS: stopping one device leaves the other running")

    # 3) moving B updates only B (new thread, old one ends)
    old_thread = b._location_thread
    b.set_location(48.85, 2.35)
    time.sleep(0.1)
    assert b.location == (48.85, 2.35) and _active["UDID-B"]
    assert b._location_thread is not old_thread, "re-set spins a fresh thread"
    print("PASS: re-setting a device's location replaces only its own thread")

    # 4) broadcast set_all hits every connected device
    mgr.set_all(1.23, 4.56)
    time.sleep(0.1)
    assert a.location == (1.23, 4.56) and b.location == (1.23, 4.56)
    assert _active["UDID-A"] and _active["UDID-B"]
    print("PASS: set_all broadcasts one location to every device")

    # 5) stop_all clears everything
    mgr.stop_all()
    time.sleep(0.1)
    assert not _active["UDID-A"] and not _active["UDID-B"]
    assert a.location is None and b.location is None
    print("PASS: stop_all clears every device")

    # 6) a failed set must never claim to be simulating
    def failing_worker(self, lat, lng, terminate, done):
        time.sleep(0.05)
        self._location_failed("device timed out", terminate, done)

    real_worker = DeviceSession._location_worker
    DeviceSession._location_worker = failing_worker
    try:
        ok, err = a.set_location(9.0, 9.0, wait=2)
        assert ok is False and err == "device timed out", (ok, err)
        assert a.status == "error" and a.location is None
        ok, err = b.set_location(9.0, 9.0)               # no wait: pending
        assert ok is None and b.status == "setting" and b.location is None
        time.sleep(0.2)
        assert b.status == "error" and b.location is None
    finally:
        DeviceSession._location_worker = real_worker
    print("PASS: failed sets report 'error', never 'simulating'")

    # 7) a second Connect while one is in progress is refused
    c = mgr.get_or_create("UDID-C", "Network", "27.0", name="iPhone-C")
    assert c.begin_connect() and c.status == "connecting"
    assert not c.begin_connect(), "double click must not start a second connect"
    c.status = "error"
    assert c.begin_connect(), "can retry after a failure"
    print("PASS: concurrent connects to one device are refused")

    # 8) a dropped tunnel is detected (cable pulled / Wi-Fi lost)
    import asyncio
    from types import SimpleNamespace
    from device_manager import tunnel_alive

    async def check():
        running = asyncio.create_task(asyncio.sleep(60))
        finished = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0.01)
        open_writer = SimpleNamespace(is_closing=lambda: False)
        closed_writer = SimpleNamespace(is_closing=lambda: True)
        ok = tunnel_alive(SimpleNamespace(_tun_read_task=running, _sock_read_task=running, _writer=open_writer))
        tun_dead = tunnel_alive(SimpleNamespace(_tun_read_task=finished, _sock_read_task=running, _writer=open_writer))
        sock_closed = tunnel_alive(SimpleNamespace(_tun_read_task=running, _sock_read_task=running, _writer=closed_writer))
        running.cancel()
        return ok, tun_dead, sock_closed

    assert asyncio.run(check()) == (True, False, False)
    print("PASS: dropped tunnels are detected")

    # 9) "Real location" on a device whose tunnel dropped mid-simulation
    #    reconnects, then clears it (the device keeps the fake spot until then)
    connects = []

    def fake_connect(self):
        connects.append(self.udid)
        self.rsd_host, self.rsd_port = "127.0.0.1", "12345"
        self.status = "connected"
        return True, None

    def failing_connect(self):
        connects.append(self.udid)
        self.status, self.last_error = "error", "device not found"
        return False, "device not found"

    real_connect, real_mount = DeviceSession.connect, DeviceSession.mount_developer_image
    DeviceSession.connect = fake_connect
    DeviceSession.mount_developer_image = lambda self: None
    try:
        a.set_location(10.0, 20.0, wait=2)
        a.rsd_host = a.rsd_port = None                  # the tunnel died
        a.status = "error"
        _cleared.clear()
        ok, err = a.stop_location()
        assert ok and err is None, (ok, err)
        assert connects == ["UDID-A"] and _cleared == ["UDID-A"]
        assert a.location is None and a.status == "connected"
        print("PASS: Real location reconnects a dropped device, then clears it")

        # 10) a clear that fails over a stale tunnel is retried on a fresh one
        attempts = []

        def flaky_clear(self):
            attempts.append(self.udid)
            if len(attempts) == 1:
                raise TimeoutError("stale tunnel")
            _cleared.append(self.udid)

        DeviceSession._clear_on_device = flaky_clear
        connects.clear(), _cleared.clear()
        b.set_location(1.0, 2.0, wait=2)
        ok, err = b.stop_location()
        DeviceSession._clear_on_device = fake_clear_on_device
        assert ok and len(attempts) == 2 and connects == ["UDID-B"] and _cleared == ["UDID-B"]
        print("PASS: a failed clear is retried once over a fresh connection")

        # 11) an unreachable device is reported as still simulating, never as reset
        DeviceSession.connect = failing_connect
        a.set_location(5.0, 6.0, wait=2)
        a.rsd_host = a.rsd_port = None
        ok, err = a.stop_location()
        assert not ok and "real location" in err and "device not found" in err, err
        assert a.status == "error" and a.location == (5.0, 6.0)
        print("PASS: an unreachable device reports the reset failed")

        # 12) "All devices back" also reaches a device whose connection dropped
        DeviceSession.connect = fake_connect
        connects.clear(), _cleared.clear()
        results = mgr.stop_all()
        assert results == {"UDID-A": "ok", "UDID-B": "ok"}, results    # C never connected
        assert connects == ["UDID-A"] and sorted(_cleared) == ["UDID-A", "UDID-B"]
        assert a.location is None
        print("PASS: stop_all also resets devices that lost their connection")

        # 13) Disconnect doesn't reconnect, and says when it left a device simulating
        mgr2 = DeviceManager()
        lost = make_connected(mgr2, "UDID-L", "iPhone-L")
        lost.set_location(3.0, 3.0, wait=2)
        lost.rsd_host = lost.rsd_port = None
        connects.clear()
        ok, err = mgr2.remove("UDID-L")
        assert not ok and "real location" in err and connects == [], (ok, err)
        mgr2.get_or_create("UDID-N", "USB", "26.0")    # never connected or set
        assert mgr2.remove("UDID-N") == (True, None)
        print("PASS: Disconnect reports a device it couldn't put back")

        # 14) quitting resets every device in parallel, and in bounded time
        def slow_clear(self):
            if self.udid == "UDID-Z":
                time.sleep(30)                          # an unresponsive device
            _cleared.append(self.udid)

        DeviceSession._clear_on_device = slow_clear
        for u in ("UDID-X", "UDID-Y", "UDID-Z"):
            make_connected(mgr2, u, u).set_location(7.0, 7.0, wait=2)
        connects.clear(), _cleared.clear()
        started = time.monotonic()
        mgr2.shutdown(timeout=1)
        assert time.monotonic() - started < 2, "shutdown must not wait on a hung device"
        assert sorted(_cleared) == ["UDID-X", "UDID-Y"] and connects == []
        print("PASS: quitting resets all devices without hanging on one")
    finally:
        DeviceSession.connect, DeviceSession.mount_developer_image = real_connect, real_mount
        DeviceSession._clear_on_device = fake_clear_on_device

    # 15) the reset waits for the device's reply, but a silent device isn't an error
    import socket
    from device_manager import clear_simulated_location, LocationSimulation
    calls = []

    class FakeChannel:
        def __init__(self, silent):
            self.silent = silent

        def stopLocationSimulation(self):
            calls.append("stop")

        def receive_plist(self):
            calls.append("ack")
            if self.silent:
                raise socket.timeout("timed out")

    class FakeDvt:
        def __init__(self, silent):
            self.channel = FakeChannel(silent)
            self.service = SimpleNamespace(socket=SimpleNamespace(
                settimeout=lambda t: calls.append(("timeout", t))))

        def make_channel(self, identifier):
            calls.append(identifier)
            return self.channel

    clear_simulated_location(FakeDvt(silent=False))
    assert calls == [LocationSimulation.IDENTIFIER, "stop", ("timeout", 5), "ack"], calls
    clear_simulated_location(FakeDvt(silent=True))
    print("PASS: the reset waits for the device to acknowledge it")

    # 16) Wi-Fi without the OS usbmux: paired devices found by their announcement
    import os
    import plistlib
    import tempfile
    records, home = tempfile.mkdtemp(), tempfile.mkdtemp()
    device_manager.KNOWN_HOSTS_FILE = os.path.join(home, "GeoPort", "known_hosts.json")
    udid = "00008150-000C6D3C3C78C01C"
    for name, rec in ((f"{udid}.plist", {"WiFiMACAddress": "D0:B3:24:10:CD:52", "HostPrivateKey": b"k"}),
                      ("SystemConfiguration.plist", {"SystemBUID": "x"}),
                      ("00008030-AAAA.plist", {"WiFiMACAddress": "aa:bb:cc:dd:ee:ff"})):   # no keys
        with open(os.path.join(records, name), "wb") as f:
            plistlib.dump(rec, f)
    real = (device_manager.PAIR_RECORD_DIRS, device_manager.get_home_folder,
            device_manager.browse_wifi_sync, device_manager.create_using_usbmux,
            device_manager.create_using_tcp)
    browsed, tcp = [], []

    async def fake_browse(timeout):
        browsed.append(timeout)
        return {"76:b2:be:9f:76:93@fe80::1-supportsRP-26._apple-mobdev2._tcp.local.":
                    ["192.168.1.206"],                          # paired elsewhere, answers first
                "d0:b3:24:10:cd:52@fe80::d2b3:24ff:fe10:cd52-supportsRP-26._apple-mobdev2._tcp.local.":
                    ["192.168.1.207"]}

    def no_usbmux(*a, **k):
        raise ConnectionRefusedError("not listed by usbmux")

    heartbeat = []      # what the device's heartbeat service was sent

    class FakeHeartbeatService:
        def __init__(self):
            self.socket = self
            self.closed = threading.Event()
            self.messages = [{"Command": "Marco", "Interval": 10, "SupportsSleepyTime": True}]

        def settimeout(self, seconds):
            heartbeat.append(("timeout", seconds))

        def recv_plist(self):
            if self.messages:
                return self.messages.pop(0)
            self.closed.wait(5)                         # the next one is 10 s away
            raise OSError("closed")

        def send_plist(self, message):
            heartbeat.append(message["Command"])

        def close(self):
            self.closed.set()

    class FakeWifiLockdown:
        def start_lockdown_service(self, name):
            heartbeat.append(name)
            return FakeHeartbeatService()

        def close(self):
            pass

    def fake_tcp(hostname, identifier, autopair, pair_record):
        tcp.append((hostname, identifier, autopair, pair_record["WiFiMACAddress"]))
        if hostname == "asleep":
            raise TimeoutError("timed out")
        return FakeWifiLockdown()

    device_manager.PAIR_RECORD_DIRS = [records]
    device_manager.get_home_folder = lambda: home
    device_manager.browse_wifi_sync = fake_browse
    device_manager.create_using_usbmux = no_usbmux
    device_manager.create_using_tcp = fake_tcp
    device_manager.wifi_hosts.clear()
    try:
        assert device_manager.paired_wifi_macs().keys() == {"d0:b3:24:10:cd:52"}
        try:
            device_manager.device_lockdown(udid, "Network")
            raise AssertionError("not found yet, must say it isn't on Wi-Fi")
        except RuntimeError as exc:
            assert str(exc) == device_manager._WIFI_UNAVAILABLE
        assert device_manager.discover_wifi() == {udid: "192.168.1.207"}
        assert isinstance(device_manager.device_lockdown(udid, "Network"), FakeWifiLockdown)
        assert tcp == [("192.168.1.207", udid, False, "D0:B3:24:10:CD:52")] * 2, \
            "one connection for the heartbeat, one for the caller"
        assert heartbeat == [device_manager.HEARTBEAT_SERVICE, ("timeout", 30), "Polo"], \
            "the device refuses services over Wi-Fi without a heartbeat"
        device_manager.device_lockdown(udid, "Network")
        assert len(tcp) == 3 and len(heartbeat) == 3, "one heartbeat per device, kept running"
        hb = device_manager._heartbeats[udid]
        DeviceSession(udid, "Network", "27.0").disconnect()
        hb._thread.join(2)
        assert not hb._thread.is_alive() and udid not in device_manager._heartbeats, \
            "Disconnect stops the heartbeat"
        tcp.clear()
        browsed.clear()
        assert device_manager.discover_wifi(exclude={udid}) == {} and not browsed, \
            "no browsing when usbmux already lists every paired device on Wi-Fi"
        device_manager.wifi_hosts[udid] = ("asleep", device_manager.wifi_hosts[udid][1])
        started = time.monotonic()
        try:
            device_manager.device_lockdown(udid, "Network")
            raise AssertionError("an asleep device must give the asleep hint")
        except RuntimeError as exc:
            assert str(exc) == device_manager.WIFI_ASLEEP
        assert time.monotonic() - started < 1, "a heartbeat that can't start doesn't hold things up"
    finally:
        (device_manager.PAIR_RECORD_DIRS, device_manager.get_home_folder,
         device_manager.browse_wifi_sync, device_manager.create_using_usbmux,
         device_manager.create_using_tcp) = real
        device_manager.wifi_hosts.clear()
        device_manager.stop_wifi_heartbeat(udid)
    print("PASS: paired devices on Wi-Fi are found and reached without the OS usbmux")

    # 16a) a paired device announcing a private Wi-Fi address (not the MAC in
    #      its pair record) is found by the pair record it accepts
    asked, closed = [], []
    ipad = "00008120-00084DD634880032"                  # listed on Wi-Fi by the OS usbmux
    with open(os.path.join(records, f"{ipad}.plist"), "wb") as f:
        plistlib.dump({"WiFiMACAddress": "F4:B5:99:08:82:74", "HostPrivateKey": b"k"}, f)

    class FakeLockdown:
        def __init__(self, hostname, identifier, pair_record):
            self.hostname = hostname
            self.paired = hostname == "192.168.1.223" and pair_record["WiFiMACAddress"] == "D0:B3:24:10:CD:52"
            self.udid = identifier if self.paired else None

        def close(self):
            closed.append(self.hostname)

    async def private_browse(timeout):
        return {"c2:1d:1e:73:5b:6b@fe80::c01d:1eff:fe73:5b6b-supportsRP-26._apple-mobdev2._tcp.local.":
                    ["192.168.1.223"],                          # the iPhone, private address
                "76:b2:be:9f:76:93@fe80::1-supportsRP-26._apple-mobdev2._tcp.local.":
                    ["192.168.1.206"],                          # someone else's
                "aa:aa:aa:aa:aa:aa@fe80::2-supportsRP-26._apple-mobdev2._tcp.local.":
                    ["192.168.1.250"],                          # not answering
                "f4:b5:99:08:82:74@fe80::f6b5:99ff:fe08:8274-supportsRP-24._apple-mobdev2._tcp.local.":
                    ["192.168.1.234"]}                          # the iPad, excluded

    def probe_tcp(hostname, identifier, autopair, pair_record):
        asked.append((hostname, identifier, autopair))
        if hostname == "192.168.1.250":
            raise TimeoutError("timed out")
        return FakeLockdown(hostname, identifier, pair_record)

    real = (device_manager.PAIR_RECORD_DIRS, device_manager.get_home_folder,
            device_manager.browse_wifi_sync, device_manager.create_using_tcp)
    device_manager.PAIR_RECORD_DIRS = [records]
    device_manager.get_home_folder = lambda: home
    device_manager.browse_wifi_sync = private_browse
    device_manager.create_using_tcp = probe_tcp
    try:
        assert device_manager.discover_wifi(exclude={ipad}) == {udid: "192.168.1.223"}
        assert sorted(asked) == [("192.168.1.206", udid, False), ("192.168.1.223", udid, False),
                                 ("192.168.1.250", udid, False)], \
            "asks unknown devices only (not the excluded iPad), and never asks to pair"
        assert sorted(closed) == ["192.168.1.206", "192.168.1.223"]
        assert device_manager.wifi_hosts[udid][0] == "192.168.1.223"
        asked.clear()
        assert device_manager.discover_wifi(exclude={ipad}) == {udid: "192.168.1.223"} and not asked, \
            "a private address already identified isn't asked again"
    finally:
        (device_manager.PAIR_RECORD_DIRS, device_manager.get_home_folder,
         device_manager.browse_wifi_sync, device_manager.create_using_tcp) = real
        device_manager.wifi_hosts.clear()
        device_manager.wifi_aliases.clear()
    print("PASS: a paired device using a private Wi-Fi address is found by its pair record")

    # 16c) a paired device Bonjour can't see is looked for where it was last
    #      reached, on an iPhone's Personal Hotspot, or at an address typed in
    there, probed = {}, []                              # ip -> UDID of what's at that address

    class Answering:
        def __init__(self, hostname, identifier):
            self.paired = there.get(hostname) == identifier
            self.udid = identifier if self.paired else None

        def close(self):
            pass

    def probing_tcp(hostname, identifier, autopair, pair_record):
        probed.append(hostname)
        if hostname not in there:
            raise TimeoutError("timed out")
        assert autopair is False, "never asks to pair"
        return Answering(hostname, identifier)

    async def no_bonjour(timeout):
        return {}

    async def broken_bonjour(timeout):
        raise OSError("multicast blocked")

    real = (device_manager.PAIR_RECORD_DIRS, device_manager.get_home_folder,
            device_manager.browse_wifi_sync, device_manager.create_using_tcp,
            device_manager.local_ipv4_addresses)
    device_manager.PAIR_RECORD_DIRS = [records]
    device_manager.get_home_folder = lambda: home
    device_manager.browse_wifi_sync = no_bonjour
    device_manager.create_using_tcp = probing_tcp
    device_manager.local_ipv4_addresses = lambda: ["192.168.1.5"]
    known = device_manager.KNOWN_HOSTS_FILE
    try:
        if os.path.exists(known):
            os.remove(known)
        assert device_manager.discover_wifi(exclude={ipad}) == {} and not probed, \
            "nothing known and no hotspot: nowhere to look"
        there["192.168.1.223"] = udid
        assert device_manager.add_manual_host("192.168.1.99") is None and not device_manager.wifi_hosts, \
            "an address where no paired device answers adds nothing"
        assert device_manager.load_known_hosts() == {}
        assert device_manager.add_manual_host("192.168.1.223") == udid
        assert device_manager.wifi_hosts[udid][0] == "192.168.1.223"
        assert device_manager.load_known_hosts() == {udid: "192.168.1.223"}, "remembered across restarts"
        device_manager.wifi_hosts.clear()
        probed.clear()
        assert device_manager.discover_wifi(exclude={ipad}) == {udid: "192.168.1.223"}
        assert probed == ["192.168.1.223"], "asks the remembered address, not the whole network"
        device_manager.wifi_hosts.clear()
        device_manager.browse_wifi_sync = broken_bonjour
        assert device_manager.discover_wifi(exclude={ipad}) == {udid: "192.168.1.223"}, \
            "a failed Bonjour browse doesn't stop the known addresses being asked"
        # on an iPhone's Personal Hotspot (172.20.10.0/28), the iPad joined to it
        os.remove(known)
        device_manager.wifi_hosts.clear()
        there.clear()
        there["172.20.10.7"] = ipad
        probed.clear()
        device_manager.local_ipv4_addresses = lambda: ["172.20.10.2"]
        assert device_manager.discover_wifi(exclude={udid}) == {ipad: "172.20.10.7"}
        assert set(probed) == {f"172.20.10.{i}" for i in range(1, 15)} - {"172.20.10.2"}, \
            "asks everything else on the hotspot, including the iPhone itself"
        assert device_manager.load_known_hosts() == {ipad: "172.20.10.7"}
    finally:
        (device_manager.PAIR_RECORD_DIRS, device_manager.get_home_folder,
         device_manager.browse_wifi_sync, device_manager.create_using_tcp,
         device_manager.local_ipv4_addresses) = real
        device_manager.wifi_hosts.clear()
        device_manager.wifi_aliases.clear()
    print("PASS: paired devices are found at remembered, hotspot and hand-typed addresses without Bonjour")

    # 16b) the browse keeps every device announcing, not only the first one
    #      (pymobiledevice3's browse_mobdev2 did, so another iPhone hid the iPad)
    import ipaddress
    from zeroconf import DNSQuestionType, ServiceStateChange

    class FakeZeroconf:
        zeroconf = "zc"

        async def async_close(self):
            pass

    class FakeBrowser:
        def __init__(self, zc, types, handlers, question_type=None):
            assert question_type is DNSQuestionType.QM, "unicast answers didn't arrive on Windows"
            for name, change in (("other@fe80::1._apple-mobdev2._tcp.local.", ServiceStateChange.Added),
                                 ("ipad@fe80::2._apple-mobdev2._tcp.local.", ServiceStateChange.Added),
                                 ("ipad@fe80::2._apple-mobdev2._tcp.local.", ServiceStateChange.Updated),
                                 ("gone@fe80::3._apple-mobdev2._tcp.local.", ServiceStateChange.Removed)):
                handlers[0](zeroconf=zc, service_type=types[0], name=name, state_change=change)

        async def async_cancel(self):
            pass

    class FakeInfo:
        def __init__(self, service_type, name):
            self.name = name

        async def async_request(self, zc, timeout, question_type=None):
            assert question_type is DNSQuestionType.QM
            return True

        def ip_addresses_by_version(self, version):
            return [ipaddress.ip_address("192.168.1.206" if self.name.startswith("other")
                                         else "192.168.1.234")]

    real = (device_manager.AsyncZeroconf, device_manager.AsyncServiceBrowser,
            device_manager.AsyncServiceInfo)
    (device_manager.AsyncZeroconf, device_manager.AsyncServiceBrowser,
     device_manager.AsyncServiceInfo) = FakeZeroconf, FakeBrowser, FakeInfo
    try:
        assert asyncio.run(device_manager.browse_wifi_sync(0)) == {
            "other@fe80::1._apple-mobdev2._tcp.local.": ["192.168.1.206"],
            "ipad@fe80::2._apple-mobdev2._tcp.local.": ["192.168.1.234"]}
    finally:
        (device_manager.AsyncZeroconf, device_manager.AsyncServiceBrowser,
         device_manager.AsyncServiceInfo) = real
    print("PASS: Wi-Fi discovery keeps every device on the network, not just the first")

    # 17) a device that stops answering while being prepared fails the check
    #     instead of hanging it (pymobiledevice3's sockets have no timeout)
    release = threading.Event()

    def silent_device(*a, **k):
        release.wait()
        raise ConnectionError("gave up")

    real = (device_manager.DEVICE_ANSWER_TIMEOUT, device_manager.device_lockdown)
    device_manager.DEVICE_ANSWER_TIMEOUT = 0.2
    device_manager.device_lockdown = silent_device
    try:
        assert device_manager.device_call(lambda x: x * 2, 21) == 42
        try:
            device_manager.device_call(lambda: 1 / 0)
            raise AssertionError("the call's own errors must come through")
        except ZeroDivisionError:
            pass
        d = mgr.get_or_create("UDID-D", "USB", "26.5", name="iPad-D")
        started = time.monotonic()
        try:
            d.mount_developer_image()
            raise AssertionError("a silent device must not pass the image check")
        except device_manager.DeviceNotAnswering as exc:
            assert str(exc) == device_manager.NOT_ANSWERING
        assert time.monotonic() - started < 1, "must give up after the time limit"
    finally:
        device_manager.DEVICE_ANSWER_TIMEOUT, device_manager.device_lockdown = real
        release.set()
    print("PASS: a device that stops answering fails the image check instead of hanging")

    print("\nALL MULTI-DEVICE LOGIC TESTS PASSED")


if __name__ == "__main__":
    main()
