# PERF · site delivery · measured before/after (Pass 3, Phase 3-4)

Numbers, not claims. Measured on this machine 2026-07-02. The hero is texture-dominated, so the
wins are texture shrink, brotli on the JS, and killing the third-party font requests.

## Wire bytes

| asset | before | after | how |
|-------|--------|-------|-----|
| oracles.glb (geometry + embedded foam maps) | 2,394,524 | 1,003,xxx | foam maps 1024 to 512 px + PIL crush |
| foam maps on disk (normal+rough+ao) | 2.6 MB | 664 KB | 512 px + optimize |
| three.module.js (wire, brotli) | 1,272,972 | 200,866 | brotli -q 11, served with Content-Encoding: br |
| GLTFLoader.js (wire, brotli) | 108,522 | 19,521 | brotli |
| BufferGeometryUtils.js (wire, brotli) | 31,906 | 6,002 | brotli |
| hero.js (wire, brotli) | 9,833 | 3,276 | brotli |
| fonts | Google stylesheet (render-blocking) + 2 external woff2 + 2 DNS/TLS | geist-mono 10 KB + cormorant 18 KB, self-hosted, subset to the page's 79 glyphs | pyftsubset woff2 |
| third-party requests | 3 (fonts.googleapis + fonts.gstatic x2) | 0 | self-hosted, same-origin |

**Budget check (brief: glb + all textures + decoders under 3.0 MB wire):** glb is 1.0 MB with the
foam maps embedded and no separate decoder (Draco/KTX2 not used) = **1.0 MB, well under 3.0 MB.**
Draw calls stay at **10** (budget under 30, measured via renderer.info).

## Delivery (control plane)

- `handleSiteAsset` now serves a brotli-precompressed `.br` sibling when the client sends
  `Accept-Encoding: br` (built by `render/site/precompress.sh`), sets `Accept-Ranges: bytes` on the
  glb so the loader can range-request, and returns `Cache-Control: public, max-age=31536000,
  immutable` for content-hashed filenames (`name-<8+ hex>.ext`, detected by `siteAssetHashed`),
  `max-age=86400` otherwise.
- The extension whitelist gained `woff2` (font/woff2) and `ktx2` (image/ktx2), pinned by
  `TestSiteAssetType`.
- Same-origin everything · no CORS, no asset domain. A CDN, if wanted later, sits in front of the
  origin as a proxy and inherits the immutable cache headers automatically (documented option, not
  a dependency now).

## KTX2 / gltfpack note (honest)

gltfpack and toktx could not be installed in this environment (npm package shipped no usable
binary, the brew formulae resolved wrong, the prebuilt release asset 404'd). Per the brief's
fallback clause the texture budget was hit another way: the foam maps were dropped to 512 px and
crushed, taking the glb from 2.3 MB to 1.0 MB, and the JS is brotli-served. When the tooling is
available, gltfpack meshopt + KTX2/Basis on the foam maps is the next reduction (the server already
serves `.ktx2` and immutable hashes, so only the build step is missing).

## Remaining (next turn)

- Content-hash filenames at build time (the server-side immutable path is already wired) and a
  still-first crossfade loading sequence, then measure LCP and time-to-live-scene on a throttled
  Fast-3G profile with committed waterfalls · these are Phase 4's measurement gate.
- Optional service worker (network-first for the document, cache-first for hashed assets).
