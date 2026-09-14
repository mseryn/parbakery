#!/usr/bin/env python3
"""Parse Kubernetes pod logs, one JSON record per line, into a table.

Each line is one container log record as collected on a Kubernetes node: the
container's output in `log`, the container runtime's framing around it (`time`,
`stream`, and `_p`, which is F for a complete line and P for a fragment of a long
one), and the pod's metadata (`namespace`, `pod`, `container`, `node_name`,
`filename`). The application's own line inside `log` is usually a timestamp, a
level, a message and a trailing JSON object of context.

This splits all of that into one row per record, with one column per field.

Usage:
    python3 pod_logs.py export.txt                  # export.parsed.jsonl and .csv
    python3 pod_logs.py export.txt --out parsed/
    python3 pod_logs.py exports/ --out parsed/      # every export in a directory
    python3 pod_logs.py a.txt b.txt exports/ --out parsed/
    python3 pod_logs.py exports/ --out parsed/ --workers 4

Several files are parsed side by side, one worker process per file, using
parbakery.py's worker count, process start method and progress display. There
is no fault tolerance: an error in any file stops the run.

Values are kept as the text they were written as; nothing is converted. The CSV
can be read by parbakery.py.

Python 3.8 or newer. One file needs only the standard library; several use
parbake's progress display, which needs psutil, as parbake does.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

# A line number in front of a record, as `cat -n` or `grep -n` adds when lines
# are copied out of a terminal. Only recognised when a record follows it.
LINE_NUMBER = re.compile(r"^[ \t]*\d+(?:\t|:| +)(?=\{)")


# --- records ---------------------------------------------------------------

@dataclass
class Record:
    """One log record, from its line to the fields it holds."""

    line: int
    text: str
    fields: Optional[dict] = None
    partials_joined: int = 0
    problems: List[str] = field(default_factory=list)

    def problem(self, code, detail=""):
        self.problems.append(f"{code}: {detail}" if detail else code)


def read_records(lines: Iterable[str]) -> Iterator[Record]:
    """One record per non-blank line, with any copied line number removed."""
    for number, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r\n")
        if line.strip():
            yield Record(number, LINE_NUMBER.sub("", line, count=1))


def parse_outer(record: Record) -> None:
    """Parse the record's JSON, keeping every number as the text it was written as."""
    try:
        value = json.loads(record.text, parse_float=str, parse_int=str)
    except json.JSONDecodeError as problem:
        record.problem("not-json", f"{problem.msg} at character {problem.pos}")
        return
    if not isinstance(value, dict):
        record.problem("not-json", "valid JSON, but not an object")
        return
    record.fields = value


# --- fragments -------------------------------------------------------------

def join_partials(records: Iterable[Record]) -> Iterator[Record]:
    """Reassemble log lines the container runtime split into fragments.

    A line longer than the runtime's buffer arrives as records with `_p` set to P,
    followed by one with F. Fragments are held per source and stream, because
    records from different containers interleave, and joined into the F record.
    """
    pending: Dict[tuple, List[Record]] = {}
    for record in records:
        fields = record.fields
        if fields is None or "_p" not in fields:
            yield record
            continue
        source = (fields.get("filename") or fields.get("pod"),
                  fields.get("container"), fields.get("stream"))
        if fields.get("_p") == "P":
            pending.setdefault(source, []).append(record)
            continue
        parts = pending.pop(source, [])
        if parts:
            fields["log"] = "".join(str(part.fields.get("log", "")) for part in parts) \
                + str(fields.get("log", ""))
            record.partials_joined = len(parts)
            record.line = parts[0].line
        yield record
    for parts in pending.values():
        for part in parts:
            part.problem("partial-unfinished",
                         "a fragment with no final record after it; kept as found")
            yield part


# --- the application's own line --------------------------------------------

LOGGING_LINE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)\s+-\s+(?P<level>[A-Za-z]+)\s+-\s+(?P<rest>.*)$",
    re.DOTALL)
CONTEXT_START = re.compile(r"\s+-\s+\{")
LOGFMT_PAIR = re.compile(r'([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|\S*)')

# Keys a structured log line commonly uses for its time, level and message.
TIME_KEYS = ("time", "timestamp", "ts", "@timestamp")
LEVEL_KEYS = ("level", "severity", "lvl")
MESSAGE_KEYS = ("msg", "message", "event")


