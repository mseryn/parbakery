#!/usr/bin/env python3
"""Turning numbers into something a person can read at a glance.

Small enough to be tempting to write twice. It was, in three places, so it
lives here instead.
"""

BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")


def describe_bytes(count):
    """A byte count as text: 26.5 MB.

    There is no standard-library equivalent, and pulling in a package for six
    lines of arithmetic is a worse trade than owning them.
    """
    size = float(count)
    for unit in BYTE_UNITS:
        if size < 1024 or unit == BYTE_UNITS[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024


def describe_seconds(seconds):
    """A duration as text, short enough for a table: 45s, 3m12s, 1h04m."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def describe_number(value):
    """A number without inventing precision it does not have: 5 rather than 5.0."""
    if value is None:
        return "-"
    if float(value).is_integer() and abs(value) < 2 ** 53:
        return f"{int(value):,}"
    return f"{value:,.6g}"


def truncate_end(text, limit):
    """Shorten from the right, for a value in a table cell.

    textwrap.shorten is the obvious thing to reach for and does not fit: it cuts
    on word boundaries, so a value with no spaces is either kept whole or
    replaced entirely by the placeholder. "x3008c0s25b0n0,x3008c0s31b1n0"
    becomes just the ellipsis.
    """
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def truncate_start(text, limit):
    """Shorten from the left, for a filename, where the end is what identifies it.

    "ANL-ALCF-DJC-POLARIS_20220809_20221231.csv" tells you more from its tail
    than its head when several files share a prefix.
    """
    text = str(text)
    return text if len(text) <= limit else "..." + text[-(limit - 3):]
