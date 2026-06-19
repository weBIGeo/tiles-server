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
from logging.handlers import RotatingFileHandler
import sys
import config

# ANSI color codes
_RESET = "\x1b[0m"
_GRAY = "\x1b[90m"
_CYAN = "\x1b[36m"
_BLUE = "\x1b[94m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_BRIGHT_RED = "\x1b[91m"

_LEVEL_COLORS = {
    logging.DEBUG:    _CYAN,
    logging.INFO:     _BLUE,
    logging.WARNING:  _YELLOW,
    logging.ERROR:    _RED,
    logging.CRITICAL: _BRIGHT_RED,
}

_LEVEL_NAMES = {
    logging.DEBUG:    "Debug   ",
    logging.INFO:     "Info    ",
    logging.WARNING:  "Warning ",
    logging.ERROR:    "Error   ",
    logging.CRITICAL: "Critical",
}


def _try_enable_ansi_windows() -> None:
    """Enable ANSI escape codes in the Windows console if needed."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass

## A formatter based on the one for the weBIGeo project (see https://github.com/weBIGeo/webigeo/blob/main/webgpu_app/util/error_logging.cpp)
class _WebiGeoFormatter(logging.Formatter):

    def __init__(self, use_color: bool) -> None:
        super().__init__()
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        time_str = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        level_str = _LEVEL_NAMES.get(record.levelno, f"{record.levelname:<8}")
        file_part = f"{record.filename}:{record.lineno}"
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)

        if self._use_color:
            color = _LEVEL_COLORS.get(record.levelno, "")
            left = f"{color}{time_str} | {level_str} | {file_part:<25} |{_RESET}"
            if record.levelno == logging.DEBUG:
                return f"{left} {_GRAY}{msg}{_RESET}"
            return f"{left} {msg}"

        return f"{time_str} | {level_str} | {file_part:<25} | {msg}"


_LOGO_GRAY  = "\x1b[38;5;245m"
_LOGO_GREEN = "\x1b[32m"

_LOGO_TEMPLATE = r"""
%5        _    .  ,   .           .    *                             *         .
%5    *  / \_ *  / \_     %1   .        ___ %2___%3 ___    .    %4 ___ _             _    %5       /\'__
%5      /    \  /    \,   %1__ __ _____| _ )%2_ _%3/ __|___ ___ %4/ __| |___ _  _ __| |___%5 .   _/  /  \  *'.
%5 .   /\/\  /\/ :' __ \_ %1\ V  V / -_) _ \%2| |%3 (_ / -_) _ \%4 (__| / _ \ || / _` (_-<%5  _^/  ^/    `--.
%5    /    \/  \  _/  \-'\%1 \_/\_/\___|___/%2___%3\___\___\___/%4\___|_\___/\_,_\__,_/__/%5 /.' ^_   \_   .'\
%2===================================================================================================%3"""


def print_logo() -> None:
    logo = _LOGO_TEMPLATE
    logo = logo.replace("%1", _CYAN)
    logo = logo.replace("%2", _LOGO_GRAY)
    logo = logo.replace("%3", _RESET)
    logo = logo.replace("%4", _LOGO_GREEN)
    logo = logo.replace("%5", _LOGO_GRAY)
    print(logo)


def setup_logging(log_file: str | None = None) -> None:
    """Configure the root logger with weBIGeo-style colored formatting."""
    _try_enable_ansi_windows()

    level = getattr(logging, config.log_level.upper(), logging.DEBUG)

    use_color = sys.stdout.isatty()
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(_WebiGeoFormatter(use_color=use_color))

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(console_handler)

    if log_file and log_file != "":
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=config.log_file_max_bytes,
            backupCount=config.log_file_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(_WebiGeoFormatter(use_color=False))
        root.addHandler(file_handler)

    for name, override in config.log_level_overrides.items():
        logging.getLogger(name).setLevel(getattr(logging, override.upper(), logging.WARNING))
