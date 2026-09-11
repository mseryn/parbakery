#!/usr/bin/env python3
"""Printing a description so a person can read it.

Kept apart from the measuring so that how something is shown can change without
touching what is measured."""

from settings import (
    DEFAULT_VALUES_SHOWN,
    MINIMUM_LISTING_COVERAGE,
    SINGLE_VALUE_THRESHOLD,
)


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

