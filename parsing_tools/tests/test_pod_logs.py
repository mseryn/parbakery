"""Tests for parsing_tools/pod_logs.py.

Run with:
    cd parbake && pytest parsing_tools/tests -v
"""

import csv
import json
from pathlib import Path


from parsing_tools.pod_logs import (
    Record,
    join_partials,
    main,
    message_fields,
    parse_file,
    parse_log,
    parse_outer,
    read_records,
    row_for,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sambastack_sample.txt"


def rows_of(lines):
    rows = []
    for record in join_partials(_parsed(read_records(lines))):
        rows.append(row_for(record)[0])
    return rows


def _parsed(records):
    for record in records:
        parse_outer(record)
        yield record


def a_record(fields, line=1):
    record = Record(line=line, text=json.dumps(fields))
    parse_outer(record)
    return record


# --- reading lines ----------------------------------------------------------

def test_each_line_is_one_record():
    records = list(read_records(FIXTURE.read_text().splitlines(keepends=True)))
    assert [record.line for record in records] == [1, 2, 3]
    assert all(record.text.startswith("{") for record in records)


def test_blank_lines_are_skipped_and_line_numbers_still_count_them():
    records = list(read_records(['{"a":"1"}\n', "\n", '{"a":"2"}\n']))
    assert [record.line for record in records] == [1, 3]


def test_a_line_number_copied_in_front_of_a_record_is_removed():
    """As `cat -n` or `grep -n` adds when lines are copied out of a terminal."""
    lines = ['     1\t{"a":"1"}\n', '     2 {"a":"2"}\n', '3:{"a":"3"}\n']
    assert [record.text for record in read_records(lines)] == [
        '{"a":"1"}', '{"a":"2"}', '{"a":"3"}']


def test_numbers_are_kept_as_the_text_they_were_written_as():
    record = a_record({})
    record.text = '{"@timestamp":1786665594.023551,"count":2970}'
    parse_outer(record)
    assert record.fields == {"@timestamp": "1786665594.023551", "count": "2970"}


def test_a_line_that_is_not_json_keeps_its_text_and_says_why():
    record = Record(line=4, text="{this is not json")
    parse_outer(record)
    row, _ = row_for(record)
    assert row["parse.raw"] == "{this is not json"
    assert row["parse.problems"].startswith("not-json")


# --- the sample -------------------------------------------------------------

def test_the_sample_splits_into_fields():
    started, usage, finished = rows_of(FIXTURE.read_text().splitlines(keepends=True))

    assert started["pod"] == "auth-and-billing-58bb7d4dfb-wxfnw"
    assert started["appId"] == "hf-in-box-server"
    assert started["log.time"] == "2026-08-13 23:59:54"
    assert started["log.level"] == "INFO"
    assert started["log.message"] == "started call"
    # Only inside the log line's trailing JSON, and lifted out of it.
    assert started["grpc.method"] == "SaveUsage"
    assert started["protocol"] == "grpc"

    assert usage["log.event"] == "/api/usage:save called"
    assert usage["log.ModelId"] == "gpt-oss-120b-8k"
    assert usage["log.InputToken"] == "2970"
    assert usage["log.OutputToken"] == "1280"
    assert usage["log.InputDurationInSeconds"] == "0"

    assert finished["grpc.code"] == "OK"
    assert finished["grpc.time_ms"] == "2.149"
    assert all(row["parse.problems"] == "" for row in (started, usage, finished))


# --- fragments --------------------------------------------------------------

def test_fragments_of_one_long_line_are_joined_into_the_final_record():
    base = {"filename": "/var/log/pods/a/0.log", "container": "a", "stream": "stdout"}
    records = [a_record(dict(base, _p="P", log="first half "), 1),
               a_record(dict(base, _p="P", log="second half "), 2),
               a_record(dict(base, _p="F", log="the end"), 3)]
    joined = list(join_partials(records))

    assert len(joined) == 1
    assert joined[0].fields["log"] == "first half second half the end"
    assert joined[0].partials_joined == 2
    assert joined[0].line == 1


def test_fragments_from_interleaved_containers_are_kept_apart():
    one = {"filename": "one.log", "container": "one", "stream": "stdout"}
    two = {"filename": "two.log", "container": "two", "stream": "stdout"}
    records = [a_record(dict(one, _p="P", log="one-a "), 1),
               a_record(dict(two, _p="P", log="two-a "), 2),
               a_record(dict(one, _p="F", log="one-b"), 3),
               a_record(dict(two, _p="F", log="two-b"), 4)]
    assert sorted(record.fields["log"] for record in join_partials(records)) == [
        "one-a one-b", "two-a two-b"]


def test_a_fragment_with_no_end_is_kept_and_flagged():
    out = list(join_partials([a_record({"filename": "x", "_p": "P", "log": "cut off"})]))
    assert out[0].fields["log"] == "cut off"
    assert out[0].problems[0].startswith("partial-unfinished")


# --- log line shapes --------------------------------------------------------

def test_a_logging_style_line_with_trailing_context():
    fields, context, shape = parse_log(
        '2026-08-13 23:59:54  -  INFO  -  started call  -  {"appId": "x"}')
    assert shape == "logging"
    assert fields == {"log.time": "2026-08-13 23:59:54", "log.level": "INFO",
                      "log.message": "started call"}
    assert context == {"appId": "x"}


def test_a_log_line_that_is_entirely_json():
    fields, context, shape = parse_log(
        '{"level":"warn","msg":"cache miss","key":"abc","ts":"2026-08-13T00:00:00Z"}')
    assert shape == "json"
    assert fields == {"log.time": "2026-08-13T00:00:00Z", "log.level": "warn",
                      "log.message": "cache miss"}
    assert context["key"] == "abc"


def test_a_logfmt_line():
    fields, context, shape = parse_log(
        'time=2026-08-13T00:00:00Z level=error msg="upstream timed out" retries=3')
    assert shape == "logfmt"
    assert fields["log.level"] == "error"
    assert fields["log.message"] == "upstream timed out"
    assert fields["log.retries"] == "3"
    assert context is None


def test_plain_text_is_kept_whole():
    assert parse_log("Traceback (most recent call last):") == (
        {"log.message": "Traceback (most recent call last):"}, None, "text")


def test_a_brace_inside_the_message_is_not_taken_for_the_context():
    fields, context, _ = parse_log(
        '2026-08-13 23:59:54  -  ERROR  -  bad payload - {not json  -  {"appId": "x"}')
    assert fields["log.message"] == "bad payload - {not json"
    assert context == {"appId": "x"}


def test_message_pairs_tolerate_a_pipe_without_spaces():
    assert message_fields("save | ModelId=gpt-oss-120b-8k| InputToken=2970") == {
        "log.ModelId": "gpt-oss-120b-8k", "log.InputToken": "2970", "log.event": "save"}
    assert message_fields("no pairs here") == {}


def test_a_context_value_that_disagrees_is_kept_beside_and_reported():
    record = a_record({"appId": "outer",
                       "log": '2026-08-13 23:59:54  -  INFO  -  x  -  '
                              '{"appId": "inner", "extra": "1"}'})
    row, _ = row_for(record)
    assert row["appId"] == "outer"
    assert row["context.appId"] == "inner"
    assert row["extra"] == "1"
    assert "conflict" in row["parse.problems"]


# --- the whole file ---------------------------------------------------------

def test_the_file_is_written_as_jsonl_and_csv(tmp_path):
    summary = parse_file(FIXTURE, tmp_path)

    jsonl, table = summary.written
    rows = [json.loads(line) for line in jsonl.read_text().splitlines()]
    with open(table, newline="") as handle:
        csv_rows = list(csv.DictReader(handle))

    assert len(rows) == len(csv_rows) == 3
    assert csv_rows[1]["log.InputToken"] == "2970"
    header = list(csv_rows[0].keys())
    assert "grpc.code" in header and "log.userName" in header
    assert header[-1].startswith("parse.")
    assert summary.records == 3 and not summary.problems


def test_the_command_line_prints_a_summary(tmp_path, capsys):
    main([str(FIXTURE), "--out", str(tmp_path)])
    printed = capsys.readouterr().out
    assert "records              3" in printed
    assert "levels               INFO 3" in printed
    assert "problems             none" in printed
    assert (tmp_path / "sambastack_sample.parsed.csv").is_file()


def test_a_missing_source_is_reported_not_raised(tmp_path, capsys):
    main([str(tmp_path / "nowhere.txt"), "--out", str(tmp_path)])
    printed = capsys.readouterr().err
    assert "no such file or directory" in printed
    assert "nothing to parse" in printed


# --- progress ---------------------------------------------------------------

class FakeTerminal:
    def __init__(self, is_terminal):
        self.is_terminal = is_terminal
        self.written = ""

    def isatty(self):
        return self.is_terminal

    def write(self, text):
        self.written += text

    def flush(self):
        pass


def test_the_progress_bar_is_drawn_to_a_terminal(tmp_path):
    terminal = FakeTerminal(True)
    parse_file(FIXTURE, tmp_path, progress_stream=terminal)
    last = terminal.written.rstrip("\n").split("\r")[-1]
    assert last == "[####################] 100%   3 records"
    assert terminal.written.endswith("\n")


def test_nothing_is_drawn_when_the_output_is_not_a_terminal(tmp_path):
    pipe = FakeTerminal(False)
    parse_file(FIXTURE, tmp_path, progress_stream=pipe)
    assert pipe.written == ""


def test_the_bar_fills_with_bytes_read():
    from parsing_tools.pod_logs import Progress
    progress = Progress(total_bytes=200, stream=FakeTerminal(False))
    progress.advance(50, records=7)
    assert progress.line() == "[#####---------------]  25%   7 records"


# --- directories ------------------------------------------------------------

def a_directory_of_exports(root):
    root.mkdir()
    sample = FIXTURE.read_text()
    (root / "b_export.txt").write_text(sample)
    (root / "a_export.txt").write_text(sample)
    (root / "notes.txt").write_text("not a log export\n")
    (root / ".hidden.txt").write_text(sample)
    (root / "already.parsed.csv").write_text("a,b\n")
    (root / "nested").mkdir()
    (root / "nested" / "deeper.txt").write_text(sample)
    return root


def test_a_directory_contributes_the_exports_directly_inside_it(tmp_path):
    from parsing_tools.pod_logs import find_sources
    root = a_directory_of_exports(tmp_path / "exports")
    sources, skipped = find_sources([root])

    # Sorted, top level only, as parbakery.py reads a directory.
    assert [path.name for path in sources] == ["a_export.txt", "b_export.txt"]
    assert [(path.name, why) for path, why in skipped] == [
        ("notes.txt", "its first line is not a JSON record")]


def test_a_file_named_directly_is_parsed_whatever_it_holds(tmp_path):
    from parsing_tools.pod_logs import find_sources
    odd = tmp_path / "odd.txt"
    odd.write_text("not a log export\n")
    assert find_sources([odd]) == ([odd], [])


def test_a_path_that_does_not_exist_is_reported(tmp_path):
    from parsing_tools.pod_logs import find_sources
    assert find_sources([tmp_path / "nowhere"])[1][0][1] == "no such file or directory"


def test_running_on_a_directory_parses_each_export_and_totals_them(tmp_path, capsys):
    root = a_directory_of_exports(tmp_path / "exports")
    out = tmp_path / "parsed"
    main([str(root), "--out", str(out)])
    printed = capsys.readouterr()

    assert sorted(path.name for path in out.iterdir()) == [
        "a_export.parsed.csv", "a_export.parsed.jsonl",
        "b_export.parsed.csv", "b_export.parsed.jsonl"]
    assert "a_export.txt: 3 records, 0 problem(s)" in printed.out
    assert "2 files" in printed.out
    assert "records              6" in printed.out
    assert "skipped" in printed.err and "notes.txt" in printed.err


def test_running_twice_beside_the_sources_does_not_parse_the_first_run(tmp_path, capsys):
    root = a_directory_of_exports(tmp_path / "exports")
    main([str(root)])
    capsys.readouterr()
    main([str(root)])
    assert "records              6" in capsys.readouterr().out


def test_two_sources_that_would_write_the_same_output_do_not_overwrite(tmp_path, capsys):
    first, second = tmp_path / "one", tmp_path / "two"
    for directory in (first, second):
        directory.mkdir()
        (directory / "export.txt").write_text(FIXTURE.read_text())
    main([str(first), str(second), "--out", str(tmp_path / "parsed")])
    printed = capsys.readouterr()
    assert "would overwrite" in printed.err
    assert "records              3" in printed.out


# --- parallel ---------------------------------------------------------------

def test_several_files_in_parallel_write_what_one_at_a_time_writes(tmp_path):
    from parsing_tools.pod_logs import parse_in_parallel
    exports = tmp_path / "exports"
    exports.mkdir()
    lines = FIXTURE.read_text().splitlines()
    for number in range(4):
        (exports / f"pod-{number}.txt").write_text(
            "\n".join(lines[index % 3] for index in range(50 + number * 20)) + "\n")
    sources = sorted(exports.iterdir())

    one_at_a_time = tmp_path / "serial"
    for source in sources:
        parse_file(source, one_at_a_time)

    together = tmp_path / "parallel"
    summaries = parse_in_parallel([(source, together) for source in sources], workers=4)

    assert [summary.records for summary in summaries] == [50, 70, 90, 110]
    for written in one_at_a_time.iterdir():
        assert written.read_bytes() == (together / written.name).read_bytes()


def test_the_workers_option_is_passed_through(tmp_path, capsys, monkeypatch):
    import parsing_tools.pod_logs as pod_logs
    seen = {}

    def record_workers(jobs, workers=None):
        seen["workers"] = workers
        return [parse_file(source, out) for source, out in jobs]

    monkeypatch.setattr(pod_logs, "parse_in_parallel", record_workers)
    root = a_directory_of_exports(tmp_path / "exports")
    main([str(root), "--out", str(tmp_path / "parsed"), "--workers", "3"])
    assert seen["workers"] == 3
