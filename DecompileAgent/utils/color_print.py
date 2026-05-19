import os
import sys
from typing import Optional


_ANSI_CODES = {
    "default": "0",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
}


def _colors_enabled() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("COLOR_PRINT", "").strip() in ("0", "false", "False"):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def colorize(text: str, color: Optional[str] = None, bold: bool = False) -> str:
    if not _colors_enabled():
        return text
    if not color:
        color = "default"
    code = _ANSI_CODES.get(color, _ANSI_CODES["default"])
    prefix = "\033["
    parts = []
    if bold:
        parts.append("1")
    parts.append(code)
    start = prefix + ";".join(parts) + "m"
    end = "\033[0m"
    return f"{start}{text}{end}"


def cprint(
    text: str,
    color: Optional[str] = None,
    bold: bool = False,
    end: str = "\n",
    flush: bool = True,
):
    print(colorize(text, color=color, bold=bold), end=end, flush=flush)

