"""Red-eye correction for flash photos. Ported unchanged from the author's earlier archive clean-up tools.

In each eye (InsightFace landmark), the pupil's flash-red pixels are found as strongly red relative
to green and blue (skin is reddish too, but far less saturated), kept only as the blob at the
pupil, and turned dark neutral (the green-blue level on all three channels). White catchlights,
iris colour and golden flash glow are not red, so they are untouched;
the edge is weighted by how red each pixel is, so there is no hard rim.
"""
import cv2
import numpy as np


def redness(rgb):
    """Red against the mean of green and blue, as a ratio (skin about 1.3-1.5, flash pupils 2+)."""
    f = rgb.astype(np.float32)
    return f[..., 0] / (0.5 * (f[..., 1] + f[..., 2]) + 8.0)


def pupil_weight(img, ex, ey, fw, lift=0.3, min_red=45):
    """Soft weight (0..1) over the red pupil around eye centre (ex, ey) for a face fw wide, as
    (y0, x0, weight); None when the eye has no red pupil. Red is judged against this eye's own
    surroundings (skin and the white of the eye), because a warm photo makes all skin redder,
    and only within a small disc at the landmark, so nothing outside the pupil is touched."""
    h, w = img.shape[:2]
    r = max(5, int(0.07 * fw))
    y0, x0 = max(0, int(ey) - r), max(0, int(ex) - r)
    win = img[y0:min(h, int(ey) + r), x0:min(w, int(ex) + r)]
    if win.size == 0:
        return None
    ratio = redness(win)
    yy, xx = np.mgrid[:win.shape[0], :win.shape[1]]
    dist = np.hypot(yy - (ey - y0), xx - (ex - x0))
    disc = dist <= 0.04 * fw                         # where a pupil can be, given the landmark
    ring = dist > 0.05 * fw                          # skin, lids and the white of the eye
    if not ring.any():
        return None
    base = float(np.median(ratio[ring]))
    red = (ratio >= base + lift) & (ratio >= 1.35) & (win[..., 0] >= min_red) & disc
    n, lab, st, _ = cv2.connectedComponentsWithStats(red.astype(np.uint8), connectivity=8)
    best, best_d = 0, None
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] < 5:
            continue
        d = float(dist[lab == i].min())
        if best_d is None or d < best_d:
            best, best_d = i, d
    if not best:
        return None
    blob = (lab == best).astype(np.uint8)
    k = max(3, int(0.012 * fw) | 1)
    ell = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    near = (cv2.dilate(cv2.morphologyEx(blob, cv2.MORPH_CLOSE, ell), ell) > 0) & (dist <= 0.05 * fw)
    weight = np.clip((ratio - (base + 0.1)) / (lift - 0.1), 0, 1) * near
    weight = cv2.GaussianBlur(weight.astype(np.float32), (0, 0), max(0.6, 0.002 * fw))
    return y0, x0, weight


def correct(img, faces, only=None):
    """Return (corrected copy, [(face index, eye index)] corrected). `faces`: [{'box', 'kps'}] in
    pixels; `only`: optional set of face indexes to correct (the rest are left alone)."""
    out = img.copy()
    fixed = []
    for j, f in enumerate(faces):
        if only is not None and j not in only:
            continue
        fw = f['box'][2] - f['box'][0]
        for e, (ex, ey) in enumerate(f.get('kps', [])[:2]):
            m = pupil_weight(out, ex, ey, fw)
            if m is None:
                continue
            y0, x0, wgt = m
            sl = np.s_[y0:y0 + wgt.shape[0], x0:x0 + wgt.shape[1]]
            win = out[sl].astype(np.float32)
            # a real pupil is dark and neutral: the green-blue level, a little darker, on all three
            # channels (pulling only red down leaves a green pupil where the flash red was strong)
            dark = 0.85 * 0.5 * (win[..., 1] + win[..., 2])
            win = win * (1 - wgt[..., None]) + dark[..., None] * wgt[..., None]
            out[sl] = np.clip(win, 0, 255).astype(np.uint8)
            fixed.append((j, e))
    return out, fixed
