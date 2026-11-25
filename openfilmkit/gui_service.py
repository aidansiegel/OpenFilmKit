"""The desktop app's back end: one long-running process the app talks to over JSON lines on stdin/stdout.

Each request is one line, {"id": 1, "method": "preview", "params": {...}}; each reply is one line, {"id": 1,
"result": ...} or {"id": 1, "error": "what went wrong, in plain words"}. Lines without an "id" are events
({"event": "scan", ...}) for work that takes a while. Everything else (warnings, tracebacks) goes to stderr.

It does nothing the command line can't: it calls the same functions, so a picture saved from the app has the
same recipe a `convert` makes and can be redone from the command line. Live previews keep the shrunken scan and
its curve in memory, so moving a slider only re-renders a small image.
"""
import argparse
import base64
import datetime
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np
from PIL import Image

from . import __version__, destinations, roll
from .films import auto, convert as conv, stocks as film_stocks
from .ui import model

PREVIEW_SIDE = 1400
THUMB_SIDE = 360
DEFAULTS = dict(contrast=1.3, gamma=1.0, warmth=0.0, saturation=1.0)


class Problem(Exception):
    """An error to show the person as is."""


def _jpeg(pixels_or_image, quality=88):
    im = pixels_or_image if isinstance(pixels_or_image, Image.Image) else Image.fromarray(pixels_or_image)
    buf = io.BytesIO()
    im.convert('RGB').save(buf, 'JPEG', quality=quality)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


