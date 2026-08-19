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
# Surface-normal tiles from a BEV ALS height raster.
#
# Deliberately agnostic to what the source actually is: it processes whatever
# single GeoTIFF TILE_SOURCE points at, DTM or DSM, one tileset per source
# file. There is no product parameter anywhere - point TILE_SOURCE at the other
# file and you get the other tileset, in its own db (DB_PATH is derived from the
# source filename, so the two can never end up mixed in one file).
#
# Pipeline per tile: EPSG:3035 source -> reproject to the EPSG:3857 tile grid
# (+1px apron) -> Web Mercator altitude correction -> 3x3 gradient -> ENU normal
# -> hemi-octahedral 8:8 in R/G of an RGB PNG (util/encoding.py, spec in
# docs/normal_map_encoding.md) -> SQLite via util/tile_db.py.
#
# The pyramid is the one place this deliberately diverges from
# tile_creators/cosmos_snow.py, which builds a parent by re-decoding its four
# already-quantized children. Doing that here would re-quantize the normals once
# per zoom level and compound the error all the way down. Instead the reduction
# runs on the exact float normal field, block by block, and PNG encoding is only
# ever a terminal write-out - see _generate_block / _reduce_normals_2x.

import concurrent.futures
import io
import logging
import math
import os
import threading
import time

import numpy as np
import rasterio
import rasterio.warp
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import from_bounds

import processes
from util import encoding
from util import tile_db
from tile_creators import progress

logger = logging.getLogger(__name__)

# The single source raster this tileset is built from. A BEV ALS DTM or DSM
# GeoTIFF: 1 m/px, EPSG:3035, 50x50 km per tile (see scripts/fetch_bev_als.py).
TILE_SOURCE = r"data\als_tiles\DSM\ALS_DSM_CRS3035RES50000mN2650000E4500000.tif"

TILES_DIR = "data/als_normal_tiles"
# One db per source file, named after it, so switching TILE_SOURCE between the
# DTM and the DSM variant of the same area produces two separate tilesets rather
# than silently interleaving them into one.
DB_PATH = os.path.join(
    TILES_DIR, os.path.splitext(os.path.basename(TILE_SOURCE.replace("\\", "/")))[0] + ".db"
)

PROCESS_KEY = "als_normals"
PROCESS_LABEL = "ALS normals ({name})"

# Highest zoom generated. BEV ALS is 1 m/px native, and Web Mercator hits 1 m/px
# at z ~= 16.67 at Austrian latitudes, so z17 (~0.80 m/px ground) is the closest
# 1:1 match and is what scripts/normal_map_playground.ipynb measured.
#
# docs/max_zoomlevel_normal_height_maps.md nonetheless recommends *z16* as the
# default: z17 costs 4x the storage (~7.4 GB vs ~1.8 GB per 50 km source file,
# per product) for detail that is partly noise - PNG only compresses the z17
# normal field by ~28%, and two honest gradient estimators disagree by 0.77 deg
# mean on it. Left at 17 here because that is the configuration the notebook
# actually validated; flipping it is this one line, but flip RESAMPLING with it
# (see below) since z16 turns the reprojection into a downsample.
MAX_ZOOM = 16 #17
MIN_ZOOM = 0

TILE_SIZE = 256

# Zoom at which the generator works one "block" at a time. A block is a single
# BLOCK_ZOOM tile, reprojected in one pass at MAX_ZOOM resolution, and is the
# unit that bounds peak memory: 2^(MAX_ZOOM-BLOCK_ZOOM) tiles per side, so at
# MAX_ZOOM=17 that is 16x16 = a 4096x4096 height array (~67 MB float32) plus its
# normal field (~200 MB). Lower this if that is too much; raising it costs
# proportionally more reprojection calls with more apron overlap.
#
# It is also the level down to which the whole pyramid can be built from a
# single block's float data - every tile at MAX_ZOOM..BLOCK_ZOOM lies entirely
# inside one block, so no cross-block stitching is needed until below it.
BLOCK_ZOOM = 13

# One extra destination pixel per side, so a gradient at the very edge of a tile
# has a real neighbour instead of an edge-clamped copy of itself (which produces
# a visible crease along every tile border).
APRON = 1

