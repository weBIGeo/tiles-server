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

import concurrent.futures
import datetime as dt
import io
import logging
import math
import os
import re
import threading
import time

import boto3
import numpy as np
import rasterio
import rasterio.warp
import rasterio.windows
from botocore.exceptions import ClientError
from PIL import Image
from rasterio.enums import Resampling

from const import BBOXES

import config
import processes
import tile_db
from tile_creators import progress

logger = logging.getLogger(__name__)

# One SQLite db per day, named "<YYYY-MM-DD>.db", under this directory.
TILES_DIR = "data/snow_tiles"
# Cached source GeoTIFFs, one per day, under this subdirectory - each is the
# fixed snapshot for that historical date, so once downloaded it never
# changes and is cached indefinitely.
SOURCE_DIR = "data/snow_tiles/source"

PROCESS_KEY_TEMPLATE = "cosmos_snow_{date}"
PROCESS_LABEL_TEMPLATE = "COSMOS snow depth ({date})"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# S3 key layout per the exolabs gitbook docs (dated-folder, NOT the flat
# overwritten-daily layout this repo originally assumed - see
# docs/cosmos-api.md and the S3 bucket/prefix settings in config.py):
#   https://exolabs-ch.gitbook.io/cosmos/raw-data-listing#snow-depth
#   <ROI>/<YYYY-MM-DD>/<YYYY-MM-DD>+000_<ROI>_HS_product.tif
# "+000" is the analysis/nowcast product, not a +024/+048 forecast variant.
S3_KEY_TEMPLATE = "{roi}/{date}/{date}+000_{roi}_HS_product.tif"

BBOX = BBOXES['AUSTRIA']

MIN_ZOOM = 0
# Source GeoTIFF is 20m/px (EPSG:3857). Web Mercator resolution is
# 156543.03392/2^z m/px: z13 ~= 19.1m/px (finer than native, full detail
# captured), z12 ~= 38.2m/px (coarser - would lose detail). z13 is therefore
# the highest zoom that reflects real resolution rather than upsampling.
MAX_ZOOM = 13

TILE_SIZE = 256

# _save_tile() no longer commits on every call (that meant a disk sync per
# tile - thousands of them per generation run); this is how many saved tiles
# accumulate before a batch commit, bounding how much work an ungraceful
# crash mid-run could lose.
COMMIT_BATCH_SIZE = 500

# Concurrent slice/encode/save (z=MAX_ZOOM) and decode/downsample/encode/save
# (z<MAX_ZOOM) workers. Unlike debug_ortho's DOWNLOAD_WORKERS=16 (sized for
# HTTP latency hiding), this workload is CPU-bound (PNG encode, numpy
# slicing/downsampling) with no network I/O, so it's sized off core count.
GENERATION_WORKERS = os.cpu_count() or 4

# Web Mercator (EPSG:3857) world half-extent in meters - used to convert an
# XYZ tile index directly into its bounds in that CRS.
WEBMERCATOR_ORIGIN = 20037508.342789244

# date -> open TileDb. Guarded by _lock, which protects this registry and
# _generating below - TileDb itself is internally thread-safe, so _lock no
# longer needs to (and doesn't) guard individual tile reads/writes.
_dbs: dict[str, tile_db.TileDb] = {}
# Dates whose background generation thread is currently running, so a
# duplicate /generate call for the same date is a no-op instead of spawning
# a second thread racing the first.
_generating: set[str] = set()
_lock = threading.Lock()

# Public status wording kept stable for /v1/cosmos-snow/<date>/status and
# docs/map.html, backed by the generic processes registry.
_STATE_TO_STATUS = {
    processes.RUNNING: "generating",
    processes.DONE: "ready",
    processes.ERROR: "error",
}


def _db_path(date: str) -> str:
    return os.path.join(TILES_DIR, f"{date}.db")


def _source_path(date: str) -> str:
    return os.path.join(SOURCE_DIR, f"{date}.tif")


