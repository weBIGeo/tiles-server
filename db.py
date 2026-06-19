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

import sqlite3
import threading

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def init(path: str = "data/tiles-server.db") -> None:
    """Open (or create) the SQLite database and initialise all tables."""
    global _conn
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript("""
            -- No tables yet. Add geo-tile schema here as the server grows
            -- (e.g. sources, tilesets, processing jobs, ...).
        """)
        _conn.commit()
