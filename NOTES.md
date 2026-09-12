# parbake — what it is and how it works

Design notes. `README.md` is the practical guide: how to run it, what the flags
do. This file is the reasoning — why it works the way it does, what was measured
to decide that, and what was deliberately left out.

Written 2026-09-10, against the code as it stands.

---

## 1. What the system is for

Two problems, one pass over the data.

**Documenting a directory of HPC data files.** You have a directory of CSVs from
an ALCF export. What is in them? Which columns are empty, constant, or full of
sentinel values? What does a column actually hold, as opposed to what its name
suggests? The tool measures and reports; it never interprets.

**Getting a Croissant file started.** A Croissant file describing an ALCF dataset
takes real human effort — field meanings, units, caveats, licence, release
status. Most of that cannot be automated and should not be. But some of it is
mechanical: the file's identity, the column names in order, and the measurements
that tell a reviewer where to look. The tool produces that mechanical part as a
starting point, and makes it structurally impossible to mistake for a finished
one.

### Why "par-baked"

Par-baking is bread taken out of the oven before it is done, so somebody else can
finish it later. A par-baked Croissant is machine-generated and unreviewed. It is
the *start* of a Croissant file, not one.

The word carries the important property: par-baked bread is not edible. Neither
is a par-baked Croissant usable as documentation. The tool goes to some length to
make that inescapable — see §7.

---

## 2. The shape of the thing

Two entry points, sharing one measurement engine.

```
document_directory.py          scan a directory, describe every CSV in it
  └─ describe_csv.py           measure one CSV        ← also runs standalone
       ├─ IdentifierCheck      un-anonymised data check
       └─ ColumnSummary        per-column accumulators
  ├─ parbaked_croissant.py     build the par-baked Croissant
  │    └─ croissant_to_md.py   render its Markdown (your existing renderer)
  └─ progress.py               terminal display for a parallel run
```

`describe_csv.py` is the core and knows nothing about directories, workers or
Croissant. `document_directory.py` orchestrates. Everything else hangs off those
two.

### What a run produces

```
parbake_output/
  DIRECTORY_DOCUMENTATION.txt          the index: every file in the directory
  <name>.txt                           one human-readable report per CSV
  parbaked_croissants/
    <name>.parbaked.json               the start of a Croissant file
    <name>.parbaked.md                 the same, rendered for people
```

Non-CSV files are listed in the index as "not examined" rather than dropped. A
file silently missing from the index is the failure this whole thing exists to
prevent, so it is never allowed to happen quietly.

Nothing is ever written with the name `.croissant.json`. That name is reserved
for a file a person has finished, and reserving it is a cheap, permanent signal.

---

## 3. How the data is read

Three decisions in `LITERAL_READ_SETTINGS` account for most of the tool's
correctness. All three were arrived at by being wrong first.

```python
{"dtype": str, "keep_default_na": False, "na_filter": False, "skip_blank_lines": False}
```

**Everything is read as text.** pandas is told not to convert anything on the way
in. Without `na_filter=False`, the literal strings `NA`, `NULL`, `N/A`, `nan` and
`None` become missing values, and the distinction between "this field was empty"
and "this field contained the letters N-A" disappears. That distinction matters
in job-accounting data, where a column can legitimately hold the text `NA`.

The cost is real: roughly **22 MB/s** rather than the ~46 MB/s a native pandas
parse manages. That is the price of the numbers being trustworthy, and it was
paid deliberately.

Numbers are still measured. Values are coerced into a *temporary* for
measurement and the result is never persisted — the column stays text.

**`skip_blank_lines=False`.** pandas drops empty lines by default. In a
one-column file an empty *value* is an empty *line*, so `"v\n1\n\n3\n"` read as
2 rows with 0 blanks instead of 3 rows with 1. That breaks the row count and
loses a blank outright.

**`index_col=False`** (in the reader, not the settings dict). Without it, a file
whose *first data row* has more fields than the header makes pandas promote
column 0 to the index for the entire file. Every value then shifts one position
left and every column's measurements are attributed to the wrong field name —
silently, with no warning. This was a real bug in shipped code, found by testing
rather than by reading.

