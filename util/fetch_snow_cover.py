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
# Standalone utility to fetch exolabs / COSMOS snow-depth data for the Alps.
# This is NOT wired into the tiles-server in any way - it is a scratch tool to
# explore how the exolabs data can be obtained.
#
# Docs: https://exolabs-ch.gitbook.io/cosmos
#
# There are two completely separate ways to get the data, and they give you
# DIFFERENT things (see the module-level notes at the bottom of this file):
#
#   * WMS / XYZ tiles  -> pre-rendered RGB(A) PNG visualization tiles
#                         (Web Mercator / EPSG:3857, "gmaps" tiling scheme).
#                         Good for showing a map. NOT the raw snow depth.
#
#   * S3 GeoTIFF       -> the raw daily product (snow height values, alps-wide,
#                         single GeoTIFF per product, overwritten every day).
#                         Use this if you need actual numbers / your own styling.
#
# Dependencies (see util/requirements.txt):
#   requests           - for the WMS/XYZ tile download
#   boto3   (optional) - for the S3 GeoTIFF download (or use the AWS CLI)
#
# Usage examples:
#   python util/fetch_snow_cover.py list
#   python util/fetch_snow_cover.py tiles --product "snowdepth map - alps" --zoom 10
#   python util/fetch_snow_cover.py tif    --out data/exolabs
#   python util/fetch_snow_cover.py tif    --crop-austria --out data/exolabs
#############################################################################

import argparse
import math
import os
import sys

import requests

# Make the parent dir importable so we can reuse the server's config.py
# (config.py is git-ignored, config.example.py holds the placeholders).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402


# --------------------------------------------------------------------------
# Region of interest. Austria bounding box in WGS84 (lon/lat degrees).
# west, south, east, north
# --------------------------------------------------------------------------
AUSTRIA_BBOX = (9.53, 46.37, 17.16, 49.02)


# --------------------------------------------------------------------------
# WMS / XYZ tiles
# --------------------------------------------------------------------------
def _session() -> requests.Session:
    s = requests.Session()
    s.auth = (config.cosmos_wms_user, config.cosmos_wms_password)
    return s


def list_products(session: requests.Session) -> dict:
    """Return the product catalog served at /urlrequest.

    The catalog looks like:
      {"snowdepth_map": [{"name": "snowdepth map - alps", "url": "<token>"}, ...]}
    The "url" is an opaque token, not a usable URL on its own.
    """
    r = session.get(f"{config.cosmos_wms_base}/urlrequest", timeout=30)
    r.raise_for_status()
    return r.json()


def find_token(catalog: dict, product_name: str) -> str:
    for entries in catalog.values():
        for e in entries:
            if e.get("name") == product_name:
                return e["url"]
    available = [e["name"] for entries in catalog.values() for e in entries]
    raise SystemExit(f"Product '{product_name}' not found. Available: {available}")


def resolve_tile_base(session: requests.Session, token: str) -> str:
    """Resolve a product token to its XYZ tile base URL.

    GET /<token>/wms issues a 302 redirect whose Location points at the real,
    canonical tile endpoint, e.g.
        https://p20.cosmos-project.ch/<token>_map/gmaps/sd20alps@epsg3857/wms
    Stripping the trailing "/wms" gives the XYZ base; tiles live at
        <base>/{z}/{x}/{y}.png
    """
    url = f"{config.cosmos_wms_base}/{token}/wms"
    r = session.get(url, allow_redirects=False, timeout=30)
    loc = r.headers.get("Location")
    if not loc:
        raise SystemExit(f"No redirect Location for token {token} (status {r.status_code})")
    return loc[:-len("/wms")] if loc.endswith("/wms") else loc


def deg2tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    """WGS84 lon/lat -> XYZ (Google/OSM scheme) tile indices at zoom z."""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_range(bbox: tuple[float, float, float, float], z: int):
    west, south, east, north = bbox
    x0, y0 = deg2tile(north, west, z)  # top-left
    x1, y1 = deg2tile(south, east, z)  # bottom-right
    return min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)