# "sobel" (3x3, blends the diagonal neighbours) or "finite_difference" (the
# 4-neighbour method weBIGeo's normal.wgsl uses at draw time). Sobel by default:
# averaging across three rows suppresses part of the per-sample LiDAR noise,
# which is the point of baking normals at source resolution rather than deriving
# them in the shader. The two disagree by 0.77 deg mean / 3.35 deg p99 on real
# alpine z17 data - that spread is the source's own uncertainty, and is what
# justifies the 8-bit encoding (see docs/normal_map_encoding.md).
NORMAL_METHOD = "sobel"

# Resampling used to warp the source onto the tile grid. bilinear is right while
# MAX_ZOOM oversamples the 1 m source (z17 = ~0.80 m/px ground). At z16 or lower
# the warp becomes a *downsample* and this should become Resampling.average,
# which is what actually averages the extra source samples away instead of
# point-sampling past them.
RESAMPLING = Resampling.bilinear

# Set to a number to override the source's declared nodata. BEV GeoTIFFs
# occasionally carry an undeclared sentinel (-9999 and friends) instead of a
# tagged nodata value; the "valid ..%" figure logged per run is the tell - if it
# reads 100% on a tile that clearly has voids, set this.
SOURCE_NODATA = None

# See cosmos_snow.COMMIT_BATCH_SIZE - how many saved tiles accumulate before a
# batch commit, bounding what an ungraceful crash mid-run can lose.
COMMIT_BATCH_SIZE = 500

# CPU-bound (PNG encode + numpy slicing), no network I/O - same sizing rationale
# as cosmos_snow.GENERATION_WORKERS.
GENERATION_WORKERS = os.cpu_count() or 4

# Web Mercator (EPSG:3857) world half-extent in meters, and the sphere radius it
# is defined on - used to turn an XYZ tile index into bounds, and a bounds value
# back into a latitude for the altitude correction.
WEBMERCATOR_ORIGIN = 20037508.342789244
EARTH_RADIUS = 6378137.0
# Web Mercator meters/pixel at zoom 0 on the equator.
WEB_MERCATOR_RES_ZOOM0 = 156543.03392804097

# Derived block geometry.
TILES_PER_BLOCK_SIDE = 2 ** (MAX_ZOOM - BLOCK_ZOOM)
BLOCK_PX = TILES_PER_BLOCK_SIDE * TILE_SIZE
APRON_PX = BLOCK_PX + 2 * APRON
# Tiles one block contributes across MAX_ZOOM..BLOCK_ZOOM (4^k per level).
TILES_PER_BLOCK_TOTAL = sum(4 ** k for k in range(MAX_ZOOM - BLOCK_ZOOM + 1))

_db: tile_db.TileDb | None = None
_lock = threading.Lock()
# Lazily read once by source_bounds(); None is a valid cached answer (an
# unreadable/CRS-less source), hence the separate "has been read" flag.
_source_bounds: tuple[float, float, float, float] | None = None
_source_bounds_read = False

# Public status wording kept stable for the route and docs/map.html, backed by
# the generic processes registry - same mapping as cosmos_snow.
_STATE_TO_STATUS = {
    processes.RUNNING: "generating",
    processes.DONE: "ready",
    processes.ERROR: "error",
}


def source_name() -> str:
    return os.path.basename(TILE_SOURCE.replace("\\", "/"))


def source_bounds() -> tuple[float, float, float, float] | None:
    """(west, south, east, north) WGS84 bounds of TILE_SOURCE, or None if it
    can't be read. Cached after the first call - /v1/als-normals/status is
    polled, and this exists so a client can jump to the coverage instead of
    hunting for it (one 50 km source tile is a small target on a world map).
    Densified because the EPSG:3035 footprint's edges curve in EPSG:4326."""
    global _source_bounds, _source_bounds_read
    with _lock:
        if _source_bounds_read:
            return _source_bounds

    bounds = None
    try:
        with rasterio.open(TILE_SOURCE) as src:
            if src.crs is not None:
                bounds = rasterio.warp.transform_bounds(
                    src.crs, "EPSG:4326", *src.bounds, densify_pts=21
                )
    except Exception:
        logger.warning("als-normals: could not read bounds of %s", TILE_SOURCE, exc_info=True)

    with _lock:
        _source_bounds = bounds
        _source_bounds_read = True
    return bounds


