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

import io
import logging
import math
import os
import sqlite3
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

import config
import processes

logger = logging.getLogger(__name__)

PROCESS_KEY = "cosmos_snow"
PROCESS_LABEL = "COSMOS snow depth tiles"

# Path to the SQLite database file this dataset is cached in.
DB_PATH = "data/cosmos_snow_tiles.db"

# Fixed historical date this module serves - not "today" and not refreshed on
# a schedule. See docs/cosmos-api.md for the exolabs COSMOS API background.
SNOW_DATE = "2026-01-01"

# S3 key layout per the exolabs gitbook docs (dated-folder, NOT the flat
# overwritten-daily layout this repo originally assumed - see
# docs/cosmos-api.md and the S3 bucket/prefix settings in config.py):
#   https://exolabs-ch.gitbook.io/cosmos/raw-data-listing#snow-depth
#   <ROI>/<YYYY-MM-DD>/<YYYY-MM-DD>+000_<ROI>_HS_product.tif
# "+000" is the analysis/nowcast product, not a +024/+048 forecast variant.
S3_KEY_TEMPLATE = "{roi}/{date}/{date}+000_{roi}_HS_product.tif"

# Local cache of the downloaded source GeoTIFF. SNOW_DATE is a fixed
# historical date, so once downloaded the file never changes - cached
# indefinitely, no re-download/refresh logic needed.
SOURCE_TIF_PATH = f"data/cosmos_snow_source_{SNOW_DATE}.tif"

# Austria bbox, WGS84 lon/lat: west, south, east, north. Same value/convention
# as util/fetch_snow_cover.py's AUSTRIA_BBOX and debug_ortho.DISTRICT1_BBOX.
AUSTRIA_BBOX = (9.53, 46.37, 17.16, 49.02)

MIN_ZOOM = 0
# Source GeoTIFF is 20m/px (EPSG:3857). Web Mercator resolution is
# 156543.03392/2^z m/px: z13 ~= 19.1m/px (finer than native, full detail
# captured), z12 ~= 38.2m/px (coarser - would lose detail). z13 is therefore
# the highest zoom that reflects real resolution rather than upsampling.
MAX_ZOOM = 13

TILE_SIZE = 256

# Web Mercator (EPSG:3857) world half-extent in meters - used to convert an
# XYZ tile index directly into its bounds in that CRS.
WEBMERCATOR_ORIGIN = 20037508.342789244

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

# Public status wording kept stable for /v1/cosmos-snow/status and
# docs/map.html, backed by the generic processes registry.
_STATE_TO_STATUS = {
    processes.RUNNING: "generating",
    processes.DONE: "ready",
    processes.ERROR: "error",
}


