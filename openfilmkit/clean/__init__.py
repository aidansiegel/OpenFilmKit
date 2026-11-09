"""Clean-up of converted viewing copies: dust, holder crop, optional color, straightening, red eye.

Order follows the archive pipeline this came from (clean_batch.py first_pass): protect the date
stamp and faces, remove dust from the original, optionally fix color, then crop. Manual
adjustments and red eye come after. Nothing is invented: dust is filled only from its immediate
surroundings (OpenCV inpainting), and there is no generative fill.

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
from . import redeye, restore

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


def load(path):
    try:
        with Image.open(path) as im:
            if im.mode not in ('RGB', 'L', 'P', 'RGBA'):
                raise CleanError(f'{im.mode} image; clean works on 8-bit converted viewing copies')
            return np.ascontiguousarray(np.asarray(im.convert('RGB')))
    except (OSError, ValueError) as e:
        raise CleanError(f'cannot read image: {e}') from e


def save(img, dest):
    from ..films.convert import srgb_icc
    im = Image.fromarray(img)
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
          straighten=None, lift_shadows=None, red_eye=False):
    """Clean `source` into a new file with a recipe beside it (<output>.json). Returns the recipe."""
    source = Path(source).expanduser().resolve()
    dest = Path(output).expanduser().resolve() if output else default_output(source)
    recipe_path = Path(str(dest) + '.json')
    if dest == source or dest.suffix.lower() not in ('.jpg', '.jpeg', '.png'):
        raise CleanError('choose a separate .jpg or .png output')
    for p in (dest, recipe_path):
        if p.exists():
            raise CleanError(f'refusing to overwrite {p}')
    if straighten is not None and not -15 <= straighten <= 15:
        raise CleanError('straighten must be between -15 and 15 degrees')
    if lift_shadows is not None and not 0.3 <= lift_shadows <= 1:
        raise CleanError('lift-shadows gamma must be between 0.3 and 1')
    digest = sha(source)
    img = load(source)
    h, w = img.shape[:2]
    steps = []

    stamp = restore.stamp_mask(img)
    faces = find_faces(img) if protect_faces else []
    if dust:
        protect = stamp | restore.feature_mask(img.shape, faces)   # eyes and mouths
        mask = restore.dust_mask(img, protect=protect)
        cleaned = restore.remove_dust(img, mask)
        steps.append({'step': 'dust', 'pixels_filled': int(mask.sum()),
                      'share': round(float(mask.mean()), 6), 'faces_protected': len(faces),
                      'face_detector': FACE_DETECTOR if protect_faces else None,
                      'stamp_pixels_protected': int(stamp.sum()), 'method': 'OpenCV inpaint (Telea)'})
    else:
        cleaned = img
    if color:
        cleaned, shift = restore.fix_color(cleaned, exclude=stamp)
        steps.append({'step': 'color', 'cast_shift_ab': [round(v, 2) for v in shift]})
    if crop:
        l, t, r, b = restore.find_crop(img)
        cleaned = np.ascontiguousarray(cleaned[t:b, l:r])
        steps.append({'step': 'crop', 'box': [l, t, r, b], 'changed': [l, t, r, b] != [0, 0, w, h]})
    if straighten:
        cleaned = restore.straighten(cleaned, straighten)
        steps.append({'step': 'straighten', 'degrees': straighten})
    if lift_shadows is not None:
        cleaned = restore.lift_shadows(cleaned, gamma=lift_shadows)
        steps.append({'step': 'lift_shadows', 'gamma': lift_shadows})
    if red_eye:
        found = find_faces(cleaned)
        cleaned, fixed = redeye.correct(cleaned, found)
        steps.append({'step': 'red_eye', 'faces_with_eyes': len(found),
                      'eyes_corrected': [list(e) for e in fixed]})

    dest.parent.mkdir(parents=True, exist_ok=True)
    save(cleaned, dest)
    if sha(source) != digest:
        raise CleanError('source changed during cleaning')
    source_recipe = Path(str(source) + '.json')
    record = {
        'openfilmkit': {'version': __version__, 'record_format': 1},
        'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'status': 'Cleaned copy — requires visual review',
        'source': str(source), 'source_sha256': digest,
        'source_recipe_sha256': sha(source_recipe) if source_recipe.exists() else None,
        'options': settings(crop, dust, protect_faces, color, straighten, lift_shadows, red_eye),
        'steps': steps, 'faces': faces,
        'source_dimensions': [w, h], 'output_dimensions': [cleaned.shape[1], cleaned.shape[0]],
        'output': str(dest), 'output_sha256': sha(dest),
        'libraries': {'opencv': cv2.__version__, 'numpy': np.__version__},
        'generative_processing': False, 'approved': False,
    }
    recipe_path.write_text(json.dumps(record, indent=2) + '\n')
    return record


def settings(crop=True, dust=True, protect_faces=True, color=False, straighten=None,
             lift_shadows=None, red_eye=False):
    """The clean options, as recorded in a recipe and compared by roll_sources()."""
    return {'crop': crop, 'dust': dust, 'protect_faces': protect_faces, 'color': color,
            'straighten': straighten, 'lift_shadows': lift_shadows, 'red_eye': red_eye}


def roll_sources(roll_dir, wanted=None):
    """Conversions in a roll's derived/ folder without a cleaned copy made with `wanted` settings
    (a settings() dict; default settings when None). A copy made with other settings, such as an
    earlier --no-dust pass, doesn't count, so dust removal can be left for later."""
    wanted = wanted or settings()
    derived = Path(roll_dir) / 'derived'
    todo = []
    for p in sorted(derived.glob('*')):
        # Hidden files include the ._ sidecars macOS writes on exFAT drives.
        if p.name.startswith('.') or p.suffix.lower() not in ('.jpg', '.jpeg', '.png') or '-clean-v' in p.stem:
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
