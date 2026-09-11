"""Tests for describe_csv.py.

Run with:

    pytest                      # from the parbake directory
    pytest -v                   # one line per test
    pytest -k numbers           # just the number tests

Most tests use one small CSV whose contents are written out below, so the
expected answers can be checked by eye against the file rather than taken on
trust.
"""

from pathlib import Path

import pytest

from describe_csv import describe_csv
from measuring import ColumnSummary


# The test file. Five rows, chosen so every answer is countable by hand.
#
#   name          five different values, lengths 4 to 7
#   category      red x3, blue x1, one blank
#   amount        four numbers (10, 20, 30, -5) and one "NA"
#   mostly_empty  blank except for one "x"
#   always_same   the same value in every row
#
EXAMPLE_CSV = """\
name,category,amount,mostly_empty,always_same
alpha,red,10,,CONST
bravo,red,20,x,CONST
charlie,blue,30,,CONST
delta,red,NA,,CONST
echo,,-5,,CONST
"""


@pytest.fixture
def example_csv(tmp_path):
    """Write the example file and return its path."""
    csv_path = tmp_path / "example.csv"
    csv_path.write_text(EXAMPLE_CSV)
    return csv_path


@pytest.fixture
def described(example_csv):
    """The full-scan result for the example file."""
    return describe_csv(example_csv)


def column(result, name):
    """Shorthand for getting one column's summary out of a result."""
    return result["_summaries"][name]


# --- the file as a whole --------------------------------------------------

def test_reads_every_row(described):
    assert described["file"]["rows_read"] == 5


def test_keeps_column_names_exactly_and_in_order(described):
    assert described["file"]["column_names"] == [
        "name", "category", "amount", "mostly_empty", "always_same"
    ]


def test_reports_column_count(described):
    assert described["file"]["column_count"] == 5


# --- blank fields versus the word "NA" ------------------------------------

def test_blank_fields_are_counted(described):
    assert column(described, "category").empty_count == 1
    assert column(described, "mostly_empty").empty_count == 4


def test_na_like_text_is_counted_separately_from_blanks(described):
    amount = column(described, "amount")
    assert amount.empty_count == 0          # no blank fields
    assert amount.null_like_text_count == 1  # but one field says "NA"


def test_the_word_na_survives_as_a_value(described):
    """The whole point of reading literally: "NA" must not become a blank."""
    values = dict(column(described, "amount").value_counts)
    assert "NA" in values
    assert values["NA"] == 1


# --- counting values ------------------------------------------------------

def test_counts_how_often_each_value_appears(described):
    assert dict(column(described, "category").value_counts) == {"red": 3, "blue": 1}


def test_most_common_value_comes_first(described):
    assert column(described, "category").most_common(1) == [("red", 3)]


def test_a_constant_column_shows_one_value_holding_everything(described):
    always_same = column(described, "always_same")
    assert always_same.distinct_count == 1
    assert always_same.most_common(1) == [("CONST", 5)]
    assert always_same.filled_in_count == 5


def test_distinct_count(described):
    assert column(described, "name").distinct_count == 5
    assert column(described, "category").distinct_count == 2


def test_blank_rows_are_not_counted_as_a_value(described):
    """A blank is recorded as blank, not as a value called "" ."""
    assert "" not in column(described, "category").value_counts


# --- the tracking ceiling -------------------------------------------------

def test_stops_tracking_new_values_at_the_ceiling(example_csv):
    """With room for two values, a five-value column must say so."""
    result = describe_csv(example_csv, values_tracked=2)
    name = column(result, "name")

    assert name.distinct_count == 2
    assert name.stopped_tracking_new_values is True


def test_known_values_keep_counting_after_the_ceiling(tmp_path):
    """Hitting the ceiling must not stop counting the values already tracked."""
    csv_path = tmp_path / "repeats.csv"
    csv_path.write_text("v\n" + "\n".join(["same"] * 10 + ["a", "b", "c"]) + "\n")

    result = describe_csv(csv_path, values_tracked=1)
    value = column(result, "v")

    assert value.most_common(1) == [("same", 10)]
    assert value.stopped_tracking_new_values is True


