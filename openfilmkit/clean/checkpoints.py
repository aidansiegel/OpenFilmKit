"""Stage checkpoints for clean, so a run that stops partway picks up where it was.

clean saves the result of each slow stage (the faces, crop box and date stamp; the dust mask; the
scratch model's probabilities, at the model's own 1700 px size; the scratch mask; and each window
MI-GAN fills) as a file in a folder. A later run with the same folder loads what is there instead
of working it out again, and the result is the same, byte for byte, as a run that never stopped.

Each file is written under a temporary name and renamed, so a file is whole or absent. Each
records the OpenFilmKit version, build and model checksums that made it, and a run with anything else
ignores it and works the stage out again. The folder must belong to one photo and one set of
options: a job package's work/checkpoints/ does (its name is the job's checksum).
"""
import json
import os
import socket
import zipfile
from pathlib import Path

import numpy as np

from .. import __version__, models


def made_with():
    from ..jobs import build
    return {'openfilmkit': __version__, 'build': build(), 'models': {k: m['sha256'] for k, m in models.MODELS.items()}}


class Checkpoints:
    """Stage results in `folder`. load() and save() take a stage name and named numpy arrays
    (booleans are stored packed); small values go in as JSON with save(..., info={...})."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.resumed = []   # [{'stage', 'made_on'}] loaded from earlier runs

    def _path(self, stage):
        return self.folder / f'{stage}.npz'

    def has(self, stage):
        return self.load(stage, note=False) is not None

    def load(self, stage, note=True):
        """{name: array, 'info': dict} saved for `stage`, or None when there is none usable."""
        path = self._path(stage)
        if not path.is_file():
            return None
        try:
            # Opened here, so it's closed when np.load gives up on it too.
            with open(path, 'rb') as f, np.load(f, allow_pickle=False) as z:
                meta = json.loads(str(z['_meta']))
                if meta['made_with'] != made_with():
                    return None
                out = {'info': meta['info'], 'made_on': meta['made_on']}
                for name in meta['arrays']:
                    if name in meta['packed']:
                        shape = meta['packed'][name]
                        out[name] = np.unpackbits(z[name], count=int(np.prod(shape))).reshape(shape).astype(bool)
                    else:
                        out[name] = z[name]
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
            # Unreadable (cut short in transit, say: rsync --partial keeps what arrived of a file
            # under its own name): work the stage out again.
            return None
        if note:
            self.resumed.append({'stage': stage, 'made_on': meta['made_on']})
        return out

    def save(self, stage, info=None, **arrays):
        self.folder.mkdir(parents=True, exist_ok=True)
        stored, packed = {}, {}
        for name, a in arrays.items():
            a = np.asarray(a)
            if a.dtype == bool:
                packed[name] = list(a.shape)
                a = np.packbits(a, axis=None)
            stored[name] = a
        meta = {'made_with': made_with(), 'made_on': socket.gethostname(), 'info': info or {},
                'arrays': list(arrays), 'packed': packed}
        path = self._path(stage)
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        try:
            with open(tmp, 'wb') as f:
                np.savez(f, _meta=np.array(json.dumps(meta)), **stored)
            os.replace(tmp, path)
        except OSError:
            # No room for it, say. A checkpoint only spares a later run some work: this run goes on
            # without it rather than stop for want of it.
            tmp.unlink(missing_ok=True)

    def journal(self, prefix):
        """A Journal of numbered entries named prefix-000000.npz, ... (see fill_damage)."""
        return Journal(self, prefix)


class Journal:
    """Numbered stage results: one file per finished step of a long stage, so a stop loses at
    most the step that was under way."""

    def __init__(self, checkpoints, prefix):
        self.checkpoints, self.prefix = checkpoints, prefix
        self.replayed, self.live = 0, True

    def get(self, n):
        """Entry n, or None once any entry before it was missing or rejected (later entries
        were made from a history this run no longer shares)."""
        saved = self.checkpoints.load(f'{self.prefix}-{n:06d}', note=False) if self.live else None
        if saved is None:
            self.live = False
            return None
        if not self.replayed:
            self.checkpoints.resumed.append({'stage': self.prefix, 'made_on': saved['made_on']})
        self.replayed += 1
        return saved

    def reject(self):
        """The entry just read doesn't fit this run: stop reading."""
        self.live = False

    def put(self, n, info=None, **arrays):
        self.checkpoints.save(f'{self.prefix}-{n:06d}', info=info, **arrays)