class Frame:
    """A scan master shrunk once for previews, with what convert would work out for it: the film's curve, the
    picture box, the color balance and exposure, and which way up."""

    def __init__(self, master, stock=None, side=PREVIEW_SIDE, quick=False, progress=None):
        say = progress or (lambda fraction, step, left=None: None)   # progress(0..1, what it's doing, seconds left or None)
        self.master = Path(master)
        say(0., 'Reading the scan')
        raw = conv.raw_open(self.master)
        stride = max(1, math.ceil(max(raw.shape[:2]) / side))
        whole = conv.shrink(raw, stride)
        self.size = [int(raw.shape[1]), int(raw.shape[0])]
        del raw
        roll_dir = roll.containing(self.master)
        about = roll.load(roll_dir) if roll_dir else {}
        meta = conv.capture_metadata(self.master)
        if not meta.get('film') and about.get('film') not in (None, '', 'unknown'):
            meta['film'] = about['film']
        self.choice = film_stocks.selection(stock, about.get('film_choice') or {}, meta, None,
                                            roll=roll_dir.name if roll_dir else None, roll_stock=about.get('stock'))
        self.film = self.choice['film']
        profile = film_stocks.profile(self.choice['profile_id'])
        # An empty holder slot (no film: the light went straight through) has no picture to find, levels to fit,
        # color to measure or way up: it's shown plainly, whole, and said to be empty.
        self.empty = conv.looks_empty(whole)
        x0, y0, x1, y1 = (0, 0, whole.shape[1], whole.shape[0]) if self.empty else auto.picture_box(whole)
        self.stride, self.box = stride, (x0, y0, x1, y1)   # the picture inside the scan, in `whole`'s pixels
        self.small = np.ascontiguousarray(whole[y0:y1, x0:x1])
        self.whole = whole
        lo, hi = (np.full(3, .02), np.full(3, 1.)) if self.empty else conv.fit_levels(self.small)
        self.levels = (lo.tolist(), hi.tolist())   # handed to a save, so it isn't measured again on the full scan
        self.lut = conv.slide_lut(profile, lo, hi) if self.film == 'slide' else conv.make_lut(profile, lo, hi)
        self.health = conv.scan_health.check(self.small)
        # an empty slot is all at full scale: say that, not "red is at full scale in 99% of the scan"
        self.scan_warnings = [EMPTY_FRAME] if self.empty else plain_health(self.health)
        self.gains, self.exposure, self.roll_check, self.tone = None, 1.0, None, None
        if self.film != 'slide' and not self.empty:
            others = None
            if roll_dir:
                me = self.master.resolve()
                def measuring(done, total, left=None):
                    say(.1 + .7 * done / max(total, 1), f"Measuring the roll's color ({done} of {total} frames)" if total else "Measuring the roll's color", left)
                others = [g for n, g in conv.roll_balance(roll_dir, self.choice, profile, measure=not quick,
                                                          progress=None if quick else measuring).items()
                          if (roll_dir / 'masters' / n).resolve() != me]
            say(.82, 'Working out color, brightness and contrast')
            m = auto.measure(self.small, self.lut, others)
            self.gains, self.exposure, self.roll_check, self.tone = m['gains'], m['exposure'], m['roll_check'], m['tone']
        self.dust_masks = {}      # crop box -> where the dust is
        self.base = {**DEFAULTS, **({'contrast': 1.0} if self.film == 'slide' else {})}
        say(.9, 'Finding which way is up')
        self.auto_rotate, self.rotate_reason = (0, 'empty') if self.empty else auto.orientation(self._render(self.base, False))

    def _render(self, s, use_auto=True, src=None):
        """The picture with settings `s`: the automatic crop (or `src`, part of the whole scan) through the frame's
        curve, balance, exposure and tone; or, with auto off, the whole scan as plainly converted."""
        on = s.get('auto', True) is not False
        if src is None:
            src = self.small if on else self.whole
        return conv.render(src, self.lut, float(s['contrast']), float(s['gamma']), float(s['warmth']), 255,
                           float(s['saturation']), self.gains if on else None, self.exposure if on else 1.0,
                           self.tone if on else None)

    def rotation(self, settings):
        r = (settings or {}).get('rotate')
        return int(r) % 360 if r is not None else self.auto_rotate

    # --- the crop: settings['crop'] is None (automatic), 'none' (the whole scan) or [x0, y0, x1, y1], fractions
    # of the whole scan as shown turned. Color is measured on the automatic crop either way, so moving the box
    # changes what's in the picture, not how it looks.
    def crop_box(self, settings):
        """The crop in `whole`'s pixels (unturned)."""
        crop = (settings or {}).get('crop')
        H, W = self.whole.shape[:2]
        if crop is None:
            return self.box
        if crop == 'none':
            return (0, 0, W, H)
        x0, y0, x1, y1 = view_to_scan(crop, self.rotation(settings), bool((settings or {}).get('mirror')))
        x0, x1 = int(round(x0 * W)), int(round(x1 * W))
        y0, y1 = int(round(y0 * H)), int(round(y1 * H))
        x0, y0 = min(max(x0, 0), W - 2), min(max(y0, 0), H - 2)
        return (x0, y0, max(x0 + 2, min(x1, W)), max(y0 + 2, min(y1, H)))

    def crop_shown(self, settings):
        """What the editor draws: (the automatic crop, the crop in use or None for 'none'), as fractions of the
        whole scan turned."""
        H, W = self.whole.shape[:2]
        turn = self.rotation(settings)
        frac = lambda b: scan_to_view((b[0] / W, b[1] / H, b[2] / W, b[3] / H), turn, bool((settings or {}).get('mirror')))
        crop = (settings or {}).get('crop')
        return frac(self.box), (None if crop == 'none' else frac(self.crop_box(settings)))

    def auto_crop_px(self):
        """The automatic crop (what the preview shows) in the scan's own pixels, [x0, y0, x1, y1]."""
        x0, y0, x1, y1 = self.box
        W, H = self.size
        return [x0 * self.stride, y0 * self.stride, min(x1 * self.stride, W), min(y1 * self.stride, H)]

    def raw_crop(self, settings):
        """The crop in the scan's own pixels, for convert --crop; None for the automatic crop."""
        return raw_crop((settings or {}).get('crop'), self.rotation(settings), *self.size,
                        mirror=bool((settings or {}).get('mirror')))

    def _dust(self, out, key):
        """Dust removal on a preview: where the dust is gets found once per frame and crop (on the automatic
        picture, so moving a slider doesn't move it) and filled in on each render, as convert --dust does."""
        from . import clean
        if key not in self.dust_masks:
            if len(self.dust_masks) > 4:
                self.dust_masks.pop(next(iter(self.dust_masks)))
            x0, y0, x1, y1 = key
            self.dust_masks[key] = clean.find_dust(self._render(self.base, src=self.whole[y0:y1, x0:x1]))[0]
        mask = self.dust_masks[key]
        if mask.shape != out.shape[:2] or not mask.any():
            return out
        return clean.restore.remove_dust(np.ascontiguousarray(out), mask)

    @property
    def dust_mask(self):   # whether the automatic crop's dust has been found (the preview's progress asks)
        return self.dust_masks.get(tuple(self.box))

    def region(self, settings, box, side):
        """Part of the picture at the scan's own detail: `box` is (x0, y0, x1, y1) as fractions of the picture as
        shown (turned and cropped), rendered with the same settings to about `side` pixels on its long edge."""
        turn = self.rotation(settings)
        X0, Y0, X1, Y1 = (min(max(float(v), 0.), 1.) for v in box)
        if X1 <= X0 or Y1 <= Y0:
            raise Problem('The part to show is empty.')
        mirror = bool((settings or {}).get('mirror'))
        x0, y0, x1, y1 = view_to_scan((X0, Y0, X1, Y1), turn, mirror)   # back to the scan as it lies
        bx0, by0, bx1, by1 = self.crop_box(settings)
        sx, sy = (bx1 - bx0) * self.stride, (by1 - by0) * self.stride
        raw = conv.raw_open(self.master)
        h, w = raw.shape[:2]
        rx0, ry0 = bx0 * self.stride + int(x0 * sx), by0 * self.stride + int(y0 * sy)
        rx1, ry1 = min(w, bx0 * self.stride + int(np.ceil(x1 * sx))), min(h, by0 * self.stride + int(np.ceil(y1 * sy)))
        if rx1 - rx0 < 2 or ry1 - ry0 < 2:
            raise Problem('The part to show is too small.')
        part, _ = conv.glance(raw[ry0:ry1, rx0:rx1], max(64, min(int(side), 4000)))
        del raw
        s = {**self.base, **{k: v for k, v in (settings or {}).items() if v is not None}}
        use = s.get('auto', True) is not False
        out = conv.render(part, self.lut, float(s['contrast']), float(s['gamma']), float(s['warmth']), 255,
                          float(s['saturation']), self.gains if use else None, self.exposure if use else 1.0,
                          self.tone if use else None)
        if s.get('dust') and use:
            from . import clean
            clean.remove_dust(out)
        return shown(out, turn, mirror)

    def render(self, settings):
        """The picture as shown: cropped and turned. With settings['uncropped'], the whole scan turned (the crop
        editor draws the box over it), looking exactly as the cropped picture does inside the box."""
        s = {**self.base, **{k: v for k, v in (settings or {}).items() if v is not None}}
        on = s.get('auto', True) is not False
        if not on:
            out = self._render(s)
        elif s.get('uncropped'):
            out = self._render(s, src=self.whole)
        else:
            key = tuple(self.crop_box(settings))
            out = self._render(s, src=None if key == tuple(self.box) else self.whole[key[1]:key[3], key[0]:key[2]])
            if s.get('dust'):
                out = self._dust(out, key)
        return shown(out, self.rotation(settings), bool(s.get('mirror')))


