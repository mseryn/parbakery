#!/usr/bin/env python3
"""Copy a file to local disk before reading it, for filesystems slow to open.

On a parallel filesystem like Lustre, reading a file costs far more in round
trips than in bandwidth. Every open is a request to a metadata server the whole
machine shares, and a run that touches a thousand files spends most of its life
waiting rather than reading. Copying a file over in one go and reading it from
local disk turns many small waits into one large sequential transfer, which is
what such a filesystem is actually good at.

This is not free and it is not always right -- it reads every byte of every
file, so it only pays when the file was going to be read through anyway, and on
a local filesystem it is pure loss. It is off unless asked for.

Copying happens inside each worker, not in the parent: a worker copies its file,
reads it, deletes the copy, and only then takes the next one. That holds the
cost to what was asked for -- no more copies on local disk than there are
workers, and no more readers on the shared filesystem than there are workers --
without making any worker wait for the others. A worker that finishes early
starts copying its next file immediately rather than idling until its batch is
done.
"""

import shutil
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp

from settings import STAGING_PREFIX


class StagingArea:
    """The local directory the copies go in.

    Made by the parent and its path handed to the workers, so that one process
    is responsible for removing it. A worker killed mid-copy cannot clean up
    after itself; the parent removing the whole directory at the end covers it.

    Used as a context manager, so the directory goes even if the run is
    interrupted:

        with StagingArea() as staging:
            ...                     # hand staging.root to the workers
    """

    def __init__(self, parent_directory=None):
        # parent_directory=None lets mkdtemp pick, which honours $TMPDIR. On a
        # compute node that usually points at node-local storage, which is the
        # whole point of copying.
        self.parent_directory = parent_directory
        self.root = None

    def __enter__(self):
        if self.parent_directory is not None:
            Path(self.parent_directory).mkdir(parents=True, exist_ok=True)
        self.root = Path(mkdtemp(prefix=STAGING_PREFIX, dir=self.parent_directory))
        return self

    def __exit__(self, *_unused):
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)
            self.root = None
        return False                    # never swallow an exception


@contextmanager
def staged_copy(root, original):
    """Copy one file into `root`, yield where it landed, then delete it.

    Yields (path to read, problem) -- the problem is None when the copy worked.
    A file that cannot be copied is yielded as itself, so it still gets read,
    just over the slow filesystem. Losing a copy should cost time, not results.

    The copy keeps the file's own name, in a directory of its own. Names are
    load-bearing here, since reports and checkpoints are named after the file,
    and two files in different source directories can share one.
    """
    original = Path(original)
    holding = Path(mkdtemp(dir=root))
    try:
        destination = holding / original.name
        try:
            # copy2 keeps size and modification time, which is what the
            # checkpoint fingerprint is made of -- so a run that resumes from a
            # checkpoint still recognises its file after being copied.
            shutil.copy2(original, destination)
        except Exception as problem:
            yield original, problem
        else:
            yield destination, None
    finally:
        # The copy goes as soon as it has been read, so this worker's next file
        # is the only one it has on local disk.
        shutil.rmtree(holding, ignore_errors=True)
