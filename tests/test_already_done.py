"""Tests for --skip-existing: leaving alone files a previous run documented.

The risk in skipping work is quietly claiming things about files nobody looked
at. These tests check that a skipped file's index entry says what the previous
run actually found, and says "not known" whenever it cannot.
"""

import json

import pytest

from already_done import (
    already_documented,
    expected_outputs,
    outcome_from_previous_run,
)
from parbakery import build_index, document_directory
from settings import (
    DEFAULT_VALUES_SHOWN,
    DEFAULT_VALUES_TRACKED,
    INDEX_FILENAME,
)


def make_settings(preview_rows=None, skip_croissant=False):
    return {
        "separator": ",",
        "preview_rows": preview_rows,
        "batch_size": None,
        "values_tracked": DEFAULT_VALUES_TRACKED,
        "values_shown": DEFAULT_VALUES_SHOWN,
        "skip_identifier_check": False,
        "skip_croissant": skip_croissant,
        "scope_description": "every row of every file",
    }


@pytest.fixture
def three_files(tmp_path):
    """Three CSVs, one with a column that gets flagged and one all-empty column."""
    source = tmp_path / "data"
    source.mkdir()
    for number in range(3):
        rows = "".join(
            f"/home/someone/run{row}/out.log,{row},\n" for row in range(30)
        )
        (source / f"file_{number}.csv").write_text(
            "log_path,count,always_empty\n" + rows, encoding="utf-8")
    return source


# --- deciding what counts as done -----------------------------------------

def test_nothing_is_done_in_an_empty_output_directory(three_files, tmp_path):
    for csv_file in sorted(three_files.glob("*.csv")):
        assert not already_documented(tmp_path / "out", csv_file)


def test_a_file_is_done_once_all_its_output_is_there(three_files, tmp_path):
    output = tmp_path / "out"
    document_directory(three_files, output, make_settings(), workers=1)
    for csv_file in sorted(three_files.glob("*.csv")):
        assert already_documented(output, csv_file)


def test_a_half_written_file_is_not_done(three_files, tmp_path):
    """A run killed mid-file can leave a report with no Croissant beside it."""
    output = tmp_path / "out"
    document_directory(three_files, output, make_settings(), workers=1)

    csv_file = sorted(three_files.glob("*.csv"))[0]
    expected_outputs(output, csv_file)["croissant"].unlink()
    assert not already_documented(output, csv_file)


def test_croissants_are_not_required_if_they_were_never_asked_for(three_files, tmp_path):
    """A --no-croissant run is complete without them."""
    output = tmp_path / "out"
    document_directory(three_files, output, make_settings(skip_croissant=True), workers=1)

    csv_file = sorted(three_files.glob("*.csv"))[0]
    assert not already_documented(output, csv_file, want_croissant=True)
    assert already_documented(output, csv_file, want_croissant=False)


# --- rebuilding the index entry -------------------------------------------

def test_a_skipped_file_keeps_the_findings_of_the_run_that_read_it(three_files, tmp_path):
    """The index is rewritten every run, so a skip must not empty it out."""
    output = tmp_path / "out"
    fresh, _ = document_directory(three_files, output, make_settings(), workers=1)

    for original in fresh:
        reused = outcome_from_previous_run(output, original["file"])
        assert reused["rows_read"] == original["rows_read"]
        assert reused["column_count"] == original["column_count"]
        assert reused["scope"] == original["scope"]
        assert reused["columns_to_check"] == original["columns_to_check"]
        assert reused["empty_columns"] == original["empty_columns"]
        assert reused["constant_columns"] == original["constant_columns"]
        assert reused["skipped"] is True


def test_the_fixture_really_does_flag_something(three_files, tmp_path):
    """Otherwise the test above would pass by comparing empty lists."""
    output = tmp_path / "out"
    fresh, _ = document_directory(three_files, output, make_settings(), workers=1)
    assert fresh[0]["columns_to_check"], "fixture stopped flagging anything"
    assert fresh[0]["empty_columns"], "fixture stopped having an empty column"


