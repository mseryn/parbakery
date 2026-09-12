#!/usr/bin/env python3
"""What the run is costing right now: memory held, and how much CPU is busy.

Uses psutil, which is the usual answer to this question. An earlier version read
/proc directly and came to about seventy lines of parsing, race handling and a
fallback for kernels built without the children file -- all of which psutil
already does, on more platforms, with more care.

CPU is reported as cores busy, not CPU time used. Time used only ever goes up,
so it says nothing about what the run is doing now; cores busy rises when the
workers are reading and falls when they are waiting on disk, which is the thing
worth watching. A rate needs two samples, which is why this is a class rather
than a function.
"""

import os
import time

import psutil

from formatting import describe_bytes

# How often to sample. Slower than the progress bars on purpose: it is context
# rather than progress, and measuring every frame would be work spent watching
# instead of working.
REFRESH_SECONDS = 5.0

TOTAL_CORES = psutil.cpu_count() or 1


def process_tree(root_pid=None):
    """This process and every process descended from it.

    Descendants, not just direct children. The worker pool is started through a
    forkserver, so the processes doing the reading are grandchildren:

        main
         |-- manager
         `-- forkserver
              |-- worker
              ...

    Walking one level found the manager and the forkserver -- two small, idle
    processes -- and missed every worker, reporting 137 MB for a run using 740.
    """
    try:
        root = psutil.Process(root_pid or os.getpid())
        return [root] + root.children(recursive=True)
    except psutil.Error:
        return []


class ResourceMonitor:
    """Memory held and cores busy, across the whole process tree.

    Holds the previous CPU reading so the next one can be turned into a rate.
    """

    def __init__(self, root_pid=None):
        self.root_pid = root_pid

        # pid -> CPU seconds it had used when we last saw it. Kept per process
        # rather than as one total: when a worker exits its time would otherwise
        # vanish from the sum, and the next rate would come out negative.
        self._cpu_by_pid = {}
        self._sampled_at = None

    def sample(self):
        """Memory in use, cores busy, and how many processes are running."""
        processes = process_tree(self.root_pid)
        memory = 0

        for process in processes:
            try:
                memory += process.memory_info().rss
                times = process.cpu_times()
                self._cpu_by_pid[process.pid] = times.user + times.system
            except psutil.Error:
                continue        # it finished while we were looking

        now = time.monotonic()
        cpu_total = sum(self._cpu_by_pid.values())

        cores_busy = None
        if self._sampled_at is not None:
            elapsed = now - self._sampled_at
            if elapsed > 0:
                cores_busy = (cpu_total - self._cpu_total) / elapsed

        self._sampled_at = now
        self._cpu_total = cpu_total

        return {
            "processes": len(processes),
            "memory_bytes": memory,
            "cores_busy": cores_busy,       # None until there are two samples
        }

    def line(self, workers=None):
        """One line: workers running, memory held, cores busy.

        The worker count is passed in rather than counted from the tree. The run
        already knows how many it started, and the tree also holds a forkserver
        and a manager that are not workers.
        """
        reading = self.sample()

        workers_text = (f"{workers} worker(s)" if workers is not None
                        else f"{reading['processes']} process(es)")

        busy = reading["cores_busy"]
        cpu_text = "  ?  " if busy is None else f"{busy:.1f}"

        return (f"  {workers_text}"
                f"  |  {describe_bytes(reading['memory_bytes'])} in use"
                f"  |  {cpu_text} of {TOTAL_CORES} cores busy")
