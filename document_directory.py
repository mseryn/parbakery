#!/usr/bin/env python3
"""Document a directory of data files.

Looks in a directory, finds the files, and writes:

  * DIRECTORY_DOCUMENTATION.txt -- the index: every file, and a summary line
    for each CSV it managed to read
  * <name>.txt                  -- a full description of each CSV, one per file

We are only interested in CSV files at the moment, but every file in the
directory gets listed one way or the other. A file that is silently left out of
the report is worse than one listed as "not a CSV" -- if you cannot see it, you
cannot tell whether it mattered.

Reading a preview is the default. These files can be hundreds of gigabytes, so
a full scan is something you ask for, never something that happens because you
pointed the tool at a directory and pressed enter.

Usage:
    python3 document_directory.py ../copies_of_data
    python3 document_directory.py ../copies_of_data --preview 10000
    python3 document_directory.py ../copies_of_data --full
    python3 document_directory.py ../copies_of_data --out somewhere_else
"""

import argparse
import contextlib
import multiprocessing
import os
import queue as queue_module
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from tempfile import gettempdir

from already_done import already_documented, outcome_from_previous_run
from checkpoints import CheckpointStore
from describe_csv import describe_csv
from identifiers import IdentifierCheck, names_worth_checking
from parbaked_croissant import render_parbaked_markdown, write_parbaked_croissant
from progress import ProgressDisplay, print_key
from reporting import build_index, print_report
from settings import (
    CHECKPOINT_DIRECTORY_NAME,
    CHECKPOINT_EVERY_ROWS,
    CROISSANT_SUBDIRECTORY,
    DEFAULT_OUTPUT_DIRECTORY,
    DEFAULT_PREVIEW_ROWS,
    DEFAULT_VALUES_SHOWN,
    DEFAULT_VALUES_TRACKED,
    INDEX_FILENAME,
    MARKDOWN_SUBDIRECTORY,
    TEXT_SUBDIRECTORY,
)
from sources import dataset_stem, estimate_row_count, matched_csv_suffix
from staging import StagingArea, staged_copy

# One worker per file, each in its own process. Reading a CSV is mostly pandas
# doing CPU work, so threads would queue up behind each other; separate
# processes actually run at the same time. More workers than files is waste.
DEFAULT_WORKER_LIMIT = 8


def find_files(directory_to_scan):
    """Sort a directory's files into CSVs and everything else.

    Only looks at the directory itself, not subdirectories -- we can add
    recursion later if the corpus needs it.

    Returns two lists of Path objects, each sorted by filename so that repeated
    runs produce the same report.
    """
    csv_files = []
    other_files = []

    for entry in sorted(directory_to_scan.iterdir()):
        if not entry.is_file():
            continue
        # Matched on the whole name, not Path.suffix: for "jobs.csv.gz" the
        # suffix is ".gz", so a suffix check silently skips every compressed file.
        if matched_csv_suffix(entry):
            csv_files.append(entry)
        else:
            other_files.append(entry)

    return csv_files, other_files


