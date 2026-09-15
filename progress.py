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

from formatting import describe_seconds, truncate_end, truncate_start
from resources import REFRESH_SECONDS as RESOURCE_SECONDS, ResourceMonitor

BAR_WIDTH = 20
FILLED = "#"
EMPTY = "-"

# How often the display is redrawn, in seconds. Fast enough to look alive,
# slow enough that drawing it is not part of the measurement.
#
# Kept separate from the resource line's own rate (RESOURCE_SECONDS, five
# seconds). Reading every worker's CPU and memory is far more expensive than
# re-rendering text, and on a run with hundreds of workers doing it on the
# redraw clock means most of the time goes on measuring rather than working.
REDRAW_SECONDS = 0.2


# The least of a trailing field worth showing when it has to be cut. Less than
# this and the field is left out, rather than ending the line in a stub like
# "4s…" that looks like part of the time.
SHORTEST_CUT_FIELD = 8


def fit_line(line, limit):
    """A display line cut to `limit` characters, at a field boundary where possible.

    Fields in a display line are separated by at least two spaces. When the
    limit falls between fields, the line ends at the last whole one. When it
    falls inside the last field -- a finished file's note -- that field is cut
    with an ellipsis if enough of it still shows, and otherwise left out.
    """
    if len(line) <= limit:
        return line
    cut = line[:limit]
    # Between fields only on a run of two or more spaces. A single space is
    # inside a field -- "20 cols, 3 constant" -- and cutting there would look
    # like the whole of it.
    around = line[max(0, limit - 1): limit + 1]
    if around == "  " or line[limit: limit + 2] == "  " or cut.endswith("  "):
        return cut.rstrip()
    boundary = cut.rfind("  ")
    if boundary < 0:
        return truncate_end(line, limit)
    if len(cut) - (boundary + 2) >= SHORTEST_CUT_FIELD:
        return line[: limit - 1] + "…"
    return cut[:boundary].rstrip()


def render_bar(fraction, width=BAR_WIDTH):
    """A text progress bar. `fraction` is 0.0 to 1.0, or None if unknown."""
    if fraction is None:
        return "?" * width
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return FILLED * filled + EMPTY * (width - filled)


# What each word in the display means. The progress line is squeezed to fit
# beside a bar, so every word in it is an abbreviation of something; spelling
# them out once costs five lines and saves everyone guessing.
DISPLAY_KEY = (
    "rows      rows checked",
    "cols      columns checked",
    "time      time elapsed",
    "constant  flagged columns holding the same value 99%+ of the time",
    "check     flagged for an anonymity check",
)

# Only meaningful when files are being copied to local disk first, so it is
# left out of the key otherwise rather than explaining something that will
# never appear.
STAGING_KEY = ("copying   being copied to local disk, not yet read",)


def print_key(say=print, staging=False):
    """Say what each word in the display means."""
    say("  key")
    for line in DISPLAY_KEY + (STAGING_KEY if staging else ()):
        say(f"    {line}")


class FileProgress:
    """What we know about one file's progress."""

    def __init__(self, name, estimated_rows=None):
        self.name = name
        self.estimated_rows = estimated_rows
        self.rows_read = 0
        self.seconds = 0.0          # what the worker reported, once it finishes
        self.started_at = None      # our own clock, for the time in between
        self.state = "waiting"      # waiting | copying | reading | done | failed
        self.finished_at = None     # our clock again, to show recent completions
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
        label = f"[{index}/{total}] {truncate_start(self.name, name_width):<{name_width}}"

        if self.state in ("waiting", "copying"):
            return f"  {label}  {'':<{BAR_WIDTH}}   {self.state}"

        bar = render_bar(self.fraction)
        if self.state == "failed":
            return f"  {label}  [{bar}]  FAILED  {self.note}"

        if self.state == "done":
            return (f"  {label}  [{bar}]   done  {self.rows_read:>12,} rows"
                    f"  {describe_seconds(self.elapsed):>6}  {self.note}")

        # Still reading. An estimated total gives a percentage; without one we
        # can still show the rows so far, which is better than nothing.
        if self.fraction is None:
            share = "  ?  "
        else:
            share = f"{self.fraction:>4.0%}"
        return (f"  {label}  [{bar}]  {share}  {self.rows_read:>12,} rows"
                f"  {describe_seconds(self.elapsed):>6}")