def _first(mapping, keys):
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def parse_log(line: str) -> Tuple[Dict[str, str], Optional[dict], str]:
    """Split one application log line into fields.

    Returns (fields, context, shape). Fields are named log.*; context is the
    JSON object the line carried, if any; shape names the form that was
    recognised, so a file's variety can be counted.

        logging   2026-08-13 23:59:54  -  INFO  -  message  -  {context}
        json      the whole line is a JSON object
        logfmt    level=info msg="message" key=value
        text      anything else, kept whole as the message
    """
    stripped = line.strip()
    found = LOGGING_LINE.match(stripped)
    if found:
        message, context = _split_context(found.group("rest"))
        fields = {"log.time": found.group("time"), "log.level": found.group("level"),
                  "log.message": message}
        fields.update(message_fields(message))
        return fields, context, "logging"

    if stripped.startswith("{"):
        try:
            value = json.loads(stripped, parse_float=str, parse_int=str)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            fields = {}
            for name, keys in (("log.time", TIME_KEYS), ("log.level", LEVEL_KEYS),
                               ("log.message", MESSAGE_KEYS)):
                found_value = _first(value, keys)
                if found_value is not None:
                    fields[name] = text_of(found_value)
            return fields, value, "json"

    pairs = LOGFMT_PAIR.findall(stripped)
    if len(pairs) >= 2 and _mostly_pairs(stripped):
        values = {key: _unquote(value) for key, value in pairs}
        fields = {f"log.{key}": value for key, value in values.items()}
        for name, keys in (("log.time", TIME_KEYS), ("log.level", LEVEL_KEYS),
                           ("log.message", MESSAGE_KEYS)):
            found_value = _first(values, keys)
            if found_value is not None:
                fields[name] = found_value
        return fields, None, "logfmt"

    return {"log.message": stripped}, None, "text"


def _split_context(rest):
    """Separate a trailing JSON object from the message before it.

    Tried from the left, so the first "  -  {" whose remainder parses to the end
    is the outermost object, and a brace inside the message is not mistaken for it.
    """
    for found in CONTEXT_START.finditer(rest):
        start = found.end() - 1
        try:
            value = json.loads(rest[start:], parse_float=str, parse_int=str)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return rest[:found.start()].strip(), value
    return rest.strip(), None


def _mostly_pairs(text):
    residue = LOGFMT_PAIR.sub("", text)
    return len(residue.strip()) <= len(text) * 0.2


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def message_fields(message: str) -> Dict[str, str]:
    """Pull key=value pairs out of a pipe-separated message.

        /api/usage:save called | userName=a@b | ModelId=gpt-oss-120b-8k| InputToken=2970

    becomes log.event, log.userName, log.ModelId and log.InputToken. The pipe
    need not have spaces around it. A part without "=" is the event.
    """
    if "|" not in message or "=" not in message:
        return {}
    fields, event = {}, []
    for part in message.split("|"):
        part = part.strip()
        if not part:
            continue
        key, separator, value = part.partition("=")
        key = key.strip()
        if separator and key and not any(character.isspace() for character in key):
            fields.setdefault(f"log.{key}", value.strip())
        else:
            event.append(part)
    if event:
        fields["log.event"] = " | ".join(event)
    return fields


# --- one output row --------------------------------------------------------