def describe_one_csv(csv_file, output_directory, settings, on_progress=None,
                     checkpoints=None, record_as=None):
    """Describe one CSV and write its report next to the index.

    record_as names the file to write down, when csv_file is a copy made on
    faster storage. Everything the run leaves behind -- the report, the index,
    the Croissant -- has to name the original, because the copy is deleted as
    soon as it has been read.

    Returns a dictionary summarising how it went, so the index can say what
    happened to every file -- including the ones that failed.
    """
    recorded_file = record_as if record_as is not None else csv_file
    stem = dataset_stem(recorded_file)
    report_path = output_directory / TEXT_SUBDIRECTORY / f"{stem}.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = describe_csv(
            csv_file,
            separator=settings["separator"],
            preview_rows=settings["preview_rows"],
            batch_size=settings["batch_size"],
            values_tracked=settings["values_tracked"],
            values_shown=settings["values_shown"],
            identifier_check=None if settings["skip_identifier_check"] else IdentifierCheck(),
            on_progress=on_progress,
            checkpoints=checkpoints,
            record_as=recorded_file,
        )
    except Exception as problem:
        # One unreadable file must not stop the rest of the directory. The
        # reason is written out in full so it can be diagnosed later.
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            f"Could not read {recorded_file}\n\n{traceback.format_exc()}",
            encoding="utf-8",
        )
        return {
            "file": recorded_file,
            "ok": False,
            "problem": f"{type(problem).__name__}: {problem}",
            "report_path": report_path,
        }

    # print_report writes to the screen; sending that to a file keeps one
    # version of the report rather than two that can drift apart.
    with open(report_path, "w", encoding="utf-8") as handle:
        with contextlib.redirect_stdout(handle):
            print_report(result, settings["values_shown"])

    # The par-baked Croissant, and its Markdown rendered by the same tool that
    # renders reviewed ones. A failure here must not lose the report we just
    # wrote, so it is recorded rather than raised.
    croissant_path = markdown_path = None
    croissant_problem = None
    if not settings.get("skip_croissant"):
        try:
            croissant_path = (output_directory / CROISSANT_SUBDIRECTORY
                              / f"{stem}.parbaked.json")
            document = write_parbaked_croissant(result, croissant_path)

            markdown_path = (output_directory / MARKDOWN_SUBDIRECTORY
                             / f"{stem}.parbaked.md")
            markdown_path.parent.mkdir(parents=True, exist_ok=True)
            markdown_path.write_text(render_parbaked_markdown(document), encoding="utf-8")
        except Exception as problem:
            croissant_problem = f"{type(problem).__name__}: {problem}"

    concerns = names_worth_checking(result["identifiers"]["columns_of_concern"])
    # Columns carrying nothing. Worth surfacing in the index: a file where a
    # third of the columns are empty or constant is a different proposition
    # from one where none are, and you should not have to open a report to see it.
    empty_columns = [name for name, column in result["columns"].items()
                     if column["is_all_empty"]]
    constant_columns = [name for name, column in result["columns"].items()
                        if column["holds_one_value"] and not column["is_all_empty"]]

    return {
        "file": recorded_file,
        "ok": True,
        "rows_read": result["file"]["rows_read"],
        "column_count": result["file"]["column_count"],
        "scope": result["file"]["scope"],
        "seconds": result["file"]["seconds_taken"],
        "columns_to_check": concerns,
        "empty_columns": empty_columns,
        "constant_columns": constant_columns,
        "report_path": report_path,
        "croissant_path": croissant_path,
        "markdown_path": markdown_path,
        "croissant_problem": croissant_problem,
    }


def worker_count(requested, file_count):
    """How many worker processes to actually start.

    Never more than there are files, because an idle worker still costs the
    memory of its own Python and pandas.
    """
    if requested is None:
        requested = min(DEFAULT_WORKER_LIMIT, os.cpu_count() or 1)
    return max(1, min(requested, file_count))


def _describe_in_worker(job):
    """Describe one file. Runs in a worker process.

    Progress goes back to the parent over a queue rather than being printed
    here: several workers writing to the same terminal at once would interleave
    into nonsense.

    When a staging directory is given, this worker copies its own file there,
    reads the copy, and deletes it before returning -- so it takes its next
    file the moment it is ready, without waiting for any other worker.
    """
    (csv_file, output_directory, settings, progress_queue,
     checkpoint_directory, checkpoint_every, staging_root) = job

    if staging_root is None:
        return _describe_and_report(csv_file, None, job)

    progress_queue.put(("copying", csv_file.name, 0, 0.0))
    with staged_copy(staging_root, csv_file) as (read_path, problem):
        # Estimating from the local copy costs a fraction of what it costs over
        # a filesystem where every open is a round trip, which is why the run
        # did not do it up front.
        progress_queue.put(
            ("estimate", csv_file.name, estimated_rows_for(read_path, settings), 0.0))
        outcome = _describe_and_report(read_path, csv_file, job)

    if problem is not None:
        outcome["staging_problem"] = f"{type(problem).__name__}: {problem}"
    return outcome


def _describe_and_report(read_path, record_as, job):
    """Read one file, sending progress back to the parent as it goes."""
    (csv_file, output_directory, settings, progress_queue,
     checkpoint_directory, checkpoint_every, _staging_root) = job

    # The display is keyed by the name of the file the user asked for, which is
    # not the file being read when a local copy was made.
    shown_name = csv_file.name

    def report(rows_read, seconds):
        progress_queue.put(("progress", shown_name, rows_read, seconds))

    progress_queue.put(("start", shown_name, 0, 0.0))
    # Built inside the worker: a CheckpointStore is cheap to make and this keeps
    # the job tuple to plain data that pickles without fuss.
    checkpoints = CheckpointStore(checkpoint_directory,
                                  enabled=checkpoint_directory is not None,
                                  every_rows=checkpoint_every)
    outcome = describe_one_csv(read_path, output_directory, settings,
                               on_progress=report, checkpoints=checkpoints,
                               record_as=record_as)
    progress_queue.put(("finished", shown_name, outcome, 0.0))
    return outcome


