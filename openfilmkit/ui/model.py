"""What the terminal UI shows, worked out from the files: rolls, their frames, what each frame has
(a master, conversions, cleaned copies) and what it still needs, and a recipe in plain words.

Nothing here draws anything or needs Textual, so it can be tested on its own.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import roll

IMAGES = ('.jpg', '.jpeg', '.png', '.tif', '.tiff')
CLEANED = re.compile(r'^(?P<source>.+)-clean-v(?P<n>\d+)$')
CONVERTED = re.compile(r'^(?P<frame>.+)-v(?P<n>\d+)$')


@dataclass
class Item:
    """One photo to work on: a conversion (or, outside a roll, any photo), with its master and its
    cleaned copies."""
    name: str                       # what it's called in the list: frame-01, or the file's stem
    source: Path | None = None      # the conversion (or the photo) that gets cleaned
    master: Path | None = None      # the scan master, in a roll
    cleaned: list = field(default_factory=list)   # cleaned copies of source, oldest first

    @property
    def latest(self):
        return self.cleaned[-1] if self.cleaned else None

    @property
    def state(self):
        if self.source is None:
            return 'to convert'
        return 'cleaned' if self.cleaned else 'to clean'


def _images(folder):
    return sorted(p for p in Path(folder).iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGES and not p.name.startswith('.'))


def places(folder):
    """The places to show for `folder`: [(label, path)]. A roll is one place; a folder of rolls is
    one place per roll; any other folder is itself a place (its photos)."""
    folder = Path(folder).expanduser().resolve()
    if roll.is_roll(folder):
        return [(folder.name, folder)]
    rolls = sorted(p for p in folder.iterdir() if p.is_dir() and roll.is_roll(p)) if folder.is_dir() else []
    out = [(p.name, p) for p in rolls]
    if not rolls or _images(folder):
        out.insert(0, (folder.name or str(folder), folder))
    return out


def items(place):
    """The photos of a place, each with its master and cleaned copies."""
    place = Path(place)
    if roll.is_roll(place):
        return _roll_items(place)
    found, by_stem = [], {}
    for p in _images(place):
        m = CLEANED.match(p.stem)
        if m:
            continue
        item = Item(name=p.stem, source=p)
        found.append(item)
        by_stem[p.stem] = item
    for p in _images(place):
        m = CLEANED.match(p.stem)
        if m and m['source'] in by_stem:
            by_stem[m['source']].cleaned.append(p)
    for item in found:
        item.cleaned.sort(key=lambda p: int(CLEANED.match(p.stem)['n']))
    return found


def _version(stem, pattern):
    m = pattern.match(stem)
    return int(m['n']) if m else 0


def _frame_of(stem):
    m = CONVERTED.match(stem)
    return m['frame'] if m else stem


def _roll_items(path):
    """A roll's frames: one item per conversion (a frame converted twice, or to a JPEG and a TIFF,
    has one for each), or the bare master of a frame not converted yet. A cleaned copy belongs to
    the conversion it was made from: same name before -clean-vN, same kind of file."""
    masters = {p.stem: p for p in sorted((path / 'masters').glob('frame-*.tif')) if not p.name.startswith('.')}
    derived = _images(path / 'derived') if (path / 'derived').is_dir() else []
    conversions = [p for p in derived if not CLEANED.match(p.stem)]
    cleaned = {}
    for p in derived:
        m = CLEANED.match(p.stem)
        if m:
            cleaned.setdefault((m['source'], p.suffix.lower()), []).append(p)
    out = []
    for frame in sorted(set(masters) | {_frame_of(p.stem) for p in conversions}):
        mine = sorted((p for p in conversions if _frame_of(p.stem) == frame),
                      key=lambda p: (_version(p.stem, CONVERTED), p.suffix.lower()))
        if not mine:
            out.append(Item(name=frame, master=masters.get(frame)))
        for conv in mine:
            copies = sorted(cleaned.get((conv.stem, conv.suffix.lower()), []),
                            key=lambda p: _version(p.stem, CLEANED))
            out.append(Item(name=frame if len(mine) == 1 else conv.name, source=conv,
                            master=masters.get(frame), cleaned=copies))
    return out


def roll_title(place):
    """A roll's film and stock, for the heading."""
    place = Path(place)
    if not roll.is_roll(place):
        return 'photos'
    info = roll.load(place)
    choice = info.get('film_choice') or {}
    parts = [choice.get('stock') or info.get('stock') or '', info.get('film') if info.get('film') != 'unknown' else '']
    return ' · '.join(p for p in parts if p) or 'roll'


