#!/usr/bin/env python3
"""What the run is costing right now: workers, CPU, memory.

Read from /proc, because that is where Linux keeps it and it needs no
dependency. A run that gets killed for using too much memory should have been
showing you the memory beforehand.

Everything here degrades quietly. If /proc is not there, or a worker exits
between listing it and reading it, the line just says less rather than taking
the run down with it.
"""

import os
from pathlib import Path

CLOCK_TICKS_PER_SECOND = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

# How often the resource line is refreshed. Slower than the progress bars on
# purpose: it is context, not progress, and reading /proc for every worker on
# every frame would be work spent measuring instead of working.
REFRESH_SECONDS = 5.0


def _read_proc(pid, filename):
    try:
        return Path(f"/proc/{pid}/{filename}").read_text()
    except (OSError, ValueError):
        return None             # the process finished; not an error


def _direct_children(pid):
    """The pids whose parent is `pid`."""
    listed = _read_proc(pid, f"task/{pid}/children")
    if listed is not None:
        return [int(child) for child in listed.split()]

    # Some kernels are built without the children file. Scanning /proc for
    # matching parents is slower but always available.
    children = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        stat = _read_proc(int(entry), "stat")
        if not stat:
            continue
        try:
            parent = int(stat[stat.rindex(")") + 2:].split()[1])
        except (ValueError, IndexError):
            continue
        if parent == pid:
            children.append(int(entry))
    return children


def process_tree(root_pid=None, depth_limit=8):
    """The run's own pid and every process descended from it.

    Descendants, not just direct children. The worker pool is started through a
    forkserver, so the processes doing the reading are GRANDchildren:

        main
         |-- manager
         `-- forkserver
              |-- worker
              |-- worker
              ...

    Walking one level down found the manager and the forkserver -- two small,
    idle processes -- and missed every worker. On a four-worker run that
    reported 137 MB while the run was actually using 740 MB, which is worse than
    reporting nothing when the number exists to warn about memory.
    """
    root_pid = root_pid or os.getpid()
    seen = {root_pid}
    frontier = [(root_pid, 0)]

    while frontier:
        pid, depth = frontier.pop()
        if depth >= depth_limit:
            continue
        for child in _direct_children(pid):
            if child not in seen:       # a guard against a cycle we should never see
                seen.add(child)
                frontier.append((child, depth + 1))

    return sorted(seen)


def memory_bytes(pid):
    """Resident memory for one process, or None if it has gone."""
    status = _read_proc(pid, "status")
    if not status:
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return None


def cpu_seconds(pid):
    """CPU time used by one process, user plus system."""
    stat = _read_proc(pid, "stat")
    if not stat:
        return None
    # The command name can contain spaces and brackets, so fields are counted
    # from after the closing bracket rather than by splitting the whole line.
    try:
        after_name = stat[stat.rindex(")") + 2:].split()
        utime, stime = int(after_name[11]), int(after_name[12])
    except (ValueError, IndexError):
        return None
    return (utime + stime) / CLOCK_TICKS_PER_SECOND


def snapshot(root_pid=None):
    """Processes running, memory in use, CPU used so far, across the whole tree."""
    pids = process_tree(root_pid)
    memory = [memory_bytes(pid) for pid in pids]
    cpu = [cpu_seconds(pid) for pid in pids]
    return {
        "processes": len(pids),
        "memory_bytes": sum(m for m in memory if m),
        "cpu_seconds": sum(c for c in cpu if c),
    }


def describe_bytes(count):
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024


def describe_seconds(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def resource_line(reading=None, workers=None):
    """One line: workers running, memory in use, CPU used so far.

    The worker count is passed in rather than guessed from the process tree.
    The run already knows how many it started, and the tree also holds a
    forkserver and a manager that are not workers and should not be counted as
    though they were.
    """
    reading = reading or snapshot()
    workers_text = (f"{workers} worker(s)" if workers is not None
                    else f"{reading['processes']} process(es)")
    return (f"  {workers_text}"
            f"  |  {describe_bytes(reading['memory_bytes'])} in use"
            f"  |  {describe_seconds(reading['cpu_seconds'])} CPU used")