def test_the_index_of_a_skipped_run_says_the_same_things(three_files, tmp_path):
    output = tmp_path / "out"
    document_directory(three_files, output, make_settings(), workers=1)
    first = (output / INDEX_FILENAME).read_text(encoding="utf-8")

    document_directory(three_files, output, make_settings(), workers=1,
                       skip_existing=True)
    second = (output / INDEX_FILENAME).read_text(encoding="utf-8")

    for finding in ("check before sharing", "empty in every row", "one value covers"):
        assert first.count(finding) == second.count(finding)
    assert "SKIPPED" in second


def test_files_stay_in_directory_order_when_some_are_skipped(three_files, tmp_path):
    output = tmp_path / "out"
    document_directory(three_files, output, make_settings(), workers=1)

    # One file is made incomplete, so it is read again while the others are not.
    middle = sorted(three_files.glob("*.csv"))[1]
    expected_outputs(output, middle)["croissant"].unlink()

    results, _ = document_directory(three_files, output, make_settings(),
                                    workers=1, skip_existing=True)
    assert [outcome["file"].name for outcome in results] == [
        "file_0.csv", "file_1.csv", "file_2.csv"]


# --- not claiming what it does not know -----------------------------------

def test_an_older_croissant_reports_its_check_as_unknown(three_files, tmp_path):
    """A Croissant from before the flags were recorded must not read as "clear".

    Reporting an empty list would put "nothing to check" in the index for a
    file nobody has checked. This tool flags and never certifies.
    """
    output = tmp_path / "out"
    document_directory(three_files, output, make_settings(), workers=1)

    csv_file = sorted(three_files.glob("*.csv"))[0]
    croissant = expected_outputs(output, csv_file)["croissant"]
    document = json.loads(croissant.read_text(encoding="utf-8"))
    del document["_parbake"]["columns_of_concern"]          # as an older run left it
    croissant.write_text(json.dumps(document), encoding="utf-8")

    reused = outcome_from_previous_run(output, csv_file)
    assert reused["columns_to_check"] == []
    assert reused["check_results_unknown"] is True

    index = build_index(three_files, [reused], [], make_settings())
    assert "NOT KNOWN" in index
    assert "without --skip-existing" in index


def test_an_unreadable_croissant_does_not_stop_the_run(three_files, tmp_path):
    output = tmp_path / "out"
    document_directory(three_files, output, make_settings(), workers=1)

    csv_file = sorted(three_files.glob("*.csv"))[0]
    expected_outputs(output, csv_file)["croissant"].write_text(
        "{ this is not json", encoding="utf-8")

    reused = outcome_from_previous_run(output, csv_file)
    assert reused["ok"] is True
    assert reused["check_results_unknown"] is True
    assert reused["column_count"] == 0


def test_the_flag_is_off_by_default(three_files, tmp_path):
    """Normally every file is read again and its output overwritten."""
    output = tmp_path / "out"
    document_directory(three_files, output, make_settings(), workers=1)
    results, _ = document_directory(three_files, output, make_settings(), workers=1)
    assert not any(outcome.get("skipped") for outcome in results)


def test_a_skipped_file_is_never_opened(three_files, tmp_path, monkeypatch):
    """The whole point is that a skipped file costs no reads at all."""
    output = tmp_path / "out"
    document_directory(three_files, output, make_settings(), workers=1)

    import describe_csv
    def refuse(*_args, **_kwargs):
        raise AssertionError("a skipped file was read")
    monkeypatch.setattr(describe_csv, "describe_csv", refuse)
    monkeypatch.setattr("parbakery.describe_csv", refuse)

    results, _ = document_directory(three_files, output, make_settings(),
                                    workers=1, skip_existing=True)
    assert all(outcome["skipped"] for outcome in results)
