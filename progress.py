#!/usr/bin/env python3
"""Progress display for a run that works on several files at once.

One line per file, redrawn in place, so a long run looks like this:

    [1/4] POLARIS_20220809_20221231.csv  [##########----------]  52%   20,480 rows   0.8s
    [2/4] aurora_dim_job_comp_2026-01.csv  [####################] done   31,108 rows   0.4s
    [3/4] id_mapping.csv                   waiting

Redrawing in place needs a terminal. When the output is a file or a pipe, the
same information is printed as ordinary lines instead, because a screenful of
cursor-movement codes in a log is worse than no progress at all.
"""

import shutil
import sys
import time

BAR_WIDTH = 20
FILLED = "#"
EMPTY = "-"

# How often the display is redrawn, in seconds. Fast enough to look alive,
# slow enough that drawing it is not part of the measurement.
REFRESH_SECONDS = 0.2


def render_bar(fraction, width=BAR_WIDTH):
    """A text progress bar. `fraction` is 0.0 to 1.0, or None if unknown."""
    if fraction is None:
        return "?" * width
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return FILLED * filled + EMPTY * (width - filled)


def format_duration(seconds):
    """Seconds as something short: 45s, 3m12s, 1h04m."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def shorten_name(name, limit):
    """Trim a filename from the left, keeping the end, which is the useful part."""
    if len(name) <= limit:
        return name
    return "..." + name[-(limit - 3):]


class FileProgress:
    """What we know about one file's progress."""

    def __init__(self, name, estimated_rows=None):
        self.name = name
        self.estimated_rows = estimated_rows
        self.rows_read = 0
        self.seconds = 0.0          # what the worker reported, once it finishes
        self.started_at = None      # our own clock, for the time in between
        self.state = "waiting"      # waiting | reading | done | failed
        self.note = ""              # a short summary once it has finished
        self.reported = False       # used when we cannot redraw, to print once

    @property
    def elapsed(self):
        """How long this file has been going.

        Timed here rather than taken from the worker's messages. Those only
        arrive when a batch finishes, so a display driven by them alone sits
        frozen on a slow file and looks like it has hung.
        """
        if self.state in ("done", "failed"):
            return self.seconds
        if self.started_at is None:
            return 0.0
        return time.monotonic() - self.started_at

    @property
    def fraction(self):
        """How far through, or None when we cannot tell."""
        if self.state == "done":
            return 1.0
        if not self.estimated_rows:
            return None
        return min(self.rows_read / self.estimated_rows, 1.0)

    def line(self, index, total, name_width):
        """One line of the display."""
        label = f"[{index}/{total}] {shorten_name(self.name, name_width):<{name_width}}"

        if self.state == "waiting":
            return f"  {label}  {'':<{BAR_WIDTH}}   waiting"

        bar = render_bar(self.fraction)
        if self.state == "failed":
            return f"  {label}  [{bar}]  FAILED  {self.note}"

        if self.state == "done":
            return (f"  {label}  [{bar}]   done  {self.rows_read:>12,} rows"
                    f"  {format_duration(self.elapsed):>6}  {self.note}")

        # Still reading. An estimated total gives a percentage; without one we
        # can still show the rows so far, which is better than nothing.
        if self.fraction is None:
            share = "  ?  "
        else:
            share = f"{self.fraction:>4.0%}"
        return (f"  {label}  [{bar}]  {share}  {self.rows_read:>12,} rows"
                f"  {format_duration(self.elapsed):>6}")


class ProgressDisplay:
    """Draws one line per file, in place when the terminal allows it."""

    def __init__(self, names, estimated_rows=None, stream=None):
        self.stream = stream or sys.stderr
        # Redrawing needs a terminal. Anything else gets plain lines.
        self.can_redraw = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.files = [
            FileProgress(name, (estimated_rows or {}).get(name)) for name in names
        ]
        self.by_name = {progress.name: progress for progress in self.files}
        self.lines_drawn = 0
        self.last_drawn_at = 0.0

        width = shutil.get_terminal_size((100, 24)).columns
        longest = max((len(name) for name in names), default=20)
        # Leave room for the bar, counts and timing.
        self.name_width = max(12, min(longest, width - 58))

    def update(self, name, **changes):
        """Record what a worker told us. Does not draw."""
        progress = self.by_name.get(name)
        if progress is None:
            return
        # Start our own clock the moment a file begins.
        if changes.get("state") == "reading" and progress.started_at is None:
            progress.started_at = time.monotonic()
        for field, value in changes.items():
            setattr(progress, field, value)

    def draw(self, force=False):
        """Redraw, at most every REFRESH_SECONDS unless forced."""
        now = time.monotonic()
        if not force and now - self.last_drawn_at < REFRESH_SECONDS:
            return
        self.last_drawn_at = now

        lines = [
            progress.line(number, len(self.files), self.name_width)
            for number, progress in enumerate(self.files, start=1)
        ]

        if self.can_redraw:
            if self.lines_drawn:
                # Up N lines, then clear from there to the end of the screen.
                self.stream.write(f"\033[{self.lines_drawn}A\033[J")
            self.stream.write("\n".join(lines) + "\n")
            self.lines_drawn = len(lines)
        else:
            # Not a terminal: only say something when a file finishes, or the
            # log fills up with near-identical lines.
            for progress in self.files:
                if progress.state in ("done", "failed") and not progress.reported:
                    self.stream.write(progress.line(
                        self.files.index(progress) + 1, len(self.files), self.name_width
                    ) + "\n")
                    progress.reported = True
        self.stream.flush()

    def finish(self):
        """Draw one last time, so the final state is what stays on screen."""
        self.draw(force=True)
