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
import datetime
import multiprocessing
import os
import queue as queue_module
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

# Settings live in describe_csv so there is one place to change them.
from checkpoints import CheckpointStore
from describe_csv import describe_csv
from identifiers import IdentifierCheck
from reporting import describe_size, print_report
from settings import (
    CHECKPOINT_DIRECTORY_NAME,
    CHECKPOINT_EVERY_ROWS,
    CSV_SUFFIXES,
    DEFAULT_PREVIEW_ROWS,
    DEFAULT_VALUES_SHOWN,
    DEFAULT_VALUES_TRACKED,
)
from sources import dataset_stem, estimate_row_count, matched_csv_suffix
from parbaked_croissant import render_parbaked_markdown, write_parbaked_croissant
from progress import ProgressDisplay, print_key

DEFAULT_OUTPUT_DIRECTORY = "parbake_output"
INDEX_FILENAME = "DIRECTORY_DOCUMENTATION.txt"

# Output is sorted by kind, one subdirectory each, so a directory of fifty
# datasets does not become a heap of a hundred and fifty files. Everything here
# is par-baked -- unreviewed and machine-generated -- and the names say so.
#
# DIRECTORY_DOCUMENTATION.txt stays at the top level: it is the index to all
# three, and filing it under one of them would be odd.
CROISSANT_SUBDIRECTORY = "parbaked_croissants"     # .parbaked.json
MARKDOWN_SUBDIRECTORY = "parbaked_markdown"        # .parbaked.md
TEXT_SUBDIRECTORY = "parbaked_txt"                 # .txt, the readable reports