def test_a_column_under_the_ceiling_is_not_flagged(described):
    assert column(described, "category").stopped_tracking_new_values is False


# --- numbers --------------------------------------------------------------

def test_measures_the_values_that_are_numbers(described):
    amount = column(described, "amount")
    assert amount.number_count == 4          # 10, 20, 30, -5
    assert amount.not_a_number_count == 1    # "NA"
    assert amount.smallest_number == -5
    assert amount.largest_number == 30


def test_a_column_with_one_bad_value_keeps_its_numbers(described):
    """We report the range AND the count of things that were not numbers,
    rather than throwing the range away."""
    amount = column(described, "amount")
    assert amount.is_all_numbers is False
    assert amount.smallest_number is not None


def test_a_fully_numeric_column_says_so(tmp_path):
    csv_path = tmp_path / "numbers.csv"
    csv_path.write_text("n\n1\n2\n3\n")
    assert column(describe_csv(csv_path), "n").is_all_numbers is True


def test_text_columns_have_no_numbers(described):
    assert column(described, "name").number_count == 0


@pytest.mark.parametrize("word", ["nan", "inf", "-inf", "Infinity", "NaN"])
def test_words_that_look_like_numbers_are_not_counted_as_numbers(tmp_path, word):
    """"inf" in a text file is a word, not a measurement."""
    csv_path = tmp_path / "words.csv"
    csv_path.write_text(f"v\n{word}\n")
    value = column(describe_csv(csv_path), "v")

    assert value.number_count == 0
    assert value.smallest_number is None


# --- value lengths --------------------------------------------------------

def test_measures_shortest_and_longest_value(described):
    name = column(described, "name")
    assert name.shortest_value_length == 4   # "echo"
    assert name.longest_value_length == 7    # "charlie"


def test_an_all_blank_column_has_no_lengths(tmp_path):
    csv_path = tmp_path / "blanks.csv"
    csv_path.write_text("a,b\n,x\n,y\n")
    blank = column(describe_csv(csv_path), "a")

    assert blank.empty_count == 2
    assert blank.shortest_value_length is None
    assert blank.distinct_count == 0


# --- preview versus full scan ---------------------------------------------

def test_preview_stops_after_the_requested_rows(example_csv):
    result = describe_csv(example_csv, preview_rows=2)
    assert result["file"]["rows_read"] == 2


def test_preview_measures_only_what_it_read(example_csv):
    result = describe_csv(example_csv, preview_rows=2)
    assert column(result, "name").distinct_count == 2


def test_preview_and_full_scan_are_comparable(example_csv):
    """Both modes must measure the same things, or comparing them is useless."""
    preview = describe_csv(example_csv, preview_rows=2)
    full = describe_csv(example_csv)

    assert preview["columns"].keys() == full["columns"].keys()
    for name in full["columns"]:
        assert preview["columns"][name].keys() == full["columns"][name].keys()


def test_preview_says_it_is_a_preview(example_csv):
    result = describe_csv(example_csv, preview_rows=2)
    assert "preview" in result["file"]["scope"]


# --- batch size must not change the answers -------------------------------

@pytest.mark.parametrize("batch_size", [1, 2, 3, 5, 1000])
def test_batch_size_does_not_change_the_results(example_csv, batch_size):
    """Batch size is a speed setting. If it changed the measurements, a faster
    run would be a different answer, which would make all of this useless."""
    baseline = describe_csv(example_csv, batch_size=1000)["columns"]
    result = describe_csv(example_csv, batch_size=batch_size)["columns"]
    assert result == baseline


# --- one column on its own ------------------------------------------------

def test_column_summary_can_be_used_directly():
    """ColumnSummary is usable without a file, which keeps it easy to extend."""
    import pandas

    summary = ColumnSummary("test", position=0)
    summary.add_batch(pandas.Series(["a", "b", "a", ""]))

    assert summary.rows_seen == 4
    assert summary.empty_count == 1
    assert summary.filled_in_count == 3
    assert summary.most_common(1) == [("a", 2)]


# --- suppressing lists that say nothing -----------------------------------

