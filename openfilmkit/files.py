"""Giving a finished file its name without ever replacing another.

A check that a name is free and a rename after it leave an instant between them: a file put there
in that instant (an archival master, say) would be replaced without a word, since a rename on
macOS and Linux replaces whatever has the name. place() takes the name in one step instead.
"""
import os
from pathlib import Path


def place(tmp, dest):
    """Give the finished file `tmp` the name `dest`; FileExistsError, with nothing changed, if a
    file has that name. By a hard link, which can't be made over a file, then `tmp` taken away.
    Where the file system has no links (exFAT, FAT), the name is first claimed by creating it
    empty (which also can't be done over a file) and `tmp` renamed onto that claim, so the only
    file ever replaced is this one's own empty one; if that rename fails, the claim goes again.

    Stopped between the link and taking `tmp` away, both names are left on the finished file
    (os.path.samefile tells): callers that clear up after a stopped run look for that.
    """
    tmp, dest = Path(tmp), Path(dest)
    try:
        os.link(tmp, dest)
    except FileExistsError:
        raise
    except OSError:          # no links here: claimed, then taken
        claim = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # FileExistsError: someone's, left be
        try:
            os.close(claim)
            os.replace(tmp, dest)
        except BaseException:
            try:
                if dest.stat().st_size == 0:   # still the empty claim, this run's
                    dest.unlink()
            except OSError:
                pass
            raise
        return
    tmp.unlink()


def mine(tmp, dest):
    """Whether `dest` is the finished file `tmp` (both names left on it by a stopped place())."""
    try:
        return os.path.samefile(tmp, dest)
    except OSError:
        return False
