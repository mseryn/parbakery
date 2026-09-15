# parbake

Tools for documenting directories of HPC data files. Point them at a directory
of CSVs and get back a plain-text description of what is in each one.

Three outputs per CSV: a plain-text description, a par-baked Croissant file,
and the Markdown rendered from it.

Two purposes:

- **Data quality.** What does each column actually contain? Which columns are
  empty, constant, or full of sentinel values?
- **Checking a file before sharing it.** Which columns might still hold
  usernames or project names that were supposed to be anonymised?

Nothing here interprets the data. It reports measurements and leaves the
conclusions to a person.


## Requirements

Python 3.9 or newer, pandas, and psutil.

    pip install pandas psutil

psutil is used for the resource line at the top of a run. It replaced about
seventy lines of hand-written /proc parsing.

`pytest` as well if you want to run the tests.


## Running it

Document a whole directory:

    cd parbake
    python3 parbakery.py ../copies_of_data

That reads the **first 1,000 rows** of each CSV and writes its reports to
`./parbake_output`. Reading a preview is the default on purpose: these files can
be hundreds of gigabytes, and a full scan should be something you ask for.

    python3 parbakery.py ../copies_of_data --full
    python3 parbakery.py ../copies_of_data --preview 10000
    python3 parbakery.py ../copies_of_data --out /somewhere/else

A single file, printed to the screen:

    python3 describe_csv.py ../copies_of_data/aurora_dim_job_comp_2026-01.csv
    python3 describe_csv.py data.csv --preview
    python3 describe_csv.py data.csv --json summary.json

`--help` on either script lists every option.


## Reading from a shared filesystem

On a parallel filesystem like Lustre, opening a file costs a round trip to a
metadata server that the whole machine shares. Reading is not the slow part;
waiting is. A run over a thousand files can spend minutes doing nothing visible
before the first row is read.

    python3 parbakery.py /lus/eagle/.../log_syslog --full \
        --workers 32 --batch-local-copies

`--batch-local-copies` has each worker copy its file to local disk, read it
there, and delete the copy before taking the next one. One large sequential
transfer instead of many small waits. Because a worker holds exactly one copy
at a time, there are never more copies on local disk -- or more readers on the
shared filesystem -- than there are workers. `--local-copy-dir` says where the
copies go; the default is `$TMPDIR`, which on a compute node is usually
node-local storage.

It is off by default, and on a local filesystem it is pure loss: it reads every
byte of every file, so it only pays when the file was going to be read through
anyway.


## Carrying on where a run left off

    python3 parbakery.py ../copies_of_data --full --skip-existing

`--skip-existing` leaves alone any file whose output is already in `--out`, and
takes its index entry from the Croissant the previous run wrote. For picking up
after a run was interrupted, or for adding new files to a directory that was
documented last week.

A file counts as done only when *every* output this run would write is there --
a run killed mid-file can leave a report with no Croissant beside it, and
treating that as finished would bake in the half-written state.

It looks only at whether the output exists, not at whether the file has changed
since. That is unlike checkpointing, which fingerprints size and modification
time. A file edited since it was documented keeps its old description until you
run it again without the flag.

