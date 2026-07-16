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

# Copy this file to config.py and adjust the values for your deployment.

# Path to the SQLite database file.
db_path = "data/tiles-server.db"

# Host and port the server listens on.
# Use "0.0.0.0" to accept connections from any network interface.
host = "127.0.0.1"
port = 8001

# Waitress worker thread count. The tile client fires many concurrent tile
# requests per zoom-in burst (a "quad" is 4 sibling tiles, and several quads
# can be in flight at once), so the waitress default of 4 threads causes
# request queueing/timeouts under load.
threads = 32

# Logging level. (e.g. DEBUG, INFO, WARNING, ERROR, CRITICAL)
log_level = "DEBUG"

# Log file path. Set to empty string to disable file logging.
log_file = "data/latest.log"
# Log file rotation: maximum size per file in bytes and number of backup files to keep.
log_file_max_bytes = 5 * 1024 * 1024  # 5 MiB
log_file_backup_count = 3

# Per-logger level overrides. Use this to silence noisy third-party libraries
log_level_overrides = {
    "waitress": "ERROR",
    "filelock": "ERROR",
    "urllib3":  "ERROR",
}

# Email notifications. Leave smtp_host or notify_email empty/None to disable.
# Gmail: smtp_host="smtp.gmail.com", smtp_password=<app password from myaccount.google.com/apppasswords>
notify_email  = ""   # recipient address
smtp_host     = ""   # e.g. "smtp.gmail.com"
smtp_port     = 587
smtp_user     = ""   # sender login (usually same as notify_email)
smtp_password = ""

# ntfy.sh push notifications. Set ntfy_topic to enable (pick an unguessable name).
# Install the ntfy app and subscribe to the same topic to receive push notifications.
ntfy_topic  = "" # e.g. "webigeo-tiles-abc123"
ntfy_server = "https://ntfy.sh"  # override for self-hosted instances

# === exolabs / COSMOS snow data (used only by util/fetch_snow_cover.py) ===
# Docs: https://exolabs-ch.gitbook.io/cosmos
# WMS / XYZ tile access (pre-rendered visualization tiles).
cosmos_wms_base     = "https://p20.cosmos-project.ch"
cosmos_wms_user     = "YOUR_WMS_USER"
cosmos_wms_password = "YOUR_WMS_PASSWORD"

# S3 raw GeoTIFF access (raw daily snow-depth product, overwritten every day).
# Credentials come from the AWS CSV exolabs handed over.
cosmos_s3_bucket             = "exolabs-swiss-project"
cosmos_s3_prefix             = "alps"
cosmos_aws_access_key_id     = "YOUR_AWS_ACCESS_KEY_ID"
cosmos_aws_secret_access_key = "YOUR_AWS_SECRET_ACCESS_KEY"
cosmos_aws_region            = "eu-central-1"  # adjust if the bucket lives elsewhere

# === Debug ortho tiles (self-generating debug dataset, see debug_ortho.py) ===
debug_ortho_db_path = "data/debug_ortho_tiles.db"

# basemap.at orthofoto WMTS tile URL. Path order is {z}/{y}/{x}, matching
# basemap.at's own TileMatrix/TileRow/TileCol layout (not the usual z/x/y).
debug_ortho_url_template = "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg"

# Real zoom range downloaded from basemap.at (20 is the source's actual max zoom).
debug_ortho_min_zoom = 0
debug_ortho_max_zoom = 20

# Vienna 1st district (Innere Stadt) bbox, WGS84 lon/lat: west, south, east, north.
debug_ortho_district1_bbox = (16.3552089, 48.1995268, 16.3848946, 48.2184891)

# Stephansplatz center point, used to pick the z(max_zoom) parent tiles for
# the synthetic overzoom block.
debug_ortho_stephansplatz_lat = 48.2084639
debug_ortho_stephansplatz_lon = 16.3720438

# Radius (in z(max_zoom) tiles) around the Stephansplatz center tile.
# radius=1 -> 3x3 parent block -> 6x6 synthetic child block.
debug_ortho_synth_parent_radius = 1

# Synthetic (overzoom) zoom level - cropped/upscaled from debug_ortho_max_zoom,
# since basemap.at has no real imagery beyond it.
debug_ortho_synth_zoom = 21

# Red tint applied to synthetic tiles to visually mark them as artificial.
debug_ortho_tint_color = (255, 0, 0)  # RGB
debug_ortho_tint_alpha = 0.35         # 0 = no tint, 1 = solid red

# HTTP client settings for downloading tiles from basemap.at.
debug_ortho_user_agent = "weBIGeo-Tiles-Server-debug-ortho/1.0"
debug_ortho_request_timeout = 15   # seconds
debug_ortho_request_delay = 0.05   # seconds, polite delay between requests
debug_ortho_retry_count = 3        # additional attempts after the first failure

# Log a progress line every N tiles during generation (console status report).
debug_ortho_progress_log_interval = 10
