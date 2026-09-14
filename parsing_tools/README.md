# parsing_tools

Tools that turn exports into tables parbake can measure. Nothing here is called by
`parbakery.py`; run a tool first, then point parbake at its CSV output.


## pod_logs.py

Parses Kubernetes pod logs -- one JSON record per line -- and writes one row per
record as CSV and JSON Lines.

    python3 parsing_tools/pod_logs.py export.txt --out parsed/
    python3 parsing_tools/pod_logs.py exports/ --out parsed/
    python3 parsing_tools/pod_logs.py a.txt b.txt exports/ --out parsed/
    python3 parsing_tools/pod_logs.py exports/ --out parsed/ --workers 4

Then, to measure the result:

    python3 parbakery.py parsed/ --out parsed_documentation --full

Python 3.8 or newer. One file needs only the standard library. Several use
parbake's progress display, which needs `psutil`, as parbake does.


### Input

Files, directories, or both.

- **A file named on the command line** is always parsed.
- **A directory** contributes the files directly inside it, sorted by name.
  Subdirectories are not entered, which is how `parbakery.py` reads a directory.
  Hidden files and this tool's own `.parsed.csv` and `.parsed.jsonl` output are
  passed over, so running twice on a directory does not parse the first run. A
  file whose first non-blank line is not a JSON record is passed over and named on
  stderr.
- **Two sources that would write the same output name** into one `--out`
  directory: the second is skipped and reported rather than overwriting the first.

One record per line. Blank lines are skipped. A line number in front of a record,
as `cat -n` or `grep -n` adds when lines are copied out of a terminal, is removed.

Each record is one container log line as collected on a Kubernetes node:

| fields | from |
|---|---|
| `log` | the container's output |
| `time`, `stream`, `_p` | the container runtime's log framing. `_p` is `F` for a complete line, `P` for a fragment of a long one |
| `namespace`, `pod`, `container`, `node_name`, `filename` | the pod's metadata |
| `@timestamp`, `appId`, `correlationId`, `grpc.*`, ... | fields the collector lifted out of the application's line |

The application's line inside `log` is usually a timestamp, a level, a message,
and a trailing JSON object of context:

    2026-08-13 23:59:54  -  INFO  -  started call  -  {"appId": "hf-in-box-server", ...}


### Output

    <stem>.parsed.csv       one row per record, one column per field
    <stem>.parsed.jsonl     the same rows, one JSON object per line

written beside each source, or into `--out`.

### Several files at once

Several files are parsed side by side, one worker process per file. Each file is
independent, so this changes nothing in the output: it is byte for byte what
parsing them one at a time writes.

The parts are parbakery.py's: the default worker count (up to 8, capped by CPU
count and file count), the process start method, and the progress display. Set
the count with `--workers N`.

There is no fault tolerance, checkpointing or restarting. An error in any file
stops the run.

Measured on 412 MB in 8 files: 16.5 s with one worker, 5.1 s with eight. The
gain is less than the worker count; the cause was not measured.

One file is parsed in the calling process; it cannot be split, because fragments
must be joined in order.


### Progress

One file draws a single bar on stderr, measured in bytes read:

    [########------------]  42%   10,512 records

Several files use parbakery.py's display, one line per file plus workers,
memory and CPU:

    8 worker(s)  |  232.8 MB in use  |    ?   of 16 cores busy

    [1/8] pod-0.txt     [#################---]   83%        37,055 rows      2s
    [2/8] pod-1.txt     [###############-----]   74%        43,641 rows      2s

Both redraw in place only when stderr is a terminal.

For one file, a summary is printed: record count, log levels, sources, the
commonest events, problems, and how many records carry each field. For several,
one line per file and then that summary for all of them together:

    auth-and-billing.txt: 60,000 records, 0 problem(s) -> parsed/auth-and-billing.parsed.csv

Values are kept as the text they were written as. Numbers are not converted:
`@timestamp` stays `1786665594.023551`.

| columns | contents |
|---|---|
| outer field names | as they appear in the record |
| `log.time`, `log.level`, `log.message` | the application's line, split |
| `log.event`, `log.<key>` | a pipe-separated message: `save called \| userName=x \| InputToken=2970` |
| context keys | keys from the line's trailing JSON, merged with the outer fields |
| `context.<key>` | a context value that disagrees with the outer field of that name |
| `parse.line` | the line the record started on |
| `parse.log_shape` | `logging`, `json`, `logfmt` or `text` |
| `parse.partials_joined` | how many fragments were joined into this record |
| `parse.problems` | anything reported, `; `-separated |
| `parse.raw` | the text of a line that could not be parsed |

Context keys that agree with an outer field are not repeated. One that disagrees
is kept beside it and reported.


### Fragments

The container runtime splits a line longer than its buffer into records with `_p`
of `P`, followed by one with `F`. Fragments are joined, in order, into the `F`
record from the same source and stream; records from different containers may
interleave.


### Log line shapes

| shape | example | becomes |
|---|---|---|
| `logging` | `2026-08-13 23:59:54  -  INFO  -  message  -  {...}` | time, level, message, context |
| `json` | `{"level":"warn","msg":"cache miss","ts":"..."}` | time, level and message from common keys; the object as context |
| `logfmt` | `level=error msg="timed out" retries=3` | one `log.<key>` per pair |
| `text` | anything else | kept whole as `log.message` |


### Problems reported

| code | meaning |
|---|---|
| `not-json` | the line did not parse; its text is in `parse.raw` |
| `conflict` | a context value disagreed with the outer field of that name |
| `partial-unfinished` | a `P` fragment had no `F` record after it |


### Tests

    cd parbake && pytest parsing_tools/tests

30 tests, run as part of parbake's suite. The fixture is three records from a real
export, with the email address and API key hash replaced.


### Limits

- **Four log line shapes are recognised**, from one sample. Other shapes are kept
  whole in `log.message`.
- **Unfinished fragments are written last**, after every complete record.
- **Nothing is redacted.** User names and API key hashes in the messages are
  carried into the output. parbake's anonymity check flags `log.userName`.