def _apply_message(display, message):
    """Fold one message from a worker into the display."""
    kind, name, payload, seconds = message

    if kind == "copying":
        display.update(name, state="copying")
    elif kind == "estimate":
        # Only sent when the file was copied locally; otherwise the parent
        # worked the estimate out before the run started.
        display.update(name, estimated_rows=payload)
    elif kind == "start":
        display.update(name, state="reading")
    elif kind == "progress":
        display.update(name, state="reading", rows_read=payload, seconds=seconds)
    elif kind == "finished":
        outcome = payload
        if outcome["ok"]:
            note = summarise_outcome(outcome)
            display.update(name, state="done", rows_read=outcome["rows_read"],
                           seconds=outcome["seconds"], note=note)
        else:
            display.update(name, state="failed", note=outcome["problem"][:60])


def summarise_outcome(outcome):
    """The short note shown beside a finished file."""
    parts = [f"{outcome['column_count']} cols"]
    constant = len(outcome["empty_columns"]) + len(outcome["constant_columns"])
    if constant:
        parts.append(f"{constant} constant")
    if outcome["columns_to_check"]:
        parts.append(f"check {len(outcome['columns_to_check'])}")
    return ", ".join(parts)


def estimated_rows_for(path, settings):
    """Roughly how many rows this file will yield, for the progress bar.

    A preview never reads past its row limit, so that is the target when one is
    set. Returns None when the file is too small or too odd to guess from, which
    the display shows as a "?" bar rather than a percentage.
    """
    estimate = estimate_row_count(path)
    if settings["preview_rows"] is not None and estimate is not None:
        return min(estimate, settings["preview_rows"])
    return estimate


def _pool_context():
    """The multiprocessing context workers are started from.

    "fork" is the default on Linux but is unsafe here: the manager below runs a
    thread, and forking a multi-threaded process can deadlock the child. Python
    3.12 warns about it and 3.14 changes the default. "forkserver" forks from a
    clean single-threaded helper, which is both safe and quicker to start than
    "spawn"; not every platform has it, so fall back.
    """
    try:
        return multiprocessing.get_context("forkserver")
    except ValueError:
        return multiprocessing.get_context("spawn")


def run_in_parallel(csv_files, output_directory, settings, workers, display,
                    checkpoint_directory=None, checkpoint_every=CHECKPOINT_EVERY_ROWS,
                    staging_root=None):
    """Describe every file, several at a time, updating the display as they go.

    Every file is handed to the pool at once and workers take the next one as
    soon as they are free. With staging_root set each worker also copies its
    own file before reading it, which keeps that cost off the critical path of
    every other worker.
    """
    results_by_name = {}
    context = _pool_context()

    # A managed queue can be passed to another process and written to from there.
    with context.Manager() as manager:
        progress_queue = manager.Queue()
        jobs = [(csv_file, output_directory, settings, progress_queue,
                 checkpoint_directory, checkpoint_every, staging_root)
                for csv_file in csv_files]

        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            futures = [pool.submit(_describe_in_worker, job) for job in jobs]

            while not all(future.done() for future in futures):
                _drain(progress_queue, display)
                display.draw()
                time.sleep(0.05)

            # Anything still in the queue after the last worker finished.
            _drain(progress_queue, display)

            for future, csv_file in zip(futures, csv_files):
                try:
                    results_by_name[csv_file.name] = future.result()
                except Exception as problem:
                    # A worker that died rather than returning a failure.
                    results_by_name[csv_file.name] = {
                        "file": csv_file, "ok": False,
                        "problem": f"worker failed: {type(problem).__name__}: {problem}",
                        "report_path": (output_directory / TEXT_SUBDIRECTORY
                                        / f"{dataset_stem(csv_file)}.txt"),
                    }
                    display.update(csv_file.name, state="failed",
                                   note=f"worker failed: {type(problem).__name__}")

    return [results_by_name[csv_file.name] for csv_file in csv_files]


