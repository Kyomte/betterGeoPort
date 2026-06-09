# GeoPort (offline + multi-device rebuild)

An iOS location simulator — set a simulated GPS location on your own iPhone/iPad
from a map, the same mechanism Xcode's "Simulate Location" uses, for app testing.

This is a rebuild of [GeoPort by davesc63](https://github.com/davesc63/GeoPort)
(GPL-3.0) with three additions:

- **Offline maps** — tiles are served through a local cache (`~/GeoPort/tiles`).
  They cache automatically as you browse, and you can pre-download a whole
  region ("Download this area") for guaranteed offline use later.
- **Multi-device** — connect several devices at once; each gets its own movable
  pin, plus a "Set all devices here" broadcast.
- **Privacy / offline-first** — the `api.geoport.me` telemetry was removed, the
  server binds to `127.0.0.1` only, and startup never blocks on the network.

## Requirements

- macOS (Apple Silicon or Intel), Python 3.11–3.13
- Xcode command-line tools + Homebrew OpenSSL (to build `sslpsk-pmd3`)
- The iOS device with **Developer Mode** enabled

## Run (from source)

```bash
python3 -m venv .venv && source .venv/bin/activate
# build deps for the pymobiledevice3 tunnel stack:
export SDKROOT="$(xcrun --show-sdk-path)" CC=/usr/bin/clang
export CFLAGS="-I$(brew --prefix openssl@3)/include" LDFLAGS="-L$(brew --prefix openssl@3)/lib"
pip install -r requirements.txt

# iOS 17+ tunnels need root:
sudo ./run --no-browser --port 54321
# then open http://localhost:54321
```

## Wi-Fi vs USB

Wi-Fi works for devices macOS has registered as a network device (usbmux
"Network"). A device gets registered automatically once it stays on the same
Wi-Fi with "Show this device when on Wi-Fi" enabled in Finder. USB always works.
See `NOTES_IPAD_WIFI.md` on the `ipad-wifi-fix` branch for the deep dive.

## Layout

| File | Purpose |
|------|---------|
| `main.py` | Flask app: routes, device listing, lifecycle |
| `device_manager.py` | Per-device sessions: tunnels + location threads |
| `tiles.py` | Offline tile cache, area pre-download, cache management |
| `templates/map.html` | Single-page UI (vendored Leaflet, no CDNs) |
| `static/vendor/leaflet/` | Vendored Leaflet (offline) |

Based on GeoPort by davesc63 — GPL-3.0.
