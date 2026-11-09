"""Scratches and debris that dust removal leaves: found by a trained model, filled by another.

Ported from the author's archive pipeline (model_detect.py, model_fill.py, model_lib.py), with
the same decisions; only the runtime changed: both models run in OpenCV's DNN module instead of
PyTorch and onnxruntime, and faces come from YuNet (see clean.find_faces) instead of InsightFace.

1. Scratch probability: Microsoft's "Bringing Old Photos Back to Life" UNet on a grayscale copy
   1700 px on its long side (where it finds the most damage; at 2x and 4x it finds less), run in
   overlapping 512 px tiles.
2. The mask (build_mask), from that probability:
   - the skin of each face (see skin_area) uses a lower threshold and two extra speck finders,
     because dust on a face is what people notice; only each eye's catchlight is protected there,
     and inside the eyes only confident detections count (iris texture is not dust). The archive
     pipeline applied this to the whole face box widened by 30 %, which takes in the hair: on a
     1984 NASA portrait that filled 2,770 pixels of hair strands that were not damage;
   - a thick detection darker than its surroundings is scene (a shadow, a trench), not damage:
     dust and emulsion scratches on a negative print bright;
   - the scan's own edge and the camera date stamp are never touched.
3. The fill (fill_damage): MI-GAN, window by window around each piece of damage, with every
   unfilled damaged pixel in the window a hole at once. Small spots the fill invents are evened
   out against a conservative (Telea) fill, and too-smooth fills get grain from nearby film.

Here the model draws the picture that goes where the damage was, so clean records which model filled
how many pixels, and what share of the picture that is.
"""
import cv2
import numpy as np
from PIL import Image

from . import restore
from .. import models

FACE_PAD = 0.3          # face box widened by this share of its size on each side
TILE, MARGIN = 512, 48  # MI-GAN window; damage within MARGIN of its edge waits for a later window
DETECT_SIDE, DETECT_PAD = 1700, 64


# ---------------------------------------------------------------- models

def load_nets(say=None):
    """Fetch the model files if needed (ModelError if that fails) and return load(name), which
    reads 'scratch' or 'fill' as a cv2.dnn network. repair() reads each one when it needs it, so
    only one is in memory at a time: after a 512 px tile the scratch net holds 1.3 GB.

    OpenCV's Winograd convolution is turned off for the scratch net: it took another 1.1 GB, for
    a speed-up of a second a tile and probabilities that differ by a few millionths."""
    files = {name: str(models.path(name, say)) for name in ('scratch', 'fill')}

    def load(name):
        net = cv2.dnn.readNetFromONNX(files[name])
        if name == 'scratch':
            net.enableWinograd(False)
        return net
    return load


def scratch_probability(net, img):
    """Scratch probability (float32, 0..1) at full size for an RGB uint8 image."""
    h, w = img.shape[:2]
    s = DETECT_SIDE / max(h, w)
    gray = Image.fromarray(img).convert('L')   # PIL's gray and bicubic resize, as the pipeline had
    small = np.asarray(gray.resize((round(w * s), round(h * s)), Image.BICUBIC))
    x = (small.astype(np.float32) / 255.0 - 0.5) / 0.5
    sh, sw = x.shape
    prob = np.zeros((sh, sw), np.float32)
    step = TILE - 2 * DETECT_PAD
    for y0 in range(0, sh, step):
        for x0 in range(0, sw, step):
            ys = np.clip(np.arange(y0 - DETECT_PAD, y0 - DETECT_PAD + TILE), 0, sh - 1)
            xs = np.clip(np.arange(x0 - DETECT_PAD, x0 - DETECT_PAD + TILE), 0, sw - 1)
            net.setInput(np.ascontiguousarray(x[np.ix_(ys, xs)])[None, None])
            p = net.forward()[0, 0]
            hh, ww = min(step, sh - y0), min(step, sw - x0)
            prob[y0:y0 + hh, x0:x0 + ww] = p[DETECT_PAD:DETECT_PAD + hh, DETECT_PAD:DETECT_PAD + ww]
    return cv2.resize(prob, (w, h), interpolation=cv2.INTER_LINEAR)