### Batching

The batch size is not a fixed number of rows. Memory is rows *times* columns, so
a fixed row count makes a wide file cost far more than a narrow one -- and a
worker that is fine on one file gets killed by the next. `rows_per_batch()`
holds the number of *cells* roughly fixed instead:

    rows = clamp(CELL_BUDGET_PER_BATCH // column_count,
                 MINIMUM_BATCH_ROWS, MAXIMUM_BATCH_ROWS)     # 1,000,000 // n, 2k..100k

A 66-column file went from 426 MB to 175 MB per worker for about 10-15% more
time. The ceiling of 100,000 rows is the old fixed default, kept so that narrow
files are never worse off than they were: without it, a 3-column file ballooned
to 333,333-row batches and measured 247 MB / 3.5s against the old 201 MB / 3.2s
-- worse on both counts. With the ceiling the change is strictly one-sided.

Two reasons the budget is not larger:

- **Memory.** Each worker holds a batch, and eight workers hold eight of them. A
  batch of strings is not small.
- **Progress.** The row count only moves when a batch finishes, so large batches
  make a progress bar sit still.

Measured on a 236,592-row file: going from 500,000 to 100,000 rows cost **5%** of
reading speed and gave **three times** the progress updates.

There is a test asserting that batch size does not change any measurement.
That test exists because pandas' own bad-line handling *is* chunk-dependent —
on one file with two over-wide rows, `chunksize=1` skipped neither, `chunksize=2`
skipped one, and `chunksize>=3` skipped both. A speed setting that changes the
answers would make the whole tool worthless, so the current reader avoids that
code path entirely.

---

## 4. What is measured, and why

Per column, accumulated one batch at a time in `ColumnSummary`. Nothing grows
with file size except the value table, which has a hard ceiling.

| Measurement | Why it earns its place |
|---|---|
| rows, empty fields | the denominators everything else is read against |
| NA-ish text count | counted separately from blanks, so you can see which you have |
| distinct values | shown as `1,000+` once the tracking ceiling is hit — a floor, never presented as a total |
| commonest values, with counts and shares | the single most useful data-quality signal, see below |
| shortest / longest value length | catches truncation and padding |
| numbers parsed / not parsed | how numeric a column really is |
| smallest, largest | range, over the values that are actually numbers |
| empty / holds-one-value | columns carrying no information |

### Counts, not a set

The most consequential design choice in the measurement layer. An earlier tool
(`summarize_csv.py`) kept unique values in a `set()`, so it knew *which* values
appeared but not *how often*. That is the difference between

> `GPUS_REQUESTED` — 1 unique value: `-1`

and

> `GPUS_REQUESTED` — `-1` in all 39,432 rows

The second tells you the column is dead. The first makes you go and check. Same
storage cost — a dict instead of a set.

### A bad value does not destroy the numbers

A column holding `10, 20, 30, NA, -5` reports a range of −5 to 30 **and** "1
value was not a number". The older approach demoted the whole column to
categorical and threw the range away, which on a multi-hour run means losing
numbers at hour 2.9 because of one row.

### Columns carrying no information

Two categories, reported separately:

- **empty in every row** — `empty_count == rows_seen`
- **one value covers ≥ 99% of rows** — `SINGLE_VALUE_THRESHOLD`

The share is measured against *all* rows, blanks included, so a column that is
half blank and half one value scores 0.5 and does not qualify. It is not holding
one value; it is mostly empty.

Near-constant columns report a **row count**, not a percentage: `COBALT_JOBID
'0' 100.0% (1 of 39,432 rows differ)`. An early version printed `(0.00% is
something else)`, which reads as nonsense.

On the Polaris export this finds 4 empty and 29 near-constant columns out of 66 —
half the file carries nothing. The 4 empty ones match the croissant's documented
list exactly. Of the 29, exactly 25 are *perfectly* constant, matching the
croissant's "twenty-five columns are constant"; the other 4 have real outliers
hiding in them, which is arguably the more interesting set.

### Suppressing value lists that say nothing