def init() -> None:
    """Open (or create) the cosmos-snow tiles database and kick off generation
    of any missing tiles in a background thread. Never blocks server startup.
    Completeness is inferred from which tiles already exist in the DB (same
    pattern as tile_creators/debug_ortho.py), so an interrupted previous run
    is resumed rather than masked by a stale flag."""
    global _conn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS tiles (
                z INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                data BLOB NOT NULL,
                PRIMARY KEY (z, x, y)
            );
        """)
        _conn.commit()

    processes.start(PROCESS_KEY, PROCESS_LABEL)
    logger.info("cosmos-snow: checking dataset at %s, generating any missing tiles in background", DB_PATH)
    threading.Thread(target=_generate_all, name="cosmos-snow-gen", daemon=True).start()


def get_status() -> str:
    p = processes.get(PROCESS_KEY)
    return _STATE_TO_STATUS[p["state"]] if p else "generating"


def is_ready() -> bool:
    p = processes.get(PROCESS_KEY)
    return p is not None and p["state"] == processes.DONE


def get_tile(z: int, x: int, y: int) -> bytes | None:
    with _lock:
        row = _conn.execute(
            "SELECT data FROM tiles WHERE z = ? AND x = ? AND y = ?", (z, x, y)
        ).fetchone()
    return row["data"] if row else None


def _save_tile(z: int, x: int, y: int, data: bytes) -> None:
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO tiles (z, x, y, data) VALUES (?, ?, ?, ?)",
            (z, x, y, data),
        )
        _conn.commit()


def _tile_exists(z: int, x: int, y: int) -> bool:
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM tiles WHERE z = ? AND x = ? AND y = ? LIMIT 1", (z, x, y)
        ).fetchone()
    return row is not None


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


# --------------------------------------------------------------------------
# S3 download
# --------------------------------------------------------------------------
def _ensure_source_tif() -> str:
    """Return a local path to the source GeoTIFF, downloading it from S3 into
    SOURCE_TIF_PATH if not already cached."""
    if os.path.exists(SOURCE_TIF_PATH):
        return SOURCE_TIF_PATH

    os.makedirs(os.path.dirname(SOURCE_TIF_PATH), exist_ok=True)
    key = S3_KEY_TEMPLATE.format(roi=config.cosmos_s3_prefix, date=SNOW_DATE)
    tmp_path = SOURCE_TIF_PATH + ".part"

    logger.info("cosmos-snow: downloading s3://%s/%s", config.cosmos_s3_bucket, key)
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
                f"cosmos-snow: S3 key not found: s3://{config.cosmos_s3_bucket}/{key} "
                f"(the dated-folder layout from the exolabs gitbook docs may not match "
                f"reality - see https://exolabs-ch.gitbook.io/cosmos/raw-data-listing#snow-depth ; "
                f"try `aws s3 ls s3://{config.cosmos_s3_bucket}/{config.cosmos_s3_prefix}/{SNOW_DATE}/` "
                f"to see what's actually there)"
            ) from e
        raise

    os.replace(tmp_path, SOURCE_TIF_PATH)
    return SOURCE_TIF_PATH


# --------------------------------------------------------------------------
# Raster read
# --------------------------------------------------------------------------
def _read_tile_from_raster(src: rasterio.DatasetReader, z: int, x: int, y: int) -> tuple[np.ndarray, np.ndarray]:
    """Windowed read of one 256x256 tile directly from the source raster.
    Returns (value_cm: uint16 array, valid: bool array)."""
    left, bottom, right, top = _tile_bounds_3857(z, x, y)
    minx, miny, maxx, maxy = rasterio.warp.transform_bounds(
        "EPSG:3857", src.crs, left, bottom, right, top
    )
    window = rasterio.windows.from_bounds(minx, miny, maxx, maxy, transform=src.transform)

    data = src.read(
        1,
        window=window,
        out_shape=(TILE_SIZE, TILE_SIZE),
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
def _generate_all() -> None:
    try:
        start = time.monotonic()
        tif_path = _ensure_source_tif()

        zoom_ranges = {z: _tile_range(AUSTRIA_BBOX, z) for z in range(MIN_ZOOM, MAX_ZOOM + 1)}
        total = sum(
            (xmax - xmin + 1) * (ymax - ymin + 1) for xmin, xmax, ymin, ymax in zoom_ranges.values()
        )
        done = 0
        processes.update(PROCESS_KEY, done, total=total, message=f"{done}/{total} tiles generated")

        with rasterio.open(tif_path) as src:
            logger.info(
                "cosmos-snow: source crs=%s nodata=%s dtype=%s shape=%s",
                src.crs, src.nodata, src.dtypes[0], src.shape,
            )
            if src.crs is None:
                raise RuntimeError("cosmos-snow: source GeoTIFF has no CRS - cannot georeference tiles")

            xmin, xmax, ymin, ymax = zoom_ranges[MAX_ZOOM]
            for x in range(xmin, xmax + 1):
                for y in range(ymin, ymax + 1):
                    if not _tile_exists(MAX_ZOOM, x, y):
                        value_cm, valid = _read_tile_from_raster(src, MAX_ZOOM, x, y)
                        _save_tile(MAX_ZOOM, x, y, _encode_tile(value_cm, valid))
                    done += 1
                    processes.update(PROCESS_KEY, done, message=f"{done}/{total} tiles generated (z={MAX_ZOOM})")

        for z in range(MAX_ZOOM - 1, MIN_ZOOM - 1, -1):
            xmin, xmax, ymin, ymax = zoom_ranges[z]
            for x in range(xmin, xmax + 1):
                for y in range(ymin, ymax + 1):
                    if not _tile_exists(z, x, y):
                        children = {
                            (dx, dy): get_tile(z + 1, x * 2 + dx, y * 2 + dy)
                            for dx in (0, 1) for dy in (0, 1)
                        }
                        value_cm, valid = _compose_parent_tile(children)
                        _save_tile(z, x, y, _encode_tile(value_cm, valid))
                    done += 1
                    processes.update(PROCESS_KEY, done, message=f"{done}/{total} tiles generated (z={z})")

        elapsed = time.monotonic() - start
        processes.finish(PROCESS_KEY, message=f"{done} tiles in {elapsed:.1f}s")
        logger.info("cosmos-snow: dataset ready (%d tiles) in %.1fs", done, elapsed)
    except Exception:
        logger.exception("cosmos-snow: generation failed")
        processes.fail(PROCESS_KEY, message="generation failed, check server log")
