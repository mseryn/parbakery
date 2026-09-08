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

import numpy
import pandas


# --- settings you might reasonably change ---------------------------------

# Text that usually means "no value" but is stored as letters. We count these
# separately from genuinely empty fields, so you can see which one you have.
# Compared without regard to upper/lower case. Edit this list to taste.
NULL_LIKE_TEXT = frozenset({"na", "n/a", "null", "nan", "none"})

# How many different values we keep track of per column. Beyond this we stop
# taking note of new ones, which keeps memory flat on a column like a row ID
# where every value is different.
DEFAULT_VALUES_TRACKED = 1000

# A column "holds one value" when a single value covers at least this share of
# ALL its rows -- blanks included, so a column that is half empty does not
# qualify. At 0.99 a column is allowed a 1% margin of anything else, which
# catches a constant column that has a handful of stray rows in it.
SINGLE_VALUE_THRESHOLD = 0.99

# How many of those we actually show in the report.
DEFAULT_VALUES_SHOWN = 10

# Column names that often hold something identifying. A column whose name
# matches one of these is flagged for a person to look at, whatever it contains
# -- this costs nothing, because it only looks at the header.
#
# THIS LIST NEEDS WORK. Add to it freely; matching is case-insensitive and looks
# for the word anywhere in the column name, so "username" matches
# "USERNAME_GENID" and "submitting_username" alike.
COLUMN_NAMES_TO_WATCH = (
    "user", "username", "login", "uid", "owner", "submitter",
    "email", "mail", "name", "project", "account", "person",
)

# How many values we look at when asking "does this column hold paths?".
# Asked of every column of every batch, so it is deliberately a peek, not a scan.
PATH_PROBE_VALUES = 1000

# A list of values is only worth printing when the values repeat. If the values
# we would show together account for less than this share of the rows, the list
# is a sample of a long tail and says nothing about the column, so we print one
# summary line instead. Measured on real files: identifier and timestamp columns
# land under 1%, while columns with a real shape to them land well above 10%.
MINIMUM_LISTING_COVERAGE = 0.10

# Rows read at a time during a full scan. Bigger is faster but uses more memory.
DEFAULT_BATCH_SIZE = 500_000

# Rows read by --preview when no number is given.
DEFAULT_PREVIEW_ROWS = 1000

# How pandas is told to read. These three keep the text exactly as written.
LITERAL_READ_SETTINGS = {
    "dtype": str,           # never convert anything to a number on the way in
    "keep_default_na": False,   # "NA" stays the word "NA"
    "na_filter": False,         # ...and so do "NULL", "None", "NaN"
    "skip_blank_lines": False,  # an empty line is a row, not nothing
}


