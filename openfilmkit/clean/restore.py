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


def to8(img):
    """An 8-bit copy of a 16-bit image (65535 / 255 = 257 exactly); 8-bit images pass through."""
    return img if img.dtype == np.uint8 else np.rint(img / 257.0).astype(np.uint8)


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
    """Pixels of the camera's orange-red LED date stamp (plus a margin), to leave untouched.

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
    out = (img.astype(np.float32) - lo * scale) * gain
    out = img.astype(np.float32) * (1 - strength) + out * strength
    return np.clip(out, 0, 255 * scale).astype(img.dtype)


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
    if img.dtype != np.uint8:   # float Lab has the same a*, b* units as 8-bit Lab (offset by 128)
        lab = cv2.cvtColor(img.astype(np.float32) / 65535, cv2.COLOR_RGB2LAB)
        lab[..., 1] += shift[0]
        lab[..., 2] += shift[1]
        out = np.rint(np.clip(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB), 0, 1) * 65535).astype(np.uint16)
        return out, (float(shift[0]), float(shift[1]))
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab[..., 1] += shift[0]
    lab[..., 2] += shift[1]
    out = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)
    return out, (float(shift[0]), float(shift[1]))


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
        return lut[img]
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
    diff = gray.astype(np.int16) - bg.astype(np.int16)
    cand = (diff > threshold).astype(np.uint8)
    # smoothness of the surroundings: local std of the median-filtered image, leaving out the
    # candidates themselves. A speck about as wide as the median window leaks into the filtered
    # copy, and counted in, it made its own surroundings look busy: on a 960-pixel portrait that
    # turned away a quarter of dust-sized specks on plain ground.
    bgf = bg.astype(np.float32)
    around = (cv2.dilate(cand, np.ones((3, 3), np.uint8)) == 0).astype(np.float32)
    win = (k * 2 + 1, k * 2 + 1)
    count = np.maximum(cv2.blur(around, win), 1e-3)
    mean = cv2.blur(bgf * around, win) / count
    sq = cv2.blur(bgf * bgf * around, win) / count
    local_std = np.sqrt(np.maximum(sq - mean * mean, 0))
    # fine detail and the photo's grain level (robust std of it, from a quarter of the pixels)
    gf = gray.astype(np.float32)
    fine = gf - cv2.GaussianBlur(gf, (0, 0), 1.5)
    grain = 1.4826 * float(np.median(np.abs(fine[::2, ::2])))
    texture_limit = texture_factor * max(grain, grain_floor)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(cand, connectivity=8)
    max_area = max(12, int(h * w * max_area_fraction))
    thin = max(2, int(min(h, w) * 0.0012))
    # thickness of each candidate: the largest distance from its inside to its edge
    dist = cv2.distanceTransform(cand, cv2.DIST_L2, 3)
    thickness = np.zeros(n, np.float32)
    np.maximum.at(thickness, labels.ravel(), dist.ravel())
    # Dust is sparse; bright detail in foliage, gravel or lace comes in crowds. Count candidates
    # around each one and leave crowded ones alone.
    win = max(31, int(min(h, w) * 0.04) | 1)
    centers = np.zeros((h, w), np.float32)
    cxs = np.clip(stats[1:, 0] + stats[1:, 2] // 2, 0, w - 1)
    cys = np.clip(stats[1:, 1] + stats[1:, 3] // 2, 0, h - 1)
    centers[cys, cxs] = 1
    crowd = cv2.boxFilter(centers, -1, (win, win), normalize=False)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if crowd[cys[i - 1], cxs[i - 1]] > max_crowd:
            continue
        speck = area <= max_area
        # fibers and hairs: thin along their whole length, straight or curled
        fiber = area <= max_area * 12 and thickness[i] <= thin and max(bw, bh) >= 4 * thickness[i] * 2
        short_line = False
        if fiber and not speck:
            # Long straight thin lines are as likely to be an edge in the scene (a fence rail, a
            # frame) as a scratch, so they stay. Curled fibers go, and so do short straight
            # scratches, which are checked below against smooth surroundings (skin, wall, sky).
            path = area / max(1.0, 2 * float(thickness[i]))
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
        if ring.sum() >= 20 and float(fine[ya:yb, xa:xb][ring].std()) > texture_limit:
            continue
        keep[i] = True
    mask = keep[labels]
    if protect is not None:
        mask &= ~protect
    grow = max(1, int(min(h, w) * 0.0008))
    return cv2.dilate(mask.astype(np.uint8), np.ones((2 * grow + 1, 2 * grow + 1), np.uint8)) > 0


def remove_dust(img, mask):
    if not mask.any():
        return img
    radius = max(2, int(min(img.shape[:2]) * 0.0015))
    if img.dtype != np.uint8:   # 16-bit inpainting is single-channel; per channel equals 3-channel at 8 bits
        holes = mask.astype(np.uint8) * 255
        return np.dstack([cv2.inpaint(np.ascontiguousarray(img[..., c]), holes, radius, cv2.INPAINT_TELEA)
                          for c in range(3)])
    return cv2.inpaint(img, mask.astype(np.uint8) * 255, radius, cv2.INPAINT_TELEA)

