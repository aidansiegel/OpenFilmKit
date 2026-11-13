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
        elif kind == 'to_srgb':
            lines.append(f"  Colors: converted from {step.get('from')} to sRGB")
        elif kind == 'upright':
            lines.append('  Turned upright, as its orientation tag says')
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

def _as_clean_reads(a, orientation, icc):
    """Pixels (RGB, or gray as a 2-D array) the way clean reads their photo (see pixels.py): in sRGB
    and turned upright, so an original lines up with its cleaned copy. With a color profile clean
    would refuse, the photo can still be looked at, as it is."""
    import numpy as np
    from .. import pixels
    try:
        a, _ = pixels.to_srgb(a, icc, exact=False)
    except pixels.ProfileError:
        a = a if a.ndim == 3 else np.dstack([a] * 3)
    return pixels.upright(a, orientation)


def _full(path):
    """The whole image as an RGB uint8 array (a 16-bit TIFF's top 8 bits), as clean reads it."""
    import numpy as np
    from .. import pixels
    path = Path(path)
    if path.suffix.lower() in ('.tif', '.tiff'):
        import tifffile
        with tifffile.TiffFile(path) as tif:
            page = pixels.tiff_page(tif)   # the picture, as clean reads it: not a thumbnail first, not a mask
            a, tags = page.asarray(), pixels.tiff_tags(page)
        a = a if a.ndim == 2 else a[..., :3]
        a = (a >> 8).astype(np.uint8) if a.dtype == np.uint16 else a.astype(np.uint8)
    else:
        from PIL import Image
        with Image.open(path) as im:
            a, tags = np.asarray(im if im.mode == 'L' else im.convert('RGB')), pixels.tags(im)
    return _as_clean_reads(a, *tags)


def zoomed(z, center):
    """The part of a picture shown at zoom `z` (1 = all of it) around `center` (fractions across
    and down): (left, top, right, bottom) as fractions, kept inside the picture. None at zoom 1."""
    if z <= 1:
        return None
    half = 0.5 / z
    cx = min(max(center[0], half), 1 - half)
    cy = min(max(center[1], half), 1 - half)
    return (cx - half, cy - half, cx + half, cy + half)


def _crop(a, region):
    """Part of an image array: `region` as fractions (see zoomed()), at least a pixel."""
    if not region:
        return a
    h, w = a.shape[:2]
    l, t = int(region[0] * w), int(region[1] * h)
    return a[t:max(t + 1, round(region[3] * h)), l:max(l + 1, round(region[2] * w))]


def _single(im):
    im.info['panels'] = [(0.0, 1.0, 0.0, 1.0)]
    return im


def preview_image(path, side=1200, region=None):
    """A Pillow image of `path` for showing in the terminal, at most `side` px (16-bit TIFFs too);
    with `region`, only that part of it (see zoomed())."""
    from PIL import Image
    path = Path(path)
    if region or path.suffix.lower() in ('.tif', '.tiff'):
        im = Image.fromarray(_crop(_full(path), region))
        im.thumbnail((side, side))
    else:
        import numpy as np
        from .. import pixels
        with Image.open(path) as im:
            tags = pixels.tags(im)
            im.draft('RGB', (side, side))   # JPEGs decode straight at a smaller size
            im = im if im.mode == 'L' else im.convert('RGB')
            im.thumbnail((side, side))
            im = Image.fromarray(_as_clean_reads(np.asarray(im), *tags))
    return _single(im)


def size_of(path):
    """(width, height) of an image, from its header."""
    from PIL import Image
    with Image.open(path) as im:
        return im.size


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


def before_image(source, cleaned, side=1200, region=None):
    """The original as the cleaned copy shows it (cropped the same way), so flipping between the
    two shows only what the cleaning changed. A straightened copy can't be matched: then the
    whole original."""
    from PIL import Image
    try:
        im = Image.fromarray(_crop(_original_like(source, cleaned), region))
    except NotComparable:
        return preview_image(source, side, region)
    im.thumbnail((side, side))
    return _single(im)


