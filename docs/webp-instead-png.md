# WebP instead of PNG for cosmos_snow tiles

A quick smoke test measuring how much smaller the `cosmos_snow` tiles would be if
encoded as WebP instead of PNG. Companion to
[`tile_creators/cosmos_snow.py`](../tile_creators/cosmos_snow.py), specifically
`_encode_tile`/`_decode_tile`.

## TL;DR

Switching to **lossless WebP** would save **~45%** of storage/transfer size for this
tile format, with an exact (bit-for-bit) round-trip of the underlying data - but only
if encoded with `exact=True`, not just `lossless=True`.

## Why exactness matters here

`_encode_tile` doesn't produce a normal picture: it packs a `uint16` snow-depth value
(cm) into the R (hi byte) and G (lo byte) channels, with A as a validity/transparency
flag (see `_encode_tile`/`_decode_tile` in `cosmos_snow.py`). Any lossy compression, or
any lossless mode that "helps" by discarding RGB data behind a transparent pixel, would
silently corrupt depth values on tiles adjacent to nodata.

Pillow's WebP writer has exactly that trap: `lossless=True` alone lets libwebp zero out
RGB channels wherever alpha is 0 (a legitimate optimization for normal images, since
transparent pixels don't affect how they look). Passing `exact=True` disables that and
preserves every channel byte-for-byte.

- `lossless=True` only → **did not** round-trip exactly (`np.array_equal` failed on
  fully-transparent pixels).
- `lossless=True, exact=True` → round-tripped exactly on every sampled tile.
- Regular lossy WebP (`quality=80`) compresses far more (~85% smaller) but is not
  usable at all - it would corrupt the depth values.

## Test method

Used the real, already-generated dataset for `2026-01-01`
(`data/snow_tiles/2026-01-01.db`, 21,651 tiles across zoom 0-13). For each zoom level,
sampled up to 150-300 existing tile PNGs, decoded them, re-encoded as
`WEBP, lossless=True, exact=True`, and compared byte sizes. Verified round-trip
correctness by decoding the WebP back and comparing the RGBA array to the original
with `numpy.array_equal`.

## Per-zoom sample results

| z | sampled n | avg PNG size | avg WebP size | savings |
|---|---|---|---|---|
| 13 | 300 | 9064 B | 5020 B | 44.6% |
| 12 | 300 | 9915 B | 5737 B | 42.1% |
| 10 | 200 | 15522 B | 9420 B | 39.3% |
| 6 | 6 | 5252 B | 2951 B | 43.8% |

## Whole-dataset projection

Extrapolating the per-zoom sampled averages across the actual tile counts at every
zoom level (0-13, 21,651 tiles total):

| | PNG (actual) | WebP (projected) |
|---|---|---|
| Total size | 203.7 MB | 111.5 MB |

**Estimated savings: ~45%.**

## What would need to change to adopt this

Localized to two spots:

- `_encode_tile`/`_decode_tile` in `cosmos_snow.py`: swap `format="PNG"` for
  `format="WEBP", lossless=True, exact=True` (decode side needs no change - Pillow
  auto-detects format from the bytes).
- `routes_v1.py`'s `cosmos_snow_tile` response `mimetype`, and the `.png` URL
  extension/route registration - if the extension is meant to reflect the actual
  encoding.

Not yet decided/implemented - this doc only covers the measurement.
