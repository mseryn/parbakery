#!/usr/bin/env python3
"""Describe what is in a CSV file.

Two jobs, both aimed at large files:

  * data quality -- what does each column actually contain?
  * validation   -- if this file is shared, is it the same file?

Two ways to run it. A preview reads only the first N rows, so you can see the
shape of a file in seconds. A full scan reads everything in batches, holding
only running totals in memory, so file size is not a limit.

Both modes measure exactly the same things, so a preview and a full scan can be
compared directly -- which is how you find out whether a preview can be trusted.

Everything is read as text. Nothing is converted on the way in, so the literal
word "NA" stays the word "NA" instead of quietly becoming a missing value.
Numbers are still measured; we just make a copy to measure rather than changing
what was read.

This module is the orchestration. The pieces live next door:

    settings.py     every constant
    sources.py      which files we read, compression, row estimates
    measuring.py    ColumnSummary -- the per-column accumulators
    identifiers.py  IdentifierCheck -- the un-anonymised data check
    checkpoints.py  saving progress so a killed run can be resumed
    reporting.py    printing the result

Usage:
    python3 describe_csv.py data.csv                # full scan
    python3 describe_csv.py data.csv --preview      # first 1000 rows
    python3 describe_csv.py data.csv --preview 5000
    python3 describe_csv.py data.csv --json summary.json
"""

import argparse
import sys
import time
from pathlib import Path

import pandas

from checkpoints import CheckpointStore, fingerprint_settings
from identifiers import IdentifierCheck
from measuring import ColumnSummary
from reporting import print_report
from settings import (
    DEFAULT_PREVIEW_ROWS,
    DEFAULT_VALUES_SHOWN,
    DEFAULT_VALUES_TRACKED,
    LITERAL_READ_SETTINGS,
)
from sources import (
    compression_of,
    rows_per_batch,
    dataset_stem,
    estimate_row_count,
    matched_csv_suffix,
    read_header,
)

# Re-exported so `from describe_csv import ...` keeps working for the things
# callers legitimately reach for. Everything else should be imported from the
# module that owns it.
__all__ = [
    "ColumnSummary", "IdentifierCheck", "describe_csv", "dataset_stem",
    "estimate_row_count", "matched_csv_suffix", "read_header", "print_report",
]


