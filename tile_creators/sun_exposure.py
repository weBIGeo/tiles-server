#############################################################################
# weBIGeo Tiles
# Copyright (C) 2026 Gerald Kimmersdorfer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#############################################################################
#
# Monthly direct-sun tiles (hours/day and clear-sky Wh/m^2/day) from a BEV ALS
# height raster, including shadows cast by terrain, buildings and trees.
#
# Agnostic like als_normals.py: whatever single GeoTIFF TILE_SOURCE points at
# (DTM or DSM) is both the surface that receives the sun and the geometry that
# casts shadows. On a DSM that means sun on roofs and canopy tops; on a DTM,
# bare-earth terrain shadow only.
#
# The method, its accuracy argument and its limitations are written up in
# docs/sun_exposure.md; the short version:
#
# 1. Geometry is read at three nested resolutions around the compute area -
#    1 m native near it, 8 m and 32 m *max-pooled* further out - chosen so every
#    occluder is resolved at least as finely as the sun disk (~0.5 deg) from
#    every receiver.
# 2. For each of ~500 azimuths (0.5 deg apart, only across the range the sun
#    actually visits in a year) a convex-hull sweep (Dozier 1981, Timonen &
#    Westerholm 2010) gives every receiver its exact horizon elevation in that
#    direction, at any distance, in amortized O(1) - earth curvature and
#    refraction included.
# 3. Per month, the sun path is sampled every minute of every day and
#    binned by azimuth into cumulative-over-altitude tables, so "how many
#    minutes per day is the sun in this azimuth bin AND above this horizon" is
#    a single table lookup. Summing that over all azimuth bins is the month's
#    sun duration; the same trick with sun-vector-weighted clear-sky irradiance
#    gives the energy on the inclined surface. The horizon depends only on
#    geometry, so all requested months share one set of sweeps.
# 4. The 3035 result is averaged onto the Web Mercator tile grid and reduced
#    into a mean/std pyramid (exact, leaf-count weighted, same pattern as
#    als_normals.py), and written as RGB PNGs: mean + std, with a fixed
#    per-product scale (see "Encoding" below) so a client never needs the meta.
#
# The first milestone computes one COMPUTE_AREA_M square (by default at the
# centre of TILE_SOURCE), not the whole 50 km source - see the "full-source
# mode" note in docs/sun_exposure.md for what that extension needs.

import calendar
import io
import json
import logging
import math
import os
import re
import threading
import time

import numba
import numpy as np
import rasterio
import rasterio.warp
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import from_bounds, from_origin
from rasterio.windows import Window

import processes
from tile_creators import progress
from util import sun
from util import tile_db

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Source & output
# --------------------------------------------------------------------------
# The single source raster. A BEV ALS DTM or DSM GeoTIFF: 1 m/px, EPSG:3035,
# 50x50 km per tile (see scripts/fetch_bev_als.py). Same default as
# als_normals.TILE_SOURCE, but deliberately its own constant - the two tile
# sources are independent and may well be pointed at different files.
TILE_SOURCE = r"data\als_tiles\DSM\ALS_DSM_CRS3035RES50000mN2650000E4500000.tif"

TILES_DIR = "data/sun_exposure_tiles"
# One directory per source file (named after it), holding
# <MM>_hours.db, <MM>_energy.db and <MM>_meta.json per generated month - so
# switching TILE_SOURCE between DTM and DSM never mixes the two.
SOURCE_DIR = os.path.join(
    TILES_DIR, os.path.splitext(os.path.basename(TILE_SOURCE.replace("\\", "/")))[0]
)

# Set to a number to override the source's declared nodata (see
# als_normals.SOURCE_NODATA for when BEV files need this).
SOURCE_NODATA = None

PROCESS_KEY_TEMPLATE = "sun_exposure_{month}"
PROCESS_LABEL_TEMPLATE = "Sun exposure {month_name} ({source})"

PRODUCTS = ("hours", "energy")

# --------------------------------------------------------------------------
# Compute area
# --------------------------------------------------------------------------
# Side length of the square that gets computed, in metres. Only this area gets
# results; everything around it only casts shadows. A few km is minutes of
# work; the whole 50 km source is a different league (see
# docs/sun_exposure.md, "full-source mode") and not supported yet.
COMPUTE_AREA_M = 5000
# (lon, lat) WGS84 centre of the compute area, or None for the centre of
# TILE_SOURCE. The centre is also the best place for a test: a central area
# sees ~24 km of real surroundings in every direction, while an area near the
# source edge gets too much sun from the side where terrain is missing.
#COMPUTE_AREA_CENTER = None
COMPUTE_AREA_CENTER = (12.625394, 47.172218)

# --------------------------------------------------------------------------
# Nested occluder geometry
# --------------------------------------------------------------------------
# Rule behind these numbers: cell size / (smallest distance to any receiver)
# <= ~0.5 deg, the angular size of the sun disk. Detail finer than that only
# produces a partial shadow, so resolving it buys nothing - and resolving
# anything coarser would start to miss real shadows.
#
# fine: native resolution, out to NEAR_APRON_M beyond the compute area. This
# is where trees and houses are resolved.
FINE_RES = 1.0
NEAR_APRON_M = 1000.0
# mid: max-pooled, from NEAR_APRON_M out to MID_APRON_M. 8 m / 1 km = 0.46 deg.
MID_RES = 8.0
MID_APRON_M = 4000.0
# far: max-pooled, from MID_APRON_M to the source edge (or FAR_MAX_M, whichever
# comes first). 32 m / 4 km = 0.46 deg.
FAR_RES = 32.0
FAR_MAX_M = 60000.0
# Why max-pooling rather than averaging for mid/far: an average would erase a
# lone tree or a mast in the distance. Max is conservative - it can overstate
# a shadow by less than one sun disk, and it biases distant terrain horizons up
# by at most ~0.1-0.2 deg - and is documented as such.

# Budget for one strip read while building the pooled grids, in source pixels
# (float32, so ~256 MB). Bounds peak memory when pooling the whole source.
READ_STRIP_PIXELS = 64_000_000

# Earth curvature and atmospheric refraction on lines of sight to terrain:
# an effective earth radius R' = R / (1 - k), with the standard terrestrial
# refraction coefficient k = 0.13. Matters in the Alps: ~27 m of apparent drop
# at 20 km.
EARTH_RADIUS = 6371000.0
REFRACTION_COEFF = 0.13

# --------------------------------------------------------------------------
# Sun sampling
# --------------------------------------------------------------------------
# Spacing of the sweep directions. 0.5 deg ~ the sun disk (0.53 deg): a gap
# between two directions is narrower than the sun itself.
AZ_STEP_DEG = 0.5
# Extra azimuth range beyond what the sun reaches over REFERENCE_YEAR.
AZ_MARGIN_DEG = 2.0
# The sun path per calendar month changes negligibly between years; this is
# the year whose dates are actually sampled.
REFERENCE_YEAR = 2025
# Sun sampling interval. Only feeds the (cheap) tables, never the sweep, so it
# can be fine: the sun moves ~0.25 deg/min, so 60 s puts every 0.5 deg
# azimuth bin within reach of a sample on every day of the month.
SUN_STEP_S = 60
# Lowest apparent sun altitude that is sampled, and the start of the altitude
# axis of every table. Below 0 on purpose: from a peak the horizon can lie
# below the astronomical one (looking down, plus earth curvature), and the
# sun is still visible there.
ALT_MIN_DEG = -3.0
# Altitude axis resolution of the tables. Lookups interpolate linearly
# between entries, so this only has to be fine relative to how quickly the
# per-bin sun time changes with altitude.
ALT_STEP_DEG = 0.1

