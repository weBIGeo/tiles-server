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
# Normal-vector encoding for normal tiles, shared by tile_creators/als_normals.py
# and scripts/normal_map_playground.ipynb so the two can never drift apart.
#
# Format: hemi-octahedral projection -> 127-centred 8-bit quantization -> R/G of an
# RGB PNG (B reserved/zero, no alpha channel). See docs/normal_map_encoding.md for
# the full reasoning; the short version:
#
#   - Heightfield normals always point up (n.z >= 0), so only a hemisphere needs
#     encoding. Plain octahedral would confine every value to the |x|+|y| <= 1
#     diamond and waste half the code space; the hemi-oct 45-degree rotation fills
#     the whole square instead - sqrt(2) better precision for free, and no
#     octahedral fold inside the sampled domain, so GPU bilinear filtering of the
#     texture is always safe.
#   - 8 bits per component costs 0.24 degrees mean / 0.55 degrees max error
#     (measured over a uniform hemisphere; plain oct is 0.34 / 0.94 for the same
#     8 bits). That is roughly 10x below the ~5 degree noise floor of normals
#     derived from 1m ALS data, so 16 bits would measure nothing real while
#     roughly doubling the tile size.
#   - Quantization is centred on 127 with a half-range of 127 (codes 0..254) rather
#     than the conventional round((e*0.5+0.5)*255), so that e=0 - a perfectly flat
#     surface - round-trips exactly instead of coming back tilted by 0.225 degrees.
#
# Frame convention: normals are ENU - +X east, +Y north, +Z up - and metric (the
# Web Mercator altitude correction is applied before encoding). Nothing here
# depends on that; it is stated so the convention lives next to the format.

import numpy as np

# Quantization constants. Codes run 0..254 symmetric about CENTER; 255 is unused,
# which is what buys the exact round-trip of the flat normal.
QUANT_CENTER = 127
QUANT_HALF_RANGE = 127

# The value written for pixels with no valid source data - the flat normal
# (0, 0, 1). Exposed so tile creators don't hardcode the byte triple.
FLAT_NORMAL_RGB = (QUANT_CENTER, QUANT_CENTER, 0)


def _sign_not_zero(v: np.ndarray) -> np.ndarray:
    """signNotZero from the weBIGeo shaders: +1 for v >= 0, -1 otherwise.
    Differs from np.sign only at 0, where np.sign would return 0 and collapse
    the octahedral fold."""
    return np.where(v >= 0.0, 1.0, -1.0).astype(v.dtype)


# --------------------------------------------------------------------------
# Hemi-octahedral projection (the format actually used by normal tiles)
# --------------------------------------------------------------------------
def normal_to_hemioct(n: np.ndarray) -> np.ndarray:
    """(..., 3) normalized upper-hemisphere normals -> (..., 2) in [-1, 1].

    Requires n[..., 2] >= 0. Unlike plain octahedral this fills the whole
    [-1,1] square with the hemisphere, so there is no fold in the sampled
    domain and interpolating between two encoded values stays valid."""
    n = np.asarray(n)
    l1 = np.abs(n[..., 0]) + np.abs(n[..., 1]) + n[..., 2]
    # Guard against a zero-length input rather than emitting nan/inf.
    l1 = np.where(l1 > 0.0, l1, 1.0)
    px = n[..., 0] / l1
    py = n[..., 1] / l1
    return np.stack([px + py, px - py], axis=-1)


def hemioct_to_normal(e: np.ndarray) -> np.ndarray:
    """Inverse of normal_to_hemioct. (..., 2) -> (..., 3) normalized."""
    e = np.asarray(e)
    tx = (e[..., 0] + e[..., 1]) * 0.5
    ty = (e[..., 0] - e[..., 1]) * 0.5
    tz = 1.0 - np.abs(tx) - np.abs(ty)
    n = np.stack([tx, ty, tz], axis=-1)
    norm = np.linalg.norm(n, axis=-1, keepdims=True)
    return n / np.where(norm > 0.0, norm, 1.0)