def describe_csv(
    path,
    separator=",",
    preview_rows=None,
    batch_size=None,
    values_tracked=DEFAULT_VALUES_TRACKED,
    values_shown=DEFAULT_VALUES_SHOWN,
    identifier_check=None,
    on_progress=None,
    checkpoints=None,
    dataset_stem_override=None,
):
    """Read a CSV and measure it.

    preview_rows=None reads the whole file. A number reads only that many rows.

    batch_size=None works one out from how wide the file is, so a worker uses
    about the same memory whatever shape the file is. Pass a number to override.

    checkpoints is an optional CheckpointStore. When given, progress is written
    out periodically and a previous run's progress is picked up, so a killed run
    does not start again from the beginning.

    identifier_check is an optional IdentifierCheck. When given, it runs inside
    this same read -- no second pass over the file.

    Returns a dictionary describing the file and every column in it.
    """
    path = Path(path)
    started_at = time.monotonic()

    column_names = read_header(path, separator)
    summaries = {
        name: ColumnSummary(name, position, values_tracked)
        for position, name in enumerate(column_names)
    }

    if batch_size is None:
        batch_size = rows_per_batch(len(column_names))
    # No point reading a big batch if we only want 1,000 rows.
    batch_rows = batch_size if preview_rows is None else min(batch_size, preview_rows)

    # Pick up where a killed run stopped, if there is anything to pick up.
    stem = dataset_stem_override or dataset_stem(path)
    settings_fingerprint = fingerprint_settings(separator, values_tracked, preview_rows)
    rows_read = 0
    resumed_from = None

    checkpoint_note = None
    if checkpoints is not None:
        saved, checkpoint_note = checkpoints.load(stem, path, settings_fingerprint)
        if saved:
            rows_read, summaries, identifier_state = checkpoints.rebuild(
                saved, column_names, values_tracked)
            if identifier_state and identifier_check is not None:
                identifier_check.restore(identifier_state)
            resumed_from = rows_read

    rows_at_last_save = rows_read

    # Resuming means skipping the header plus the rows already done. skiprows=N
    # on its own would eat the header and promote the next line into one, so the
    # names are supplied explicitly instead.
    read_arguments = {}
    if rows_read:
        read_arguments = {"header": None, "names": column_names,
                          "skiprows": rows_read + 1}

    batches = pandas.read_csv(
        path, sep=separator, chunksize=batch_rows,
        **read_arguments, **LITERAL_READ_SETTINGS
    )
    for batch in batches:
        if preview_rows is not None and rows_read + len(batch) > preview_rows:
            batch = batch.iloc[: preview_rows - rows_read]
        if batch.empty:
            break

        for name in column_names:
            summaries[name].add_batch(batch[name])
            if identifier_check is not None:
                identifier_check.check_batch(name, batch[name])
        rows_read += len(batch)

        if checkpoints is not None and checkpoints.is_due(rows_read, rows_at_last_save):
            checkpoints.save(stem, path, settings_fingerprint, rows_read,
                             summaries, identifier_check)
            rows_at_last_save = rows_read

        if on_progress is not None:
            on_progress(rows_read, time.monotonic() - started_at)

        if preview_rows is not None and rows_read >= preview_rows:
            break

    # This file is done, so its checkpoint has nothing left to offer.
    if checkpoints is not None:
        checkpoints.discard(stem)

    identifier_report = {
        "columns_of_concern": (
            identifier_check.columns_of_concern() if identifier_check else []
        ),
        "checked": identifier_check is not None,
    }

    return {
        "file": {
            "path": str(path.resolve()),
            "size_in_bytes": path.stat().st_size,
            "column_names": column_names,
            "column_count": len(column_names),
            "rows_read": rows_read,
            "scope": ("full scan" if preview_rows is None
                      else f"preview, first {preview_rows} rows"),
            "seconds_taken": round(time.monotonic() - started_at, 2),
            "resumed_from_row": resumed_from,
            "checkpoint_note": checkpoint_note,
        },
        "columns": {name: summary.as_dict(values_shown)
                    for name, summary in summaries.items()},
        "identifiers": identifier_report,
        # Kept so callers can ask for more detail than as_dict() gives.
        "_summaries": summaries,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_path", help="the CSV file to describe")
    parser.add_argument(
        "--preview", nargs="?", type=int, const=DEFAULT_PREVIEW_ROWS, default=None,
        metavar="N",
        help=f"read only the first N rows (default {DEFAULT_PREVIEW_ROWS} if no "
             "number given). Without this, the whole file is read.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=None, metavar="N",
        help="rows read at a time. By default this is worked out from how wide "
             "the file is, so memory stays about the same whatever the shape.",
    )
    parser.add_argument(
        "--values-tracked", type=int, default=DEFAULT_VALUES_TRACKED,
        help=f"different values counted per column (default {DEFAULT_VALUES_TRACKED:,})",
    )
    parser.add_argument(
        "--values-shown", type=int, default=DEFAULT_VALUES_SHOWN,
        help=f"values listed per column in the report (default {DEFAULT_VALUES_SHOWN})",
    )
    parser.add_argument(
        "--no-identifier-check", action="store_true",
        help="skip the check for un-anonymised data entirely",
    )
    parser.add_argument("--separator", default=",", help="field separator (default: ,)")
    parser.add_argument("--json", dest="json_path", help="also write the summary as JSON")
    parser.add_argument("--quiet", action="store_true", help="do not show progress while reading")
    arguments = parser.parse_args()

    csv_path = Path(arguments.csv_path)
    if not csv_path.is_file():
        raise SystemExit(f"No such file: {csv_path}")

    def show_progress(rows_read, seconds):
        rate = rows_read / seconds if seconds else 0
        print(f"\r  read {rows_read:,} rows  ({rate:,.0f} rows/s)", end="", file=sys.stderr)

    identifier_check = None if arguments.no_identifier_check else IdentifierCheck()

    result = describe_csv(
        csv_path,
        separator=arguments.separator,
        preview_rows=arguments.preview,
        batch_size=arguments.batch_size,
        values_tracked=arguments.values_tracked,
        values_shown=arguments.values_shown,
        identifier_check=identifier_check,
        on_progress=None if arguments.quiet else show_progress,
    )
    if not arguments.quiet:
        print(file=sys.stderr)

    print_report(result, arguments.values_shown)

    if arguments.json_path:
        import json
        # _summaries holds live objects, not something JSON can write.
        payload = {key: value for key, value in result.items() if key != "_summaries"}
        Path(arguments.json_path).write_text(json.dumps(payload, indent=2, default=str))
        print(f"\nJSON written to {arguments.json_path}")


if __name__ == "__main__":
    main()
