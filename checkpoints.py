#!/usr/bin/env python3
"""Saving progress so a killed run can carry on rather than start again.

Reading a large file takes a long time, and long-running processes get killed --
by the out-of-memory killer, by a scheduler, by someone closing a laptop. This
writes the accumulator state out every so often so the next run picks up where
the last one stopped.

## Why this is safe when splitting a file across workers is not

Splitting makes two independently-capped value tables that have to be merged,
and which values survive depends on where the split fell -- so the number of
workers would change the measurements.

Checkpointing serialises THE ONE accumulator and carries on adding to it. The
capped table sees exactly the same values in exactly the same order as a read
that was never interrupted. Verified: killed and resumed at three different
points on a two-million-row file, every column identical to a straight read,
including the seven columns that hit the cap.

## What makes a checkpoint usable

All of these, or it is ignored and the file is read from the start:

  - the format version matches
  - the source file's size and modification time are unchanged
  - the settings that affect measurements are unchanged

Size and modification time rather than a hash of the contents: hashing a 500 GB
file to decide whether a resume is safe would cost more than the resume saves.
That is a deliberate trade, and it means a file edited without changing its size
or timestamp would resume wrongly. Nothing realistic does that.

Batch size is deliberately NOT part of that fingerprint. Batch size cannot
change a measurement -- there is a test asserting exactly that -- so resuming
with a different one is fine, which matters because lowering it is the usual
response to being killed.
"""

import json
import os
import time
from pathlib import Path

from measuring import ColumnSummary
from settings import CHECKPOINT_EVERY_ROWS

# Bumped whenever the saved shape changes, so an old checkpoint is ignored
# rather than misread.
CHECKPOINT_FORMAT = 1


def fingerprint_source(path):
    """What we compare to decide whether a checkpoint still matches its file."""
    stat = Path(path).stat()
    return {
        "path": str(Path(path).resolve()),
        "size_in_bytes": stat.st_size,
        "modified_at": int(stat.st_mtime),
    }


def fingerprint_settings(separator, values_tracked, preview_rows):
    """The settings that would change what gets measured.

    Batch size is not here on purpose -- see the note at the top of the module.
    """
    return {
        "separator": separator,
        "values_tracked": values_tracked,
        "preview_rows": preview_rows,
    }


class CheckpointStore:
    """Where checkpoints live, and the rules about using them.

    One file per dataset, named after it, in a directory of their own so the
    whole lot can be removed at the end of a successful run.
    """

    def __init__(self, directory, enabled=True, every_rows=CHECKPOINT_EVERY_ROWS):
        self.directory = Path(directory) if directory else None
        self.enabled = bool(enabled and self.directory)
        self.every_rows = every_rows

    # -- where things go ---------------------------------------------------

    def path_for(self, dataset_stem):
        return self.directory / f"{dataset_stem}.checkpoint.json"

    def is_due(self, rows_done, rows_at_last_save):
        """Has enough been read since the last save to be worth writing again?"""
        return self.enabled and rows_done - rows_at_last_save >= self.every_rows

    # -- writing -----------------------------------------------------------

    def save(self, dataset_stem, source_path, settings, rows_done,
             summaries, identifier_check=None):
        """Write a checkpoint, atomically.

        Atomically because a checkpoint half-written when the process died is
        worse than none at all: the next run would read it and trust it.
        """
        if not self.enabled:
            return None

        document = {
            "parbake_checkpoint": CHECKPOINT_FORMAT,
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": fingerprint_source(source_path),
            "settings": settings,
            "rows_done": rows_done,
            "columns": {name: summary.state() for name, summary in summaries.items()},
            "identifiers": identifier_check.state() if identifier_check else None,
        }

        self.directory.mkdir(parents=True, exist_ok=True)
        final = self.path_for(dataset_stem)
        partial = final.with_suffix(".json.writing")
        partial.write_text(json.dumps(document), encoding="utf-8")
        os.replace(partial, final)          # atomic on every platform we care about
        return final

    # -- reading -----------------------------------------------------------

    def load(self, dataset_stem, source_path, settings):
        """A usable checkpoint for this dataset, or (None, reason).

        The reason is returned rather than logged so the caller can decide
        whether it is worth telling anyone. An absent checkpoint is the normal
        case and not worth a line of output; a rejected one usually is.
        """
        if not self.enabled:
            return None, "checkpointing is off"

        path = self.path_for(dataset_stem)
        if not path.is_file():
            return None, "no checkpoint"

        try:
            document = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as problem:
            return None, f"checkpoint unreadable ({type(problem).__name__})"

        if document.get("parbake_checkpoint") != CHECKPOINT_FORMAT:
            return None, "checkpoint written by a different version"

        try:
            current = fingerprint_source(source_path)
        except OSError:
            return None, "source file is gone"

        if document.get("source") != current:
            return None, "the data file has changed since the checkpoint"
        if document.get("settings") != settings:
            return None, "settings have changed since the checkpoint"
        if not document.get("rows_done"):
            return None, "checkpoint has no progress in it"

        return document, f"resuming after {document['rows_done']:,} rows"

    @staticmethod
    def rebuild(document, column_names, values_tracked):
        """Turn a loaded checkpoint back into accumulators.

        Returns (rows_done, summaries, identifier_state). A column that is in
        the file but not the checkpoint starts fresh, which should not happen --
        the settings fingerprint would have rejected it first -- but costs
        nothing to allow for.
        """
        saved = document.get("columns", {})
        summaries = {}
        for position, name in enumerate(column_names):
            if name in saved:
                summaries[name] = ColumnSummary.from_state(
                    name, position, saved[name], values_tracked)
            else:
                summaries[name] = ColumnSummary(name, position, values_tracked)
        return document["rows_done"], summaries, document.get("identifiers")

    # -- tidying up --------------------------------------------------------

    def discard(self, dataset_stem):
        """Remove one dataset's checkpoint, once it has finished."""
        if not self.enabled:
            return
        self.path_for(dataset_stem).unlink(missing_ok=True)
        self.path_for(dataset_stem).with_suffix(".json.writing").unlink(missing_ok=True)

    def clear(self):
        """Remove every checkpoint after a run that finished, then the directory.

        Only files this module writes are removed. --checkpoint-dir can point
        anywhere, including somewhere shared, and deleting a stranger's files
        because they happened to be in the way is not acceptable behaviour for
        a tool that is supposed to be tidying up after itself.

        Returns True if the directory itself went. It will not if anything else
        is in there, which is the correct outcome rather than a failure.
        """
        if not self.directory or not self.directory.is_dir():
            return False

        for entry in sorted(self.directory.iterdir()):
            if entry.is_file() and entry.name.endswith(
                (".checkpoint.json", ".checkpoint.json.writing")
            ):
                entry.unlink()

        try:
            self.directory.rmdir()          # only succeeds if it is now empty
        except OSError:
            return False                     # something else lives here
        return True

    def outstanding(self):
        """Checkpoints currently on disk, for reporting what a run would resume."""
        if not self.directory or not self.directory.is_dir():
            return []
        return sorted(self.directory.glob("*.checkpoint.json"))
