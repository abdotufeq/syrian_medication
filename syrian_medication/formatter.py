"""Terminal output formatting: banners, progress bars, tables, and colored text.

All helpers write directly to stdout so they are visible regardless of the
logging level.  They fall back to plain text when the terminal does not
support ANSI colors.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import timedelta


# ── ANSI color helpers ───────────────────────────────────────────────────────

class _C:
    """Minimal ANSI color palette."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    UNDERLINE = "\033[4m"

    # Foreground colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # Bright foreground colors
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_CYAN = "\033[96m"

    # Background colors
    BG_BLUE = "\033[44m"
    BG_CYAN = "\033[46m"
    BG_YELLOW = "\033[43m"


def _supports_color() -> bool:
    """Check if the terminal likely supports ANSI colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


USE_COLOR = _supports_color()


def c(text: str, color: str, bold: bool = False) -> str:
    """Wrap *text* in ANSI color codes.  Returns plain text when colors are disabled."""
    if not USE_COLOR:
        return text
    parts = []
    if bold:
        parts.append(_C.BOLD)
    parts.append(color)
    return f"{''.join(parts)}{text}{_C.RESET}"


def green(text: str, bold: bool = False) -> str:
    return c(text, _C.GREEN, bold)


def red(text: str, bold: bool = False) -> str:
    return c(text, _C.RED, bold)


def yellow(text: str, bold: bool = False) -> str:
    return c(text, _C.YELLOW, bold)


def blue(text: str, bold: bool = False) -> str:
    return c(text, _C.BLUE, bold)


def cyan(text: str, bold: bool = False) -> str:
    return c(text, _C.CYAN, bold)


def bold(text: str) -> str:
    if not USE_COLOR:
        return text
    return f"{_C.BOLD}{text}{_C.RESET}"


def dim(text: str) -> str:
    if not USE_COLOR:
        return text
    return f"{_C.DIM}{text}{_C.RESET}"


# ── Banners ──────────────────────────────────────────────────────────────────

def banner(title: str, width: int = 70, char: str = "═") -> None:
    """Print a centered banner with a colored title."""
    padding = (width - len(title) - 2) // 2
    line = char * width
    print()
    print(c(line, _C.CYAN))
    print(c(f"║{' ' * padding} {bold(title)} {' ' * (width - padding - len(title) - 4)} ║", _C.CYAN))
    print(c(line, _C.CYAN))
    print()


def section(title: str, width: int = 70) -> None:
    """Print a section header with a thin underline."""
    print()
    print(bold(c(title, _C.BRIGHT_CYAN)))
    print(c("─" * width, _C.DIM))


def divider(width: int = 70, char: str = "─") -> None:
    """Print a simple horizontal divider line."""
    print(c(char * width, _C.DIM))


# ── Progress bar ─────────────────────────────────────────────────────────────

class ProgressBar:
    """Simple terminal progress bar that writes to stdout.

    Usage:
        bar = ProgressBar(total=1120, label="Scraping MOH pages")
        for i in range(1, 1121):
            # ... do work ...
            bar.update(i, extra=f"{stored} unique")
        bar.finish()
    """

    BAR_WIDTH = 30

    def __init__(
        self,
        total: int,
        label: str = "",
        start: int = 0,
    ) -> None:
        self.total = total
        self.label = label
        self.current = start
        self.start_time = time.time()
        self._last_print = 0.0

    def update(self, current: int, extra: str = "") -> None:
        self.current = current
        now = time.time()
        # Throttle: print at most once per 0.5 seconds
        if now - self._last_print < 0.5 and current != self.total:
            return
        self._last_print = now

        pct = current / self.total if self.total else 0
        filled = int(self.BAR_WIDTH * pct)
        bar = "█" * filled + "░" * (self.BAR_WIDTH - filled)

        elapsed = now - self.start_time
        rate = current / elapsed if elapsed > 0 else 0
        eta = (self.total - current) / rate if rate > 0 else 0

        parts = []
        if self.label:
            parts.append(bold(c(self.label, _C.CYAN)))
        parts.append(f" [{bar}] {pct:.0%}")
        parts.append(f" ({current}/{self.total})")
        parts.append(f" {rate:.1f}/s")
        if eta > 0 and current < self.total:
            parts.append(f" ETA {timedelta(seconds=int(eta))}")
        if extra:
            parts.append(f"  {dim(extra)}")

        print(f"\r{''.join(parts)}", end="", flush=True, file=sys.stderr)

    def finish(self, message: str = "Done") -> None:
        self.update(self.total)
        elapsed = time.time() - self.start_time
        print(f"\r", end="", flush=True, file=sys.stderr)
        pct_str = f"100%"
        bar = "█" * self.BAR_WIDTH
        rate = self.current / elapsed if elapsed > 0 else 0
        line = (
            f"  {bold(c(self.label, _C.CYAN))} "
            f"[{green(bar)}] {green(pct_str)} "
            f"({self.current}/{self.total}) "
            f"{dim(f'{rate:.1f}/s · {timedelta(seconds=int(elapsed))}')}"
        )
        if message:
            line += f"  {green('✓ ' + message)}"
        print(line, file=sys.stderr)
        print()  # newline after progress bar


# ── Status icons ─────────────────────────────────────────────────────────────

def status_icon(status: str) -> str:
    """Return a colored icon for a given status string."""
    icons = {
        "accessible": green("✓"),
        "ok": green("✓"),
        "success": green("✓"),
        "no_website": dim("○"),
        "forbidden": red("✗"),
        "connection_error": red("✗"),
        "timeout": red("✗"),
        "error": red("✗"),
        "ssl_error": yellow("⚠"),
    }
    return icons.get(status, "?")


# ── Summary table ────────────────────────────────────────────────────────────

def summary_table(rows: list[tuple[str, str]], title: str = "") -> None:
    """Print a two-column key-value table with aligned columns.

    Example:
        summary_table([
            ("Ministry rows", "13,249"),
            ("Manufacturer rows", "4,721"),
            ("Photos", "9,013"),
        ], title="Database Summary")
    """
    if not rows:
        return

    if title:
        print()
        print(bold(c(title, _C.BRIGHT_CYAN)))
        print(c("─" * 50, _C.DIM))

    # Find the widest key
    key_width = max(len(key) for key, _ in rows)

    for key, value in rows:
        label = f"  {key.ljust(key_width)}  {value}"
        print(label)
    print()


# ── Formatted numbers ────────────────────────────────────────────────────────

def fmt(n: int | float) -> str:
    """Format an integer with thousands separators."""
    return f"{int(n):,}"


def fmt_duration(seconds: float) -> str:
    """Format seconds as a human-readable duration."""
    return str(timedelta(seconds=int(seconds)))


# ── Boxed output ─────────────────────────────────────────────────────────────

def box(text: str, width: int = 60) -> None:
    """Print text inside a bordered box."""
    print()
    print(c("╔" + "═" * width + "╗", _C.CYAN))
    for line in text.split("\n"):
        padded = f"║ {line:<{width-2}} ║"
        print(c(padded, _C.CYAN))
    print(c("╚" + "═" * width + "╝", _C.CYAN))
    print()
