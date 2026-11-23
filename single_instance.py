"""One process per lock file — a bot, not a chorus.

The sandbox start command runs again on every resume, and it does not ask
whether the previous tree is still alive. On 2026-11-22 that left five
`start.sh` trees and TWO `bot.py` processes on one token in one box. Discord
delivers every event to every session, so both answered: each join-gate
decision was logged twice, with different confidences, and the server got two
differently-worded replies to the same message, a second apart.

`flock` is the right primitive here rather than a pidfile: the kernel drops the
lock when the holder dies, so a crashed bot can never leave a lock that a live
one has to fight. Keep the returned handle open for the life of the process.
"""
from __future__ import annotations

import fcntl
import os


class AlreadyRunning(RuntimeError):
    """Another live process holds the lock."""

    def __init__(self, path: str, pid: str | None = None):
        self.path = path
        self.pid = pid
        where = f" (pid {pid})" if pid else ""
        super().__init__(f"another instance holds {path}{where}")


def claim(path: str):
    """Take an exclusive lock. Returns the open handle — keep it referenced."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder = (fh.read() or "").strip() or None
        fh.close()
        raise AlreadyRunning(path, holder)
    # record who holds it, so the loser can name the winner in its log
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def holder(path: str) -> str | None:
    """The pid recorded in the lock file, if any (diagnostics only)."""
    try:
        with open(path) as f:
            return (f.read() or "").strip() or None
    except OSError:
        return None
