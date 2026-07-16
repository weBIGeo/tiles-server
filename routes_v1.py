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
import util
from tile_creators import cosmos_snow, debug_ortho
from flask import Blueprint, Response, abort, jsonify

bp = Blueprint("v1", __name__, url_prefix="/v1")

VERSION = util.read_version()

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


def cosmos_snow_status():
    return jsonify({"status": cosmos_snow.get_status()})


def cosmos_snow_tile(z: int, x: int, y: int):
    data = cosmos_snow.get_tile(z, x, y)
    if data is not None:
        return Response(data, mimetype="image/png")
    if not cosmos_snow.is_ready():
        return jsonify({"error": "cosmos snow tiles are still being generated"}), 503
    abort(404)


bp.add_url_rule("/cosmos-snow/status", view_func=cosmos_snow_status)
bp.add_url_rule("/cosmos-snow/<int:z>/<int:x>/<int:y>.png", view_func=cosmos_snow_tile)
