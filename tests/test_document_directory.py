"""Tests for document_directory.py.

Run with:

    pytest test_document_directory.py -v

Each test builds a small directory whose contents we already know, documents
it, and checks the reports say what they should.
"""

import pytest

from describe_csv import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_PREVIEW_ROWS,
    DEFAULT_VALUES_SHOWN,
    DEFAULT_VALUES_TRACKED,
)
from document_directory import (
    INDEX_FILENAME,
    build_index,
    describe_one_csv,
    document_directory,
    find_files,
)


def make_settings(preview_rows=DEFAULT_PREVIEW_ROWS, skip_identifier_check=False):
    """The bundle of settings the functions expect, with sensible defaults."""
    return {
        "separator": ",",
        "preview_rows": preview_rows,
        "batch_size": DEFAULT_BATCH_SIZE,
        "values_tracked": DEFAULT_VALUES_TRACKED,
        "values_shown": DEFAULT_VALUES_SHOWN,
        "skip_identifier_check": skip_identifier_check,
        "scope_description": "the first 1,000 rows of each file",
    }


@pytest.fixture
def example_directory(tmp_path):
    """A directory with known contents, rebuilt fresh for every test.

    Three CSVs and two non-CSVs, plus a subdirectory that should be ignored.
    """
    source = tmp_path / "data"
    source.mkdir()
    (source / "jobs.csv").write_text("username,exit_code\nchulwoo,0\nazamatm,1\n")
    (source / "telemetry.csv").write_text("node,reading\nx3008,1.5\nx3009,2.5\n")
    (source / "UPPERCASE.CSV").write_text("m,n\n5,6\n")
    (source / "notes.txt").write_text("not a csv\n")
    (source / "diagram.pdf").write_text("also not a csv\n")
    (source / "subfolder").mkdir()
    return source


@pytest.fixture
def output_directory(tmp_path):
    return tmp_path / "out"


# --- finding files (unchanged behaviour) ----------------------------------

def test_finds_every_csv(example_directory):
    csv_files, _ = find_files(example_directory)
    assert len(csv_files) == 3


def test_extension_match_ignores_case(example_directory):
    csv_files, _ = find_files(example_directory)
    assert "UPPERCASE.CSV" in [path.name for path in csv_files]


def test_non_csv_files_are_kept_separately(example_directory):
    _, other_files = find_files(example_directory)
    assert sorted(path.name for path in other_files) == ["diagram.pdf", "notes.txt"]


def test_subdirectories_are_ignored(example_directory):
    csv_files, other_files = find_files(example_directory)
    assert "subfolder" not in [path.name for path in csv_files + other_files]


def test_results_are_sorted_so_runs_are_repeatable(example_directory):
    csv_files, other_files = find_files(example_directory)
    assert [p.name for p in csv_files] == sorted(p.name for p in csv_files)
    assert [p.name for p in other_files] == sorted(p.name for p in other_files)


# --- describing one file --------------------------------------------------

def test_writes_a_report_for_one_csv(example_directory, output_directory):
    output_directory.mkdir()
    outcome = describe_one_csv(
        example_directory / "jobs.csv", output_directory, make_settings()
    )

    assert outcome["ok"] is True
    assert outcome["rows_read"] == 2
    assert outcome["column_count"] == 2
    assert outcome["report_path"].exists()
    assert "username" in outcome["report_path"].read_text()


def test_the_report_is_named_after_the_csv(example_directory, output_directory):
    output_directory.mkdir()
    outcome = describe_one_csv(
        example_directory / "jobs.csv", output_directory, make_settings()
    )
    assert outcome["report_path"].name == "jobs.txt"


def test_identifier_concerns_are_carried_up_to_the_index(example_directory, output_directory):
    """The index should say which columns need a look, without opening the report."""
    output_directory.mkdir()
    outcome = describe_one_csv(
        example_directory / "jobs.csv", output_directory, make_settings()
    )
    assert "username" in outcome["columns_to_check"]


def test_the_identifier_check_can_be_skipped(example_directory, output_directory):
    output_directory.mkdir()
    outcome = describe_one_csv(
        example_directory / "jobs.csv", output_directory,
        make_settings(skip_identifier_check=True),
    )
    assert outcome["columns_to_check"] == []


# --- a file that cannot be read -------------------------------------------

