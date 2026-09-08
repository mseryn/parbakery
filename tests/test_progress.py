"""Tests for progress.py.

The display is drawn from plain values, so it can be checked without a terminal.

Run with:

    pytest tests/test_progress.py -v
"""

import io

import pytest

from progress import (
    BAR_WIDTH,
    FileProgress,
    ProgressDisplay,
    format_duration,
    render_bar,
    shorten_name,
)


# --- the bar --------------------------------------------------------------

@pytest.mark.parametrize("fraction, expected", [
    (0.0, "-" * 20),
    (0.5, "#" * 10 + "-" * 10),
    (1.0, "#" * 20),
])
def test_bar_fills_in_proportion(fraction, expected):
    assert render_bar(fraction) == expected


def test_an_unknown_fraction_is_drawn_as_unknown():
    """A file whose row count we could not estimate should not show a made-up
    percentage."""
    assert render_bar(None) == "?" * BAR_WIDTH


@pytest.mark.parametrize("fraction", [-0.5, 1.5])
def test_the_bar_cannot_overflow(fraction):
    """Row estimates are approximate, so a file can read past 100%."""
    bar = render_bar(fraction)
    assert len(bar) == BAR_WIDTH


# --- durations and names --------------------------------------------------

@pytest.mark.parametrize("seconds, expected", [
    (5, "5s"), (45, "45s"), (90, "1m30s"), (3700, "1h01m"), (7325, "2h02m"),
])
def test_durations_stay_short(seconds, expected):
    assert format_duration(seconds) == expected


def test_a_long_name_is_trimmed_from_the_left():
    """The end of a filename is the part that distinguishes it."""
    trimmed = shorten_name("ANL-ALCF-DJC-POLARIS_20220809_20221231.csv", 25)
    assert len(trimmed) == 25
    assert trimmed.endswith("20221231.csv")


def test_a_short_name_is_left_alone():
    assert shorten_name("jobs.csv", 25) == "jobs.csv"


# --- one file's state -----------------------------------------------------

def test_a_waiting_file_says_so():
    progress = FileProgress("jobs.csv", estimated_rows=100)
    assert "waiting" in progress.line(1, 3, 20)


def test_progress_is_shown_against_the_estimate():
    progress = FileProgress("jobs.csv", estimated_rows=1000)
    progress.state = "reading"
    progress.rows_read = 250
    assert progress.fraction == 0.25


def test_a_file_past_its_estimate_is_capped_at_full():
    """Estimates are approximate; a bar that reads 130% looks broken."""
    progress = FileProgress("jobs.csv", estimated_rows=100)
    progress.state = "reading"
    progress.rows_read = 130
    assert progress.fraction == 1.0


def test_a_file_with_no_estimate_has_no_fraction():
    progress = FileProgress("jobs.csv", estimated_rows=None)
    progress.state = "reading"
    progress.rows_read = 500
    assert progress.fraction is None
    assert "500" in progress.line(1, 1, 20)      # the row count still shows


def test_a_finished_file_is_full_even_if_the_estimate_was_wrong():
    progress = FileProgress("jobs.csv", estimated_rows=1_000_000)
    progress.state = "done"
    progress.rows_read = 12
    assert progress.fraction == 1.0


def test_a_failed_file_says_so():
    progress = FileProgress("broken.csv")
    progress.state = "failed"
    progress.note = "ParserError"
    line = progress.line(1, 1, 20)
    assert "FAILED" in line and "ParserError" in line


# --- the display ----------------------------------------------------------

class FakeTerminal(io.StringIO):
    """A stream that claims to be a terminal, so the redraw path can be tested."""

    def isatty(self):
        return True


def test_the_display_has_one_line_per_file():
    stream = FakeTerminal()
    display = ProgressDisplay(["a.csv", "b.csv", "c.csv"], stream=stream)
    display.draw(force=True)

    written = stream.getvalue()
    assert len([l for l in written.splitlines() if l.strip()]) == 3
    for name in ("a.csv", "b.csv", "c.csv"):
        assert name in written


def test_redrawing_moves_the_cursor_back_up_over_the_last_frame():
    """Otherwise each refresh would append a new block and the terminal would
    scroll away."""
    stream = FakeTerminal()
    display = ProgressDisplay(["a.csv", "b.csv"], stream=stream)

    display.draw(force=True)
    first_frame = stream.getvalue()
    assert "\x1b[" not in first_frame          # nothing to move back over yet

    display.draw(force=True)
    assert "\x1b[2A" in stream.getvalue()      # up two lines, one per file


def test_drawing_is_rate_limited():
    """Redrawing on every message would cost more than the work being measured."""
    stream = FakeTerminal()
    display = ProgressDisplay(["a.csv"], stream=stream)

    display.draw(force=True)
    length_after_first = len(stream.getvalue())
    display.draw()                              # too soon, should be ignored
    assert len(stream.getvalue()) == length_after_first


def test_updates_reach_the_right_file():
    display = ProgressDisplay(["a.csv", "b.csv"], {"a.csv": 100}, stream=io.StringIO())
    display.update("a.csv", state="reading", rows_read=50)

    assert display.by_name["a.csv"].rows_read == 50
    assert display.by_name["b.csv"].rows_read == 0


def test_an_update_for_an_unknown_file_is_ignored():
    """A stray message must not crash a run that is otherwise going fine."""
    display = ProgressDisplay(["a.csv"], stream=io.StringIO())
    display.update("nonexistent.csv", state="reading")


def test_the_clock_starts_when_a_file_starts():
    """Timed by the display, not by the worker's messages -- those only arrive
    when a batch finishes, which would leave a slow file looking frozen."""
    display = ProgressDisplay(["a.csv"], stream=io.StringIO())
    assert display.by_name["a.csv"].started_at is None

    display.update("a.csv", state="reading")
    assert display.by_name["a.csv"].started_at is not None


def test_a_finished_file_reports_the_workers_time_not_ours():
    display = ProgressDisplay(["a.csv"], stream=io.StringIO())
    display.update("a.csv", state="reading")
    display.update("a.csv", state="done", seconds=42.0)
    assert display.by_name["a.csv"].elapsed == 42.0


def test_redrawing_is_off_when_the_output_is_not_a_terminal():
    """Cursor-movement codes in a log file are worse than no progress at all."""
    display = ProgressDisplay(["a.csv"], stream=io.StringIO())
    assert display.can_redraw is False


def test_a_non_terminal_reports_each_file_once_when_it_finishes():
    stream = io.StringIO()
    display = ProgressDisplay(["a.csv"], stream=stream)

    display.update("a.csv", state="reading")
    display.draw(force=True)
    assert stream.getvalue() == ""            # nothing while it is still going

    display.update("a.csv", state="done", rows_read=10, seconds=1.0)
    display.draw(force=True)
    assert "done" in stream.getvalue()

    display.draw(force=True)                  # and not a second time
    assert stream.getvalue().count("done") == 1