def _validate_date(date: str) -> None:
    if not _DATE_RE.match(date):
        raise ValueError(f"invalid date {date!r}, expected YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(date)
    except ValueError as e:
        raise ValueError(f"invalid date {date!r}: {e}") from e
    if parsed > dt.date.today():
        raise ValueError(f"date {date!r} is in the future - no COSMOS analysis product exists for it yet")


def init() -> None:
    """Create the snow-tiles directories and register every already-generated
    date found on disk as ready, without kicking off any new generation.
    Generation for a given date only starts on demand via start_generation()
    (see the /v1/cosmos-snow/<date>/generate route) - there's no fixed date
    this module auto-generates on startup."""
    os.makedirs(TILES_DIR, exist_ok=True)
    os.makedirs(SOURCE_DIR, exist_ok=True)

    dates = _dates_on_disk()
    for date in dates:
        _open_db(date)
        key = PROCESS_KEY_TEMPLATE.format(date=date)
        if processes.get(key) is None:
            processes.start(key, PROCESS_LABEL_TEMPLATE.format(date=date))
            processes.finish(key, message="loaded from disk")
    logger.info("cosmos-snow: loaded %d existing date(s) from %s", len(dates), TILES_DIR)


def _dates_on_disk() -> list[str]:
    if not os.path.isdir(TILES_DIR):
        return []
    return sorted(
        fn[:-3] for fn in os.listdir(TILES_DIR)
        if fn.endswith(".db") and _DATE_RE.match(fn[:-3])
    )


def list_dates() -> list[dict]:
    return [{"date": d, "status": get_status(d)} for d in _dates_on_disk()]


def start_generation(date: str) -> str:
    """Kick off (or resume) tile generation for `date` in a background
    thread, or return the current status if it's already generating/ready.
    Never blocks - returns immediately with "generating" or "ready"."""
    _validate_date(date)
    key = PROCESS_KEY_TEMPLATE.format(date=date)

    with _lock:
        if date in _generating:
            return "generating"
        p = processes.get(key)
        if p is not None and p["state"] == processes.DONE:
            return "ready"
        _generating.add(date)

    _open_db(date)
    processes.start(key, PROCESS_LABEL_TEMPLATE.format(date=date))
    logger.info("cosmos-snow[%s]: starting generation in background", date)
    threading.Thread(target=_generate_for_date, args=(date,), name=f"cosmos-snow-gen-{date}", daemon=True).start()
    return "generating"


def get_status(date: str) -> str:
    p = processes.get(PROCESS_KEY_TEMPLATE.format(date=date))
    return _STATE_TO_STATUS[p["state"]] if p else "unknown"


def is_ready(date: str) -> bool:
    p = processes.get(PROCESS_KEY_TEMPLATE.format(date=date))
    return p is not None and p["state"] == processes.DONE


def _open_db(date: str) -> tile_db.TileDb:
    """Open (creating if needed) the db for `date`, registering it in _dbs."""
    with _lock:
        conn = _dbs.get(date)
        if conn is not None:
            return conn
        conn = tile_db.TileDb(_db_path(date), commit_batch_size=COMMIT_BATCH_SIZE)
        _dbs[date] = conn
        return conn


def _get_db(date: str) -> tile_db.TileDb | None:
    """Like _open_db, but read-only: returns None instead of creating a new
    (empty) db file for a date nothing has ever generated."""
    with _lock:
        conn = _dbs.get(date)
        if conn is not None:
            return conn
        if not os.path.exists(_db_path(date)):
            return None
    return _open_db(date)


def get_tile(date: str, z: int, x: int, y: int) -> bytes | None:
    conn = _get_db(date)
    return conn.get_tile(z, x, y) if conn is not None else None


def _save_tile(date: str, z: int, x: int, y: int, data: bytes) -> None:
    """Queues the tile, auto-committing every COMMIT_BATCH_SIZE saves (see
    TileDb.save_tile) - callers can still force an early flush via _commit()."""
    _dbs[date].save_tile(z, x, y, data)


def _commit(date: str) -> None:
    _dbs[date].commit()


def _tile_exists(date: str, z: int, x: int, y: int) -> bool:
    return _dbs[date].tile_exists(z, x, y)


# --------------------------------------------------------------------------
# Tile index <-> geography
# --------------------------------------------------------------------------
def _deg2tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    """WGS84 lon/lat -> XYZ (Google/OSM scheme) tile indices at zoom z."""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def _tile_range(bbox: tuple[float, float, float, float], z: int) -> tuple[int, int, int, int]:
    west, south, east, north = bbox
    x0, y0 = _deg2tile(north, west, z)  # top-left
    x1, y1 = _deg2tile(south, east, z)  # bottom-right
    return min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)


def _tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """XYZ tile -> (left, bottom, right, top) in EPSG:3857 meters, standard
    Google/OSM scheme (y=0 at the north)."""
    n = 2 ** z
    size = (2 * WEBMERCATOR_ORIGIN) / n
    left = -WEBMERCATOR_ORIGIN + x * size
    right = left + size
    top = WEBMERCATOR_ORIGIN - y * size
    bottom = top - size
    return left, bottom, right, top


