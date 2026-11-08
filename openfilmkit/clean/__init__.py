"""Clean-up of converted viewing copies: dust, holder crop, optional colour, straightening, red eye.

Order follows the archive pipeline this came from (clean_batch.py first_pass): protect the date
stamp and faces, remove dust from the original, optionally fix colour, then crop. Manual
adjustments and red eye come after. Nothing is invented: dust is filled only from its immediate
surroundings (OpenCV inpainting), and there is no generative fill.

Faces are found with OpenCV's bundled Haar cascades (no model download). They give face boxes but
not landmarks, so whole faces are protected from dust removal, where the archive pipeline (with
InsightFace landmarks) protected only eyes and mouths. Eyes for red-eye come from OpenCV's eye
cascade inside each face.

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


class CleanError(Exception):
    pass


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def find_faces(img):
    """[{'box': [x0, y0, x1, y1], 'eyes': [[x, y], [x, y]]}] in pixels; eyes ordered left, right,
    present only when two are found in the face's upper half."""
    h, w = img.shape[:2]
    s = min(1.0, DETECT_SIDE / max(h, w))
    gray = cv2.cvtColor(cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
                        if s < 1 else img, cv2.COLOR_RGB2GRAY)
    gray = cv2.equalizeHist(gray)
    face_cc = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    eye_cc = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')
    least = max(24, int(min(gray.shape) * 0.03))
    faces = []
    for x, y, fw, fh in face_cc.detectMultiScale(gray, 1.1, 5, minSize=(least, least)):
        face = {'box': [round(x / s), round(y / s), round((x + fw) / s), round((y + fh) / s)]}
        upper = gray[y:y + fh // 2, x:x + fw]
        eyes = eye_cc.detectMultiScale(upper, 1.1, 5, minSize=(max(6, fw // 10),) * 2)
        if len(eyes) >= 2:
            eyes = sorted(sorted(eyes, key=lambda e: -e[2] * e[3])[:2], key=lambda e: e[0])
            face['eyes'] = [[round((x + ex + ew / 2) / s), round((y + ey + eh / 2) / s)]
                            for ex, ey, ew, eh in eyes]
        faces.append(face)
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


def clean(source, output=None, crop=True, dust=True, protect_faces=True, colour=False,
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
        # Boxes only: feature_mask protects the whole face when it has no landmarks.
        protect = stamp | restore.feature_mask(img.shape, [{'box': f['box']} for f in faces])
        mask = restore.dust_mask(img, protect=protect)
        cleaned = restore.remove_dust(img, mask)
        steps.append({'step': 'dust', 'pixels_filled': int(mask.sum()),
                      'share': round(float(mask.mean()), 6), 'faces_protected': len(faces),
                      'stamp_pixels_protected': int(stamp.sum()), 'method': 'OpenCV inpaint (Telea)'})
    else:
        cleaned = img
    if colour:
        cleaned, shift = restore.fix_colour(cleaned, exclude=stamp)
        steps.append({'step': 'colour', 'cast_shift_ab': [round(v, 2) for v in shift]})
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
        found = [f for f in find_faces(cleaned) if 'eyes' in f]
        cleaned, fixed = redeye.correct(cleaned, [{'box': f['box'], 'kps': f['eyes']} for f in found])
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
        'options': settings(crop, dust, protect_faces, colour, straighten, lift_shadows, red_eye),
        'steps': steps, 'faces': faces,
        'source_dimensions': [w, h], 'output_dimensions': [cleaned.shape[1], cleaned.shape[0]],
        'output': str(dest), 'output_sha256': sha(dest),
        'libraries': {'opencv': cv2.__version__, 'numpy': np.__version__},
        'generative_processing': False, 'approved': False,
    }
    recipe_path.write_text(json.dumps(record, indent=2) + '\n')
    return record


def settings(crop=True, dust=True, protect_faces=True, colour=False, straighten=None,
             lift_shadows=None, red_eye=False):
    """The clean options, as recorded in a recipe and compared by roll_sources()."""
    return {'crop': crop, 'dust': dust, 'protect_faces': protect_faces, 'colour': colour,
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