def side_by_side(source, cleaned, side=1200, gap=8, fixes=False, region=None):
    """Before and after next to each other in one image: (image, pixels changed or None). With
    `fixes`, the after side has every changed pixel marked in red (see changes()). The image's
    info['panels'] says where each side is, as fractions (left, right, top, bottom)."""
    from PIL import Image
    a = before_image(source, cleaned, side, region)
    b, n = changes(source, cleaned, side, region=region) if fixes else (preview_image(cleaned, side, region), None)
    h = max(a.height, b.height)
    width = a.width + gap + b.width
    out = Image.new('RGB', (width, h), (24, 24, 24))
    out.paste(a, (0, (h - a.height) // 2))
    out.paste(b, (a.width + gap, (h - b.height) // 2))
    out.info['panels'] = [(0.0, a.width / width, (h - a.height) / 2 / h, (h + a.height) / 2 / h),
                          ((a.width + gap) / width, 1.0, (h - b.height) / 2 / h, (h + b.height) / 2 / h)]
    return out, n


def changes(source, cleaned, side=1200, level=8, band=1024, region=None):
    """The cleaned copy with every pixel that differs from the original (by more than `level`, so
    JPEG noise doesn't count) marked in red: (Pillow image, pixels changed). Worked out at full
    size and shrunk keeping any change in a block, so a single filled speck still shows. With
    `region`, only that part (and the pixels changed in it)."""
    import numpy as np
    from PIL import Image
    a, b = _original_like(source, cleaned), _full(cleaned)
    if a.shape != b.shape:
        raise NotComparable(f'the original ({a.shape[1]}x{a.shape[0]}) and the cleaned copy '
                            f'({b.shape[1]}x{b.shape[0]}) are different sizes')
    a, b = _crop(a, region), _crop(b, region)
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
    # Shown at the size before_image() gives the original, so the two halves of side_by_side() match;
    # the marks, made on the coarser grid of the whole-number factor, are spread onto it.
    im = Image.fromarray(b)
    im.thumbnail((side, side), Image.BOX)
    small = np.asarray(im).copy()
    marks = np.asarray(Image.fromarray(marks).resize(im.size, Image.NEAREST))
    grown = marks.copy()                       # a pixel wider, so a speck is visible at a glance
    grown[1:] |= marks[:-1]
    grown[:-1] |= marks[1:]
    grown[:, 1:] |= marks[:, :-1]
    grown[:, :-1] |= marks[:, 1:]
    small[grown] = (230, 30, 30)
    return _single(Image.fromarray(small)), changed


def default_radius(size):
    """The radius clean gives a spot when none is chosen: 0.4 % of the short side."""
    return max(2, round(min(size) * 0.004))


def draw_spots(im, spots, size, region=None):
    """Rings on a preview where spots are marked: `spots` [(x, y, r)] in pixels of a picture of
    `size`, drawn on every panel of `im` (see side_by_side()) showing `region` of it."""
    from PIL import ImageDraw
    if not spots:
        return im
    im = im.copy()
    pen = ImageDraw.Draw(im)
    l, t, r, b = region or (0, 0, 1, 1)
    for x0, x1, y0, y1 in im.info.get('panels', [(0, 1, 0, 1)]):
        pw, ph = (x1 - x0) * im.width, (y1 - y0) * im.height
        for x, y, rad in spots:
            if not (l <= x / size[0] <= r and t <= y / size[1] <= b):
                continue                       # outside the part shown
            cx = x0 * im.width + (x / size[0] - l) / (r - l) * pw
            cy = y0 * im.height + (y / size[1] - t) / (b - t) * ph
            rr = max(3, rad / size[0] / (r - l) * pw)
            pen.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=(255, 210, 0), width=2)
    return im


def clean_args(options):
    """openfilmkit clean arguments that repeat a recipe's options (see clean.settings()), so a new
    copy is made the same way: spots marked on the old one then land in the same places."""
    args = []
    for key, off in (('crop', '--no-crop'), ('dust', '--no-dust'), ('protect_faces', '--no-faces')):
        if options.get(key) is False:
            args.append(off)
    for key in ('color', 'red_eye', 'scratches', 'spare_long_lines'):
        if options.get(key):
            args.append('--' + key.replace('_', '-'))
    for key in ('straighten', 'lift_shadows'):
        if options.get(key) is not None:
            args += ['--' + key.replace('_', '-'), str(options[key])]
    return args


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
