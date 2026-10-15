# All-Austria ALS height mosaic

Why `scripts/build_als_mosaic.py` handles the BEV ALS DTM/DSM the way it does, and
what it costs. Unless stated otherwise, numbers were measured on the
`N2650000E4500000` DTM/DSM tile (alpine terrain) and its east neighbour.

## Decisions

1. **BEV originals stay untouched on disk, in `D:\DEM\AUT\{DTM,DSM}`.** A float32
   EPSG:3035 VRT (`austria_{product}_3035.vrt`, next to them) makes them one seamless
   raster.
   - There is no intermediate re-compressed archive. D has room for all of it
     (DTM 264 GB + DSM 307 GB, as downloaded).
   - A first test streamed tiles into an int32-cm 3035 archive and deleted the
     originals. That rounded heights twice (archive, then mosaic), which raised the
     normal error from ~0.09° to ~0.12° mean. It also bought nothing once disk space
     wasn't the constraint.
2. **The tile creators' input is an EPSG:3857 mosaic on the exact z17 tile grid,**
   warped once from the source VRT. It lives in `data/als_mosaic/{product}/3857_z17`.
   - Every creator becomes a window read: no per-block `reproject`, and no rotated-quad
     footprint logic.
   - It uses the same bilinear warp `als_normals` does today, so the input is
     unchanged.
3. **The mosaic stores heights as int32 centimetres** (ZSTD, horizontal predictor),
   rounded once, directly from the float source. Its effect on z17 normals is below
   the 8-bit normal encoding on every statistic:

   | z17 normal error | mean | p99 | max |
   |---|---|---|---|
   | 1 cm rounding of the mosaic (DTM) | 0.087° | 0.22° | 0.39° |
   | 1 cm rounding of the mosaic (DSM) | 0.080° | 0.22° | 0.39° |
   | 8-bit normal encoding (`docs/normal_map_encoding.md`) | 0.26° | n/a | 0.55° |
   | ALS data itself (Sobel vs finite difference) | 0.77° | 3.3° | n/a |

   The rounding is independent of the encoding error, so the two add roughly in
   quadrature: the total goes from 0.26° to ~0.27° mean. These figures come from
   5 random z13 blocks per product, rounding the pipeline's own warp output.
4. **Layout:** one GeoTIFF per z10 tile (32768² px = 128×128 z17 tiles), plus a VRT
   over them (`austria_{product}_3857_z17.vrt`). The VRT unscales to float32 metres
   with nodata −9999, so readers see one seamless float raster.
   - **No overviews:** the tile creators only read full resolution.
   - **No download resume:** a rerun skips files that already exist; a crashed chunk
     only leaves a `.part` behind.

## Source data

- 55 tiles per product, each 50×50 km, float32 at 1 m, LZW + floating-point
  predictor, with internal overviews.
- Download size: **DTM 264 GB, DSM 307 GB**. The largest tile is 9.8 GB.
- **CRS:** the files carry only a `LOCAL_CS`, so the source VRT sets EPSG:3035
  explicitly.
- **Shared edges:** each tile is **50001×50001 px**. Pixel centres lie on the 50 km
  grid, so neighbouring tiles share one row/column. In the source VRT, a tile always
  contributes its top row and left column; its last row/column only where the
  south/east neighbour is absent. Every pixel therefore comes from exactly one file,
  and the VRT is gapless even while only part of Austria is downloaded. `sources`
  reports whether the shared edges are actually identical.
- **Precision:** values use full float32 resolution (step = float32 ULP, ~0.12 mm).
  They are not rounded to mm or cm. Sub-mm detail is noise, which is why lossless
  float compression stops at ~2× (see the benchmark below).

## Size

The 3857 z17 mosaic is expected to be **~140 GB (DTM) and ~180 GB (DSM)**:
- These are extrapolated from int32-cm compression ratios on alpine samples, ±20%.
- The mosaic has ~1.54× the pixels of the 1 m source, because z17 pixels are ~0.81 m
  on the ground at 47.5°N.

Measured on the two-tile DTM test: 12 z10 chunks, 6.9 GB in total.

## Compression benchmark

Measured on three alpine 2048² windows of the 1 m source. "Ratio" is relative to raw
float32. "Normal error" is the Sobel normal on the 1 m grid, compared with the
unquantized original; at z17 the errors are ~1.25× larger (see the table above).

| Storage | Max height error | DTM ratio | DSM ratio | Normal error mean / p99 |
|---|---|---|---|---|
| float LZW+P3 (BEV as shipped) | 0 | 1.75× | 1.49× | 0 |
| float ZSTD+P3 (lossless) | 0 | 2.26× | 1.98× | 0 |
| LERC_ZSTD, MAX_Z_ERROR 1 mm | 1 mm | 2.50× | 2.36× | 0.014° / 0.034° |
| int32 1 mm + ZSTD+P2 | 0.5 mm | 2.57× | 2.11× | 0.007° / 0.017° |
| **int32 1 cm + ZSTD+P2** | **5 mm** | **3.85×** | **2.92×** | **0.069° / 0.17°** |
| uint16 6 cm (global offset) | 3 cm | ~6.5× | ~4.2× | ~0.41° / ~1.0° |

How the options compare:
- **Lossless:** ZSTD matches DEFLATE's ratio at about 4× the speed. Lossless LERC
  doesn't compress float noise at all.
- **LERC vs integer quantization:** LERC is per-block integer quantization done
  automatically. At an equal quantization step, it compressed slightly worse here than
  plain integers + ZSTD + P2.
- **uint16:** it needs a ≥ 6 cm step to cover Austria's ~100–4000 m with 65535
  levels. That error is the same size as the 8-bit encoding error.

## Warp cost, and GDAL's window-dependence

What the mosaic saves per tileset:
- Per z13 block, the warp is ~0.4 s of ~3.9 s serial work (normals 1.5 s, float
  pyramid 2.0 s; PNG encoding comes on top). That is about 5–10% of generation time.
- Pre-warping saves that once per product and per creator. The main gain is
  simplicity.

GDAL's warp output depends slightly on the destination window. Warping the same block
with a 1 px vs 16 px margin differs by up to 2.8 cm in ~0.03% of pixels; with a 40 px
margin, up to 7 cm in ~0.5%.
- **What it is not:** not caused by the transform approximation (`tolerance=0` is
  bit-identical), `SOURCE_EXTRA`, the warp memory limit, threading, or nodata.
- **What it means:** a few-cm horizontal sampling shift on steep terrain, well within
  ALS horizontal accuracy.
- **Why it doesn't matter here:** `als_normals` has the same property today. The
  mosaic warps in the same 4096² windows, so it is no worse than the current pipeline.
- **How it shows up:** `verify` reports it as rare cm-level outliers, e.g. a 7 cm max
  in one block's apron ring, whose pixels come from a neighbouring warp window.

## Use in the tile creators

`tile_creators/als_normals.py` reads the mosaic VRT directly:
- It detects a source that sits on the exact `MAX_ZOOM` tile grid and window-reads
  each z13 block. On an aligned grid this is bit-identical to its warp path; anything
  else is still warped.
- `REGION_CENTER_LATLON` / `REGION_HALF_SIZE_M` limit generation to a square, currently
  40×40 km around Großglockner. Blocks without data are skipped, so the region may
  reach past what has been mosaicked so far.

## Not covered yet

Country-scale generation in `tile_creators/als_normals.py` still needs three changes:
- footprint from the chunk list instead of the VRT's bounding box
- a reworked upper-level cache (~14 GB RAM at Austria scale)
- resume support

z17 output is ~250 GB of PNG per product.
