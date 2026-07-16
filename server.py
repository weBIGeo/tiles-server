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

import logging
import os
import config
import log_config
import util
import db
from tile_creators import debug_ortho
import notify
import routes_v1
from flask import Flask, send_from_directory
from flask_cors import CORS
from waitress import serve

logger = logging.getLogger("server")

VERSION = util.read_version()

app = Flask(__name__)
CORS(app)
app.register_blueprint(routes_v1.bp)


@app.route("/", methods=["GET"])
def index():
    return send_from_directory("docs", "index.html")


@app.route("/map", methods=["GET"])
def map_view():
    return send_from_directory("docs", "map.html")


# Unversioned alias — delegates to the routes_v1 handler
@app.route("/status", methods=["GET"])
def server_status():
    return routes_v1.status()


if __name__ == "__main__":
    os.makedirs(os.path.dirname(config.db_path), exist_ok=True)
    log_config.setup_logging(log_file=config.log_file)
    log_config.print_logo()
    db.init(config.db_path)
    debug_ortho.init()
    msg = f" === weBIGeo Tiles Server v{VERSION} started === "
    sep = " " + "=" * (len(msg) - 2) + " "
    logger.info(sep)
    logger.info(msg)
    logger.info(sep)
    notify.notify("weBIGeo Tiles Server started", f"Server v{VERSION} started on {config.host}:{config.port}")

    logger.info(f"Starting waitress server on http://{config.host}:{config.port} ({config.threads} threads)")
    serve(app, host=config.host, port=config.port, threads=config.threads)
