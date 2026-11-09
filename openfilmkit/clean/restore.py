"""Clean-up of converted film scans: film-holder crop, color cast correction, dust removal.

Ported from the author's earlier archive clean-up tools; the algorithms are unchanged. Only a
per-photo no-crop list, which named two archive photos, is gone: use
`openfilmkit clean --no-crop` instead.

Every function takes and returns an RGB numpy array, and sizes scale with the image, so the same
code tunes on 3000 px copies and runs on the 4800 dpi originals. Detection works on 8-bit images.
The adjustments (color, straightening, shadows, dust fill) also take 16-bit images: they measure on
an 8-bit copy (to8), so they decide exactly as they would at 8 bits, and apply at full depth. Nothing here guesses at
missing picture: dust is filled only from its immediate surroundings, faces and the camera date
stamp are never touched by it, and lines running through the picture (not touching an edge) are
left alone by the crop.
"""
import cv2
import numpy as np


BAND = 1024   # rows per strip for full-image filters: keeps a 73 MP scan's temporaries small


def by_bands(fn, h, margin, band=BAND):
    """Yield (y0, y1, fn(a, b) cropped to rows y0:y1) for full-width strips. fn(a, b) works on rows
    a:b, with `margin` extra rows above and below (where the image has them) so that a filter that
    reaches `margin` rows sees the same pixels as on the whole image, and rows y0:y1 come out the
    same."""
    for y0 in range(0, h, band):
        y1 = min(h, y0 + band)
        a, b = max(0, y0 - margin), min(h, y1 + margin)
        yield y0, y1, fn(a, b)[y0 - a:y0 - a + (y1 - y0)]


def grain_level(gray):
    """Robust std of a photo's fine detail (mostly grain): 1.4826 x the median |high-pass| over every
    other pixel. Computed in strips, so the whole photo is never held as floats."""
    h = gray.shape[0]
    parts = []
    for y0, y1, fine in by_bands(lambda a, b: fine_detail(gray[a:b]), h, 8):
        parts.append(np.abs(fine[(-y0) % 2::2, ::2]).ravel())   # every other row of the whole image
    return 1.4826 * float(np.median(np.concatenate(parts)))


def fine_detail(gray):
    """Fine detail: the gray image minus a light blur (sigma 1.5; it reaches 6 pixels)."""
    gf = gray.astype(np.float32)
    return gf - cv2.GaussianBlur(gf, (0, 0), 1.5)