def _tile_range_bounds_3857(z: int, xmin: int, xmax: int, ymin: int, ymax: int) -> tuple[float, float, float, float]:
    """Union (left, bottom, right, top) in EPSG:3857 meters of every tile in
    an [xmin,xmax]x[ymin,ymax] range - since the tile grid is contiguous and
    gap-free, this is just the top-left tile's (left, top) and the
    bottom-right tile's (right, bottom)."""
    left, _, _, top = _tile_bounds_3857(z, xmin, ymin)
    _, bottom, right, _ = _tile_bounds_3857(z, xmax, ymax)
    return left, bottom, right, top


# --------------------------------------------------------------------------
# S3 download
# --------------------------------------------------------------------------
def _ensure_source_tif(date: str) -> str:
    """Return a local path to `date`'s source GeoTIFF, downloading it from S3
    if not already cached."""
    path = _source_path(date)
    if os.path.exists(path):
        return path

    os.makedirs(SOURCE_DIR, exist_ok=True)
    key = S3_KEY_TEMPLATE.format(roi=config.cosmos_s3_prefix, date=date)
    tmp_path = path + ".part"

    logger.info("cosmos-snow[%s]: downloading s3://%s/%s", date, config.cosmos_s3_bucket, key)
    s3 = boto3.client(
        "s3",
        aws_access_key_id=config.cosmos_aws_access_key_id,
        aws_secret_access_key=config.cosmos_aws_secret_access_key,
        region_name=getattr(config, "cosmos_aws_region", None),
    )
    try:
        s3.download_file(config.cosmos_s3_bucket, key, tmp_path)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if code in ("404", "NoSuchKey"):
            raise RuntimeError(
                f"cosmos-snow[{date}]: S3 key not found: s3://{config.cosmos_s3_bucket}/{key} "
                f"(the dated-folder layout from the exolabs gitbook docs may not match "
                f"reality for this date - see https://exolabs-ch.gitbook.io/cosmos/raw-data-listing#snow-depth ; "
                f"try `aws s3 ls s3://{config.cosmos_s3_bucket}/{config.cosmos_s3_prefix}/{date}/` "
                f"to see what's actually there)"
            ) from e
        raise

    os.replace(tmp_path, path)
    return path


# --------------------------------------------------------------------------
# Raster read
# --------------------------------------------------------------------------
def _read_mosaic_from_raster(
    src: rasterio.DatasetReader, z: int, xmin: int, xmax: int, ymin: int, ymax: int
) -> tuple[np.ndarray, np.ndarray]:
    """Windowed read of an entire tile range in one pass, tile-grid-aligned
    so each tile is a plain slice of the result - one bulk read instead of
    one small windowed read per output tile. Returns (value_cm: uint16
    array, valid: bool array), each shaped
    ((ymax-ymin+1)*TILE_SIZE, (xmax-xmin+1)*TILE_SIZE)."""
    left, bottom, right, top = _tile_range_bounds_3857(z, xmin, xmax, ymin, ymax)
    minx, miny, maxx, maxy = rasterio.warp.transform_bounds(
        "EPSG:3857", src.crs, left, bottom, right, top
    )
    window = rasterio.windows.from_bounds(minx, miny, maxx, maxy, transform=src.transform)

    mosaic_h = (ymax - ymin + 1) * TILE_SIZE
    mosaic_w = (xmax - xmin + 1) * TILE_SIZE
    data = src.read(
        1,
        window=window,
        out_shape=(mosaic_h, mosaic_w),
        resampling=Resampling.nearest,
        boundless=True,
        masked=True,
    )
    valid = ~np.ma.getmaskarray(data)
    value_cm = np.clip(data.filled(0), 0, None).astype(np.uint16)
    return value_cm, valid