A list of values is only worth printing when the values repeat. If the values
shown together cover less than `MINIMUM_LISTING_COVERAGE` (10%) of rows, one
summary line is printed instead of twenty.

The threshold came from the data, not a guess. Top-20 coverage across both real
files falls into two clear groups: identifier and timestamp columns land **below
1%**, everything with a real shape lands **above 14%**. Almost nothing sits in
between, so the threshold is not sensitive.

---

## 5. The un-anonymised data check

A separate concern that rides along on the same pass, because a second read of
hundreds of gigabytes is not free.

### The signal

An ALCF username always contains letters. An anonymised ID is a hash reduced to
an integer, so it is all digits. That is the entire test: `.isdigit()` on the
values of columns worth looking at.

```
username_id        all digits -- consistent with an anonymised id       '76'
USERNAME_GENID     all digits -- consistent with an anonymised id       '30861277613258'
username           contains letters -- NOT consistent with an anonymised id   'chulwoo'
```

It needs no list of names, which is why it survives contact with reality: there
are millions of ALCF usernames and no tool is going to hold them all.

### How this got here

An earlier version matched against a crosswalk of known plaintext values. It
worked — on the raw Aurora file it found 371 usernames and 162 project names —
but it does not scale. Measured: exact matching stays flat (0.46s per batch at a
million known values), but *substring* matching grows with the needle count and
reaches roughly **19 hours per batch** at a million names. It was also the wrong
dependency: the crosswalk came from a local anonymisation script, not the real
one. All of it was removed.

### Which columns get looked at

- the column's **name** suggests an identifier (`COLUMN_NAMES_TO_WATCH`)
- or the **values look like paths** — a path carries a username inside it
  (`/home/chulwoo/run.py`) even when the column name gives nothing away

Path detection samples the first `PATH_PROBE_VALUES` (1,000) values rather than
scanning. That probe is asked of every column of every batch, and scanning
entire columns to answer it cost **more than every other part of the check put
together** — 0.30s against 0.015s on a 66-column file. Overhead dropped from 16%
to 6.6%. A column of paths shows paths immediately; one that does not is not a
column of paths.

### What it cannot do

It cannot say a file is safe to share. "All digits" means consistent with *an*
anonymiser. It says nothing about whether the mapping is safe, whether a hash is
reversible, or whether identity leaks through some combination of other columns.
It says where to look. `--no-identifier-check` turns it off entirely.

**Known rough edge:** `COLUMN_NAMES_TO_WATCH` contains the bare word `name`, so
it flags `MACHINE_NAME`, `QUEUE_NAME` and `JOB_NAME` on the Polaris file. Three
false alarms out of five flagged columns. The list is a tuple at the top of
`describe_csv.py` and is meant to be edited.

---

## 6. Preview versus full scan

`--preview` (default, 1,000 rows) and `--full`. Both measure exactly the same
things through the same code path, so their outputs compare directly. Preview is
the default because pointing a tool at a directory and pressing enter should
never start a six-hour read.

**A preview is not the file**, and the difference is not subtle:

| column | first 1,000 rows | full file |
|---|---|---|
| `GPUS_REQUESTED` | 1 distinct | 1 distinct |
| `EXIT_CODE` | 11 | 42 |
| `QUEUE_NAME` | 3 | 200 |
| `USERNAME_GENID` | 25 | 321 |

And on constant-column detection, the preview reports **42** near-constant
columns where the full scan finds **29** — thirteen columns look dead in the
first thousand rows and are not.

So: previews are reliable for finding genuinely dead columns and badly
misleading about variety. The index records which mode was used, on every entry.

---

## 7. The par-baked Croissant

The part that most needed getting right, because the failure mode is a file that
looks finished.

### It fails validation on purpose

A par-baked file that passes `mlcroissant validate` is dangerous, because passing
validation is the thing people check before treating a file as done. Structure is
not meaning: a file can be perfectly well formed and say nothing true.

`conformsTo` names a version that does not exist:

```
http://mlcommons.org/croissant/PARBAKED-DO-NOT-SUBMIT
```