def fill_window(net):
    """run(crop, keep) for fill_damage: MI-GAN on one window, as the authors' ONNX pipeline does
    it for a window of 512 px or less (resize to 512, generate, resize back, then blend through
    the keep mask widened by a 3 px max filter and softened by a 5 px Gaussian)."""
    kernel = cv2.getGaussianKernel(5, 1.0)

    def run(crop, keep):
        th, tw = keep.shape
        image = cv2.resize(crop, (TILE, TILE), interpolation=cv2.INTER_LINEAR) if (th, tw) != (TILE, TILE) else crop
        m = cv2.resize(keep, (TILE, TILE), interpolation=cv2.INTER_NEAREST) if (th, tw) != (TILE, TILE) else keep
        image = image.astype(np.float32).transpose(2, 0, 1) * 2 / 255 - 1
        m = m.astype(np.float32)[None] / 255
        net.setInput(np.concatenate([m - 0.5, image * m])[None])
        out = np.clip((net.forward()[0] * 0.5 + 0.5) * 255, 0, 255).transpose(1, 2, 0)
        if (th, tw) != (TILE, TILE):
            out = cv2.resize(out, (tw, th), interpolation=cv2.INTER_LINEAR)
        soft = cv2.dilate(keep, np.ones((3, 3), np.uint8)).astype(np.float32)
        soft = cv2.sepFilter2D(soft, -1, kernel, kernel, borderType=cv2.BORDER_REFLECT_101)[..., None] / 255
        return np.clip(crop.astype(np.float32) * soft + out * (1 - soft), 0, 255).astype(np.uint8)
    return run


# ---------------------------------------------------------------- mask

def grow_for(shape):
    """Pixels to widen a detection by, scaled to the photo (1 px per 2000 px of long side)."""
    return max(1, round(max(shape[:2]) / 2000))


def face_box(f, shape):
    h, w = shape[:2]
    x0, y0, x1, y1 = f['box']
    fw, fh = x1 - x0, y1 - y0
    return (max(0, int(x0 - FACE_PAD * fw)), max(0, int(y0 - FACE_PAD * fh)),
            min(w, int(x1 + FACE_PAD * fw)), min(h, int(y1 + FACE_PAD * fh)), fw)


def skin_area(img, faces, texture_factor=2.5, grain_floor=1.5):
    """The skin in each face's zone, where the face rules apply: smooth (fine detail within
    texture_factor times the photo's grain, as in restore.dust_mask) and, in a color photo, close
    in color to the middle of the face. Small gaps and holes are closed, so a scratch or speck
    on a cheek still counts as skin; masses of hair, eyebrows, collars and hats do not."""
    h, w = img.shape[:2]
    out = np.zeros((h, w), bool)
    if not faces:
        return out
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    limit = texture_factor * max(restore.grain_level(gray), grain_floor)
    for f in faces:   # color and detail only inside each face's zone, not over the whole photo
        l, t, r, b, fw = face_box(f, img.shape)
        x0, y0, x1, y1 = f['box']
        middle = img[int(y0 + 0.45 * (y1 - y0)):int(y0 + 0.85 * (y1 - y0)),
                     int(x0 + 0.3 * (x1 - x0)):int(x0 + 0.7 * (x1 - x0))]
        core = (cv2.cvtColor(np.ascontiguousarray(middle), cv2.COLOR_RGB2LAB).astype(np.float32).reshape(-1, 3)
                if middle.size else np.zeros((0, 3), np.float32))
        if r <= l or b <= t or len(core) < 20:
            continue
        L0, a0, b0 = np.median(core, axis=0)
        reg = cv2.cvtColor(np.ascontiguousarray(img[t:b, l:r]), cv2.COLOR_RGB2LAB).astype(np.float32)
        fa, fb, fc, fd = max(0, t - 8), min(h, b + 8), max(0, l - 8), min(w, r + 8)
        fr = restore.fine_detail(gray[fa:fb, fc:fd])[t - fa:b - fa, l - fc:r - fc]
        k = max(3, int(0.04 * fw) | 1)
        local = np.sqrt(np.maximum(cv2.blur(fr * fr, (k, k)) - cv2.blur(fr, (k, k)) ** 2, 0))
        near = np.abs(reg[..., 0] - L0) < 70
        if np.hypot(a0 - 128, b0 - 128) > 6:   # a color photo: skin is near the face's own color
            near &= (np.abs(reg[..., 1] - a0) < 14) & (np.abs(reg[..., 2] - b0) < 16)
        skin = ((local <= limit) & near).astype(np.uint8)
        c = max(3, int(0.03 * fw) | 1)
        skin = cv2.morphologyEx(skin, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (c, c)))
        skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (c, c)))
        # fill holes: flood the non-skin from a frame round the zone; what it can't reach is a hole
        outside = np.pad(1 - skin, 1, constant_values=1)
        cv2.floodFill(outside, None, (0, 0), 2)
        out[t:b, l:r] |= (skin > 0) | (outside[1:-1, 1:-1] == 1)
    return out