def read_recipe(path):
    try:
        return json.loads(Path(str(path) + '.json').read_text())
    except (OSError, ValueError):
        return None


def describe(item):
    """The selected photo in plain words: [lines]."""
    lines = []
    if item.master:
        lines.append(f'Master: {item.master.name}')
    if item.source:
        rec = read_recipe(item.source)
        lines.append(f'Conversion: {item.source.name}' + (f" ({rec['film_selection'].get('stock')})"
                                                          if rec and rec.get('film_selection') else ''))
    if not item.cleaned:
        lines.append('Not cleaned yet.' if item.source else 'Not converted yet.')
        return lines
    rec = read_recipe(item.latest)
    lines.append(f'Cleaned: {item.latest.name}' + (f'  ({len(item.cleaned)} versions)' if len(item.cleaned) > 1 else ''))
    if not rec:
        return lines + ['(no recipe beside it)']
    for step in rec.get('steps', []):
        kind = step.get('step')
        if kind == 'dust':
            lines.append(f"  Dust: {step.get('pixels_filled', 0):,} pixels filled from their surroundings"
                         + (f", eyes and mouths of {step['faces_protected']} face{'s' if step['faces_protected'] != 1 else ''}"
                            " spared" if step.get('faces_protected') else ''))
        elif kind == 'scratches':
            lines.append(f"  Scratches: {step.get('pixels_filled', 0):,} pixels filled by MI-GAN"
                         f" ({step.get('model_calls', 0)} windows)")
        elif kind == 'crop':
            lines.append('  Crop: ' + ('holder edges trimmed' if step.get('changed') else 'nothing to trim'))
        elif kind == 'color':
            lines.append('  Color: levels and cast corrected')
        elif kind == 'spots':
            lines.append(f"  Spots: {len(step.get('spots', []))} filled by hand")
        elif kind == 'red_eye':
            lines.append(f"  Red eye: {len(step.get('eyes_corrected', []))} eyes")
        else:
            lines.append(f'  {kind.replace("_", " ").capitalize()}')
    model = rec.get('filled_in_by_model')
    if model:
        lines.append(f"  Filled in by a model: {model['pixels']:,} pixels ({model['share'] * 100:.2f}% of the picture)")
    else:
        lines.append('  Nothing filled in by a model.')
    job = rec.get('job')
    if job:
        lines.append(f"  Made on {job.get('ran_on')} (job {job.get('id', '')[:12]})")
    if rec.get('resumed_from_checkpoints'):
        lines.append('  Resumed from: ' + ', '.join(r['stage'] for r in rec['resumed_from_checkpoints']))
    lines.append(f"  {rec.get('status', '')}")
    return lines


# ---------------------------------------------------------------- previews

def _full(path):
    """The whole image as an RGB uint8 array (a 16-bit TIFF's top 8 bits)."""
    import numpy as np
    path = Path(path)
    if path.suffix.lower() in ('.tif', '.tiff'):
        import tifffile
        a = tifffile.imread(path)
        if a.ndim == 2:
            a = np.dstack([a] * 3)
        a = a[..., :3]
        return (a >> 8).astype(np.uint8) if a.dtype == np.uint16 else a.astype(np.uint8)
    from PIL import Image
    with Image.open(path) as im:
        return np.asarray(im.convert('RGB'))


def preview_image(path, side=1200):
    """A Pillow image of `path` for showing in the terminal, at most `side` px (16-bit TIFFs too)."""
    from PIL import Image
    path = Path(path)
    if path.suffix.lower() in ('.tif', '.tiff'):
        im = Image.fromarray(_full(path))
    else:
        im = Image.open(path)
        im.draft('RGB', (side, side))   # JPEGs decode straight at a smaller size
        im = im.convert('RGB')
    im.thumbnail((side, side))
    return im


class NotComparable(Exception):
    """The cleaned copy's pixels don't line up with the original's (it was straightened)."""


