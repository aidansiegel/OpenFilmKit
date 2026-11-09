"""Clean-up of converted viewing copies: dust, holder crop, optional color, straightening, red eye.

Order follows the archive pipeline this came from (clean_batch.py): protect the date stamp and
faces, remove dust from the original, optionally fix color, then crop; then, if asked, the
scratch pass (see scratches.py) on the cropped copy. Manual adjustments and red eye come after.
Dust is filled only from its immediate surroundings (OpenCV inpainting). The scratch pass is the
one step where a model draws the picture that goes into the damaged spots, so it is opt-in, and
the recipe says which model filled how many pixels, and what share of the picture that is.

16-bit TIFFs (from `convert --tiff`) are cleaned at full depth: every decision (dust, faces, the
stamp, the crop, color statistics, scratches, red eye) is made on an 8-bit copy, exactly as for
an 8-bit file, and applied to the 16-bit image. Color, crop, straightening, shadows and dust fills
are computed at 16 bits; scratch fills and red eye, which replace small regions, are 8-bit
precision inside those regions.

Faces are found with YuNet, a small face detector built into OpenCV (model bundled in data/, MIT
license, from the OpenCV Zoo). It gives five landmarks per face, so, as in the archive pipeline,
only eyes and mouths are protected from dust removal and the rest of the face is cleaned. It looks
at all four rotations, because film frames are often scanned sideways: on photos turned 90 degrees,
upright-only YuNet found 12% of faces, four turns 97% (154 archive photos, measured against the
archive pipeline's InsightFace detector; OpenCV's older Haar cascades found 32%).

Needs OpenCV (opencv-python-headless), numpy and Pillow.
"""
import datetime
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .. import __version__
from . import redeye, restore, scratches as scratch_pass
from .. import models

Image.MAX_IMAGE_PIXELS = None
DETECT_SIDE = 1200
FACE_MODEL = Path(__file__).resolve().parents[1] / 'data' / 'face_detection_yunet_2023mar.onnx'
FACE_SCORE = 0.6
# Recorded in each recipe, so a cleaned copy says what decided which faces were spared.
FACE_DETECTOR = (f'YuNet ({FACE_MODEL.name}, OpenCV Zoo) on a copy {DETECT_SIDE} px on its long side, '
                 f'all four rotations, score >= {FACE_SCORE}')


class CleanError(Exception):
    pass


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def _unrotate(pts, turns, shape):
    """Points found in np.rot90(img, turns), whose shape is `shape`, back in img's coordinates."""
    pts = np.asarray(pts, float)
    h, w = shape[:2]
    for _ in range(turns):   # one turn back: (x, y) -> (h - 1 - y, x)
        pts = np.stack([h - 1 - pts[:, 1], pts[:, 0]], 1)
        h, w = w, h
    return pts


