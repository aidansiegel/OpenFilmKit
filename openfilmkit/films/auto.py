"""Automatic finishing for a conversion: per-frame color balance and exposure, the picture's edges, and which way up.

What a lab scanner or SilverFast's auto-adjust does for each frame, and what the kit didn't before November 2026:
its conversions kept the scene's light cast, came out dark, kept the holder's edge and left portrait frames sideways.
Tuned on 14 hand-graded keepers and checked by eye on a second roll (see the November 21 bake-off): balance and
exposure took the median color difference to the keepers from 16.8 to 10.4 ΔE without training on them.

Everything is measured on a small copy of the frame and recorded in the conversion's recipe, so it can be redone
exactly or switched off (`convert --no-auto`).
"""
from pathlib import Path

import numpy as np

W = np.array([.2126, .7152, .0722])            # Rec. 709 luminance
# Two directions a frame's balance (log gains, luminance held) can move in. Amber-blue is the light: daylight,
# shade, flash and lamps differ along it, so frames on one roll differ along it too. Green-magenta is mostly the
# film and the scan, which a whole roll shares, so a frame far off its roll there is usually a misreading.
AMBER_BLUE = np.array([-1., 0., 1.]) / np.sqrt(2)
GREEN_MAGENTA = np.array([1., -2., 1.]) / np.sqrt(6)
FACE_MODEL = Path(__file__).resolve().parents[1] / 'data' / 'face_detection_yunet_2023mar.onnx'


def decode(e):
    return np.where(e <= 0.04045, e / 12.92, ((e + 0.055) / 1.055) ** 2.4)


def encode(lin):
    return np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * np.maximum(lin, 0) ** (1 / 2.4) - 0.055)


def _interior(a, inset=.06):
    h, w = a.shape[:2]
    my, mx = int(h * inset), int(w * inset)
    return a[my:h - my, mx:w - mx] if h > 2 * my and w > 2 * mx else a


def balance(lin, share=.15, rounds=5):
    """Per-channel gains that make the frame's most neutral pixels gray (luminance kept the same).

    The pixels whose three channels are closest to each other (in log terms, the most gray-looking 15% of the
    mid-tones) are taken as gray in the scene, and the gains pull them to gray; repeated, so the choice of which
    pixels look gray improves as the cast goes. A whole frame of one color (grass, a pool) has gray-looking pixels
    too, unlike averaging the whole frame (gray world), which would turn the grass gray."""
    x = _interior(lin).reshape(-1, 3)
    g = np.ones(3)
    for _ in range(rounds):
        b = x * g
        Y = b @ W
        ok = (Y > .03) & (Y < .75) & (b.max(1) < .98)
        if ok.sum() < 100:
            break
        chroma = np.log(np.maximum(b[ok], 1e-4)).std(1)
        grayest = b[ok][chroma <= np.quantile(chroma, share)]
        m = grayest.mean(0)
        g = g * (m @ W / np.maximum(m, 1e-6))
    g = np.clip(g / (g @ W), .5, 2.)
    return g / (g @ W)


def exposure(lin, white=.9, mid=.32, cap=4.):
    """One brightness factor: halfway (geometrically) between putting the brightest 0.5% at white and the median at
    a mid-gray a little above 18%, the level the keepers sit at. Neither alone works: highlights only leaves dark
    frames dark, and the median only blows out frames that are mostly shadow."""
    Y = _interior(lin).reshape(-1, 3) @ W
    to_white = white / max(float(np.percentile(Y, 99.5)), 1e-4)
    to_mid = mid / max(float(np.median(Y)), 1e-4)
    return float(min(np.sqrt(to_white * to_mid), cap))