TO8 = ((np.arange(65536) + 128) // 257).astype(np.uint8)   # rint(v / 257) for every 16-bit value


def to8(img):
    """An 8-bit copy of a 16-bit image (65535 / 255 = 257 exactly); 8-bit images pass through.
    Made in strips: dividing a 73 MP scan at once took 3.5 GB of floats."""
    if img.dtype == np.uint8:
        return img
    out = np.empty(img.shape, np.uint8)
    for y0 in range(0, img.shape[0], BAND):
        out[y0:y0 + BAND] = TO8[img[y0:y0 + BAND]]
    return out


def in_strips(fn, img):
    """fn applied to full-width strips of img, for per-pixel work (the same result as on the whole
    image, without whole-image float copies)."""
    first = fn(img[:BAND])
    out = np.empty(img.shape[:1] + first.shape[1:], first.dtype)
    out[:BAND] = first
    for y0 in range(BAND, img.shape[0], BAND):
        out[y0:y0 + BAND] = fn(img[y0:y0 + BAND])
    return out


# ---------------------------------------------------------------- crop

def _is_strip(line):
    """Film-holder light: at least two channels clipped near 255 along most of the line (a white or
    cyan/blue band where light passed the holder), not a merely bright or flat part of the picture."""
    med = np.median(line, axis=0)
    clipped = int((med >= 245).sum())
    close = np.mean(np.abs(line.astype(np.int16) - med).max(axis=1) <= 22)
    return (clipped == 3 and close >= 0.85) or (clipped >= 2 and close >= 0.35)


def _edge_run(lines, sliver):
    """Lines from the edge to the inner end of a holder strip, or 0. The strip may begin after a
    sliver (up to `sliver` lines) of the neighboring frame, and must end in a sharp change."""
    flags = [_is_strip(l) for l in lines]
    start = next((i for i, f in enumerate(flags[:sliver + 1]) if f), None)
    if start is None:
        return 0
    end = start
    while end < len(flags) and flags[end]:
        end += 1
    if end >= len(flags) or end - start < 3:
        return 0
    # the strip's inner edge is often blurred over a few dozen lines: look just past the blur
    strip = np.median(np.concatenate(lines[start:end]), axis=0)
    after = lines[end + 15:end + 45]
    if len(after) < 10 or np.abs(strip - np.median(np.concatenate(after), axis=0)).max() < 40:
        return 0
    # crop past the blurred edge too: up to where the picture settles
    settle = end
    while settle < min(len(lines), end + 45) and np.median(lines[settle], axis=0).min() >= 200:
        settle += 1
    return settle


def find_crop(img, max_fraction=0.08, margin_fraction=0.002, sliver_fraction=0.01):
    """(left, top, right, bottom) inside any film-holder strip at an edge (see _edge_run).

    Never more than max_fraction of a side, never a dark edge (too often real background), and
    never a line that runs through the picture away from the edge."""
    h, w = img.shape[:2]
    mx, my = int(w * max_fraction), int(h * max_fraction)
    sx, sy = max(1, int(w * sliver_fraction)), max(1, int(h * sliver_fraction))
    left = _edge_run([img[:, x] for x in range(mx)], sx)
    right = _edge_run([img[:, w - 1 - x] for x in range(mx)], sx)
    top = _edge_run([img[y] for y in range(my)], sy)
    bottom = _edge_run([img[h - 1 - y] for y in range(my)], sy)
    pad_x, pad_y = int(w * margin_fraction), int(h * margin_fraction)
    l = left + pad_x if left else 0
    t = top + pad_y if top else 0
    r = w - (right + pad_x) if right else w
    b = h - (bottom + pad_y) if bottom else h
    return l, t, r, b


# ---------------------------------------------------------------- date stamp and protection

def stamp_mask(img):
    """Pixels of the camera's LED date stamp (plus a margin), to leave untouched: a bright
    orange-red stamp, or a faint one (see faint_stamp)."""
    return bright_stamp(img) | faint_stamp(img)


def bright_stamp(img):
    """A bright orange-red date stamp, the usual look on prints.

    A stamp is a short line of small digits in a corner: orange-red pixels in the top or bottom
    quarter, grouped along the line, each group low, not too wide and lying wholly in the left or
    right 40 %. Taking every orange pixel in the outer bands instead protected a Navy portrait's
    gold cap insignia, ribbon bar, buttons and glasses (28,596 pixels) from dust removal."""
    r, g, b = [img[..., i].astype(np.int16) for i in range(3)]
    m = ((r > 170) & (r - b > 110) & (g > 50) & (g < r - 30) & (b < 140)).astype(np.uint8)
    h, w = m.shape
    m[int(h * 0.25):int(h * 0.75)] = 0
    if not m.any():
        return m > 0
    gap = max(3, int(min(h, w) * 0.02))   # join the digits of one line, not separate objects
    n, lab, st, _ = cv2.connectedComponentsWithStats(
        cv2.dilate(m, np.ones((max(1, gap // 3), gap), np.uint8)), connectivity=8)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        x, y, bw, bh, _ = st[i]
        in_corner = x + bw <= 0.40 * w or x >= 0.60 * w
        keep[i] = in_corner and bh <= 0.08 * h and bw <= 0.45 * w
    m &= keep[lab].astype(np.uint8)
    k = max(3, int(min(h, w) * 0.012))
    return cv2.dilate(m, np.ones((k, k), np.uint8)) > 0


def faint_stamp(img):
    """A date stamp printed faintly over the picture, as on many negative conversions and some
    prints: its seven-segment digits are thin bars only a little brighter and warmer than what is
    behind them, so the dust pass took them for specks and nibbled them away.

    Warm bars alone are everywhere (brick paving, foliage, siding), so the bars must make digits:
    horizontal ones at the top, middle or bottom of a digit-sized box and upright ones at its
    sides, at least three such digits of one size in a row, in a corner. A print turned sideways
    has its stamp running down the side, so the side quarters are checked turned a quarter turn.
    A camera prints its stamp at a fixed size on the frame, so the search runs on a copy whose
    short side is STAMP_SIDE pixels whatever the image's resolution, and the result is scaled back.
    On 118 prints and conversions this found 35 stamps, 11 of them missed by bright_stamp, and
    nothing else."""
    h, w = img.shape[:2]
    s = STAMP_SIDE / min(h, w)
    if s == 1:
        return faint_stamp_at(img)
    work = cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))),
                      interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    return cv2.resize(faint_stamp_at(work).astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0


STAMP_SIDE = 1600


def faint_stamp_at(img):
    """faint_stamp at the image's own size."""
    h, w = img.shape[:2]
    short = min(h, w)
    out = np.zeros((h, w), np.uint8)
    for a, b in ((0, h // 4), (h - h // 4, h)):        # top and bottom quarters
        box = _stamp_line(img[a:b], short, w)
        if box:
            x0, y0, x1, y1 = box
            out[a + y0:a + y1, x0:x1] = 1
    for a, b in ((0, w // 4), (w - w // 4, w)):        # left and right quarters, turned upright
        box = _stamp_line(np.ascontiguousarray(np.rot90(img[:, a:b])), short, h)
        if box:                                         # turned (r, c) is (row c, column b - 1 - r)
            x0, y0, x1, y1 = box
            out[x0:x1, b - y1:b - y0] = 1
    k = max(3, int(short * 0.012))
    return cv2.dilate(out, np.ones((k, k), np.uint8)) > 0


def _warm_bars(band, short):
    """(x, y, w, h) of short thin bars a little brighter and warmer (more red than blue) than the
    median round them. Worked out in strips."""
    sig = max(1.0, short * 0.0004)
    k = max(5, int(short * 0.008) | 1)

    def warmer(a, b):
        sm = cv2.GaussianBlur(band[a:b].astype(np.float32), (0, 0), sig)
        bg = np.dstack([cv2.medianBlur(np.clip(sm[..., c], 0, 255).astype(np.uint8), k) for c in range(3)])
        d = sm - bg
        return ((d[..., 0] > 10) & (d[..., 0] - d[..., 2] > 7) & (d[..., 1] > 3)).astype(np.uint8)
    cand = np.empty(band.shape[:2], np.uint8)
    for y0, y1, part in by_bands(warmer, band.shape[0], int(4 * sig) + 2 + k // 2):
        cand[y0:y1] = part
    n, _, st, _ = cv2.connectedComponentsWithStats(cand, connectivity=8)
    bars = []
    for x, y, bw, bh, _ in st[1:]:
        long_, thin = max(bw, bh), min(bw, bh)
        if short * 0.004 <= long_ <= short * 0.025 and thin <= short * 0.006 and long_ >= 1.8 * thin:
            bars.append((int(x), int(y), int(bw), int(bh)))
    return bars


def _digits(bars, short):
    """Bars grouped into seven-segment digits: [(x, y, w, h, bars, stroke)], stroke being the
    median bar thickness."""
    m = np.zeros((max(y + bh for x, y, bw, bh in bars) + 2, max(x + bw for x, y, bw, bh in bars) + 2), np.uint8)
    for x, y, bw, bh in bars:
        m[y:y + bh, x:x + bw] = 1
    gy, gx = max(3, int(short * 0.014)), max(3, int(short * 0.006))   # bridges a missing segment, not two digits
    n, _, st, _ = cv2.connectedComponentsWithStats(cv2.dilate(m, np.ones((gy, gx), np.uint8)), connectivity=8)
    digits = []
    for X, Y, W, H, _ in st[1:]:
        X, Y, W, H = X + gx // 2, Y + gy // 2, W - gx, H - gy
        mine = [b for b in bars if X <= b[0] + b[2] / 2 <= X + W and Y <= b[1] + b[3] / 2 <= Y + H]
        one = all(b[3] > b[2] for b in mine)   # a 1 is two upright bars, as narrow as one
        if len(mine) < 2 or not short * 0.012 <= H <= short * 0.06 or not (0.2 * H <= W or one) or W > 0.85 * H:
            continue
        fit, slots = 0, set()
        for x, y, bw, bh in mine:
            cx, cy = (x + bw / 2 - X) / max(W, 1), (y + bh / 2 - Y) / H
            if bw > bh:   # across: at the top, middle or bottom
                row = min(range(3), key=lambda r: abs(cy - r / 2))
                if abs(cy - row / 2) <= 0.15 and bw >= 0.25 * W:
                    fit += 1
                    slots.add(('across', row))
            elif (cx <= 0.35 or cx >= 0.65 or W < 0.35 * H) and 0.15 * H <= bh <= 0.6 * H:   # upright, at a side
                fit += 1
                slots.add(('upright', cx >= 0.5, cy >= 0.5))
        if fit >= 0.8 * len(mine) and len(slots) >= 2:
            digits.append((X, Y, W, H, len(mine), float(np.median([min(b[2], b[3]) for b in mine]))))
    return digits


def _stamp_line(band, short, width):
    """(x0, y0, x1, y1) of a faint date stamp's line of digits in a top or bottom band, or None."""
    bars = _warm_bars(band, short)
    if not bars:
        return None
    digits = sorted(_digits(bars, short))
    line = []
    for i, d in enumerate(digits):   # digits of one height and stroke, tops aligned, close in a row
        row = [d]
        for e in digits[i + 1:]:
            if (abs(e[3] - d[3]) <= 0.25 * d[3] and abs(e[1] - d[1]) <= 0.2 * d[3]
                    and max(e[5], d[5]) <= 2 * min(e[5], d[5])
                    and 0 <= e[0] - (row[-1][0] + row[-1][2]) <= 2.2 * d[3]):
                row.append(e)
        if len(row) >= 2 and sum(e[4] for e in row) >= 7 and len(row) > len(line):
            line = row
    if not line:
        return None
    x0, x1 = min(d[0] for d in line), max(d[0] + d[2] for d in line)
    y0, y1 = min(d[1] for d in line), max(d[1] + d[3] for d in line)
    dh, stroke = y1 - y0, float(np.median([d[5] for d in line]))
    lo, hi, grew = x0 - 2 * dh, x1 + 2 * dh, True
    while grew:   # a fainter digit at either end: bars of the same stroke in the line's band, close by
        grew = False
        for x, y, bw, bh in bars:
            if (y0 - 0.1 * dh <= y + bh / 2 <= y1 + 0.1 * dh and x0 - 0.6 * dh <= x + bw / 2 <= x1 + 0.6 * dh
                    and lo <= x and x + bw <= hi and stroke / 2 <= min(bw, bh) <= stroke * 2
                    and (x < x0 or x + bw > x1)):
                x0, x1, grew = min(x0, x), max(x1, x + bw), True
    if not (x1 <= 0.40 * width or x0 >= 0.60 * width):   # a stamp sits in a corner
        return None
    return x0, y0, x1, y1

def feature_mask(shape, faces):
    """Eyes and mouth of each face (bright catchlights and teeth are real), as discs sized from the
    face. `faces`: [{'box': [x0, y0, x1, y1], 'kps': [[x, y] x 5]}] in pixels (InsightFace order:
    left eye, right eye, nose, left mouth corner, right mouth corner). The rest of the face is open
    to dust removal."""
    m = np.zeros(shape[:2], np.uint8)
    for f in faces:
        x0, y0, x1, y1 = f['box']
        fw = max(1.0, x1 - x0)
        kps = f.get('kps')
        if not kps:
            m[max(0, int(y0)):int(y1), max(0, int(x0)):int(x1)] = 1   # no landmarks: protect whole face
            continue
        le, re, _, lm, rm = [(int(x), int(y)) for x, y in kps]
        for c in (le, re):
            cv2.circle(m, c, int(fw * 0.10), 1, -1)
        mc = ((lm[0] + rm[0]) // 2, (lm[1] + rm[1]) // 2)
        half = max(4, int(np.hypot(rm[0] - lm[0], rm[1] - lm[1]) * 0.75))
        cv2.ellipse(m, mc, (half, int(half * 0.6)), 0, 0, 360, 1, -1)
    return m > 0


def box_mask(shape, boxes, pad=0.5):
    m = np.zeros(shape[:2], bool)
    for x0, y0, x1, y1 in boxes:
        dx, dy = (x1 - x0) * pad, (y1 - y0) * pad
        m[max(0, int(y0 - dy)):int(y1 + dy), max(0, int(x0 - dx)):int(x1 + dx)] = True
    return m


# ---------------------------------------------------------------- color

def color_cast(img):
    """Mean a*, b* (Lab, 0 = neutral) of the image's near-neutral pixels: a measure of cast."""
    small = cv2.resize(img, (400, int(400 * img.shape[0] / img.shape[1])), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(small, cv2.COLOR_RGB2LAB).astype(np.float32)
    a, b = lab[..., 1] - 128, lab[..., 2] - 128
    neutral = (np.hypot(a, b) < 25) & (lab[..., 0] > 40) & (lab[..., 0] < 235)
    if neutral.sum() < 200:
        neutral = np.ones_like(neutral)
    return float(a[neutral].mean()), float(b[neutral].mean())


def correct_color(img, exclude=None, strength=0.8, low=0.5, high=99.5, max_gain_spread=0.35):
    """Per-channel levels (black and white point from percentiles) blended with the original.

    Removes the scanner's cast and lifts washed-out scans. `exclude` (bool mask) keeps the date
    stamp and similar out of the statistics. Channel gains are kept within max_gain_spread of each
    other so a genuinely warm or cool scene is not forced gray."""
    small = to8(img)
    sample = small if exclude is None else small[~exclude]
    sample = sample.reshape(-1, 3)
    if len(sample) > 400_000:
        sample = sample[:: len(sample) // 400_000]
    lo = np.percentile(sample, low, axis=0)
    hi = np.percentile(sample, high, axis=0)
    gain = 255.0 / np.maximum(hi - lo, 1)
    g_mid = np.median(gain)
    gain = np.clip(gain, g_mid * (1 - max_gain_spread), g_mid * (1 + max_gain_spread))
    lo = np.clip(lo, 0, 60)
    scale = 1.0 if img.dtype == np.uint8 else 257.0   # the 8-bit levels, at the image's depth

    def levels(part):
        out = (part.astype(np.float32) - lo * scale) * gain
        out = part.astype(np.float32) * (1 - strength) + out * strength
        return np.clip(out, 0, 255 * scale).astype(img.dtype)
    return in_strips(levels, img)


def neutralize_cast(img, exclude=None, strength=0.7, cap=10.0):
    """Shift color (Lab a*, b*) so the photo's near-neutral areas move toward gray.

    The cast is measured on low-chroma, mid-brightness pixels (walls, whites, grays); the shift is
    a fraction (`strength`) of it and never more than `cap` Lab units, so genuinely colored scenes
    keep most of their color."""
    small = to8(img)
    probe = small if exclude is None else np.where(exclude[..., None], 128, small).astype(np.uint8)
    a_m, b_m = color_cast(probe)
    shift = -strength * np.array([a_m, b_m])
    n = float(np.hypot(*shift))
    if n > cap:
        shift *= cap / n

    def shifted(part):
        if img.dtype != np.uint8:   # float Lab has the same a*, b* units as 8-bit Lab (offset by 128)
            lab = cv2.cvtColor(part.astype(np.float32) / 65535, cv2.COLOR_RGB2LAB)
            lab[..., 1] += shift[0]
            lab[..., 2] += shift[1]
            return np.rint(np.clip(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB), 0, 1) * 65535).astype(np.uint16)
        lab = cv2.cvtColor(part, cv2.COLOR_RGB2LAB).astype(np.float32)
        lab[..., 1] += shift[0]
        lab[..., 2] += shift[1]
        return cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)
    return in_strips(shifted, img), (float(shift[0]), float(shift[1]))


def fix_color(img, exclude=None):
    """Levels per channel (lifts washed-out scans, removes channel offsets), then cast removal."""
    leveled = correct_color(img, exclude=exclude, strength=0.6)
    return neutralize_cast(leveled, exclude=exclude)


# ---------------------------------------------------------------- manual adjustments

def straighten(img, degrees):
    """Rotate by `degrees` (positive = counter-clockwise) and crop to the largest upright rectangle
    with no empty corners."""
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    rotated = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
    a = abs(np.radians(degrees))
    # largest axis-aligned rectangle inside a w x h rectangle rotated by a (same aspect handling)
    sin_a, cos_a = np.sin(a), np.cos(a)
    if w <= 2 * sin_a * cos_a * h or h <= 2 * sin_a * cos_a * w:
        side = min(w, h)
        x = 0.5 * side
        cw, ch = (x / sin_a, x / cos_a) if w >= h else (x / cos_a, x / sin_a)
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        cw, ch = (w * cos_a - h * sin_a) / cos_2a, (h * cos_a - w * sin_a) / cos_2a
    cw, ch = int(cw) - 2, int(ch) - 2
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    return rotated[y0:y0 + ch, x0:x0 + cw]


def lift_shadows(img, gamma=0.85, amount=1.0):
    """Brighten dark and middle tones (gamma below 1) without moving black and white points."""
    if img.dtype != np.uint8:
        lut = np.linspace(0, 1, 65536) ** gamma * 65535
        lut = np.clip(np.arange(65536) * (1 - amount) + lut * amount, 0, 65535).astype(np.uint16)
        return in_strips(lambda part: lut[part], img)
    lut = (np.linspace(0, 1, 256) ** gamma * 255).astype(np.float32)
    lut = np.clip(np.arange(256) * (1 - amount) + lut * amount, 0, 255).astype(np.uint8)
    return cv2.LUT(img, lut)


# ---------------------------------------------------------------- dust

def dust_mask(img, protect=None, threshold=28, max_area_fraction=2.5e-5, smooth_std=14, max_crowd=5,
              texture_factor=2.5, grain_floor=1.5):
    """Small bright specks and hair-thin bright lines: dust and scratches on the negative.

    A pixel is a candidate when it is clearly brighter than a median-filtered copy of the image;
    connected candidates are kept when small (or thin), and only where the surroundings are smooth
    enough that a real highlight is unlikely. `protect` (bool mask) is never included.

    Smooth means two things. The median-filtered copy must vary little (no edges or shading), and
    the fine detail in a ring round the candidate must be no stronger than texture_factor times
    the photo's own grain: a glint on a strand of hair sits in fine, structured texture that the
    median filter hides, while dust on skin, a wall or sky sits in plain grain. On a 1984 NASA
    portrait the first test alone took 42 hair glints for dust; on full-resolution archive scans
    the second drops 2 of 537 detections. The limit follows the photo's grain, so a grainy scan
    is not left uncleaned."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    k = max(5, int(min(h, w) * 0.004) | 1)
    bg = cv2.medianBlur(gray, k)
    cand = ((gray.astype(np.int16) - bg) > threshold).astype(np.uint8)
    # smoothness of the surroundings: local std of the median-filtered image, leaving out the
    # candidates themselves. A speck about as wide as the median window leaks into the filtered
    # copy, and counted in, it made its own surroundings look busy: on a 960-pixel portrait that
    # turned away a quarter of dust-sized specks on plain ground. Worked out in strips.
    win = (k * 2 + 1, k * 2 + 1)

    def smoothness(a, b):
        bgf = bg[a:b].astype(np.float32)
        around = (cv2.dilate(cand[max(0, a - 1):b + 1], np.ones((3, 3), np.uint8))[a - max(0, a - 1):][:b - a]
                  == 0).astype(np.float32)
        count = np.maximum(cv2.blur(around, win), 1e-3)
        mean = cv2.blur(bgf * around, win) / count
        sq = cv2.blur(bgf * bgf * around, win) / count
        return np.sqrt(np.maximum(sq - mean * mean, 0))
    local_std = np.empty((h, w), np.float32)
    for y0, y1, part in by_bands(smoothness, h, k + 2):
        local_std[y0:y1] = part
    del bg
    # the photo's grain level, for the texture test below
    texture_limit = texture_factor * max(grain_level(gray), grain_floor)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(cand, connectivity=8)
    max_area = max(12, int(h * w * max_area_fraction))
    thin = max(2, int(min(h, w) * 0.0012))
    # Dust is sparse; bright detail in foliage, gravel or lace comes in crowds. Count candidates
    # around each one and leave crowded ones alone.
    cwin = max(31, int(min(h, w) * 0.04) | 1)
    cxs = np.clip(stats[1:, 0] + stats[1:, 2] // 2, 0, w - 1)
    cys = np.clip(stats[1:, 1] + stats[1:, 3] // 2, 0, h - 1)
    crowd_at = np.zeros(n - 1, np.float32)

    def crowding(a, b):
        centers = np.zeros((b - a, w), np.float32)
        inside = (cys >= a) & (cys < b)
        centers[cys[inside] - a, cxs[inside]] = 1
        return cv2.boxFilter(centers, -1, (cwin, cwin), normalize=False)
    for y0, y1, part in by_bands(crowding, h, cwin):
        here = (cys >= y0) & (cys < y1)
        crowd_at[here] = part[cys[here] - y0, cxs[here]]
    keep = np.zeros(n, bool)
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if crowd_at[i - 1] > max_crowd:
            continue
        speck = area <= max_area
        # thickness: the largest distance from its inside to its edge (worked out on its own box)
        ya, yb, xa, xb = max(0, y - 2), min(h, y + bh + 2), max(0, x - 2), min(w, x + bw + 2)
        thick = float(cv2.distanceTransform((labels[ya:yb, xa:xb] == i).astype(np.uint8), cv2.DIST_L2, 3).max())
        # fibers and hairs: thin along their whole length, straight or curled
        fiber = area <= max_area * 12 and thick <= thin and max(bw, bh) >= 4 * thick * 2
        short_line = False
        if fiber and not speck:
            # Long straight thin lines are as likely to be an edge in the scene (a fence rail, a
            # frame) as a scratch, so they stay. Curled fibers go, and so do short straight
            # scratches, which are checked below against smooth surroundings (skin, wall, sky).
            path = area / max(1.0, 2 * thick)
            if path < 1.4 * float(np.hypot(bw, bh)):
                if max(bw, bh) > min(h, w) * 0.02:
                    continue
                short_line = True
        if not (speck or fiber):
            continue
        ys, xs = np.nonzero(labels[y:y + bh, x:x + bw] == i)
        std_here = float(np.median(local_std[y + ys, x + xs]))
        limit = smooth_std if (speck or short_line) else smooth_std * 1.8
        if std_here > limit:
            continue
        r = max(bw, bh) // 2 + 9
        cx, cy = x + bw // 2, y + bh // 2
        ya, yb, xa, xb = max(0, cy - r), min(h, cy + r + 1), max(0, cx - r), min(w, cx + r + 1)
        comp = (labels[ya:yb, xa:xb] == i).astype(np.uint8)
        ring = ~(cv2.dilate(comp, np.ones((5, 5), np.uint8)) > 0) & (labels[ya:yb, xa:xb] == 0)
        fa, fb, fc, fd = max(0, ya - 8), min(h, yb + 8), max(0, xa - 8), min(w, xb + 8)
        fine = fine_detail(gray[fa:fb, fc:fd])[ya - fa:yb - fa, xa - fc:xb - fc]
        if ring.sum() >= 20 and float(fine[ring].std()) > texture_limit:
            continue
        keep[i] = True
    grow = max(1, int(min(h, w) * 0.0008))
    mask = cv2.dilate(keep[labels].astype(np.uint8), np.ones((2 * grow + 1, 2 * grow + 1), np.uint8)) > 0
    if protect is not None:   # after widening, so a speck beside a stamp digit doesn't take its edge
        mask &= ~protect
    return mask


def remove_dust(img, mask, out=None):
    """Fill the mask from its surroundings, into `out` (a copy of img by default; img itself is
    fine)."""
    return inpaint(img, mask, max(2, int(min(img.shape[:2]) * 0.0015)), out)


def inpaint(img, mask, radius, out=None):
    """cv2.inpaint(img, mask, radius, INPAINT_TELEA), into `out` (a copy of img by default; img
    itself is fine). 16-bit images are filled per channel, which at 8 bits equals filling the
    three channels at once.

    Telea looks no further than its radius from a hole, twice over (the distances it works out
    round a hole, then the pixels it averages), so each group of nearby holes is filled in a
    window round it: the same pixels as filling the whole image at once, without the whole
    image's work arrays (a 73 MP scan's took over a gigabyte)."""
    if out is None:
        out = img.copy()
    if not mask.any():
        return out
    reach = 2 * radius + 2
    holes = (mask > 0).astype(np.uint8) * 255
    groups, _ = cv2.findContours(cv2.dilate(holes, np.ones((2 * reach + 1, 2 * reach + 1), np.uint8)),
                                 cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for g in groups:
        x, y, bw, bh = cv2.boundingRect(g)   # the group and `reach` round it
        here = np.zeros((bh, bw), np.uint8)
        cv2.drawContours(here, [g], -1, 1, cv2.FILLED, offset=(-x, -y))
        win = img[y:y + bh, x:x + bw]
        hole = holes[y:y + bh, x:x + bw]
        if img.dtype != np.uint8:   # OpenCV inpaints 16 bits one channel at a time
            filled = np.dstack([cv2.inpaint(np.ascontiguousarray(win[..., c]), hole, radius, cv2.INPAINT_TELEA)
                                for c in range(3)])
        else:
            filled = cv2.inpaint(np.ascontiguousarray(win), hole, radius, cv2.INPAINT_TELEA)
        mine = (hole > 0) & (here > 0)
        out[y:y + bh, x:x + bw][mine] = filled[mine]
    return out

