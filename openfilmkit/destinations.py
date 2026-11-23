"""Where finished pictures go when they leave a roll: clean copies in a folder the person picks, often a USB
stick or a memory card, and ejecting that drive afterwards.

A roll keeps its own pictures in derived/ (frame-03-v2.jpg beside its recipe); an export copies the chosen
ones out under plain names, "Roll 2026-11-23 03.jpg", with nothing else beside them.
"""
import hashlib
import os
import plistlib
import shutil
import string
import subprocess
import sys
from pathlib import Path

from . import files

SPARE = 50 * 2**20   # bytes left free on the drive after a copy, so it isn't filled to the last byte


class Problem(Exception):
    pass


def _drive(path, name, removable):
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return None
    return dict(path=str(path), name=name, removable=removable, free=free)


def _hidden_mounts():
    """Mount points macOS keeps out of Finder ('nobrowse': system and helper volumes)."""
    try:
        said = subprocess.run(['mount'], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {line.split(' on ', 1)[1].rsplit(' (', 1)[0] for line in said.splitlines()
            if ' on ' in line and 'nobrowse' in line.rsplit(' (', 1)[-1]}


def _mac_volumes():
    out, hidden = [], _hidden_mounts()
    for v in sorted(Path('/Volumes').iterdir()) if Path('/Volumes').is_dir() else []:
        try:
            if v.resolve() == Path('/') or v.name.startswith(('.', 'com.apple.')) or not os.path.ismount(v) or str(v) in hidden:
                continue   # the startup disk's own entry, the system's snapshots, and plain folders left in /Volumes
            info = plistlib.loads(subprocess.run(['diskutil', 'info', '-plist', str(v)], capture_output=True,
                                                 timeout=10).stdout or b'<plist><dict/></plist>')
        except (OSError, subprocess.TimeoutExpired, plistlib.InvalidFileException, ValueError):
            info = {}
        if info.get('WritableVolume') is False:
            continue
        removable = bool(info.get('Ejectable') or info.get('RemovableMedia') or info.get('Removable')
                         or info.get('Internal') is False)
        d = _drive(v, info.get('VolumeName') or v.name, removable)
        if d:
            out.append(d)
    return out


def _linux_volumes():
    user = os.environ.get('USER') or ''
    out = []
    for base in (Path('/media') / user, Path('/run/media') / user):
        if base.is_dir():
            for v in sorted(base.iterdir()):
                d = v.is_dir() and _drive(v, v.name, True)
                if d:
                    out.append(d)
    return out


def _windows_volumes():
    import ctypes
    out = []
    for letter in string.ascii_uppercase:
        root = f'{letter}:\\'
        if ctypes.windll.kernel32.GetDriveTypeW(root) == 2:   # DRIVE_REMOVABLE
            d = _drive(root, f'{letter}:', True)
            if d:
                out.append(d)
    return out


def places(also=()):
    """Where an export could go: folders from `also` (one chosen before, say) that are still there, then the
    person's Pictures folder, then each drive that's plugged in ({path, name, removable, free}), removable
    ones (sticks, cards) first. A folder in `also` that has gone (its drive was ejected) is left out."""
    pictures = Path.home() / 'Pictures'
    out = [d for d in [pictures.is_dir() and _drive(pictures, 'Pictures', False)] if d]
    try:
        drives = (_mac_volumes() if sys.platform == 'darwin' else
                  _windows_volumes() if os.name == 'nt' else _linux_volumes())
    except Exception:   # a list of drives is a convenience: never stop an export for it
        drives = []
    drives.sort(key=lambda d: not d['removable'])
    listed = {Path(d['path']).resolve() for d in out + drives}
    extra = []
    for folder in also or ():
        try:
            here = Path(folder).resolve()
        except (OSError, ValueError):
            continue
        if here in listed or not here.is_dir():
            continue
        on = next((d for d in drives if Path(d['path']).resolve() in here.parents), None)
        d = _drive(here, here.name, bool(on and on['removable']))
        if d:
            extra.append(d)
            listed.add(here)
    return extra + out + drives


def plain_name(roll_name, master, suffix):
    """'Roll 2026-11-23 03.jpg' for masters/frame-03.tif; the scan's own name where it has no number."""
    stem = Path(master).stem
    number = stem.rpartition('-')[2]
    return f'{roll_name} {number}{suffix}' if number.isdigit() else f'{roll_name} {stem}{suffix}'


def _digest(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def copy_out(picture, folder, name):
    """Copy `picture` into `folder` as `name`, never over another file: the same picture already there is
    left as it is (exporting twice copies nothing new), a different one keeps its name and this one is
    "name (2)". Returns the path it is at."""
    picture, folder = Path(picture), Path(folder)
    if not folder.is_dir():
        raise Problem(f'{folder} isn’t there any more. If it was on a drive, plug the drive back in, '
                      'or choose somewhere else.')
    size = picture.stat().st_size
    stem, suffix = Path(name).stem, Path(name).suffix
    n, mine = 1, None
    while True:
        dest = folder / (name if n == 1 else f'{stem} ({n}){suffix}')
        if not dest.exists():
            break
        if dest.stat().st_size == size and _digest(dest) == (mine := mine or _digest(picture)):
            return dest
        n += 1
    if shutil.disk_usage(folder).free < size + SPARE:
        raise Problem(f'There isn’t room in {folder.name} for {name}. Free some space or choose somewhere else.')
    tmp = folder / f'.{dest.name}.partial'
    try:
        shutil.copyfile(picture, tmp)
        files.place(tmp, dest)
    except FileExistsError:   # something took the name meanwhile: try the next one
        tmp.unlink(missing_ok=True)
        return copy_out(picture, folder, name)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def eject(path):
    """Eject the drive holding `path`, so it can be pulled out safely."""
    path = str(path)
    if sys.platform == 'darwin':
        cmd = ['diskutil', 'eject', path]
    elif os.name == 'nt':
        raise Problem('Use “Safely Remove Hardware” in the taskbar to eject this drive.')
    else:
        cmd = ['gio', 'mount', '--eject', path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Problem(f'Couldn’t eject the drive: {e}') from e
    if p.returncode:
        said = (p.stderr or p.stdout).strip().splitlines()
        raise Problem('Couldn’t eject the drive. Close anything that has files open on it, then try again.'
                      + (f' ({said[-1]})' if said else ''))
    return dict(ejected=True)
