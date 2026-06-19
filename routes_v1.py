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

import util
from flask import Blueprint, jsonify

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
