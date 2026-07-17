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

"""Thread-safe SQLite-backed tile cache, shared by tile_creators/* so no
tile source needs to touch sqlite3, manage its own lock, or hand-roll commit
batching. Every tile source uses the same "tiles" table shape and the same
WAL pragmas (see tile_creators/cosmos_snow.py and
tile_creators/debug_ortho.py, both callers) - TileDb is the one place that
opens such a file, so they upgrade together instead of drifting."""

import os
import sqlite3
import threading


class TileDb:
    def __init__(self, path: str, commit_batch_size: int = 1):
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        self._lock = threading.Lock()
        self._commit_batch_size = commit_batch_size
        self._since_commit = 0

        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL + NORMAL synchronous: SQLite's recommended pairing, durable
        # against an application crash but not an OS crash/power loss for
        # the most recent transaction - an acceptable tradeoff for
        # regenerable tile cache data, and far fewer disk syncs than the
        # default rollback-journal/FULL synchronous combo.
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS tiles (
                z INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                data BLOB NOT NULL,
                PRIMARY KEY (z, x, y)
            );
        """)
        self._conn.commit()

    def get_tile(self, z: int, x: int, y: int) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM tiles WHERE z = ? AND x = ? AND y = ?", (z, x, y)
            ).fetchone()
        return row["data"] if row else None

    def tile_exists(self, z: int, x: int, y: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tiles WHERE z = ? AND x = ? AND y = ? LIMIT 1", (z, x, y)
            ).fetchone()
        return row is not None

    def save_tile(self, z: int, x: int, y: int, data: bytes) -> None:
        """Inserts/replaces the tile. Commits automatically every
        `commit_batch_size` saves (default: every save) - callers never
        track this themselves. Call commit() to force-flush early (e.g. at
        the end of a batch of work, or to persist partial progress before
        raising)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tiles (z, x, y, data) VALUES (?, ?, ?, ?)",
                (z, x, y, data),
            )
            self._since_commit += 1
            if self._since_commit >= self._commit_batch_size:
                self._conn.commit()
                self._since_commit = 0

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()
            self._since_commit = 0
