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
# RGBA PNG, B = Toksvig roughness factor, A = snow steepness visibility. See
# docs/normal_map_encoding.md for the full reasoning; the short version:
#
#   - Heightfield normals always point up (n.z >= 0), so only a hemisphere needs
#     encoding. Plain octahedral would confine every value to the |x|+|y| <= 1
#     diamond and waste half the code space; the hemi-oct 45-degree rotation fills
#     the whole square instead - sqrt(2) better precision for free, and no
#     octahedral fold inside the sampled domain, so GPU bilinear filtering of the
#     texture is always safe.
#   - 8 bits per component costs 0.26 degrees mean / 0.55 degrees max on real
#     alpine z17 data (plain oct is 0.36 / 0.94 for the same 8 bits). Two honest
#     gradient estimators of the same surface - finite difference and Sobel -
#     disagree by 0.77 degrees mean / 3.3 degrees p99 on that same tile, so the
#     quantization sits well under what the data actually pins down. 16 bits
#     costs 2.4x the bytes to reach 0.0014 degrees, which measures nothing real.
#   - Quantization is centred on 127 with a half-range of 127 (codes 0..254) rather
#     than the conventional round((e*0.5+0.5)*255), so that e=0 - a perfectly flat
#     surface - round-trips exactly instead of coming back tilted by 0.225 degrees.
#   - B and A are plain 0..255 unorm - unlike R/G they are unsigned [0,1]
#     quantities with no zero-symmetry to preserve, so there is no reason for the
#     127-centred trick there.
#   - Alpha carries real per-pixel data here, which docs/normal_map_encoding.md
#     generally rejects (browsers premultiply RGB by alpha; libwebp's
#     lossless=True zeroes RGB behind A=0 unless exact=True is passed). This
#     format is an explicit, documented exception: the only consumer is a raw
#     WGSL textureLoad (never filtered/blended sampling), and this tileset is
#     never re-encoded as WebP or round-tripped through a canvas. See that doc's
#     "Rejected alternatives" section for the full override reasoning - it does
#     not silently contradict itself.
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
# (0, 0, 1), full Toksvig factor (a flat surface has zero normal variance) and
# full snow visibility (a flat pixel's own steepness is 0 degrees, which is
# always inside the visible band). Exposed so tile creators don't hardcode the
# byte quadruple. Not a special case: these are exactly the values a genuinely
# flat, valid pixel would compute anyway.
FLAT_TILE_RGBA = (QUANT_CENTER, QUANT_CENTER, 255, 255)


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
# Scalar (B/A) channel quantization
# --------------------------------------------------------------------------
def scalar_to_byte(x: np.ndarray) -> np.ndarray:
    """[0, 1] float -> uint8 0..255 unorm. Plain quantization, no 127-centring -
    unlike the signed hemi-oct components, these are already unsigned [0,1]
    quantities with no zero-symmetry to preserve."""
    return np.rint(np.clip(np.asarray(x), 0.0, 1.0) * 255.0).astype(np.uint8)


def byte_to_scalar(b: np.ndarray) -> np.ndarray:
    """Inverse of scalar_to_byte. uint8 -> float32 in [0, 1]."""
    return np.asarray(b).astype(np.float32) / 255.0


# --------------------------------------------------------------------------
# Whole-tile convenience round-trip
# --------------------------------------------------------------------------
def encode_tile(mean_normal: np.ndarray, snow_visibility: np.ndarray, valid: np.ndarray | None = None) -> np.ndarray:
    """(H, W, 3) possibly non-unit mean normal + (H, W) snow visibility in
    [0, 1] -> (H, W, 4) uint8 RGBA, ready for Image.fromarray(..., "RGBA").

    `mean_normal` need not be unit length: its magnitude, clipped to [0, 1],
    IS the Toksvig roughness factor L (1 = the inputs that were averaged into
    this pixel all agreed, 0 = they cancelled out) - this is the only place a
    mean vector is normalized to a direction, so magnitude is never discarded
    before this point. R/G = hemi-oct of the unit direction, B = L,
    A = snow_visibility. Where `valid` is False the pixel is replaced by
    FLAT_TILE_RGBA, so a nodata void reads as flat, smooth, fully
    snow-visible ground rather than as whatever a zero-filled input happened
    to produce."""
    mean_normal = np.asarray(mean_normal)
    length = np.clip(np.linalg.norm(mean_normal, axis=-1), 0.0, 1.0)
    unit = mean_normal / np.where(length[..., None] > 0.0, length[..., None], 1.0)

    rgba = np.zeros(mean_normal.shape[:-1] + (4,), dtype=np.uint8)
    rgba[..., :2] = hemioct_to_bytes(normal_to_hemioct(unit))
    rgba[..., 2] = scalar_to_byte(length)
    rgba[..., 3] = scalar_to_byte(snow_visibility)
    if valid is not None:
        rgba[~valid] = FLAT_TILE_RGBA
    return rgba


def decode_tile(rgba: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Inverse of encode_tile. (H, W, 4) uint8 -> (normal (H, W, 3) float32
    unit vector, roughness_L (H, W) float32, snow_visibility (H, W) float32)."""
    rgba = np.asarray(rgba)
    normal = hemioct_to_normal(bytes_to_hemioct(rgba[..., :2]))
    roughness = byte_to_scalar(rgba[..., 2])
    snow_visibility = byte_to_scalar(rgba[..., 3])
    return normal, roughness, snow_visibility


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
    quantify an encoding's round-trip error against the source normals.

    Deliberately NOT arccos(dot). For nearly-parallel vectors arccos is badly
    conditioned - arccos(1-d) ~= sqrt(2d), so an error d in the dot product
    blows up to sqrt(2d) in the angle. With a float32 dot that puts a floor of
    ~0.028 degrees on anything this can measure, which is coarser than the error
    of a 16-bit encoding: comparing a vector to *itself* would report up to
    0.044 degrees. The chord form below is exact for unit vectors and stays
    well-conditioned at small angles; float64 keeps the input rounding out of
    the result too."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    chord = np.linalg.norm(a - b, axis=-1)
    return np.degrees(2.0 * np.arcsin(np.clip(chord / 2.0, 0.0, 1.0)))