class ColumnSummary:
    """Everything we are measuring about a single column.

    Updated one batch at a time. Nothing here grows with the size of the file
    except the tracked-values table, which has a hard ceiling.

    To measure something new, add it in three places: a starting value in
    __init__, the update in add_batch, and a line in as_dict.
    """

    def __init__(self, column_name, position, values_tracked=DEFAULT_VALUES_TRACKED):
        self.column_name = column_name
        self.position = position          # which column it is, left to right
        self.values_tracked = values_tracked

        self.rows_seen = 0
        self.empty_count = 0              # the field was blank
        self.null_like_text_count = 0     # the field said "NA", "NULL", ...

        # value -> how many times we saw it. Stops accepting new values once it
        # is full, but keeps counting the ones already in it.
        self.value_counts = {}
        self.stopped_tracking_new_values = False

        self.shortest_value_length = None
        self.longest_value_length = None

        # Numeric measurements. We keep these even if some values are not
        # numbers; not_a_number_count tells you how much to trust them.
        self.number_count = 0
        self.not_a_number_count = 0
        self.smallest_number = None
        self.largest_number = None

    # -- taking in a batch of values ---------------------------------------

    def add_batch(self, values):
        """Fold one batch of this column's values into the running totals."""
        self.rows_seen += len(values)

        is_empty = values == ""
        self.empty_count += int(is_empty.sum())

        filled_in = values[~is_empty]
        if filled_in.empty:
            return

        self.null_like_text_count += int(
            filled_in.str.lower().isin(NULL_LIKE_TEXT).sum()
        )

        self._measure_lengths(filled_in)
        self._count_values(filled_in)
        self._measure_numbers(filled_in)

    def _measure_lengths(self, filled_in):
        """Shortest and longest value, in characters.

        Useful for spotting truncation, or padding that should not be there.
        """
        lengths = filled_in.str.len()
        shortest, longest = int(lengths.min()), int(lengths.max())

        if self.shortest_value_length is None:
            self.shortest_value_length = shortest
            self.longest_value_length = longest
        else:
            self.shortest_value_length = min(self.shortest_value_length, shortest)
            self.longest_value_length = max(self.longest_value_length, longest)

    def _count_values(self, filled_in):
        """Count how often each value appears, up to the tracking ceiling.

        Once the table is full we stop adding new values but keep counting the
        ones already there. That keeps the counts for common values correct,
        which is what matters -- and distinct_count_is_at_least records that we
        stopped, so nobody mistakes a floor for a total.
        """
        for value, count in filled_in.value_counts().items():
            if value in self.value_counts:
                self.value_counts[value] += int(count)
            elif len(self.value_counts) < self.values_tracked:
                self.value_counts[value] = int(count)
            else:
                self.stopped_tracking_new_values = True

    def _measure_numbers(self, filled_in):
        """Measure the values that are numbers, without changing what was read.

        pandas.to_numeric turns anything it cannot read into "not a number", so
        counting those tells us how numeric the column really is. We also drop
        infinities, because the word "inf" in a text file is a word, not a
        measurement.
        """
        as_numbers = pandas.to_numeric(filled_in, errors="coerce")
        is_real_number = numpy.isfinite(as_numbers)

        numbers = as_numbers[is_real_number]
        self.number_count += len(numbers)
        self.not_a_number_count += len(filled_in) - len(numbers)

        if numbers.empty:
            return

        smallest, largest = float(numbers.min()), float(numbers.max())
        self.smallest_number = (
            smallest if self.smallest_number is None
            else min(self.smallest_number, smallest)
        )
        self.largest_number = (
            largest if self.largest_number is None
            else max(self.largest_number, largest)
        )

    # -- reading the results back out --------------------------------------

    @property
    def filled_in_count(self):
        """Rows where the field was not blank."""
        return self.rows_seen - self.empty_count

    @property
    def distinct_count(self):
        """How many different values we saw.

        A floor, not a total, if stopped_tracking_new_values is set.
        """
        return len(self.value_counts)

    @property
    def is_all_numbers(self):
        """True if every filled-in value was a number."""
        return self.filled_in_count > 0 and self.not_a_number_count == 0

    @property
    def is_all_empty(self):
        """Every row of this column is blank."""
        return self.rows_seen > 0 and self.empty_count == self.rows_seen

    @property
    def single_value_share(self):
        """What share of ALL rows the commonest value covers.

        Measured against every row rather than the filled-in ones, so a column
        that is half blank and half one value scores 0.5, not 1.0. It is not
        holding one value; it is mostly empty.
        """
        if not self.rows_seen or not self.value_counts:
            return 0.0
        return self.most_common(1)[0][1] / self.rows_seen

    @property
    def holds_one_value(self):
        """True when a single value covers all but a small margin of the rows."""
        return self.single_value_share >= SINGLE_VALUE_THRESHOLD

    def most_common(self, how_many):
        """The most frequent values, commonest first.

        Ties are broken by the value itself so that two runs over the same file
        produce the same report.
        """
        return sorted(
            self.value_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )[:how_many]

    def share_covered_by(self, values_and_counts):
        """What share of the filled-in rows these values account for.

        Tells you whether a list of values describes the column or just samples
        a long tail: twenty timestamps covering 0.8% of rows describe nothing.
        """
        if not self.filled_in_count:
            return 0.0
        return sum(count for _, count in values_and_counts) / self.filled_in_count

    def as_dict(self, values_shown=DEFAULT_VALUES_SHOWN):
        """A plain dictionary, for writing to JSON."""
        summary = {
            "position": self.position,
            "rows_seen": self.rows_seen,
            "empty_count": self.empty_count,
            "null_like_text_count": self.null_like_text_count,
            "distinct_count": self.distinct_count,
            "distinct_count_is_at_least": self.stopped_tracking_new_values,
            "shortest_value_length": self.shortest_value_length,
            "longest_value_length": self.longest_value_length,
            "most_common_values": [
                {"value": value, "count": count}
                for value, count in self.most_common(values_shown)
            ],
            "number_count": self.number_count,
            "not_a_number_count": self.not_a_number_count,
            "is_all_numbers": self.is_all_numbers,
            "is_all_empty": self.is_all_empty,
            "holds_one_value": self.holds_one_value,
            "single_value_share": round(self.single_value_share, 4),
        }
        if self.number_count:
            summary["smallest_number"] = self.smallest_number
            summary["largest_number"] = self.largest_number
        return summary