def _drain(progress_queue, display):
    """Take everything waiting on the queue, without blocking."""
    while True:
        try:
            _apply_message(display, progress_queue.get_nowait())
        except queue_module.Empty:
            return


def run_one_at_a_time(csv_files, output_directory, settings, display,
                      checkpoint_directory=None,
                      checkpoint_every=CHECKPOINT_EVERY_ROWS, staging_root=None):
    """The same work in this process. Used with --workers 1, and easier to debug."""
    results = []
    for csv_file in csv_files:

        def report(rows_read, seconds, name=csv_file.name):
            display.update(name, rows_read=rows_read, seconds=seconds)
            display.draw()

        checkpoints = CheckpointStore(checkpoint_directory,
                                      enabled=checkpoint_directory is not None,
                                      every_rows=checkpoint_every)

        if staging_root is None:
            display.update(csv_file.name, state="reading")
            display.draw()
            outcome = describe_one_csv(csv_file, output_directory, settings,
                                       on_progress=report, checkpoints=checkpoints)
        else:
            display.update(csv_file.name, state="copying")
            display.draw(force=True)
            with staged_copy(staging_root, csv_file) as (read_path, problem):
                display.update(csv_file.name, state="reading",
                               estimated_rows=estimated_rows_for(read_path, settings))
                display.draw()
                outcome = describe_one_csv(read_path, output_directory, settings,
                                           on_progress=report,
                                           checkpoints=checkpoints,
                                           record_as=csv_file)
            if problem is not None:
                outcome["staging_problem"] = f"{type(problem).__name__}: {problem}"

        _apply_message(display, ("finished", csv_file.name, outcome, 0.0))
        display.draw(force=True)
        results.append(outcome)
    return results


def _split_off_already_done(csv_files, output_directory, settings, skip_existing):
    """Separate the files a previous run finished from the ones still to read.

    Decided here in the parent rather than in a worker, so a skipped file is
    never opened, never copied, and never costs a round trip to the shared
    filesystem -- which is the whole point of skipping it.
    """
    if not skip_existing:
        return csv_files, []

    want_croissant = not settings["skip_croissant"]
    still_to_do, skipped = [], []
    for csv_file in csv_files:
        if already_documented(output_directory, csv_file, want_croissant):
            skipped.append(csv_file)
        else:
            still_to_do.append(csv_file)
    return still_to_do, skipped


def _announce(directory_to_scan, csv_files, skipped, other_files, settings,
              active_workers, checkpoint_directory, stage_locally, staging_parent,
              skip_existing):
    """Say what is about to happen, before anything slow starts.

    Printed up front on purpose: on a directory of a thousand files the first
    real output is a while away, and a run that says nothing looks like a run
    that has hung.
    """
    print(f"Scanning {directory_to_scan}")
    print(f"  {len(csv_files) + len(skipped)} CSV file(s), "
          f"{len(other_files)} other file(s)")
    if skip_existing:
        print(f"  {len(skipped)} already documented, skipping "
              f"{'them' if len(skipped) != 1 else 'it'}; {len(csv_files)} to read")
    print(f"  reading {settings['scope_description']}")
    print(f"  {active_workers} worker(s)")

    if checkpoint_directory:
        waiting = CheckpointStore(checkpoint_directory).outstanding()
        if waiting:
            print(f"  {len(waiting)} checkpoint(s) from an earlier run -- those "
                  "files will carry on rather than start again")
    else:
        print("  checkpointing off")

    if stage_locally:
        where = staging_parent or os.environ.get("TMPDIR") or gettempdir()
        print(f"  each worker copies its file to {where} first, reads it there, "
              "then deletes it")
        print(f"  so never more than {active_workers} "
              f"{'copy' if active_workers == 1 else 'copies'} on local disk at once")

    print()
    print_key(staging=stage_locally)
    print()


