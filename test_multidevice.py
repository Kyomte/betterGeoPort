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


def fake_location_worker(self, lat, lng):
    _active[self.udid] = True
    try:
        while not self._terminate_location.is_set():
            time.sleep(0.02)
    finally:
        _active[self.udid] = False


def fake_clear(self):
    return None


DeviceSession._location_worker = fake_location_worker
DeviceSession._clear_location_rsd = fake_clear  # not awaited in this path


def fake_stop_location(self):
    # mirror real stop_location but skip the device clear call
    with self._lock:
        self._stop_location_thread()
        self.location = None
        self.status = "connected"
        return True, None


DeviceSession.stop_location = fake_stop_location


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

    print("\nALL MULTI-DEVICE LOGIC TESTS PASSED")


if __name__ == "__main__":
    main()
