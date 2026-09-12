#!/usr/bin/env python3
"""Per-column measurements, accumulated one batch at a time."""

import numpy
import pandas

from settings import (
    DEFAULT_VALUES_SHOWN,
    DEFAULT_VALUES_TRACKED,
    NULL_LIKE_TEXT,
    SINGLE_VALUE_THRESHOLD,
)


class ColumnSummary:
    """Everything measured about a single column, folded in one batch at a time.

    Nothing here grows with the size of the file except the tracked-value table,
    which has a hard ceiling. That is what lets a file of any size be described
    in one pass.

    To measure something new, add it in three places: a starting value in
    __init__, the update in add_batch, and a line in as_dict.
    """

    def __init__(self, column_name, position, values_tracked=DEFAULT_VALUES_TRACKED):
        self.column_name = column_name
        self.position = position          # which column it is, left to right
        self.values_tracked = values_tracked

        self.rows_seen = 0
        self.empty_count = 0
        self.null_like_text_count = 0     # the field said "NA", "NULL", ...

        # value -> how many times we saw it. Stops accepting new values once it
        # is full, but keeps counting the ones already in it.
        self.value_counts = {}
        self.stopped_tracking_new_values = False

        self.shortest_value_length = None
        self.longest_value_length = None

        # Numeric measurements. We keep these even if some values are not
        # numbers; not_a_number_count tells you how much to trust them.
        self.number_count = 0
        self.not_a_number_count = 0
        self.smallest_number = None
        self.largest_number = None

    # -- taking in a batch of values ---------------------------------------

    def add_batch(self, values):
        """Fold one batch of this column's values into the running totals."""
        self.rows_seen += len(values)

        is_empty = values == ""
        self.empty_count += int(is_empty.sum())

        filled_in = values[~is_empty]
        if filled_in.empty:
            return

        self.null_like_text_count += int(
            filled_in.str.lower().isin(NULL_LIKE_TEXT).sum()
        )

        self._measure_lengths(filled_in)
        self._count_values(filled_in)
        self._measure_numbers(filled_in)

    def _measure_lengths(self, filled_in):
        """Shortest and longest value, in characters.

        Useful for spotting truncation, or padding that should not be there.
        """
        lengths = filled_in.str.len()
        shortest, longest = int(lengths.min()), int(lengths.max())

        if self.shortest_value_length is None:
            self.shortest_value_length = shortest
            self.longest_value_length = longest
        else:
            self.shortest_value_length = min(self.shortest_value_length, shortest)
            self.longest_value_length = max(self.longest_value_length, longest)

    def _count_values(self, filled_in):
        """Count how often each value appears, up to the tracking ceiling.

        Once the table is full we stop adding new values but keep counting the
        ones already there. That keeps the counts for common values correct,
        which is what matters -- and distinct_count_is_at_least records that we
        stopped, so nobody mistakes a floor for a total.
        """
        for value, count in filled_in.value_counts().items():
            if value in self.value_counts:
                self.value_counts[value] += int(count)
            elif len(self.value_counts) < self.values_tracked:
                self.value_counts[value] = int(count)
            else:
                self.stopped_tracking_new_values = True

    def _measure_numbers(self, filled_in):
        """Measure the values that are numbers, without changing what was read.

        pandas.to_numeric turns anything it cannot read into "not a number", so
        counting those tells us how numeric the column really is.

        The isfinite check is doing more than it looks. to_numeric reads "nan"
        and "inf" as real float values, so without it the literal words would be
        counted as numbers and "inf" would become the largest value in the
        column. Discarding everything non-finite catches both, and needs no
        pattern matching.
        """
        as_numbers = pandas.to_numeric(filled_in, errors="coerce")
        is_real_number = numpy.isfinite(as_numbers)

        numbers = as_numbers[is_real_number]
        self.number_count += len(numbers)
        self.not_a_number_count += len(filled_in) - len(numbers)

        if numbers.empty:
            return

        smallest, largest = float(numbers.min()), float(numbers.max())
        self.smallest_number = (
            smallest if self.smallest_number is None
            else min(self.smallest_number, smallest)
        )
        self.largest_number = (
            largest if self.largest_number is None
            else max(self.largest_number, largest)
        )

    # -- reading the results back out --------------------------------------

    @property
    def filled_in_count(self):
        """Rows where the field was not blank."""
        return self.rows_seen - self.empty_count

    @property
    def distinct_count(self):
        """How many different values we saw.

        A floor, not a total, if stopped_tracking_new_values is set.
        """
        return len(self.value_counts)

    @property
    def is_all_numbers(self):
        """True if every filled-in value was a number."""
        return self.filled_in_count > 0 and self.not_a_number_count == 0

    @property
    def is_all_empty(self):
        """Every row of this column is blank."""
        return self.rows_seen > 0 and self.empty_count == self.rows_seen

    @property
    def single_value_share(self):
        """What share of ALL rows the commonest value covers.

        Measured against every row rather than the filled-in ones, so a column
        that is half blank and half one value scores 0.5, not 1.0. It is not
        holding one value; it is mostly empty.
        """
        if not self.rows_seen or not self.value_counts:
            return 0.0
        return self.most_common(1)[0][1] / self.rows_seen

    @property
    def holds_one_value(self):
        """True when a single value covers all but a small margin of the rows."""
        return self.single_value_share >= SINGLE_VALUE_THRESHOLD

    def most_common(self, how_many):
        """The most frequent values, commonest first.

        Ties are broken by the value itself, so two runs over the same file
        produce the same report. That is why value_counts is a plain dict rather
        than a collections.Counter: Counter.most_common breaks ties by insertion
        order, which depends on what order the batches happened to arrive in.
        """
        return sorted(
            self.value_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )[:how_many]

    def share_covered_by(self, values_and_counts):
        """What share of the filled-in rows these values account for.

        Tells you whether a list of values describes the column or just samples
        a long tail: twenty timestamps covering 0.8% of rows describe nothing.
        """
        if not self.filled_in_count:
            return 0.0
        return sum(count for _, count in values_and_counts) / self.filled_in_count

    # -- saving and restoring ----------------------------------------------

    def state(self):
        """Everything needed to carry on from here, as plain data.

        Used for checkpointing. Note this is the WHOLE accumulator, not a
        summary of it -- restoring and carrying on gives byte-identical results
        to never having stopped, because the value table continues from exactly
        where it was rather than being merged with another one.
        """
        return {
            "rows_seen": self.rows_seen,
            "empty_count": self.empty_count,
            "null_like_text_count": self.null_like_text_count,
            "value_counts": dict(self.value_counts),
            "stopped_tracking_new_values": self.stopped_tracking_new_values,
            "shortest_value_length": self.shortest_value_length,
            "longest_value_length": self.longest_value_length,
            "number_count": self.number_count,
            "not_a_number_count": self.not_a_number_count,
            "smallest_number": self.smallest_number,
            "largest_number": self.largest_number,
        }

    @classmethod
    def from_state(cls, column_name, position, state, values_tracked=DEFAULT_VALUES_TRACKED):
        """Rebuild a summary saved by state()."""
        summary = cls(column_name, position, values_tracked)
        summary.rows_seen = state["rows_seen"]
        summary.empty_count = state["empty_count"]
        summary.null_like_text_count = state["null_like_text_count"]
        summary.value_counts = dict(state["value_counts"])
        summary.stopped_tracking_new_values = state["stopped_tracking_new_values"]
        summary.shortest_value_length = state["shortest_value_length"]
        summary.longest_value_length = state["longest_value_length"]
        summary.number_count = state["number_count"]
        summary.not_a_number_count = state["not_a_number_count"]
        summary.smallest_number = state["smallest_number"]
        summary.largest_number = state["largest_number"]
        return summary

    def as_dict(self, values_shown=DEFAULT_VALUES_SHOWN):
        """A plain dictionary, for writing to JSON."""
        summary = {
            "position": self.position,
            "rows_seen": self.rows_seen,
            "empty_count": self.empty_count,
            "null_like_text_count": self.null_like_text_count,
            "distinct_count": self.distinct_count,
            "distinct_count_is_at_least": self.stopped_tracking_new_values,
            "shortest_value_length": self.shortest_value_length,
            "longest_value_length": self.longest_value_length,
            "most_common_values": [
                {"value": value, "count": count}
                for value, count in self.most_common(values_shown)
            ],
            "number_count": self.number_count,
            "not_a_number_count": self.not_a_number_count,
            "is_all_numbers": self.is_all_numbers,
            "is_all_empty": self.is_all_empty,
            "holds_one_value": self.holds_one_value,
            "single_value_share": round(self.single_value_share, 4),
        }
        if self.number_count:
            summary["smallest_number"] = self.smallest_number
            summary["largest_number"] = self.largest_number
        return summary

