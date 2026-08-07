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

import os
import re


# NOTE: The version is intentionally kept only in README.md. This function extracts it.
# I know its weird, but its one single line of truth - and i keep forgetting to bump it.
def read_version() -> str:
    try:
        # ../README.md - this module lives in util/, the README at the repo root.
        readme = os.path.join(os.path.dirname(os.path.dirname(__file__)), "README.md")
        with open(readme, encoding="utf-8") as f:
            m = re.search(r"img\.shields\.io/badge/version-([^-]+)-", f.read())
            if m:
                return m.group(1)
    except Exception:
        pass
    return "unknown"
