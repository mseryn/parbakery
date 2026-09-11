#!/usr/bin/env python3
"""Looking for data that has not been anonymised.

Rides along on the normal read, so it costs no extra pass over the file."""

from settings import COLUMN_NAMES_TO_WATCH, PATH_PROBE_VALUES


class IdentifierCheck:
    """Looks for data that has not been anonymised.

    Runs inside the normal read, so it costs no extra pass over the file.

    The idea needs nothing but the data itself: an ALCF username always contains
    letters, while an anonymised id is a hash reduced to an integer, so it is all
    digits. A column called "username" whose values are all digits has been
    anonymised. The same column with letters in it has not.

    Two kinds of column get looked at:

      named      the column's NAME suggests it holds something identifying
      paths      the values look like file paths, which can carry a username
                 inside them even when the column name gives nothing away

    This cannot tell you a file is safe to share. It tells you where to look.
    """

    def __init__(self):
        self.shapes = {}      # column -> counts, see _record_shape

    # -- deciding which columns deserve attention --------------------------

    @staticmethod
    def name_suggests_identifier(column_name):
        """Does the column's NAME suggest it holds something identifying?"""
        lowered = column_name.lower()
        return [word for word in COLUMN_NAMES_TO_WATCH if word in lowered]

    @staticmethod
    def values_look_like_paths(values):
        """Do these values look like file paths?

        A path is where a username hides without the column being called
        anything suspicious -- "/home/chulwoo/run.py". Checked with startswith
        rather than a pattern, because that is all "looks like a path" means
        here.

        Only the first PATH_PROBE_VALUES values are looked at. This question is
        asked about every column of every batch, and scanning entire columns to
        answer it cost more than every other part of this check put together --
        0.30s against 0.015s on a 66-column file. A column of paths shows paths
        immediately; one that does not is not a column of paths. The full share
        is still counted exactly, once a column is being tracked.
        """
        return bool(values.head(PATH_PROBE_VALUES).str.startswith("/").any())

    # -- taking in a batch --------------------------------------------------

    def check_batch(self, column_name, values):
        """Look at one column of one batch."""
        filled_in = values[values != ""]
        if filled_in.empty:
            return

        watched_by_name = bool(self.name_suggests_identifier(column_name))
        looks_like_paths = self.values_look_like_paths(filled_in)

        # Skipping every other column is most of why this is cheap.
        if watched_by_name or looks_like_paths:
            self._record_shape(column_name, filled_in, watched_by_name)

    def _record_shape(self, column_name, filled_in, watched_by_name):
        """Count how many values are all digits, and how many look like paths.

        all-digits is the anonymisation signal: an ALCF username cannot be all
        digits, so a username column that is 100% digits has been through an
        anonymiser, and one that is not, has not.
        """
        shape = self.shapes.setdefault(column_name, {
            "values_seen": 0,
            "all_digit_values": 0,
            "path_like_values": 0,
            "watched_by_name": watched_by_name,
            "matched_words": self.name_suggests_identifier(column_name),
            "example": filled_in.iloc[0],
        })
        shape["values_seen"] += len(filled_in)
        shape["all_digit_values"] += int(filled_in.str.isdigit().sum())
        shape["path_like_values"] += int(filled_in.str.startswith("/").sum())

    # -- reading the results back out --------------------------------------

    def state(self):
        """The shape counts gathered so far, as plain data.

        Has to be checkpointed alongside the column summaries, or a resumed run
        reports the wrong anonymisation verdicts for the rows it skipped.
        """
        return {name: dict(shape) for name, shape in self.shapes.items()}

    def restore(self, state):
        """Carry on from counts saved by state()."""
        self.shapes = {name: dict(shape) for name, shape in state.items()}

    def columns_of_concern(self):
        """Columns worth a person's attention, with why.

        verdict describes the shape; it is not a judgement:
          all digits          consistent with an anonymised id
          contains letters    NOT consistent with an anonymised id
          holds paths         a path can carry a username inside it
        """
        results = []
        for column_name, shape in sorted(self.shapes.items()):
            seen = shape["values_seen"]
            digit_share = shape["all_digit_values"] / seen if seen else 0
            path_share = shape["path_like_values"] / seen if seen else 0

            if path_share > 0:
                verdict = "holds paths -- a path can carry a username inside it"
            elif digit_share == 1.0:
                verdict = "all digits -- consistent with an anonymised id"
            else:
                verdict = "contains letters -- NOT consistent with an anonymised id"

            results.append({
                "column_name": column_name,
                "watched_by_name": shape["watched_by_name"],
                "matched_words": shape["matched_words"],
                "all_digit_share": round(digit_share, 4),
                "path_like_share": round(path_share, 4),
                "verdict": verdict,
                "example": shape["example"],
            })
        return results


# Files at or below this size are counted exactly rather than estimated:
# reading 8 MB to count newlines takes a few hundredths of a second, and
