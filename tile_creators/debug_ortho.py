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
import threading
import time

import requests
from PIL import Image, ImageDraw, ImageFont

import processes
import tile_db
from tile_creators import progress

logger = logging.getLogger(__name__)

PROCESS_KEY = "debug_ortho"
PROCESS_LABEL = "Debug ortho tiles"

# Path to the SQLite database file this debug dataset is cached in.
DB_PATH = "data/debug_ortho_tiles.db"

# basemap.at orthofoto WMTS tile URL. Path order is {z}/{y}/{x}, matching
# basemap.at's own TileMatrix/TileRow/TileCol layout (not the usual z/x/y).
URL_TEMPLATE = "https://gataki.cg.tuwien.ac.at/raw/basemap/tiles/{z}/{y}/{x}.jpeg"
#URL_TEMPLATE = "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg"

# Real zoom range downloaded from basemap.at (20 is the source's actual max zoom).
MIN_ZOOM = 0
MAX_ZOOM = 20

# Vienna 1st district (Innere Stadt) bbox, WGS84 lon/lat: west, south, east, north.
DISTRICT1_BBOX = (16.3552089, 48.1995268, 16.3848946, 48.2184891)

# Stephansplatz center point, used to pick the z(MAX_ZOOM) parent tiles for
# the synthetic overzoom block.
STEPHANSPLATZ_LAT = 48.2084639
STEPHANSPLATZ_LON = 16.3720438

# Radius (in z(MAX_ZOOM) tiles) around the Stephansplatz center tile.
# A z20 tile is ~25m across at Vienna's latitude, so radius=6 -> 13x13 parent
# block -> 26x26 synthetic child block, ~330m across - enough to cover all of
# Stephansplatz including Stephansdom (St. Stephen's Cathedral), not just the
# center point.
SYNTH_PARENT_RADIUS = 6

# Synthetic (overzoom) zoom level - cropped/upscaled from MAX_ZOOM,
# since basemap.at has no real imagery beyond it.
SYNTH_ZOOM = 21

# Label font colors (RGBA). Synthetic tiles use red instead of the normal
# yellow to visually mark them as artificial.
LABEL_FONT_COLOR = (255, 255, 0, 255)
SYNTH_LABEL_FONT_COLOR = (255, 0, 0, 255)

# HTTP client settings for downloading tiles from basemap.at.
USER_AGENT = "weBIGeo-Tiles-Server-debug-ortho/1.0"
REQUEST_TIMEOUT = 15   # seconds
REQUEST_DELAY = 0.05   # seconds, polite delay between requests
RETRY_COUNT = 3        # additional attempts after the first failure

_db: tile_db.TileDb | None = None

# Public status wording kept stable for /v1/debug-ortho/status and docs/map.html,
# backed by the generic processes registry rather than an enum of our own.
_STATE_TO_STATUS = {
    processes.RUNNING: "generating",
    processes.DONE: "ready",
    processes.ERROR: "error",
}


def init() -> None:
    """Open (or create) the debug ortho tiles database and kick off generation
    of any missing tiles in a background thread. Never blocks server startup.
    There's no persisted "complete" flag - completeness is just whichever
    tiles already exist in the DB, checked (and skipped) one by one as
    _generate_all walks the full expected tile set, so a config change (e.g.
    a wider synth radius) or an interrupted previous run is picked up and
    resumed correctly rather than being masked by a stale flag."""
    global _db
    _db = tile_db.TileDb(DB_PATH)

    processes.start(PROCESS_KEY, PROCESS_LABEL)
    logger.info("debug-ortho: checking dataset at %s, generating any missing tiles in background", DB_PATH)
    threading.Thread(target=_generate_all, name="debug-ortho-gen", daemon=True).start()


def get_status() -> str:
    p = processes.get(PROCESS_KEY)
    return _STATE_TO_STATUS[p["state"]] if p else "generating"


def is_ready() -> bool:
    p = processes.get(PROCESS_KEY)
    return p is not None and p["state"] == processes.DONE


def get_tile(z: int, x: int, y: int) -> bytes | None:
    return _db.get_tile(z, x, y)


def _save_tile(z: int, x: int, y: int, data: bytes) -> None:
    _db.save_tile(z, x, y, data)


def _tile_exists(z: int, x: int, y: int) -> bool:
    return _db.tile_exists(z, x, y)


def _synth_children_exist(px: int, py: int) -> bool:
    """Whether all 4 zoom-(MAX_ZOOM+1) children of a synth parent tile already
    exist, i.e. that parent's pristine bytes aren't needed this run."""
    return all(
        _tile_exists(SYNTH_ZOOM, px * 2 + dx, py * 2 + dy)
        for dx in (0, 1) for dy in (0, 1)
    )


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


def _download_tile(session: requests.Session, z: int, x: int, y: int) -> bytes | None:
    url = URL_TEMPLATE.format(z=z, y=y, x=x)
    for attempt in range(RETRY_COUNT + 1):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200 and r.content:
                return r.content
            logger.warning("debug-ortho: %s -> HTTP %s (attempt %d)", url, r.status_code, attempt + 1)
        except requests.RequestException as e:
            logger.warning("debug-ortho: %s -> %s (attempt %d)", url, e, attempt + 1)
        time.sleep(REQUEST_DELAY)
    return None


_LABEL_ZOOM_FONT_SIZE = 56
_LABEL_XY_FONT_SIZE = 24
_LABEL_LINE_SPACING = 4
_LABEL_PADDING = 6
_LABEL_BACKDROP_FILL = (0, 0, 0, 160)