# Receiver elevations the energy tables are built for; the air mass (and
# hence the beam irradiance) depends on elevation, and lookups interpolate
# linearly between the two nearest levels.
ENERGY_ELEVATIONS_M = (0.0, 1000.0, 2000.0, 3000.0, 4000.0)
# Linke turbidity per month - how hazy the cloudless atmosphere is. A typical
# Alpine climatology in the range r.sun documents (clear winters ~2, hazier
# summers ~3.5), *not* measured values for any specific place. Energy scales
# as exp(-0.8662 * T_LK * m * dR), so this is the main knob of the energy
# tileset's absolute level.
LINKE_TURBIDITY = {
    1: 2.0, 2: 2.2, 3: 2.5, 4: 2.9, 5: 3.2, 6: 3.4,
    7: 3.5, 8: 3.3, 9: 2.9, 10: 2.6, 11: 2.2, 12: 2.0,
}

# --------------------------------------------------------------------------
# Tiles
# --------------------------------------------------------------------------
# z16 = ~1.6 m/px at Austrian latitudes, the same as als_normals. With the
# 1 m sweep grid that puts ~2.5 computed pixels into every tile pixel, so even
# max-zoom std carries real sub-pixel variation (e.g. a shadow edge).
MAX_ZOOM = 17
MIN_ZOOM = 0
TILE_SIZE = 256

# Encoding (see docs/sun_exposure.md). The scales are fixed parts of the tile
# format, not per-dataset values - a renderer hardcodes them (the meta still
# echoes them for information). Changing one invalidates every stored tile.
#
# hours:  R = round(mean * 254 / HOURS_MAX), 0..254, R = 255 marks nodata;
#         G = round(std * 255 / (HOURS_MAX / 2)); B = 0.
# energy: mean as 16 bit big-endian, R = high byte, G = low byte:
#         v = round(mean * 65534 / ENERGY_MAX), 0..65534, 0xFFFF marks nodata;
#         B = round(std * 255 / (ENERGY_MAX / 2)).
# Energy gets 16 bit because it spans ~50x between a shaded north slope in
# winter and a sunny summer one - 8 bit would band badly at the low end. Split
# bytes mean a renderer must fetch texels unfiltered and interpolate after
# decoding.
HOURS_MAX = 16.0  # h/day - longest possible day at Austrian latitudes ~16.0 h (49 N)
# Wh/m^2/day. The most any fixed surface in Austria can receive under this
# module's clear-sky model is ~8350 (June, 49 N, 4000 m, best-oriented slope -
# not a flat surface: in winter a steep south slope gets ~3.5x flat, only in
# June are the two about equal). ~8% headroom on top for LINKE_TURBIDITY
# tweaks, since this is a fixed format constant.
ENERGY_MAX = 9000.0
NODATA_CODE = 255
NODATA_CODE_16 = 0xFFFF

COMMIT_BATCH_SIZE = 500

WEBMERCATOR_ORIGIN = 20037508.342789244

# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
_MONTH_RE = re.compile(r"^(0[1-9]|1[0-2])$")

# (month, product) -> open TileDb, guarded by _lock (TileDb is itself
# thread-safe; the lock only protects this registry and _running).
_dbs: dict[tuple[str, str], tile_db.TileDb] = {}
# Months of the generation run currently in progress. Only one run at a time:
# the sweep is CPU-bound and already uses every core.
_running: set[str] = set()
_lock = threading.Lock()

_STATE_TO_STATUS = {
    processes.RUNNING: "generating",
    processes.DONE: "ready",
    processes.ERROR: "error",
}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def source_name() -> str:
    return os.path.basename(TILE_SOURCE.replace("\\", "/"))


def _process_key(month: str) -> str:
    return PROCESS_KEY_TEMPLATE.format(month=month)


def _process_label(month: str) -> str:
    return PROCESS_LABEL_TEMPLATE.format(month_name=calendar.month_name[int(month)], source=source_name())


def _db_path(month: str, product: str) -> str:
    return os.path.join(SOURCE_DIR, f"{month}_{product}.db")


def _meta_path(month: str) -> str:
    return os.path.join(SOURCE_DIR, f"{month}_meta.json")


def validate_month(month: str) -> None:
    if not _MONTH_RE.match(month):
        raise ValueError(f"invalid month {month!r}, expected 01..12")


def init() -> None:
    """Register every month already generated on disk as ready. Nothing is
    generated on boot - a run is minutes to hours of CPU, so it only starts on
    demand via start_generation() (the /v1/sun-exposure/generate route)."""
    os.makedirs(SOURCE_DIR, exist_ok=True)
    months = _months_on_disk()
    for month in months:
        key = _process_key(month)
        if processes.get(key) is None:
            processes.start(key, _process_label(month))
            processes.finish(key, message="loaded from disk")
    logger.info("sun-exposure: loaded %d existing month(s) from %s", len(months), SOURCE_DIR)


def _months_on_disk() -> list[str]:
    """A month counts as generated only if its meta file exists - it is
    written last, after both tilesets are complete."""
    if not os.path.isdir(SOURCE_DIR):
        return []
    return sorted(
        fn[:2] for fn in os.listdir(SOURCE_DIR)
        if fn.endswith("_meta.json") and _MONTH_RE.match(fn[:2])
    )


def list_months() -> list[dict]:
    months = set(_months_on_disk())
    with _lock:
        months |= _running
    return [{"month": m, "status": get_status(m)} for m in sorted(months)]


def get_status(month: str) -> str:
    p = processes.get(_process_key(month))
    return _STATE_TO_STATUS[p["state"]] if p else "unknown"


def is_ready(month: str) -> bool:
    p = processes.get(_process_key(month))
    return p is not None and p["state"] == processes.DONE


