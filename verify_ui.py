"""Headless UI verification for GeoPort. Loads the running server, captures
console errors + every network request, inspects the DOM, and screenshots."""
import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:54330/"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/geoport_ui.png"

console, errors, requests = [], [], []

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 820})
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: requests.append(r.url))

    page.goto(URL, wait_until="networkidle", timeout=20000)
    page.wait_for_timeout(3500)  # let tiles + device poll settle

    info = page.evaluate("""() => ({
        title: document.title,
        deviceCards: document.querySelectorAll('.device').length,
        deviceNames: [...document.querySelectorAll('.device .name')].map(n=>n.textContent),
        providers: [...document.querySelectorAll('#providerSelect option')].map(o=>o.textContent),
        tileImgs: document.querySelectorAll('img.leaflet-tile').length,
        onlineBadge: document.querySelector('#onlineBadge')?.innerText,
        cacheInfo: document.querySelector('#cacheInfo')?.textContent,
        leafletLoaded: typeof window.L !== 'undefined',
        mapPanes: document.querySelectorAll('.leaflet-map-pane').length,
    })""")

    page.screenshot(path=OUT, full_page=False)
    browser.close()

print("=== PAGE INFO ===")
for k, v in info.items():
    print(f"  {k}: {v}")

print("\n=== NETWORK REQUESTS (unique hosts) ===")
from urllib.parse import urlparse
hosts = {}
for u in requests:
    h = urlparse(u).netloc
    hosts[h] = hosts.get(h, 0) + 1
for h, n in sorted(hosts.items()):
    print(f"  {h}: {n}")
external = [u for u in requests if urlparse(u).netloc not in ("127.0.0.1:54330", "localhost:54330", "")]
print("\n=== EXTERNAL (non-localhost) REQUESTS ===")
print("  NONE — fully self-hosted" if not external else "\n".join("  "+u for u in external))

print("\n=== CONSOLE ERRORS ===")
errs = [c for c in console if c.startswith("error")] + errors
print("  none" if not errs else "\n".join("  "+e for e in errs))
print("\nSCREENSHOT:", OUT)
