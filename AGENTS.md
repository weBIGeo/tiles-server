# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

weBIGeo Tiles Server — a Flask app that processes and serves geo tiles (weather, snow,
other overlays) for the weBIGeo project. It is currently a **skeleton**: only a status
endpoint, a landing page, and a self-generating debug tileset exist. Tile
processing/serving logic is added incrementally. It shares its scaffolding (server setup,
logging, notifications, DB plumbing) with the sibling
[weBIGeo Cloud Server](https://github.com/weBIGeo/clouds-server) — when adding generic
infrastructure, check whether the equivalent already exists there before inventing a new
pattern.

## Setup & running

```bash
pip install -r requirements.txt
cp config.example.py config.py   # then edit config.py for your deployment
python server.py
```

`config.py` is git-ignored (secrets, local paths); `config.example.py` is the documented
template and single source of truth for available settings — when adding a new config
value, add it to `config.example.py` with a comment, not just to `config.py`.

There is no test suite, linter, or build step configured yet.

**Do not run `python server.py` (or otherwise start/exercise the running server)
yourself.** The user always runs and verifies the server — including real downloads
(e.g. S3) and long-running tile generation, which they want to watch and control
directly. Static checks (`python -m py_compile ...`) and reading logs the user shares are
fine; starting the process, hitting its endpoints, or killing it are not.

## Architecture

- **server.py** — Flask entrypoint. Registers `routes_v1.bp`, serves `docs/index.html` at
  `/`, initializes logging/db/debug_ortho/notify on startup, then serves via `waitress`
  (not the dev server — the tile client fires many concurrent per-quad requests, so the
  default 4 threads causes queuing/timeouts; see `config.threads`).
- **routes_v1.py** — all `/v1/...` HTTP routes (Blueprint); new endpoints go here.
  `server.py` also exposes an unversioned `/status` alias delegating to `routes_v1.status()`.
- **config.py / config.example.py** — the only configuration mechanism (plain Python
  module, not env vars or a file format). Modules read `import config; config.some_setting`.
- **db.py** — single shared SQLite connection (`check_same_thread=False` + a
  `threading.Lock`) for the main tiles DB. No tables yet; schema is added here as tile
  processing is built out.
- **const.py** — shared constants that aren't deployment config. Currently `BBOXES`,
  named WGS84 bounding boxes (Austria, Vienna, Schneeberg) as `(w, s, e, n)`.
- **util/tile_db.py** — `TileDb`, a thread-safe SQLite-backed tile cache shared by
  `tile_creators/*` (separate from `db.py`, since each tile source has its own cache
  file). Owns its own connection, lock, and commit-batching counter; opens with
  `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL`; creates the standard
  `tiles(z, x, y, data)` table. A tile source never touches `sqlite3` directly — it calls
  `get_tile`/`save_tile`/`tile_exists`/`commit`.
- **log_config.py** — custom colored logging formatter matching weBIGeo projects (mirrors
  a C++ formatter linked in the module). Sets up console + rotating file handlers and
  per-logger level overrides from `config.log_level_overrides`.
- **notify.py** — fire-and-forget notifications (SMTP email, ntfy.sh push) for
  operational events like server start. Both channels no-op silently if unconfigured.
- **processes.py** — generic in-memory registry for long-running background processes
  (`start`/`update`/`finish`/`fail`, keyed by an arbitrary string). `/v1/processes`
  exposes a snapshot (`running` plus `done`/`error` for `FINISHED_RETENTION_SECONDS`),
  and `docs/index.html` polls it for live progress bars. Prefer reporting into it over
  ad-hoc INFO-level progress logging when adding a long-running job.
- **tile_creators/** — package for tile-source modules. Each owns its own dataset and,
  unlike the rest of the app, its own tunables as module-level constants (implementation
  details of one source, not deployment config).
  - **tile_creators/als_normals.py** — surface-normal tiles from a BEV ALS height raster.
    Deliberately agnostic to DTM vs DSM: it processes whatever raster `TILE_SOURCE` points
    at, and `DB_PATH` is derived from that filename so the two products can't interleave
    in one db. Intended source is the `scripts/build_als_mosaic.py` EPSG:3857 z17 mosaic
    VRT: a source on the exact `MAX_ZOOM` tile grid is window-read per block, anything
    else (e.g. a single EPSG:3035 BEV tile) is warped per block — same result, slower.
    Coverage comes from the raster's bounds, optionally limited to a square around
    `REGION_CENTER_LATLON` (`REGION_HALF_SIZE_M`; currently 40×40 km around Großglockner;
    `None` = everything); blocks without source data are skipped. Since there is one db
    per source and an existing db skips generation, changing the region means deleting the
    db. Per tile: window read / reproject → Web Mercator altitude correction (per
    destination row) → 3×3 gradient → `util/encoding.py` → RGB PNG. `init()` starts a
    background daemon thread whenever `TILE_SOURCE` exists (like `debug_ortho`); **no
    resume**, every boot regenerates every tile. Tiles are servable as they land —
    `/v1/als-normals/...` returns whatever is in the db now, 503 per missing tile.
    Deliberate divergence from `cosmos_snow.py`: pyramid parents are reduced from the
    exact **float** normal field block-by-block (`BLOCK_ZOOM` bounds peak memory), never
    by re-decoding already-quantized children, so quantization error doesn't compound.
    Format: `docs/normal_map_encoding.md`; `MAX_ZOOM` rationale:
    `docs/max_zoomlevel_normal_height_maps.md`.
  - **tile_creators/debug_ortho.py** — self-contained synthetic/debug tile source:
    downloads real basemap.at orthophoto tiles over Vienna's 1st district into its own
    SQLite db, labels each tile with its z/x/y, and synthesizes one "overzoom" level
    around Stephansplatz by cropping/upscaling + red-tinting max-zoom parents (to make
    fabricated detail visually distinct). Generation runs in a background daemon thread on
    startup without blocking boot; progress is reported into `processes.py` under key
    `"debug_ortho"`, and `/v1/debug-ortho/status` derives its `generating|ready|error`
    wording from that entry (tile requests 503 until ready). Reference implementation for
    how a "tile source" fits together (bbox → tile range → fetch/generate → SQLite cache →
    serve).
  - **tile_creators/sun_exposure.py** — monthly direct-sun tiles (two tilesets per month:
    `hours` h/day and clear-sky `energy` Wh/m²/day, RGB PNG with mean + std at fixed
    per-product scales so clients need no meta — hours: 8-bit R = mean, G = std, cap 16 h;
    energy: 16-bit mean in R/G (hi/lo), B = std, cap 9000 Wh) from the same kind of ALS
    raster, including shadows from terrain, buildings and trees. Convex-hull horizon sweep
    in ~530 azimuths 0.5° apart (numba kernel `_sweep_direction`) over nested
    1 m / 8 m / 32 m (max-pooled) geometry, combined with per-month sun-path lookup
    tables, so all requested months share one sweep. Generated **on demand** (`POST
    /v1/sun-exposure/generate?months=06[,12]`), one run at a time, and for now only for
    one `COMPUTE_AREA_M` square (default 2 km at the source centre) — full-source mode is
    a documented extension, not implemented. Months keyed `01`..`12`; `<MM>_meta.json`
    (written last) marks a month as generated. Method/encoding/limitations:
    `docs/sun_exposure.md`; synthetic checks: `scripts/validate_sun_exposure.py`.
  - **tile_creators/progress.py** — `RateTracker`, the shared windowed "tiles/s"
    throughput helper the generation loops feed into their `processes.py` messages, so
    they all report rate the same way. Windowed (not cumulative) because resumed runs skip
    cached tiles in a burst that would skew the average.
- **util/** — shared modules **the server imports**. Anything the server never imports
  belongs in `scripts/` instead.
  - **util/general.py** — small shared helpers. Notably `read_version()` extracts the
    version string from the badge in `README.md` (the version is intentionally *not*
    duplicated in code — bump it only in the README badge); it resolves the README one
    directory up since this module lives in `util/`.
  - **util/sun.py** — vectorized sun position (port of weBIGeo's
    `nucleus/utils/sun_calculations.cpp`/suncalc; compass azimuth, apparent altitude) and
    r.sun's clear-sky beam irradiance. Used by `tile_creators/sun_exposure.py`.
  - **util/encoding.py** — normal-vector encode/decode shared by
    `tile_creators/als_normals.py` and the playground notebook: hemi-octahedral projection
    plus 127-centred 8-bit quantization written into the R/G channels of a normal tile.
    See `docs/normal_map_encoding.md`.
- **scripts/** — standalone CLIs and notebooks, **never imported by the server**. They
  reach `config`/`const` by inserting the repo root on `sys.path`, and have their own
  `scripts/requirements.txt` separate from the server's.
  - **scripts/fetch_snow_cover.py** — CLI for exploring exolabs/COSMOS snow-depth data:
    lists products, downloads WMS/XYZ tiles, or fetches/crops raw S3 GeoTIFFs. See
    `docs/cosmos-api.md` for the two access paths (rendered WMS tiles vs. raw GeoTIFF
    values) and why raw GeoTIFF is recommended for future integration.
  - **scripts/fetch_bev_als.py** — CLI for downloading BEV ALS DTM/DSM GeoTIFF tiles
    (1 m/px, EPSG:3035, 50×50 km per tile) and stamping the CRS the downloads omit.
  - **scripts/build_als_mosaic.py** — builds the all-Austria ALS height mosaic: `sources`
    downloads missing BEV tiles into `D:\DEM\AUT\{product}` (kept untouched) and writes a
    seamless float32 EPSG:3035 VRT; `webmercator` warps that once into an int32-centimetre
    EPSG:3857 mosaic on the exact z17 tile grid (one file per z10 tile, in
    `data/als_mosaic/{product}/3857_z17`, plus a VRT that unscales to float32 metres) so
    tile creators can window-read it; `verify` compares the mosaic against the sources
    through `tile_creators/als_normals.py`. Imports `fetch_bev_als` and `als_normals`
    helpers. See `docs/als_mosaic.md`.
  - **scripts/validate_sun_exposure.py** — synthetic checks for
    `tile_creators/sun_exposure.py` (sun position vs suncalc, hull sweep and monthly tables
    vs brute-force ray marching, tile pipeline). No data or server needed; takes several
    minutes because the references are deliberately naive.
  - **scripts/normal_map_playground.ipynb** — exploratory notebook for terrain normals:
    reprojects DTM/DSM into a single slippy tile, compares gradient methods, and measures
    the encoding in `util/encoding.py`.
- **docs/index.html** — landing page served at `/`.
- **docs/map.html** — Leaflet map served at `/map`, with a drag-reorderable overlay stack:
  `debug_ortho`, the date-selectable COSMOS snow layer, the ALS normals, and the
  month-selectable sun exposure layers. Every layer renders its tiles as-is, with no
  client-side decoding — the point is to see what the server actually stored. Generation
  triggers and progress live on `docs/index.html`, not here.
- **docs/cosmos-api.md** — research notes on the exolabs/COSMOS snow data API, companion to
  `scripts/fetch_snow_cover.py`.
- **docs/als_mosaic.md** — measurements and decisions behind `scripts/build_als_mosaic.py`
  (codec/quantization benchmark, CRS choice, chunk layout, size estimates).

## Conventions worth preserving

- Every source file starts with the GPLv3 header block; keep it on new files.
- `util/` vs `scripts/`: a module the server imports goes in `util/` (imported as
  `from util import general`); a standalone CLI or notebook the server never touches goes
  in `scripts/`. Nothing in `util/` may import from `scripts/`.
- Config values are always read as `config.<name>`, with a
  `getattr(config, "name", default)` fallback for optional/notification-related settings
  so `config.py` can omit them.
- Background/generation work (see `tile_creators/debug_ortho.py`) uses a daemon thread
  plus `processes.py` for progress, rather than blocking server startup or logging
  per-item progress at INFO level — follow this for future long-running jobs.
- A tile source's own tunables (URL templates, bboxes, retry/delay settings, etc.) live as
  constants at the top of its `tile_creators/*.py` module, not in `config.py` — `config.py`
  is reserved for deployment-level settings (paths, ports, credentials).
- **Keep comments in code to a minimum — only where it's really necessary to describe the
  code.** Comments must not carry thoughts, plans, reasoning, TODOs-as-narration, or other
  process/narrative notes; never leave behind a draft of the plan you worked from.

## Important

- Always ask for architectural decisions first.
- Ask if something is unclear.
- Only do little syntax/smoke tests. Don't run the whole server.