class IdentifierCheck:
    """Looks for data that has not been anonymised.

    Runs inside the normal read, so it costs no extra pass over the file.

    The idea needs nothing but the data itself: an ALCF username always contains
    letters, while an anonymised id is a hash reduced to an integer, so it is all
    digits. A column called "username" whose values are all digits has been
    anonymised. The same column with letters in it has not.

    Two kinds of column get looked at:

      named      the column's NAME suggests it holds something identifying
      paths      the values look like file paths, which can carry a username
                 inside them even when the column name gives nothing away

    This cannot tell you a file is safe to share. It tells you where to look.
    """

    def __init__(self):
        self.shapes = {}      # column -> counts, see _record_shape

    # -- deciding which columns deserve attention --------------------------

    @staticmethod
    def name_suggests_identifier(column_name):
        """Does the column's NAME suggest it holds something identifying?"""
        lowered = column_name.lower()
        return [word for word in COLUMN_NAMES_TO_WATCH if word in lowered]

    @staticmethod
    def values_look_like_paths(values):
        """Do these values look like file paths?

        A path is where a username hides without the column being called
        anything suspicious -- "/home/chulwoo/run.py". Checked with startswith
        rather than a pattern, because that is all "looks like a path" means
        here.

        Only the first PATH_PROBE_VALUES values are looked at. This question is
        asked about every column of every batch, and scanning entire columns to
        answer it cost more than every other part of this check put together --
        0.30s against 0.015s on a 66-column file. A column of paths shows paths
        immediately; one that does not is not a column of paths. The full share
        is still counted exactly, once a column is being tracked.
        """
        return bool(values.head(PATH_PROBE_VALUES).str.startswith("/").any())

    # -- taking in a batch --------------------------------------------------

    def check_batch(self, column_name, values):
        """Look at one column of one batch."""
        filled_in = values[values != ""]
        if filled_in.empty:
            return

        watched_by_name = bool(self.name_suggests_identifier(column_name))
        looks_like_paths = self.values_look_like_paths(filled_in)

        # Skipping every other column is most of why this is cheap.
        if watched_by_name or looks_like_paths:
            self._record_shape(column_name, filled_in, watched_by_name)

    def _record_shape(self, column_name, filled_in, watched_by_name):
        """Count how many values are all digits, and how many look like paths.

        all-digits is the anonymisation signal: an ALCF username cannot be all
        digits, so a username column that is 100% digits has been through an
        anonymiser, and one that is not, has not.
        """
        shape = self.shapes.setdefault(column_name, {
            "values_seen": 0,
            "all_digit_values": 0,
            "path_like_values": 0,
            "watched_by_name": watched_by_name,
            "matched_words": self.name_suggests_identifier(column_name),
            "example": filled_in.iloc[0],
        })
        shape["values_seen"] += len(filled_in)
        shape["all_digit_values"] += int(filled_in.str.isdigit().sum())
        shape["path_like_values"] += int(filled_in.str.startswith("/").sum())

    # -- reading the results back out --------------------------------------

    def columns_of_concern(self):
        """Columns worth a person's attention, with why.

        verdict describes the shape; it is not a judgement:
          all digits          consistent with an anonymised id
          contains letters    NOT consistent with an anonymised id
          holds paths         a path can carry a username inside it
        """
        results = []
        for column_name, shape in sorted(self.shapes.items()):
            seen = shape["values_seen"]
            digit_share = shape["all_digit_values"] / seen if seen else 0
            path_share = shape["path_like_values"] / seen if seen else 0

            if path_share > 0:
                verdict = "holds paths -- a path can carry a username inside it"
            elif digit_share == 1.0:
                verdict = "all digits -- consistent with an anonymised id"
            else:
                verdict = "contains letters -- NOT consistent with an anonymised id"

            results.append({
                "column_name": column_name,
                "watched_by_name": shape["watched_by_name"],
                "matched_words": shape["matched_words"],
                "all_digit_share": round(digit_share, 4),
                "path_like_share": round(path_share, 4),
                "verdict": verdict,
                "example": shape["example"],
            })
        return results


