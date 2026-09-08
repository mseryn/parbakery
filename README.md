# parbake

Tools for documenting directories of HPC data files. Point them at a directory
of CSVs and get back a plain-text description of what is in each one.

Two purposes:

- **Data quality.** What does each column actually contain? Which columns are
  empty, constant, or full of sentinel values?
- **Checking a file before sharing it.** Which columns might still hold
  usernames or project names that were supposed to be anonymised?

Nothing here interprets the data. It reports measurements and leaves the
conclusions to a person.


## Requirements

Python 3.9 or newer, and pandas.

    pip install pandas

`pytest` as well if you want to run the tests.


## Running it

Document a whole directory:

    cd parbake
    python3 document_directory.py ../copies_of_data

That reads the **first 1,000 rows** of each CSV and writes its reports to
`./parbake_output`. Reading a preview is the default on purpose: these files can
be hundreds of gigabytes, and a full scan should be something you ask for.

    python3 document_directory.py ../copies_of_data --full
    python3 document_directory.py ../copies_of_data --preview 10000
    python3 document_directory.py ../copies_of_data --out /somewhere/else

A single file, printed to the screen:

    python3 describe_csv.py ../copies_of_data/aurora_dim_job_comp_2026-01.csv
    python3 describe_csv.py data.csv --preview
    python3 describe_csv.py data.csv --json summary.json

`--help` on either script lists every option.


## What you get

    parbake_output/
      DIRECTORY_DOCUMENTATION.txt    the index: every file in the directory
      <name>.txt                     one report per CSV

The index lists every file, so nothing is silently left out. Files that are not
CSVs are listed as "not examined" rather than dropped. For each CSV it records
the row and column counts, how much was read, and anything worth a second look:

    ANL-ALCF-DJC-POLARIS_20220809_20221231.csv  (26.5 MB)
        39,432 rows read, 66 columns, 1.56s -- full scan
        check before sharing: JOB_NAME, MACHINE_NAME, QUEUE_NAME
        empty in every row (4): MACHINE_PARTITION, PYTHON_EXECUTABLE_PATH, ...
        one value covers 99%+ of rows (29): COBALT_JOBID, MACHINE_NAME, ...
        described in ANL-ALCF-DJC-POLARIS_20220809_20221231.txt

The per-file report has four parts:

1. **The file** -- size, columns, rows read, and whether it was a preview or a
   full scan.
2. **Columns worth checking before sharing** -- see below.
3. **One row per column** -- blanks, NA-ish text, distinct values, shortest and
   longest value, and the commonest value with its share.
4. **Numbers** -- for columns holding any: how many values parsed as numbers,
   how many did not, and the smallest and largest.

Then the commonest values per column, and a list of columns carrying no
information.


## What is measured

Per column:

| Measurement | Notes |
|---|---|
| rows, blank fields | a blank field is an empty one, nothing else |
| NA-ish text | the literal words `NA`, `N/A`, `NULL`, `nan`, `None` |
| distinct values | a floor, shown as `1,000+`, once the tracking limit is hit |
| shortest / longest value | in characters. Catches truncation and padding |
| commonest values | with counts and shares |
| numbers | how many parsed, how many did not, smallest, largest |
| empty / constant | every row blank, or one value covering 99%+ of rows |

Two things worth knowing about how the file is read.

**Everything is read as text.** Nothing is converted on the way in, so the
literal word `NA` stays the word `NA` instead of quietly becoming a missing
value. Blank fields and NA-ish text are counted separately, so you can see which
you have. This costs roughly half the reading speed, and it is the reason the
numbers are trustworthy.

**A column with one bad value keeps its numbers.** A column holding
`10, 20, 30, NA, -5` reports a range of -5 to 30 *and* "1 value was not a
number", rather than discarding the range because one value spoiled it.


## Checking a file before sharing it

An ALCF username always contains letters. An anonymised ID is a hash reduced to
an integer, so it is all digits. That is the whole test:

    username_id     all digits -- consistent with an anonymised id
    username        contains letters -- NOT consistent with an anonymised id

It needs no list of names, so it works on a file from anywhere. Columns are
looked at if their **name** suggests an identifier, or if their **values look
like file paths** -- a path can carry a username inside it (`/home/chulwoo/run.py`)
even when the column name gives nothing away.

Turn it off with `--no-identifier-check`. It adds about 7% to a run.

**This cannot tell you a file is safe to share.** "All digits" means consistent
with *an* anonymiser. It does not mean the mapping is safe, that a hash cannot
be reversed, or that identity does not leak through some combination of other
columns. It tells you where to look.


## Settings

All of them live at the top of `describe_csv.py`, with a comment on each.
`document_directory.py` imports them, so there is one place to change anything.

| Setting | Default | What it does |
|---|---|---|
| `DEFAULT_PREVIEW_ROWS` | 1000 | rows read by `--preview` |
| `DEFAULT_BATCH_SIZE` | 500,000 | rows held in memory at once |
| `DEFAULT_VALUES_TRACKED` | 1000 | different values counted per column |
| `DEFAULT_VALUES_SHOWN` | 10 | values listed per column in the report |
| `SINGLE_VALUE_THRESHOLD` | 0.99 | share of rows for a column to count as constant |
| `MINIMUM_LISTING_COVERAGE` | 0.10 | below this, a value list is suppressed as uninformative |
| `NULL_LIKE_TEXT` | `na n/a null nan none` | text counted as NA-ish |
| `COLUMN_NAMES_TO_WATCH` | `user project name ...` | column names flagged for a look |
| `PATH_PROBE_VALUES` | 1000 | values sampled when asking "does this column hold paths?" |

`COLUMN_NAMES_TO_WATCH` needs work. It currently contains the bare word `name`,
which flags `MACHINE_NAME` and `QUEUE_NAME` alongside the real ones.


## Tests

    cd parbake
    pytest

86 tests, about a second. They cover the measurements against files whose
contents are known, the identifier check, and the directory pass. Three
behaviours are pinned deliberately because getting them wrong would be quiet
rather than loud:

- the literal word `NA` survives as text and is not counted as a blank
- batch size does not change any measurement, so tuning for speed cannot change
  an answer
- one unreadable file does not stop the rest of the directory


## Limitations

- **CSV only.** Other formats are listed in the index as "not examined".
- **No subdirectories.** Only the directory you name.
- **A preview is not the file.** On the Polaris export, the first 1,000 rows
  show 42 columns that look constant; the full file has 29. Previews are
  reliable for finding dead columns and misleading about variety.
- **Distinct counts stop at 1,000** per column by default and are then shown as
  `1,000+`. Raise `--values-tracked` if you need the real number.
- **Nothing is compared across columns or across files.**
