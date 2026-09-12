#!/usr/bin/env python3
"""Settings for reading and describing CSV files.

Everything here is meant to be changed. One place to look, one place to edit;
every other module imports from this one rather than keeping its own copy.

Where a value was chosen by measurement rather than taste, the comment says what
was measured. NOTES.md has the fuller reasoning.
"""

import sys

# parbake needs Python 3.8. Checked here because every other module imports this
# one, so the message arrives before anything else can fail confusingly.
#
# 3.8 is the floor for two reasons: ProcessPoolExecutor(mp_context=...), which is
# how the worker pool avoids forking a process that already has threads, and
# Path.unlink(missing_ok=True). On an older interpreter the first of those failed
# with a bare "TypeError: __init__() got an unexpected keyword argument", which
# says nothing useful about the cause.
if sys.version_info < (3, 8):
    raise SystemExit(
        "parbake needs Python 3.8 or newer; this is "
        f"{sys.version_info.major}.{sys.version_info.minor}.\n"
        "On a cluster there is usually a newer one available -- try\n"
        "    module avail python\n"
        "and load one, or point at it directly: /path/to/python3.11 parbakery.py ..."
    )


# --- which files we read --------------------------------------------------

# Compression pandas can decompress by itself, given the filename. Reading needs
# no special handling; estimating a row count does, because counting newlines in
# compressed bytes measures nothing.
#
# zip is left out deliberately: an archive can hold several files, and choosing
# one is a judgement rather than a lookup.
COMPRESSION_SUFFIXES = (".gz", ".bz2", ".xz")

# Everything treated as a CSV, longest first so ".csv.gz" wins over ".csv".
CSV_SUFFIXES = tuple(
    sorted((".csv",) + tuple(f".csv{c}" for c in COMPRESSION_SUFFIXES),
           key=len, reverse=True)
)

# How pandas is told to read. All four keep the text exactly as written.
LITERAL_READ_SETTINGS = {
    "dtype": str,               # never convert anything on the way in
    "keep_default_na": False,   # "NA" stays the word "NA"
    "na_filter": False,         # ...and so do "NULL", "None", "NaN"
    "skip_blank_lines": False,  # an empty line is a row, not nothing
}


# --- how much to read at a time -------------------------------------------

# Batch size in CELLS rather than rows, because memory is rows times columns.
# A fixed row count makes a 66-column file cost twenty times a 3-column one, and
# a worker that is fine on one file gets killed on the next.
#
# Measured on a 66-column file, peak RSS per worker: 660,000 cells came to
# 158 MB, 6,600,000 cells to 418 MB -- roughly 44 bytes a cell on a ~130 MB
# floor, with time barely moving. At eight workers the old fixed 100,000 rows
# came to 3.3 GB, which is enough to be killed on a modest node.
CELL_BUDGET_PER_BATCH = 1_000_000

# Bounds on the row count that budget works out to. The lower keeps a very wide
# file from reading a handful of rows at a time.
#
# The upper is 100,000 because that was the old fixed default and going above it
# made narrow files worse on both counts: a 3-column file at a 250,000 cap took
# 247 MB and 3.5 s against 201 MB and 3.2 s at 100,000. Capping here makes cell
# budgeting an improvement for wide files and a no-op for narrow ones.
MINIMUM_BATCH_ROWS = 2_000
MAXIMUM_BATCH_ROWS = 100_000

# Rows read by --preview when no number is given.
DEFAULT_PREVIEW_ROWS = 1000

# Files at or below this size have their rows counted exactly rather than
# estimated: reading 8 MB to count newlines takes hundredths of a second, and
# sampling a small file gives a worse answer than reading it.
EXACT_ROW_COUNT_LIMIT = 8 << 20


# --- what gets measured ---------------------------------------------------

# Text that usually means "no value" but is stored as letters. Counted
# separately from genuinely empty fields, so you can see which you have.
# Compared without regard to case.
NULL_LIKE_TEXT = frozenset({"na", "n/a", "null", "nan", "none"})

# Different values counted per column. Past this, new values stop being taken
# note of, which keeps memory flat on a column like a row ID where every value
# differs. The distinct count then becomes a floor, and says so.
DEFAULT_VALUES_TRACKED = 1000

# A column "holds one value" when one value covers at least this share of ALL
# its rows -- blanks included, so a half-empty column does not qualify. The 1%
# margin catches a constant column with a handful of stray rows in it.
SINGLE_VALUE_THRESHOLD = 0.99


# --- what gets shown ------------------------------------------------------

# Values listed per column in the report.
DEFAULT_VALUES_SHOWN = 10

# A list of values is only worth printing when the values repeat. Below this
# share of the rows the list is a sample of a long tail and describes nothing,
# so one summary line is printed instead. Measured on real files: identifier and
# timestamp columns land under 1%, columns with a real shape well above 10%.
MINIMUM_LISTING_COVERAGE = 0.10


# --- looking for data that has not been anonymised ------------------------

# Column names that often hold something identifying. Matching is
# case-insensitive and looks for the word anywhere in the name, so "username"
# matches "USERNAME_GENID" and "submitting_username" alike.
#
# THIS LIST NEEDS WORK -- the bare word "name" flags MACHINE_NAME and
# QUEUE_NAME, which are not identities.
COLUMN_NAMES_TO_WATCH = (
    "user", "username", "login", "uid", "owner", "submitter",
    "email", "mail", "name", "project", "account", "person",
)

# Values looked at when asking "does this column hold paths?". Asked of every
# column of every batch, so it is deliberately a peek rather than a scan.
PATH_PROBE_VALUES = 1000


# --- carrying on after a run is killed ------------------------------------

# Rows between checkpoints. No size threshold is needed: a file smaller than
# this never reaches one.
CHECKPOINT_EVERY_ROWS = 1_000_000

# Checkpoints live in their own directory inside the output, so the whole lot
# can be removed when a run finishes. A leading dot keeps them out of the way.
CHECKPOINT_DIRECTORY_NAME = ".parbake_checkpoints"


# --- copying files to local disk before reading them ----------------------

# On a parallel filesystem like Lustre, opening a file costs a round trip to a
# metadata server shared by the whole machine. A run over a thousand files
# spends that cost a thousand times before it reads anything worth having.
# Copying each file over in one sequential transfer and reading it locally
# trades many small waits for one large one.
#
# Off by default: it reads every byte of every file, so it only pays when the
# file was going to be read through anyway, and on a local filesystem it is
# pure loss.
STAGING_PREFIX = "parbake_staging_"


# --- where the output goes ------------------------------------------------

# Output is sorted by kind, one subdirectory each, so a directory of fifty
# datasets does not become a heap of a hundred and fifty files. Everything here
# is par-baked -- unreviewed and machine-generated -- and the names say so.
#
# DIRECTORY_DOCUMENTATION.txt stays at the top level: it is the index to all
# three, and filing it under one of them would be odd.
DEFAULT_OUTPUT_DIRECTORY = "parbake_output"
INDEX_FILENAME = "DIRECTORY_DOCUMENTATION.txt"

CROISSANT_SUBDIRECTORY = "parbaked_croissants"     # .parbaked.json
MARKDOWN_SUBDIRECTORY = "parbaked_markdown"        # .parbaked.md
TEXT_SUBDIRECTORY = "parbaked_txt"                 # .txt, the readable reports

OUTPUT_SUBDIRECTORIES = (
    CROISSANT_SUBDIRECTORY, MARKDOWN_SUBDIRECTORY, TEXT_SUBDIRECTORY,
)
