"""Tests for progress.py.

The display is drawn from plain values, so it can be checked without a terminal.

Run with:

    pytest tests/test_progress.py -v
"""

import io
import os

import pytest

from progress import (
    BAR_WIDTH,
    FileProgress,
    ProgressDisplay,
    describe_seconds,
    render_bar,
    truncate_start,
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
    assert describe_seconds(seconds) == expected


def test_a_long_name_is_trimmed_from_the_left():
    """The end of a filename is the part that distinguishes it."""
    trimmed = truncate_start("ANL-ALCF-DJC-POLARIS_20220809_20221231.csv", 25)
    assert len(trimmed) == 25
    assert trimmed.endswith("20221231.csv")


def test_a_short_name_is_left_alone():
    assert truncate_start("jobs.csv", 25) == "jobs.csv"


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
    display = ProgressDisplay(["a.csv", "b.csv", "c.csv"], stream=stream,
                              show_resources=False)
    display.draw(force=True)

    written = stream.getvalue()
    assert len([l for l in written.splitlines() if l.strip()]) == 3
    for name in ("a.csv", "b.csv", "c.csv"):
        assert name in written


def test_redrawing_moves_the_cursor_back_up_over_the_last_frame():
    """Otherwise each refresh would append a new block and the terminal would
    scroll away."""
    stream = FakeTerminal()
    display = ProgressDisplay(["a.csv", "b.csv"], stream=stream,
                              show_resources=False)

    display.draw(force=True)
    first_frame = stream.getvalue()
    assert "\x1b[" not in first_frame          # nothing to move back over yet

    display.draw(force=True)
    assert "\x1b[2A" in stream.getvalue()      # up two lines, one per file


def test_redrawing_covers_the_resource_line_too():
    """The frame is taller with the resource header, and the cursor has to go
    back over all of it or the display walks down the screen."""
    stream = FakeTerminal()
    display = ProgressDisplay(["a.csv", "b.csv"], stream=stream,
                              show_resources=True)

    display.draw(force=True)
    display.draw(force=True)
    assert "\x1b[4A" in stream.getvalue()      # 2 files + resource line + blank


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


# --- the key --------------------------------------------------------------

def test_the_key_explains_every_word_the_display_uses():
    """A word in the display with no entry in the key is jargon nobody can look
    up. "dull" got shipped that way, and this is the test that would have caught it."""
    from progress import DISPLAY_KEY

    explained = {line.split()[0] for line in DISPLAY_KEY}
    assert {"rows", "cols", "time", "constant", "check"} <= explained


def test_the_key_prints_each_term_with_its_meaning():
    said = []
    from progress import print_key
    print_key(said.append)

    text = "\n".join(said)
    assert "key" in text
    for term in ("rows", "cols", "time", "constant", "check"):
        assert term in text


# --- the resource line ----------------------------------------------------

def test_the_resource_line_reports_workers_memory_and_cpu():
    from resources import ResourceMonitor

    line = ResourceMonitor().line(workers=4)
    assert "4 worker(s)" in line
    assert "in use" in line
    assert "cores busy" in line


def test_the_worker_count_is_told_not_guessed():
    """The tree also holds a forkserver and a manager, which are not workers.
    Counting processes and calling them workers reported 2 for a 4-worker run."""
    from resources import ResourceMonitor

    assert "4 worker(s)" in ResourceMonitor().line(workers=4)
    assert "process(es)" in ResourceMonitor().line()     # honest when not told


def test_cpu_is_a_rate_not_a_running_total():
    """Time used only ever goes up, so it says nothing about what the run is
    doing now. Cores busy rises while reading and falls while waiting."""
    import time

    from resources import ResourceMonitor

    monitor = ResourceMonitor()
    assert monitor.sample()["cores_busy"] is None       # no rate from one sample

    start = time.monotonic()
    while time.monotonic() - start < 0.3:
        sum(index * index for index in range(100_000))
    busy = monitor.sample()["cores_busy"]
    assert busy is not None and busy > 0.1, f"expected a core to look busy, got {busy}"

    time.sleep(0.3)                                      # now do nothing
    assert monitor.sample()["cores_busy"] < busy


def test_a_worker_finishing_cannot_make_the_rate_negative():
    """CPU is tracked per process. Summing the tree and diffing the totals would
    go negative the moment a worker exited and took its time out of the sum."""
    from resources import ResourceMonitor

    monitor = ResourceMonitor()
    monitor.sample()
    monitor._cpu_by_pid[999_999] = 5.0      # a process that will not appear again
    monitor._cpu_total = sum(monitor._cpu_by_pid.values())

    assert monitor.sample()["cores_busy"] >= 0


def test_the_resource_line_can_be_turned_off():
    stream = FakeTerminal()
    display = ProgressDisplay(["a.csv"], stream=stream, show_resources=False)
    display.draw(force=True)
    assert "worker(s) running" not in stream.getvalue()


def test_the_resource_line_appears_above_the_bars():
    stream = FakeTerminal()
    display = ProgressDisplay(["a.csv"], stream=stream, show_resources=True,
                              workers=3)
    display.draw(force=True)

    lines = [l for l in stream.getvalue().splitlines() if l.strip()]
    assert "3 worker(s)" in lines[0]
    assert "a.csv" in lines[1]


def test_a_process_that_has_gone_is_skipped_not_raised():
    """Workers come and go between being listed and being read."""
    from resources import ResourceMonitor, process_tree

    assert process_tree(999_999_999) == []      # no such process, no exception
    assert ResourceMonitor(999_999_999).sample()["processes"] == 0


def test_a_sample_counts_memory_across_the_tree():
    from resources import ResourceMonitor

    reading = ResourceMonitor().sample()
    assert reading["processes"] >= 1
    assert reading["memory_bytes"] > 0


def test_the_tree_reaches_grandchildren_not_just_children():
    """Workers are started through a forkserver, so they are grandchildren.
    Walking one level found the forkserver and the manager -- two small idle
    processes -- and missed every worker, reporting 137 MB for a run using 740.
    """
    import os
    import subprocess
    import sys
    import time

    from resources import process_tree

    # a child that itself has a child, mirroring main -> forkserver -> worker
    grandparent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time;"
         "subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']);"
         "time.sleep(5)"]
    )
    try:
        time.sleep(1.5)
        found = [process.pid for process in process_tree(os.getpid())]

        assert grandparent.pid in found, "did not find the direct child"
        assert len(found) >= 3, f"grandchild missing; tree was {found}"
    finally:
        grandparent.kill()
        grandparent.wait()


