"""Roll folders: roll.json plus masters/frame-NN.tif and their records.

roll-07/
  roll.json
  masters/frame-01.tif  frame-01.tif.json  frame-01.tif.scan.log
"""
import datetime
import json
import re
from pathlib import Path

FILM_TYPES = ('color-negative', 'bw-negative', 'slide', 'unknown')
FRAME = re.compile(r'^frame-(\d+)\.tif', re.I)   # FRAME-04.TIF takes its number too: on most drives it is the same name


class RollError(Exception):
    pass


def is_roll(path):
    return (Path(path) / 'roll.json').is_file()


def create(path, film='unknown', stock='', year=None, note=''):
    path = Path(path).expanduser().absolute()
    if film not in FILM_TYPES:
        raise RollError(f'film must be one of {FILM_TYPES}')
    if (path / 'roll.json').exists():
        raise RollError(f'{path} is already a roll')
    (path / 'masters').mkdir(parents=True, exist_ok=True)
    record = {'format': 1, 'name': path.name, 'film': film, 'stock': stock, 'year': year,
              'note': note,
              'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    with (path / 'roll.json').open('x') as f:
        json.dump(record, f, indent=2)
        f.write('\n')
    return record


def load(path):
    file = Path(path) / 'roll.json'
    try:
        record = json.loads(file.read_text())
    except ValueError as e:
        raise RollError(f'{file} is damaged ({e})') from e
    if not isinstance(record, dict):
        raise RollError(f"{file} is damaged (it doesn't hold a roll's record)")
    return record


def set_choice(path, choice):
    """Store a film choice (films.stocks.roll_choice) in roll.json, keeping the old file."""
    path = Path(path)
    current = path / 'roll.json'
    record = load(path)
    history = path / 'settings-history'
    history.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    (history / f'roll-{stamp}.json').write_text(current.read_text())
    record['film_choice'] = choice
    tmp = current.with_suffix('.json.new')
    tmp.write_text(json.dumps(record, indent=2) + '\n')
    tmp.replace(current)
    return record


def containing(path):
    """The roll folder holding a master at roll/masters/frame.tif, or None."""
    path = Path(path).resolve()
    if path.parent.name == 'masters' and is_roll(path.parent.parent):
        return path.parent.parent
    return None


def next_derived(path, stem, suffix='.jpg'):
    """roll/derived/<stem>-vN<suffix>, the first N not yet used."""
    derived = Path(path) / 'derived'
    n = 1
    while any((derived / f'{stem}-v{n}{suffix}{extra}').exists() for extra in ('', '.json')):
        n += 1
    return derived / f'{stem}-v{n}{suffix}'


ATTEMPTS = 'attempts'   # masters/attempts/: failed scans set aside, so that the scan done again takes their number


def set_aside_failed(path):
    """Move the files of failed scans at the end of the roll into masters/attempts/, so the next scan
    reuses their frame number instead of leaving a gap. Only numbers above the last real master, and only
    where the record says the scan failed or was stopped and no image took the name: nothing is renumbered,
    nothing in progress is touched, and what a failed attempt left (record, log, partial file) is kept.
    Returns the paths it moved to."""
    masters = Path(path) / 'masters'
    if not masters.is_dir():
        return []
    by_number = {}
    for p in masters.iterdir():
        if (m := FRAME.match(p.name)):
            by_number.setdefault(int(m.group(1)), []).append(p)
    images = [n for n, ps in by_number.items() if any(p.name.lower().endswith(('.tif', '.tiff')) for p in ps)]
    last = max(images, default=0)
    moved = []
    for n in sorted(k for k in by_number if k > last):
        ps = by_number[n]
        records = [p for p in ps if p.name.lower().endswith('.tif.json')]
        try:
            failed = len(records) == 1 and json.loads(records[0].read_text()).get('status') == 'failed_or_interrupted'
        except (OSError, ValueError, AttributeError):
            failed = False
        if not failed:
            break   # a scan in progress, or something the kit didn't write: leave it, and all after it, alone
        stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')
        (masters / ATTEMPTS).mkdir(exist_ok=True)
        for p in ps:   # frame-03.tif.json -> attempts/frame-03-<when>.tif.json
            stem = p.name[:p.name.lower().index('.tif')]
            dest = masters / ATTEMPTS / f'{stem}-{stamp}{p.name[len(stem):]}'
            p.rename(dest)
            moved.append(dest)
    return moved


def next_frame(path):
    """The next unused masters/frame-NN.tif, counting failed attempts too (see set_aside_failed)."""
    masters = Path(path) / 'masters'
    numbers = [int(m.group(1)) for p in masters.glob('*')
               if (m := FRAME.match(p.name))]
    n = max(numbers, default=0) + 1
    return masters / f'frame-{n:02d}.tif'