# --------------------------------------------------------------------------
# Lifecycle / public API
# --------------------------------------------------------------------------
def init() -> None:
    """Start generating in a background daemon thread, as debug_ortho does -
    boot is never blocked, and tiles become servable as they land rather than
    only at the end.

    There is no per-tile resume: a block's lower zoom levels are derived from
    its own MAX_ZOOM float field, so there is nothing to resume a partial run
    *from* anyway. Instead, if DB_PATH already has tiles from a prior run,
    generation is skipped entirely - restarting the server does not redo a
    completed tileset."""
    os.makedirs(TILES_DIR, exist_ok=True)
    if not os.path.exists(TILE_SOURCE):
        logger.warning("als-normals: source raster not found, not generating: %s", TILE_SOURCE)
        return

    conn = _open_db()
    if conn.has_tiles():
        logger.info("als-normals: %s already has tiles, skipping generation", DB_PATH)
        processes.start(PROCESS_KEY, PROCESS_LABEL.format(name=source_name()))
        processes.finish(PROCESS_KEY, message="already generated")
        return

    processes.start(PROCESS_KEY, PROCESS_LABEL.format(name=source_name()))
    logger.info("als-normals: generating from %s in background", TILE_SOURCE)
    threading.Thread(target=_generate, args=(conn,), name="als-normals-gen", daemon=True).start()


def get_status() -> str:
    p = processes.get(PROCESS_KEY)
    return _STATE_TO_STATUS[p["state"]] if p else "unknown"


def is_ready() -> bool:
    p = processes.get(PROCESS_KEY)
    return p is not None and p["state"] == processes.DONE


def _open_db() -> tile_db.TileDb:
    global _db
    with _lock:
        if _db is None:
            _db = tile_db.TileDb(DB_PATH, commit_batch_size=COMMIT_BATCH_SIZE)
        return _db


def get_db() -> tile_db.TileDb | None:
    """Read-only counterpart to _open_db: returns None rather than creating an
    empty db file for a tileset nothing has generated yet."""
    with _lock:
        if _db is not None:
            return _db
        if not os.path.exists(DB_PATH):
            return None
    return _open_db()


# --------------------------------------------------------------------------
# Tile index <-> geography
# --------------------------------------------------------------------------
def _tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """XYZ tile -> (left, bottom, right, top) in EPSG:3857 meters, standard
    Google/OSM scheme (y=0 at the north)."""
    size = (2 * WEBMERCATOR_ORIGIN) / 2 ** z
    left = -WEBMERCATOR_ORIGIN + x * size
    top = WEBMERCATOR_ORIGIN - y * size
    return left, top - size, left + size, top