mlcroissant reports that as an **error**, not a warning. That was verified
empirically against mlcroissant 1.1.0 before anything was built on it — the whole
mechanism would have been useless if it only warned.

### The failure ladder

The file is built so that fixing it is a ladder rather than a maze. Every error a
person sees is real work, and it validates exactly when that work is done:

| state | errors |
|---|---|
| as generated | **2** — the bad `conformsTo`, and the `@type` it implies |
| fix `conformsTo` | **17** — a missing checksum, and 16 fields with no `dataType` |
| add a checksum | **16** — the fields with no `dataType` |
| add the `dataType`s | **0** — it validates |

That ladder is why the FileObject uses `cr:FileObject` rather than
`sc:FileObject`. Under the par-baked `conformsTo`, mlcroissant falls back to
schema.org type expectations, so `cr:` raises one extra error there. But `sc:`
leaves the file structurally wrong under a *real* `conformsTo`, and fixing the
version then produces **35 errors** about "malformed source data" that have
nothing to do with what is actually missing. One clear extra error now beats a
maze later.

### It keeps saying it is not finished

The banner appears in the dataset description, the RecordSet description, the
FileObject description, a `_parbake` block, and **every single field
description**. One notice is missable. This should not be.

`_parbake` also carries `outstanding_for_a_human` — the list of what is still
missing — so the list travels with the file instead of living in someone's head.

### What it refuses to contain

- no `dataType` on any field — guessing a type from a sample is exactly the
  judgement this tool must not make, and its absence is one of the things that
  makes the file fail
- no real field descriptions, only a placeholder that cannot be mistaken for one
- no licence, citation, creator, publisher, or publication date
- **no `rai:` fields** — writing "no known biases" would be a claim nobody has
  checked

Measurements live in `_parbake_measurements`, deliberately outside the Croissant
fields. A measurement is not a property of the schema, and mapping one into a
semantic slot would be the same guess in a different costume.

### The Markdown

Rendered by `croissant_to_md.py` — your existing renderer, the same one that
produces reviewed documents. A par-baked file should read as an unfinished
version of the real thing, not as output from a different tool. The empty Type
column is conspicuous, which is the point.

---

## 8. Parallelism

One process per file. Reading a CSV is mostly pandas doing CPU work, so threads
would queue behind each other; separate processes actually run at the same time.

Measured on 956 MB across 6 files, full scan:

| workers | wall | speedup |
|---|---|---|
| 1 | 42.4s | 1.0× |
| 2 | 22.4s | 1.9× |
| 4 | 15.9s | 2.7× |
| 6 | 10.5s | 4.0× |

`--workers N`, defaulting to `min(8, cpu_count)` and capped at the file count —
an idle worker still costs a whole Python and its pandas.

**Results are identical to sequential.** Every per-file report from the 6-worker
run was byte-identical to the sequential one except the elapsed-time line. There
are tests asserting parallel and sequential agree on every measurement, and that
results come back in listed order regardless of which worker finished first.

**Start method.** `forkserver`, not the Linux default `fork`. The progress queue
needs a `Manager`, which runs a thread, and forking a multi-threaded process can
deadlock the child — Python 3.12 warns about it and 3.14 changes the default.
`forkserver` forks from a clean single-threaded helper: safe, and quicker to
start than `spawn`. Costs about 0.45s of startup for four workers.

Progress travels back over a managed queue rather than being printed in the
worker, because several workers writing to one terminal interleave into nonsense.

---

### Reading from a shared filesystem

Parallelism helps when the bottleneck is CPU. On a parallel filesystem it often
is not. Opening a file on Lustre costs a round trip to a metadata server shared
by the whole machine, and a run over a thousand files pays that a thousand times
before reading anything worth having.

This showed up as a hang. A run on 1,226 files across 250 GB printed its header
and then sat there. The cause was a loop in the parent that estimated every
file's row count up front, purely to give the progress bars a denominator --
1,226 sequential opens, each a metadata round trip, before a single worker
started. Locally that loop takes 0.3 seconds; on Lustre it is minutes of blank
screen.

