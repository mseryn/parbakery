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
import traceback
from pathlib import Path

# Settings live in describe_csv so there is one place to change them.
from describe_csv import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_PREVIEW_ROWS,
    DEFAULT_VALUES_SHOWN,
    DEFAULT_VALUES_TRACKED,
    IdentifierCheck,
    describe_csv,
    describe_size,
    print_report,
)

CSV_EXTENSIONS = (".csv",)
DEFAULT_OUTPUT_DIRECTORY = "parbake_output"
INDEX_FILENAME = "DIRECTORY_DOCUMENTATION.txt"


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
        if entry.suffix.lower() in CSV_EXTENSIONS:
            csv_files.append(entry)
        else:
            other_files.append(entry)

    return csv_files, other_files


def describe_one_csv(csv_file, output_directory, settings):
    """Describe one CSV and write its report next to the index.

    Returns a dictionary summarising how it went, so the index can say what
    happened to every file -- including the ones that failed.
    """
    report_path = output_directory / f"{csv_file.stem}.txt"
    try:
        result = describe_csv(
            csv_file,
            separator=settings["separator"],
            preview_rows=settings["preview_rows"],
            batch_size=settings["batch_size"],
            values_tracked=settings["values_tracked"],
            values_shown=settings["values_shown"],
            identifier_check=None if settings["skip_identifier_check"] else IdentifierCheck(),
        )
    except Exception as problem:
        # One unreadable file must not stop the rest of the directory. The
        # reason is written out in full so it can be diagnosed later.
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
        lines.append(f"      described in {outcome['report_path'].name}")
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


def document_directory(directory_to_scan, output_directory, settings):
    """Document every CSV in a directory, and write the index.

    Returns (results, other_files).
    """
    output_directory.mkdir(parents=True, exist_ok=True)
    csv_files, other_files = find_files(directory_to_scan)

    print(f"Scanning {directory_to_scan}")
    print(f"  {len(csv_files)} CSV file(s), {len(other_files)} other file(s)")
    print(f"  reading {settings['scope_description']}")
    print()

    results = []
    for file_number, csv_file in enumerate(csv_files, start=1):
        size = describe_size(csv_file.stat().st_size)
        print(f"  [{file_number}/{len(csv_files)}] {csv_file.name}  ({size})", flush=True)

        outcome = describe_one_csv(csv_file, output_directory, settings)
        results.append(outcome)


        if outcome["ok"]:
            detail = (f"{outcome['rows_read']:,} rows, "
                      f"{outcome['column_count']} columns, {outcome['seconds']}s")
            carrying_nothing = len(outcome["empty_columns"]) + len(outcome["constant_columns"])
            if carrying_nothing:
                detail += f"  |  {carrying_nothing} column(s) carry no information"
            if outcome["columns_to_check"]:
                detail += f"  |  check: {', '.join(outcome['columns_to_check'])}"
            print(f"        {detail}")
        else:
            print(f"        COULD NOT READ: {outcome['problem']}")
        print()

    index_path = output_directory / INDEX_FILENAME
    index_path.write_text(
        build_index(directory_to_scan, results, other_files, settings), encoding="utf-8"
    )

    failed = [outcome for outcome in results if not outcome["ok"]]
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
    parser.add_argument("--separator", default=",", help="field separator (default: ,)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--values-tracked", type=int, default=DEFAULT_VALUES_TRACKED)
    parser.add_argument("--values-shown", type=int, default=DEFAULT_VALUES_SHOWN)
    parser.add_argument(
        "--no-identifier-check", action="store_true",
        help="skip the check for un-anonymised data",
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
        "scope_description": (
            "every row of every file"
            if preview_rows is None
            else f"the first {preview_rows:,} rows of each file"
        ),
    }

    document_directory(directory_to_scan, Path(arguments.out), settings)


if __name__ == "__main__":
    main()