# --------------------------------------------------------------------------
# 127-centred 8-bit quantization
# --------------------------------------------------------------------------
def hemioct_to_bytes(e: np.ndarray) -> np.ndarray:
    """(..., 2) in [-1, 1] -> uint8 codes 0..254, symmetric about 127."""
    e = np.clip(np.asarray(e), -1.0, 1.0)
    return (np.rint(e * QUANT_HALF_RANGE) + QUANT_CENTER).astype(np.uint8)


def bytes_to_hemioct(b: np.ndarray) -> np.ndarray:
    """Inverse of hemioct_to_bytes. uint8 -> float32 in [-1, 1]."""
    return (np.asarray(b).astype(np.float32) - QUANT_CENTER) / QUANT_HALF_RANGE


# --------------------------------------------------------------------------
# Whole-tile convenience round-trip
# --------------------------------------------------------------------------
def encode_normals(n: np.ndarray, valid: np.ndarray | None = None) -> np.ndarray:
    """(H, W, 3) normals -> (H, W, 3) uint8 RGB, ready for Image.fromarray.

    R/G carry the quantized hemi-oct pair, B is reserved and written as 0.
    Where `valid` is False the pixel is replaced by FLAT_NORMAL_RGB, so a
    nodata void reads as flat ground rather than as whatever the gradient of
    a zero-filled height array happened to produce."""
    rg = hemioct_to_bytes(normal_to_hemioct(n))
    rgb = np.zeros(rg.shape[:-1] + (3,), dtype=np.uint8)
    rgb[..., :2] = rg
    if valid is not None:
        rgb[~valid] = FLAT_NORMAL_RGB
    return rgb


def decode_normals(rgb: np.ndarray) -> np.ndarray:
    """Inverse of encode_normals. (H, W, 3 or 4) uint8 -> (H, W, 3) float32
    normals. The B (and any A) channel is ignored."""
    return hemioct_to_normal(bytes_to_hemioct(np.asarray(rgb)[..., :2]))


# --------------------------------------------------------------------------
# Plain octahedral - not used by the tile format
# --------------------------------------------------------------------------
# Mirrors weBIGeo's WGSL v3f32_to_oct / oct_to_v3f32. Kept for the encoding
# comparison in scripts/normal_map_playground.ipynb, and for interop should the
# renderer ever need the standard full-sphere form.
def normal_to_oct(n: np.ndarray) -> np.ndarray:
    """(..., 3) normalized -> (..., 2) in [-1, 1]. Handles the full sphere."""
    n = np.asarray(n)
    l1 = np.abs(n[..., 0]) + np.abs(n[..., 1]) + np.abs(n[..., 2])
    l1 = np.where(l1 > 0.0, l1, 1.0)
    e = n[..., :2] / l1[..., None]
    folded = (1.0 - np.abs(e[..., ::-1])) * _sign_not_zero(e)
    return np.where((n[..., 2] <= 0.0)[..., None], folded, e)


def oct_to_normal(e: np.ndarray) -> np.ndarray:
    """Inverse of normal_to_oct. (..., 2) -> (..., 3) normalized."""
    e = np.asarray(e)
    z = 1.0 - np.abs(e[..., 0]) - np.abs(e[..., 1])
    folded = (1.0 - np.abs(e[..., ::-1])) * _sign_not_zero(e)
    xy = np.where((z < 0.0)[..., None], folded, e)
    n = np.concatenate([xy, z[..., None]], axis=-1)
    norm = np.linalg.norm(n, axis=-1, keepdims=True)
    return n / np.where(norm > 0.0, norm, 1.0)


# --------------------------------------------------------------------------
# Measurement helper
# --------------------------------------------------------------------------
def angle_between_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Angle in degrees between two arrays of (..., 3) unit vectors. Used to
    quantify an encoding's round-trip error against the source normals."""
    dot = np.clip(np.sum(np.asarray(a) * np.asarray(b), axis=-1), -1.0, 1.0)
    return np.degrees(np.arccos(dot))
