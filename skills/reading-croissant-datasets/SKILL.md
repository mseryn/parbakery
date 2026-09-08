---
name: reading-croissant-datasets
description: Read a Croissant metadata file alongside its dataset before answering questions about that data. Use whenever you are given a .croissant.json, a .parbaked.json, or a generated documentation Markdown file together with a CSV or other data file. Establishes whether a human has reviewed the metadata, finds the caveats that change what the numbers mean, and stops you presenting an unreviewed or naive reading as fact.
---

# Reading a Croissant file with its dataset

You have been given metadata and the data it describes. The metadata exists
because the data is misleading on its own. Read it first, and read it for the
parts that change answers — not just the field names.

Everything below assumes you will also compute against the data. Do that. Do not
estimate a count you could measure.

---

## Step 1 — Establish whether a human has reviewed this file

**Do this before reading anything else, and state the answer in your first
response.** A file nobody has reviewed can be structurally perfect and still say
nothing true.

Check all of these. Any one of them means the file is **par-baked**:

| Where | What you are looking for |
|---|---|
| `conformsTo` | contains `PARBAKED` |
| top-level keys | a `_parbake` key exists |
| `_parbake.status` | says `NOT REVIEWED BY A HUMAN` |
| any field's `description` | says `NOT REVIEWED` |
| fields | none of them has a `dataType` |
| the Markdown | the text `PAR-BAKED` appears |

If **any** of those hold, say so plainly, up front, in these terms:

> This metadata is par-baked: machine-generated and not reviewed by a human. The
> measurements in it are real. Everything else — what fields mean, their types
> and units, the licence, the caveats — is absent, not established. I will tell
> you which of my answers rest on the data itself and which would normally come
> from documentation that does not exist yet.

Repeat the caveat whenever you state something that a reviewed file would have
told you. Do not say it once and then answer as though it were settled.

**Never** treat a par-baked file as a citation, a licence, a provenance record,
or evidence that the data is fit for a purpose. Do not help publish or submit
one. If asked whether the dataset can be shared or cited, the answer from a
par-baked file is always "this file cannot tell you; a person must decide."

---

## Step 2 — What a par-baked file does and does not contain

**Real, and safe to use:**

- the source filename and size
- the column names, exactly as written, in file order
- everything under `_parbake_measurements` — counts, distinct values, most
  common values with their shares, value lengths, how many values parsed as
  numbers and how many did not, and the smallest and largest of those
- `_parbake.scope` and `rows_read_is_the_whole_file` — **check these**. If the
  scope is a preview, every measurement describes the first N rows, not the
  dataset. Say so when you use one.

**Absent on purpose, and not for you to fill in:**

- field descriptions, meanings and units
- data types
- licence, citation, creator, publication date
- responsible-AI notes: limitations, biases, personal information

The absences are deliberate. The tool that wrote the file refuses to guess, and
so should you. If someone asks what a column means and the file is par-baked,
you may describe **what is in it** — "`exit_code` holds 43 distinct values, most
commonly `0` at 60% of rows" — and you must not say what those values signify.

`_parbake.outstanding_for_a_human` lists what is still missing. Use it when
asked what remains to be done.

---

## Step 3 — In a reviewed file, read the caveats before answering

In a reviewed Croissant, the parts that change answers are not the field list.
They are:

- **`rai:dataLimitations`** — the single highest-value block. Read every entry
  before answering any question about counts, rates, or outcomes.
- **Enumeration RecordSets** (`cr:isEnumeration: true`) — the decoding for coded
  columns. If a column has one, you must use it. Do not interpret a code from
  general knowledge when the file tells you what it means here.
- **`rai:personalSensitiveInformation`** — read before doing anything involving
  users, projects, or sharing.
- **Field `description`** — the per-field meaning.
- **`citeAs` and `license`** — before anyone publishes anything.

A limitation that contradicts your expectation is the most useful sentence in
the file. Follow the file, and say that you are doing so.

---

## Step 4 — The traps that actually catch people

These are the shapes of error that recur in HPC job and telemetry data. Each is
a real category, illustrated from ALCF documentation. **The illustrations are
examples of the shape, not facts to reuse** — read the caveats in the file in
front of you, because the specifics differ per dataset and per machine, and some
are marked provisional.

**Sentinel values that look like measurements.** A column of `-1` is not a
measurement of minus one. In one ALCF export `GPUS_REQUESTED` is `-1` on every
row, meaning GPU allocation was never captured. A mean over that column returns
`-1.0` and is meaningless. Check whether a column holds one value across
essentially every row before computing anything from it.

**Codes that do not mean what they usually mean.** Exit codes are the classic
case: the intuitive reading of a signal number can be exactly backwards for a
particular scheduler and machine. Use the enumeration in the file. If there
isn't one, say the codes are undecoded rather than assuming a convention.

**Two columns that nearly agree.** Where a file has two similar columns, the
disagreement is the interesting part, and one of them is usually biased. Check
whether the documentation says which to trust before picking one.

**A population that is silently biased.** Some records are recorded
differently by construction — for example, interactive jobs that always record a
zero exit status regardless of outcome. A rate computed over everything then
looks better than reality. Ask what is *not* represented before quoting a rate.

**The filename disagreeing with the contents.** Date ranges in filenames are
often selection criteria, not content bounds. Read the timestamps.

**Join keys that do not join.** Identifiers from different machines or different
scheduler eras do not correspond even when they look alike. Check formats, and
check the documentation for the intended key, before joining anything.

**Units that are not what the name suggests.** "Cores" may be hardware threads.
"Seconds" may be something else. If the file gives a unit, use it; if it does
not, say the unit is undocumented rather than assuming.

**Blank versus the word "NA".** A field containing the letters `NA` is not
necessarily missing. If measurements distinguish empty fields from NA-like text,
respect the distinction.

---

## Step 5 — How to answer

- **Compute rather than estimate.** If you can run code against the data, run it.
- **Say where each claim came from** — measured from the data, read from the
  metadata, or neither.
- **Quote the caveat you relied on.** "The documentation states X, so I counted
  it this way" is a complete answer. "The count is N" is not.
- **Report what you could not determine.** An unanswerable question answered
  anyway is the failure mode this whole file exists to prevent.
- **Do not fill silence with plausibility.** If a field has no description, say
  it has no description.

---

## Step 6 — Refuse these

- Presenting a par-baked file's contents as reviewed, verified, or complete.
- Writing a field description, data type, unit or meaning into a par-baked file
  and passing it off as documentation. If asked to draft one, label every line
  as a proposal for a human to confirm.
- Removing the `PARBAKED` marker, the `_parbake` block, or the placeholder text
  so a file will validate. Those exist to stop exactly that.
- Stating that a dataset may be published, cited, or shared on the strength of
  metadata that does not say so.
- Answering a question about what data means when the file's own answer is
  absent, without saying that it is absent.

---

## Quick check before your first answer

1. Is it par-baked? Say so if yes.
2. Full scan or preview? Say which, if the file records it.
3. Read `rai:dataLimitations` in full, or note that there are none recorded.
4. List the coded columns that have an enumeration, and the ones that do not.
5. Note any column holding a single value across nearly all rows.
6. Only then answer the question you were asked.