def test_share_covered_by_measures_how_much_a_list_explains(tmp_path):
    """Ten rows, one value in six of them: the top value covers 60%."""
    csv_path = tmp_path / "coverage.csv"
    csv_path.write_text("v\n" + "\n".join(["same"] * 6 + list("abcd")) + "\n")
    summary = describe_csv(csv_path)["_summaries"]["v"]

    assert summary.share_covered_by(summary.most_common(1)) == 0.6
    assert summary.share_covered_by(summary.most_common(20)) == 1.0


def test_a_column_where_every_value_is_different_covers_almost_nothing(tmp_path):
    """The case the suppression exists for: 100 unique values, top 20 cover 20%."""
    csv_path = tmp_path / "unique.csv"
    csv_path.write_text("v\n" + "\n".join(f"value{n}" for n in range(100)) + "\n")
    summary = describe_csv(csv_path)["_summaries"]["v"]

    assert summary.share_covered_by(summary.most_common(20)) == pytest.approx(0.20)


def test_coverage_is_zero_for_an_empty_column(tmp_path):
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("a,b\n,x\n,y\n")
    summary = describe_csv(csv_path)["_summaries"]["a"]
    assert summary.share_covered_by([]) == 0.0


# --- columns carrying no information --------------------------------------

def test_an_all_blank_column_is_reported_as_empty(tmp_path):
    csv_path = tmp_path / "blank.csv"
    csv_path.write_text("blank,pad\n,x\n,y\n")
    assert describe_csv(csv_path)["_summaries"]["blank"].is_all_empty is True


def test_a_column_with_values_is_not_empty(described):
    assert column(described, "name").is_all_empty is False


def test_a_constant_column_holds_one_value(described):
    always_same = column(described, "always_same")
    assert always_same.holds_one_value is True
    assert always_same.single_value_share == 1.0


def test_a_varied_column_does_not(described):
    assert column(described, "name").holds_one_value is False


def test_the_margin_allows_a_few_odd_rows(tmp_path):
    """199 of 200 rows the same is inside the 1% margin; 195 of 200 is not."""
    for same, expected in ((199, True), (195, False)):
        csv_path = tmp_path / f"margin{same}.csv"
        csv_path.write_text("v\n" + "\n".join(["x"] * same + ["y"] * (200 - same)) + "\n")
        assert describe_csv(csv_path)["_summaries"]["v"].holds_one_value is expected


def test_a_half_empty_column_does_not_count_as_holding_one_value(tmp_path):
    """Measured against all rows, so half blank and half one value scores 0.5.
    That column is not holding one value; it is mostly empty."""
    csv_path = tmp_path / "sparse.csv"
    csv_path.write_text("v,pad\n" + "\n".join([",x"] * 50 + ["same,x"] * 50) + "\n")
    summary = describe_csv(csv_path)["_summaries"]["v"]

    assert summary.single_value_share == 0.5
    assert summary.holds_one_value is False
    assert summary.is_all_empty is False


def test_an_empty_column_is_not_also_counted_as_constant(tmp_path):
    """Every row blank means empty, not "one value covers everything"."""
    csv_path = tmp_path / "blank.csv"
    csv_path.write_text("blank,pad\n,x\n,y\n")
    summary = describe_csv(csv_path)["_summaries"]["blank"]

    assert summary.is_all_empty is True
    assert summary.holds_one_value is False


def test_these_reach_the_json(described):
    entry = described["columns"]["always_same"]
    assert entry["holds_one_value"] is True
    assert entry["is_all_empty"] is False
    assert entry["single_value_share"] == 1.0


def test_a_mostly_empty_column_is_neither_empty_nor_constant(described):
    """mostly_empty is blank in 4 of 5 rows with a single 'x' in the fifth."""
    summary = column(described, "mostly_empty")
    assert summary.is_all_empty is False
    assert summary.holds_one_value is False
    assert summary.single_value_share == 0.2


# --- compressed files -----------------------------------------------------

import bz2 as _bz2      # noqa: E402
import gzip as _gzip    # noqa: E402
import lzma as _lzma    # noqa: E402

from sources import (  # noqa: E402
    compression_of,
    dataset_stem,
    estimate_row_count,
    matched_csv_suffix,
)