def _point_to_tile_3857(x: float, y: float, z: int) -> tuple[int, int]:
    """EPSG:3857 meters -> the XYZ tile index containing that point, clamped to
    the valid range at zoom z."""
    n = 2 ** z
    size = (2 * WEBMERCATOR_ORIGIN) / n
    tx = int((x + WEBMERCATOR_ORIGIN) // size)
    ty = int((WEBMERCATOR_ORIGIN - y) // size)
    return max(0, min(n - 1, tx)), max(0, min(n - 1, ty))


def _covered_blocks(src: rasterio.DatasetReader) -> list[tuple[int, int]]:
    """Every BLOCK_ZOOM tile that could hold source data, derived from the
    raster's own bounds rather than a hardcoded bbox.

    A 50 km EPSG:3035 tile is not axis-aligned in EPSG:3857, so its bounding box
    in the tile grid necessarily includes corner blocks that hold no data at
    all. Those are cheap to reject here (bbox overlap in the source CRS, with
    densified edges so the reprojected curve is not under-estimated); whatever
    survives is checked for real by the per-tile validity mask later."""
    left, bottom, right, top = rasterio.warp.transform_bounds(
        src.crs, "EPSG:3857", *src.bounds, densify_pts=21
    )
    x0, y0 = _point_to_tile_3857(left, top, BLOCK_ZOOM)
    x1, y1 = _point_to_tile_3857(right, bottom, BLOCK_ZOOM)

    sb = src.bounds
    blocks = []
    for by in range(y0, y1 + 1):
        for bx in range(x0, x1 + 1):
            minx, miny, maxx, maxy = rasterio.warp.transform_bounds(
                "EPSG:3857", src.crs, *_tile_bounds_3857(BLOCK_ZOOM, bx, by), densify_pts=21
            )
            if maxx <= sb.left or minx >= sb.right or maxy <= sb.bottom or miny >= sb.top:
                continue
            blocks.append((bx, by))
    return blocks


# --------------------------------------------------------------------------
# Source read
# --------------------------------------------------------------------------
def _read_block_heights(src: rasterio.DatasetReader, bx: int, by: int) -> np.ndarray:
    """One BLOCK_ZOOM block -> (APRON_PX, APRON_PX) float32 heights in meters,
    NaN where there is no source data, with the Web Mercator altitude correction
    already applied.

    NaN rather than 0 for voids matters: a zero would read as sea level and
    fabricate a cliff along every coverage border.

    Note this warps the block properly instead of taking cosmos_snow's
    windowed-resample shortcut - across 50 km the EPSG:3035 -> EPSG:3857 skew is
    far too large to approximate with an axis-aligned window read."""
    left, bottom, right, top = _tile_bounds_3857(BLOCK_ZOOM, bx, by)
    px = (right - left) / BLOCK_PX
    apron_bounds = (left - px * APRON, bottom - px * APRON, right + px * APRON, top + px * APRON)
    dst_transform = from_bounds(*apron_bounds, APRON_PX, APRON_PX)

    # init_dest_nodata defaults to True, so anything the warp never touches
    # (i.e. outside the source footprint) keeps dst_nodata.
    dst = np.full((APRON_PX, APRON_PX), np.nan, dtype=np.float32)
    rasterio.warp.reproject(
        source=rasterio.band(src, 1),
        destination=dst,
        src_transform=src.transform,
        src_crs=src.crs,
        src_nodata=SOURCE_NODATA if SOURCE_NODATA is not None else src.nodata,
        dst_transform=dst_transform,
        dst_crs="EPSG:3857",
        dst_nodata=np.nan,
        resampling=RESAMPLING,
    )

    # Web Mercator inflates horizontal distances away from the equator by
    # 1/cos(lat). weBIGeo folds that into the height rather than the quad size
    # (AlpineMapsOrg/renderer#5), so the raw Web Mercator quad width can be used
    # unchanged below and the resulting normal is still the true metric one.
    # Done per destination row - exact, and no more expensive than one constant.
    ys = apron_bounds[3] - (np.arange(APRON_PX, dtype=np.float64) + 0.5) * px
    lat = 2.0 * np.arctan(np.exp(ys / EARTH_RADIUS)) - math.pi / 2.0
    dst *= (1.0 / np.cos(lat)).astype(np.float32)[:, None]
    return dst


def _stencil_valid(height_apron: np.ndarray) -> np.ndarray:
    """(APRON_PX, APRON_PX) heights -> (BLOCK_PX, BLOCK_PX) bool. A normal is
    only trusted where its full 3x3 height stencil was valid, so a void never
    leaks a fabricated gradient into a neighbouring real pixel."""
    ok = np.isfinite(height_apron)
    h, w = ok.shape
    out = np.ones((h - 2, w - 2), dtype=bool)
    for dy in range(3):
        for dx in range(3):
            out &= ok[dy:dy + h - 2, dx:dx + w - 2]
    return out


# --------------------------------------------------------------------------
# Height field -> ENU normals
# --------------------------------------------------------------------------
# Both methods return +X east, +Y north, +Z up. Slippy-map rows increase
# *southward*, which is why the south neighbour is the one that enters the +Y
# component un-negated in both. See docs/normal_map_encoding.md.
def _normal_by_sobel(h: np.ndarray, quad_width: float, quad_height: float) -> np.ndarray:
    tl, tm, tr = h[:-2, :-2], h[:-2, 1:-1], h[:-2, 2:]
    ml, mr = h[1:-1, :-2], h[1:-1, 2:]
    bl, bm, br = h[2:, :-2], h[2:, 1:-1], h[2:, 2:]

    dzdx = (-(tl + 2 * ml + bl) + (tr + 2 * mr + br)) / 8.0 / quad_width
    dzdy = (-(tl + 2 * tm + tr) + (bl + 2 * bm + br)) / 8.0 / quad_height

    # dzdy is the southward derivative, i.e. already -dh/dy_north, so it enters
    # un-negated while dzdx (eastward) is negated.
    return np.stack([-dzdx, dzdy, np.ones_like(dzdx)], axis=-1)


def _normal_by_finite_difference(h: np.ndarray, quad_width: float, quad_height: float) -> np.ndarray:
    nx = (h[1:-1, :-2] - h[1:-1, 2:]) / quad_width
    ny = (h[2:, 1:-1] - h[:-2, 1:-1]) / quad_height
    return np.stack([nx, ny, np.full_like(nx, 2.0)], axis=-1)


def _block_normals(height_apron: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """(APRON_PX, APRON_PX) heights -> (BLOCK_PX, BLOCK_PX, 3) float32 unit ENU
    normals. Invalid pixels come back as the zero vector, which is the invariant
    _reduce_normals_2x relies on to exclude them from an average."""
    # Voids are filled with 0 only so the stencil arithmetic stays finite - every
    # output pixel that touched one is already False in `valid` and is zeroed
    # below, so the fabricated gradient never survives.
    h = np.where(np.isfinite(height_apron), height_apron, np.float32(0.0))

    quad = WEB_MERCATOR_RES_ZOOM0 / 2 ** MAX_ZOOM
    if NORMAL_METHOD == "sobel":
        normal = _normal_by_sobel(h, quad, quad)
    elif NORMAL_METHOD == "finite_difference":
        normal = _normal_by_finite_difference(h, quad, quad)
    else:
        raise ValueError(f"unknown NORMAL_METHOD {NORMAL_METHOD!r}")

    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    normal = normal.astype(np.float32)
    normal[~valid] = 0.0
    return normal


# --------------------------------------------------------------------------
# Float-space pyramid
# --------------------------------------------------------------------------
def _reduce_normals_2x(normals: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(H,W,3) unit normals + (H,W) validity -> the same at half resolution.

    Averages the four child vectors and renormalizes. Invalid children are zero
    vectors (see _block_normals) so they contribute nothing to the sum - a void
    never drags a real normal toward a fabricated direction - and an output is
    valid iff at least one of its four inputs was. A fully invalid output stays
    the zero vector and is turned into the flat normal at encode time.

    All of this happens on exact float data: unlike cosmos_snow, no parent is
    ever built by decoding an already-quantized child, so quantization error
    does not accumulate down the pyramid."""
    h, w = valid.shape
    total = normals.reshape(h // 2, 2, w // 2, 2, 3).sum(axis=(1, 3))
    out_valid = valid.reshape(h // 2, 2, w // 2, 2).any(axis=(1, 3))
    norm = np.linalg.norm(total, axis=-1, keepdims=True)
    out = np.divide(total, norm, out=np.zeros_like(total), where=norm > 0)
    return out.astype(np.float32), out_valid


# --------------------------------------------------------------------------
# PNG encode
# --------------------------------------------------------------------------
def _encode_tile(normals: np.ndarray, valid: np.ndarray) -> bytes:
    """(TILE_SIZE, TILE_SIZE, 3) normals -> RGB PNG bytes. All of the format
    lives in util/encoding.py; nothing here knows how a normal becomes a byte."""
    rgb = encoding.encode_normals(normals, valid=valid)
    buf = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def _save_tile(conn: tile_db.TileDb, z: int, x: int, y: int, normals: np.ndarray, valid: np.ndarray) -> bool:
    """Worker: encode and store one tile. Returns True if it was written.

    Runs on a worker thread - the block arrays are never mutated once the
    normals are computed, so concurrent slicing needs no lock, and TileDb's own
    lock covers the save."""
    if not valid.any():
        # Entirely outside the source footprint. Writing an all-flat tile here
        # would be indistinguishable from real flat ground and would bloat the
        # db with the whole non-axis-aligned corner of every 3035 source tile.
        return False
    conn.save_tile(z, x, y, _encode_tile(normals, valid))
    return True


def _save_level(
    conn: tile_db.TileDb, executor: concurrent.futures.Executor, z: int,
    normals: np.ndarray, valid: np.ndarray, tx0: int, ty0: int,
    key: str, total: int, done: int, rate: progress.RateTracker,
) -> tuple[int, int]:
    """Slice one block's float normal field at zoom `z` into TILE_SIZE tiles and
    write them in parallel. (tx0, ty0) is the tile index of the field's top-left
    corner at that zoom. Returns the updated (done, written_this_level)."""
    per_side = valid.shape[0] // TILE_SIZE
    futures = {}
    for j in range(per_side):
        for i in range(per_side):
            rows = slice(j * TILE_SIZE, (j + 1) * TILE_SIZE)
            cols = slice(i * TILE_SIZE, (i + 1) * TILE_SIZE)
            futures[executor.submit(
                _save_tile, conn, z, tx0 + i, ty0 + j, normals[rows, cols], valid[rows, cols]
            )] = (i, j)

    written = 0
    for future in concurrent.futures.as_completed(futures):
        written += bool(future.result())  # re-raises a worker exception here
        done += 1
        processes.update(key, done, message=f"{done}/{total} tiles (z={z}, {rate.sample(done)})")
    return done, written


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _generate_block(
    conn: tile_db.TileDb, executor: concurrent.futures.Executor, src: rasterio.DatasetReader,
    bx: int, by: int, key: str, total: int, done: int, rate: progress.RateTracker,
) -> tuple[int, int, tuple[np.ndarray, np.ndarray] | None]:
    """Reproject, differentiate and write one block across MAX_ZOOM..BLOCK_ZOOM.

    Returns (done, written, block_state), where block_state is the block's
    BLOCK_ZOOM-level float normals + validity - kept by the caller so the levels
    above BLOCK_ZOOM can be reduced from exact data too - or None if the block
    turned out to hold no source data after all."""
    heights = _read_block_heights(src, bx, by)
    valid = _stencil_valid(heights)
    if not valid.any():
        # Survived the bbox pre-check but is genuinely empty (the 3035 footprint
        # is a rotated quad in this grid). Count its tiles as done so the
        # progress bar still reaches total.
        return done + TILES_PER_BLOCK_TOTAL, 0, None

    normals = _block_normals(heights, valid)
    del heights

    written = 0
    for z in range(MAX_ZOOM, BLOCK_ZOOM - 1, -1):
        if z != MAX_ZOOM:
            normals, valid = _reduce_normals_2x(normals, valid)
        per_side = 2 ** (z - BLOCK_ZOOM)
        done, w = _save_level(
            conn, executor, z, normals, valid, bx * per_side, by * per_side, key, total, done, rate
        )
        written += w

    return done, written, (normals, valid)


def _generate_upper_levels(
    conn: tile_db.TileDb, executor: concurrent.futures.Executor,
    cache: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]],
    key: str, total: int, done: int, rate: progress.RateTracker,
) -> tuple[int, int]:
    """Build BLOCK_ZOOM-1 .. MIN_ZOOM from the per-block float arrays.

    Below BLOCK_ZOOM a parent spans four blocks, so this is where the pyramid
    stops being block-local - but it is still the same float reduction, just fed
    from the cached arrays instead of a freshly warped one. A missing quadrant
    (a block that held no data) stays invalid, so coverage edges degrade to flat
    rather than to garbage."""
    half = TILE_SIZE // 2
    written = 0
    for z in range(BLOCK_ZOOM - 1, MIN_ZOOM - 1, -1):
        parents = {(x // 2, y // 2) for x, y in cache}
        level: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}
        futures = {}
        for (x, y) in sorted(parents):
            normals = np.zeros((TILE_SIZE, TILE_SIZE, 3), dtype=np.float32)
            valid = np.zeros((TILE_SIZE, TILE_SIZE), dtype=bool)
            for dy in (0, 1):
                for dx in (0, 1):
                    child = cache.get((x * 2 + dx, y * 2 + dy))
                    if child is None:
                        continue
                    cn, cv = _reduce_normals_2x(*child)
                    normals[dy * half:(dy + 1) * half, dx * half:(dx + 1) * half] = cn
                    valid[dy * half:(dy + 1) * half, dx * half:(dx + 1) * half] = cv
            level[(x, y)] = (normals, valid)
            futures[executor.submit(_save_tile, conn, z, x, y, normals, valid)] = (x, y)

        for future in concurrent.futures.as_completed(futures):
            written += bool(future.result())
            done += 1
            processes.update(key, done, message=f"{done}/{total} tiles (z={z}, {rate.sample(done)})")
        conn.commit()
        cache = level
    return done, written


def _count_total(blocks: list[tuple[int, int]]) -> int:
    """Upper bound on tiles to be visited, for the progress bar. Blocks and
    tiles that turn out to be empty are still counted as done when skipped, so
    the bar reaches total exactly even though fewer tiles get written."""
    total = len(blocks) * TILES_PER_BLOCK_TOTAL
    level = set(blocks)
    for _ in range(BLOCK_ZOOM - MIN_ZOOM):
        level = {(x // 2, y // 2) for x, y in level}
        total += len(level)
    return total


def _generate(conn: tile_db.TileDb) -> None:
    key = PROCESS_KEY
    try:
        t_start = time.monotonic()
        with rasterio.open(TILE_SOURCE) as src:
            logger.info(
                "als-normals: source %s crs=%s nodata=%s dtype=%s shape=%s",
                TILE_SOURCE, src.crs, src.nodata, src.dtypes[0], src.shape,
            )
            if src.crs is None:
                raise RuntimeError(
                    f"als-normals: {TILE_SOURCE} has no CRS - cannot georeference tiles "
                    f"(scripts/fetch_bev_als.py stamps EPSG:3035 onto BEV downloads that omit it)"
                )

            blocks = _covered_blocks(src)
            if not blocks:
                raise RuntimeError(f"als-normals: no tiles cover the source bounds of {TILE_SOURCE}")
            total = _count_total(blocks)
            logger.info(
                "als-normals: %d block(s) at z%d, %d tile(s) across z%d..z%d, %dx%d px per block",
                len(blocks), BLOCK_ZOOM, total, MIN_ZOOM, MAX_ZOOM, BLOCK_PX, BLOCK_PX,
            )

            done = 0
            written = 0
            rate = progress.RateTracker()
            processes.update(key, done, total=total, message=f"0/{total} tiles")

            cache: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=GENERATION_WORKERS) as executor:
                for bx, by in blocks:
                    done, w, state = _generate_block(
                        conn, executor, src, bx, by, key, total, done, rate
                    )
                    written += w
                    if state is not None:
                        cache[(bx, by)] = state
                    conn.commit()

                # ~0.8 MB per block held until the levels below BLOCK_ZOOM are
                # built - a few hundred MB for a full 50 km source file.
                logger.info(
                    "als-normals: z%d..z%d done (%d tiles written), reducing %d cached block(s)",
                    BLOCK_ZOOM, MAX_ZOOM, written, len(cache),
                )
                done, w = _generate_upper_levels(conn, executor, cache, key, total, done, rate)
                written += w

        conn.commit()
        elapsed = time.monotonic() - t_start
        processes.finish(key, message=f"{written} tiles in {elapsed:.1f}s")
        logger.info("als-normals: tileset ready (%d tiles) in %.1fs", written, elapsed)
    except Exception:
        logger.exception("als-normals: generation failed")
        try:
            conn.commit()  # persist whatever tiles succeeded before the failure
        except Exception:
            logger.exception("als-normals: failed to flush partial progress after error")
        processes.fail(key, message="generation failed, check server log")