Two changes came out of it.

**Estimating moved off the startup path.** With `--batch-local-copies` each
worker estimates its own file, from its local copy, and sends the number back on
the progress queue. Bars start as `?` and fill in. The parent does no per-file
I/O at all, so the display appears immediately.

**`--batch-local-copies` copies before reading.** A worker copies its file to
local disk, reads it there, deletes the copy, and only then takes the next one.
One large sequential transfer instead of many small waits, which is what such a
filesystem is actually good at.

The first version of this batched: copy `workers` files, process them all,
delete them, repeat. That was wrong. It made every worker wait for the slowest
member of its batch before any of them could start the next -- a barrier that
buys nothing. Copying per worker gives the same guarantee for free: a worker
holds exactly one copy at a time, so there are never more copies on local disk,
or more readers on the shared filesystem, than there are workers. No barrier,
no bookkeeping. There is a test that reads completion order off the display and
fails if a barrier ever comes back.

It is off by default. It reads every byte of every file, so it only pays when
the file was going to be read through anyway; on a local filesystem it is loss.

One thing it deliberately does not do is prefetch the next file while reading
the current one. That would hide the copy time, but it holds two files locally
and puts twice the readers on the shared filesystem, which is the cost the flag
exists to bound.

## 9. Skipping work already done

`--skip-existing` leaves alone any file whose output is already present. For
carrying on after an interrupted run, or adding files to a directory documented
last week.

A file counts as done only when *every* output this run would write exists --
report, and Croissant and Markdown unless `--no-croissant` was given. All of
them, not any: a run killed mid-file leaves a report with no Croissant beside
it, and treating that as finished would bake in the half-written state.

The decision is made in the parent, before any job is submitted, so a skipped
file is never opened, never copied, and never costs a round trip.

The awkward part is the index. It is rewritten from scratch every run, so
skipped files would leave holes in it -- on a resumed 1,226-file run, most of
the index. The par-baked Croissant already holds everything the index reports,
in `_parbake` and `_parbake_measurements`, so entries are read back from there
rather than reduced to the word "skipped". A fresh index and a skipped one are
identical apart from the marker line.

That exposed a gap. The anonymity flags -- the most consequential thing measured
-- lived *only* in the prose report, not in the machine-readable Croissant. They
are now recorded in `_parbake` as `columns_of_concern`. Croissants written
before that change do not have the key, and those report **NOT KNOWN**, never an
empty list: an absent line would read as "nothing to check" about a file nobody
checked, and this tool flags rather than certifies.

It is worth being clear about what this is not. Checkpointing fingerprints a
file by size and modification time and notices when it changes. This does not.
It looks only at whether the output exists, so a file edited since it was
documented keeps its old description until read again. That is what the flag is
for, but it is a sharp edge.

---

## 10. The progress display

`progress.py`, about 290 lines. One line per file, redrawn in place with ANSI
cursor movement. It leans on `resources.py`, which uses psutil:

```
  [1/6] dataset_1.csv  [################----]   78%       200,000 rows      7s
  [2/6] dataset_2.csv  [####################]   done      236,592 rows      7s  66 cols, 33 constant, check 3
  [3/6] dataset_3.csv                         waiting
```

Piped to a file it prints one plain line per completed file instead — cursor
codes in a log are worse than no progress at all.

**Row estimates** give the bars a denominator. Files at or below
`EXACT_ROW_COUNT_LIMIT` (8 MB) are counted exactly, because reading 8 MB to count
newlines takes hundredths of a second and sampling a small file gives a worse
answer than reading it. Larger files are sampled at eight points across the file.

Sampling only the *start* was 24% low on the Polaris export, because its
node-list column reaches 2,048 characters and its early rows are longer than
average. Eight points brings that to about 7%, which is fine for a bar.

**The display keeps its own clock.** Elapsed time is timed in the parent, not
taken from worker messages — those only arrive when a batch finishes, so a
display driven by them alone sits frozen on a slow file and looks hung.