@pytest.fixture
def compressed_copies(tmp_path):
    """The same CSV, stored four ways."""
    body = EXAMPLE_CSV.encode()
    paths = {"plain": tmp_path / "example.csv"}
    paths["plain"].write_bytes(body)
    paths["gz"] = tmp_path / "example.csv.gz"
    paths["gz"].write_bytes(_gzip.compress(body))
    paths["bz2"] = tmp_path / "example.csv.bz2"
    paths["bz2"].write_bytes(_bz2.compress(body))
    paths["xz"] = tmp_path / "example.csv.xz"
    paths["xz"].write_bytes(_lzma.compress(body))
    return paths


@pytest.mark.parametrize("name, expected", [
    ("jobs.csv", ".csv"),
    ("jobs.csv.gz", ".csv.gz"),
    ("jobs.csv.bz2", ".csv.bz2"),
    ("jobs.csv.xz", ".csv.xz"),
    ("jobs.CSV.GZ", ".csv.gz"),
    ("notes.txt", ""),
    ("archive.csv.zip", ""),      # a zip can hold several files; not our call
])
def test_which_files_count_as_csv(name, expected):
    """Path.suffix says ".gz" for a compressed CSV, which is how every one of
    them was silently skipped."""
    assert matched_csv_suffix(Path(name)) == expected


@pytest.mark.parametrize("name, expected", [
    ("jobs.csv", "jobs"),
    ("jobs.csv.gz", "jobs"),
    ("aurora_2026-01.csv.bz2", "aurora_2026-01"),
    ("notes.txt", "notes"),
])
def test_the_stem_drops_both_extensions(name, expected):
    """Otherwise a report for jobs.csv.gz is called jobs.csv.txt."""
    assert dataset_stem(Path(name)) == expected


@pytest.mark.parametrize("name, expected", [
    ("jobs.csv", None), ("jobs.csv.gz", "gzip"),
    ("jobs.csv.bz2", "bz2"), ("jobs.csv.xz", "xz"),
])
def test_compression_is_recognised(name, expected):
    assert compression_of(Path(name)) == expected


@pytest.mark.parametrize("kind", ["gz", "bz2", "xz"])
def test_a_compressed_file_reads(compressed_copies, kind):
    assert describe_csv(compressed_copies[kind])["file"]["rows_read"] == 5


@pytest.mark.parametrize("kind", ["gz", "bz2", "xz"])
def test_compression_changes_nothing_that_is_measured(compressed_copies, kind):
    """The same data stored two ways must describe identically, or the format
    a file happens to arrive in would change the answers."""
    plain = describe_csv(compressed_copies["plain"])["columns"]
    assert describe_csv(compressed_copies[kind])["columns"] == plain


def test_the_recorded_size_is_the_size_on_disk(compressed_copies):
    """Compressed bytes, which is what the file actually occupies."""
    described = describe_csv(compressed_copies["gz"])["file"]
    assert described["size_in_bytes"] == compressed_copies["gz"].stat().st_size


@pytest.mark.parametrize("kind", ["gz", "bz2", "xz"])
def test_row_estimates_do_not_count_compressed_bytes(tmp_path, kind):
    """Counting newlines in compressed bytes measures nothing. On a real file
    that gave 269 against an actual 1,999."""
    body = ("v\n" + "\n".join(f"value{n}" for n in range(20_000)) + "\n").encode()
    compress = {"gz": _gzip.compress, "bz2": _bz2.compress, "xz": _lzma.compress}[kind]
    path = tmp_path / f"big.csv.{kind}"
    path.write_bytes(compress(body))

    estimate = estimate_row_count(path)
    assert estimate is not None
    assert abs(estimate - 20_000) / 20_000 < 0.10      # within 10% is fine for a bar


def test_a_truncated_compressed_file_costs_a_bar_not_a_run(tmp_path):
    """A broken file should not take the whole run down for want of a percentage."""
    path = tmp_path / "broken.csv.gz"
    path.write_bytes(_gzip.compress(b"v\n1\n2\n")[:12])
    assert estimate_row_count(path) is None
