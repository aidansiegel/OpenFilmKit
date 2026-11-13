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


def unmounted(path):
    """Where `path`'s drive should be mounted, if nothing is mounted there; else None.

    A path under /Volumes/NAME (macOS), /mnt/NAME, /media/NAME or /media/USER/NAME, or
    /run/media/USER/NAME is taken to be on a drive mounted there. While it isn't, a file written
    to that path lands on whatever disk is under it: the internal one, if the folder is still
    there (on Linux it often is). Anywhere else, None."""
    parts = Path(os.path.realpath(path)).parts   # by where links lead (~/TRAVEL to /Volumes/TRAVEL, say)
    if parts[1:2] in (('Volumes',), ('mnt',)) and len(parts) > 2:
        places = [parts[:3]]
    elif parts[1:2] == ('media',) and len(parts) > 2:
        places = [parts[:3], parts[:4]] if len(parts) > 3 else [parts[:3]]
    elif parts[1:3] == ('run', 'media') and len(parts) > 4:
        places = [parts[:5]]
    else:
        return None
    if any(os.path.ismount(Path(*p)) for p in places):
        return None
    return Path(*places[-1])


def mine(tmp, dest):
    """Whether `dest` is the finished file `tmp` (both names left on it by a stopped place())."""
    try:
        return os.path.samefile(tmp, dest)
    except OSError:
        return False
