# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

weBIGeo Tiles Server — a Flask app that will process and serve geo tiles (weather,
snow, other overlays) for the weBIGeo project. It is currently a **skeleton**: only a
status endpoint, a landing page, and a self-generating debug tileset exist. Tile
processing/serving logic is added incrementally. It shares its scaffolding (server
setup, logging, notifications, DB plumbing) with the sibling
[weBIGeo Cloud Server](https://github.com/weBIGeo/clouds-server) repo — when adding
generic infrastructure, check whether the equivalent already exists there before
inventing a new pattern.

## Setup & running

```bash
pip install -r requirements.txt
cp config.example.py config.py   # then edit config.py for your deployment
python server.py
```

`config.py` is git-ignored (secrets, local paths); `config.example.py` is the
documented template and single source of truth for available settings — when adding
a new config value, add it to `config.example.py` with a comment, not just to `config.py`.

There is no test suite, linter, or build step configured yet.

## Architecture

- **server.py** — Flask app entrypoint. Registers `routes_v1.bp`, serves `docs/index.html`
  at `/`, initializes logging/db/debug_ortho/notify on startup, then serves via
  `waitress` (not Flask's dev server — see `config.threads` for why: the tile client
  fires many concurrent per-quad tile requests, so the default 4 threads causes
  queuing/timeouts).
- **routes_v1.py** — all `/v1/...` HTTP routes (Flask Blueprint). `server.py` also
  exposes an unversioned `/status` alias that delegates to `routes_v1.status()`. New
  endpoints go here.
- **config.py / config.example.py** — the only configuration mechanism (plain Python
  module, not env vars or a config file format). Every module reads settings via
  `import config; config.some_setting`.
- **db.py** — single shared SQLite connection (`check_same_thread=False` + a
  `threading.Lock`) for the main tiles DB. No tables exist yet; schema is added here
  as tile processing is built out.
- **log_config.py** — custom colored logging formatter matching the style used across
  weBIGeo projects (see the C++ formatter it mirrors, linked in the module). Sets up
  console + rotating file handlers and per-logger level overrides from
  `config.log_level_overrides`.
- **notify.py** — fire-and-forget notifications (email via SMTP, push via ntfy.sh) used
  for operational events like server start. Both channels no-op silently if unconfigured.
- **processes.py** — generic in-memory registry for long-running background processes
  (`start`/`update`/`finish`/`fail`, keyed by an arbitrary string). Any module reports
  its own progress into it; `/v1/processes` exposes a snapshot (`running` processes plus
  `done`/`error` ones for `FINISHED_RETENTION_SECONDS` after they finish) and
  `docs/index.html` polls it to render live progress bars. This is the mechanism for
  surfacing background work in the UI — prefer reporting into it over ad-hoc INFO-level
  progress logging when adding a new long-running job.
- **tile_creators/** — package for tile-source modules (each one owns its own dataset
  and, unlike the rest of the app, its own tunables as module-level constants rather
  than entries in `config.py` — these are implementation details of one tile source,
  not deployment config).
  - **tile_creators/debug_ortho.py** — a self-contained synthetic/debug tile source:
    downloads real basemap.at orthophoto tiles over Vienna's 1st district into its own
    SQLite db (`DB_PATH`, separate from the main db), labels each tile with its z/x/y,
    and additionally synthesizes one "overzoom" level around Stephansplatz by
    cropping/upscaling + red-tinting the max-zoom parent tiles (to visually distinguish
    fabricated detail from real imagery). Generation runs in a background daemon thread
    on startup and does not block server boot; progress is reported into `processes.py`
    under the key `"debug_ortho"`, and `/v1/debug-ortho/status` derives its
    `generating|ready|error` wording from that same registry entry (tile requests 503
    until ready). This module is a reference implementation for how a "tile source"
    fits together (bbox → tile range → fetch/generate → SQLite cache → serve), useful
    as a pattern when wiring up real data sources (e.g. exolabs/COSMOS).
- **util.py** — small shared helpers. Notably `read_version()` extracts the version
  string from the badge in `README.md` (the version is intentionally *not* duplicated
  in code — bump it only in the README badge).
- **util/fetch_snow_cover.py** — standalone CLI (not imported by the server) for
  exploring exolabs/COSMOS snow-depth data: lists products, downloads WMS/XYZ tiles,
  or fetches/crops the raw S3 GeoTIFFs. Has its own `util/requirements.txt`. See
  `docs/cosmos-api.md` for the full writeup of the two exolabs access paths (rendered
  WMS tiles vs. raw GeoTIFF values) and why the raw GeoTIFF is the recommended path for
  future integration.
- **docs/index.html** — the landing page served at `/`.
- **docs/map.html** — Leaflet map served at `/map`, viewing the `debug_ortho` tileset.
- **docs/cosmos-api.md** — research notes on the exolabs/COSMOS snow data API,
  companion to `util/fetch_snow_cover.py`.

## Conventions worth preserving

- Every source file starts with the GPLv3 header block; keep it on new files.
- Config values are always read as `config.<name>`, with sensible fallback via
  `getattr(config, "name", default)` for optional/notification-related settings so
  `config.py` can omit them entirely.
- Background/generation work (see `tile_creators/debug_ortho.py`) uses a daemon thread
  plus `processes.py` for progress reporting rather than blocking server startup or
  logging per-item progress at INFO level — follow this pattern for future long-running
  tile generation jobs.
- A tile source's own tunables (URL templates, bboxes, retry/delay settings, etc.) live
  as constants at the top of its `tile_creators/*.py` module, not in `config.py` —
  `config.py` is reserved for deployment-level settings (paths, ports, credentials).