**It never writes more lines than the terminal is tall.** Redrawing works by
moving the cursor up over the previous frame, and the cursor cannot travel above
the top of the screen. A frame taller than the terminal therefore lands in the
wrong place, clears from there, and scrolls the whole display away — every
refresh. On 1,226 files the frame was 1,226 lines and 94 KB, five times a
second, and the screen flickered continuously.

Above that height the display shows a summary line accounting for every file,
then as many file rows as fit. 94 KB per frame became 2 KB.

Which rows to show is decided by what is *moving*, not by position in the list.
An earlier version windowed from the first unfinished file, which looks
reasonable until one slow file near the top pins the view: the screen sits
still while hundreds of others come and go below it. Active files are picked out
wherever they sit, and any room left over goes to the most recent completions,
so progress is visible. Every row carries its own `[n/total]`, so a gap between
them reads fine.

```
  1,226 files: 800 done, 251 reading, 175 waiting
  [1/1226] syslog-2024-0000.csv.gz    [????????????????????]  ?  9,000,000 rows
  [802/1226] syslog-2024-0801.csv.gz  [????????????????????]  ?        500 rows
  ...
  ... and 229 more being read
```

**The two clocks are separate on purpose.** The display redraws every 0.2s;
the resource line refreshes every 5s. Walking every worker's CPU and memory is
far more expensive than re-rendering text, and on a run with 250 workers doing
it on the redraw clock spends most of the time measuring rather than working.
They were briefly the same by accident — see §13.

---

## 11. Settings

All in `settings.py`, grouped by what they affect, each with a comment. Every
other module imports them from there, so there is one place to change anything.
They started at the top of `describe_csv.py` and moved out when the second and
third readers of them appeared.

| Setting | Value | Meaning |
|---|---|---|
| `DEFAULT_PREVIEW_ROWS` | 1,000 | rows read by `--preview` |
| `CELL_BUDGET_PER_BATCH` | 1,000,000 | cells held at once; the row count follows from the file's width |
| `DEFAULT_VALUES_TRACKED` | 1,000 | different values counted per column |
| `DEFAULT_VALUES_SHOWN` | 10 | values listed per column in the report |
| `SINGLE_VALUE_THRESHOLD` | 0.99 | share of rows for a column to count as constant |
| `MINIMUM_LISTING_COVERAGE` | 0.10 | below this, a value list is suppressed |
| `PATH_PROBE_VALUES` | 1,000 | values sampled when asking "does this hold paths?" |
| `EXACT_ROW_COUNT_LIMIT` | 8 MB | below this, count rows exactly |
| `NULL_LIKE_TEXT` | `na n/a null nan none` | text counted as NA-ish |
| `COLUMN_NAMES_TO_WATCH` | `user project name …` | column names flagged for a look |

---

## 12. Principles the code holds to

These are not style preferences. Breaking one of them is a defect.

**Measurements, not conclusions.** The tool reports what it counted. It does not
say a column is an enumeration, a timestamp, unused, or a sentinel. Those are
interpretations and they belong to a person.

**Every measurement carries its denominator and its scope.** A claim over 1,000
rows must never be phrased like a claim over 41 million. Every report says
whether it was a preview or a full scan.

**A floor is never presented as a total.** A capped distinct count reads
`1,000+`, and the JSON carries `distinct_count_is_at_least`.

**Flag, do not conclude.** The identifier check says where to look. It never
says a file is safe.

**Silent omission is a bug.** Every file in a scanned directory appears in the
index. Every unreadable file is recorded with its reason.

**One bad file does not stop the run.** A malformed CSV fails that file only; the
reason is written into its report and into the index.

**A speed setting must never change an answer.** Tested explicitly, for batch
size and for parallelism.

---

## 13. Bugs found, and what they taught

Worth recording, because all of them were **silent** — none raised an error, all
produced plausible-looking numbers, and all were found by measuring rather than
reading.

**Column shift on an over-wide first row.** Missing `index_col=False`. pandas
promoted column 0 to the index for the whole file and every measurement was
attributed to the wrong column name. *Lesson: pandas' conveniences are not
neutral. Turn them off explicitly.*