def picture_box(raw, low_high=None, limit=.12, threshold=.55):
    """The picture's box (left, top, right, bottom) in `raw`, a linear RGB16 frame: inside the holder (no light),
    any light past the film's edge (full scale) and the clear film between frames (the orange base). Each side is
    trimmed by at most `limit` of its length, so a dark or bright edge in the picture can't eat into it much."""
    from .convert import fit_levels
    a = raw.astype(float)
    hi = (low_high[1] if low_high is not None else fit_levels(raw)[1]) * 65535
    border = (a.max(2) > .97 * 65535) | (a.max(2) < .03 * 65535) | (a > .88 * hi).all(2)
    h, w = border.shape

    def trim(frac, n):
        # holder, light leak and film base can have short transitions between them: bridge gaps up to 2% of the
        # side, and stop at the first longer run of picture
        f = np.convolve(frac, np.ones(3) / 3, 'same')
        last, run, gap = -1, 0, max(2, int(n * .02))
        for i in range(int(n * limit)):
            if f[i] > threshold:
                last, run = i, 0
            else:
                run += 1
                if run > gap:
                    break
        return last + 1
    rows, cols = border.mean(1), border.mean(0)
    m = int(.006 * max(h, w)) + 1               # a hair inside a found edge, so no sliver of it survives
    inset = lambda t: t + m if t else 0          # a side with no edge found is left exactly as it is
    box = [inset(trim(cols, w)), inset(trim(rows, h)), w - inset(trim(cols[::-1], w)), h - inset(trim(rows[::-1], h))]
    if box[2] - box[0] < w * .6 or box[3] - box[1] < h * .6:
        return [0, 0, w, h]                      # found too much "edge": not a holder, leave it whole
    return box


def _faces(rgb, detector):
    import cv2
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    detector.setInputSize((bgr.shape[1], bgr.shape[0]))
    _, found = detector.detect(bgr)
    return 0. if found is None else float(sum(f[14] for f in found))