def download_tiles(product_name: str, zoom: int, bbox, out_dir: str) -> None:
    session = _session()
    catalog = list_products(session)
    token = find_token(catalog, product_name)
    base = resolve_tile_base(session, token)
    print(f"product : {product_name}")
    print(f"token   : {token}")
    print(f"tilebase: {base}/{{z}}/{{x}}/{{y}}.png")

    xmin, xmax, ymin, ymax = tile_range(bbox, zoom)
    total = (xmax - xmin + 1) * (ymax - ymin + 1)
    print(f"zoom {zoom}: x[{xmin}..{xmax}] y[{ymin}..{ymax}]  ({total} tiles)")

    saved = 0
    for x in range(xmin, xmax + 1):
        for y in range(ymin, ymax + 1):
            r = session.get(f"{base}/{zoom}/{x}/{y}.png", timeout=30)
            if r.status_code != 200 or not r.content:
                continue
            d = os.path.join(out_dir, str(zoom), str(x))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{y}.png"), "wb") as f:
                f.write(r.content)
            saved += 1
    print(f"saved {saved}/{total} tiles to {out_dir}/")
    print("NOTE: these are pre-rendered colormap PNGs (a picture of the snow "
          "depth), not raw values. For raw values use the 'tif' command.")


# --------------------------------------------------------------------------
# S3 raw GeoTIFFs
# --------------------------------------------------------------------------
# Product files in s3://<bucket>/<prefix>/ (overwritten daily):
#   000_alps_HS_product.tif        analysis / nowcast (HS = "Hoehe der Schneedecke")
#   000-024_alps_HS_product.tif    \ 24h / 48h forecast products
#   000-048_alps_HS_product.tif    /
#   024_alps_HS_product.tif        ... (see exolabs handover for the full list)
#   048_alps_HS_product.tif
RAW_TIFS = [
    "000_alps_HS_product.tif",
    "000-024_alps_HS_product.tif",
    "000-048_alps_HS_product.tif",
    "024_alps_HS_product.tif",
    "024-000_alps_HS_product.tif",
    "048_alps_HS_product.tif",
    "048-000_alps_HS_product.tif",
]


def download_raw_tifs(out_dir: str, crop_austria: bool) -> None:
    try:
        import boto3
    except ImportError:
        raise SystemExit("boto3 not installed. Run 'pip install boto3' or use the "
                         "AWS CLI directly (see the commands printed by --help).")

    if not config.cosmos_aws_access_key_id or not config.cosmos_aws_secret_access_key:
        raise SystemExit("AWS credentials are empty in config.py. Fill in "
                         "cosmos_aws_access_key_id / cosmos_aws_secret_access_key "
                         "from the CSV exolabs handed over.")

    os.makedirs(out_dir, exist_ok=True)
    s3 = boto3.client(
        "s3",
        aws_access_key_id=config.cosmos_aws_access_key_id,
        aws_secret_access_key=config.cosmos_aws_secret_access_key,
        region_name=getattr(config, "cosmos_aws_region", None),
    )
    for name in RAW_TIFS:
        key = f"{config.cosmos_s3_prefix}/{name}"
        dst = os.path.join(out_dir, name)
        print(f"s3://{config.cosmos_s3_bucket}/{key} -> {dst}")
        s3.download_file(config.cosmos_s3_bucket, key, dst)
        if crop_austria:
            _crop_to_austria(dst)
    print(f"done. raw GeoTIFFs in {out_dir}/")


def _crop_to_austria(tif_path: str) -> None:
    """Crop an alps-wide GeoTIFF to the Austria bbox using gdal (if available)."""
    try:
        from osgeo import gdal
    except ImportError:
        print("  (skip crop: GDAL python bindings not installed; install gdal or "
              "run gdal_translate -projwin manually)")
        return
    west, south, east, north = AUSTRIA_BBOX
    out = tif_path.replace(".tif", "_austria.tif")
    # projWin is (ulx, uly, lrx, lry) in the dataset's CRS; GDAL warps the
    # WGS84 bbox via projWinSRS=EPSG:4326.
    gdal.Translate(out, tif_path, projWin=[west, north, east, south],
                   projWinSRS="EPSG:4326")
    print(f"  cropped -> {out}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Fetch exolabs/COSMOS snow-depth data for the Alps.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list the available products from /urlrequest")

    pt = sub.add_parser("tiles", help="download pre-rendered XYZ tiles for a region")
    pt.add_argument("--product", default="snowdepth map - alps")
    pt.add_argument("--zoom", type=int, default=10)
    pt.add_argument("--out", default="data/exolabs/tiles")

    pr = sub.add_parser("tif", help="download the raw daily GeoTIFFs from S3")
    pr.add_argument("--out", default="data/exolabs")
    pr.add_argument("--crop-austria", action="store_true",
                    help="also write an Austria-cropped copy of each GeoTIFF (needs GDAL)")

    args = p.parse_args()

    if args.cmd == "list":
        import json
        print(json.dumps(list_products(_session()), indent=2))
    elif args.cmd == "tiles":
        download_tiles(args.product, args.zoom, AUSTRIA_BBOX, args.out)
    elif args.cmd == "tif":
        download_raw_tifs(args.out, args.crop_austria)


if __name__ == "__main__":
    main()