def shown(pixels, turn, mirror=False):
    """Pixels of the scan as it lies, as the picture is seen: turned clockwise, then mirrored left to right."""
    if turn:
        pixels = np.rot90(pixels, -turn // 90)
    if mirror:
        pixels = pixels[:, ::-1]
    return np.ascontiguousarray(pixels) if turn or mirror else pixels


TURN = {0: lambda x, y: (x, y), 90: lambda x, y: (1 - y, x), 180: lambda x, y: (1 - x, 1 - y), 270: lambda x, y: (y, 1 - x)}
UNTURN = {0: lambda X, Y: (X, Y), 90: lambda X, Y: (Y, 1 - X), 180: lambda X, Y: (1 - X, 1 - Y), 270: lambda X, Y: (1 - Y, X)}


def _box_through(box, f):
    x0, y0, x1, y1 = (min(max(float(v), 0.), 1.) for v in box)
    xs, ys = zip(f(x0, y0), f(x1, y1))
    return [round(min(xs), 5), round(min(ys), 5), round(max(xs), 5), round(max(ys), 5)]


def raw_crop(crop, turn, w, h, mirror=False):
    """settings['crop'] in the scan's own pixels (w x h, unturned) for convert --crop; None for the automatic
    crop, which convert finds itself."""
    if crop is None:
        return None
    if crop == 'none':
        return [0, 0, w, h]
    x0, y0, x1, y1 = view_to_scan(crop, turn, mirror)
    x0, y0 = min(max(int(round(x0 * w)), 0), w - 2), min(max(int(round(y0 * h)), 0), h - 2)
    return [x0, y0, max(x0 + 2, min(int(round(x1 * w)), w)), max(y0 + 2, min(int(round(y1 * h)), h))]


def crop_source(s):
    """How a picture's crop was chosen, as its recipe records it (convert's crop_choice)."""
    if s.get('auto', True) is False:
        return 'whole'
    crop = s.get('crop')
    return 'automatic' if crop is None else 'none' if crop == 'none' else 'manual'


def mirror_box(box):
    """A box flipped left to right (its own inverse)."""
    x0, y0, x1, y1 = box
    return [round(1 - x1, 5), y0, round(1 - x0, 5), y1]


def view_to_scan(box, turn, mirror=False):
    """A box drawn on the picture as seen (turned, then mirrored if asked) as fractions of the scan as it lies."""
    return unturn_box(mirror_box(box) if mirror else box, turn)


def scan_to_view(box, turn, mirror=False):
    """A box on the scan as it lies, as it is on the picture as seen."""
    b = turn_box(box, turn)
    return mirror_box(b) if mirror else b


def turn_box(box, turn):
    """A box (fractions of the scan as it lies) as it is after turning the picture `turn` degrees clockwise."""
    return _box_through(box, TURN[int(turn) % 360])


def unturn_box(box, turn):
    """A box drawn on the picture turned `turn` degrees clockwise, as fractions of the scan as it lies."""
    return _box_through(box, UNTURN[int(turn) % 360])


def _utc():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _queue_names():
    from . import queues
    try:
        return sorted((queues._config().get('queues') or {}).keys())
    except queues.QueueError:
        return []


def _queue(name):
    from . import queues
    try:
        return queues.queue_settings(name)
    except queues.QueueError as e:
        raise Problem(str(e)) from e


def _already_saved(roll_dir, master, s, stock, suffix, turn, crop_px=None):
    """The newest picture in the roll's derived/ made from this scan, as it is now, with these adjustments; or None."""
    try:
        made_after = master.stat().st_mtime
    except OSError:
        return None
    for recipe in sorted((roll_dir / 'derived').glob(f'{master.stem}-v*{suffix}.json'),
                         key=lambda p: p.stat().st_mtime, reverse=True):
        picture = Path(str(recipe)[:-len('.json')])
        try:
            r = json.loads(recipe.read_text())
        except (OSError, ValueError):
            continue
        if not picture.is_file() or Path(r.get('source', '')).resolve() != master.resolve():
            continue
        if recipe.stat().st_mtime < made_after:     # rescanned since
            return None
        same = (all(abs(float(r.get(k, -99)) - float(s[k])) < 1e-6 for k in ('contrast', 'gamma', 'warmth', 'saturation'))
                and (r.get('automatic') is not None) == (s.get('auto', True) is not False)
                and bool(r.get('dust')) == bool(s.get('dust'))
                and bool(r.get('mirror')) == bool(s.get('mirror'))
                and (turn is None or r.get('rotation_cw') == turn)
                and (not stock or (r.get('film_selection') or {}).get('stock_id') == stock)
                and r.get('downscale') is None
                and r.get('crop_choice', 'automatic' if r.get('automatic') else 'whole') == crop_source(s)
                and (crop_px is None or all(abs(a - b) <= 2 for a, b in zip(r.get('raw_crop_pixels') or [], crop_px))))
        return picture if same else None            # only the newest counts: an older match was replaced
    return None


FILM_WORDS = {'color-negative': 'Color negative', 'slide': 'Slide', 'bw-negative': 'Black and white',
              'unknown': 'Film'}
MAKERS = ('kodak', 'fuji', 'agfa', 'konica', 'ilford', 'imation', 'kirkland', 'scotch', 'ferrania', 'polaroid',
          'lomography', 'cinestill', 'foma', 'rollei')


def maker(stock_id):
    """The brand a stock id starts with ('kodak-gold200' -> 'kodak'), for the canister the app draws; or 'unknown'."""
    head = str(stock_id or '').split('-')[0]
    return next((m for m in MAKERS if head.startswith(m)), 'unknown')


def film_details(choice):
    """What the Film card shows: the stock, its kind and speed, and the edge print as read off the film."""
    edge = re.sub(r'\s*\(.*?\)\s*$', '', choice.get('edge_markings') or '').strip()   # "(read by ..., date)" is a note
    basis = choice.get('selection_basis') or ''
    return dict(stock_id=choice['stock_id'], name=choice['stock'], kind=FILM_WORDS.get(choice['film'], 'Film'),
                film=choice['film'], iso=choice.get('iso_speed'), maker=maker(choice['stock_id']), edge_print=edge,
                chosen='here' if basis == 'explicit stock option' else 'roll' if 'roll' in basis else 'unknown',
                profile=choice.get('profile_name'))


def scan_details(master, size, warnings):
    """What the Scan card shows: resolution, size, scanner, when, which frame on the film, and any problems."""
    record = film_stocks.read(str(master) + '.json', {}) or {}
    tiff, scanner = record.get('tiff') or {}, record.get('scanner') or {}
    dpi = tiff.get('dpi_x')
    if not dpi:   # another program's scan: the TIFF's own resolution tag
        try:
            import tifffile
            with tifffile.TiffFile(master) as t:
                tag = t.pages[0].tags.get('XResolution')
                unit = t.pages[0].tags.get('ResolutionUnit')
                if tag:
                    num, den = tag.value
                    dpi = num / den * (2.54 if unit and unit.value == 3 else 1)
        except Exception:
            dpi = None
    dpi = round(dpi) if dpi and dpi > 72 else None   # 72 is "not set" in most programs
    note = ((record.get('film') or {}).get('note') or '') if isinstance(record.get('film'), dict) else ''
    m = re.search(r'frame\s*(\d+[A-Z]?)', note, re.I) or re.search(r'frame-(\d+)', Path(master).name, re.I)
    named = ' '.join(x for x in (scanner.get('vendor', '').title() if scanner.get('vendor') else '',
                                 scanner.get('model', '')) if x)
    problems = [w[0].upper() + w[1:] for w in warnings]
    return dict(dpi=dpi, width=size[0], height=size[1], scanner=named or None, captured_utc=record.get('started_utc'),
                frame=(m.group(1).lstrip('0') or '0') if m else None, problems=problems,
                summary='No problems found' if not problems else f"{len(problems)} thing{'s' if len(problems) > 1 else ''} to know about this scan")


class Service:
    def __init__(self, write):
        self.write = write
        self.frames = {}          # (master, stock) -> Frame; the last few only
        self.frames_lock = threading.Lock()
        self.scan_proc, self.scan_stopping = None, False
        self.exporting = None     # the thread saving a roll (export)
        self.stop_export = None
        self.measuring = set()    # rolls whose balance is being measured in the background
        self.export_log = None    # the current or last export, for the Queue tab: {started_utc, total, items, done}

    # --- what's on disk ---------------------------------------------------------------
    def open(self, path):
        folder = Path(path).expanduser()
        if not folder.is_dir():
            raise Problem(f"{folder} isn't a folder.")
        out = []
        for label, place in model.places(folder):
            frames = []
            for item in model.items(place):
                frames.append(dict(name=item.name, master=str(item.master) if item.master else None,
                                   picture=str(item.latest or item.source) if (item.latest or item.source) else None,
                                   state=item.state))
            if roll.is_roll(place):
                self._measure_roll(place)
            out.append(dict(label=label, path=str(place), is_roll=roll.is_roll(place),
                            title=model.roll_title(place), frames=frames))
        return dict(folder=str(folder.resolve()), places=out)

    def _measure_roll(self, place):
        """Measure a roll's balance for the roll check in the background, so thumbnails (which use only what's
        measured) don't wait for it and the first preview finds it done. One roll at a time; again is a no-op."""
        place = Path(place).resolve()
        if place in self.measuring:
            return
        self.measuring.add(place)

        def run():
            try:
                about = roll.load(place)
                choice = film_stocks.selection(None, about.get('film_choice') or {}, {}, None, roll=place.name,
                                               roll_stock=about.get('stock'))
                if choice['film'] != 'slide':
                    conv.roll_balance(place, choice, film_stocks.profile(choice['profile_id']))
            except Exception:
                traceback.print_exc(file=sys.stderr)
            finally:
                self.measuring.discard(place)
        threading.Thread(target=run, daemon=True).start()

    def stocks(self):
        return [dict(id=s['id'], name=s['name'], film=s.get('film', 'color-negative'), iso=s.get('iso'),
                     kind=FILM_WORDS.get(s.get('film', 'color-negative'), 'Film'), maker=maker(s['id']))
                for s in film_stocks.stocks()]

    def new_roll(self, parent, name=None, film='color-negative'):
        """A new roll folder in `parent`. Without a name, "Roll <today>" by this computer's own date, with
        " 2", " 3"... after it when that is taken, so a second roll the same day just works."""
        parent = Path(parent).expanduser()
        if name is None:
            base = f"Roll {datetime.date.today().isoformat()}"
            name = next(n for n in (base if i == 1 else f'{base} {i}' for i in range(1, 10000))
                        if not (parent / n).exists())
        target = parent / name
        if target.exists():
            raise Problem(f'There is already a folder called {name} in {parent}.')
        roll.create(target, film=film)
        return dict(path=str(target))

    # --- pictures ---------------------------------------------------------------------
    def _progress(self, master):
        """A Frame's progress as 'preview-progress' events, for the app's bar under "Developing…"."""
        def send(fraction, step, left=None):
            self.write(dict(event='preview-progress', master=str(master), fraction=round(float(fraction), 3), step=step,
                            seconds_left=left))
        return send

    def _frame(self, master, stock):
        with self.frames_lock:   # zoomed parts are made on other threads
            return self._frame_unlocked(master, stock)

    def _frame_unlocked(self, master, stock):
        key = (str(master), stock or '')
        if key not in self.frames:
            if len(self.frames) >= 3:
                self.frames.pop(next(iter(self.frames)))
            try:
                self.frames[key] = Frame(master, stock, progress=self._progress(master))
            except ValueError as e:
                raise Problem(str(e)) from e
        return self.frames[key]

    def thumb(self, path, master=False, side=THUMB_SIDE, settings=None, stock=None):
        """A small picture: a master converted with the frame's own adjustments, film and turn (what Export will
        save), or a photo (a saved picture) shrunk."""
        side = max(64, min(int(side), 2400))
        try:
            if master:
                return _jpeg(Frame(path, stock, side=side, quick=True).render(settings or {}), 80)
            return _jpeg(model.preview_image(path, side=side), 85)
        except (OSError, ValueError) as e:
            raise Problem(f"Couldn't read {Path(path).name}: {e}") from e

    def preview(self, master, settings=None, stock=None):
        f = self._frame(master, stock)
        if (settings or {}).get('dust') and tuple(f.crop_box(settings)) not in f.dust_masks:
            self._progress(master)(.95, 'Finding dust')
        image = _jpeg(f.render(settings))
        self._progress(master)(1., 'Done')
        auto_crop, crop_used = f.crop_shown(settings)
        return dict(image=image, auto_crop=auto_crop, crop_used=crop_used, crop=(settings or {}).get('crop'),
                    mirror=bool((settings or {}).get('mirror')),
                    stock=f.choice['stock_id'], stock_name=f.choice['stock'],
                    film=f.film, size=f.size, warnings=f.scan_warnings, rotate=f.rotation(settings),
                    auto_rotate=f.auto_rotate, rotate_reason=f.rotate_reason, empty=f.empty,
                    film_card=film_details(f.choice), scan_card=scan_details(f.master, f.size, f.scan_warnings))

    def preview_region(self, master, box, settings=None, stock=None, side=1400):
        """A zoomed-in part of the preview at the scan's full detail (box: fractions of the picture as shown).
        Runs beside other requests; the app keeps only the newest answer."""
        f = self._frame(master, stock)
        try:
            image = _jpeg(f.region(settings, box, side), 90)
        except (OSError, ValueError) as e:
            raise Problem(f"Couldn't read that part of {Path(master).name}: {e}") from e
        return dict(image=image, box=list(box))

    def details(self, master, stock=None):
        """The Film and Scan cards for a frame without making a preview (fast once the frame has been opened)."""
        f = self._frame(master, stock)
        return dict(film_card=film_details(f.choice), scan_card=scan_details(f.master, f.size, f.scan_warnings))

    @staticmethod
    def _settings(settings):
        return {**DEFAULTS, **{k: v for k, v in (settings or {}).items() if v is not None}}

    @staticmethod
    def _args(master, s, stock, output, tiff, turn, crop_px=None, levels=None):
        """convert's options for a picture saved from the app (full size, as the preview showed it)."""
        return argparse.Namespace(source=str(master), output=str(output), stock=stock, roll=None, profile=None,
                                  crop=crop_px, crop_source=crop_source(s), calibration=None, scene=None, recover_clipped_blue=None,
                                  levels_from=None, levels=levels, max_side=0, contrast=float(s['contrast']),
                                  gamma=float(s['gamma']), warmth=float(s['warmth']),
                                  saturation=float(s['saturation']), lut=None, tiff=tiff,
                                  rotate=turn, auto=s.get('auto', True) is not False, dust=bool(s.get('dust')),
                                  mirror=bool(s.get('mirror')))

    def _plan(self, master, s, stock, pin_turn=False):
        """(turn, crop in scan pixels) for saving a frame. The turn: the one chosen, else the preview's when it must
        match the screen (a save, or a crop box drawn on the turned picture), else None (convert decides)."""
        on = s.get('auto', True) is not False
        if s.get('rotate') is not None:
            turn = int(s['rotate']) % 360
        elif not on:
            turn = 0
        elif pin_turn or isinstance(s.get('crop'), (list, tuple)):
            turn = self._frame(master, stock).rotation(s)
        else:
            turn = None
        crop = s.get('crop') if on else None
        if crop is None:
            # The automatic crop: for a save, the box the preview showed. Left to convert, it was found again on a
            # copy of another size and came out a little different (dogfooding: up to 2% narrower, and color
            # measured on a different area), so the saved picture wasn't the one on screen.
            return turn, (self._frame(master, stock).auto_crop_px() if pin_turn and on else None)
        try:
            h, w = conv.raw_open(master).shape[:2]
        except (OSError, ValueError) as e:
            raise Problem(f"Couldn't read {Path(master).name}: {e}") from e
        return turn, raw_crop(crop, turn or 0, w, h, mirror=bool(s.get('mirror')))

    def save(self, master, settings=None, stock=None, output=None, tiff=False):
        master = Path(master)
        s = self._settings(settings)
        # the turn and crop the preview showed, so the saved picture is the one on screen
        turn, crop_px = self._plan(master, s, stock, pin_turn=True)
        if output is None:
            roll_dir = roll.containing(master)
            if not roll_dir:
                raise Problem('Choose where to save it: this scan is not in a roll folder.')
            output = roll.next_derived(roll_dir, master.stem, '.tif' if tiff else '.jpg')
        try:
            # the preview's levels: measured again on the full-size scan, where grain moves the extremes, the
            # saved picture came out up to 12 levels off the one on screen (dogfooding, real negatives)
            record = conv.convert(self._args(master, s, stock, output, tiff, turn, crop_px,
                                             levels=self._frame(master, stock).levels))
        except ValueError as e:
            raise Problem(str(e)) from e
        return dict(output=record['output'], warnings=record['channel_health']['warnings'])

    # --- a whole roll -------------------------------------------------------------------
    def export(self, items, tiff=False, redo=False, folder=None):
        """Save many frames, one after another, in the background: Export on a roll.

        `items`: [{master, settings, stock}], each frame with the adjustments the app holds for it. A frame
        already saved from the same scan with the same adjustments is skipped (unless `redo`), so Export again
        after changing two frames saves those two. A turn the person chose is kept; otherwise convert turns it
        upright itself. Events: 'export' after each frame ({index, total, master, status: saved, skipped or
        failed, output, message}), then 'export-done' ({saved, skipped, failed, stopped}). cancel_export stops
        it after the frame being saved.

        With `folder`, each picture is also copied there under a plain name ("Roll 2026-11-23 03.jpg"), skipped
        ones too, so exporting the same roll to a new USB stick fills it; the event says where ('copy')."""
        if self.exporting and self.exporting.is_alive():
            raise Problem('An export is already running.')
        work = [dict(master=str(i['master']), settings=i.get('settings') or {}, stock=i.get('stock')) for i in items]
        if not work:
            raise Problem('Nothing to export: no frames were chosen.')
        if folder is not None and not Path(folder).is_dir():
            raise Problem(f'{folder} isn’t there. If it’s on a drive, plug the drive in, or choose somewhere else.')
        self.stop_export = threading.Event()
        stop = self.stop_export
        log = self.export_log = dict(started_utc=_utc(), total=len(work), tiff=tiff, done=None,
                                     items=[dict(master=i['master'], status='waiting') for i in work])

        def run():
            counts = dict(saved=0, skipped=0, failed=0)
            spent = 0.                    # seconds on frames actually saved: skipped ones take none
            for n, item in enumerate(work, 1):
                if stop.is_set():
                    break
                ev = dict(event='export', index=n, total=len(work), master=item['master'])
                began = time.monotonic()
                try:
                    ev.update(self._export_one(item, tiff, redo, folder))
                except (Problem, destinations.Problem, ValueError, OSError) as e:
                    ev.update(status='failed', message=str(e))
                except Exception as e:   # a bug: say so for this frame and go on to the next
                    traceback.print_exc(file=sys.stderr)
                    ev.update(status='failed', message=f'Something failed in the back end ({type(e).__name__}: {e}).')
                counts[ev['status']] += 1
                if ev['status'] == 'saved':
                    spent += time.monotonic() - began
                # time left: the average saved frame times the frames left, once one has been saved (the rest may
                # be skipped and take none, so it's an upper bound: it can only come in early)
                ev['seconds_left'] = round(spent / counts['saved'] * (len(work) - n)) if counts['saved'] else None
                log['items'][n - 1] = {k: v for k, v in ev.items() if k not in ('event', 'index', 'total', 'seconds_left')}
                self.write(ev)
            log['done'] = dict(**counts, stopped=stop.is_set(), finished_utc=_utc())
            # Eject is offered only for a drive the roll isn't on: ejecting the one the app is reading from would fail
            self.write(dict(event='export-done', **counts, stopped=stop.is_set(), total=len(work), folder=folder,
                            removable=_removable(folder) and not _same_drive(folder, work[0]['master'])))
        self.exporting = threading.Thread(target=run, daemon=True)
        self.exporting.start()
        return dict(started=True, total=len(work))

    def changed(self, items):
        """For each frame ({master, settings, stock}): True when it has a saved picture but none made with these
        adjustments (it was changed since saving), False when one matches, None when it was never saved."""
        out = {}
        for i in items:
            master = Path(i['master'])
            roll_dir = roll.containing(master)
            if not roll_dir or not any((roll_dir / 'derived').glob(f'{master.stem}-v*.json')):
                out[str(i['master'])] = None
                continue
            s = self._settings(i.get('settings'))
            turn, crop_px = self._plan(master, s, i.get('stock'))
            out[str(i['master'])] = not any(_already_saved(roll_dir, master, s, i.get('stock'), suffix, turn, crop_px)
                                            for suffix in ('.jpg', '.tif'))
        return out

    def cancel_export(self):
        if self.exporting and self.exporting.is_alive():
            self.stop_export.set()
            return dict(cancelled=True)
        return dict(cancelled=False)

    # --- the Queue tab -------------------------------------------------------------------
    def work(self):
        """The Queue tab's "On this computer": the export running now, or the last one, frame by frame
        (status waiting, saved, skipped or failed); and the shared queues set up in hosts.toml, by name."""
        log = self.export_log
        running = bool(self.exporting and self.exporting.is_alive())
        return dict(export=None if log is None else dict(log, running=running, items=[dict(i) for i in log['items']]),
                    queues=_queue_names())

    def queue_status(self, name=None):
        """A shared queue (`openfilmkit queue status`): its jobs and the machines working on them. Reading it
        reaches the queue's machine over SSH, so it can take a few seconds; it runs beside other requests."""
        from . import queues
        q = _queue(name)
        try:
            info = queues.status(q)
        except queues.QueueError as e:
            raise Problem(str(e)) from e
        jobs = [dict(id=r['id'], short=r['id'][:12], photo=Path(r['photo']).name if r.get('photo') else None,
                     photo_path=r.get('photo'), output=r.get('output'), state=r['state'], worker=r.get('worker'),
                     error=r.get('error'), why_waiting=r.get('why_waiting'), tries=r.get('tries'),
                     tries_left=(queues.TRIES - r['tries']) if r['state'] == 'failed' and not r.get('final', True) else 0)
                for r in info['jobs']]
        workers = [dict(name=w['name'], seen=f"not seen for {queues.ago(w['seen_ago_s'])}" if w['silent']
                        else f"seen {queues.ago(w['seen_ago_s'])} ago", silent=w['silent'], running=w.get('running'),
                        memory_gb=w.get('memory_gb')) for w in info['workers']]
        counts = {k: sum(j['state'] == k for j in jobs) for k in ('waiting', 'running', 'done', 'failed', 'stopped', 'damaged')}
        return dict(queue=info['queue'], where=q['queue'], jobs=jobs, workers=workers, counts=counts)

    def queue_collect(self, name=None):
        """Bring finished jobs back beside their originals and clear them from the queue (`queue collect`)."""
        from . import queues
        try:
            records = queues.collect(_queue(name), say=lambda *a: None)
        except queues.QueueError as e:
            raise Problem(str(e)) from e
        return dict(collected=[r['output'] for r in records if 'error' not in r],
                    failed=[dict(id=r['id'][:12], error=r['error']) for r in records if 'error' in r])

    def queue_retry(self, name=None, jobs=None):
        """Have the workers try failed jobs again: the IDs given, or every failed one (`queue retry`)."""
        from . import queues
        try:
            return dict(retried=queues.retry(_queue(name), jobs or None, say=lambda *a: None))
        except queues.QueueError as e:
            raise Problem(str(e)) from e

    def queue_remove(self, name=None, jobs=None):
        """Take jobs out of the queue, whatever their state (`queue remove`). Needs the IDs: never all of them
        by default."""
        from . import queues
        if not jobs:
            raise Problem('Choose which jobs to take out of the queue.')
        try:
            return dict(removed=queues.remove(_queue(name), jobs, say=lambda *a: None))
        except queues.QueueError as e:
            raise Problem(str(e)) from e

    def places(self, also=None):
        """Where an export could go: folders in `also` still there (the last one used), Pictures, then the drives
        plugged in, sticks and cards first."""
        return dict(places=destinations.places(also or ()))

    def eject(self, path):
        try:
            return destinations.eject(path)
        except destinations.Problem as e:
            raise Problem(str(e)) from e

    def _export_one(self, item, tiff, redo, folder=None):
        out = self._derive(item, tiff, redo)
        if folder is not None and out.get('output'):
            master = Path(item['master'])
            roll_dir = roll.containing(master)
            try:
                name = roll.load(roll_dir).get('name') or roll_dir.name
            except roll.RollError:
                name = roll_dir.name
            out['copy'] = str(destinations.copy_out(out['output'], folder,
                                                   destinations.plain_name(name, master, Path(out['output']).suffix)))
        return out

    def _derive(self, item, tiff, redo):
        master = Path(item['master'])
        roll_dir = roll.containing(master)
        if not roll_dir:
            raise Problem(f'{master.name} is not in a roll folder, so there is nowhere to save it.')
        s = self._settings(item['settings'])
        turn, crop_px = self._plan(master, s, item['stock'])
        suffix = '.tif' if tiff else '.jpg'
        if not redo:
            done = _already_saved(roll_dir, master, s, item['stock'], suffix, turn, crop_px)
            if done:
                return dict(status='skipped', output=str(done), message='Already saved with these adjustments')
        output = roll.next_derived(roll_dir, master.stem, suffix)
        record = conv.convert(self._args(master, s, item['stock'], output, tiff, turn, crop_px))
        return dict(status='saved', output=record['output'], warnings=record['channel_health']['warnings'])

    # --- the scanner ------------------------------------------------------------------
    def _kit(self, *args):
        return [sys.executable, '-m', 'openfilmkit', *args]

    def doctor(self):
        p = subprocess.run(self._kit('doctor', '--json'), capture_output=True, text=True, timeout=60)
        try:
            info = json.loads(p.stdout) if p.stdout.strip() else None
        except ValueError:
            info = None
        return dict(ok=p.returncode == 0, info=info, message=(p.stderr.strip().splitlines() or [''])[-1])

    def scan(self, target, dpi=None, note=''):
        """Scan one frame into the roll `target` in the background. Events: 'scan-progress' {fraction,
        seconds_left} as the scanner reports it, 'scan' {line} for what else it says, and at the end
        'scan-done' {ok, stopped, output, message (why it failed, in plain words), warnings (plain words
        about the saved frame), empty (the frame looks like no film), result (the full JSON)}."""
        if self.scan_proc and self.scan_proc.poll() is None:
            raise Problem('A scan is already running.')
        cmd = self._kit('scan', str(target), '--json') + (['--dpi', str(int(dpi))] if dpi else []) + (
            ['--note', note] if note else [])
        self.scan_stopping = False
        # The scan holds the read end of a pipe that only this process writes to (and never does): if the back end
        # goes, however it goes (the app quit, a crash, a kill), the scan sees the pipe close and stops, rather
        # than running on unseen with the scanner busy. See cli.stop_when_parent_goes.
        self.scan_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                          stdin=subprocess.PIPE, env={**os.environ, 'OPENFILMKIT_STOP_WITH_PARENT': '1'})
        proc, started = self.scan_proc, time.monotonic()

        def follow():
            said, sent = [], -1.0
            for line in proc.stderr:   # text mode: scanimage's "Progress: 12.3%\r" arrives a line at a time
                line = line.strip()
                if not line:
                    continue
                m = PROGRESS_LINE.match(line)
                if m:
                    fraction = min(1.0, float(m[1]) / 100)
                    if fraction - sent >= .01 or fraction == 1:
                        sent, spent = fraction, time.monotonic() - started
                        left = round(spent * (1 - fraction) / fraction) if fraction >= .05 and spent >= 3 else None
                        self.write(dict(event='scan-progress', fraction=round(fraction, 3), seconds_left=left))
                    continue
                said.append(line)
                self.write(dict(event='scan', line=line))
            out = proc.stdout.read()
            proc.wait()
            proc.stdin.close()   # the scan's "the app is still here" pipe, no longer needed
            try:
                result = json.loads(out) if out.strip() else None
            except ValueError:
                result = None
            self.write(dict(event='scan-done', code=proc.returncode, result=result,
                            **scan_outcome(proc.returncode, result, said, self.scan_stopping)))
        threading.Thread(target=follow, daemon=True).start()
        return dict(started=True)

    def cancel_scan(self):
        if self.scan_proc and self.scan_proc.poll() is None:
            self.scan_stopping = True
            self.scan_proc.terminate()
            return dict(cancelled=True)
        return dict(cancelled=False)

    def hello(self):
        return dict(version=__version__, python=sys.version.split()[0], home=str(Path.home()))


