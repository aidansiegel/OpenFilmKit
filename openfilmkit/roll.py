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
FRAME = re.compile(r'^frame-(\d+)\.tif')


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
    return json.loads((Path(path) / 'roll.json').read_text())


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


def next_frame(path):
    """The next unused masters/frame-NN.tif, counting failed attempts too."""
    masters = Path(path) / 'masters'
    numbers = [int(m.group(1)) for p in masters.glob('frame-*')
               if (m := FRAME.match(p.name))]
    n = max(numbers, default=0) + 1
    return masters / f'frame-{n:02d}.tif'