# --- more files than screen -----------------------------------------------
#
# Redrawing works by moving the cursor up over the previous frame. The cursor
# cannot go above the top of the screen, so a frame taller than the terminal
# leaves it in the wrong place and the display scrolls away every refresh --
# which on a run over a thousand files looks like the screen flickering.

def many_files(count=1226):
    return [f"syslog-2024-{number:04d}.csv.gz" for number in range(count)]


@pytest.fixture
def short_terminal(monkeypatch):
    """Pretend the terminal is 24 lines tall, whatever is running the tests."""
    import progress
    size = os.terminal_size((100, 24))
    monkeypatch.setattr(progress.shutil, "get_terminal_size", lambda _fallback=None: size)
    return 24


def test_a_frame_never_grows_taller_than_the_terminal(short_terminal):
    names = many_files()
    stream = FakeTerminal()
    display = ProgressDisplay(names, stream=stream, show_resources=False, workers=250)
    display.draw(force=True)

    assert stream.getvalue().count("\n") <= short_terminal


def test_a_tall_frame_stays_bounded_as_the_run_progresses(short_terminal):
    names = many_files()
    stream = FakeTerminal()
    display = ProgressDisplay(names, stream=stream, show_resources=False, workers=250)

    for name in names[:400]:
        display.update(name, state="done", rows_read=1000, note="66 cols")
    for name in names[400:650]:
        display.update(name, state="reading", rows_read=500)

    stream.truncate(0)
    stream.seek(0)
    display.draw(force=True)
    assert stream.getvalue().count("\n") <= short_terminal


def test_every_file_is_still_accounted_for_when_most_are_hidden(short_terminal):
    names = many_files()
    stream = FakeTerminal()
    display = ProgressDisplay(names, stream=stream, show_resources=False, workers=250)
    for name in names[:400]:
        display.update(name, state="done", rows_read=1000)
    for name in names[400:650]:
        display.update(name, state="reading", rows_read=500)

    line = display.summary_line()
    assert "1,226 files" in line
    assert "400 done" in line
    assert "250 reading" in line
    assert "576 waiting" in line


def test_a_short_list_is_shown_in_full_as_before(short_terminal):
    """The windowing must not disturb runs that already fit on screen."""
    stream = FakeTerminal()
    display = ProgressDisplay(["a.csv", "b.csv", "c.csv"], stream=stream,
                              show_resources=False)
    display.draw(force=True)

    written = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(written) == 3               # no summary line, no "more below"
    assert not any("files:" in line for line in written)


def test_one_slow_file_does_not_pin_the_display(short_terminal):
    """A window chosen by position would sit on the straggler and look frozen.

    File 0 is still being read while files 1-800 have come and gone and 801
    onwards are live. The files moving right now have to be on screen.
    """
    names = many_files()
    stream = FakeTerminal()
    display = ProgressDisplay(names, stream=stream, show_resources=False, workers=250)

    display.update(names[0], state="reading", rows_read=9_000_000)
    for name in names[1:801]:
        display.update(name, state="done", rows_read=1000)
    for name in names[801:1051]:
        display.update(name, state="reading", rows_read=500)

    stream.truncate(0)
    stream.seek(0)
    display.draw(force=True)
    written = stream.getvalue()

    assert names[0] in written             # the straggler is still visible
    assert names[801] in written           # and so is what is moving now
    assert written.count("\n") <= short_terminal


def test_finishing_files_are_shown_when_there_is_room(short_terminal):
    """With few workers there is space left over; recent completions fill it."""
    names = many_files()
    stream = FakeTerminal()
    display = ProgressDisplay(names, stream=stream, show_resources=False, workers=8)
    for name in names[:1200]:
        display.update(name, state="done", rows_read=1000, note="66 cols")
    for name in names[1200:1206]:
        display.update(name, state="reading", rows_read=77)

    stream.truncate(0)
    stream.seek(0)
    display.draw(force=True)
    written = stream.getvalue()

    assert names[1200] in written          # being read
    assert names[1199] in written          # finished most recently
    assert "done" in written
    assert written.count("\n") <= short_terminal


def test_a_file_records_when_it_finished():
    display = ProgressDisplay(["a.csv"], show_resources=False)
    assert display.by_name["a.csv"].finished_at is None
    display.update("a.csv", state="done")
    assert display.by_name["a.csv"].finished_at is not None


def test_the_resource_line_is_not_read_on_the_redraw_clock():
    """Walking every worker's /proc five times a second is work not spent reading.

    The two rates are deliberately different: the display redraws several times
    a second, the resource figures are refreshed every few seconds.
    """
    from progress import REDRAW_SECONDS, RESOURCE_SECONDS
    assert RESOURCE_SECONDS > REDRAW_SECONDS
    assert RESOURCE_SECONDS >= 5.0