PROGRESS_LINE = re.compile(r'^Progress:\s*([\d.]+)%$')
NOTICEABLE = .02   # share of a channel lost at 0 or full scale from which the app mentions it (health.py's own
                   # threshold, .001, is for the scan log and the command line)
CLAMPED = ('One color lost some detail in this scan, a known fault of this scanner. If it shows on every frame, '
           'the scanner needs its color offsets adjusted.')
TOO_BRIGHT = 'Part of this scan was brighter than the scanner can record, so detail there is lost.'
GAPPY = ('This scan holds fewer shades than it should, so smooth areas like sky may show banding if you '
         'change them a lot.')


def plain_health(report):
    """health.check()'s findings as the app says them: plain words, once each, and only what's large
    enough to see. The exact figures stay in the scan log and in Details."""
    channels = (report or {}).get('channels') or {}
    out = []
    if any(c.get('at_zero', 0) > NOTICEABLE for c in channels.values()):
        out.append(CLAMPED)
    if any(c.get('at_full_scale', 0) > NOTICEABLE for c in channels.values()):
        out.append(TOO_BRIGHT)
    if any((c.get('missing_bits') or 0) >= health_costly() for c in channels.values()):
        out.append(GAPPY)
    return out


def health_costly():
    return conv.scan_health.COSTLY