def _original_like(source, cleaned):
    """The original as a full array, cropped the way the cleaned copy was (from its recipe)."""
    rec = read_recipe(cleaned) or {}
    steps = {s.get('step'): s for s in rec.get('steps', [])}
    if 'straighten' in steps:
        raise NotComparable('the cleaned copy was straightened, so its pixels no longer line up with the original')
    a = _full(source)
    box = steps.get('crop', {}).get('box')
    if box:
        l, t, r, b = box
        a = a[t:b, l:r]
    return a


def before_image(source, cleaned, side=1200):
    """The original as the cleaned copy shows it (cropped the same way), so flipping between the
    two shows only what the cleaning changed. A straightened copy can't be matched: then the
    whole original."""
    from PIL import Image
    try:
        im = Image.fromarray(_original_like(source, cleaned))
    except NotComparable:
        return preview_image(source, side)
    im.thumbnail((side, side))
    return im


def side_by_side(source, cleaned, side=1200, gap=8, fixes=False):
    """Before and after next to each other in one image: (image, pixels changed or None). With
    `fixes`, the after side has every changed pixel marked in red (see changes())."""
    from PIL import Image
    a = before_image(source, cleaned, side)
    b, n = changes(source, cleaned, side) if fixes else (preview_image(cleaned, side), None)
    h = max(a.height, b.height)
    out = Image.new('RGB', (a.width + gap + b.width, h), (24, 24, 24))
    out.paste(a, (0, (h - a.height) // 2))
    out.paste(b, (a.width + gap, (h - b.height) // 2))
    return out, n


def changes(source, cleaned, side=1200, level=8, band=1024):
    """The cleaned copy with every pixel that differs from the original (by more than `level`, so
    JPEG noise doesn't count) marked in red: (Pillow image, pixels changed). Worked out at full
    size and shrunk keeping any change in a block, so a single filled speck still shows."""
    import numpy as np
    from PIL import Image
    a, b = _original_like(source, cleaned), _full(cleaned)
    if a.shape != b.shape:
        raise NotComparable(f'the original ({a.shape[1]}x{a.shape[0]}) and the cleaned copy '
                            f'({b.shape[1]}x{b.shape[0]}) are different sizes')
    h, w = b.shape[:2]
    f = max(1, -(-max(h, w) // side))         # shrink factor, rounded up
    sh, sw = -(-h // f), -(-w // f)
    marks = np.zeros((sh, sw), bool)
    changed = 0
    step = max(f, band // f * f)
    for y0 in range(0, h, step):
        diff = (np.abs(a[y0:y0 + step].astype(np.int16) - b[y0:y0 + step]).max(axis=2) > level)
        changed += int(diff.sum())
        rows, cols = diff.shape
        padded = np.zeros((-(-rows // f) * f, sw * f), bool)
        padded[:rows, :cols] = diff
        marks[y0 // f:y0 // f + padded.shape[0] // f] |= padded.reshape(-1, f, sw, f).any(axis=(1, 3))
    small = np.asarray(Image.fromarray(b).resize((sw, sh), Image.BOX)).copy()
    grown = marks.copy()                       # a pixel wider, so a speck is visible at a glance
    grown[1:] |= marks[:-1]
    grown[:-1] |= marks[1:]
    grown[:, 1:] |= marks[:, :-1]
    grown[:, :-1] |= marks[:, 1:]
    small[grown] = (230, 30, 30)
    return Image.fromarray(small), changed


def screen_here():
    """Whether this machine has a screen to open a viewer on (not, say, over SSH to a Pi)."""
    if sys.platform in ('darwin', 'win32'):
        return not os.environ.get('SSH_CONNECTION')
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def open_in_viewer(*paths):
    """Show files in the system's viewer: Quick Look on macOS (all of them in one window), the
    default viewer elsewhere. Returns what was done, or raises OSError with why not."""
    paths = [str(p) for p in paths if p]
    if not paths:
        raise OSError('nothing to show')
    if not screen_here():
        raise OSError('no screen here to open a viewer on; the preview in the window is all there is')
    if sys.platform == 'darwin':
        subprocess.Popen(['qlmanage', '-p', *paths], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 'Quick Look'
    if sys.platform == 'win32':
        for p in paths:
            os.startfile(p)   # noqa: S606 - the user's own files, in their default viewer
        return 'the default viewer'
    opener = shutil.which('xdg-open')
    if not opener:
        raise OSError('no xdg-open here to open a viewer with')
    for p in paths:
        subprocess.Popen([opener, p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 'the default viewer'
