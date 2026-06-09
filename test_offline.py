"""Force-offline test of the tile cache: cached tiles must still serve, and
uncached tiles must fall back to the placeholder (no network call)."""
import os
import tiles

# 1) Pretend we are fully offline.
tiles.is_online = lambda: False
import main  # registers the blueprint on the Flask app
main.is_online = lambda: False

client = main.app.test_client()

# A tile we cached earlier (from the provider test). Ensure one exists.
cached_rel = None
root = tiles.CACHE_ROOT
for r, _d, files in os.walk(root):
    for f in files:
        if f.endswith(".png"):
            p = os.path.join(r, f)
            parts = p[len(root) + 1:].split(os.sep)  # provider/z/x/y.png
            if len(parts) == 4:
                cached_rel = f"/tiles/{parts[0]}/{parts[1]}/{parts[2]}/{parts[3]}"
                break
    if cached_rel:
        break

assert cached_rel, "need at least one cached tile to test (run the app online first)"

# Cached tile served from disk while offline
r1 = client.get(cached_rel)
assert r1.status_code == 200, r1.status_code
assert r1.headers.get("X-GeoPort-Tile") == "cache", r1.headers.get("X-GeoPort-Tile")
print(f"PASS: offline + cached  -> served from cache  ({cached_rel})")

# Uncached tile while offline -> placeholder, NO network
prov = cached_rel.split("/")[2]
uncached = f"/tiles/{prov}/3/7/3.png"
assert not os.path.exists(os.path.join(root, prov, "3", "7", "3.png")), "pick an uncached tile"
r2 = client.get(uncached)
assert r2.status_code == 200, r2.status_code
assert r2.headers.get("X-GeoPort-Tile") == "placeholder", r2.headers.get("X-GeoPort-Tile")
assert r2.data[:8] == b"\x89PNG\r\n\x1a\n", "placeholder should be a PNG"
print(f"PASS: offline + uncached -> neutral placeholder ({uncached})")

print("\nOFFLINE TILE BEHAVIOUR VERIFIED")