EMPTY_FRAME = ('this frame looks empty: the scanner saw straight through it. If the strip has ended, take the '
               'holder out; if not, slide it on and scan again.')


def scan_outcome(code, result, said, stopped):
    """What the app tells someone about a finished scan: ok, stopped, the saved file, why it failed in plain
    words, and plain warnings about the frame (empty, or a scanner fault). The kit's own lines stay in 'said'."""
    capture = ((result or {}).get('captures') or [None])[0] or {}
    ok = code == 0 and capture.get('status') == 'captured_verified'
    out = dict(ok=ok, stopped=bool(stopped) and not ok, output=capture.get('output') if ok else None,
               message=None, warnings=[], empty=False)
    if ok:
        channels = (capture.get('channel_health') or {}).get('channels') or {}
        if channels and min(c.get('median', 0) for c in channels.values()) > conv.EMPTY * 65535:
            out['empty'] = True
            out['warnings'].append(EMPTY_FRAME[0].upper() + EMPTY_FRAME[1:])
        else:
            out['warnings'] += plain_health({'channels': channels})
        return out
    error = capture.get('error') or (result or {}).get('error') or ''
    text = ' '.join([error, *said]).lower()
    if stopped:
        out['message'] = 'You stopped the scan, so nothing was saved. This frame can be scanned again.'
    elif 'open of device' in text and 'device busy' in text:
        out['message'] = ('Another program is using the scanner. Close it, then scan this frame again.')
    elif 'open of device' in text:   # the kit has already tried a second time (capture.OPEN_RETRIES)
        out['message'] = ('The scanner didn’t answer when the scan started. Switch it off and on, wait ten '
                          'seconds, then scan this frame again.')
    elif 'device i/o' in text or 'status 9' in text:
        out['message'] = ('The scanner stopped partway through. Check that its cable is in and the holder can move '
                          'freely, then scan this frame again.')
    elif 'said nothing and written nothing' in text:
        out['message'] = 'The scanner stopped responding. Switch it off and on, then scan this frame again.'
    elif 'no scanner found' in text or 'no scanner named' in text:
        out['message'] = 'The scanner isn’t connected. Check that it’s plugged in and switched on, then try again.'
    elif 'gib free' in text:
        out['message'] = 'There isn’t room on the drive for another scan. Free some space or choose another drive.'
    elif 'not mounted' in text:
        out['message'] = 'The drive this roll is on isn’t connected. Plug it back in, then try again.'
    elif 'smaller than a full frame' in text:
        out['message'] = 'The scan came out smaller than a whole frame. Check the holder is all the way in, then scan again.'
    else:
        out['message'] = 'The scan didn’t finish' + (f': {error}' if error else '. The scanner messages say more.')
    return out