class ProgressDisplay:
    """Draws one line per file, in place when the terminal allows it."""

    def __init__(self, names, estimated_rows=None, stream=None,
                 show_resources=True, workers=None):
        self.stream = stream or sys.stderr
        self.show_resources = show_resources
        self.workers = workers
        # Holds the previous CPU reading, so the next one can be a rate.
        self.monitor = ResourceMonitor() if show_resources else None
        self.resource_text = ""
        self.resources_read_at = 0.0
        # Redrawing needs a terminal. Anything else gets plain lines.
        self.can_redraw = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.files = [
            FileProgress(name, (estimated_rows or {}).get(name)) for name in names
        ]
        self.by_name = {progress.name: progress for progress in self.files}
        self.lines_drawn = 0
        self.last_drawn_at = 0.0

        self.longest_name = max((len(name) for name in names), default=20)
        self.columns = shutil.get_terminal_size((100, 24)).columns
        self.name_width = self.fit_names(self.columns)

    def fit_names(self, columns):
        """How wide the file name can be so a line fits in this many columns.

        Measured from a real line with an empty name rather than assumed: the
        bar, the share, the row count and the time take a fixed amount, and the
        [n/total] label grows with the number of files. A line wider than the
        terminal wraps onto a second row, the cursor is then moved back up too
        few rows on the next redraw, and every frame leaves a copy of the last
        one behind -- the display repeats itself down the screen.
        """
        total = len(self.files)
        fixed = 0
        # A finished line is a few characters longer than a reading one before
        # its note starts. Fitting to the longer keeps the time on screen; only
        # the note, which can be any length, is cut.
        for state in ("reading", "done"):
            sample = FileProgress("", estimated_rows=1)
            sample.state = state
            fixed = max(fixed, len(sample.line(total, total, 0)))
        return max(12, min(self.longest_name, columns - 1 - fixed))

    def update(self, name, **changes):
        """Record what a worker told us. Does not draw."""
        progress = self.by_name.get(name)
        if progress is None:
            return
        # Start our own clock the moment a file begins.
        if changes.get("state") == "reading" and progress.started_at is None:
            progress.started_at = time.monotonic()
        if changes.get("state") in ("done", "failed") and progress.finished_at is None:
            progress.finished_at = time.monotonic()
        for field, value in changes.items():
            setattr(progress, field, value)

    def summary_line(self):
        """One line accounting for every file, for when they cannot all be shown."""
        counts = {}
        for progress in self.files:
            counts[progress.state] = counts.get(progress.state, 0) + 1

        # Only mention the states that apply: "0 failed" on a healthy run is
        # noise, and "copying" is meaningless unless files are being copied.
        parts = []
        for state in ("done", "failed", "copying", "reading", "waiting"):
            if counts.get(state):
                parts.append(f"{counts[state]:,} {state}")
        return f"  {len(self.files):,} files: " + (", ".join(parts) or "starting")

    def window_of_file_lines(self, room):
        """At most `room` lines, showing the files that are actually moving.

        Picking a window by position instead lets one slow file near the top
        pin the screen in place, so it sits still while hundreds of others come
        and go below it. Active files are picked out wherever they sit in the
        list; every line carries its own [n/total], so a gap between them reads
        perfectly well.

        Room left over goes to whatever finished most recently, so completions
        can be watched happening rather than only counted.
        """
        if room <= 0:
            return []
        total = len(self.files)

        active = [position for position, progress in enumerate(self.files)
                  if progress.state in ("reading", "copying")]
        not_shown = max(0, len(active) - room)
        shown = set(active[:room])

        spare = room - len(shown)
        if spare > 0:
            finished = sorted(
                (position for position, progress in enumerate(self.files)
                 if progress.finished_at is not None),
                key=lambda position: self.files[position].finished_at,
            )
            shown.update(finished[-spare:])

        # Before anything has started there is nothing moving to show, so show
        # what is about to be.
        if not shown:
            shown = set(range(min(room, total)))

        drawn = [self.files[position].line(position + 1, total, self.name_width)
                 for position in sorted(shown)]
        if not_shown:
            drawn[-1] = f"  ... and {not_shown:,} more being read"
        return drawn

    def draw(self, force=False):
        """Redraw, at most every REDRAW_SECONDS unless forced."""
        now = time.monotonic()
        if not force and now - self.last_drawn_at < REDRAW_SECONDS:
            return
        self.last_drawn_at = now

        lines = []
        if self.show_resources:
            # Refreshed on its own slower clock: it is context, not progress,
            # and reading /proc for every worker on every frame would be work
            # spent measuring instead of working.
            if now - self.resources_read_at >= RESOURCE_SECONDS or not self.resource_text:
                self.resource_text = self.monitor.line(workers=self.workers)
                self.resources_read_at = now
            lines.append(self.resource_text)
            lines.append("")

        if self.can_redraw:
            # A window can be resized mid-run, so its width is read every frame.
            columns = shutil.get_terminal_size((100, 24)).columns
            if columns != self.columns:
                self.columns = columns
                self.name_width = self.fit_names(columns)

            # Redrawing works by moving the cursor up over what was written
            # last time. The cursor cannot travel above the top of the screen,
            # so writing more lines than the terminal is tall leaves it in the
            # wrong place and the whole display scrolls away every frame. One
            # row is left spare for the cursor to rest on.
            height = shutil.get_terminal_size((100, 24)).lines
            room = max(1, height - len(lines) - 1)
        else:
            room = len(self.files)

        if room >= len(self.files):
            lines += [
                progress.line(number, len(self.files), self.name_width)
                for number, progress in enumerate(self.files, start=1)
            ]
        else:
            # More files than screen. A count stands in for the ones not shown
            # so nothing disappears silently, and the index written at the end
            # has every file regardless.
            lines.append(self.summary_line())
            lines += self.window_of_file_lines(room - 1)

        if self.can_redraw:
            # Every line is cut to one short of the width, so none wraps and the
            # cursor movement below always lands back on the first line. The last
            # column is left empty because some terminals wrap on a line that
            # fills it exactly.
            lines = [fit_line(line, self.columns - 1) for line in lines]

            # Each line is written over the last frame's in place, clearing only
            # what is left to its right, and anything below the new frame is
            # cleared at the end. Clearing the whole area first and then writing
            # it again showed the screen blank for a moment, five times a second.
            frame = []
            if self.lines_drawn:
                frame.append(f"\033[{self.lines_drawn}A")
            frame += [f"\r{line}\033[K\n" for line in lines]
            frame.append("\033[J")
            self.stream.write("".join(frame))
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
