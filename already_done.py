#!/usr/bin/env python3
"""Recognise files a previous run already documented, and reuse its findings.

A run over a few hundred gigabytes can be interrupted -- a job hits its wall
clock, a node falls over, someone adds twenty files to a directory that was
documented last week. Reading all of it again to produce output that is already
sitting there is wasted time, so --skip-existing leaves those files alone.

This is deliberately not the same as checkpointing. A checkpoint carries one
half-read file onwards mid-run; this skips a file whose output is already
complete. Checkpoints notice when a file has changed underneath them, by size
and modification time. This does not: it only looks at whether the output is
there. Told to skip, it skips -- so a file edited since it was documented keeps
its old description until asked to reprocess. That is the point of the flag,
but it is worth knowing.

The index is rebuilt from scratch on every run, so a skipped file would leave a
hole in it. The par-baked Croissant already holds everything the index says
about a file, so the entry is read back from there rather than being reduced to
"skipped".
"""

import json

from settings import (
    CROISSANT_SUBDIRECTORY,
    MARKDOWN_SUBDIRECTORY,
    SINGLE_VALUE_THRESHOLD,
    TEXT_SUBDIRECTORY,
)
from identifiers import names_worth_checking
from sources import dataset_stem


def expected_outputs(output_directory, csv_file, want_croissant=True):
    """The files a finished run leaves behind for one CSV.

    want_croissant follows --no-croissant: a run made without Croissants is
    complete without them, and should not be made to do the whole directory
    again because of output it was never asked for.
    """
    stem = dataset_stem(csv_file)
    outputs = {"report": output_directory / TEXT_SUBDIRECTORY / f"{stem}.txt"}
    if want_croissant:
        outputs["croissant"] = (output_directory / CROISSANT_SUBDIRECTORY
                                / f"{stem}.parbaked.json")
        outputs["markdown"] = (output_directory / MARKDOWN_SUBDIRECTORY
                               / f"{stem}.parbaked.md")
    return outputs


def already_documented(output_directory, csv_file, want_croissant=True):
    """True when every output this run would write is already there.

    Every one of them, not any: a run killed partway through a file can leave a
    report with no Croissant beside it, and treating that as done would bake in
    the half-finished state.
    """
    outputs = expected_outputs(output_directory, csv_file, want_croissant)
    return all(path.exists() for path in outputs.values())


def outcome_from_previous_run(output_directory, csv_file, want_croissant=True):
    """Rebuild a skipped file's index entry from what the last run wrote.

    Returns an outcome shaped like the one describe_one_csv returns, so the
    index cannot tell the difference, with "skipped" set so the display can.

    Falls back to a bare entry when there is no Croissant to read, or when it
    cannot be read: the file is still skipped -- the output is there and the
    flag said to leave it -- but nothing is claimed about what is in it.
    """
    outputs = expected_outputs(output_directory, csv_file, want_croissant)
    outcome = {
        "file": csv_file,
        "ok": True,
        "skipped": True,
        "report_path": outputs["report"],
        "croissant_path": outputs.get("croissant"),
        "markdown_path": outputs.get("markdown"),
        "croissant_problem": None,
        "seconds": 0.0,
    }

    document = _read_croissant(outputs.get("croissant"))
    if document is None:
        return {**outcome, **_nothing_known()}
    return {**outcome, **_findings_from(document)}


def _read_croissant(path):
    if path is None or not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        # A truncated or hand-edited Croissant should cost a detailed index
        # entry, not the run.
        return None


def _nothing_known():
    """What to say when the previous run's findings cannot be read back.

    Columns worth checking are reported as unknown rather than as none. This
    tool flags and never certifies, and an empty list here would read as "there
    is nothing to check" about a file nobody has looked at.
    """
    return {
        "rows_read": 0,
        "column_count": 0,
        "scope": "not re-read; see the existing report",
        "columns_to_check": [],
        "check_results_unknown": True,
        "empty_columns": [],
        "constant_columns": [],
    }


def _findings_from(document):
    """The index's facts, taken from a par-baked Croissant."""
    provenance = document.get("_parbake", {})
    measurements = document.get("_parbake_measurements", {})
    if not isinstance(measurements, dict) or not measurements:
        return _nothing_known()

    # Croissants written before the anonymity flags were recorded have neither
    # key. Saying "none to check" for those would be a claim the file does not
    # support, so they are marked unknown instead.
    knows_about_checks = "columns_of_concern" in provenance

    return {
        "rows_read": provenance.get("rows_read", 0),
        "column_count": len(measurements),
        "scope": provenance.get("scope", "unknown"),
        "columns_to_check": names_worth_checking(
            provenance.get("columns_of_concern", [])),
        "check_results_unknown": not knows_about_checks,
        "empty_columns": [name for name, column in measurements.items()
                          if _is_all_empty(column)],
        "constant_columns": [name for name, column in measurements.items()
                             if _holds_one_value(column) and not _is_all_empty(column)],
    }


def _is_all_empty(column):
    rows_seen = column.get("rows_seen", 0)
    return bool(rows_seen) and column.get("empty_count", 0) == rows_seen


def _holds_one_value(column):
    """The same test the measuring code applies, against the recorded counts."""
    rows_seen = column.get("rows_seen", 0)
    most_common = column.get("most_common_values") or []
    if not rows_seen or not most_common:
        return False
    return most_common[0].get("count", 0) / rows_seen >= SINGLE_VALUE_THRESHOLD