def _same_drive(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _removable(folder):
    """Whether `folder` is on a drive that can be ejected (from the same list Export offers)."""
    if not folder:
        return False
    try:
        here = Path(folder).resolve()
        return any(d['removable'] and (here == Path(d['path']).resolve() or Path(d['path']).resolve() in here.parents)
                   for d in destinations.places())
    except OSError:
        return False


METHODS = ('hello', 'open', 'stocks', 'new_roll', 'thumb', 'preview', 'save', 'preview_region', 'details', 'changed', 'export', 'cancel_export', 'places', 'eject', 'work', 'queue_status', 'queue_collect', 'queue_retry', 'queue_remove', 'doctor', 'scan', 'cancel_scan')

SLOW = ('places', 'eject', 'preview_region', 'queue_status', 'queue_collect', 'queue_retry', 'queue_remove')


def serve(stdin=sys.stdin, stdout=sys.stdout):
    lock = threading.Lock()

    def write(obj):
        with lock:
            stdout.write(json.dumps(obj, default=str) + '\n')
            stdout.flush()

    service = Service(write)
    slow = []
    for line in stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError:
            write(dict(id=None, error='The app sent a request the back end could not read.'))
            continue
        rid, method, params = req.get('id'), req.get('method'), req.get('params') or {}
        if method not in METHODS:
            write(dict(id=rid, error=f'Unknown request: {method}'))
            continue
        if method in SLOW:   # over the network: answered when done, without holding up previews meanwhile
            t = threading.Thread(target=answer, args=(service, rid, method, params, write), daemon=True)
            t.start()
            slow.append(t)
        else:
            answer(service, rid, method, params, write)
    for t in slow:   # the app closed its end: answer what was asked before going
        t.join()


def answer(service, rid, method, params, write):
    try:
        write(dict(id=rid, result=getattr(service, method)(**params)))
    except Problem as e:
        write(dict(id=rid, error=str(e)))
    except Exception as e:   # anything else is a bug: keep serving, say so, log the details
        traceback.print_exc(file=sys.stderr)
        write(dict(id=rid, error=f'Something failed in the back end ({type(e).__name__}: {e}).', detail=traceback.format_exc()))


if __name__ == '__main__':
    serve()
