#############################################################################
# weBIGeo Tiles
# Copyright (C) 2011-2015 Vladimir Agafonkin
#      from: https://github.com/mourner/suncalc
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
"""Sun position and clear-sky beam irradiance, vectorized over numpy arrays.

The position part is a port of weBIGeo's
nucleus/utils/sun_calculations.cpp, which is itself a C++ port of suncalc.js.
Same formulas, same constants, and the same output convention as its
calculate_sun_angles():

- azimuth: compass bearing in degrees, 0 = north, 90 = east, clockwise, in
  [0, 360). suncalc's native azimuth is measured from *south* towards west;
  weBIGeo adds 180 deg, and so does this module.
- altitude: degrees above the astronomical horizon. positions() returns the
  *apparent* altitude, i.e. with the Meeus refraction term the C++ code carries
  (astroRefraction) added on top. The C++ code defines that term but doesn't
  apply it; here it is applied, because a sun that is geometrically just below
  the horizon is still visible - and that matters for low-sun shadows.

Time is always UTC, as numpy datetime64 (any unit) or anything
np.asarray(..., dtype="datetime64[ms]") accepts. Accuracy is suncalc's: well
under 0.1 deg for this century - far below the 0.5 deg the sun exposure
tiles resolve.

The irradiance part (beam_normal_irradiance) is the clear-sky beam model of
GRASS r.sun (Suri & Hofierka 2004, "A new GIS-based solar radiation model and
its application to photovoltaic assessments"), used by
tile_creators/sun_exposure.py for its energy tileset."""

import numpy as np

_RAD = np.pi / 180.0
_OBLIQUITY = _RAD * 23.4397  # obliquity of the Earth

_DAY_MS = 1000.0 * 60.0 * 60.0 * 24.0
_J1970 = 2440588.0
_J2000 = 2451545.0

# Solar constant (W/m^2) as used by r.sun.
SOLAR_CONSTANT = 1367.0


def _to_days(times_utc) -> np.ndarray:
    """UTC datetime64 -> days since J2000 (suncalc's toDays)."""
    ms = np.asarray(times_utc, dtype="datetime64[ms]").astype(np.int64).astype(np.float64)
    return ms / _DAY_MS - 0.5 + _J1970 - _J2000


def _astro_refraction(h: np.ndarray) -> np.ndarray:
    """Atmospheric refraction in radians for a true altitude h in radians.
    Formula 16.4 of Meeus, "Astronomical Algorithms" (2nd ed.), exactly as
    in the C++ astroRefraction: only valid for h >= 0, so negative altitudes
    are clamped to 0 (which also avoids the division by zero at
    h = -0.08901179)."""
    h = np.maximum(h, 0.0)
    return 0.0002967 / np.tan(h + 0.00312536 / (h + 0.08901179))


