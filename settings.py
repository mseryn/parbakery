#!/usr/bin/env python3
"""Settings for reading and describing CSV files.

Everything here is meant to be changed. One place to look, one place to edit;
every other module imports from this one rather than keeping its own copy."""

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

# Compression we can read. pandas decompresses these itself based on the
# filename, so reading needs no special handling -- but estimating a row count
# does, because counting newlines in compressed bytes is meaningless.
#
# zip is left out deliberately: an archive can hold several files, and deciding
# which one is the dataset is a judgement rather than a lookup.
COMPRESSION_SUFFIXES = (".gz", ".bz2", ".xz")

# Everything we treat as a CSV, longest first so ".csv.gz" wins over ".csv".
CSV_SUFFIXES = tuple(
    sorted((".csv",) + tuple(f".csv{c}" for c in COMPRESSION_SUFFIXES),
           key=len, reverse=True)
)

# How big a batch to read, expressed in CELLS rather than rows.
#
# Memory is rows x columns, so a fixed row count means a 66-column file uses
# twenty times the memory of a 3-column one. Measured on a 66-column file, peak
# RSS per worker was:
#
#       10,000 rows =   660,000 cells   158 MB   11.9 s
#       25,000 rows = 1,650,000 cells   210 MB   11.1 s
#       50,000 rows = 3,300,000 cells   297 MB   10.7 s
#      100,000 rows = 6,600,000 cells   418 MB   10.3 s
#
# About 44 bytes per cell on top of a ~130 MB floor, and time barely moves. At
# eight workers the old 100,000-row default came to 3.3 GB, which is enough to
# be killed on a modest node. A cell budget keeps a worker's memory roughly the
# same whatever shape the file is.
CELL_BUDGET_PER_BATCH = 1_000_000

# Bounds on the row count the budget works out to.
#
# The lower one keeps a very wide file from reading a handful of rows at a time.
#
# The upper one is 100,000 because that was the old fixed default, and going
# above it made narrow files worse on both counts. Measured on a 3-column file,
# a 250,000-row cap gave 247 MB and 3.5 s against 201 MB and 3.2 s at 100,000 --
# more memory AND slower, for nothing. Capping here means the change is an
# improvement for wide files and a no-op for narrow ones, rather than a trade.
MINIMUM_BATCH_ROWS = 2_000
MAXIMUM_BATCH_ROWS = 100_000

# Rows read by --preview when no number is given.
DEFAULT_PREVIEW_ROWS = 1000

# How pandas is told to read. These three keep the text exactly as written.
LITERAL_READ_SETTINGS = {
    "dtype": str,           # never convert anything to a number on the way in
    "keep_default_na": False,   # "NA" stays the word "NA"
    "na_filter": False,         # ...and so do "NULL", "None", "NaN"
    "skip_blank_lines": False,  # an empty line is a row, not nothing
}


# Files at or below this size are counted exactly rather than estimated:
# reading 8 MB to count newlines takes a few hundredths of a second, and
# sampling a small file gives a worse answer than just reading it.
EXACT_ROW_COUNT_LIMIT = 8 << 20


# --- checkpointing, for runs that get killed ------------------------------

# Rows between checkpoints. No size threshold is needed: a file smaller than
# this simply never reaches one.
CHECKPOINT_EVERY_ROWS = 1_000_000

# Checkpoints live in their own directory inside the output, so the whole lot
# can be removed when a run finishes. A leading dot keeps them out of the way.
CHECKPOINT_DIRECTORY_NAME = ".parbake_checkpoints"