def document_directory(directory_to_scan, output_directory, settings, workers=1,
                       checkpoint_directory=None,
                       checkpoint_every=CHECKPOINT_EVERY_ROWS,
                       stage_locally=False, staging_parent=None,
                       skip_existing=False):
    """Document every CSV in a directory, and write the index.

    stage_locally has each worker copy its file to local disk before reading
    it, for shared filesystems where opening a file is expensive. See staging.py.

    skip_existing leaves alone any file whose output is already there, and
    takes its index entry from the previous run. See already_done.py.

    Returns (results, other_files).
    """
    output_directory.mkdir(parents=True, exist_ok=True)
    csv_files, other_files = find_files(directory_to_scan)

    every_csv_file = csv_files
    csv_files, skipped = _split_off_already_done(
        csv_files, output_directory, settings, skip_existing)

    active_workers = worker_count(workers, len(csv_files)) if csv_files else 1
    _announce(directory_to_scan, csv_files, skipped, other_files, settings,
              active_workers, checkpoint_directory, stage_locally, staging_parent,
              skip_existing)

    if not csv_files:
        results = []
    else:
        # An estimate per file, so the bars have something to fill towards.
        #
        # When files are being copied locally each worker estimates its own,
        # from the copy, and sends the number back. Estimating them all here
        # first would mean opening every file over the slow filesystem before
        # anything appeared on screen, which is the delay that mode exists to
        # avoid. Those files start with a "?" bar and fill in as they begin.
        estimated_rows = {}
        if not stage_locally:
            for csv_file in csv_files:
                estimated_rows[csv_file.name] = estimated_rows_for(csv_file, settings)

        display = ProgressDisplay(
            [csv_file.name for csv_file in csv_files], estimated_rows,
            stream=sys.stdout, workers=active_workers,
        )
        display.draw(force=True)

        # One staging directory for the whole run, made here so that this
        # process is the one responsible for removing it. Workers copy into it
        # and clear up after themselves; this catches whatever a killed worker
        # could not.
        with ExitStack() as cleanup:
            staging_root = None
            if stage_locally:
                staging_root = cleanup.enter_context(StagingArea(staging_parent)).root

            if active_workers == 1:
                results = run_one_at_a_time(csv_files, output_directory, settings,
                                            display, checkpoint_directory,
                                            checkpoint_every, staging_root)
            else:
                results = run_in_parallel(
                    csv_files, output_directory, settings, active_workers, display,
                    checkpoint_directory, checkpoint_every, staging_root,
                )
        display.finish()

        for outcome in results:
            if outcome.get("staging_problem"):
                print(f"  could not copy {outcome['file'].name} locally, read it "
                      f"in place ({outcome['staging_problem']})")

    # Skipped files are kept out of the progress display -- there is no
    # progress to show -- but they belong in the index, in the order they
    # appear in the directory rather than bunched at one end.
    if skipped:
        want_croissant = not settings["skip_croissant"]
        reused = {csv_file: outcome_from_previous_run(
            output_directory, csv_file, want_croissant) for csv_file in skipped}
        by_file = {outcome["file"]: outcome for outcome in results}
        by_file.update(reused)
        results = [by_file[csv_file] for csv_file in every_csv_file
                   if csv_file in by_file]

    index_path = output_directory / INDEX_FILENAME
    index_path.write_text(
        build_index(directory_to_scan, results, other_files, settings), encoding="utf-8"
    )

    failed = [outcome for outcome in results if not outcome["ok"]]

    # Only clear the checkpoints if every file got through. If anything failed,
    # leaving them means the next run can carry on rather than start again.
    if checkpoint_directory and not failed:
        if CheckpointStore(checkpoint_directory).clear():
            pass                      # nothing left to resume, nothing to say

    print()
    read_this_time = len(results) - len(failed) - len(skipped)
    if skipped:
        print(f"  {read_this_time} CSV file(s) read, {len(skipped)} skipped "
              "as already documented")
    else:
        print(f"  {read_this_time} of {len(results)} CSV file(s) described")
    if failed:
        print(f"  {len(failed)} could not be read -- see the index")
    print(f"\nWrote {index_path}")
    print(f"      and {len(results)} report(s) in {output_directory}")

    return results, other_files


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "directory", nargs="?", default=".",
        help="directory to document (default: the current directory)",
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUTPUT_DIRECTORY,
        help=f"where to write the reports (default: ./{DEFAULT_OUTPUT_DIRECTORY})",
    )
    parser.add_argument(
        "--preview", nargs="?", type=int, const=DEFAULT_PREVIEW_ROWS,
        default=DEFAULT_PREVIEW_ROWS, metavar="N",
        help=f"read the first N rows of each file (default {DEFAULT_PREVIEW_ROWS})",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="read every file all the way through. These files can be hundreds "
             "of gigabytes, so this is never the default.",
    )
    parser.add_argument(
        "--workers", type=int, default=None, metavar="N",
        help=f"files to read at the same time, one process each "
             f"(default: up to {DEFAULT_WORKER_LIMIT}, capped by CPU count and file count)",
    )
    parser.add_argument("--separator", default=",", help="field separator (default: ,)")
    parser.add_argument(
        "--batch-size", type=int, default=None, metavar="N",
        help="rows read at a time per worker. By default this is worked out "
             "from how wide each file is, so memory stays about the same "
             "whatever the shape. Lower it if a run is being killed.",
    )
    parser.add_argument("--values-tracked", type=int, default=DEFAULT_VALUES_TRACKED)
    parser.add_argument("--values-shown", type=int, default=DEFAULT_VALUES_SHOWN)
    parser.add_argument(
        "--no-identifier-check", action="store_true",
        help="skip the check for un-anonymised data",
    )
    parser.add_argument(
        "--no-croissant", action="store_true",
        help="skip the par-baked Croissant files and their Markdown",
    )
    parser.add_argument(
        "--no-checkpoints", action="store_true",
        help="do not save progress. A killed run then starts again from the "
             "beginning rather than carrying on.",
    )
    parser.add_argument(
        "--checkpoint-every", type=int, default=CHECKPOINT_EVERY_ROWS, metavar="N",
        help=f"rows between saves (default {CHECKPOINT_EVERY_ROWS:,}). Lower it "
             "to lose less when a run is killed, at the cost of writing more often.",
    )
    parser.add_argument(
        "--checkpoint-dir", metavar="DIR", default=None,
        help=f"where progress is saved (default: <out>/{CHECKPOINT_DIRECTORY_NAME}). "
             "Removed when every file has been described.",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="leave alone any file whose output is already in --out, and take "
             "its index entry from the previous run. For carrying on after a "
             "run was interrupted, or adding new files to a directory that has "
             "already been documented. Off by default: normally every file is "
             "read again and its output overwritten. Note that this looks only "
             "at whether the output exists -- a file edited since it was "
             "documented keeps its old description until read again.",
    )
    parser.add_argument(
        "--batch-local-copies", action="store_true",
        help="have each worker copy its file to local disk and read it there. "
             "On a shared filesystem like Lustre, opening a file costs a round "
             "trip to a metadata server, and a run over hundreds of files spends "
             "most of its time waiting. A worker copies one file, reads it, and "
             "deletes the copy before taking the next, so there are never more "
             "copies -- or more readers on the shared filesystem -- than there "
             "are workers. Off by default: on a local filesystem it is pure loss.",
    )
    parser.add_argument(
        "--local-copy-dir", metavar="DIR", default=None,
        help="where --batch-local-copies puts the copies (default: $TMPDIR, "
             "which on a compute node is usually node-local storage). Each copy "
             "is removed as soon as its file has been read.",
    )
    arguments = parser.parse_args()

    directory_to_scan = Path(arguments.directory).resolve()
    if not directory_to_scan.is_dir():
        raise SystemExit(f"Not a directory: {directory_to_scan}")

    preview_rows = None if arguments.full else arguments.preview
    settings = {
        "separator": arguments.separator,
        "preview_rows": preview_rows,
        "batch_size": arguments.batch_size,
        "values_tracked": arguments.values_tracked,
        "values_shown": arguments.values_shown,
        "skip_identifier_check": arguments.no_identifier_check,
        "skip_croissant": arguments.no_croissant,
        "scope_description": (
            "every row of every file"
            if preview_rows is None
            else f"the first {preview_rows:,} rows of each file"
        ),
    }

    output_directory = Path(arguments.out)
    checkpoint_directory = None
    if not arguments.no_checkpoints:
        checkpoint_directory = Path(
            arguments.checkpoint_dir or output_directory / CHECKPOINT_DIRECTORY_NAME)

    document_directory(directory_to_scan, output_directory, settings,
                       workers=arguments.workers,
                       checkpoint_directory=checkpoint_directory,
                       checkpoint_every=arguments.checkpoint_every,
                       stage_locally=arguments.batch_local_copies,
                       staging_parent=arguments.local_copy_dir,
                       skip_existing=arguments.skip_existing)


if __name__ == "__main__":
    main()