def eye_masks(gray, faces):
    """(catchlights, irises): a small disc on each eye's catchlight, found as the spot inside the
    iris brightest against its surroundings (top-hat), not the brightest pixel, which is often the
    white of the eye; and a disc around each eye."""
    h, w = gray.shape
    catch = np.zeros((h, w), np.uint8)
    irises = np.zeros((h, w), np.uint8)
    for f in faces:
        fw = f['box'][2] - f['box'][0]
        for ex, ey in f.get('kps', [])[:2]:
            ri = max(3, int(0.05 * fw))
            cv2.circle(irises, (int(ex), int(ey)), int(1.4 * ri), 1, -1)
            ey0, ex0 = max(0, int(ey) - 2 * ri), max(0, int(ex) - 2 * ri)
            win = gray[ey0:max(0, int(ey) + 2 * ri), ex0:max(0, int(ex) + 2 * ri)]
            if not win.size:
                continue
            k = max(3, int(0.03 * fw) | 1)
            hat = cv2.morphologyEx(cv2.GaussianBlur(win, (3, 3), 0), cv2.MORPH_TOPHAT,
                                   cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))).astype(np.float32)
            yy, xx = np.mgrid[:win.shape[0], :win.shape[1]]
            hat[(yy + ey0 - ey) ** 2 + (xx + ex0 - ex) ** 2 > ri ** 2] = 0
            cy, cx = np.unravel_index(np.argmax(hat), hat.shape)
            cv2.circle(catch, (ex0 + int(cx), ey0 + int(cy)), max(2, int(0.018 * fw)), 1, -1)
    return catch > 0, irises > 0