def text_of(value) -> str:
    """A field value as text, for a CSV cell. Nothing is reinterpreted."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def row_for(record: Record) -> Tuple[Dict[str, str], str]:
    """Flatten a record into one row, and name the shape of its log line.

    The outer fields keep their names. The log line's parts are log.*. Keys from
    its JSON context join the outer namespace, since the collector already lifted
    some of them there: one that is new is added, one that agrees is not
    repeated, and one that disagrees is kept beside it as context.<key> and
    reported.
    """
    row: Dict[str, str] = {}
    shape = ""
    if record.fields is None:
        row["parse.raw"] = record.text
    else:
        for key, value in record.fields.items():
            row[key] = text_of(value)
        log = record.fields.get("log")
        if isinstance(log, str):
            parsed, context, shape = parse_log(log)
            for key, value in parsed.items():
                row.setdefault(key, value)
            for key, value in (context or {}).items():
                value = text_of(value)
                if key not in row:
                    row[key] = value
                elif row[key] != value:
                    row[f"context.{key}"] = value
                    record.problem("conflict",
                                   f"{key} is {row[key]!r} outside the log line "
                                   f"and {value!r} inside it")

    row["parse.line"] = str(record.line)
    row["parse.log_shape"] = shape
    row["parse.partials_joined"] = str(record.partials_joined)
    row["parse.problems"] = "; ".join(record.problems)
    return row, shape


# --- progress --------------------------------------------------------------

class Progress:
    """A one-line progress bar on stderr, measured in bytes of the source read.

        [2/14] export.txt  [########------------]  42%   18,204 records

    Drawn only to a terminal, and at most every REDRAW_SECONDS. When stderr is a
    file or a pipe nothing is drawn, so a log is not filled with carriage returns.
    """

    WIDTH = 20
    REDRAW_SECONDS = 0.2

    def __init__(self, total_bytes, stream=None, label=""):
        self.label = label
        self.total = total_bytes
        self.done = 0
        self.records = 0
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.last_drawn = 0.0

    def line(self):
        fraction = min(1.0, self.done / self.total) if self.total else 1.0
        filled = int(fraction * self.WIDTH)
        return (f"{self.label}[{'#' * filled}{'-' * (self.WIDTH - filled)}] "
                f"{fraction:4.0%}   {self.records:,} records")

    def advance(self, byte_count, records=0):
        self.done += byte_count
        self.records += records
        now = time.monotonic()
        if self.enabled and now - self.last_drawn >= self.REDRAW_SECONDS:
            self.last_drawn = now
            self.stream.write("\r" + self.line())
            self.stream.flush()

    def finish(self):
        if self.enabled:
            self.done = self.total
            self.stream.write("\r" + self.line() + "\n")
            self.stream.flush()


class QueueProgress:
    """Progress from a worker process, sent to the parent over a queue.

    The messages are the ones parbakery.py's workers send, so its display draws
    them. That display counts rows, so a running estimate of the file's records --
    records so far scaled by the share of bytes read -- is sent with each update,
    which makes the bar exactly as far along as the bytes are.
    """

    def __init__(self, name, total_bytes, queue):
        self.name = name
        self.total = total_bytes
        self.done = 0
        self.records = 0
        self.queue = queue
        self.started_at = time.monotonic()
        self.last_sent = 0.0
        queue.put(("start", name, 0, 0.0))

    def advance(self, byte_count, records=0):
        self.done += byte_count
        self.records += records
        now = time.monotonic()
        if now - self.last_sent >= Progress.REDRAW_SECONDS and self.done and self.records:
            self.last_sent = now
            estimate = max(self.records, int(self.records * self.total / self.done))
            self.queue.put(("estimate", self.name, estimate, 0.0))
            self.queue.put(("progress", self.name, self.records, now - self.started_at))

    def finish(self):
        pass


def counted_lines(handle, progress):
    """Decode a binary file line by line, telling the progress bar each line's size.

    Read as bytes so the count is exact; a text-mode file cannot report its
    position while it is being iterated.
    """
    for raw in handle:
        progress.advance(len(raw))
        yield raw.decode("utf-8", errors="replace")


# --- the whole file --------------------------------------------------------

@dataclass
class Summary:
    source: Path
    records: int = 0
    partials: int = 0
    shapes: Counter = field(default_factory=Counter)
    levels: Counter = field(default_factory=Counter)
    sources: Counter = field(default_factory=Counter)
    events: Counter = field(default_factory=Counter)
    problems: Counter = field(default_factory=Counter)
    fields: Counter = field(default_factory=Counter)
    written: List[Path] = field(default_factory=list)

    def add(self, other):
        """Fold another file's counts into this one, for a directory's totals."""
        self.records += other.records
        self.partials += other.partials
        for name in ("shapes", "levels", "sources", "events", "problems", "fields"):
            getattr(self, name).update(getattr(other, name))

    def one_line(self):
        problems = sum(self.problems.values())
        csv_path = self.written[-1] if self.written else ""
        return (f"{self.source.name}: {self.records:,} records, "
                f"{problems:,} problem(s) -> {csv_path}")

    def lines(self):
        out = [f"{self.source}",
               f"  records              {self.records}"]
        if self.partials:
            out.append(f"  fragments joined     {self.partials}")
        out.append("  log line shapes      " + _counts(self.shapes))
        if self.levels:
            out.append("  levels               " + _counts(self.levels))
        out.append("  sources              " + _counts(self.sources, 6))
        if self.events:
            out.append("  commonest events     " + _counts(self.events, 8))
        out.append("  problems             " + (_counts(self.problems) if self.problems
                                                 else "none"))
        out.append(f"  fields               {len(self.fields)}")
        for name, count in sorted(self.fields.items(), key=lambda item: (-item[1], item[0])):
            out.append(f"      {count:>8}  {name}")
        for path in self.written:
            out.append(f"  written              {path}")
        return out


def _counts(counter, limit=None):
    shown = counter.most_common(limit)
    text = ", ".join(f"{name or '(none)'} {count}" for name, count in shown)
    if limit and len(counter) > limit:
        text += f", and {len(counter) - limit} more"
    return text or "none"


def parse_file(source: Path, out_directory: Path, progress_stream=None,
               label="", progress=None) -> Summary:
    """Parse one export into <stem>.parsed.jsonl and <stem>.parsed.csv.

    The JSONL is written first, in one streaming pass, while the set of columns
    is collected; the CSV is then written from it, so a file of any size needs
    memory only for the list of columns.
    """
    summary = Summary(source=source)
    out_directory.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_directory / f"{source.stem}.parsed.jsonl"
    csv_path = out_directory / f"{source.stem}.parsed.csv"
    columns: Dict[str, None] = {}

    def parsed(records):
        for record in records:
            parse_outer(record)
            yield record

    if progress is None:
        progress = Progress(source.stat().st_size, progress_stream, label)
    with open(source, "rb") as handle, open(jsonl_path, "w", encoding="utf-8") as jsonl:
        lines = counted_lines(handle, progress)
        for record in join_partials(parsed(read_records(lines))):
            row, shape = row_for(record)
            jsonl.write(json.dumps(row, ensure_ascii=False) + "\n")
            _tally(summary, record, row, shape)
            progress.advance(0, records=1)
            for name in row:
                columns.setdefault(name, None)
    progress.finish()

    ordered = [name for name in columns if not name.startswith("parse.")] + \
              [name for name in columns if name.startswith("parse.")]
    with open(jsonl_path, encoding="utf-8") as jsonl, \
            open(csv_path, "w", encoding="utf-8", newline="") as table:
        writer = csv.DictWriter(table, fieldnames=ordered, restval="")
        writer.writeheader()
        for line in jsonl:
            writer.writerow(json.loads(line))

    summary.written = [jsonl_path, csv_path]
    return summary


def _tally(summary, record, row, shape):
    summary.records += 1
    summary.partials += record.partials_joined
    summary.shapes[shape or "not parsed"] += 1
    if row.get("log.level"):
        summary.levels[row["log.level"]] += 1
    source = "/".join(part for part in (row.get("namespace"), row.get("container")) if part)
    summary.sources[source] += 1
    event = row.get("log.event") or row.get("log.message")
    if event:
        summary.events[event[:80]] += 1
    for problem in record.problems:
        summary.problems[problem.split(":", 1)[0]] += 1
    for name in row:
        if not name.startswith("parse.") and row[name] != "":
            summary.fields[name] += 1


# Our own output, so running on a directory twice does not parse the first run.
OUTPUT_SUFFIXES = (".parsed.jsonl", ".parsed.csv")


def looks_like_records(path: Path) -> bool:
    """Is the first non-blank line of this file a JSON record?

    Read as bytes and only as far as the first non-blank line, so a directory of
    large files is sorted without reading them.
    """
    try:
        with open(path, "rb") as handle:
            for raw in handle:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    return LINE_NUMBER.sub("", line, count=1).startswith("{")
    except OSError:
        return False
    return False


def find_sources(paths: Iterable[Path]) -> Tuple[List[Path], List[Tuple[Path, str]]]:
    """The files to parse, and the ones passed over with the reason.

    A file named on the command line is always parsed. A directory contributes the
    files directly inside it, sorted by name, as parbakery.py reads a directory:
    subdirectories are not entered. Within a directory, hidden files, this tool's
    own output, and files whose first line is not a record are passed over.
    """
    sources, skipped = [], []
    for path in paths:
        if path.is_file():
            sources.append(path)
        elif path.is_dir():
            for entry in sorted(path.iterdir()):
                if not entry.is_file():
                    continue
                if entry.name.startswith("."):
                    continue
                if entry.name.endswith(OUTPUT_SUFFIXES):
                    continue
                if looks_like_records(entry):
                    sources.append(entry)
                else:
                    skipped.append((entry, "its first line is not a JSON record"))
        else:
            skipped.append((path, "no such file or directory"))
    return sources, skipped


def _parse_in_worker(job):
    """Parse one file in a worker process, reporting progress over the queue."""
    source, out_directory, queue = job
    started = time.monotonic()
    progress = QueueProgress(source.name, source.stat().st_size, queue)
    summary = parse_file(source, out_directory, progress=progress)
    queue.put(("finished", source.name, summary, time.monotonic() - started))
    return summary


def _apply(display, message):
    """Fold one worker message into parbakery's display."""
    kind, name, payload, seconds = message
    if kind == "start":
        display.update(name, state="reading")
    elif kind == "estimate":
        display.update(name, estimated_rows=payload)
    elif kind == "progress":
        display.update(name, state="reading", rows_read=payload, seconds=seconds)
    elif kind == "finished":
        problems = sum(payload.problems.values())
        display.update(name, state="done", rows_read=payload.records, seconds=seconds,
                       note=f"{problems:,} problem(s)")


def parse_in_parallel(jobs: List[Tuple[Path, Path]], workers=None) -> List[Summary]:
    """Parse several files at once, one worker process per file.

    The parts come from parbakery.py rather than being written again: how many
    workers to start, the start method that avoids forking a process with threads
    in it, and the one-line-per-file display. They are imported here and not at
    the top of the module, so worker processes -- which import this module --
    do not import parbake, and pandas with it.

    No fault tolerance, checkpointing or restarting: an exception in a worker is
    raised here and ends the run.
    """
    import queue as queue_module
    from concurrent.futures import ProcessPoolExecutor

    parbake = str(Path(__file__).resolve().parent.parent)
    if parbake not in sys.path:
        sys.path.insert(0, parbake)
    from parbakery import _pool_context, worker_count
    from progress import ProgressDisplay

    count = worker_count(workers, len(jobs))
    display = ProgressDisplay([source.name for source, _ in jobs], workers=count)
    context = _pool_context()

    def drain(queue):
        while True:
            try:
                _apply(display, queue.get_nowait())
            except queue_module.Empty:
                return

    with context.Manager() as manager:
        queue = manager.Queue()
        with ProcessPoolExecutor(max_workers=count, mp_context=context) as pool:
            futures = [pool.submit(_parse_in_worker, (source, out_directory, queue))
                       for source, out_directory in jobs]
            while not all(future.done() for future in futures):
                drain(queue)
                display.draw()
                time.sleep(0.05)
            drain(queue)
            summaries = [future.result() for future in futures]
    display.finish()
    return summaries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sources", nargs="+", type=Path,
                        help="exports, or directories of them")
    parser.add_argument("--out", type=Path,
                        help="directory for the output (default: beside each source)")
    parser.add_argument("--workers", type=int, metavar="N",
                        help="files to parse at once (default: as parbakery.py -- "
                             "up to 8, capped by CPU count and file count)")
    arguments = parser.parse_args(argv)

    sources, skipped = find_sources(arguments.sources)
    for path, why in skipped:
        print(f"skipped {path}: {why}", file=sys.stderr)
    if not sources:
        print("nothing to parse", file=sys.stderr)
        return

    if len(sources) == 1:
        summary = parse_file(sources[0], arguments.out or sources[0].parent)
        print("\n".join(summary.lines()))
        return

    jobs, claimed = [], {}
    for source in sources:
        destination = (arguments.out or source.parent) / f"{source.stem}.parsed.csv"
        if destination in claimed:
            print(f"skipped {source}: its output would overwrite that of "
                  f"{claimed[destination]}", file=sys.stderr)
            continue
        claimed[destination] = source
        jobs.append((source, arguments.out or source.parent))

    summaries = parse_in_parallel(jobs, arguments.workers)

    total = Summary(source=Path(f"{len(summaries)} files"))
    for summary in summaries:
        print(summary.one_line())
        total.add(summary)
    print()
    print("\n".join(total.lines()))


if __name__ == "__main__":
    main()