def _sky(rgb):
    h = rgb.shape[0]
    top, bottom = rgb[:h // 5].reshape(-1, 3).mean(0), rgb[-h // 5:].reshape(-1, 3).mean(0)
    return (top.mean() - bottom.mean()) + .5 * ((top[2] - top[0]) - (bottom[2] - bottom[0]))


def orientation(rgb):
    """Clockwise degrees (0, 90 or 270) that put a converted picture (uint8 RGB) upright, and why.

    Faces first (OpenCV's YuNet, which the kit already ships for red eye): the quarter turn where faces are found
    clearly more confidently than in the others, but an upright face already found keeps the picture as it is.
    Without faces, the sky: the top of a scene is usually brighter and bluer than the bottom, and only a large
    difference counts. Upside down (180) is never chosen: people turn a camera sideways, not over."""
    import cv2
    s = 800 / max(rgb.shape[:2])
    small = cv2.resize(rgb, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else rgb
    try:
        detector = cv2.FaceDetectorYN.create(str(FACE_MODEL), '', (320, 320), .7, .3, 5000)
    except cv2.error:
        detector = None
    turns = {0: 0, 1: 270, 3: 90}               # np.rot90 k (counterclockwise) -> clockwise degrees
    face, sky = {}, {}
    for k in turns:
        r = np.ascontiguousarray(np.rot90(small, k))
        face[k] = _faces(r, detector) if detector is not None else 0.
        sky[k] = _sky(r.astype(float) / 255)
    best = max(face, key=face.get)
    if face[best] >= .8 and face[best] > 2 * max(v for k, v in face.items() if k != best):
        return turns[best], 'faces'
    if face[0] >= .8:
        return 0, 'faces'
    best = max(sky, key=sky.get)
    if best != 0 and sky[best] - sky[0] > .3:
        return turns[best], 'sky'
    return 0, 'no clear cue'


def settle(gains, roll, greener=.1, pinker=.25, least=4):
    """A frame's balance checked against its roll's: (gains, what was done).

    `roll` holds the balance of every frame on the roll, this one included. Amber-blue is left alone, since the
    light really does change from frame to frame. On green-magenta, a frame whose correction adds green beyond
    `greener` (in log terms, about 10%) of the roll's median is brought back to that distance: no common light
    needs that, but a scene with little that's truly gray (orange inflatables on grass) misleads the per-frame
    balance into it. A correction that adds magenta gets more room (`pinker`), because fluorescent and other
    discharge lamps really are green, and frames under them need it. Without `least` frames, nothing changes."""
    others = [np.asarray(g, float) for g in roll if g is not None]
    if gains is None or len(others) < least:
        return gains, None
    lg = np.log(np.asarray(gains, float))
    typical = float(np.median(np.log(np.stack(others)) @ GREEN_MAGENTA))
    off = float(lg @ GREEN_MAGENTA) - typical          # below zero: this frame's correction adds green
    shift = off + greener if off < -greener else off - pinker if off > pinker else 0.
    g = np.exp(lg - shift * GREEN_MAGENTA)
    g = g / (g @ W)
    return g, dict(frames=len(others), roll_green_magenta=round(typical, 4), frame_green_magenta=round(typical + off, 4),
                   moved=round(-shift, 4), allowed=[-greener, pinker])


KNEE = .8          # linear light above this rolls off toward white instead of clipping


def shoulder(x, knee=KNEE):
    """Highlights rolled off toward white, the way print film does, instead of cut off at it: below `knee`
    nothing changes; above, values approach 1 and never pass it, so a sky or a lit wall keeps its detail."""
    x = np.maximum(x, 0)
    over = x > knee
    if not over.any():
        return x
    y = x.copy()
    y[over] = knee + (1 - knee) * (1 - np.exp(-(x[over] - knee) / (1 - knee)))
    return y


def curve(v, black=0., pivot=.5, contrast=1.3, middle=None):
    """The tone curve: `black` (encoded luminance) becomes black, `pivot` becomes `middle` (itself by default, so
    the frame's middle keeps its brightness while the shadows deepen), and the S through it is as steep as
    `contrast`. contrast 1.3, pivot 0.5 and black 0 is the curve every frame got before."""
    middle = pivot if middle is None else middle
    p = np.clip((pivot - black) / (1 - black), .02, .98) if black else pivot
    v = np.clip((v - black) / (1 - black), 0, 1) if black else np.clip(v, 0, 1)
    k = p ** contrast * (1 - middle) / (middle * (1 - p) ** contrast)
    up = v ** contrast
    return up / np.maximum(up + k * (1 - v) ** contrast, 1e-12)


def tone(lin, spread=.62, lowest=1.2, highest=2., most_white=.01, lift=.88):
    """The frame's own black point, pivot and contrast, from its linear light (balanced and exposed).

    Black: just under the darkest 0.5%, so shadows are deep without crushing them. Pivot: the frame's middle
    (median), lifted a little (`lift`, a power: the keepers sit brighter than a plain conversion), so the curve adds
    contrast around what the picture is mostly made of.
    Contrast: as much as brings the middle 80% of tones to `spread` (the keepers' typical range), between
    `lowest` and `highest`, but none that pushes more than another `most_white` of the frame to white."""
    e = encode(shoulder(_interior(lin).reshape(-1, 3)))
    L = np.clip(e @ W, 0, 1)
    if L.size > 200000:
        L = L[:: L.size // 200000 + 1]
    black = float(np.clip(np.percentile(L, .5) * .85, 0, .15))
    pivot = float(np.clip(np.median(L), .2, .8))
    white_before = (L > .995).mean()
    best = lowest
    for c in np.arange(lowest, highest + 1e-9, .05):
        t = curve(L, black, pivot, c, pivot ** lift)
        if (t > .995).mean() > white_before + most_white and c > lowest:
            break
        best = float(c)
        lo, hi = np.percentile(t, [10, 90])
        if hi - lo >= spread:
            break
    return dict(black=round(black, 4), pivot=round(pivot, 4), middle=round(pivot ** lift, 4), contrast=round(best, 3))


def measure(small, lut, roll=None):
    """What automatic finishing does to a frame, from a small copy of it and its conversion curve:
    {gains, exposure, roll_check, tone}. `roll`: the other frames' balance on the same roll (roll_balance in
    convert), or None to skip that check."""
    e = np.stack([lut[small[:, :, c], c] for c in range(3)], 2).astype(float) / 65535
    lin = decode(e)
    gains, check = balance(lin), None
    if roll is not None:
        gains, check = settle(gains, list(roll) + [gains])
        if check is not None:
            check['frame_balance_rgb'] = [round(float(g), 5) for g in balance(lin)]
    k = exposure(lin * gains)
    return dict(gains=gains, exposure=k, roll_check=check, tone=tone(lin * gains * k))