def _overlap(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / smaller if smaller > 0 else 0.0


def find_faces(img):
    """[{'box': [x0, y0, x1, y1], 'kps': [[x, y] x 5], 'score'}] in pixels, best first. kps are the
    eyes, nose tip and mouth corners (eyes first, as restore.feature_mask and redeye expect)."""
    h, w = img.shape[:2]
    s = min(1.0, DETECT_SIDE / max(h, w))
    small = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else img
    bgr = np.ascontiguousarray(small[:, :, ::-1])
    found = []
    for turns in range(4):
        view = np.ascontiguousarray(np.rot90(bgr, turns))
        det = cv2.FaceDetectorYN.create(str(FACE_MODEL), '', (view.shape[1], view.shape[0]), FACE_SCORE, 0.3, 5000)
        _, faces = det.detect(view)
        for f in [] if faces is None else faces:
            x, y, fw, fh = (float(v) for v in f[:4])
            pts = _unrotate([[x, y], [x + fw, y], [x, y + fh], [x + fw, y + fh]]
                            + [[float(f[4 + 2 * i]), float(f[5 + 2 * i])] for i in range(5)], turns, view.shape) / s
            found.append({'box': [round(float(v), 1) for v in (*pts[:4].min(0), *pts[:4].max(0))],
                          'kps': [[round(float(x), 1), round(float(y), 1)] for x, y in pts[4:]],
                          'score': round(float(f[14]), 3)})
    faces = []   # the same face seen in more than one rotation counts once, at its best score
    for f in sorted(found, key=lambda f: -f['score']):
        if all(_overlap(f['box'], g['box']) < 0.5 for g in faces):
            faces.append(f)
    return faces


OUTPUTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff')


def load(path):
    """RGB uint8, or uint16 for a 16-bit TIFF with a color profile (a converted copy). A 16-bit file
    without one is a linear scan master, which needs `convert` first."""
    if Path(path).suffix.lower() in ('.tif', '.tiff'):
        import tifffile
        try:
            with tifffile.TiffFile(path) as tif:
                page = tif.pages[0]
                img = page.asarray()
                icc = 34675 in page.tags
        except (OSError, ValueError, tifffile.TiffFileError) as e:
            raise CleanError(f'cannot read image: {e}') from e
        if img.ndim != 3 or img.shape[2] not in (3, 4) or img.dtype not in (np.uint8, np.uint16):
            raise CleanError(f'{img.dtype} image of shape {img.shape}; clean works on RGB conversions')
        if img.dtype == np.uint16 and not icc:
            raise CleanError('16-bit TIFF without a color profile: this looks like a linear scan master; '
                             'convert it first')
        return np.ascontiguousarray(img[..., :3])
    try:
        with Image.open(path) as im:
            if im.mode not in ('RGB', 'L', 'P', 'RGBA'):
                raise CleanError(f'{im.mode} image; clean works on 8-bit converted viewing copies')
            return np.array(im.convert('RGB'))   # writable: clean works on it in place
    except (OSError, ValueError) as e:
        raise CleanError(f'cannot read image: {e}') from e


def save(img, dest):
    from ..films.convert import srgb_icc, write_tiff
    if dest.suffix.lower() in ('.tif', '.tiff'):
        write_tiff(dest, img, srgb_icc())
        return
    im = Image.fromarray(restore.to8(img))
    if dest.suffix.lower() == '.png':
        im.save(dest, icc_profile=srgb_icc())
    else:
        im.save(dest, quality=95, subsampling=0, icc_profile=srgb_icc())


def default_output(source):
    """<stem>-clean-vN<suffix> beside the source, the first N not yet used."""
    source = Path(source)
    n = 1
    while any(source.with_name(f'{source.stem}-clean-v{n}{source.suffix}{x}').exists() for x in ('', '.json')):
        n += 1
    return source.with_name(f'{source.stem}-clean-v{n}{source.suffix}')


def clean(source, output=None, crop=True, dust=True, protect_faces=True, color=False,
          straighten=None, lift_shadows=None, red_eye=False, scratches=False, spare_long_lines=False,
          spots=None, say=None):
    """Clean `source` into a new file with a recipe beside it (<output>.json). Returns the recipe.
    `spots`: [(x, y) or (x, y, radius)] to fill by hand, in pixels of the finished copy (so they
    can be read off a copy cleaned earlier with the same options); the radius defaults to 0.4 % of
    the short side. `say` reports model downloads (scratches only)."""
    source = Path(source).expanduser().resolve()
    dest = Path(output).expanduser().resolve() if output else default_output(source)
    recipe_path = Path(str(dest) + '.json')
    if dest == source or dest.suffix.lower() not in OUTPUTS:
        raise CleanError('choose a separate .jpg, .png or .tif output')
    for p in (dest, recipe_path):
        if p.exists():
            raise CleanError(f'refusing to overwrite {p}')
    if straighten is not None and not -15 <= straighten <= 15:
        raise CleanError('straighten must be between -15 and 15 degrees')
    if lift_shadows is not None and not 0.3 <= lift_shadows <= 1:
        raise CleanError('lift-shadows gamma must be between 0.3 and 1')
    spots = [tuple(int(v) for v in s) for s in spots or []]
    if any(len(s) not in (2, 3) or (len(s) == 3 and s[2] < 1) for s in spots):
        raise CleanError('a spot is X,Y or X,Y,R, with a radius of at least 1 pixel')
    digest = sha(source)
    img = load(source)
    view = restore.to8   # decisions on 8 bits; for an 8-bit image this is the image itself
    h, w = img.shape[:2]
    bits = 16 if img.dtype == np.uint16 else 8
    steps = []

    # A 73 MP 16-bit scan is 440 MB, so the steps below work on one copy of it, in place where
    # they can (it was loaded for this run alone), and the 8-bit view is made once.
    small = view(img)
    stamp = restore.stamp_mask(small)
    faces = find_faces(small) if protect_faces else []
    box = restore.find_crop(small) if crop else None
    if dust:
        protect = stamp | restore.feature_mask(img.shape, faces)   # eyes and mouths
        mask = restore.dust_mask(small, protect=protect)
        del small, protect
        cleaned = restore.remove_dust(img, mask, out=img)
        steps.append({'step': 'dust', 'pixels_filled': int(mask.sum()),
                      'share': round(float(mask.mean()), 6), 'faces_protected': len(faces),
                      'face_detector': FACE_DETECTOR if protect_faces else None,
                      'stamp_pixels_protected': int(stamp.sum()), 'method': 'OpenCV inpaint (Telea)'})
    else:
        del small
        cleaned = img
    del img
    if color:
        cleaned, shift = restore.fix_color(cleaned, exclude=stamp)
        steps.append({'step': 'color', 'cast_shift_ab': [round(v, 2) for v in shift]})
    if crop:
        l, t, r, b = box
        cleaned = np.ascontiguousarray(cleaned[t:b, l:r])
        steps.append({'step': 'crop', 'box': [l, t, r, b], 'changed': [l, t, r, b] != [0, 0, w, h]})
    model_fill = None
    if scratches:
        try:
            nets = scratch_pass.load_nets(say)
        except models.ModelError as e:
            raise CleanError(str(e)) from e
        # Faces and the stamp again, on the cropped copy the pass works on.
        small = view(cleaned)
        on = find_faces(small) if protect_faces else []
        repaired, stats, filled = scratch_pass.repair(small, on, restore.stamp_mask(small), restore.dust_mask, nets,
                                                      spare_long_lines=spare_long_lines)
        if cleaned.dtype == np.uint8:
            cleaned = repaired
        else:   # every change is inside the filled mask
            cleaned[filled > 0] = repaired[filled > 0].astype(np.uint16) * 257
        del repaired, filled
        n = int(stats.get('pixels_filled', 0))
        model_fill = {'model': 'MI-GAN (Picsart AI Research), an inpainting model',
                      'where': 'scratches, fibers and debris found by the scratch-detection model',
                      'pixels': n, 'share': round(n / (small.shape[0] * small.shape[1]), 6)}
        steps.append({'step': 'scratches', **stats, 'faces': len(on), 'spare_long_lines': spare_long_lines,
                      'method': 'found by a scratch-detection model (UNet), filled in by MI-GAN (OpenCV DNN)',
                      'models': {k: {'file': m['file'], 'sha256': m['sha256'], 'source': m['source']}
                                 for k, m in models.MODELS.items()}})
    if straighten:
        cleaned = restore.straighten(cleaned, straighten)
        steps.append({'step': 'straighten', 'degrees': straighten})
    if lift_shadows is not None:
        cleaned = restore.lift_shadows(cleaned, gamma=lift_shadows)
        steps.append({'step': 'lift_shadows', 'gamma': lift_shadows})
    if red_eye:
        small = view(cleaned)
        found = find_faces(small)
        corrected, fixed = redeye.correct(small, found)
        if cleaned.dtype == np.uint8:
            cleaned = corrected
        else:   # only the pupils change
            changed = (corrected != small).any(axis=2)
            cleaned[changed] = corrected[changed].astype(np.uint16) * 257
        steps.append({'step': 'red_eye', 'faces_with_eyes': len(found),
                      'eyes_corrected': [list(e) for e in fixed]})
    if spots:
        oh, ow = cleaned.shape[:2]
        spots = [s if len(s) == 3 else (*s, max(2, round(min(oh, ow) * 0.004))) for s in spots]
        if any(not (0 <= x < ow and 0 <= y < oh) or r > min(oh, ow) // 10 for x, y, r in spots):
            raise CleanError(f'spots must lie inside the {ow}x{oh} cleaned copy, with a radius up to '
                             f'{min(oh, ow) // 10} pixels')
        holes = np.zeros((oh, ow), np.uint8)
        for x, y, r in spots:
            cv2.circle(holes, (x, y), r, 1, -1)
        cleaned = restore.remove_dust(cleaned, holes > 0, out=cleaned)
        steps.append({'step': 'spots', 'spots': [list(s) for s in spots], 'pixels_filled': int(holes.sum()),
                      'method': 'OpenCV inpaint (Telea), at spots chosen by hand'})

    dest.parent.mkdir(parents=True, exist_ok=True)
    save(cleaned, dest)
    if sha(source) != digest:
        raise CleanError('source changed during cleaning')
    source_recipe = Path(str(source) + '.json')
    record = {
        'openfilmkit': {'version': __version__, 'record_format': 2},
        'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'status': 'Cleaned copy — requires visual review',
        'source': str(source), 'source_sha256': digest,
        'source_recipe_sha256': sha(source_recipe) if source_recipe.exists() else None,
        'options': settings(crop, dust, protect_faces, color, straighten, lift_shadows, red_eye,
                            scratches, spare_long_lines),
        'steps': steps, 'faces': faces,
        'source_dimensions': [w, h], 'output_dimensions': [cleaned.shape[1], cleaned.shape[0]],
        'output': str(dest), 'output_sha256': sha(dest),
        'bits_per_channel': {'source': bits,
                             'output': 16 if cleaned.dtype == np.uint16 and dest.suffix.lower() in ('.tif', '.tiff') else 8},
        'libraries': {'opencv': cv2.__version__, 'numpy': np.__version__},
        # Only the damaged spots a model filled in, if any; everything else is the scan.
        'filled_in_by_model': model_fill, 'approved': False,
    }
    recipe_path.write_text(json.dumps(record, indent=2) + '\n')
    return record


def settings(crop=True, dust=True, protect_faces=True, color=False, straighten=None,
             lift_shadows=None, red_eye=False, scratches=False, spare_long_lines=False):
    """The clean options, as recorded in a recipe and compared by roll_sources()."""
    return {'crop': crop, 'dust': dust, 'protect_faces': protect_faces, 'color': color,
            'straighten': straighten, 'lift_shadows': lift_shadows, 'red_eye': red_eye,
            'scratches': scratches, 'spare_long_lines': spare_long_lines}


def roll_sources(roll_dir, wanted=None):
    """Conversions in a roll's derived/ folder without a cleaned copy made with `wanted` settings
    (a settings() dict; default settings when None). A copy made with other settings, such as an
    earlier --no-dust pass, doesn't count, so dust removal can be left for later."""
    wanted = wanted or settings()
    derived = Path(roll_dir) / 'derived'
    todo = []
    for p in sorted(derived.glob('*')):
        # Hidden files include the ._ sidecars macOS writes on exFAT drives.
        if p.name.startswith('.') or p.suffix.lower() not in OUTPUTS or '-clean-v' in p.stem:
            continue
        done = False
        for recipe in derived.glob(f'{p.stem}-clean-v*{p.suffix}.json'):
            if recipe.name.startswith('.'):
                continue
            try:
                done = json.loads(recipe.read_text()).get('options') == wanted
            except (OSError, ValueError):
                done = False
            if done:
                break
        if not done:
            todo.append(p)
    return todo