def _draw_label(img: Image.Image, z: int, x: int, y: int, font_color: tuple = LABEL_FONT_COLOR) -> Image.Image:
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    zoom_font = ImageFont.load_default(size=_LABEL_ZOOM_FONT_SIZE)
    xy_font = ImageFont.load_default(size=_LABEL_XY_FONT_SIZE)
    lines = [(f"{z}", zoom_font), (f"x{x}", xy_font), (f"y{y}", xy_font)]

    line_sizes = [draw.textbbox((0, 0), text, font=font)[2:] for text, font in lines]
    block_width = max(w for w, h in line_sizes)
    block_height = sum(h for w, h in line_sizes) + _LABEL_LINE_SPACING * (len(lines) - 1)

    cx, cy = base.width / 2, base.height / 2
    draw.rectangle(
        (
            cx - block_width / 2 - _LABEL_PADDING,
            cy - block_height / 2 - _LABEL_PADDING,
            cx + block_width / 2 + _LABEL_PADDING,
            cy + block_height / 2 + _LABEL_PADDING,
        ),
        fill=_LABEL_BACKDROP_FILL,
    )

    y_cursor = cy - block_height / 2
    for (text, font), (_, h) in zip(lines, line_sizes):
        draw.text((cx, y_cursor), text, font=font, fill=font_color, anchor="ma")
        y_cursor += h + _LABEL_LINE_SPACING

    draw.rectangle(
        (0, 0, base.width - 1, base.height - 1), outline=_LABEL_BACKDROP_FILL, width=1
    )

    return Image.alpha_composite(base, overlay).convert("RGB")


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _generate_all() -> None:
    try:
        start = time.monotonic()
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT

        # Identify the z(MAX_ZOOM) parent tiles around Stephansplatz up front so
        # the main pyramid loop below knows which tiles to keep a pristine
        # (pre-label) copy of for the zoom-21 overzoom step.
        cx, cy = _deg2tile(
            STEPHANSPLATZ_LAT,
            STEPHANSPLATZ_LON,
            MAX_ZOOM,
        )
        r = SYNTH_PARENT_RADIUS
        parent_coords = {(px, py) for px in range(cx - r, cx + r + 1) for py in range(cy - r, cy + r + 1)}
        pristine_parents: dict[tuple[int, int], Image.Image] = {}

        zoom_ranges = {
            z: _tile_range(DISTRICT1_BBOX, z)
            for z in range(MIN_ZOOM, MAX_ZOOM + 1)
        }
        total = sum(
            (xmax - xmin + 1) * (ymax - ymin + 1) for xmin, xmax, ymin, ymax in zoom_ranges.values()
        )
        total += 4 * len(parent_coords)  # synthetic z(MAX_ZOOM+1) tiles
        done = 0
        rate = progress.RateTracker()
        processes.update(PROCESS_KEY, done, total=total, message=f"{done}/{total} tiles generated")

        for z, (xmin, xmax, ymin, ymax) in zoom_ranges.items():
            for x in range(xmin, xmax + 1):
                for y in range(ymin, ymax + 1):
                    is_synth_parent = z == MAX_ZOOM and (x, y) in parent_coords
                    # A synth parent only needs fetching fresh (for its pristine,
                    # pre-label bytes) if at least one of its zoom-(MAX_ZOOM+1)
                    # children is still missing; otherwise treat it like any
                    # other tile and just skip it if already saved.
                    needs_pristine = is_synth_parent and not _synth_children_exist(x, y)
                    if not needs_pristine and _tile_exists(z, x, y):
                        done += 1
                        processes.update(PROCESS_KEY, done, message=f"{done}/{total} tiles generated (z={z}, {rate.sample(done)})")
                        continue
                    raw = _download_tile(session, z, x, y)
                    if raw is None:
                        logger.warning("debug-ortho: giving up on z=%d x=%d y=%d", z, x, y)
                        continue
                    img = Image.open(io.BytesIO(raw)).convert("RGB")
                    if needs_pristine:
                        pristine_parents[(x, y)] = img.copy()
                    labeled = _draw_label(img, z, x, y)
                    _save_tile(z, x, y, _encode_jpeg(labeled))
                    done += 1
                    processes.update(PROCESS_KEY, done, message=f"{done}/{total} tiles generated (z={z}, {rate.sample(done)})")
                    time.sleep(REQUEST_DELAY)

        synth_z = SYNTH_ZOOM
        for (px, py) in parent_coords:
            parent_img = pristine_parents.get((px, py))
            for dx in (0, 1):
                for dy in (0, 1):
                    child_x, child_y = px * 2 + dx, py * 2 + dy
                    done += 1
                    if parent_img is None or _tile_exists(synth_z, child_x, child_y):
                        processes.update(PROCESS_KEY, done, message=f"{done}/{total} tiles generated (z={synth_z}, {rate.sample(done)})")
                        continue
                    crop_box = (dx * 128, dy * 128, dx * 128 + 128, dy * 128 + 128)
                    quadrant = parent_img.crop(crop_box).resize((256, 256), Image.BICUBIC)
                    labeled = _draw_label(quadrant, synth_z, child_x, child_y, font_color=SYNTH_LABEL_FONT_COLOR)
                    _save_tile(synth_z, child_x, child_y, _encode_jpeg(labeled))
                    processes.update(PROCESS_KEY, done, message=f"{done}/{total} tiles generated (z={synth_z}, {rate.sample(done)})")

        elapsed = time.monotonic() - start
        processes.finish(PROCESS_KEY, message=f"{done} tiles in {elapsed:.1f}s")
        logger.info("debug-ortho: dataset ready (%d tiles) in %.1fs", done, elapsed)
    except Exception:
        logger.exception("debug-ortho: generation failed")
        processes.fail(PROCESS_KEY, message="generation failed, check server log")
