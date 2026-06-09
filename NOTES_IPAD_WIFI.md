# iPad over Wi-Fi — investigation & how to finish it

This branch documents the unfinished bit: making a device that macOS has **not**
registered as a Wi-Fi ("Network") device work over Wi-Fi. On the test hardware,
the **iPhone worked over Wi-Fi but the iPad did not.**

## TL;DR of the blocker

The iOS 17.4+ location tunnel (`CoreDeviceTunnelProxy.start_tcp_tunnel()`) only
works over a **usbmux** lockdown — USB, or a Wi-Fi device that macOS has
*promoted* to a usbmux `Network` connection. It does **not** work over a raw
`create_using_tcp(ip, udid)` lockdown.

- `create_using_usbmux(udid, connection_type="Network")`
  - iPhone → **works** (macOS promoted it) → tunnel works → Wi-Fi works ✅
  - iPad → **`DeviceNotFoundError`** (never promoted) ❌
- Reaching the iPad directly with `create_using_tcp(ip, udid)` succeeds for
  *lockdown* calls (`short_info`, `developer_mode_status`) but the tunnel
  handshake then dies with:
  `StreamError: Error in path (parsing) -> magic / stream read less than
  specified amount, expected 8, found 0`

So "discover the IP and connect over TCP" is a dead end for the tunnel.

## Evidence collected

| Check | iPhone | iPad |
|---|---|---|
| usbmux `Network` promotion | YES | NO (`DeviceNotFoundError`) |
| Reachable via Bonjour `_apple-mobdev2` + TCP lockdown | yes (192.168.1.119) | yes, *intermittently* (192.168.1.197) |
| `route get <ip>` interface | `en0` (Wi-Fi) | `en8` (USB-ethernet) when cabled; `en0` only when it briefly joined Wi-Fi |
| Tunnel over usbmux-Network | works | n/a (not promoted) |
| Tunnel over raw TCP lockdown | — | `StreamError` |

The iPad kept dropping off Wi-Fi — it was at **25% battery** (Low Power Mode
sleeps Wi-Fi when the screen is off). Even when briefly on real Wi-Fi at
`192.168.1.197`, macOS still didn't promote it to a usbmux Network device.

`get_remote_pairing_tunnel_services()` (the iOS 17.0–17.3 remote-pairing path)
returned **0 services** — the device doesn't advertise `_remotepairing` here.

## What "promotion" needs

macOS/usbmuxd registers a paired device as a Network device when it stays on the
same Wi-Fi with **"Show this device when on Wi-Fi"** enabled in Finder. Get the
iPad promoted, then `create_using_usbmux(connection_type="Network")` starts
working and the existing code path in `device_manager._tcp_tunnel()` just works —
no code change required.

Things to try to force promotion:
1. Charge the iPad (wall charger), Low Power Mode **off**, keep it unlocked on
   the same Wi-Fi for several minutes.
2. Finder → iPad → tick "Show this iPad when on Wi-Fi" → **Apply/Sync once**.
3. Unpair + re-pair the iPad (Finder), so the pairing record gets the network
   escrow bag.
4. `sudo pkill usbmuxd` (it relaunches) to force re-discovery, then re-check
   `create_using_usbmux(udid, connection_type="Network")`.

## If you want to try a real Wi-Fi tunnel without usbmux promotion

The likely correct path is the CoreDevice **RemoteServiceDiscovery over the
network** rather than a plain lockdown TCP. Worth investigating:
- How `xcrun devicectl` / Xcode connect to a device wirelessly (they don't need
  usbmux promotion). Mirror that handshake.
- `pymobiledevice3.remote.tunnel_service.create_core_device_tunnel_service_using_remotepairing(udid, host, port)`
  — needs the device to advertise a remote-pairing service over Bonjour; it
  didn't on this network. Figure out what makes a device advertise it.

## Reference: the Bonjour + TCP discovery I prototyped (detects, can't tunnel)

```python
# Browse _apple-mobdev2 for IPs, then match a UDID by probing each with a TCP
# lockdown. Good for *detecting* a Wi-Fi device; NOT sufficient for the tunnel.
import asyncio
from pymobiledevice3.bonjour import browse_mobdev2
from pymobiledevice3.lockdown import create_using_tcp

def discover_wifi_ip(udid, timeout=5):
    answers = asyncio.run(browse_mobdev2(timeout=timeout))
    ips = [ip for a in answers for ip in (getattr(a, "ips", None) or [])
           if ":" not in ip and not ip.startswith("169.254")]
    for ip in dict.fromkeys(ips):
        try:
            ld = create_using_tcp(hostname=ip, identifier=udid)
            if ld.short_info.get("UniqueDeviceID") == udid:
                return ip          # reachable, but tunnel will StreamError
        except Exception:
            continue
    return None
```

## Where to wire a fix

- `device_manager.py` → `device_lockdown()` (transport → lockdown) and
  `_tcp_tunnel()` (the tunnel that needs a usbmux connection).
- `main.py` → `list_devices_route()` (surface the device) and
  `connect_device()` (the connect flow).

`main` branch deliberately keeps behaviour honest: Wi-Fi is only offered for
devices macOS actually exposes over Wi-Fi, so users don't hit the StreamError.