# --------------------------------------------------------------------------
# PNG encode/decode
# --------------------------------------------------------------------------
def _encode_tile(value_cm: np.ndarray, valid: np.ndarray) -> bytes:
    """value_cm: uint16 (256,256) depth in cm. valid: bool (256,256), True
    where the source pixel was not nodata. R = hi byte, G = lo byte, B = 0,
    A = 255 where valid and non-zero, else 0 (transparent)."""
    r = (value_cm >> 8).astype(np.uint8)
    g = (value_cm & 0xFF).astype(np.uint8)
    b = np.zeros_like(r)
    a = np.where(valid & (value_cm != 0), 255, 0).astype(np.uint8)
    img = Image.fromarray(np.dstack([r, g, b, a]), mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _decode_tile(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of _encode_tile, used only by the mipmap builder below."""
    arr = np.array(Image.open(io.BytesIO(data)).convert("RGBA"))
    value_cm = (arr[..., 0].astype(np.uint16) << 8) | arr[..., 1].astype(np.uint16)
    valid = arr[..., 3] > 0
    return value_cm, valid


# --------------------------------------------------------------------------
# Mipmap (zoom levels below MAX_ZOOM)
# --------------------------------------------------------------------------
def _downsample_2x2(value_cm: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """256x256 -> 128x128 box downsample in numeric cm-space. An output pixel
    is valid if >=1 of its 4 input pixels is valid, and its value is the mean
    of just the valid ones - so a nodata/void neighbor never drags a real
    value toward a fake number."""
    v = value_cm.reshape(128, 2, 128, 2).astype(np.float64)
    m = valid.reshape(128, 2, 128, 2)
    count = m.sum(axis=(1, 3))
    total = np.where(m, v, 0).sum(axis=(1, 3))
    avg = np.divide(total, count, out=np.zeros_like(total), where=count > 0)
    return np.round(avg).astype(np.uint16), count > 0


def _compose_parent_tile(children: dict[tuple[int, int], bytes | None]) -> tuple[np.ndarray, np.ndarray]:
    """children keyed by (dx,dy) in {0,1}x{0,1} (child = parent*2+dx/dy, same
    quadrant convention as debug_ortho's SYNTH_ZOOM step) -> PNG bytes, or
    None if that child tile doesn't exist - treated as a fully
    transparent/nodata quadrant so edges degrade gracefully."""
    out_value = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint16)
    out_valid = np.zeros((TILE_SIZE, TILE_SIZE), dtype=bool)
    for (dx, dy), data in children.items():
        if data is None:
            continue
        qv, qm = _downsample_2x2(*_decode_tile(data))
        out_value[dy * 128:(dy + 1) * 128, dx * 128:(dx + 1) * 128] = qv
        out_valid[dy * 128:(dy + 1) * 128, dx * 128:(dx + 1) * 128] = qm
    return out_value, out_valid


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _process_mosaic_tile(
    value_cm_mosaic: np.ndarray, valid_mosaic: np.ndarray, xmin: int, ymin: int, date: str, x: int, y: int
) -> None:
    """Worker: slice one tile out of the bulk-read MAX_ZOOM mosaic, encode
    and save it. Runs on a worker thread - the mosaic arrays are never
    mutated after the read, so concurrent slicing needs no lock; touches
    only this tile's own slice/save otherwise."""
    row0 = (y - ymin) * TILE_SIZE
    col0 = (x - xmin) * TILE_SIZE
    value_cm = value_cm_mosaic[row0:row0 + TILE_SIZE, col0:col0 + TILE_SIZE]
    valid = valid_mosaic[row0:row0 + TILE_SIZE, col0:col0 + TILE_SIZE]
    _save_tile(date, MAX_ZOOM, x, y, _encode_tile(value_cm, valid))


def _generate_max_zoom_tiles(
    date: str, src: rasterio.DatasetReader, xmin: int, xmax: int, ymin: int, ymax: int,
    key: str, total: int, done: int, rate: progress.RateTracker,
) -> int:
    """Generate every MAX_ZOOM tile in [xmin,xmax]x[ymin,ymax]. Tiles already
    in the db are skipped (resume support) without touching the raster at
    all; if anything is missing, the whole range is read from the source
    GeoTIFF in one bulk pass (_read_mosaic_from_raster) and the missing
    tiles are sliced/encoded/saved in parallel - one disk read total instead
    of one per tile."""
    tasks = []
    for x in range(xmin, xmax + 1):
        for y in range(ymin, ymax + 1):
            if _tile_exists(date, MAX_ZOOM, x, y):
                done += 1
                processes.update(key, done, message=f"{done}/{total} tiles generated (z={MAX_ZOOM}, {rate.sample(done)})")
            else:
                tasks.append((x, y))

    if not tasks:
        return done

    value_cm_mosaic, valid_mosaic = _read_mosaic_from_raster(src, MAX_ZOOM, xmin, xmax, ymin, ymax)
    with concurrent.futures.ThreadPoolExecutor(max_workers=GENERATION_WORKERS) as executor:
        futures = {
            executor.submit(_process_mosaic_tile, value_cm_mosaic, valid_mosaic, xmin, ymin, date, x, y): (x, y)
            for x, y in tasks
        }
        for future in concurrent.futures.as_completed(futures):
            future.result()  # re-raise a worker exception on the main thread
            done += 1
            processes.update(key, done, message=f"{done}/{total} tiles generated (z={MAX_ZOOM}, {rate.sample(done)})")
    return done


def _process_pyramid_tile(date: str, z: int, x: int, y: int) -> None:
    """Worker: compose one lower-zoom tile from its 4 already-saved z+1
    children, encode and save it. Runs on a worker thread - get_tile/
    _save_tile go through TileDb's own lock, so concurrent calls are safe."""
    children = {
        (dx, dy): get_tile(date, z + 1, x * 2 + dx, y * 2 + dy)
        for dx in (0, 1) for dy in (0, 1)
    }
    value_cm, valid = _compose_parent_tile(children)
    _save_tile(date, z, x, y, _encode_tile(value_cm, valid))


def _generate_pyramid_level(
    date: str, z: int, xmin: int, xmax: int, ymin: int, ymax: int,
    key: str, total: int, done: int, rate: progress.RateTracker,
) -> int:
    """Generate every tile at zoom `z` in [xmin,xmax]x[ymin,ymax] from its
    already-saved z+1 children, in parallel. Must only be called after zoom
    z+1 is fully generated and committed."""
    tasks = []
    for x in range(xmin, xmax + 1):
        for y in range(ymin, ymax + 1):
            if _tile_exists(date, z, x, y):
                done += 1
                processes.update(key, done, message=f"{done}/{total} tiles generated (z={z}, {rate.sample(done)})")
            else:
                tasks.append((x, y))

    if not tasks:
        return done

    with concurrent.futures.ThreadPoolExecutor(max_workers=GENERATION_WORKERS) as executor:
        futures = {executor.submit(_process_pyramid_tile, date, z, x, y): (x, y) for x, y in tasks}
        for future in concurrent.futures.as_completed(futures):
            future.result()  # re-raise a worker exception on the main thread
            done += 1
            processes.update(key, done, message=f"{done}/{total} tiles generated (z={z}, {rate.sample(done)})")
    return done


def _generate_for_date(date: str) -> None:
    key = PROCESS_KEY_TEMPLATE.format(date=date)
    try:
        start = time.monotonic()
        tif_path = _ensure_source_tif(date)

        zoom_ranges = {z: _tile_range(BBOX, z) for z in range(MIN_ZOOM, MAX_ZOOM + 1)}
        total = sum(
            (xmax - xmin + 1) * (ymax - ymin + 1) for xmin, xmax, ymin, ymax in zoom_ranges.values()
        )
        done = 0
        rate = progress.RateTracker()
        processes.update(key, done, total=total, message=f"{done}/{total} tiles generated")

        with rasterio.open(tif_path) as src:
            logger.info(
                "cosmos-snow[%s]: source crs=%s nodata=%s dtype=%s shape=%s",
                date, src.crs, src.nodata, src.dtypes[0], src.shape,
            )
            if src.crs is None:
                raise RuntimeError(f"cosmos-snow[{date}]: source GeoTIFF has no CRS - cannot georeference tiles")

            xmin, xmax, ymin, ymax = zoom_ranges[MAX_ZOOM]
            done = _generate_max_zoom_tiles(date, src, xmin, xmax, ymin, ymax, key, total, done, rate)
        _commit(date)  # flush the tail of this zoom level

        for z in range(MAX_ZOOM - 1, MIN_ZOOM - 1, -1):
            xmin, xmax, ymin, ymax = zoom_ranges[z]
            done = _generate_pyramid_level(date, z, xmin, xmax, ymin, ymax, key, total, done, rate)
            _commit(date)  # flush the tail of this zoom level

        elapsed = time.monotonic() - start
        processes.finish(key, message=f"{done} tiles in {elapsed:.1f}s")
        logger.info("cosmos-snow[%s]: dataset ready (%d tiles) in %.1fs", date, done, elapsed)
    except Exception:
        logger.exception("cosmos-snow[%s]: generation failed", date)
        try:
            _commit(date)  # persist whatever tiles succeeded before the failure
        except Exception:
            logger.exception("cosmos-snow[%s]: failed to flush partial progress after error", date)
        processes.fail(key, message="generation failed, check server log")
    finally:
        with _lock:
            _generating.discard(date)