**Chunk-dependent measurements.** pandas' bad-line handling varies with where the
chunk boundary falls, so `--chunk-rows` — a documented performance knob — changed
the row count and the measurements. *Lesson: test that tuning cannot change an
answer.*

**Blank lines silently dropped.** `skip_blank_lines` defaults to `True`, so row
counts under-reported and, in a one-column file, blanks vanished entirely.
*Lesson: found while investigating something else, which is how most of these
turned up.*

**A test that passed for the wrong reason.** After a refactor, a test claiming to
check the substring length floor passed because the column was being skipped
entirely. *Lesson: a green test is not evidence unless you know why it is green.*

**Two tests referencing a column that did not exist** in the fixture — they failed
immediately, which was lucky; the same mistake in an assertion that happened to
be true would have passed vacuously.

**A validation approach that made things worse.** The first Croissant used
`sc:FileObject`, which gave clean errors while par-baked and then 35 confusing
ones the moment someone fixed `conformsTo`. *Lesson: design for the person who
has to finish the file, not for the tidiest failure right now.*

**A claim of mine that was simply wrong.** I stated that tests only ran from
inside `parbake/`. They ran from anywhere. The `pythonpath` setting added to fix
it was doing nothing at the time — though it became load-bearing later when the
tests moved into `tests/`. *Lesson: check the claim before writing the fix.*

**A shadowed constant that quietly undid a design decision.** `progress.py`
imported `REFRESH_SECONDS` (5.0s) from `resources.py` and then, twenty lines
later, defined `REFRESH_SECONDS = 0.2` for its own redraw rate. The import was
dead and nothing complained. The resource line — deliberately slow, because
walking every worker's `/proc` is expensive — had been running at 0.2s, twenty-
five times its intended rate, across every worker. *Lesson: two different rates
must not share a name. The names are now `REDRAW_SECONDS` and `RESOURCE_SECONDS`,
with a test asserting they stay apart.*

**A display taller than the screen.** The terminal's *width* was consulted and
its *height* never was, so the frame grew with the file count: 1,226 lines into a
24-line terminal. The redraw's cursor-up cannot travel past the top of the
screen, so every frame started from the wrong row and scrolled the display away.
*Lesson: a thing that works at demo size is not thereby tested. The bug needed
1,000 files to appear and was invisible on six.*

**A barrier that bought nothing.** The first version of local copying worked in
batches: copy `workers` files, process them all, delete them, repeat. Every
worker waited for the slowest member of its batch. The per-worker version gives
the same bound with no waiting. *Lesson: the obvious way to enforce a cap was
not the cheap way — the cap fell out of the structure once it was arranged
properly.*

**A measurement that existed only in prose.** The anonymity flags — the most
consequential thing the tool produces — were written into the text report and
nowhere else. Nothing reading the Croissant could see them. It went unnoticed
until `--skip-existing` needed to read findings back. *Lesson: if a finding
matters, it belongs in the machine-readable artefact, not only in the one meant
for people.*

---

## 14. Tests

263 tests, a few seconds, plain pytest.

| file | tests | covers |
|---|---|---|
| `test_describe_csv.py` | 73 | measurements against files whose contents are known |
| `test_progress.py` | 48 | the display, via a fake terminal |
| `test_document_directory.py` | 40 | the directory pass, parallelism, output layout |
| `test_parbaked_croissant.py` | 28 | the Croissant, including real validator runs |
| `test_checkpoints.py` | 18 | saving and resuming a killed read |
| `test_identifiers.py` | 16 | the un-anonymised data check |
| `test_staging.py` | 16 | copying to local disk, and cleaning up after |
| `test_already_done.py` | 12 | skipping files a previous run finished |
| `test_skill_markers.py` | 12 | the strings the skill looks for actually exist |

The ones that matter most, because their failure would be quiet:

- the literal word `NA` survives as text and is not counted as a blank
- batch size does not change any measurement
- parallel and sequential produce identical results
- one unreadable file does not stop the rest of the directory
- the par-baked Croissant fails validation, fails **for the intended reason**,
  cannot be made to validate by deleting the tripwire alone, and **does**
  validate once the outstanding work is genuinely done