OUTPUT_SUBDIRECTORIES = (
    CROISSANT_SUBDIRECTORY, MARKDOWN_SUBDIRECTORY, TEXT_SUBDIRECTORY,
)

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
                     checkpoints=None):
    """Describe one CSV and write its report next to the index.

    Returns a dictionary summarising how it went, so the index can say what
    happened to every file -- including the ones that failed.
    """
    stem = dataset_stem(csv_file)
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
        )
    except Exception as problem:
        # One unreadable file must not stop the rest of the directory. The
        # reason is written out in full so it can be diagnosed later.
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            f"Could not read {csv_file}\n\n{traceback.format_exc()}",
            encoding="utf-8",
        )
        return {
            "file": csv_file,
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

    concerns = [
        entry["column_name"]
        for entry in result["identifiers"]["columns_of_concern"]
        if "NOT consistent" in entry["verdict"] or "path" in entry["verdict"]
    ]
    # Columns carrying nothing. Worth surfacing in the index: a file where a
    # third of the columns are empty or constant is a different proposition
    # from one where none are, and you should not have to open a report to see it.
    empty_columns = [name for name, column in result["columns"].items()
                     if column["is_all_empty"]]
    constant_columns = [name for name, column in result["columns"].items()
                        if column["holds_one_value"] and not column["is_all_empty"]]

    return {
        "file": csv_file,
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


def build_index(directory_to_scan, results, other_files, settings):
    """Build the text of DIRECTORY_DOCUMENTATION.txt."""
    scanned_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    lines = [
        "# DIRECTORY DOCUMENTATION",
        "",
        f"Directory scanned: {directory_to_scan}",
        f"Scanned at:        {scanned_at}",
        f"Read:              {settings['scope_description']}",
        "",
        f"# CSV files ({len(results)})",
        "",
    ]

    if not results:
        lines.append("  (none)")
    for outcome in results:
        name = outcome["file"].name
        size = describe_size(outcome["file"].stat().st_size)
        # Each file's entry runs to several lines, so they need separating or
        # one file's detail reads as the next file's heading.
        if lines[-1] != "":
            lines.append("")
        if not outcome["ok"]:
            lines.append(f"  {name}  ({size})")
            lines.append(f"      COULD NOT READ: {outcome['problem']}")
            lines.append(f"      details in {outcome['report_path'].name}")
            continue
        lines.append(f"  {name}  ({size})")
        lines.append(
            f"      {outcome['rows_read']:,} rows read, {outcome['column_count']} columns"
            f", {outcome['seconds']}s -- {outcome['scope']}"
        )
        if outcome["columns_to_check"]:
            lines.append(
                f"      check before sharing: {', '.join(outcome['columns_to_check'])}"
            )
        if outcome["empty_columns"]:
            lines.append(
                f"      empty in every row ({len(outcome['empty_columns'])}): "
                f"{', '.join(outcome['empty_columns'])}"
            )
        if outcome["constant_columns"]:
            lines.append(
                f"      one value covers 99%+ of rows "
                f"({len(outcome['constant_columns'])}): "
                f"{', '.join(outcome['constant_columns'])}"
            )
        lines.append(
            f"      described in {TEXT_SUBDIRECTORY}/{outcome['report_path'].name}")
        if outcome.get("croissant_path"):
            lines.append(
                f"      par-baked croissant: {CROISSANT_SUBDIRECTORY}/"
                f"{outcome['croissant_path'].name}"
                f" -- NOT REVIEWED, fails validation on purpose"
            )
            if outcome.get("markdown_path"):
                lines.append(
                    f"      rendered as: {MARKDOWN_SUBDIRECTORY}/"
                    f"{outcome['markdown_path'].name}")
        if outcome.get("croissant_problem"):
            lines.append(f"      croissant NOT written: {outcome['croissant_problem']}")
    lines.append("")

    lines.append(f"# Other files, not examined ({len(other_files)})")
    lines.append("")
    if other_files:
        for other_file in other_files:
            lines.append(f"  {other_file.name}  ({describe_size(other_file.stat().st_size)})")
    else:
        lines.append("  (none)")
    lines.append("")

    return "\n".join(lines)


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
    """
    (csv_file, output_directory, settings, progress_queue,
     checkpoint_directory, checkpoint_every) = job

    def report(rows_read, seconds):
        progress_queue.put(("progress", csv_file.name, rows_read, seconds))

    progress_queue.put(("start", csv_file.name, 0, 0.0))
    # Built inside the worker: a CheckpointStore is cheap to make and this keeps
    # the job tuple to plain data that pickles without fuss.
    checkpoints = CheckpointStore(checkpoint_directory,
                                  enabled=checkpoint_directory is not None,
                                  every_rows=checkpoint_every)
    outcome = describe_one_csv(csv_file, output_directory, settings,
                               on_progress=report, checkpoints=checkpoints)
    progress_queue.put(("finished", csv_file.name, outcome, 0.0))
    return outcome


def _apply_message(display, message):
    """Fold one message from a worker into the display."""
    kind, name, payload, seconds = message

    if kind == "start":
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


def run_in_parallel(csv_files, output_directory, settings, workers, display,
                    checkpoint_directory=None, checkpoint_every=CHECKPOINT_EVERY_ROWS):
    """Describe every file, several at a time, updating the display as they go."""
    results_by_name = {}

    # "fork" is the default on Linux but is unsafe here: the manager below runs
    # a thread, and forking a multi-threaded process can deadlock the child.
    # Python 3.12 warns about it and 3.14 changes the default. "forkserver"
    # forks from a clean single-threaded helper, which is both safe and quicker
    # to start than "spawn"; not every platform has it, so fall back.
    try:
        start_method = multiprocessing.get_context("forkserver")
    except ValueError:
        start_method = multiprocessing.get_context("spawn")

    # A managed queue can be passed to another process and written to from there.
    with start_method.Manager() as manager:
        progress_queue = manager.Queue()
        jobs = [(csv_file, output_directory, settings, progress_queue,
                 checkpoint_directory, checkpoint_every)
                for csv_file in csv_files]

        with ProcessPoolExecutor(max_workers=workers, mp_context=start_method) as pool:
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
                        "report_path": output_directory / TEXT_SUBDIRECTORY / f"{dataset_stem(csv_file)}.txt",
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
                      checkpoint_every=CHECKPOINT_EVERY_ROWS):
    """The same work in this process. Used with --workers 1, and easier to debug."""
    results = []
    for csv_file in csv_files:
        display.update(csv_file.name, state="reading")
        display.draw()

        def report(rows_read, seconds, name=csv_file.name):
            display.update(name, rows_read=rows_read, seconds=seconds)
            display.draw()

        checkpoints = CheckpointStore(checkpoint_directory,
                                      enabled=checkpoint_directory is not None,
                                      every_rows=checkpoint_every)
        outcome = describe_one_csv(csv_file, output_directory, settings,
                                   on_progress=report, checkpoints=checkpoints)
        _apply_message(display, ("finished", csv_file.name, outcome, 0.0))
        display.draw(force=True)
        results.append(outcome)
    return results


def document_directory(directory_to_scan, output_directory, settings, workers=1,
                       checkpoint_directory=None,
                       checkpoint_every=CHECKPOINT_EVERY_ROWS):
    """Document every CSV in a directory, and write the index.

    Returns (results, other_files).
    """
    output_directory.mkdir(parents=True, exist_ok=True)
    csv_files, other_files = find_files(directory_to_scan)

    active_workers = worker_count(workers, len(csv_files)) if csv_files else 1
    print(f"Scanning {directory_to_scan}")
    print(f"  {len(csv_files)} CSV file(s), {len(other_files)} other file(s)")
    print(f"  reading {settings['scope_description']}")
    print(f"  {active_workers} worker(s)")
    if checkpoint_directory:
        waiting = CheckpointStore(checkpoint_directory).outstanding()
        if waiting:
            print(f"  {len(waiting)} checkpoint(s) from an earlier run -- those "
                  "files will carry on rather than start again")
    else:
        print("  checkpointing off")
    print()
    print_key()
    print()

    if not csv_files:
        results = []
    else:
        # An estimate per file, so the bars have something to fill towards. For
        # a preview the target is the row limit, which we know exactly.
        estimated_rows = {}
        for csv_file in csv_files:
            estimate = estimate_row_count(csv_file)
            if settings["preview_rows"] is not None and estimate is not None:
                estimate = min(estimate, settings["preview_rows"])
            estimated_rows[csv_file.name] = estimate

        display = ProgressDisplay(
            [csv_file.name for csv_file in csv_files], estimated_rows,
            stream=sys.stdout, workers=active_workers,
        )
        display.draw(force=True)

        if active_workers == 1:
            results = run_one_at_a_time(csv_files, output_directory, settings,
                                        display, checkpoint_directory,
                                        checkpoint_every)
        else:
            results = run_in_parallel(
                csv_files, output_directory, settings, active_workers, display,
                checkpoint_directory, checkpoint_every,
            )
        display.finish()

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
    print(f"  {len(results) - len(failed)} of {len(results)} CSV file(s) described")
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
                       checkpoint_every=arguments.checkpoint_every)


if __name__ == "__main__":
    main()
