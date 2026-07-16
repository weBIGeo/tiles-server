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
# Generic registry for long-running background processes (tile generation,
# imports, ...) so the frontend can show a live progress bar instead of the
# process only being visible through log lines. Any module can report into
# it under its own key; routes_v1 exposes the current snapshot at
# /v1/processes for docs/index.html to poll and render.

import threading
import time

RUNNING = "running"
DONE = "done"
ERROR = "error"

# How long a finished (done/error) process stays in list_all() after
# completion, so the UI reverts to a clean state once nothing is happening.
FINISHED_RETENTION_SECONDS = 30

_lock = threading.Lock()
_processes: dict[str, dict] = {}


def start(key: str, label: str, total: int | None = None) -> None:
    """Register a process as running, or reset an existing one to start over."""
    with _lock:
        _processes[key] = {
            "key": key,
            "label": label,
            "state": RUNNING,
            "current": 0,
            "total": total,
            "message": None,
            "finished_at": None,
        }


def update(key: str, current: int, total: int | None = None, message: str | None = None) -> None:
    with _lock:
        p = _processes.get(key)
        if p is None:
            return
        p["current"] = current
        if total is not None:
            p["total"] = total
        if message is not None:
            p["message"] = message


def finish(key: str, message: str | None = None) -> None:
    _set_final(key, DONE, message)


def fail(key: str, message: str | None = None) -> None:
    _set_final(key, ERROR, message)


def _set_final(key: str, state: str, message: str | None) -> None:
    with _lock:
        p = _processes.get(key)
        if p is None:
            return
        p["state"] = state
        if message is not None:
            p["message"] = message
        p["finished_at"] = time.monotonic()


def get(key: str) -> dict | None:
    with _lock:
        p = _processes.get(key)
        return dict(p) if p else None


def list_all() -> list[dict]:
    """Snapshot of all processes still worth showing: running ones, plus
    finished ones within FINISHED_RETENTION_SECONDS of completion."""
    now = time.monotonic()
    with _lock:
        return [
            {k: v for k, v in p.items() if k != "finished_at"}
            for p in _processes.values()
            if p["finished_at"] is None or now - p["finished_at"] < FINISHED_RETENTION_SECONDS
        ]