def read_header(path, separator):
    """Read just the column names, exactly as written, in file order."""
    header_frame = pandas.read_csv(
        path, sep=separator, nrows=0, **LITERAL_READ_SETTINGS
    )
    return list(header_frame.columns)


def describe_csv(
    path,
    separator=",",
    preview_rows=None,
    batch_size=DEFAULT_BATCH_SIZE,
    values_tracked=DEFAULT_VALUES_TRACKED,
    values_shown=DEFAULT_VALUES_SHOWN,
    identifier_check=None,
    on_progress=None,
):
    """Read a CSV and measure it.

    preview_rows=None reads the whole file. A number reads only that many rows.

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

    # No point reading in 500,000-row batches if we only want 1,000 rows.
    rows_per_batch = batch_size if preview_rows is None else min(batch_size, preview_rows)

    rows_read = 0
    batches = pandas.read_csv(
        path, sep=separator, chunksize=rows_per_batch, **LITERAL_READ_SETTINGS
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

        if on_progress is not None:
            on_progress(rows_read, time.monotonic() - started_at)

        if preview_rows is not None and rows_read >= preview_rows:
            break

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
        },
        "columns": {name: summary.as_dict(values_shown)
                    for name, summary in summaries.items()},
        "identifiers": identifier_report,
        # Kept so callers can ask for more detail than as_dict() gives.
        "_summaries": summaries,
    }


# --- showing the results --------------------------------------------------

def describe_size(size_in_bytes):
    """Turn a byte count into something readable, e.g. "26.5 MB"."""
    size = float(size_in_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size = size / 1024


def format_number(value):
    """Show a number without inventing decimal places it does not have."""
    if value is None:
        return "-"
    if float(value).is_integer() and abs(value) < 2 ** 53:
        return f"{int(value):,}"
    return f"{value:,.6g}"


def shorten(text, limit=28):
    """Keep a value short enough to fit in a table cell."""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def print_identifier_report(result):
    """Show what might not be anonymised.

    This never says a file is safe. It says what was looked at and what was
    seen; whether the file can be shared is a person's decision.
    """
    report = result["identifiers"]
    if not report["checked"]:
        return

    concerns = report["columns_of_concern"]
    if not concerns:
        print("\n  no columns matched the watch list and none look like paths")
        return

    print("\n  COLUMNS WORTH CHECKING BEFORE SHARING")
    name_width = max(len(entry["column_name"]) for entry in concerns)
    for entry in concerns:
        print(f"    {entry['column_name']:<{name_width}}  {entry['verdict']}")
        detail = []
        if entry["watched_by_name"]:
            detail.append(f"name matched: {', '.join(entry['matched_words'])}")
        if entry["all_digit_share"] < 1.0:
            detail.append(f"{1 - entry['all_digit_share']:.0%} of values contain letters")
        if entry["path_like_share"] > 0:
            detail.append(f"{entry['path_like_share']:.0%} look like paths")
        print(f"    {'':<{name_width}}  {'; '.join(detail)}")
        print(f"    {'':<{name_width}}  e.g. {entry['example'][:60]!r}")


def print_report(result, values_shown=DEFAULT_VALUES_SHOWN):
    """Print the summary as a table, then per-column detail."""
    file_details = result["file"]
    summaries = result["_summaries"]

    print(f"\n{file_details['path']}")
    print(f"  {describe_size(file_details['size_in_bytes'])} on disk"
          f"  |  {file_details['column_count']} columns"
          f"  |  {file_details['rows_read']:,} rows read"
          f"  |  {file_details['seconds_taken']}s")
    print(f"  {file_details['scope']}")

    print_identifier_report(result)

    # One row per column. The columns of this table are the measurements most
    # likely to make you stop and look closer.
    name_width = max(len(name) for name in summaries)
    header = (
        f"  {'column':<{name_width}}  {'empty':>9}  {'NA-ish':>7}  {'distinct':>9}"
        f"  {'shortest':>8}  {'longest':>7}  {'most common value':<30}  {'share':>6}"
    )
    print()
    print(header)
    print("  " + "-" * (len(header) - 2))

    for summary in summaries.values():
        top = summary.most_common(1)
        if top:
            top_value, top_count = top[0]
            share = top_count / summary.filled_in_count if summary.filled_in_count else 0
            top_text = shorten(repr(top_value), 28)
            share_text = f"{share:>5.1%}"
        else:
            top_text, share_text = "-", "-"

        distinct = f"{summary.distinct_count:,}"
        if summary.stopped_tracking_new_values:
            distinct = f"{summary.distinct_count:,}+"   # a floor, not a total

        print(
            f"  {summary.column_name:<{name_width}}"
            f"  {summary.empty_count:>9,}"
            f"  {summary.null_like_text_count:>7,}"
            f"  {distinct:>9}"
            f"  {str(summary.shortest_value_length or '-'):>8}"
            f"  {str(summary.longest_value_length or '-'):>7}"
            f"  {top_text:<30}"
            f"  {share_text:>6}"
        )

    # Columns that carry no information. Worth seeing together, because a
    # column that is empty or always the same is usually either a field the
    # export never populated or one that does not apply to this machine.
    empty_columns = [s for s in summaries.values() if s.is_all_empty]
    constant_columns = [s for s in summaries.values()
                        if s.holds_one_value and not s.is_all_empty]

    if empty_columns or constant_columns:
        print("\n  COLUMNS CARRYING NO INFORMATION")
        if empty_columns:
            print(f"\n    empty in every row ({len(empty_columns)})")
            for summary in empty_columns:
                print(f"      {summary.column_name}")
        if constant_columns:
            print(f"\n    one value covers at least "
                  f"{SINGLE_VALUE_THRESHOLD:.0%} of rows ({len(constant_columns)})")
            width = max(len(s.column_name) for s in constant_columns)
            for summary in constant_columns:
                value, count = summary.most_common(1)[0]
                # A count, not a percentage: a column with 2 odd rows in 39,432
                # rounds to "0.00% is something else", which reads as nonsense.
                other_rows = summary.rows_seen - count
                note = "" if other_rows == 0 else (
                    f"   ({other_rows:,} of {summary.rows_seen:,} rows differ)"
                )
                print(f"      {summary.column_name:<{width}}  "
                      f"{shorten(repr(value), 30):<32}"
                      f"{summary.single_value_share:>6.1%}{note}")

    # Numbers, for the columns that have any.
    numeric_summaries = [s for s in summaries.values() if s.number_count]
    if numeric_summaries:
        print(f"\n  numbers")
        header = (
            f"  {'column':<{name_width}}  {'numbers':>12}  {'not numbers':>12}"
            f"  {'smallest':>16}  {'largest':>16}"
        )
        print(header)
        print("  " + "-" * (len(header) - 2))
        for summary in numeric_summaries:
            print(
                f"  {summary.column_name:<{name_width}}"
                f"  {summary.number_count:>12,}"
                f"  {summary.not_a_number_count:>12,}"
                f"  {format_number(summary.smallest_number):>16}"
                f"  {format_number(summary.largest_number):>16}"
            )

    # The values themselves -- but only where the list actually says something.
    print(f"\n  most common values (up to {values_shown} per column)")
    suppressed = []
    for summary in summaries.values():
        if not summary.value_counts:
            print(f"\n    {summary.column_name}: (no values -- every row was empty)")
            continue

        distinct_note = (
            f"{summary.distinct_count:,} or more distinct"
            if summary.stopped_tracking_new_values
            else f"{summary.distinct_count:,} distinct"
        )
        shown = summary.most_common(values_shown)
        coverage = summary.share_covered_by(shown)

        if coverage < MINIMUM_LISTING_COVERAGE:
            # A long tail. Printing twenty of its values would be a sample of
            # noise, so say what the list would have shown instead.
            top_value, top_count = shown[0]
            top_share = top_count / summary.filled_in_count
            suppressed.append(
                f"    {summary.column_name}  ({distinct_note}) -- list suppressed, "
                f"top {len(shown)} cover only {coverage:.1%} of rows; "
                f"most common {shorten(repr(top_value), 30)} at {top_share:.1%}"
            )
            continue

        print(f"\n    {summary.column_name}  ({distinct_note})")
        for value, count in shown:
            share = count / summary.filled_in_count if summary.filled_in_count else 0
            print(f"      {shorten(repr(value), 40):<42} {count:>12,}  {share:>6.1%}")

    if suppressed:
        print(f"\n  columns whose values do not repeat enough to list "
              f"(under {MINIMUM_LISTING_COVERAGE:.0%} coverage)")
        for line in suppressed:
            print(line)


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
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help=f"rows read at a time during a full scan (default {DEFAULT_BATCH_SIZE:,})",
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
