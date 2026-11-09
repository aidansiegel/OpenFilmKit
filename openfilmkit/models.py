"""Trained models that `clean --scratches` uses, fetched once and checked against their SHA-256.

They are too big to ship in the package (about 100 MB together), so they live as assets of the
OpenFilmKit GitHub release named in MODELS and are saved under OPENFILMKIT_MODELS, or
~/.local/share/openfilmkit/models. Both run in OpenCV (cv2.dnn); nothing else is needed.

- scratch: Microsoft's scratch-detection UNet from "Bringing Old Photos Back to Life" (MIT), the
  official checkpoint converted to ONNX at a 512 px tile with half-precision weights.
- fill: the MI-GAN network (Picsart AI Research, MIT), taken unchanged out of the authors'
  migan_pipeline_v2.onnx and fixed at 512 x 512.
tools/build_models.py rebuilds both from the authors' files.
"""
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

from . import __version__

RELEASE = 'https://github.com/aidansiegel/OpenFilmKit/releases/download/models-1/'
MODELS = {
    'scratch': {'file': 'scratch_detection_512_fp16.onnx', 'bytes': 75372591,
                'sha256': 'dcf7d8e2ba56ac154a36d8848dd629aa51e06e4da3fd281dced9752e710b9006',
                'source': 'Microsoft, Bringing Old Photos Back to Life (MIT): global_checkpoints.zip, '
                          'checkpoints/detection/FT_Epoch_latest.pt'},
    'fill': {'file': 'migan_generator_512.onnx', 'bytes': 28033985,
             'sha256': 'e91485026851c1d5029d95ce3dcb4774df3d93f0b79e10fe3d15b879176d129e',
             'source': 'Picsart AI Research, MI-GAN (MIT): huggingface.co/andraniksargsyan/migan, '
                       'migan_pipeline_v2.onnx'},
}
LICENSES = ('scratch_detection_LICENSE', 'migan_LICENSE')


class ModelError(Exception):
    pass


def folder():
    given = os.environ.get('OPENFILMKIT_MODELS')
    return Path(given).expanduser() if given else Path.home() / '.local' / 'share' / 'openfilmkit' / 'models'


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def status():
    """{name: {'path', 'present', 'bytes', 'source'}}, without checking hashes (see path())."""
    base = folder()
    return {name: {'path': str(base / m['file']), 'present': (base / m['file']).is_file(),
                   'bytes': m['bytes'], 'source': m['source']} for name, m in MODELS.items()}


def _download(url, dest, size=None, say=None):
    part = dest.with_name(dest.name + '.part')
    req = urllib.request.Request(url, headers={'User-Agent': f'openfilmkit/{__version__}'})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, 'wb') as out:
        done, shown = 0, -1
        while chunk := r.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if say and size and done * 10 // size != shown:
                shown = done * 10 // size
                say(f'  {dest.name}: {done * 100 // size}%')
    return part


def fetch(names=None, say=None):
    """Download any missing models (and their licenses) and check them. Returns {name: path}."""
    base = folder()
    base.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in names or MODELS:
        m = MODELS[name]
        dest = base / m['file']
        if not (dest.is_file() and dest.stat().st_size == m['bytes'] and sha256(dest) == m['sha256']):
            if say:
                say(f"Downloading {m['file']} ({m['bytes'] / 1e6:.0f} MB) to {base}")
            try:
                part = _download(RELEASE + m['file'], dest, m['bytes'], say)
            except OSError as e:
                raise ModelError(f"could not download {m['file']}: {e}") from e
            if sha256(part) != m['sha256']:
                part.unlink(missing_ok=True)
                raise ModelError(f"{m['file']} did not match its checksum; nothing was kept")
            os.replace(part, dest)
        paths[name] = dest
    for lic in LICENSES:
        if not (base / lic).is_file():
            try:
                os.replace(_download(RELEASE + lic, base / lic), base / lic)
            except OSError:
                pass   # the licenses are also in the README; a missing copy is not fatal
    return paths


def path(name, say=None):
    """The checked model file, fetched first if missing or damaged."""
    return fetch([name], say)[name]


def say_to_stderr(msg):
    print(msg, file=sys.stderr, flush=True)
