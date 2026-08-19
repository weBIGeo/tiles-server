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

import processes
from util import general
from tile_creators import als_normals, cosmos_snow, debug_ortho
from flask import Blueprint, Response, abort, jsonify

bp = Blueprint("v1", __name__, url_prefix="/v1")

VERSION = general.read_version()

# Possible status values. Only IDLE is ever returned for now; WORKING will be
# used once tile processing logic is added.
STATUS_IDLE = "idle"
STATUS_WORKING = "working"
STATUS_OPTIONS = (STATUS_IDLE, STATUS_WORKING)


def status():
    return jsonify({
        "version": VERSION,
        "status": STATUS_IDLE,  # always idle until processing logic is added
    })


bp.add_url_rule("/status", view_func=status)


def processes_status():
    return jsonify(processes.list_all())


bp.add_url_rule("/processes", view_func=processes_status)


def debug_ortho_status():
    return jsonify({"status": debug_ortho.get_status()})


def debug_ortho_tile(z: int, y: int, x: int):
    data = debug_ortho.get_tile(z, x, y)
    if data is not None:
        return Response(data, mimetype="image/jpeg")
    if not debug_ortho.is_ready():
        return jsonify({"error": "debug ortho tiles are still being generated"}), 503
    abort(404)


bp.add_url_rule("/debug-ortho/status", view_func=debug_ortho_status)
bp.add_url_rule("/debug-ortho/<int:z>/<int:y>/<int:x>.jpeg", view_func=debug_ortho_tile)


def cosmos_snow_dates():
    return jsonify(cosmos_snow.list_dates())


def cosmos_snow_generate(date: str):
    try:
        status = cosmos_snow.start_generation(date)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"date": date, "status": status})


def cosmos_snow_status(date: str):
    return jsonify({"status": cosmos_snow.get_status(date)})


def cosmos_snow_tile(date: str, z: int, x: int, y: int):
    conn = cosmos_snow.get_db(date)
    data = conn.get_tile(z, x, y) if conn is not None else None
    if data is not None:
        return Response(data, mimetype="image/png")
    if not cosmos_snow.is_ready(date):
        return jsonify({"error": "cosmos snow tiles are still being generated"}), 503
    abort(404)


bp.add_url_rule("/cosmos-snow/dates", view_func=cosmos_snow_dates)
bp.add_url_rule("/cosmos-snow/<date>/generate", view_func=cosmos_snow_generate, methods=["POST"])
bp.add_url_rule("/cosmos-snow/<date>/status", view_func=cosmos_snow_status)
bp.add_url_rule("/cosmos-snow/<date>/<int:z>/<int:x>/<int:y>.png", view_func=cosmos_snow_tile)


def als_normals_status():
    """Also reports the source name, its WGS84 bounds and MAX_ZOOM - a client
    can't discover any of them from the tiles alone, and needs the bounds to
    find a single 50km footprint and MAX_ZOOM to set maxNativeZoom."""
    return jsonify({
        "status": als_normals.get_status(),
        "source": als_normals.source_name(),
        "bounds": als_normals.source_bounds(),
        "min_zoom": als_normals.MIN_ZOOM,
        "max_zoom": als_normals.MAX_ZOOM,
    })


def als_normals_tile(z: int, x: int, y: int):
    """Serves whatever is in the db right now, generated or not - a run writes
    tiles as it goes, so a partially built tileset is viewable while it builds
    instead of being withheld until the end."""
    conn = als_normals.get_db()
    data = conn.get_tile(z, x, y) if conn is not None else None
    if data is not None:
        return Response(data, mimetype="image/png")
    if not als_normals.is_ready():
        return jsonify({"error": "this als normal tile has not been generated yet"}), 503
    # Generation finished and this tile still isn't there: it's outside the
    # source raster's footprint, which is normal for the corners of a
    # non-axis-aligned EPSG:3035 tile rather than an error.
    abort(404)


bp.add_url_rule("/als-normals/status", view_func=als_normals_status)
bp.add_url_rule("/als-normals/<int:z>/<int:x>/<int:y>.png", view_func=als_normals_tile)