This is the coarse version of carrying on: whole files that are already done.
For a single enormous file that was killed halfway through, see
[Being killed, and carrying on](#being-killed-and-carrying-on) -- the two work
together, and a run can use both.


## What you get

    parbake_output/
      DIRECTORY_DOCUMENTATION.txt      the index: every file in the directory
      parbaked_txt/
        <name>.txt                     one readable report per CSV
      parbaked_croissants/
        <name>.parbaked.json           the start of a Croissant file
      parbaked_markdown/
        <name>.parbaked.md             the same, rendered for people

One folder per kind, so a directory of fifty datasets does not become a heap of
a hundred and fifty files. The index stays at the top level, because it is the
index to all three.

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


## Par-baked Croissant files

"Par-baked" means machine-generated and not reviewed by anybody. These files are
the *start* of a Croissant file, not one. Everything in them is either a
measurement or a placeholder: no field has a description, a data type, a unit or
a meaning, because those are judgements nobody has made yet.

**They fail Croissant validation on purpose.** A par-baked file that passes
validation is dangerous, because passing validation is what people check before
treating a file as done. `conformsTo` names a version that does not exist, which
`mlcroissant validate` reports as an error.

Fixing one is a ladder rather than a maze -- every error is real work, and the
file validates exactly when that work is finished. Measured against
mlcroissant 1.1.0:

| state | errors |
|---|---|
| as generated | 2 -- the bad `conformsTo`, and the `@type` it implies |
| fix `conformsTo` | 17 -- a missing checksum, and 16 fields with no `dataType` |
| add a checksum | 16 -- the fields with no `dataType` |
| add the `dataType`s | 0 -- it validates |

The file says it is unreviewed in the dataset description, the RecordSet, the
FileObject, a `_parbake` block, and every single field description. The
`_parbake` block also carries the list of what a person still has to do, so the
list travels with the file.

Nothing is ever written with the name `.croissant.json`. That name is for a file
someone has finished.

Skip the whole step with `--no-croissant`.

The Markdown is rendered by `croissant_to_md.py`, the same renderer used for
finished documents, so a par-baked file reads as an unfinished version of the
real thing.


## Being killed, and carrying on

Long reads get killed -- by the out-of-memory killer, by a scheduler, by a
closed laptop. Progress is saved every `--checkpoint-every` rows (default
1,000,000), and the next run carries on rather than starting again.

This is the fine-grained version: one file, picked up mid-read. To skip files
that finished entirely, see
[Carrying on where a run left off](#carrying-on-where-a-run-left-off).

    python3 parbakery.py ../data --full           # saves progress
    python3 parbakery.py ../data --full           # carries on
    python3 parbakery.py ../data --full --no-checkpoints

Checkpoints live in `<out>/.parbake_checkpoints/` and the whole directory is
removed once every file has been described. If anything failed, they are kept so
the next run can still use them.

A resumed read gives **exactly** what an uninterrupted one would, including the
columns that hit the value-tracking cap. That is because a checkpoint holds the
whole accumulator and carries on adding to it, rather than merging two separate
ones -- which is also why the same trick cannot be used to split one file across
several workers.

A checkpoint is ignored, and the file read from the start, if the data has
changed (size or modification time), if the settings that affect measurements
have changed, or if it was written by a different version. Batch size is
deliberately not one of those: it cannot change a measurement, and lowering it
is the usual response to being killed.

### If you are being killed for memory

Checkpointing is recovery, not prevention. Memory is dominated by the batch, so
the levers are:

| | |
|---|---|
| `--batch-size N` | rows held at once. By default worked out from how wide the file is |
| `--workers N` | each worker holds its own batch |

Measured on a 66-column file, one worker: the batch size is chosen to hold about
a million cells, which came to **175 MB**. A fixed 100,000 rows would have been
**426 MB**, and eight workers of those is 3.4 GB.


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

All of them live in `settings.py`, grouped by what they affect, with a comment
on each. Every other module imports them from there, so there is one place to
change anything.

| Setting | Default | What it does |
|---|---|---|
| `DEFAULT_PREVIEW_ROWS` | 1000 | rows read by `--preview` |
| `CELL_BUDGET_PER_BATCH` | 1,000,000 | cells held at once; the row count follows from the file's width |
| `CHECKPOINT_EVERY_ROWS` | 1,000,000 | rows between saves |
| `DEFAULT_VALUES_TRACKED` | 1000 | different values counted per column |
| `DEFAULT_VALUES_SHOWN` | 10 | values listed per column in the report |
| `SINGLE_VALUE_THRESHOLD` | 0.99 | share of rows for a column to count as constant |
| `MINIMUM_LISTING_COVERAGE` | 0.10 | below this, a value list is suppressed as uninformative |
| `NULL_LIKE_TEXT` | `na n/a null nan none` | text counted as NA-ish |
| `COLUMN_NAMES_TO_WATCH` | `user project name ...` | column names flagged for a look |
| `PATH_PROBE_VALUES` | 1000 | values sampled when asking "does this column hold paths?" |

`COLUMN_NAMES_TO_WATCH` needs work. It currently contains the bare word `name`,
which flags `MACHINE_NAME` and `QUEUE_NAME` alongside the real ones.


## Files and artifacts

Paths are relative to this repository, except Input, Output and Working state
rows, which are where a run finds or writes them. Angle brackets mark a name
that varies. bakery's files are listed in bakery's README.

Program: run from the command line. Module: imported by other files. Test: run by pytest. Test fixture: data a test reads. Documentation and Configuration: in the repository. Input: read from elsewhere. Output: written by a run. Working state: written during a run and not committed.

| Path | Category | Purpose |
|---|---|---|
| `parbakery.py` | Program | Command line. Finds the CSVs in a directory, runs one worker process per file, and writes the index. |
| `describe_csv.py` | Program | Measures one CSV in batches and saves and resumes its checkpoints. parbakery.py calls it; it also runs on its own. |
| `croissant_to_md.py` | Program | Renders any Croissant JSON file as Markdown. Also imported by bakery. |
| `parsing_tools/pod_logs.py` | Program | Converts Kubernetes pod logs, one JSON record per line, to CSV and JSON Lines for parbakery.py to measure. |
| `measuring.py` | Module | ColumnSummary: the running totals kept for one column. |
| `identifiers.py` | Module | IdentifierCheck: flags columns whose values may not be anonymised. |
| `sources.py` | Module | CSV and compression suffixes, row estimates for progress, and batch size. |
| `checkpoints.py` | Module | CheckpointStore: saves, validates, loads and deletes checkpoints. |
| `already_done.py` | Module | --skip-existing: finds files whose outputs exist and rebuilds their index entries. |
| `staging.py` | Module | --batch-local-copies: a copy on local disk per worker. |
| `parbaked_croissant.py` | Module | Builds the par-baked Croissant file and its Markdown. |
| `reporting.py` | Module | Writes the per-file report and the directory index. |
| `progress.py` | Module | ProgressDisplay: one line per file, redrawn in a terminal. |
| `resources.py` | Module | Memory and CPU use of the run's processes, through psutil. |
| `formatting.py` | Module | Human-readable numbers, byte sizes and durations. |
| `settings.py` | Module | Every constant, and the Python version check. |
| `parsing_tools/__init__.py` | Module | Makes parsing_tools importable by its tests. Empty. |
| `tests/test_parbakery.py` | Test | Directory runs, parallel workers and output layout. |
| `tests/test_describe_csv.py` | Test | Measuring one CSV. |
| `tests/test_identifiers.py` | Test | The anonymisation check. |
| `tests/test_checkpoints.py` | Test | Checkpoints, and resuming a killed read. |
| `tests/test_already_done.py` | Test | --skip-existing. |
| `tests/test_staging.py` | Test | Local copies. |
| `tests/test_parbaked_croissant.py` | Test | Par-baked Croissant files, including that they fail validation for the intended reason. |
| `tests/test_progress.py` | Test | The progress display. |
| `tests/test_skill_markers.py` | Test | The markers the skill tells an AI to look for appear in generated files. |
| `parsing_tools/tests/test_pod_logs.py` | Test | pod_logs.py. |
| `parsing_tools/tests/fixtures/sambastack_sample.txt` | Test fixture | Three pod-log records from a real export, with the email address and API key hash replaced. |
| `README.md` | Documentation | How to run parbake, its options, and what it writes. |
| `NOTES.md` | Documentation | Design reasoning, and the measurements behind decisions. |
| `parsing_tools/README.md` | Documentation | How to run pod_logs.py, its output columns, and its limits. |
| `skills/reading-croissant-datasets/SKILL.md` | Documentation | Instructions for an AI model reading a Croissant file together with its data. |
| `docs/pipeline_map.html` | Documentation | Pipeline map page: run diagram, guarantees, import grid, and text for Claude chat. |
| `docs/pipeline_run.svg` | Documentation | The run diagram as a standalone SVG, for editing. |
| `docs/outside_the_pipeline.svg` | Documentation | How pod_logs.py and bakery connect to parbake, as a standalone SVG. |
| `docs/for_claude_chat.md` | Documentation | Plain-text description and Mermaid diagram of parbake. |
| `pyproject.toml` | Configuration | Project metadata and pytest settings, including both test directories. |
| `.gitignore` | Configuration | Excludes caches and parbake_output/. |
| `<out>/DIRECTORY_DOCUMENTATION.txt` | Output | Index: every file in the directory, and a summary line for each CSV. <out> defaults to parbake_output/. |
| `<out>/parbaked_txt/<name>.txt` | Output | Measurement report for one CSV. |
| `<out>/parbaked_croissants/<name>.parbaked.json` | Output | Par-baked Croissant: file identity, columns, measurements. Fails validation by design. bakery's input. |
| `<out>/parbaked_markdown/<name>.parbaked.md` | Output | Markdown rendering of the par-baked Croissant. |
| `<stem>.parsed.csv` | Output | pod_logs.py: one row per log record, one column per field. Written beside the source or into --out. |
| `<stem>.parsed.jsonl` | Output | pod_logs.py: the same rows as JSON Lines. |
| `<out>/.parbake_checkpoints/<name>.checkpoint.json` | Working state | Progress through one file. Deleted when the file finishes. |
| `$TMPDIR/parbake_staging_*/` | Working state | Copies made by --batch-local-copies. Each is deleted after it is read. |


## Tests

    cd parbake
    pytest

293 tests, a few seconds: 263 in `tests/`, 30 for `parsing_tools/` in `parsing_tools/tests/`. They cover the measurements against files whose
contents are known, the identifier check, and the directory pass. Three
behaviours are pinned deliberately because getting them wrong would be quiet
rather than loud:

- the literal word `NA` survives as text and is not counted as a blank
- batch size does not change any measurement, so tuning for speed cannot change
  an answer
- one unreadable file does not stop the rest of the directory
- reading files in parallel gives byte-identical results to reading them one at
  a time
- the par-baked Croissant files fail validation, fail for the *intended* reason,
  and do validate once the outstanding work is genuinely done
- an interrupted read resumes to a byte-identical result, including the columns
  that hit the value cap
- clearing checkpoints never deletes a file it did not write


## Limitations

- **CSV only**, plain or compressed (`.csv`, `.csv.gz`, `.csv.bz2`, `.csv.xz`). `.zip` is not read: an archive can hold several files, and choosing one is a judgement. Everything else is listed in the index as "not examined".
- **The Croissant files need a person.** They are a starting point and are
  useless until someone fills in the types, meanings, licence and caveats.
- **No subdirectories.** Only the directory you name.
- **A preview is not the file.** On the Polaris export, the first 1,000 rows
  show 42 columns that look constant; the full file has 29. Previews are
  reliable for finding dead columns and misleading about variety.
- **Distinct counts stop at 1,000** per column by default and are then shown as
  `1,000+`. Raise `--values-tracked` if you need the real number.
- **Nothing is compared across columns or across files.**