def face_flecks(img, faces, protect, min_hat=40, max_area_frac=0.0006, smooth_std=10, max_crowd=2):
    """Bright flecks on skin wider than restore.dust_mask's speck window: top-hat with a kernel of
    ~3 % of the face width, kept only when small, sparse (whiskers and stubble come in crowds), on
    smooth surroundings, and ringed by skin tone (not hair, a lens or a shirt)."""
    h, w = img.shape[:2]
    out = np.zeros((h, w), bool)
    for f in faces:
        l, t, r, b, fw = face_box(f, img.shape)
        reg = img[t:b, l:r]
        if reg.size == 0:
            continue
        g = cv2.cvtColor(reg, cv2.COLOR_RGB2GRAY)
        k = max(5, int(0.03 * fw) | 1)
        ell = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        hat = cv2.morphologyEx(cv2.GaussianBlur(g, (3, 3), 0), cv2.MORPH_TOPHAT, ell)
        cand = (hat > min_hat).astype(np.uint8)
        bg = cv2.morphologyEx(g, cv2.MORPH_OPEN, ell).astype(np.float32)
        kk = 2 * k + 1
        mean = cv2.blur(bg, (kk, kk))
        std = np.sqrt(np.maximum(cv2.blur(bg * bg, (kk, kk)) - mean * mean, 0))
        n, lab, st, _ = cv2.connectedComponentsWithStats(cand, connectivity=8)
        max_area = max(20, int(max_area_frac * fw * fw * 4))
        weak = (hat > min_hat // 2).astype(np.uint8)
        _, _, stw, _ = cv2.connectedComponentsWithStats(weak, connectivity=8)
        centers = np.zeros(g.shape, np.float32)
        centers[np.clip(stw[1:, 1] + stw[1:, 3] // 2, 0, g.shape[0] - 1),
                np.clip(stw[1:, 0] + stw[1:, 2] // 2, 0, g.shape[1] - 1)] = 1
        win = max(15, int(0.12 * fw) | 1)
        crowd = cv2.boxFilter(centers, -1, (win, win), normalize=False)
        for i in range(1, n):
            x, y, bw, bh, area = st[i]
            if area > max_area or crowd[y + bh // 2, x + bw // 2] > max_crowd + 1:
                continue
            comp = lab[y:y + bh, x:x + bw] == i
            if np.median(std[y:y + bh, x:x + bw][comp]) > smooth_std:
                continue
            ya, yb, xa, xb = max(0, y - k), min(g.shape[0], y + bh + k), max(0, x - k), min(g.shape[1], x + bw + k)
            ring = np.ones((yb - ya, xb - xa), bool)
            ring[y - ya:y - ya + bh, x - xa:x - xa + bw] &= ~comp
            rr, gg, bb = (float(np.median(reg[ya:yb, xa:xb][..., c][ring])) for c in range(3))
            if not (rr > 90 and rr > gg >= bb * 0.9 and rr - bb > 15):
                continue
            out[t + y:t + y + bh, l + x:l + x + bw] |= comp
    out &= ~protect
    return cv2.dilate(out.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0


def drop_dark_marks(mask, gray, grow, margin=10):
    """Remove thick detections darker than their surroundings (scene, not damage). Returns count."""
    h, w = mask.shape
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    dropped = 0
    for i in range(1, n):
        x, y, bw, bh, _ = st[i]
        # thickness: the largest distance from its inside to its edge, on its own box
        ya, yb, xa, xb = max(0, y - 2), min(h, y + bh + 2), max(0, x - 2), min(w, x + bw + 2)
        thick = float(cv2.distanceTransform((lab[ya:yb, xa:xb] == i).astype(np.uint8), cv2.DIST_L2, 3).max())
        if thick <= 3 * grow:
            continue
        pad = max(6, int(thick * 2))
        ya, yb, xa, xb = max(0, y - pad), min(h, y + bh + pad), max(0, x - pad), min(w, x + bw + pad)
        comp = lab[ya:yb, xa:xb] == i
        ring = (cv2.dilate(comp.astype(np.uint8), np.ones((2 * pad + 1, 2 * pad + 1), np.uint8)) > 0) & (lab[ya:yb, xa:xb] == 0)
        if ring.any() and gray[ya:yb, xa:xb][comp].mean() < np.median(gray[ya:yb, xa:xb][ring]) - margin:
            mask[ya:yb, xa:xb][comp] = 0
            dropped += 1
    return dropped


def drop_long_lines(mask, faces_area, shape, min_share=0.04):
    """Remove long detections outside faces (tile grout, power lines, seams and edges that the model
    takes for scratches). Only for photos that have such lines (clean --spare-long-lines), because
    long real scratches look the same to it. Returns count."""
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    long_side = max(shape[:2])
    dropped = 0
    for i in range(1, n):
        x, y, bw, bh, _ = st[i]
        if np.hypot(bw, bh) < min_share * long_side:
            continue
        comp = lab[y:y + bh, x:x + bw] == i
        if (comp & faces_area[y:y + bh, x:x + bw]).any():
            continue
        mask[y:y + bh, x:x + bw][comp] = 0
        dropped += 1
    return dropped


def complete_bright_blobs(mask, gray, lift=45, edge_lift=12, max_area_share=0.002, max_growth=30,
                          max_background=150, rgb=None, min_white=170, max_chroma=40, max_ring_std=8.0):
    """Grow each detection over the whole bright blob it sits on, soft edge included. Debris on the
    negative prints as a pure white blob; when the model marks only part of it, the fill copies the
    white still showing beside the hole. The blob is every connected pixel clearly brighter than
    the detection's surroundings (lift), widened over its glow down to edge_lift but only a few
    pixels out, searched in a window that grows until the blob is inside it. Left alone: blobs
    bigger than debris, blobs more than max_growth times what the model marked inside them (a
    white print on a shirt or a pale finger grew 50-100 times; debris marked only along its edges,
    16 times), and bright surroundings (above max_background), where debris hardly shows and white
    scene detail is common. With `rgb`, also blobs that are not near-white and neutral (skin showing
    through hair) and blobs on busy surroundings (a flash highlight on a shoe: smoothed surround
    varies by ~19 levels against ~3 round debris on a plain cabinet). Returns blobs completed."""
    h, w = mask.shape
    limit = max_area_share * h * w
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    done = 0
    for i in range(1, n):
        x, y, bw, bh, _ = st[i]
        pad = max(10, 2 * max(bw, bh))
        for _ in range(5):
            ya, yb, xa, xb = max(0, y - pad), min(h, y + bh + pad), max(0, x - pad), min(w, x + bw + pad)
            comp = lab[ya:yb, xa:xb] == i
            g = gray[ya:yb, xa:xb].astype(np.int16)
            bg = float(np.median(g[~comp]))
            if bg > max_background:
                blob = None
                break
            core = g > bg + lift
            if not (core & comp).any():
                blob = None
                break
            nb, lb = cv2.connectedComponents(core.astype(np.uint8), connectivity=8)
            ids = np.unique(lb[comp & core])
            seed = np.isin(lb, ids[ids > 0])
            # hysteresis: the glow round the blob, connected to it, down to edge_lift
            near = cv2.dilate(seed.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
            glow = ((g > bg + edge_lift) & (near > 0)).astype(np.uint8)
            ng, lg = cv2.connectedComponents(glow, connectivity=8)
            gids = np.unique(lg[seed])
            blob = np.isin(lg, gids[gids > 0]) | seed
            ys, xs = np.nonzero(blob)
            inside = ys.min() > 0 and xs.min() > 0 and ys.max() < blob.shape[0] - 1 and xs.max() < blob.shape[1] - 1
            if inside or (ya == 0 and xa == 0 and yb == h and xb == w):
                break
            pad *= 2
        marked = (mask[ya:yb, xa:xb] > 0) & blob if blob is not None else None
        if blob is None or blob.sum() > limit or not inside or blob.sum() > max_growth * marked.sum():
            continue
        if rgb is not None:
            core_px = rgb[ya:yb, xa:xb][seed].astype(np.int16)
            if core_px.min(axis=1).mean() < min_white or (core_px.max(axis=1) - core_px.min(axis=1)).mean() > max_chroma:
                continue
            gs = cv2.GaussianBlur(gray[ya:yb, xa:xb].astype(np.float32), (0, 0), 3)
            b8 = blob.astype(np.uint8)
            ring = (cv2.dilate(b8, np.ones((61, 61), np.uint8)) > 0) & (cv2.dilate(b8, np.ones((21, 21), np.uint8)) == 0)
            if ring.sum() < 100 or gs[ring].std() > max_ring_std:
                continue
        blob = cv2.dilate(blob.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        if (blob & ~comp).sum() == 0:
            continue
        mask[ya:yb, xa:xb][blob] = 1
        done += 1
    return done


def build_mask(img, prob, protect, faces, dust_mask=None, thr=0.4, face_thr=0.15, face_speck_thr=14,
               spare_long_lines=False):
    """The pixels to fill. `prob`: scratch probability at full size; `protect`: never touched (date
    stamp; inside faces it is replaced by catchlight protection); `dust_mask`: restore.dust_mask,
    used inside faces at a lower threshold; `spare_long_lines`: see drop_long_lines.
    Returns (mask uint8 0/1, stats)."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    protect = protect.copy()
    faces_area = np.zeros((h, w), bool)   # whole widened face boxes: for long lines and the stamp
    for f in faces:
        l, t, r, b, _ = face_box(f, img.shape)
        faces_area[t:b, l:r] = True
        protect[t:b, l:r] = False
    skin = skin_area(img, faces)
    catch, irises = eye_masks(gray, faces)
    protect |= catch
    # the threshold: thr, face_thr on skin, thr again inside the eyes
    mask = ((prob >= thr) | (skin & ~irises & (prob >= face_thr))).astype(np.uint8)
    grow = grow_for(img.shape)
    stats = {'dark_marks_kept': drop_dark_marks(mask, gray.astype(np.float32), grow)}
    stats['bright_blobs_completed'] = complete_bright_blobs(mask, gray, rgb=img)
    if spare_long_lines:
        stats['long_lines_kept'] = drop_long_lines(mask, faces_area, img.shape, min_share=0.02)
    mask = cv2.dilate(mask, np.ones((2 * grow + 1, 2 * grow + 1), np.uint8))
    if faces:
        # fibers on bright skin have a soft halo: fill a little wider inside faces (not the eyes)
        wider = cv2.dilate(mask, np.ones((4 * grow + 1, 4 * grow + 1), np.uint8))
        widen = skin & ~irises
        mask[widen] = wider[widen]
        add = np.zeros((h, w), bool)
        if dust_mask is not None:
            add |= dust_mask(img, protect=protect, threshold=face_speck_thr, max_crowd=12) & skin
        add |= face_flecks(img, faces, protect)
        add &= ~irises
        stats['face_extra_px'] = int((add & (mask == 0)).sum())
        stats['skin_px'] = int(skin.sum())
        mask[add] = 1
    mask[protect] = 0
    edge = round(max(w, h) * 0.004)  # the scan's own edge reads as a scratch; the crop step owns it
    mask[:edge] = 0
    mask[-edge:] = 0
    mask[:, :edge] = 0
    mask[:, -edge:] = 0
    stats['masked_share'] = round(float(mask.mean()), 6)
    return mask, stats


# ---------------------------------------------------------------- fill

def fill_damage(img, mask, run, tile=TILE, margin=MARGIN, max_passes=64):
    """Fill `mask` window by window around each piece of damage, largest first. `run(crop, keep)`
    returns the model's fill of `crop` (uint8 HxWx3) where `keep` (uint8 HxW) is 0. Every unfilled
    damaged pixel in a window is a hole at once, so a half-filled fleck is never context to copy.
    Returns (result, calls)."""
    h, w = mask.shape
    res = img.copy()
    todo = mask.astype(bool).copy()
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    calls = 0
    for i in np.argsort(-st[1:, cv2.CC_STAT_AREA]) + 1:
        x, y, bw, bh, _ = st[i]
        for _ in range(max_passes):
            comp = todo[y:y + bh, x:x + bw] & (lab[y:y + bh, x:x + bw] == i)
            if not comp.any():
                break
            ys, xs = np.nonzero(comp)
            cy, cx = int(ys.mean()), int(xs.mean())
            if not comp[cy, cx]:
                cy, cx = int(ys[0]), int(xs[0])   # long damage: work along it from one end
            cy, cx = y + cy, x + cx
            y0 = min(max(0, cy - tile // 2), max(0, h - tile))
            x0 = min(max(0, cx - tile // 2), max(0, w - tile))
            crop = res[y0:y0 + tile, x0:x0 + tile]
            th, tw = crop.shape[:2]
            hole = todo[y0:y0 + th, x0:x0 + tw].copy()
            inner = np.zeros_like(hole)
            inner[margin if y0 > 0 else 0:th - (margin if y0 + th < h else 0),
                  margin if x0 > 0 else 0:tw - (margin if x0 + tw < w else 0)] = True
            r = run(crop, np.where(hole, 0, 255).astype(np.uint8))
            write = hole & inner
            if not (write & (lab[y0:y0 + th, x0:x0 + tw] == i)).any():
                write = hole                     # this damage only in the margin: write it to progress
            crop[write] = r[write]
            todo[y0:y0 + th, x0:x0 + tw][write] = False
            calls += 1
    return res, calls


def settle_fill(res, conservative, mask, grow):
    """Undo small spots the model's fill made up: where it departs sharply from the conservative
    (Telea) fill, the conservative fill wins; where a filled spot is still clearly darker or brighter
    than its neighborhood, it takes the neighborhood median. Returns (result, stats)."""
    filled = mask > 0

    def g(a):
        return cv2.cvtColor(a, cv2.COLOR_RGB2GRAY).astype(np.int16)

    def small_parts(sel, max_area, grow_by):
        n, lab, st, _ = cv2.connectedComponentsWithStats(sel.astype(np.uint8), connectivity=8)
        ok = np.zeros(n, bool)
        ok[1:] = st[1:, cv2.CC_STAT_AREA] <= max_area
        out = cv2.dilate(ok[lab].astype(np.uint8), np.ones((grow_by, grow_by), np.uint8)) > 0
        return out & filled, int(ok.sum())

    res = res.copy()
    odd, n_odd = small_parts(filled & (np.abs(g(res) - g(conservative)) > 30), 400, 5)
    res[odd] = conservative[odd]
    med = cv2.medianBlur(res, max(5, (6 * grow) | 1))
    spot, n_spot = small_parts(filled & (np.abs(g(res) - g(med)) > 18), 300, 3)
    res[spot] = med[spot]
    return res, {'invented_replaced': n_odd, 'spots_evened': n_spot}


def match_grain(res, mask, sigma=1.2, block=48, smooth_ratio=0.75, min_area=200, seed=0):
    """Give too-smooth fills the film grain of their surroundings. Most fills already carry grain;
    only pieces whose fine texture is weaker than smooth_ratio of the ring around them are touched.
    Each gets real grain from clean nearby blocks of the photo (the photo minus a light blur), scaled
    to make up just the missing amount, so a fill never ends up grainier than its neighborhood.
    Returns (result, pieces matched).

    Each piece is worked out on the part of the photo it reads (its window, the donor blocks up to
    four blocks away, and the blur's reach), not on float copies of the whole photo, and only its
    own pixels are written."""
    rng = np.random.default_rng(seed)
    filled = mask > 0
    h, w = mask.shape
    out = res.copy()
    n, lab, st, _ = cv2.connectedComponentsWithStats(filled.astype(np.uint8), connectivity=8)
    reach = 5 * block + 8   # donors start up to 4 blocks away and are a block wide; the blur reaches 5 px
    done = 0
    for i in range(1, n):
        x, y, bw, bh, area = st[i]
        if area < min_area:
            continue
        p = 24
        ya, yb, xa, xb = max(0, y - p), min(h, y + bh + p), max(0, x - p), min(w, x + bw + p)
        ra, rb, rc, rd = max(0, ya - reach), min(h, yb + reach), max(0, xa - reach), min(w, xb + reach)
        f = res[ra:rb, rc:rd].astype(np.float32)
        grain = f - cv2.GaussianBlur(f, (0, 0), sigma)
        here = (slice(ya - ra, yb - ra), slice(xa - rc, xb - rc))
        gl = grain[here].mean(axis=2)
        comp = lab[ya:yb, xa:xb] == i
        inner = cv2.erode(comp.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        ring = (cv2.dilate(comp.astype(np.uint8), np.ones((25, 25), np.uint8)) > 0) & ~filled[ya:yb, xa:xb]
        if inner.sum() < 50 or ring.sum() < 100:
            continue
        have, want = float(gl[inner].std()), float(gl[ring].std())
        if have >= smooth_ratio * want:
            continue
        gain = np.sqrt(max(want ** 2 - have ** 2, 0)) / max(want, 1e-3)
        add = np.zeros((yb - ya, xb - xa, 3), np.float32)
        for by in range(0, yb - ya, block):
            for bx in range(0, xb - xa, block):
                bh2, bw2 = min(block, yb - ya - by), min(block, xb - xa - bx)
                if not comp[by:by + bh2, bx:bx + bw2].any():
                    continue
                cy, cx = ya + by, xa + bx
                for _ in range(40):                   # a clean donor block within a few blocks
                    r = block * rng.integers(1, 5)
                    t = rng.uniform(0, 2 * np.pi)
                    dy, dx = cy + int(r * np.sin(t)), cx + int(r * np.cos(t))
                    if dy < 0 or dx < 0 or dy + bh2 > h or dx + bw2 > w or filled[dy:dy + bh2, dx:dx + bw2].any():
                        continue
                    add[by:by + bh2, bx:bx + bw2] = grain[dy - ra:dy - ra + bh2, dx - rc:dx - rc + bw2] * gain
                    break
        soft = cv2.GaussianBlur(comp.astype(np.float32), (0, 0), 1.5) * comp
        piece = np.clip(f[here] + add * soft[..., None], 0, 255).astype(np.uint8)
        out[ya:yb, xa:xb][comp] = piece[comp]   # soft is zero off the piece: nothing else changes
        done += 1
    return out, done


def repair(img, faces, protect, dust_mask, load, spare_long_lines=False):
    """The whole pass on an RGB uint8 image: (result, stats, mask). Every change is inside mask.
    `load`: load_nets()."""
    prob = scratch_probability(load('scratch'), img)
    mask, stats = build_mask(img, prob, protect, faces, dust_mask=dust_mask, spare_long_lines=spare_long_lines)
    del prob
    if not mask.any():
        return img.copy(), dict(stats, model_calls=0, invented_replaced=0, spots_evened=0, grain_matched=0), mask
    telea = restore.inpaint(img, mask, 3)
    res, calls = fill_damage(img, mask, fill_window(load('fill')))
    res, settle = settle_fill(res, telea, mask, grow_for(img.shape))
    res, grained = match_grain(res, mask)
    return res, dict(stats, **settle, grain_matched=grained, model_calls=calls, pixels_filled=int(mask.sum())), mask