def get_meta(month: str) -> dict | None:
    """What a client needs to use a month's tiles: the computed area (the
    tiles only cover that small patch), zoom range and value scales."""
    try:
        with open(_meta_path(month), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def get_db(month: str, product: str) -> tile_db.TileDb | None:
    """Open the db for (month, product) read-only style: None rather than
    creating an empty file for a month nothing has generated."""
    with _lock:
        conn = _dbs.get((month, product))
        if conn is not None:
            return conn
        path = _db_path(month, product)
        # A month being generated may still have a stale db from an earlier,
        # failed run on disk; opening it would keep the file open while
        # _replace_db needs to delete it.
        if month in _running or not os.path.exists(path):
            return None
        conn = tile_db.TileDb(path, commit_batch_size=COMMIT_BATCH_SIZE)
        _dbs[(month, product)] = conn
        return conn


def start_generation(months: list[str]) -> dict[str, str]:
    """Start one background run computing all requested months that aren't
    ready yet - they share the (expensive) sweep, so asking for several at
    once is much cheaper than one after another.

    Returns {month: status} immediately. Months already generated stay as they
    are ("ready"); to regenerate one, delete its files in SOURCE_DIR and
    restart. Only one run at a time: while one is running, every request just
    reports statuses."""
    for m in months:
        validate_month(m)
    months = sorted(set(months))

    with _lock:
        busy = bool(_running)
        todo = [m for m in months if not is_ready(m)] if not busy else []
        if todo:
            _running.update(todo)

    if busy:
        return {m: get_status(m) if m in _running or is_ready(m) else "busy" for m in months}
    if not todo:
        return {m: "ready" for m in months}

    if not os.path.exists(TILE_SOURCE):
        with _lock:
            _running.difference_update(todo)
        raise ValueError(f"source raster not found: {TILE_SOURCE}")

    for m in todo:
        processes.start(_process_key(m), _process_label(m))
    logger.info("sun-exposure: starting generation for month(s) %s in background", ", ".join(todo))
    threading.Thread(target=_generate, args=(todo,), name="sun-exposure-gen", daemon=True).start()
    return {m: ("generating" if m in todo else "ready") for m in months}


# --------------------------------------------------------------------------
# Geometry: the compute area and its nested occluder grids
# --------------------------------------------------------------------------
class _Grid:
    """A north-up raster in the source CRS: heights (float32, NaN = no data),
    top-left corner (left, top) in source CRS metres, and cell size."""

    def __init__(self, heights: np.ndarray, left: float, top: float, res: float):
        self.heights = heights
        self.left = left
        self.top = top
        self.res = res

    @property
    def right(self) -> float:
        return self.left + self.heights.shape[1] * self.res

    @property
    def bottom(self) -> float:
        return self.top - self.heights.shape[0] * self.res


def _snap_down(v: float, origin: float, cell: float) -> float:
    return origin + math.floor((v - origin) / cell) * cell


def _snap_up(v: float, origin: float, cell: float) -> float:
    return origin + math.ceil((v - origin) / cell) * cell


def _read_pooled(
    src: rasterio.DatasetReader, left: float, top: float, n_cols: int, n_rows: int,
    factor: int, op: str,
) -> np.ndarray:
    """Read an (n_rows*factor, n_cols*factor) source window starting at
    (left, top) and pool it by `factor` into (n_rows, n_cols) float32.

    `op` is "max" (occluders - see the MID_RES comment for why) or "mean" (a
    plain downsample, only used when FINE_RES is coarser than the source).
    NaN-aware either way: a cell is NaN only if every source pixel in it is
    no-data or outside the raster. Read in row strips of at most
    READ_STRIP_PIXELS, so pooling the whole 50 km source never holds more than
    one strip at full resolution. (left, top) must lie on the source pixel
    grid - the callers snap to it."""
    src_res = src.res[0]
    col_off = int(round((left - src.transform.c) / src_res))
    row_off = int(round((src.transform.f - top) / src_res))
    nodata = SOURCE_NODATA if SOURCE_NODATA is not None else src.nodata

    out = np.empty((n_rows, n_cols), dtype=np.float32)
    strip = max(1, READ_STRIP_PIXELS // (n_cols * factor * factor))
    for r in range(0, n_rows, strip):
        nr = min(strip, n_rows - r)
        window = Window(col_off, row_off + r * factor, n_cols * factor, nr * factor)
        # boundless: anything outside the raster comes back masked, like nodata.
        a = src.read(1, window=window, boundless=True, masked=True)
        a = a.astype(np.float32).filled(np.nan)
        if SOURCE_NODATA is not None:
            a[a == np.float32(nodata)] = np.nan
        a = a.reshape(nr, factor, n_cols, factor)
        if op == "max":
            # fmax ignores NaN unless both operands are NaN.
            pooled = np.fmax.reduce(np.fmax.reduce(a, axis=3), axis=1)
        elif op == "mean":
            ok = np.isfinite(a)
            s = np.where(ok, a, 0.0).sum(axis=(1, 3), dtype=np.float64)
            n = ok.sum(axis=(1, 3))
            pooled = np.where(n > 0, s / np.maximum(n, 1), np.nan)
        else:
            raise ValueError(f"unknown pooling op {op!r}")
        out[r:r + nr] = pooled
    return out


def _compute_area_center(src: rasterio.DatasetReader) -> tuple[float, float]:
    if COMPUTE_AREA_CENTER is None:
        b = src.bounds
        return (b.left + b.right) / 2.0, (b.bottom + b.top) / 2.0
    xs, ys = rasterio.warp.transform("EPSG:4326", src.crs, [COMPUTE_AREA_CENTER[0]], [COMPUTE_AREA_CENTER[1]])
    return xs[0], ys[0]


def _read_geometry(src: rasterio.DatasetReader) -> tuple[_Grid, _Grid, _Grid, tuple[int, int, int, int]]:
    """Read the fine, mid and far grids around the compute area.

    Returns (fine, mid, far, area) where `area` = (r0, r1, c0, c1) is the
    compute area as a half-open pixel range *inside the fine grid* - the fine
    grid is the area plus NEAR_APRON_M on every side."""
    src_res = src.res[0]
    if abs(src.res[0] - src.res[1]) > 1e-6:
        raise RuntimeError(f"sun-exposure: non-square source pixels {src.res} are not supported")

    def factor_for(cell: float) -> int:
        f = cell / src_res
        if abs(f - round(f)) > 1e-6 or round(f) < 1:
            raise RuntimeError(
                f"sun-exposure: cell size {cell} m is not a whole multiple of the source resolution {src_res} m"
            )
        return int(round(f))

    ox, oy = src.transform.c, src.transform.f  # source grid origin, for snapping
    cx, cy = _compute_area_center(src)
    half = COMPUTE_AREA_M / 2.0

    # Fine grid = compute area + near apron, snapped to FINE_RES cells of the
    # source grid.
    area_left = _snap_down(cx - half, ox, FINE_RES)
    area_top = _snap_up(cy + half, oy, FINE_RES)
    area_px = int(round(COMPUTE_AREA_M / FINE_RES))
    apron_px = int(round(NEAR_APRON_M / FINE_RES))
    fine_left = area_left - apron_px * FINE_RES
    fine_top = area_top + apron_px * FINE_RES
    fine_px = area_px + 2 * apron_px
    fine = _Grid(
        _read_pooled(src, fine_left, fine_top, fine_px, fine_px, factor_for(FINE_RES), "mean"),
        fine_left, fine_top, FINE_RES,
    )
    area = (apron_px, apron_px + area_px, apron_px, apron_px + area_px)

    # Mid grid = compute area + MID_APRON_M, max-pooled.
    mid_left = _snap_down(area_left - MID_APRON_M, ox, MID_RES)
    mid_top = _snap_up(area_top + MID_APRON_M, oy, MID_RES)
    mid_right = _snap_up(area_left + COMPUTE_AREA_M + MID_APRON_M, ox, MID_RES)
    mid_bottom = _snap_down(area_top - COMPUTE_AREA_M - MID_APRON_M, oy, MID_RES)
    mid_cols = int(round((mid_right - mid_left) / MID_RES))
    mid_rows = int(round((mid_top - mid_bottom) / MID_RES))
    mid = _Grid(
        _read_pooled(src, mid_left, mid_top, mid_cols, mid_rows, factor_for(MID_RES), "max"),
        mid_left, mid_top, MID_RES,
    )

    # Far grid = the whole source (clipped to FAR_MAX_M around the area),
    # max-pooled. Outside the source there is simply no occluder - see the
    # "no terrain beyond TILE_SOURCE" limitation.
    b = src.bounds
    far_left = _snap_down(max(b.left, cx - FAR_MAX_M), ox, FAR_RES)
    far_top = _snap_up(min(b.top, cy + FAR_MAX_M), oy, FAR_RES)
    far_right = _snap_up(min(b.right, cx + FAR_MAX_M), ox, FAR_RES)
    far_bottom = _snap_down(max(b.bottom, cy - FAR_MAX_M), oy, FAR_RES)
    far_cols = int(round((far_right - far_left) / FAR_RES))
    far_rows = int(round((far_top - far_bottom) / FAR_RES))
    far = _Grid(
        _read_pooled(src, far_left, far_top, far_cols, far_rows, factor_for(FAR_RES), "max"),
        far_left, far_top, FAR_RES,
    )
    return fine, mid, far, area


def _grid_north_convergence_deg(src: rasterio.DatasetReader, x: float, y: float) -> float:
    """Grid bearing of true north at (x, y) in the source CRS, in degrees.

    EPSG:3035 (LAEA centred at 10E 52N) is not north-up away from its central
    meridian - in Austria true north points a few degrees off grid north. A
    true azimuth A is grid azimuth A + gamma. Computed numerically by stepping
    a little north from the point, which is exact for any CRS."""
    lons, lats = rasterio.warp.transform(src.crs, "EPSG:4326", [x], [y])
    xs, ys = rasterio.warp.transform("EPSG:4326", src.crs, [lons[0], lons[0]], [lats[0], lats[0] + 0.01])
    return math.degrees(math.atan2(xs[1] - xs[0], ys[1] - ys[0]))


def _surface_normals(fine: _Grid) -> np.ndarray:
    """(H, W) heights -> (H, W, 3) float32 unit normals in the *grid* ENU frame
    (+X grid east, +Y grid north, +Z up) - the frame the energy tables' sun
    vectors are built in, so no rotation is needed between the two.

    3x3 Sobel, the same estimator as als_normals (NORMAL_METHOD="sobel").
    Pixels whose stencil touches no-data, and the one-pixel border, fall back
    to flat (0, 0, 1): they are only ever receivers if they sit inside the
    compute area, and a void there has no meaningful slope anyway."""
    h = fine.heights
    ok = np.isfinite(h)
    hz = np.where(ok, h, np.float32(0.0))
    tl, tm, tr = hz[:-2, :-2], hz[:-2, 1:-1], hz[:-2, 2:]
    ml, mr = hz[1:-1, :-2], hz[1:-1, 2:]
    bl, bm, br = hz[2:, :-2], hz[2:, 1:-1], hz[2:, 2:]
    dzdx = ((tr + 2 * mr + br) - (tl + 2 * ml + bl)) / (8.0 * fine.res)
    # rows increase southward, so this is -dh/dy_north
    dzds = ((bl + 2 * bm + br) - (tl + 2 * tm + tr)) / (8.0 * fine.res)

    stencil_ok = np.ones(dzdx.shape, dtype=bool)
    for dy in range(3):
        for dx in range(3):
            stencil_ok &= ok[dy:dy + dzdx.shape[0], dx:dx + dzdx.shape[1]]

    n = np.zeros(h.shape + (3,), dtype=np.float32)
    n[..., 2] = 1.0
    inner = np.stack([-dzdx, dzds, np.ones_like(dzdx)], axis=-1)
    inner /= np.linalg.norm(inner, axis=-1, keepdims=True)
    inner[~stencil_ok] = (0.0, 0.0, 1.0)
    n[1:-1, 1:-1] = inner
    return n


# --------------------------------------------------------------------------
# Sun-path tables
# --------------------------------------------------------------------------
def _month_times(month: int) -> np.ndarray:
    start = np.datetime64(f"{REFERENCE_YEAR}-{month:02d}-01T00:00:00")
    end = np.datetime64(f"{REFERENCE_YEAR + (month == 12)}-{month % 12 + 1:02d}-01T00:00:00")
    return np.arange(start, end, np.timedelta64(SUN_STEP_S, "s"))


def _sweep_directions(lat: float, lon: float, gamma: float) -> tuple[np.ndarray, float]:
    """Grid azimuths (deg) of all sweep directions: AZ_STEP_DEG apart, over
    every azimuth the sun reaches above ALT_MIN_DEG during REFERENCE_YEAR (plus
    AZ_MARGIN_DEG). Also returns the highest altitude the sun reaches, which
    bounds the tables' altitude axis.

    Assumes the sun's azimuth range doesn't wrap through north - true
    everywhere the sun sets every day (south of the Arctic circle), i.e.
    anywhere BEV data exists."""
    times = np.arange(
        np.datetime64(f"{REFERENCE_YEAR}-01-01T00:00:00"),
        np.datetime64(f"{REFERENCE_YEAR + 1}-01-01T00:00:00"),
        np.timedelta64(10, "m"),
    )
    az, alt = sun.positions(times, lat, lon)
    up = alt > ALT_MIN_DEG
    az_grid = az[up] + gamma
    lo = math.floor((az_grid.min() - AZ_MARGIN_DEG) / AZ_STEP_DEG) * AZ_STEP_DEG
    hi = math.ceil((az_grid.max() + AZ_MARGIN_DEG) / AZ_STEP_DEG) * AZ_STEP_DEG
    n = int(round((hi - lo) / AZ_STEP_DEG)) + 1
    return lo + AZ_STEP_DEG * np.arange(n), float(alt.max())


def _build_tables(
    month: int, lat: float, lon: float, gamma: float, directions: np.ndarray, n_alt: int,
) -> tuple[np.ndarray, np.ndarray]:
    """The month's lookup tables, per sweep direction k and altitude index a
    (altitude = ALT_MIN_DEG + a * ALT_STEP_DEG):

    - hours[k, a]: minutes per day (averaged over the month) that the sun
      spends in direction bin k at an apparent altitude >= that altitude.
    - energy[k, e, a, :]: the same sum, but of (unit sun vector in grid ENU) x
      (clear-sky beam irradiance at receiver elevation ENERGY_ELEVATIONS_M[e])
      x dt, in Wh/m^2/day. Dotting it with a surface normal gives the direct
      energy on that surface, summed over every sun sample above the horizon.

    Cumulative "from above": a receiver with horizon H in direction k is lit by
    exactly the samples at altitude > H, so its minutes are hours[k, a(H)] -
    one lookup, no matter how many samples went in. This is where the
    per-pixel "when does the sun appear/disappear" question gets answered for
    every day at once.

    Every sample is binned to its nearest sweep direction - bins are
    AZ_STEP_DEG wide, i.e. about one sun disk."""
    times = _month_times(month)
    n_days = calendar.monthrange(REFERENCE_YEAR, month)[1]
    az, alt = sun.positions(times, lat, lon)
    keep = alt > ALT_MIN_DEG
    times, az, alt = times[keep], az[keep] + gamma, alt[keep]

    k = np.rint((az - directions[0]) / AZ_STEP_DEG).astype(np.int64)
    if k.min() < 0 or k.max() >= len(directions):
        raise RuntimeError("sun-exposure: sun azimuth outside the sweep direction range")
    a = np.clip(np.floor((alt - ALT_MIN_DEG) / ALT_STEP_DEG).astype(np.int64), 0, n_alt - 1)

    dt_min = SUN_STEP_S / 60.0
    hist = np.zeros((len(directions), n_alt), dtype=np.float64)
    np.add.at(hist, (k, a), dt_min / n_days)
    hours = np.cumsum(hist[:, ::-1], axis=1)[:, ::-1]

    doy = (times - times.astype("datetime64[Y]")).astype("timedelta64[D]").astype(np.int64) + 1
    s = sun.direction_vectors(az, alt)  # grid ENU, since az is already grid azimuth
    t_lk = LINKE_TURBIDITY[month]
    e_hist = np.zeros((len(directions), len(ENERGY_ELEVATIONS_M), n_alt, 3), dtype=np.float64)
    for e, z in enumerate(ENERGY_ELEVATIONS_M):
        b = sun.beam_normal_irradiance(alt, z, t_lk, doy)  # W/m^2
        w = b * (SUN_STEP_S / 3600.0) / n_days  # Wh/m^2 per day of the month
        for c in range(3):
            np.add.at(e_hist[:, e, :, c], (k, a), w * s[:, c])
    energy = np.cumsum(e_hist[:, :, ::-1, :], axis=2)[:, :, ::-1, :]
    return hours.astype(np.float32), energy.astype(np.float32)


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------
@numba.njit(cache=True, inline="always")
def _sample(h: np.ndarray, left: float, top: float, res: float, x: float, y: float) -> float:
    """Nearest-cell height of a pooled grid at local coordinates (x, y), NaN
    outside it."""
    c = int(math.floor((x - left) / res))
    r = int(math.floor((top - y) / res))
    if r < 0 or c < 0 or r >= h.shape[0] or c >= h.shape[1]:
        return np.nan
    return h[r, c]


@numba.njit(cache=True, inline="always")
def _inside(h: np.ndarray, left: float, top: float, res: float, x: float, y: float) -> bool:
    return left <= x < left + h.shape[1] * res and top - h.shape[0] * res < y <= top


@numba.njit(cache=True, inline="always")
def _hull_push(hx, hh, top, x, hp):
    """Add (x, hp) to an upper convex hull kept as a stack (hx, hh)[:top] and
    return (new top, index of p's horizon point or -1).

    Seen from p, previous point q has elevation (hh_q - hp) / (x - hx_q). While
    the second-to-top point a is at least as high as the top point b, b lies
    on or below segment a-p and can never again be anyone's horizon (every
    later receiver is further along the line), so it is popped. What remains
    on top is exactly the previous point with the steepest elevation from p -
    p's horizon. Every point is pushed and popped at most once: amortized
    O(1) per sample, however far away the horizon point is."""
    while top >= 2:
        ax = hx[top - 2]
        ah = hh[top - 2]
        bx = hx[top - 1]
        bh = hh[top - 1]
        # (ah - hp) / (x - ax) >= (bh - hp) / (x - bx), both denominators > 0
        if (ah - hp) * (x - bx) >= (bh - hp) * (x - ax):
            top -= 1
        else:
            break
    horizon = top - 1
    hx[top] = x
    hh[top] = hp
    return top + 1, horizon


@numba.njit(parallel=True, cache=True)
def _sweep_direction(
    fine_h, res,
    mid_h, mid_left, mid_top, mid_res,
    far_h, far_left, far_top, far_res, far_max_m,
    theta, r0, r1, c0, c1, inv_2r, inv_r,
    alt_min, alt_step, t_hours, t_energy, elev_min, elev_step, normals,
    acc_hours, acc_energy, out_tan,
):
    """One sweep direction over the whole fine grid.

    Coordinates are local metres: +x grid east, +y grid north, origin at the
    fine grid's top-left corner (so fine pixel (r, c) has its centre at
    ((c + .5) * res, -(r + .5) * res)); the mid/far grids are given by their
    top-left corner in the same frame.

    theta is the grid azimuth *towards the sun* (towards the occluders).
    Receivers look that way, so a line is processed from its far end in that
    direction, moving away from the sun: every sample is an occluder for every
    later one.

    Lines are digital lines on the fine grid: along the major axis (whichever
    of col/row the direction is closer to) exactly one pixel per step, with the
    minor coordinate rounded from the exact line position. Adjacent lines are
    one pixel apart on the minor axis, so every fine pixel is visited by
    exactly one line per direction - which is also why the parallel loop over
    lines needs no locking on the accumulators. Heights are the visited
    pixel's own; distances along the line use the exact line position, i.e. a
    receiver is displaced by at most half a pixel perpendicular to the sweep.

    Upstream of the fine grid the same geometric line continues on the mid,
    then the far grid, one cell of that grid per step. The hull doesn't care
    about uneven spacing, so a line is simply [far..., mid..., fine...].
    Samples of a line outside the fine grid but within its major-axis span (a
    line entering through a side) are taken from the mid grid too, or from the
    far grid where they lie beyond the mid grid (steep diagonals).

    Earth curvature + refraction: the hull runs on h' = h - x^2 / (2R'), x the
    distance along the line. That keeps the argmax of the elevation angle
    exact and the true tangent is slope' - x_p / R' (see docs/sun_exposure.md
    for the two-line derivation).

    For every receiver (a fine pixel inside [r0, r1) x [c0, c1)) the horizon is
    looked up straight away in this direction's tables and added to the
    accumulators - the horizon itself is never stored (except into out_tan,
    which is only for validation and ignored when empty)."""
    n_rows, n_cols = fine_h.shape
    d_e = math.sin(theta)
    d_n = math.cos(theta)
    # travel direction in pixel space (cols grow east, rows grow south)
    tc = -d_e
    tr = d_n
    by_cols = abs(tc) >= abs(tr)
    if by_cols:
        n_major = n_cols
        n_minor = n_rows
        step_major = 1 if tc > 0 else -1
        slope = tr / abs(tc)
        step_len = res / abs(tc)
    else:
        n_major = n_rows
        n_minor = n_cols
        step_major = 1 if tr > 0 else -1
        slope = tc / abs(tr)
        step_len = res / abs(tr)
    major_start = 0 if step_major > 0 else n_major - 1
    span = slope * (n_major - 1)
    l_lo = int(math.floor(min(0.0, -span))) - 1
    l_hi = int(math.ceil((n_minor - 1) + max(0.0, -span))) + 1

    n_months = t_hours.shape[0]
    n_alt = t_hours.shape[1]
    n_elev = t_energy.shape[1]
    write_tan = out_tan.shape[0] > 0
    rad2deg = 180.0 / math.pi

    for li in numba.prange(l_hi - l_lo + 1):
        line = l_lo + li
        # Position of step 0 of this line.
        if by_cols:
            px0 = (major_start + 0.5) * res
            py0 = -(line + 0.5) * res
        else:
            px0 = (line + 0.5) * res
            py0 = -(major_start + 0.5) * res

        # Upstream samples: count first, then walk them farthest first.
        n_mid = 0
        u = mid_res
        while _inside(mid_h, mid_left, mid_top, mid_res, px0 + d_e * u, py0 + d_n * u):
            n_mid += 1
            u += mid_res
        u_mid_end = n_mid * mid_res
        n_far = 0
        u = u_mid_end + far_res
        while u <= far_max_m and _inside(far_h, far_left, far_top, far_res, px0 + d_e * u, py0 + d_n * u):
            n_far += 1
            u += far_res

        cap = n_far + n_mid + n_major
        hx = np.empty(cap, dtype=np.float64)
        hh = np.empty(cap, dtype=np.float64)
        top = 0

        for j in range(n_far - 1, -1, -1):
            u = u_mid_end + (j + 1) * far_res
            h = _sample(far_h, far_left, far_top, far_res, px0 + d_e * u, py0 + d_n * u)
            if not math.isnan(h):
                top, _ = _hull_push(hx, hh, top, -u, h - u * u * inv_2r)
        for j in range(n_mid - 1, -1, -1):
            u = (j + 1) * mid_res
            h = _sample(mid_h, mid_left, mid_top, mid_res, px0 + d_e * u, py0 + d_n * u)
            if not math.isnan(h):
                top, _ = _hull_push(hx, hh, top, -u, h - u * u * inv_2r)

        for i in range(n_major):
            major = major_start + i * step_major
            minor_real = line + i * slope
            minor = int(math.floor(minor_real + 0.5))
            x = i * step_len
            receiver = False
            if 0 <= minor < n_minor:
                if by_cols:
                    row = minor
                    col = major
                else:
                    row = major
                    col = minor
                h = fine_h[row, col]
                receiver = r0 <= row < r1 and c0 <= col < c1
            else:
                row = 0
                col = 0
                if by_cols:
                    wx = (major + 0.5) * res
                    wy = -(minor_real + 0.5) * res
                else:
                    wx = (minor_real + 0.5) * res
                    wy = -(major + 0.5) * res
                h = _sample(mid_h, mid_left, mid_top, mid_res, wx, wy)
                # A diagonal line's step 0 can lie further off the fine grid
                # than the mid grid reaches; that stretch is far-grid territory
                # (>= MID_APRON_M from every receiver) and must not be dropped.
                if math.isnan(h) and not _inside(mid_h, mid_left, mid_top, mid_res, wx, wy):
                    h = _sample(far_h, far_left, far_top, far_res, wx, wy)
            if math.isnan(h):
                continue

            hp = h - x * x * inv_2r
            top, hz = _hull_push(hx, hh, top, x, hp)
            if not receiver:
                continue

            if hz >= 0:
                tan_h = (hh[hz] - hp) / (x - hx[hz]) - x * inv_r
            else:
                tan_h = -1e30
            ar = row - r0
            ac = col - c0
            if write_tan:
                out_tan[ar, ac] = tan_h

            fa = (math.atan(tan_h) * rad2deg - alt_min) / alt_step
            if fa <= 0.0:
                ia = 0
                wa = 0.0
            elif fa >= n_alt - 1:
                ia = n_alt - 2
                wa = 1.0
            else:
                ia = int(fa)
                wa = fa - ia

            fe = (h - elev_min) / elev_step
            if fe <= 0.0:
                ie = 0
                we = 0.0
            elif fe >= n_elev - 1:
                ie = n_elev - 2
                we = 1.0
            else:
                ie = int(fe)
                we = fe - ie
            nx = normals[row, col, 0]
            ny = normals[row, col, 1]
            nz = normals[row, col, 2]

            for m in range(n_months):
                acc_hours[m, ar, ac] += t_hours[m, ia] * (1.0 - wa) + t_hours[m, ia + 1] * wa
                e = 0.0
                for comp in range(3):
                    v0 = t_energy[m, ie, ia, comp] * (1.0 - wa) + t_energy[m, ie, ia + 1, comp] * wa
                    v1 = t_energy[m, ie + 1, ia, comp] * (1.0 - wa) + t_energy[m, ie + 1, ia + 1, comp] * wa
                    v = v0 * (1.0 - we) + v1 * we
                    if comp == 0:
                        e += nx * v
                    elif comp == 1:
                        e += ny * v
                    else:
                        e += nz * v
                # n . T is linear, so it can't clip cos(incidence) < 0 per sun
                # sample. Within one 0.5 deg bin the sun vectors are nearly
                # parallel, so clipping per bin is close to clipping per
                # sample - and a sun behind the local surface plane is almost
                # always already below the horizon cast by the adjacent
                # upslope pixel anyway.
                if e > 0.0:
                    acc_energy[m, ar, ac] += e


def _run_sweeps(
    fine: _Grid, mid: _Grid, far: _Grid, area: tuple[int, int, int, int],
    directions: np.ndarray, t_hours: np.ndarray, t_energy: np.ndarray, normals: np.ndarray,
    on_progress, out_tan_dir: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """Sweep every direction that has any sun in any of the requested months,
    accumulating into per-month rasters over the compute area.

    t_hours: (M, K, A), t_energy: (M, K, E, A, 3). Returns (hours h/day,
    energy Wh/m^2/day, horizon tan for direction index out_tan_dir or None),
    each (M, area_h, area_w) float32 (the tan array without the M axis). NaN
    where the receiver itself has no height data."""
    r0, r1, c0, c1 = area
    n_months = t_hours.shape[0]
    acc_hours = np.zeros((n_months, r1 - r0, c1 - c0), dtype=np.float32)
    acc_energy = np.zeros_like(acc_hours)

    r_eff = EARTH_RADIUS / (1.0 - REFRACTION_COEFF)
    # Local frame: origin at the fine grid's top-left corner.
    mid_left, mid_top = mid.left - fine.left, mid.top - fine.top
    far_left, far_top = far.left - fine.left, far.top - fine.top
    elev = np.asarray(ENERGY_ELEVATIONS_M, dtype=np.float64)
    elev_step = float(elev[1] - elev[0])
    if not np.allclose(np.diff(elev), elev_step):
        raise RuntimeError("sun-exposure: ENERGY_ELEVATIONS_M must be evenly spaced")

    # Directions the sun never reaches in the requested months contribute
    # nothing - e.g. December only needs ~40% of the yearly azimuth range.
    active = np.flatnonzero((t_hours[:, :, 0] > 0).any(axis=0))
    no_tan = np.empty((0, 0), dtype=np.float32)
    out_tan = None
    for n, k in enumerate(active):
        tan_buf = no_tan
        if out_tan_dir is not None and k == out_tan_dir:
            out_tan = np.full((r1 - r0, c1 - c0), np.nan, dtype=np.float32)
            tan_buf = out_tan
        _sweep_direction(
            fine.heights, fine.res,
            mid.heights, mid_left, mid_top, mid.res,
            far.heights, far_left, far_top, far.res, FAR_MAX_M,
            math.radians(directions[k]), r0, r1, c0, c1, 1.0 / (2.0 * r_eff), 1.0 / r_eff,
            ALT_MIN_DEG, ALT_STEP_DEG,
            np.ascontiguousarray(t_hours[:, k, :]), np.ascontiguousarray(t_energy[:, k]),
            float(elev[0]), elev_step, normals,
            acc_hours, acc_energy, tan_buf,
        )
        on_progress(n + 1, len(active))

    invalid = ~np.isfinite(fine.heights[r0:r1, c0:c1])
    acc_hours /= 60.0  # min/day -> h/day
    acc_hours[:, invalid] = np.nan
    acc_energy[:, invalid] = np.nan
    return acc_hours, acc_energy, out_tan


# --------------------------------------------------------------------------
# Tiles: 3035 result -> Web Mercator mean/std pyramid -> RGB PNG
# --------------------------------------------------------------------------
def _tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = (2 * WEBMERCATOR_ORIGIN) / 2 ** z
    left = -WEBMERCATOR_ORIGIN + x * size
    top = WEBMERCATOR_ORIGIN - y * size
    return left, top - size, left + size, top


def _point_to_tile_3857(x: float, y: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    size = (2 * WEBMERCATOR_ORIGIN) / n
    tx = int((x + WEBMERCATOR_ORIGIN) // size)
    ty = int((WEBMERCATOR_ORIGIN - y) // size)
    return max(0, min(n - 1, tx)), max(0, min(n - 1, ty))


def _to_mercator(field: np.ndarray, left: float, top: float, res: float, crs) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    """Average a (H, W) result raster in the source CRS onto the MAX_ZOOM tile
    grid. Returns (count, sum, sum_sq, tx0, ty0) over the covered tile range
    (float64, (n_ty*TILE_SIZE, n_tx*TILE_SIZE)): count = 1 per valid tile pixel,
    sum = its mean, sum_sq = its mean of squares - the leaves of the pyramid.

    Averaging x and x^2 separately is what lets a max-zoom pixel carry the
    variance of the ~2.5 computed pixels inside it, instead of a std of 0."""
    h, w = field.shape
    src_transform = from_origin(left, top, res, res)
    l3, b3, r3, t3 = rasterio.warp.transform_bounds(crs, "EPSG:3857", left, top - h * res, left + w * res, top, densify_pts=21)
    tx0, ty0 = _point_to_tile_3857(l3, t3, MAX_ZOOM)
    tx1, ty1 = _point_to_tile_3857(r3, b3, MAX_ZOOM)
    n_tx, n_ty = tx1 - tx0 + 1, ty1 - ty0 + 1
    dl, _, _, dt = _tile_bounds_3857(MAX_ZOOM, tx0, ty0)
    _, db, dr, _ = _tile_bounds_3857(MAX_ZOOM, tx1, ty1)
    dst_transform = from_bounds(dl, db, dr, dt, n_tx * TILE_SIZE, n_ty * TILE_SIZE)

    out = []
    for arr in (field, field.astype(np.float64) ** 2):
        dst = np.full((n_ty * TILE_SIZE, n_tx * TILE_SIZE), np.nan, dtype=np.float64)
        rasterio.warp.reproject(
            source=arr.astype(np.float64), destination=dst,
            src_transform=src_transform, src_crs=crs, src_nodata=np.nan,
            dst_transform=dst_transform, dst_crs="EPSG:3857", dst_nodata=np.nan,
            resampling=Resampling.average,
        )
        out.append(dst)
    mean, sq = out
    valid = np.isfinite(mean) & np.isfinite(sq)
    count = valid.astype(np.float64)
    return count, np.where(valid, mean, 0.0), np.where(valid, sq, 0.0), tx0, ty0


def _reduce_level(count, s1, s2, tx0, ty0):
    """One pyramid step: pad the level to an even tile range, then sum 2x2
    pixel blocks. Plain sums of (count, count*mean, count*mean_sq) are exactly
    the leaf-count-weighted combination als_normals._reduce_2x does with means
    - kept as sums here since nothing needs the intermediate means."""
    pad_l = (tx0 % 2) * TILE_SIZE
    pad_t = (ty0 % 2) * TILE_SIZE
    h, w = count.shape
    pad_r = ((w + pad_l) // TILE_SIZE % 2) * TILE_SIZE
    pad_b = ((h + pad_t) // TILE_SIZE % 2) * TILE_SIZE
    out = []
    for a in (count, s1, s2):
        a = np.pad(a, ((pad_t, pad_b), (pad_l, pad_r)))
        hh, ww = a.shape
        out.append(a.reshape(hh // 2, 2, ww // 2, 2).sum(axis=(1, 3)))
    return out[0], out[1], out[2], tx0 // 2, ty0 // 2


def _encode_tile(count: np.ndarray, s1: np.ndarray, s2: np.ndarray, product: str) -> bytes:
    """One tile's sums -> RGB PNG in `product`'s encoding (see the Encoding
    comment at the top): mean and std, where std is the population std over
    the tile pixel's leaves, including the sub-pixel variance at max zoom.
    See docs/sun_exposure.md for why RGB rather than a 2-channel PNG."""
    valid = count > 0
    n = np.maximum(count, 1.0)
    mean = s1 / n
    std = np.sqrt(np.maximum(s2 / n - mean * mean, 0.0))
    rgb = np.zeros(count.shape + (3,), dtype=np.uint8)
    if product == "hours":
        rgb[..., 0] = np.clip(np.rint(mean * (254.0 / HOURS_MAX)), 0, 254).astype(np.uint8)
        rgb[..., 1] = np.clip(np.rint(std * (255.0 / (HOURS_MAX / 2.0))), 0, 255).astype(np.uint8)
        rgb[~valid, 0] = NODATA_CODE
        rgb[~valid, 1] = 0
    elif product == "energy":
        v = np.clip(np.rint(mean * (65534.0 / ENERGY_MAX)), 0, 65534).astype(np.uint16)
        v[~valid] = NODATA_CODE_16
        rgb[..., 0] = v >> 8
        rgb[..., 1] = v & 0xFF
        rgb[..., 2] = np.clip(np.rint(std * (255.0 / (ENERGY_MAX / 2.0))), 0, 255).astype(np.uint8)
        rgb[~valid, 2] = 0
    else:
        raise ValueError(f"unknown product {product!r}")
    buf = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _write_tileset(conn: tile_db.TileDb, field: np.ndarray, fine: _Grid, area, crs, product: str) -> int:
    """Whole pyramid MAX_ZOOM..MIN_ZOOM for one (month, product). Holds the
    covered tile range in memory at once - fine for a compute area of a few km
    (a 2 km area is ~8x8 z16 tiles); full-source mode will need als_normals'
    block-wise pyramid instead. Returns the number of tiles written."""
    r0, _, c0, _ = area
    left = fine.left + c0 * fine.res
    top = fine.top - r0 * fine.res
    count, s1, s2, tx0, ty0 = _to_mercator(field, left, top, fine.res, crs)
    written = 0
    for z in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        if z != MAX_ZOOM:
            count, s1, s2, tx0, ty0 = _reduce_level(count, s1, s2, tx0, ty0)
        n_ty, n_tx = count.shape[0] // TILE_SIZE, count.shape[1] // TILE_SIZE
        for j in range(n_ty):
            for i in range(n_tx):
                sl = (slice(j * TILE_SIZE, (j + 1) * TILE_SIZE), slice(i * TILE_SIZE, (i + 1) * TILE_SIZE))
                if not (count[sl] > 0).any():
                    continue
                conn.save_tile(z, tx0 + i, ty0 + j, _encode_tile(count[sl], s1[sl], s2[sl], product))
                written += 1
    conn.commit()
    return written


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _update_all(months: list[str], current: int, total: int, message: str) -> None:
    for m in months:
        processes.update(_process_key(m), current, total=total, message=message)


def _replace_db(month: str, product: str) -> tile_db.TileDb:
    """A fresh, empty db for (month, product): a previous failed run's tiles
    (or a different compute area's) must not survive into the new tileset."""
    path = _db_path(month, product)
    with _lock:
        old = _dbs.pop((month, product), None)
    if old is not None:
        old.close()  # Windows won't delete an open file
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    conn = tile_db.TileDb(path, commit_batch_size=COMMIT_BATCH_SIZE)
    with _lock:
        _dbs[(month, product)] = conn
    return conn


def _generate(months: list[str]) -> None:
    t_start = time.monotonic()
    try:
        with rasterio.open(TILE_SOURCE) as src:
            if src.crs is None:
                raise RuntimeError(
                    f"sun-exposure: {TILE_SOURCE} has no CRS (scripts/fetch_bev_als.py stamps EPSG:3035 "
                    f"onto BEV downloads that omit it)"
                )
            logger.info(
                "sun-exposure: source %s crs=%s res=%s shape=%s, compute area %d m, month(s) %s",
                TILE_SOURCE, src.crs, src.res, src.shape, COMPUTE_AREA_M, ", ".join(months),
            )

            _update_all(months, 0, 1, "reading geometry (fine, mid, far)")
            t0 = time.monotonic()
            fine, mid, far, area = _read_geometry(src)
            logger.info(
                "sun-exposure: geometry read in %.1fs - fine %s @%gm, mid %s @%gm, far %s @%gm",
                time.monotonic() - t0, fine.heights.shape, fine.res, mid.heights.shape, mid.res,
                far.heights.shape, far.res,
            )

            r0, r1, c0, c1 = area
            cx = fine.left + (c0 + c1) / 2.0 * fine.res
            cy = fine.top - (r0 + r1) / 2.0 * fine.res
            lons, lats = rasterio.warp.transform(src.crs, "EPSG:4326", [cx], [cy])
            lon, lat = lons[0], lats[0]
            gamma = _grid_north_convergence_deg(src, cx, cy)

            directions, alt_max = _sweep_directions(lat, lon, gamma)
            n_alt = int(math.ceil((alt_max + 1.0 - ALT_MIN_DEG) / ALT_STEP_DEG)) + 1
            tables = [_build_tables(int(m), lat, lon, gamma, directions, n_alt) for m in months]
            t_hours = np.stack([t[0] for t in tables])
            t_energy = np.stack([t[1] for t in tables])
            del tables
            logger.info(
                "sun-exposure: centre %.5fN %.5fE, grid convergence %.3f deg, %d directions %.1f..%.1f deg, "
                "tables %.1f MB",
                lat, lon, gamma, len(directions), directions[0], directions[-1],
                (t_hours.nbytes + t_energy.nbytes) / 1e6,
            )

            normals = _surface_normals(fine)
            rate = progress.RateTracker(unit="dirs")

            def on_progress(done: int, total: int) -> None:
                _update_all(months, done, total, f"sweep {done}/{total} directions ({rate.sample(done)})")

            t0 = time.monotonic()
            hours, energy, _ = _run_sweeps(fine, mid, far, area, directions, t_hours, t_energy, normals, on_progress)
            logger.info("sun-exposure: sweeps done in %.1fs", time.monotonic() - t0)

            area_left = fine.left + c0 * fine.res
            area_top = fine.top - r0 * fine.res
            area_bounds = (area_left, area_top - (r1 - r0) * fine.res, area_left + (c1 - c0) * fine.res, area_top)
            bounds_wgs84 = rasterio.warp.transform_bounds(src.crs, "EPSG:4326", *area_bounds, densify_pts=21)

            for i, m in enumerate(months):
                key = _process_key(m)
                processes.update(key, 0, total=1, message="writing tiles")
                max_h = float(np.nanmax(hours[i])) if np.isfinite(hours[i]).any() else 0.0
                max_e = float(np.nanmax(energy[i])) if np.isfinite(energy[i]).any() else 0.0
                if max_e > ENERGY_MAX:
                    logger.warning(
                        "sun-exposure[%s]: max energy %.0f Wh/m^2/day exceeds ENERGY_MAX %.0f - values clip",
                        m, max_e, ENERGY_MAX,
                    )
                written = 0
                for product, field in (("hours", hours[i]), ("energy", energy[i])):
                    conn = _replace_db(m, product)
                    written += _write_tileset(conn, field, fine, area, src.crs, product)

                meta = {
                    "month": m,
                    "source": source_name(),
                    "bounds": bounds_wgs84,
                    "bounds_source_crs": area_bounds,
                    "source_crs": src.crs.to_string(),
                    "min_zoom": MIN_ZOOM,
                    "max_zoom": MAX_ZOOM,
                    "hours_max": HOURS_MAX,
                    "energy_max": ENERGY_MAX,
                    "stats": {
                        "hours_mean": float(np.nanmean(hours[i])), "hours_max": max_h,
                        "energy_mean": float(np.nanmean(energy[i])), "energy_max": max_e,
                    },
                    "params": {
                        "reference_year": REFERENCE_YEAR, "linke_turbidity": LINKE_TURBIDITY[int(m)],
                        "az_step_deg": AZ_STEP_DEG, "sun_step_s": SUN_STEP_S,
                        "res_fine_mid_far": [FINE_RES, MID_RES, FAR_RES],
                        "near_apron_m": NEAR_APRON_M, "mid_apron_m": MID_APRON_M,
                    },
                }
                with open(_meta_path(m), "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2)
                processes.finish(key, message=f"{written} tiles, mean {meta['stats']['hours_mean']:.2f} h/day")
                logger.info(
                    "sun-exposure[%s]: %d tiles, hours mean %.2f max %.2f h/day, energy mean %.0f max %.0f Wh/m^2/day",
                    m, written, meta["stats"]["hours_mean"], max_h, meta["stats"]["energy_mean"], max_e,
                )
        logger.info("sun-exposure: run for %s done in %.1fs", ", ".join(months), time.monotonic() - t_start)
    except Exception:
        logger.exception("sun-exposure: generation failed")
        for m in months:
            if not is_ready(m):
                processes.fail(_process_key(m), message="generation failed, check server log")
    finally:
        with _lock:
            _running.difference_update(months)
