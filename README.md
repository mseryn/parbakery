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


## Reading from a shared filesystem

On a parallel filesystem like Lustre, opening a file costs a round trip to a
metadata server that the whole machine shares. Reading is not the slow part;
waiting is. A run over a thousand files can spend minutes doing nothing visible
before the first row is read.

    python3 document_directory.py /lus/eagle/.../log_syslog --full \
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

    python3 document_directory.py ../copies_of_data --full --skip-existing

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

    python3 document_directory.py ../data --full           # saves progress
    python3 document_directory.py ../data --full           # carries on
    python3 document_directory.py ../data --full --no-checkpoints

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


## Tests

    cd parbake
    pytest

263 tests, a few seconds. They cover the measurements against files whose
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
