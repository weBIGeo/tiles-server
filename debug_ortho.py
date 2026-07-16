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

import requests
from PIL import Image, ImageDraw, ImageFont

import config

logger = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

STATUS_GENERATING = "generating"
STATUS_READY = "ready"
STATUS_ERROR = "error"
_status = STATUS_GENERATING
_status_lock = threading.Lock()


def init() -> None:
    """Open (or create) the debug ortho tiles database and, if it didn't
    already contain a completed dataset, (re)generate it in a background
    thread. Never blocks server startup."""
    global _conn
    os.makedirs(os.path.dirname(config.debug_ortho_db_path), exist_ok=True)

    already_existed = os.path.isfile(config.debug_ortho_db_path)

    _conn = sqlite3.connect(config.debug_ortho_db_path, check_same_thread=False)
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
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        _conn.commit()

    if already_existed and _is_marked_complete():
        _set_status(STATUS_READY)
        logger.info("debug-ortho: existing dataset found at %s, serving as-is", config.debug_ortho_db_path)
        return

    _set_status(STATUS_GENERATING)
    logger.info("debug-ortho: dataset missing or incomplete, generating in background")
    threading.Thread(target=_generate_all, name="debug-ortho-gen", daemon=True).start()


def get_status() -> str:
    with _status_lock:
        return _status


def is_ready() -> bool:
    return get_status() == STATUS_READY


def get_tile(z: int, x: int, y: int) -> bytes | None:
    with _lock:
        row = _conn.execute(
            "SELECT data FROM tiles WHERE z = ? AND x = ? AND y = ?", (z, x, y)
        ).fetchone()
    return row["data"] if row else None


def _set_status(new_status: str) -> None:
    global _status
    with _status_lock:
        _status = new_status


def _is_marked_complete() -> bool:
    with _lock:
        row = _conn.execute("SELECT value FROM meta WHERE key = 'complete'").fetchone()
    return row is not None and row["value"] == "1"


def _mark_complete() -> None:
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('complete', '1')")
        _conn.commit()


def _save_tile(z: int, x: int, y: int, data: bytes) -> None:
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO tiles (z, x, y, data) VALUES (?, ?, ?, ?)",
            (z, x, y, data),
        )
        _conn.commit()


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
    url = config.debug_ortho_url_template.format(z=z, y=y, x=x)
    for attempt in range(config.debug_ortho_retry_count + 1):
        try:
            r = session.get(url, timeout=config.debug_ortho_request_timeout)
            if r.status_code == 200 and r.content:
                return r.content
            logger.warning("debug-ortho: %s -> HTTP %s (attempt %d)", url, r.status_code, attempt + 1)
        except requests.RequestException as e:
            logger.warning("debug-ortho: %s -> %s (attempt %d)", url, e, attempt + 1)
        time.sleep(config.debug_ortho_request_delay)
    return None


_LABEL_ZOOM_FONT_SIZE = 56
_LABEL_XY_FONT_SIZE = 24
_LABEL_LINE_SPACING = 4
_LABEL_PADDING = 6
_LABEL_BACKDROP_FILL = (0, 0, 0, 160)


def _draw_label(img: Image.Image, z: int, x: int, y: int) -> Image.Image:
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
        draw.text((cx, y_cursor), text, font=font, fill=(255, 255, 0, 255), anchor="ma")
        y_cursor += h + _LABEL_LINE_SPACING

    draw.rectangle(
        (0, 0, base.width - 1, base.height - 1), outline=_LABEL_BACKDROP_FILL, width=1
    )

    return Image.alpha_composite(base, overlay).convert("RGB")


def _apply_red_tint(img: Image.Image) -> Image.Image:
    red_layer = Image.new("RGB", img.size, config.debug_ortho_tint_color)
    return Image.blend(img.convert("RGB"), red_layer, config.debug_ortho_tint_alpha)


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _generate_all() -> None:
    try:
        start = time.monotonic()
        session = requests.Session()
        session.headers["User-Agent"] = config.debug_ortho_user_agent

        # Identify the z(max_zoom) parent tiles around Stephansplatz up front so
        # the main pyramid loop below knows which tiles to keep a pristine
        # (pre-label) copy of for the zoom-21 overzoom step.
        cx, cy = _deg2tile(
            config.debug_ortho_stephansplatz_lat,
            config.debug_ortho_stephansplatz_lon,
            config.debug_ortho_max_zoom,
        )
        r = config.debug_ortho_synth_parent_radius
        parent_coords = {(px, py) for px in range(cx - r, cx + r + 1) for py in range(cy - r, cy + r + 1)}
        pristine_parents: dict[tuple[int, int], Image.Image] = {}

        zoom_ranges = {
            z: _tile_range(config.debug_ortho_district1_bbox, z)
            for z in range(config.debug_ortho_min_zoom, config.debug_ortho_max_zoom + 1)
        }
        total = sum(
            (xmax - xmin + 1) * (ymax - ymin + 1) for xmin, xmax, ymin, ymax in zoom_ranges.values()
        )
        total += 4 * len(parent_coords)  # synthetic z(max_zoom+1) tiles
        done = 0

        for z, (xmin, xmax, ymin, ymax) in zoom_ranges.items():
            for x in range(xmin, xmax + 1):
                for y in range(ymin, ymax + 1):
                    raw = _download_tile(session, z, x, y)
                    if raw is None:
                        logger.warning("debug-ortho: giving up on z=%d x=%d y=%d", z, x, y)
                        continue
                    img = Image.open(io.BytesIO(raw)).convert("RGB")
                    if z == config.debug_ortho_max_zoom and (x, y) in parent_coords:
                        pristine_parents[(x, y)] = img.copy()
                    labeled = _draw_label(img, z, x, y)
                    _save_tile(z, x, y, _encode_jpeg(labeled))
                    done += 1
                    if done % config.debug_ortho_progress_log_interval == 0:
                        logger.info("debug-ortho: %d/%d tiles generated (z=%d)", done, total, z)
                    time.sleep(config.debug_ortho_request_delay)

        synth_z = config.debug_ortho_synth_zoom
        for (px, py), parent_img in pristine_parents.items():
            for dx in (0, 1):
                for dy in (0, 1):
                    child_x, child_y = px * 2 + dx, py * 2 + dy
                    crop_box = (dx * 128, dy * 128, dx * 128 + 128, dy * 128 + 128)
                    quadrant = parent_img.crop(crop_box).resize((256, 256), Image.BICUBIC)
                    tinted = _apply_red_tint(quadrant)
                    labeled = _draw_label(tinted, synth_z, child_x, child_y)
                    _save_tile(synth_z, child_x, child_y, _encode_jpeg(labeled))
                    done += 1
                    if done % config.debug_ortho_progress_log_interval == 0:
                        logger.info("debug-ortho: %d/%d tiles generated (z=%d)", done, total, synth_z)

        _mark_complete()
        _set_status(STATUS_READY)
        elapsed = time.monotonic() - start
        logger.info("debug-ortho: dataset ready (%d tiles) in %.1fs", done, elapsed)
    except Exception:
        logger.exception("debug-ortho: generation failed")
        _set_status(STATUS_ERROR)