@pytest.fixture
def directory_with_a_broken_file(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    (source / "good.csv").write_text("a,b\n1,2\n")
    (source / "broken.csv").write_text('a,b,c\n"unterminated,2,3\n4,5,6\n')
    return source


def test_an_unreadable_file_does_not_stop_the_others(
    directory_with_a_broken_file, output_directory
):
    results, _ = document_directory(
        directory_with_a_broken_file, output_directory, make_settings()
    )
    by_name = {outcome["file"].name: outcome for outcome in results}

    assert by_name["broken.csv"]["ok"] is False
    assert by_name["good.csv"]["ok"] is True


def test_the_reason_a_file_failed_is_written_down(
    directory_with_a_broken_file, output_directory
):
    results, _ = document_directory(
        directory_with_a_broken_file, output_directory, make_settings()
    )
    broken = next(o for o in results if o["file"].name == "broken.csv")

    assert "ParserError" in broken["problem"]
    assert "Could not read" in broken["report_path"].read_text()


def test_a_failure_is_visible_in_the_index(directory_with_a_broken_file, output_directory):
    document_directory(directory_with_a_broken_file, output_directory, make_settings())
    index = (output_directory / INDEX_FILENAME).read_text()
    assert "COULD NOT READ" in index


# --- the whole directory --------------------------------------------------

def test_writes_the_index_and_one_report_per_csv(example_directory, output_directory):
    results, other_files = document_directory(
        example_directory, output_directory, make_settings()
    )

    assert (output_directory / INDEX_FILENAME).exists()
    assert len(results) == 3
    assert len(other_files) == 2
    for outcome in results:
        assert outcome["report_path"].exists()


def test_the_output_directory_is_created_if_missing(example_directory, tmp_path):
    target = tmp_path / "does" / "not" / "exist"
    document_directory(example_directory, target, make_settings())
    assert (target / INDEX_FILENAME).exists()


def test_every_file_appears_in_the_index(example_directory, output_directory):
    """A file silently missing from the index is what this tool exists to prevent."""
    document_directory(example_directory, output_directory, make_settings())
    index = (output_directory / INDEX_FILENAME).read_text()

    for name in ["jobs.csv", "telemetry.csv", "UPPERCASE.CSV", "notes.txt", "diagram.pdf"]:
        assert name in index


def test_the_index_states_both_counts(example_directory, output_directory):
    document_directory(example_directory, output_directory, make_settings())
    index = (output_directory / INDEX_FILENAME).read_text()

    assert "# CSV files (3)" in index
    assert "# Other files, not examined (2)" in index


def test_the_index_records_what_was_read(example_directory, output_directory):
    """A preview and a full scan produce very different numbers, so the index
    has to say which one it was."""
    document_directory(example_directory, output_directory, make_settings())
    index = (output_directory / INDEX_FILENAME).read_text()
    assert "the first 1,000 rows of each file" in index


def test_an_empty_directory_is_reported_not_an_error(tmp_path, output_directory):
    empty = tmp_path / "empty"
    empty.mkdir()
    results, other_files = document_directory(empty, output_directory, make_settings())

    assert results == []
    assert other_files == []
    assert "(none)" in (output_directory / INDEX_FILENAME).read_text()


def test_a_full_scan_reads_everything(example_directory, output_directory):
    settings = make_settings(preview_rows=None)
    settings["scope_description"] = "every row of every file"
    results, _ = document_directory(example_directory, output_directory, settings)

    jobs = next(o for o in results if o["file"].name == "jobs.csv")
    assert jobs["scope"] == "full scan"


def test_the_index_can_be_built_without_touching_the_disk(example_directory):
    """build_index is separate from writing it, so it can be checked directly."""
    csv_files, other_files = find_files(example_directory)
    text = build_index(example_directory, [], other_files, make_settings())
    assert "# CSV files (0)" in text


# --- columns carrying no information reach the index ----------------------

@pytest.fixture
def directory_with_dull_columns(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    (source / "dull.csv").write_text(
        "varied,constant,blank\n" + "\n".join(f"{n},SAME," for n in range(10)) + "\n"
    )
    return source


def test_empty_and_constant_columns_are_carried_up(directory_with_dull_columns, output_directory):
    output_directory.mkdir()
    outcome = describe_one_csv(
        directory_with_dull_columns / "dull.csv", output_directory, make_settings()
    )
    assert outcome["empty_columns"] == ["blank"]
    assert outcome["constant_columns"] == ["constant"]


def test_they_appear_in_the_index(directory_with_dull_columns, output_directory):
    document_directory(directory_with_dull_columns, output_directory, make_settings())
    index = (output_directory / INDEX_FILENAME).read_text()

    assert "empty in every row (1): blank" in index
    assert "one value covers 99%+ of rows (1): constant" in index


def test_a_file_with_nothing_dull_says_nothing(example_directory, output_directory):
    """telemetry.csv has two varied columns, so it should add no such lines."""
    output_directory.mkdir()
    outcome = describe_one_csv(
        example_directory / "telemetry.csv", output_directory, make_settings()
    )
    assert outcome["empty_columns"] == []
    assert outcome["constant_columns"] == []