def positions(times_utc, lat_deg: float, lon_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Sun position for every time in `times_utc` at (lat, lon) in degrees.

    Returns (azimuth_deg, apparent_altitude_deg), both float64 arrays shaped
    like `times_utc`. See the module docstring for the conventions."""
    d = _to_days(times_utc)
    lw = _RAD * -lon_deg
    phi = _RAD * lat_deg

    # sunCoords: declination and right ascension from the ecliptic longitude.
    m = _RAD * (357.5291 + 0.98560028 * d)  # solar mean anomaly
    c = _RAD * (1.9148 * np.sin(m) + 0.02 * np.sin(2 * m) + 0.0003 * np.sin(3 * m))  # equation of centre
    ecl_lon = m + c + _RAD * 102.9372 + np.pi  # + perihelion of the Earth
    dec = np.arcsin(np.sin(_OBLIQUITY) * np.sin(ecl_lon))
    ra = np.arctan2(np.sin(ecl_lon) * np.cos(_OBLIQUITY), np.cos(ecl_lon))

    # Hour angle from the sidereal time.
    h_angle = _RAD * (280.16 + 360.9856235 * d) - lw - ra

    az = np.arctan2(np.sin(h_angle), np.cos(h_angle) * np.sin(phi) - np.tan(dec) * np.cos(phi))
    alt = np.arcsin(np.sin(phi) * np.sin(dec) + np.cos(phi) * np.cos(dec) * np.cos(h_angle))

    az_deg = np.mod(np.degrees(az + np.pi), 360.0)
    alt_deg = np.degrees(alt + _astro_refraction(alt))
    return az_deg, alt_deg


def direction_vectors(azimuth_deg: np.ndarray, altitude_deg: np.ndarray) -> np.ndarray:
    """(azimuth, altitude) in degrees -> (..., 3) unit vectors pointing *towards*
    the sun, in an ENU frame (+X east, +Y north, +Z up) whose north is the
    azimuth's zero. Note the sign: weBIGeo's sun_rays_direction_from_sun_angles
    returns the direction the rays *travel* (away from the sun), which is the
    negation of this."""
    az = np.radians(azimuth_deg)
    alt = np.radians(altitude_deg)
    cos_alt = np.cos(alt)
    return np.stack([cos_alt * np.sin(az), cos_alt * np.cos(az), np.sin(alt)], axis=-1)


def eccentricity_correction(day_of_year: np.ndarray) -> np.ndarray:
    """Sun-Earth distance correction factor for the solar constant, r.sun's
    epsilon: 1 + 0.03344 * cos(2*pi*j/365.25 - 0.048869), j = day of year
    (1-based). Between ~0.967 (July) and ~1.034 (January)."""
    j = 2.0 * np.pi * np.asarray(day_of_year, dtype=np.float64) / 365.25
    return 1.0 + 0.03344 * np.cos(j - 0.048869)


def beam_normal_irradiance(
    apparent_altitude_deg: np.ndarray, elevation_m: float, linke_turbidity: float,
    day_of_year: np.ndarray,
) -> np.ndarray:
    """Clear-sky direct (beam) irradiance on a plane *normal to the sun*, W/m^2,
    after r.sun (Suri & Hofierka 2004, eq. 1-4):

        B0c = G0 * eps * exp(-0.8662 * T_LK * m * dR(m))

    - m: relative optical air mass, Kasten & Young (1989), corrected for the
      receiver's elevation by p/p0 = exp(-z / 8434.5).
    - dR(m): Rayleigh optical thickness at air mass m (Kasten 1996).
    - T_LK: Linke turbidity for air mass 2 - how hazy the (cloudless)
      atmosphere is; ~2 very clear, ~3.5 hazy summer valley air.

    `apparent_altitude_deg` must already include refraction (positions()
    returns exactly that), which is what r.sun's h0ref stands for - so the
    refraction correction r.sun applies itself is deliberately not repeated
    here. The air-mass formula is not meaningful for a sun at or below the
    horizon; those samples return 0 (a grazing sun contributes almost no
    energy anyway: m ~ 38 at h = 0)."""
    h = np.asarray(apparent_altitude_deg, dtype=np.float64)
    up = h > 0.0
    hs = np.where(up, h, 1.0)  # placeholder where the sun is down, masked below

    pressure_ratio = np.exp(-elevation_m / 8434.5)
    m = pressure_ratio / (np.sin(np.radians(hs)) + 0.50572 * (hs + 6.07995) ** -1.6364)

    rayleigh = np.where(
        m <= 20.0,
        1.0 / (6.6296 + 1.7513 * m - 0.1202 * m ** 2 + 0.0065 * m ** 3 - 0.00013 * m ** 4),
        1.0 / (10.4 + 0.718 * m),
    )
    b = SOLAR_CONSTANT * eccentricity_correction(day_of_year) * np.exp(-0.8662 * linke_turbidity * m * rayleigh)
    return np.where(up, b, 0.0)
