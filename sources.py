#!/usr/bin/env python3
"""Where the data comes from: which files we read, how they are compressed,
and roughly how many rows they hold.

Row estimates only ever feed a progress bar, so being a few percent out costs
nothing. Being wrong about compression costs everything, so that part is exact."""

import bz2
import lzma
import zlib
from pathlib import Path

import pandas

from settings import (
    CELL_BUDGET_PER_BATCH,
    COMPRESSION_SUFFIXES,
    CSV_SUFFIXES,
    EXACT_ROW_COUNT_LIMIT,
    LITERAL_READ_SETTINGS,
    MAXIMUM_BATCH_ROWS,
    MINIMUM_BATCH_ROWS,
)


def matched_csv_suffix(path):
    """The CSV suffix this path ends with, or "" if it is not one we read.

    Path.suffix is no use here: for "jobs.csv.gz" it returns ".gz", so a plain
    suffix check quietly treats every compressed file as something else. That
    was the whole bug.
    """
    name = str(path).lower()
    for suffix in CSV_SUFFIXES:
        if name.endswith(suffix):
            return suffix
    return ""


def dataset_stem(path):
    """The name without its extensions: jobs.csv.gz -> jobs."""
    path = Path(path)
    suffix = matched_csv_suffix(path)
    if suffix:
        return path.name[: -len(suffix)]
    return path.stem


def compression_of(path):
    """"gzip", "bz2", "xz", or None if the file is not compressed."""
    name = str(path).lower()
    for suffix in COMPRESSION_SUFFIXES:
        if name.endswith(suffix):
            return {".gz": "gzip", ".bz2": "bz2", ".xz": "xz"}[suffix]
    return None


def _decompressor_for(compression):
    if compression == "gzip":
        return zlib.decompressobj(32 + zlib.MAX_WBITS)   # 32 = accept a gzip header
    if compression == "bz2":
        return bz2.BZ2Decompressor()
    return lzma.LZMADecompressor()


def _estimate_compressed_rows(path, compression, want_bytes=1 << 18, chunk=1 << 15):
    """Estimate rows in a compressed file, from the ratio it actually achieves.

    Feeds compressed bytes in measured chunks and counts what comes out, so the
    ratio is real. Reading through gzip.open and asking the underlying file how
    far it got does not work -- buffering reads far ahead, and on a small file
    it swallows the lot, giving a ratio near 1 and an 85% error.
    """
    decompressor = _decompressor_for(compression)
    produced, compressed_fed = bytearray(), 0

    try:
        with open(path, "rb") as raw:
            while len(produced) < want_bytes:
                block = raw.read(chunk)
                if not block:
                    break
                compressed_fed += len(block)
                produced += decompressor.decompress(block)
    except Exception:
        # A truncated or unexpected file should cost a progress bar, not a run.
        return None

    newlines = produced.count(b"\n")
    if not compressed_fed or newlines < 2:
        return None

    ratio = len(produced) / compressed_fed
    average_line_bytes = len(produced) / newlines
    uncompressed_size = path.stat().st_size * ratio
    return max(1, int(uncompressed_size / average_line_bytes) - 1)


def estimate_row_count(path, sample_bytes=1 << 16, sample_points=8):
    """Roughly how many rows are in this file, without reading all of it.

    Takes a few samples from evenly spaced points, works out the average bytes
    per line across them, and divides the file size by it.

    Sampling only the start is not good enough. The Polaris export has a column
    holding node lists up to 2,048 characters, and its early rows are longer
    than its average, which made a start-only estimate 24% low. Spreading the
    samples brings that to under 3%.

    Only used to put a percentage on a progress bar, so a few percent out does
    not matter. Returns None if the file is too small or too odd to guess from.
    """
    path = Path(path)
    file_size = path.stat().st_size
    if file_size == 0:
        return None

    compression = compression_of(path)
    if compression:
        # Counting newlines in compressed bytes measures nothing.
        return _estimate_compressed_rows(path, compression)

    if file_size <= EXACT_ROW_COUNT_LIMIT:
        with open(path, "rb") as handle:
            newlines = sum(block.count(b"\n") for block in iter(
                lambda: handle.read(1 << 20), b""))
        return max(0, newlines - 1)     # the header is one of them

    total_bytes = 0
    total_newlines = 0
    with open(path, "rb") as handle:
        for point in range(sample_points):
            offset = int(file_size * point / sample_points)
            handle.seek(offset)
            if point > 0:
                # Landing mid-line would count a partial one, so start at the
                # next line break.
                handle.readline()
            sample = handle.read(sample_bytes)
            if not sample:
                continue
            # Drop the trailing partial line for the same reason.
            cut = sample.rfind(b"\n")
            if cut <= 0:
                continue
            total_bytes += cut + 1
            total_newlines += sample.count(b"\n", 0, cut + 1)

    if total_newlines < 2:
        return None

    average_line_bytes = total_bytes / total_newlines
    # The header is one of those lines, so take it back off the estimate.
    return max(1, int(file_size / average_line_bytes) - 1)


def read_header(path, separator):
    """Read just the column names, exactly as written, in file order."""
    header_frame = pandas.read_csv(
        path, sep=separator, nrows=0, **LITERAL_READ_SETTINGS
    )
    return list(header_frame.columns)


def rows_per_batch(column_count, cell_budget=CELL_BUDGET_PER_BATCH):
    """How many rows to read at a time, for a file this wide.

    Memory is rows times columns, so holding the number of rows fixed makes a
    wide file cost far more than a narrow one -- and a worker that is fine on
    one file gets killed on the next. Holding the number of CELLS roughly fixed
    makes a worker's footprint about the same whatever it is reading.
    """
    if column_count < 1:
        return MINIMUM_BATCH_ROWS
    wanted = cell_budget // column_count
    return max(MINIMUM_BATCH_ROWS, min(MAXIMUM_BATCH_ROWS, wanted))