- the skill's markers appear in generated files and **do not** appear in a
  reviewed one

---

## 15. The reading skill

`skills/reading-croissant-datasets/SKILL.md`. An AI-readable skill for the other
end of the pipeline: someone hands a Croissant and its dataset to an AI, and the
skill is what stops the AI treating an unreviewed file as documentation.

Six steps. Step 1 is the review check and it comes before anything else, with the
exact markers to look for and the sentence to say. Step 4 lists the failure
shapes that recur in HPC job data — sentinel values, codes that mean the opposite
of the convention, silently biased populations, join keys that do not join.

It deliberately teaches **shapes rather than facts**. Hardcoding "exit code −29
means walltime kill" would rot the way the old runbooks did — one of them still
describes a 14-section template that no longer exists. Each trap points at the
file in front of the reader instead.

Its effectiveness cannot be tested here. What is tested is that it never fails
for the boring reason: every string it tells an AI to look for is asserted
present in generated output and absent from a reviewed Croissant.

---

## 16. What it deliberately does not do

- **CSV only.** `.parquet`, `.json` and `.jsonl` are recognised and listed in the
  index with a stated reason, pending a reader.
- **No subdirectories.** Only the directory you name.
- **No cross-column or cross-file analysis.** No correlations, no key discovery,
  no comparing one dataset against another.
- **No pattern detection.** No "this looks like an email". If it is wanted later
  it should be an explicit, small, named list, not something clever.
- **No checksums.** There was a sha256 computed during the read; it was removed
  as not worth its complexity. `sha256sum` exists.
- **No median or percentiles.** They cannot be computed exactly in one streaming
  pass, and approximation machinery has not earned its place.
- **No promotion.** Nothing turns a par-baked file into a finished one. That is a
  person's job and the tool must not appear to do it.

---

## 17. Where to extend it

**A new measurement**: `ColumnSummary` is the extension point — a starting value
in `__init__`, an update in `add_batch`, a line in `as_dict`. There is a comment
saying exactly that. Anything added must be a measurement, not a conclusion.

**A new file format**: `find_files` classifies, `describe_csv` reads. A reader
would slot in at the read, and the measurement layer would not change.

**Recursion into subdirectories**: `find_files`, one comment marks the spot.

**The preview-versus-full comparison** is the most obviously valuable thing not
yet built. Both modes already measure the same things through the same code path
specifically so their outputs can be diffed. "The preview showed 6 distinct exit
codes; the full file has 42" tells you how far a preview can be trusted on the
next dataset, which is what decides whether a multi-hour pass is worth running.

---

## 18. Open items

- `COLUMN_NAMES_TO_WATCH` over-flags on the bare word `name` — three false alarms
  on the Polaris file.
- Preview runs overstate how much of a file is dead (42 near-constant columns
  versus 29), and the index line does not currently repeat the caveat next to
  that number.
- The encodings trap was cut from the skill pending more detail.
- Non-CSV readers are deferred pending a review of this version's correctness.
- `_estimate_compressed_rows` can read an entire file. The loop stops when it has
  produced enough *output*, but a decompressor that has reached the end of a gzip
  member returns nothing from further input — so a multi-member file (what
  logrotate produces) reads to the end for a few hundred bytes. It needs a cap on
  input bytes. Not the cause of the Lustre hang, but it makes one worse.
- `worker_count()` caps an explicit `--workers` against the file count but not
  against the CPU count, so `--workers 250` is honoured as given. At roughly
  175–200 MB per worker that is about 50 GB, and with `--batch-local-copies` it
  is also 250 concurrent copies. Needs a decision: cap it, or warn loudly with
  the projected memory.
- `bakery/knowledge.py` is written and tested but not yet wired into the baking
  flow — nothing asks "what machine is this" or offers a remembered answer.
- `bakery/` is not under version control at all.
- `parbake/.git` is three commits deep, the last being `91cb372`; everything
  after it — staging, `--skip-existing`, the display fixes, the knowledge store —
  is uncommitted.
